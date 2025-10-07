# -*- coding: utf-8 -*-
# scraper_editais.py
# Coletor PDF-first para múltiplas agências (handlers genéricos + fontes específicas)
# Requisitos: requests, beautifulsoup4, lxml, flask, pypdf

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
import sys

# ==================== CONFIGURAÇÕES ====================
DB_PATH = "editais.db"
USER_AGENT = "scraper-multi/1.0 (+seu-email@exemplo.com)"
REQUEST_TIMEOUT = 12
MAX_PER_SOURCE = 40  # limite de PDFs por fonte por execução

# ==================== REGEX ÚTEIS ====================
DATE_RE = re.compile(r'\b(?:\d{1,2}[\/\-\.\s]\d{1,2}[\/\-\.\s]\d{2,4}|\d{1,2}\sde\s(?:janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)\sde\s\d{4})', re.I)
BR_CURRENCY_RE = re.compile(r'R\$\s?[0-9\.\,]{1,20}', re.I)
EN_CURRENCY_RE = re.compile(r'(US\$|\$|EUR|€)\s?[0-9\.,]{1,20}', re.I)

GENERIC_TITLES = {"chamada", "chamadas", "edital", "acesse aqui", "saiba mais", "resultado", "call"}

# Mapeamento de meses em português
MESES_PT = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
    "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12
}

# ==================== UTILITÁRIOS ====================
def normalize_text(s):
    """Remove espaços extras e normaliza texto"""
    return None if s is None else " ".join(s.strip().split())

def make_fingerprint(link, title):
    """Cria hash único para evitar duplicatas"""
    key = (normalize_text(title) or "") + "||" + (link or "")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def filename_from_url(u):
    """Extrai nome do arquivo da URL"""
    p = urlparse(u).path
    return unquote(os.path.basename(p)) if p else None

def parse_brazilian_date(date_str):
    """
    Tenta converter data brasileira para datetime.
    Suporta formatos: DD/MM/YYYY, DD-MM-YYYY, DD de MMMM de YYYY
    """
    if not date_str:
        return None
    
    date_str = date_str.strip()
    
    # Formato: "23 de outubro de 2024"
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
    
    # Formato: DD/MM/YYYY ou DD-MM-YYYY ou DD.MM.YYYY
    for sep in ['/', '-', '.']:
        if sep in date_str:
            parts = date_str.split(sep)
            if len(parts) == 3:
                try:
                    day, month, year = map(int, parts)
                    if year < 100:  # Ano com 2 dígitos
                        year += 2000 if year < 50 else 1900
                    return datetime(year, month, day)
                except (ValueError, IndexError):
                    pass
    
    return None

def is_date_future(date_str):
    """
    Verifica se a data está no futuro (prazo ainda válido).
    Retorna True se for futuro/hoje, False se passou.
    """
    dt = parse_brazilian_date(date_str)
    if not dt:
        return None  # Data inválida = não sabemos, então não filtrar
    
    hoje = datetime.now()
    # Considera válido se ainda falta até 1 dia (margem de segurança)
    return dt >= (hoje - timedelta(days=1))

# ==================== BANCO DE DADOS ====================
def init_db():
    """Cria a tabela se não existir"""
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
        fingerprint TEXT UNIQUE,
        criado_em DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def exists_fp(fp):
    """Verifica se fingerprint já existe"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM editais WHERE fingerprint = ? LIMIT 1", (fp,))
    r = cur.fetchone() is not None
    conn.close()
    return r

def salvar(d):
    """Salva edital no banco (retorna True se for novo)"""
    fp = make_fingerprint(d.get("link"), d.get("titulo"))
    if exists_fp(fp):
        return False
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT INTO editais (titulo, agencia, prazo, valor, link, fonte, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (d.get("titulo"), d.get("agencia"), d.get("prazo"), 
              d.get("valor"), d.get("link"), d.get("fonte"), fp))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return False
    conn.close()
    return True

# ==================== DOWNLOAD / PDF → TEXTO ====================
def download_bytes(url, timeout=REQUEST_TIMEOUT):
    """Baixa conteúdo binário de uma URL"""
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(url, headers=headers, timeout=timeout, stream=True)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"  Erro download: {e}")
        return None

def extract_text_from_pdf_bytes(b, max_pages=5):
    """Extrai texto das primeiras páginas do PDF"""
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
    """Tenta extrair o título das primeiras linhas"""
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

def extract_prazo_and_valor(text):
    """
    Extrai datas e valores do texto.
    Prioriza a data mais próxima no futuro (provável prazo de submissão).
    """
    prazo = None
    valor = None
    
    if not text:
        return prazo, valor
    
    # Busca TODAS as datas no texto
    dates_found = DATE_RE.findall(text)
    
    # Filtrar e converter para datetime
    future_dates = []
    for date_str in dates_found:
        dt = parse_brazilian_date(date_str)
        if dt and is_date_future(date_str):
            future_dates.append((dt, date_str))
    
    # Pegar a data mais próxima no futuro
    if future_dates:
        future_dates.sort(key=lambda x: x[0])  # Ordenar por data
        prazo = future_dates[0][1]  # Primeira data futura (mais próxima)
    elif dates_found:
        # Se não achou futuras, pega a última data mencionada (pode ser a mais recente)
        prazo = dates_found[-1]
    
    # Busca valores (R$ primeiro, depois USD/EUR)
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
    """Encontra links diretos para PDFs na página"""
    soup = BeautifulSoup(html_text, "lxml")
    links = []
    base = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(base_url))
    
    # Links com .pdf
    for a in soup.find_all("a", href=True):
        href = a['href'].strip()
        if href.lower().endswith(".pdf"):
            full = href if href.startswith("http") else urljoin(base, href)
            title = a.get_text(" ", strip=True) or filename_from_url(full) or ""
            links.append((full, title))
    
    # Fallback: query string contendo 'pdf'
    for a in soup.find_all("a", href=True):
        href = a['href'].strip()
        if '?' in href and 'pdf' in href.lower():
            full = href if href.startswith("http") else urljoin(base, href)
            title = a.get_text(" ", strip=True) or filename_from_url(full) or ""
            if (full, title) not in links:
                links.append((full, title))
    
    return links

def find_candidate_links_by_keywords(base_url, html_text, keywords):
    """Busca links que contenham palavras-chave relevantes"""
    soup = BeautifulSoup(html_text, "lxml")
    base = "{uri.scheme}://{uri.netloc}".format(uri=urlparse(base_url))
    candidates = []
    seen = set()
    
    # Busca em áreas específicas da página
    for sel in ("#content", "main", ".content", ".container", ".region", ".portlet"):
        for block in soup.select(sel):
            for a in block.find_all("a", href=True):
                href = a['href'].strip()
                txt = a.get_text(" ", strip=True)
                full = href if href.startswith("http") else urljoin(base, href)
                key = full.split("#")[0].rstrip("/")
                
                if key in seen:
                    continue
                seen.add(key)
                
                lowtxt = (txt or "").lower()
                lowhref = full.lower()
                
                # Keywords no texto ou URL
                if any(k in lowtxt for k in keywords) or any(k.replace(" ", "") in lowhref for k in keywords):
                    candidates.append((full, txt or filename_from_url(full) or ""))
    
    # Fallback: página inteira
    if not candidates:
        for a in soup.find_all("a", href=True):
            href = a['href'].strip()
            txt = a.get_text(" ", strip=True)
            full = href if href.startswith("http") else urljoin(base, href)
            lowtxt = (txt or "").lower()
            lowhref = full.lower()
            
            if any(k in lowtxt for k in keywords) or any(k.replace(" ", "") in lowhref for k in keywords):
                candidates.append((full, txt or filename_from_url(full) or ""))
    
    return candidates

# ==================== LISTA DE FONTES ====================
SOURCES = [
    # ========== AGÊNCIAS NACIONAIS ==========
    
    # Agências Federais
    {"url": "http://memoria2.cnpq.br/web/guest/chamadas-publicas", 
     "fonte": "CNPq", 
     "keywords": ["edital", "chamada", "chamadas", "call"]},
    
    {"url": "https://www.gov.br/capes/pt-br/assuntos/editais", 
     "fonte": "CAPES", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.finep.gov.br/pt-br", 
     "fonte": "FINEP", 
     "keywords": ["edital", "chamada", "chamadas", "programa"]},
    
    # CONFAP (Conselho Nacional das FAPs)
    {"url": "https://www.confap.org.br/", 
     "fonte": "CONFAP", 
     "keywords": ["edital", "chamada", "chamadas"]},
    
    # ========== FUNDAÇÕES ESTADUAIS (FAPs) ==========
    
    # Região Sudeste
    {"url": "https://fapesp.br/chamadas", 
     "fonte": "FAPESP (SP)", 
     "keywords": ["chamada", "edital", "call", "proposal"]},
    
    {"url": "https://www.faperj.br/", 
     "fonte": "FAPERJ (RJ)", 
     "keywords": ["edital", "chamada", "resultado"]},
    
    {"url": "https://fapemig.br/", 
     "fonte": "FAPEMIG (MG)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapes.es.gov.br/", 
     "fonte": "FAPES (ES)", 
     "keywords": ["edital", "chamada"]},
    
    # Região Nordeste
    {"url": "https://www.facepe.br/", 
     "fonte": "FACEPE (PE)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapesq.rpp.br/", 
     "fonte": "FAPESQ (PB)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapeal.br/", 
     "fonte": "FAPEAL (AL)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.funcap.ce.gov.br/", 
     "fonte": "FUNCAP (CE)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapesb.ba.gov.br/", 
     "fonte": "FAPESB (BA)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapern.rn.gov.br/", 
     "fonte": "FAPERN (RN)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapepi.pi.gov.br/", 
     "fonte": "FAPEPI (PI)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapitec.se.gov.br/", 
     "fonte": "FAPITEC (SE)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapema.br/", 
     "fonte": "FAPEMA (MA)", 
     "keywords": ["edital", "chamada"]},
    
    # Região Sul
    {"url": "https://www.fapergs.rs.gov.br/", 
     "fonte": "FAPERGS (RS)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapesc.sc.gov.br/", 
     "fonte": "FAPESC (SC)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fappr.pr.gov.br/", 
     "fonte": "Fundação Araucária (PR)", 
     "keywords": ["edital", "chamada"]},
    
    # Região Centro-Oeste
    {"url": "https://www.fap.df.gov.br/", 
     "fonte": "FAPDF (DF)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fundect.ms.gov.br/", 
     "fonte": "FUNDECT (MS)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapeg.go.gov.br/", 
     "fonte": "FAPEG (GO)", 
     "keywords": ["edital", "chamada"]},
    
    # Região Norte
    {"url": "https://www.fapeam.am.gov.br/", 
     "fonte": "FAPEAM (AM)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapespa.pa.gov.br/", 
     "fonte": "FAPESPA (PA)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://fapac.ac.gov.br/", 
     "fonte": "FAPEAC (AC)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapero.ro.gov.br/", 
     "fonte": "FAPERO (RO)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.to.gov.br/fapt", 
     "fonte": "FAPTO (TO)", 
     "keywords": ["edital", "chamada"]},
    
    {"url": "https://www.fapeap.ap.gov.br/", 
     "fonte": "FAPEAP (AP)", 
     "keywords": ["edital", "chamada"]},
    
    # ========== AGÊNCIAS INTERNACIONAIS ==========
    
    # Estados Unidos
    {"url": "https://www.usaid.gov/work-usaid/partnership-opportunities", 
     "fonte": "USAID (EUA)", 
     "keywords": ["call for proposals", "funding", "notice", "grant", "opportunity"]},
    
    {"url": "https://www.nsf.gov/funding/", 
     "fonte": "NSF (EUA)", 
     "keywords": ["funding", "opportunity", "solicitation", "call"]},
    
    # Europa
    {"url": "https://ec.europa.eu/info/funding-tenders/opportunities/portal/screen/home", 
     "fonte": "União Europeia (Horizon)", 
     "keywords": ["call", "funding", "grant", "opportunity"]},
    
    {"url": "https://www.afd.fr/en/search?type=calls-for-projects", 
     "fonte": "AFD (França)", 
     "keywords": ["call", "notice", "funding", "appel à projets"]},
    
    {"url": "https://www.aecid.es/ES/convocatorias", 
     "fonte": "AECID (Espanha)", 
     "keywords": ["convocatoria", "call", "notice", "grant"]},
    
    # Reino Unido
    {"url": "https://www.ukri.org/opportunity/", 
     "fonte": "UKRI (Reino Unido)", 
     "keywords": ["funding", "opportunity", "call"]},
    
    # Canadá
    {"url": "https://www.nserc-crsng.gc.ca/Professors-Professeurs/Grants-Subs/index_eng.asp", 
     "fonte": "NSERC (Canadá)", 
     "keywords": ["funding", "opportunity", "competition"]},
    
    # Bélgica - Cooperação Acadêmica
    {"url": "https://aca-secretariat.be/", 
     "fonte": "ACA (Academic Cooperation)", 
     "keywords": ["call", "funding", "opportunity", "grant", "programme"]},
]

# ==================== FILTROS INTELIGENTES ====================

# Palavras que DEVEM estar no título/texto de um edital
EDITAL_KEYWORDS_REQUIRED = [
    "edital", "chamada", "call", "convocatória", "seleção",
    "programa", "auxílio", "bolsa", "fomento", "pesquisa"
]

# Palavras que DESQUALIFICAM (blacklist)
BLACKLIST_KEYWORDS = [
    "manual", "instruções", "tutorial", "declaração de imposto",
    "indicadores institucionais", "relatório", "ata", "prestação de contas",
    "resultado preliminar", "homologação", "retificação", "errata",
    "como acessar", "passo a passo", "orientações", "formulário",
    "www.", "http://", "https://", "obedecendo determinação"
]

def is_likely_edital(titulo, texto):
    """
    Verifica se o conteúdo é realmente um edital usando múltiplos filtros.
    Retorna True se passar nos filtros, False caso contrário.
    """
    if not titulo:
        return False
    
    titulo_lower = titulo.lower()
    texto_lower = (texto or "").lower()
    
    # FILTRO 1: Blacklist - desqualifica imediatamente
    for palavra in BLACKLIST_KEYWORDS:
        if palavra in titulo_lower or palavra in texto_lower[:1000]:
            return False
    
    # FILTRO 2: Título muito curto (menos de 10 caracteres)
    if len(titulo.strip()) < 10:
        return False
    
    # FILTRO 3: Título genérico demais
    generic_titles = ["chamada", "edital", "www.", "governo", "fundação"]
    if titulo.strip().lower() in generic_titles:
        return False
    
    # FILTRO 4: Deve ter pelo menos UMA palavra-chave de edital
    has_keyword = any(kw in titulo_lower or kw in texto_lower[:2000] 
                      for kw in EDITAL_KEYWORDS_REQUIRED)
    if not has_keyword:
        return False
    
    # FILTRO 5: Padrão de numeração (Nº XX/YYYY ou N° XX/YYYY)
    has_number_pattern = bool(re.search(r'n[ºº]\s*\d+/\d{4}', titulo_lower))
    
    # FILTRO 6: Tamanho mínimo do texto (500 palavras)
    if texto:
        word_count = len(texto.split())
        if word_count < 500:
            return False
    
    # FILTRO 7: Score de confiança
    score = 0
    if has_number_pattern:
        score += 3
    if "edital" in titulo_lower or "chamada" in titulo_lower:
        score += 2
    if any(word in texto_lower[:1000] for word in ["prazo", "submissão", "inscrição", "cronograma"]):
        score += 1
    if re.search(r'r\$\s*[0-9\.,]+', texto_lower[:2000]):  # Tem valor em reais
        score += 1
    
    # Precisa de pelo menos score 3 para ser considerado edital
    return score >= 3


def clean_title(titulo):
    """Remove caracteres estranhos e normaliza o título"""
    if not titulo:
        return None
    
    # Remove espaços duplicados e caracteres de controle
    titulo = " ".join(titulo.split())
    
    # Remove títulos muito longos sem pontuação (provavelmente lixo)
    if len(titulo) > 300 and titulo.count(" ") > 50:
        # Pega só os primeiros 150 caracteres
        titulo = titulo[:150] + "..."
    
    return titulo

# ==================== COLETOR PRINCIPAL ====================
def coletar_pdf_first(timeout=REQUEST_TIMEOUT, max_per_source=MAX_PER_SOURCE, progress_callback=None):
    """
    Coleta editais de todas as fontes configuradas.
    Estratégia: busca PDFs primeiro, depois links candidatos.
    progress_callback: função para reportar progresso (recebe current, total)
    """
    headers = {"User-Agent": USER_AGENT}
    results = []
    total_sources = len(SOURCES)
    
    for idx, src in enumerate(SOURCES, 1):
        url = src["url"]
        fonte = src.get("fonte")
        keywords = src.get("keywords", ["edital", "chamada"])
        
        # Reportar progresso
        if progress_callback:
            progress_callback(idx, total_sources)
        
        print(f"\n{'='*60}")
        print(f"[COLETA] Fonte: {fonte} ({idx}/{total_sources})")
        print(f"         URL: {url}")
        print(f"{'='*60}")
        
        # Baixar página principal
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            print(f"❌ Erro ao baixar lista: {e}")
            continue
        
        # 1) Buscar PDFs diretamente
        pdfs = find_pdf_links_on_page(url, html)
        print(f"✓ Encontrados {len(pdfs)} PDFs diretos")
        
        # 2) Se não achar PDFs, buscar links candidatos
        if not pdfs:
            print("⚠ Nenhum PDF direto. Buscando candidatos...")
            cand = find_candidate_links_by_keywords(url, html, keywords)
            print(f"✓ Encontrados {len(cand)} candidatos")
            
            # Verificar content-type via HEAD
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
        
        # 3) Processar cada PDF candidato
        count = 0
        for pdf_url, link_text in pdfs:
            if count >= max_per_source:
                break
            
            title_guess = link_text or filename_from_url(pdf_url) or pdf_url
            print(f"\n  📄 Candidato: {title_guess[:80]}...")
            
            # Baixar PDF
            b = download_bytes(pdf_url, timeout=REQUEST_TIMEOUT)
            if not b:
                print("     ❌ Falhou download")
                continue
            
            # Extrair texto do PDF
            text = extract_text_from_pdf_bytes(b)
            if not text:
                print("     ⚠ PDF sem texto extraível, ignorando...")
                continue
            
            # Extrair título e metadados
            titulo = extract_first_title_from_text(text) or title_guess
            titulo = clean_title(titulo)
            prazo, valor = extract_prazo_and_valor(text)
            
            # ⚠️ FILTRO INTELIGENTE: verificar se é realmente um edital
            if not is_likely_edital(titulo, text):
                print(f"     ⏭ Não parece ser um edital, ignorando...")
                count += 1
                continue
            
            # Montar objeto edital
            doc = {
                "titulo": normalize_text(titulo) or title_guess,
                "agencia": fonte,
                "prazo": prazo,
                "valor": valor,
                "link": pdf_url,
                "fonte": fonte
            }
            
            # ⚠️ FILTRO DE DATA: descartar se prazo já passou
            if prazo:
                is_valid = is_date_future(prazo)
                if is_valid is False:  # Explicitamente False (não None)
                    print(f"     ⏭ Prazo vencido ({prazo}), ignorando...")
                    count += 1
                    continue
            
            # Salvar no banco
            if salvar(doc):
                print(f"     ✅ SALVO: {doc['titulo'][:100]}")
                results.append(doc)
            else:
                print(f"     ⏭ Já existe: {pdf_url[:80]}")
            
            count += 1
            time.sleep(0.7)  # Pausa entre downloads
    
    return results

# ==================== FLASK UI ====================
app = Flask(__name__)

@app.route("/")
def index():
    """Página principal com busca"""
    termo = request.args.get("q", "").strip()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    if termo:
        cur.execute("""
            SELECT titulo, agencia, prazo, valor, link, fonte 
            FROM editais 
            WHERE titulo LIKE ? OR agencia LIKE ? 
            ORDER BY criado_em DESC
        """, (f"%{termo}%", f"%{termo}%"))
    else:
        cur.execute("""
            SELECT titulo, agencia, prazo, valor, link, fonte 
            FROM editais 
            ORDER BY criado_em DESC
        """)
    
    rows = cur.fetchall()
    conn.close()
    
    return render_template("index.html", editais=rows, termo=termo)

@app.route("/coletar_stream")
def coletar_stream():
    """Executa coleta com progresso em tempo real via Server-Sent Events"""
    def generate():
        novos = []
        
        def progress_callback(current, total):
            # Enviar atualização de progresso
            data = json.dumps({
                "type": "progress",
                "current": current,
                "total": total
            })
            return f"data: {data}\n\n"
        
        # Enviar atualizações durante a coleta
        total = len(SOURCES)
        for i in range(1, total + 1):
            yield progress_callback(i, total)
            
            # Processar uma fonte por vez
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
                        
                        if not is_likely_edital(titulo, text):
                            count += 1
                            continue
                        
                        doc = {
                            "titulo": normalize_text(titulo) or link_text,
                            "agencia": fonte,
                            "prazo": prazo,
                            "valor": valor,
                            "link": pdf_url,
                            "fonte": fonte
                        }
                        
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
        
        # Enviar mensagem final
        final_data = json.dumps({
            "type": "complete",
            "total": total,
            "novos": len(novos)
        })
        yield f"data: {final_data}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route("/coletar")
def rota_coletar():
    """Executa coleta manual (fallback sem progresso)"""
    print("\n🚀 INICIANDO COLETA MANUAL...")
    novos = coletar_pdf_first()
    print(f"\n✅ Coleta finalizada! Novos editais: {len(novos)}")
    return f"<h1>Coleta finalizada!</h1><p>Novos editais: {len(novos)}</p><a href='/'>Voltar</a>"

@app.route("/limpar_vencidos")
def limpar_vencidos():
    """Remove editais com prazo vencido do banco"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # Buscar todos os editais com prazo
    cur.execute("SELECT id, prazo FROM editais WHERE prazo IS NOT NULL AND prazo != ''")
    rows = cur.fetchall()
    
    removidos = 0
    for id_edital, prazo in rows:
        is_valid = is_date_future(prazo)
        if is_valid is False:  # Prazo vencido
            cur.execute("DELETE FROM editais WHERE id = ?", (id_edital,))
            removidos += 1
    
    conn.commit()
    conn.close()
    
    print(f"🗑️ Removidos {removidos} editais vencidos")
    return f"<h1>Limpeza concluída!</h1><p>Editais removidos: {removidos}</p><a href='/'>Voltar</a>"

@app.route("/export.csv")
def export_csv():
    """Exporta todos os editais em CSV"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT titulo, agencia, prazo, valor, link, fonte, criado_em 
        FROM editais 
        ORDER BY criado_em DESC
    """)
    rows = cur.fetchall()
    conn.close()
    
    # Criar CSV em memória
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(["titulo", "agencia", "prazo", "valor", "link", "fonte", "criado_em"])
    cw.writerows(rows)
    
    output = si.getvalue().encode("utf-8")
    
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=editais.csv"}
    )

# ==================== INICIALIZAÇÃO ====================
if __name__ == "__main__":
    print("="*60)
    print("🎓 SCRAPER DE EDITAIS - Iniciando...")
    print("="*60)
    
    init_db()
    print("✅ Banco de dados inicializado")
    print(f"📊 Fontes configuradas: {len(SOURCES)} agências")
    print("\n🇧🇷 Agências Nacionais:")
    nacionais = [s for s in SOURCES if any(x in s['fonte'] for x in ['CNPq', 'CAPES', 'FINEP', 'CONFAP', 'FAP', 'FUNC', 'Fund'])]
    for s in nacionais:
        print(f"   • {s['fonte']}")
    
    print("\n🌍 Agências Internacionais:")
    internacionais = [s for s in SOURCES if s not in nacionais]
    for s in internacionais:
        print(f"   • {s['fonte']}")
    
    print(f"\n📋 Total: {len(nacionais)} nacionais + {len(internacionais)} internacionais = {len(SOURCES)} agências")
    
    print("\n🌐 Servidor rodando em: http://127.0.0.1:5000")
    print("   - Página principal: http://127.0.0.1:5000")
    print("   - Executar coleta: http://127.0.0.1:5000/coletar")
    print("   - Exportar CSV: http://127.0.0.1:5000/export.csv")
    print("\n💡 Pressione CTRL+C para parar\n")
    
    app.run(host="0.0.0.0", port=10000, debug=False)
