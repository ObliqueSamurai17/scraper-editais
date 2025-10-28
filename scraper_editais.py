# -*- coding: utf-8 -*-
# scraper_editais.py
# Coletor PDF-first com coleta automatizada diária
# Requisitos: requests, beautifulsoup4, lxml, flask, pypdf, APScheduler

import os
import re
import io
import sqlite3
import time
import hashlib
import csv
import json
from urllib.parse import urljoin, urlparse, unquote
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, Response
from pypdf import PdfReader
from apscheduler.schedulers.background import BackgroundScheduler
import sys

# ==================== CONFIGURAÇÕES ====================
DB_PATH = "editais.db"
USER_AGENT = "scraper-multi/1.0 (+seu-email@exemplo.com)"
REQUEST_TIMEOUT = 12
MAX_PER_SOURCE = 40
COLETA_HORA = 6  # Hora da coleta automatizada (6h da manhã)

# ==================== REGEX ÚTEIS ====================
DATE_RE = re.compile(r'\b(?:\d{1,2}[\/\-\.\s]\d{1,2}[\/\-\.\s]\d{2,4}|\d{1,2}\sde\s(?:janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\sde\s\d{4})', re.I)
BR_CURRENCY_RE = re.compile(r'R\$\s?[0-9\.\,]{1,20}', re.I)
EN_CURRENCY_RE = re.compile(r'(US\$|\$|EUR|€)\s?[0-9\.,]{1,20}', re.I)

GENERIC_TITLES = {"chamada", "chamadas", "edital", "acesse aqui", "saiba mais", "resultado", "call"}

MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12
}

# ==================== UTILITÁRIOS ====================
def normalize_text(s):
    return None if s is None else " ".join(s.strip().split())

def make_fingerprint(link, title):
    key = (normalize_text(title) or "") + "||" + (link or "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def filename_from_url(u):
    p = urlparse(u).path
    return unquote(os.path.basename(p)) if p else None

def parse_brazilian_date(date_str):
    if not date_str:
        return None
    
    date_str = date_str.strip()
    
    match = re.search(r'(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})', date_str, re.I)
    if match:
        day = int(match.group(1))
        month_name = match.group(2).lower()
        year = int(match.group(3))
        month = MESES_PT.get(month_name)
        if month:
            try:
                return datetime(year, month, day)
            except ValueError:
                pass
    
    for sep in ['/', '-', '.']:
        if sep in date_str:
            parts = date_str.split(sep)
            if len(parts) == 3:
                try:
                    day, month, year = map(int, parts)
                    if year < 100:
                        year += 2000 if year < 50 else 1900
                    return datetime(year, month, day)
                except (ValueError, IndexError):
                    pass
    
    return None

def is_date_future(date_str):
    dt = parse_brazilian_date(date_str)
    if not dt:
        return None
    hoje = datetime.now()
    return dt >= (hoje - timedelta(days=1))

# ==================== BANCO DE DADOS ====================
def init_db():
    """Cria tabelas se não existirem e faz migração se necessário"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS editais (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        titulo TEXT,
        agencia TEXT,
        prazo TEXT,
        valor TEXT,
        link TEXT UNIQUE,
        fonte TEXT,
        data_publicacao TEXT,
        fingerprint TEXT UNIQUE,
        criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    try:
        cur.execute("SELECT data_publicacao FROM editais LIMIT 1")
    except sqlite3.OperationalError:
        print("⚙️ Adicionando coluna 'data_publicacao' na tabela editais...")
        cur.execute("ALTER TABLE editais ADD COLUMN data_publicacao TEXT")
        conn.commit()
        print("✅ Migração concluída!")
    
    cur.execute("""
    CREATE TABLE IF NOT EXISTS config (
        chave TEXT PRIMARY KEY,
        valor TEXT,
        atualizado_em DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    
    conn.commit()
    conn.close()

def get_ultima_coleta():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT valor FROM config WHERE chave = 'ultima_coleta'")
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"Erro ao buscar última coleta: {e}")
        return None

def set_ultima_coleta():
    try:
        agora = datetime.now().strftime("%d/%m/%Y às %H:%M")
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO config (chave, valor, atualizado_em)
            VALUES ('ultima_coleta', ?, CURRENT_TIMESTAMP)
        """, (agora,))
        conn.commit()
        conn.close()
        print(f"✅ Última coleta registrada: {agora}")
    except Exception as e:
        print(f"⚠️ Erro ao registrar última coleta: {e}")

def exists_fp(fp):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM editais WHERE fingerprint = ? LIMIT 1", (fp,))
    r = cur.fetchone() is not None
    conn.close()
    return r

def salvar(d):
    link = d.get("link")
    if not link or link.strip() == "":
        print(f"     ⚠️ Link vazio, não salvando: {d.get('titulo', 'sem título')[:50]}")
        return False
    
    fp = make_fingerprint(link, d.get("titulo"))
    if exists_fp(fp):
        return False
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT INTO editais (titulo, agencia, prazo, valor, link, fonte, data_publicacao, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (d.get("titulo"), d.get("agencia"), d.get("prazo"), 
              d.get("valor"), link, d.get("fonte"), 
              d.get("data_publicacao"), fp))
        conn.commit()
        print(f"     🔗 Link salvo: {link[:80]}")
    except sqlite3.OperationalError:
        try:
            cur.execute("""
            INSERT INTO editais (titulo, agencia, prazo, valor, link, fonte, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (d.get("titulo"), d.get("agencia"), d.get("prazo"), 
                  d.get("valor"), link, d.get("fonte"), fp))
            conn.commit()
            print(f"     🔗 Link salvo: {link[:80]}")
        except sqlite3.IntegrityError:
            conn.close()
            return False
    except sqlite3.IntegrityError:
        conn.close()
        return False
    conn.close()
    return True

# ==================== DOWNLOAD / PDF → TEXTO ====================
def download_bytes(url, timeout=REQUEST_TIMEOUT):
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"  Erro download: {e}")
        return None

def extract_text_from_pdf_bytes(b, max_pages=5):
    try:
        reader = PdfReader(io.BytesIO(b))
        texts = []
        for i, p in enumerate(reader.pages):
            try:
                texts.append(p.extract_text() or "")
            except Exception:
                pass
            if i + 1 >= max_pages:
                break
        return "\n".join(texts)
    except Exception as e:
        print(f"  Erro ao ler PDF: {e}")
        return None

def extract_first_title_from_text(text):
    if not text:
        return None
    head = text[:8000]
    lines = [ln.strip() for ln in head.splitlines() if ln.strip()]
    if not lines:
        return None
    for ln in lines[:12]:
        low = ln.lower()
        if len(ln) > 6 and low not in GENERIC_TITLES:
            return ln
    return lines[0] if lines else None

def extract_data_publicacao(text):
    if not text:
        return None
    
    head = text[:3000]
    
    patterns = [
        r'publicad[oa]\s+em\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
        r'data\s+de\s+publica[çc][ãa]o\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
        r'publica[çc][ãa]o\s*:?\s*(\d{1,2}[\/\-\.]\d{1,2}[\/\-\.]\d{2,4})',
        r'(\d{1,2}\s+de\s+\w+\s+de\s+\d{4})',
    ]
    
    for pattern in patterns:
        match = re.search(pattern, head, re.I)
        if match:
            data_str = match.group(1)
            if parse_brazilian_date(data_str):
                return data_str
    
    dates_found = DATE_RE.findall(head)
    if dates_found:
        for date_str in dates_found[:3]:
            if parse_brazilian_date(date_str):
                return date_str
    
    return None

def extract_prazo_and_valor(text):
    prazo = None
    valor = None
    
    if not text:
        return prazo, valor
    
    dates_found = DATE_RE.findall(text)
    future_dates = []
    for date_str in dates_found:
        dt = parse_brazilian_date(date_str)
        if dt and is_date_future(date_str):
            future_dates.append((dt, date_str))
    
    if future_dates:
        future_dates.sort(key=lambda x: x[0])
        prazo = future_dates[0][1]
    elif dates_found:
        prazo = dates_found[-1]
    
    m2 = BR_CURRENCY_RE.search(text)
    if m2:
        valor = m2.group(0).strip()
    else:
        m3 = EN_CURRENCY_RE.search(text)
        if m3:
            valor = m3.group(0).strip()
    
    return prazo, valor

# ==================== ANÁLISE HTML ====================
def find_pdf_links_on_page(base_url, html_text):
    soup = BeautifulSoup(html_text, "lxml")
    links = []
    base = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(base_url))
    
    for a in soup.find_all("a", href=True):
        href = a['href'].strip()
        if href.lower().endswith(".pdf"):
            full = href if href.startswith("http") else urljoin(base, href)
            title = a.get_text(" ", strip=True) or filename_from_url(full) or ""
            if full and full.startswith("http"):
                links.append((full, title))
            else:
                print(f"     ⚠️ URL inválida ignorada: {full}")
    
    for a in soup.find_all("a", href=True):
        href = a['href'].strip()
        if '?' in href and 'pdf' in href.lower():
            full = href if href.startswith("http") else urljoin(base, href)
            title = a.get_text(" ", strip=True) or filename_from_url(full) or ""
            if (full, title) not in links and full and full.startswith("http"):
                links.append((full, title))
            elif not full or not full.startswith("http"):
                print(f"     ⚠️ URL inválida ignorada: {full}")
    
    return links

def find_candidate_links_by_keywords(base_url, html_text, keywords):
    soup = BeautifulSoup(html_text, "lxml")
    base = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(base_url))
    candidates = []
    seen = set()
    
    for sel in ("#content", "main", ".content", ".container", ".region", ".portlet"):
        for block in soup.select(sel):
            for a in block.find_all("a", href=True):
                href = a['href'].strip()
                txt = a.get_text(" ", strip=True)
                full = href if href.startswith("http") else urljoin(base, href)
                
                if not full or not full.startswith("http"):
                    continue
                
                key = full.split("#")[0].rstrip("/")
                
                if key in seen:
                    continue
                seen.add(key)
                
                lowtxt = (txt or "").lower()
                lowhref = full.lower()
                
                if any(k in lowtxt for k in keywords) or any(k.replace(" ", "") in lowhref for k in keywords):
                    candidates.append((full, txt or filename_from_url(full) or ""))
    
    if not candidates:
        for a in soup.find_all("a", href=True):
            href = a['href'].strip()
            txt = a.get_text(" ", strip=True)
            full = href if href.startswith("http") else urljoin(base, href)
            
            if not full or not full.startswith("http"):
                continue
            
            lowtxt = (txt or "").lower()
            lowhref = full.lower()
            
            if any(k in lowtxt for k in keywords) or any(k.replace(" ", "") in lowhref for k in keywords):
                candidates.append((full, txt or filename_from_url(full) or ""))
    
    return candidates

# ==================== LISTA DE FONTES ====================
SOURCES = [
    {"url": "http://memoria2.cnpq.br/web/guest/chamadas-publicas", "fonte": "CNPq", "keywords": ["edital", "chamada", "chamadas", "call"]},
    {"url": "https://www.gov.br/capes/pt-br/assuntos/editais", "fonte": "CAPES", "keywords": ["edital", "chamada"]},
    {"url": "https://www.finep.gov.br/pt-br", "fonte": "FINEP", "keywords": ["edital", "chamada", "chamadas", "programa"]},
    {"url": "https://www.confap.org.br/", "fonte": "CONFAP", "keywords": ["edital", "chamada", "chamadas"]},
    {"url": "https://fapesp.br/chamadas", "fonte": "FAPESP (SP)", "keywords": ["chamada", "edital", "call", "proposal"]},
    {"url": "https://www.faperj.br/", "fonte": "FAPERJ (RJ)", "keywords": ["edital", "chamada", "resultado"]},
    {"url": "https://fapemig.br/", "fonte": "FAPEMIG (MG)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapes.es.gov.br/", "fonte": "FAPES (ES)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.facepe.br/", "fonte": "FACEPE (PE)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapesq.rpp.br/", "fonte": "FAPESQ (PB)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapeal.br/", "fonte": "FAPEAL (AL)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.funcap.ce.gov.br/", "fonte": "FUNCAP (CE)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapesb.ba.gov.br/", "fonte": "FAPESB (BA)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapern.rn.gov.br/", "fonte": "FAPERN (RN)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapepi.pi.gov.br/", "fonte": "FAPEPI (PI)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapitec.se.gov.br/", "fonte": "FAPITEC (SE)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapema.br/", "fonte": "FAPEMA (MA)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapergs.rs.gov.br/", "fonte": "FAPERGS (RS)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapesc.sc.gov.br/", "fonte": "FAPESC (SC)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fappr.pr.gov.br/", "fonte": "Fundação Araucária (PR)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fap.df.gov.br/", "fonte": "FAPDF (DF)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fundect.ms.gov.br/", "fonte": "FUNDECT (MS)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapeg.go.gov.br/", "fonte": "FAPEG (GO)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapeam.am.gov.br/", "fonte": "FAPEAM (AM)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapespa.pa.gov.br/", "fonte": "FAPESPA (PA)", "keywords": ["edital", "chamada"]},
    {"url": "https://fapac.ac.gov.br/", "fonte": "FAPEAC (AC)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapero.ro.gov.br/", "fonte": "FAPERO (RO)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.to.gov.br/fapt", "fonte": "FAPTO (TO)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.fapeap.ap.gov.br/", "fonte": "FAPEAP (AP)", "keywords": ["edital", "chamada"]},
    {"url": "https://www.usaid.gov/work-usaid/partnership-opportunities", "fonte": "USAID (EUA)", "keywords": ["call for proposals", "funding", "notice", "grant", "opportunity"]},
    {"url": "https://www.nsf.gov/funding/", "fonte": "NSF (EUA)", "keywords": ["funding", "opportunity", "solicitation", "call"]},
    {"url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/home", "fonte": "União Europeia (Horizon)", "keywords": ["call", "funding", "grant", "opportunity"]},
    {"url": "https://www.afd.fr/en/search?type=calls-for-projects", "fonte": "AFD (França)", "keywords": ["call", "notice", "funding", "appel à projets"]},
    {"url": "https://www.aecid.es/ES/convocatorias", "fonte": "AECID (Espanha)", "keywords": ["convocatoria", "call", "notice", "grant"]},
    {"url": "https://www.ukri.org/opportunity/", "fonte": "UKRI (Reino Unido)", "keywords": ["funding", "opportunity", "call"]},
    {"url": "https://www.nserc-crsng.gc.ca/Professors-Professeurs/Grants-Subs/index_eng.asp", "fonte": "NSERC (Canadá)", "keywords": ["funding", "opportunity", "competition"]},
    {"url": "https://aca-secretariat.be/", "fonte": "ACA (Academic Cooperation)", "keywords": ["call", "funding", "opportunity", "grant", "programme"]},
]

# ==================== FILTROS ====================
EDITAL_KEYWORDS_REQUIRED = ["edital", "chamada", "call", "convocatória", "seleção", "programa", "auxílio", "bolsa", "fomento", "pesquisa"]
BLACKLIST_KEYWORDS = ["manual", "instruções", "tutorial", "declaração de imposto", "indicadores institucionais", "relatório", "ata", "prestação de contas", "resultado preliminar", "homologação", "retificação", "errata", "como acessar", "passo a passo", "orientações", "formulário", "www.", "http://", "https://", "obedecendo determinação"]

def is_likely_edital(titulo, texto):
    if not titulo:
        return False
    titulo_lower = titulo.lower()
    texto_lower = (texto or "").lower()
    for palavra in BLACKLIST_KEYWORDS:
        if palavra in titulo_lower or palavra in texto_lower[:1000]:
            return False
    if len(titulo.strip()) < 10:
        return False
    generic_titles = ["chamada", "edital", "www.", "governo", "fundação"]
    if titulo.strip().lower() in generic_titles:
        return False
    has_keyword = any(kw in titulo_lower or kw in texto_lower[:2000] for kw in EDITAL_KEYWORDS_REQUIRED)
    if not has_keyword:
        return False
    has_number_pattern = bool(re.search(r'n[ºº]\s*\d+/\d{4}', titulo_lower))
    if texto:
        word_count = len(texto.split())
        if word_count < 500:
            return False
    score = 0
    if has_number_pattern:
        score += 3
    if "edital" in titulo_lower or "chamada" in titulo_lower:
        score += 2
    if any(word in texto_lower[:1000] for word in ["prazo", "submissão", "inscrição", "cronograma"]):
        score += 1
    if re.search(r'r\$\s*[0-9\.,]+', texto_lower[:2000]):
        score += 1
    return score >= 3

def clean_title(titulo):
    if not titulo:
        return None
    titulo = " ".join(titulo.split())
    if len(titulo) > 300 and titulo.count(" ") > 50:
        titulo = titulo[:150] + "..."
    return titulo

# ==================== COLETOR PRINCIPAL ====================
def coletar_pdf_first(timeout=REQUEST_TIMEOUT, max_per_source=MAX_PER_SOURCE, progress_callback=None):
    headers = {"User-Agent": USER_AGENT}
    results = []
    total_sources = len(SOURCES)
    
    for idx, src in enumerate(SOURCES, 1):
        url = src["url"]
        fonte = src.get("fonte")
        keywords = src.get("keywords", ["edital", "chamada"])
        
        if progress_callback:
            progress_callback(idx, total_sources)
        
        print(f"\n{'='*60}")
        print(f"[COLETA] Fonte: {fonte} ({idx}/{total_sources})")
        print(f"         URL: {url}")
        print(f"{'='*60}")
        
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            print(f"❌ Erro ao baixar lista: {e}")
            continue
        
        pdfs = find_pdf_links_on_page(url, html)
        print(f"✓ Encontrados {len(pdfs)} PDFs diretos")
        
        if not pdfs:
            print("⚠ Nenhum PDF direto. Buscando candidatos...")
            cand = find_candidate_links_by_keywords(url, html, keywords)
            print(f"✓ Encontrados {len(cand)} candidatos")
            checked = 0
            for full, txt in cand:
                if checked >= max_per_source:
                    break
                try:
                    h = requests.head(full, headers=headers, timeout=8, allow_redirects=True)
                    ctype = h.headers.get("content-type", "").lower()
                    if "pdf" in ctype or full.lower().endswith(".pdf"):
                        pdfs.append((full, txt))
                    checked += 1
                except Exception:
                    continue
        
        count = 0
        for pdf_url, link_text in pdfs:
            if count >= max_per_source:
                break
            title_guess = link_text or filename_from_url(pdf_url) or pdf_url
            print(f"\n  📄 Candidato: {title_guess[:80]}...")
            b = download_bytes(pdf_url, timeout=REQUEST_TIMEOUT)
            if not b:
                print("     ❌ Falhou download")
                continue
            text = extract_text_from_pdf_bytes(b)
            if not text:
                print("     ⚠ PDF sem texto extraível, ignorando...")
                continue
            titulo = extract_first_title_from_text(text) or title_guess
            titulo = clean_title(titulo)
            prazo, valor = extract_prazo_and_valor(text)
            data_pub = extract_data_publicacao(text)
            if not is_likely_edital(titulo, text):
                print(f"     ⏭ Não parece ser um edital, ignorando...")
                count += 1
                continue
            if not pdf_url or pdf_url.strip() == "":
                print(f"     ⚠️ PDF URL vazio, pulando...")
                count += 1
                continue
            doc = {
                "titulo": normalize_text(titulo) or title_guess,
                "agencia": fonte,
                "prazo": prazo,
                "valor": valor,
                "link": pdf_url,
                "fonte": fonte,
                "data_publicacao": data_pub
            }
            if prazo:
                is_valid = is_date_future(prazo)
                if is_valid is False:
                    print(f"     ⏭ Prazo vencido ({prazo}), ignorando...")
                    count += 1
                    continue
            if salvar(doc):
                print(f"     ✅ SALVO: {doc['titulo'][:100]}")
                if data_pub:
                    print(f"           📅 Publicado em: {data_pub}")
                results.append(doc)
            else:
                print(f"     ⏭ Já existe: {pdf_url[:80]}")
            count += 1
            time.sleep(0.7)
    
    set_ultima_coleta()
    return results

def job_coleta_automatizada():
    print("\n🤖 COLETA AUTOMATIZADA INICIADA")
    print(f"⏰ Horário: {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    try:
        novos = coletar_pdf_first()
        print(f"\n✅ Coleta automatizada concluída! Novos editais: {len(novos)}")
    except Exception as e:
        print(f"\n❌ Erro na coleta automatizada: {e}")

# ==================== FLASK ====================
app = Flask(__name__)

@app.route("/")
def index():
    termo = request.args.get("q", "").strip()
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        try:
            if termo:
                cur.execute("SELECT titulo, agencia, prazo, valor, link, fonte, data_publicacao FROM editais WHERE titulo LIKE ? OR agencia LIKE ? ORDER BY criado_em DESC", (f"%{termo}%", f"%{termo}%"))
            else:
                cur.execute("SELECT titulo, agencia, prazo, valor, link, fonte, data_publicacao FROM editais ORDER BY criado_em DESC")
        except sqlite3.OperationalError:
            if termo:
                cur.execute("SELECT titulo, agencia, prazo, valor, link, fonte, NULL as data_publicacao FROM editais WHERE titulo LIKE ? OR agencia LIKE ? ORDER BY criado_em DESC", (f"%{termo}%", f"%{termo}%"))
            else:
                cur.execute("SELECT titulo, agencia, prazo, valor, link, fonte, NULL as data_publicacao FROM editais ORDER BY criado_em DESC")
        rows = cur.fetchall()
        try:
            ultima_coleta = get_ultima_coleta()
        except:
            ultima_coleta = None
        conn.close()
        return render_template("index.html", editais=rows, termo=termo, ultima_coleta=ultima_coleta)
    except Exception as e:
        print(f"Erro na página index: {e}")
        return render_template("index.html", editais=[], termo=termo, ultima_coleta=None)

@app.route("/coletar_stream")
def coletar_stream():
    def generate():
        novos = []
        total = len(SOURCES)
        for i in range(1, total + 1):
            data = json.dumps({"type": "progress", "current": i, "total": total})
            yield f"data: {data}\n\n"
            if i <= len(SOURCES):
                src = SOURCES[i-1]
                url = src["url"]
                fonte = src.get("fonte")
                keywords = src.get("keywords", ["edital", "chamada"])
                headers = {"User-Agent": USER_AGENT}
                try:
                    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
                    r.raise_for_status()
                    html = r.text
                    pdfs = find_pdf_links_on_page(url, html)
                    if not pdfs:
                        cand = find_candidate_links_by_keywords(url, html, keywords)
                        checked = 0
                        for full, txt in cand:
                            if checked >= MAX_PER_SOURCE:
                                break
                            try:
                                h = requests.head(full, headers=headers, timeout=8, allow_redirects=True)
                                ctype = h.headers.get("content-type", "").lower()
                                if "pdf" in ctype or full.lower().endswith(".pdf"):
                                    pdfs.append((full, txt))
                                checked += 1
                            except Exception:
                                continue
                    count = 0
                    for pdf_url, link_text in pdfs:
                        if count >= MAX_PER_SOURCE:
                            break
                        b = download_bytes(pdf_url, timeout=REQUEST_TIMEOUT)
                        if not b:
                            continue
                        text = extract_text_from_pdf_bytes(b)
                        if not text:
                            continue
                        titulo = extract_first_title_from_text(text) or link_text
                        titulo = clean_title(titulo)
                        prazo, valor = extract_prazo_and_valor(text)
                        data_pub = extract_data_publicacao(text)
                        if not is_likely_edital(titulo, text):
                            count += 1
                            continue
                        if not pdf_url or pdf_url.strip() == "":
                            count += 1
                            continue
                        doc = {"titulo": normalize_text(titulo) or link_text, "agencia": fonte, "prazo": prazo, "valor": valor, "link": pdf_url, "fonte": fonte, "data_publicacao": data_pub}
                        if prazo:
                            is_valid = is_date_future(prazo)
                            if is_valid is False:
                                count += 1
                                continue
                        if salvar(doc):
                            novos.append(doc)
                        count += 1
                        time.sleep(0.7)
                except Exception as e:
                    print(f"Erro processando {fonte}: {e}")
        set_ultima_coleta()
        final_data = json.dumps({"type": "complete", "total": total, "novos": len(novos)})
        yield f"data: {final_data}\n\n"
    return Response(generate(), mimetype='text/event-stream')

@app.route("/coletar")
def rota_coletar():
    print("\n🚀 INICIANDO COLETA MANUAL...")
    novos = coletar_pdf_first()
    print(f"\n✅ Coleta finalizada! Novos editais: {len(novos)}")
    return f"<h1>Coleta finalizada!</h1><p>Novos editais: {len(novos)}</p><a href='/'>Voltar</a>"

@app.route("/limpar_vencidos")
def limpar_vencidos():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, prazo FROM editais WHERE prazo IS NOT NULL AND prazo != ''")
    rows = cur.fetchall()
    removidos = 0
    for id_edital, prazo in rows:
        is_valid = is_date_future(prazo)
        if is_valid is False:
            cur.execute("DELETE FROM editais WHERE id = ?", (id_edital,))
            removidos += 1
    conn.commit()
    conn.close()
    print(f"🗑️ Removidos {removidos} editais vencidos")
    return f"<h1>Limpeza concluída!</h1><p>Editais removidos: {removidos}</p><a href='/'>Voltar</a>"

@app.route("/export.csv")
def export_csv():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("SELECT titulo, agencia, prazo, valor, link, fonte, data_publicacao, criado_em FROM editais ORDER BY criado_em DESC")
        headers = ["titulo", "agencia", "prazo", "valor", "link", "fonte", "data_publicacao", "criado_em"]
    except sqlite3.OperationalError:
        cur.execute("SELECT titulo, agencia, prazo, valor, link, fonte, criado_em FROM editais ORDER BY criado_em DESC")
        headers = ["titulo", "agencia", "prazo", "valor", "link", "fonte", "criado_em"]
    rows = cur.fetchall()
    conn.close()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(headers)
    cw.writerows(rows)
    output = si.getvalue().encode("utf-8")
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=editais.csv"})

@app.route("/debug")
def debug_links():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, titulo, link, fonte FROM editais ORDER BY id DESC LIMIT 10")
        rows = cur.fetchall()
    except Exception as e:
        return f"<h1>Erro ao buscar dados</h1><p>{e}</p>"
    conn.close()
    html = "<html><head><title>Debug</title></head><body><h1>🔍 Debug - Últimos 10 editais</h1><table border='1' cellpadding='10' style='border-collapse: collapse;'><tr><th>ID</th><th>Título</th><th>Link</th><th>Fonte</th></tr>"
    for row in rows:
        id_edital, titulo, link, fonte = row
        link_status = "✅ OK" if link and link.strip() else "❌ VAZIO"
        html += f"<tr><td>{id_edital}</td><td>{titulo[:50]}...</td><td style='color: {'green' if link else 'red'};'>{link_status}<br><small>{link[:80] if link else 'NULL'}</small></td><td>{fonte}</td></tr>"
    html += "</table><br><a href='/'>← Voltar</a></body></html>"
    return html

@app.route("/limpar_banco")
def limpar_banco():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM editais")
        total_antes = cur.fetchone()[0]
        cur.execute("DELETE FROM editais")
        conn.commit()
        cur.execute("DELETE FROM config WHERE chave = 'ultima_coleta'")
        conn.commit()
        conn.close()
        html = f"<html><head><title>Banco Limpo</title></head><body style='font-family: Arial; padding: 40px; text-align: center;'><h1 style='color: #48bb78;'>✅ Banco limpo!</h1><p style='font-size: 1.2em;'>🗑️ <strong>{total_antes}</strong> editais removidos</p><p style='margin-top: 30px;'><a href='/' style='padding: 12px 24px; background: #667eea; color: white; text-decoration: none; border-radius: 8px;'>← Voltar</a></p><p style='margin-top: 20px; color: #ed8936;'>⚠️ Execute nova coleta</p></body></html>"
        print(f"🗑️ Banco limpo! {total_antes} editais removidos")
        return html
    except Exception as e:
        return f"<h1>❌ Erro</h1><p>{e}</p><br><a href='/'>Voltar</a>"

@app.route("/download")
def download_pdf():
    """Proxy HTTPS para baixar PDFs"""
    url = request.args.get('url', '')
    if not url:
        return "<h1>❌ URL não fornecida</h1><a href='/'>Voltar</a>", 400
    try:
        print(f"📥 Baixando PDF via proxy: {url}")
        headers = {"User-Agent": USER_AGENT}
        r = requests.get(url, headers=headers, timeout=30, stream=True, allow_redirects=True)
        r.raise_for_status()
        filename = filename_from_url(url) or "edital.pdf"
        if not filename.lower().endswith('.pdf'):
            filename = filename + '.pdf'
        return Response(r.content, mimetype='application/pdf', headers={'Content-Disposition': f'attachment; filename="{filename}"', 'Content-Type': 'application/pdf'})
    except requests.exceptions.Timeout:
        return f"<html><head><title>Timeout</title></head><body style='font-family: Arial; padding: 40px; text-align: center;'><h1>⏱️ Tempo esgotado</h1><p>Tente: <a href='{url}' target='_blank'>{url[:80]}...</a></p><p><a href='/'>← Voltar</a></p></body></html>", 504
    except Exception as e:
        print(f"❌ Erro: {e}")
        return f"<html><head><title>Erro</title></head><body style='font-family: Arial; padding: 40px; text-align: center;'><h1>⚠️ Erro ao baixar</h1><p><small>{str(e)[:100]}</small></p><p>Tente: <a href='{url}' target='_blank' style='word-break: break-all;'>{url}</a></p><p><a href='/'>← Voltar</a></p></body></html>", 500

# ==================== INICIALIZAÇÃO ====================
if __name__ == "__main__":
    print("="*60)
    print("🎓 SCRAPER DE EDITAIS - Iniciando...")
    print("="*60)
    try:
        init_db()
        print("✅ Banco inicializado")
    except Exception as e:
        print(f"❌ Erro banco: {e}")
    try:
        scheduler = BackgroundScheduler()
        scheduler.add_job(func=job_coleta_automatizada, trigger="cron", hour=COLETA_HORA, minute=0, id="coleta_diaria", name="Coleta Automatizada Diária")
        scheduler.start()
        print(f"🤖 Coleta automatizada: {COLETA_HORA}:00 diariamente")
    except Exception as e:
        print(f"⚠️ Scheduler desabilitado: {e}")
    try:
        ultima = get_ultima_coleta()
        print(f"📅 Última coleta: {ultima}" if ultima else "📅 Sem histórico")
    except:
        print("📅 Sem histórico")
    print(f"📊 {len(SOURCES)} agências configuradas")
    nacionais = [s for s in SOURCES if any(x in s['fonte'] for x in ['CNPq', 'CAPES', 'FINEP', 'CONFAP', 'FAP', 'FUNC', 'Fund'])]
    internacionais = [s for s in SOURCES if s not in nacionais]
    print(f"📋 {len(nacionais)} nacionais + {len(internacionais)} internacionais")
    print("\n🌐 Servidor: http://127.0.0.1:5000")
    print("💡 CTRL+C para parar\n")
    try:
        app.run(host="0.0.0.0", port=10000, debug=False)
    except (KeyboardInterrupt, SystemExit):
        try:
            scheduler.shutdown()
        except:
            pass
        print("\n👋 Servidor encerrado")
