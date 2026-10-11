import os, re, json, sqlite3, hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "legal_ai.sqlite3"
REGISTRY_PATH = ROOT / "source_registry.json"
DATA.mkdir(exist_ok=True)
with open(REGISTRY_PATH, encoding="utf-8") as f:
    REGISTRY = json.load(f)
ALLOWED_HOSTS = {urlparse(s["url"]).hostname for s in REGISTRY["sources"]}

app = FastAPI(title="Nigerian Legal AI Backend", version="0.1.0",
              description="Starter backend for source-registered Nigerian legal research and retrieval-grounded AI.")

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute('''CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, source_url TEXT,
            source_authority TEXT, document_type TEXT, jurisdiction TEXT, publication_date TEXT,
            effective_from TEXT, status TEXT DEFAULT 'unverified', verification_status TEXT DEFAULT 'pending',
            sha256 TEXT NOT NULL, text_content TEXT NOT NULL, ingested_at TEXT NOT NULL)''')
        con.execute('''CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL, text_content TEXT NOT NULL,
            FOREIGN KEY(document_id) REFERENCES documents(id))''')
init_db()

class URLIngest(BaseModel):
    url: HttpUrl
    title: str | None = None
    document_type: str = "unclassified"
    jurisdiction: str = "Federal"
    publication_date: str | None = None
    effective_from: str | None = None
    status: str = "unverified"

class SearchRequest(BaseModel):
    question: str
    top_k: int = 6

def extract_text(filename: str, content: bytes, content_type: str = "") -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf" or "pdf" in content_type.lower():
        try:
            from pypdf import PdfReader
            import io
            return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(content)).pages).strip()
        except Exception as e:
            raise HTTPException(400, f"Could not extract PDF text; scanned PDFs may need OCR: {e}")
    if suffix == ".docx":
        try:
            from docx import Document
            import io
            return "\n".join(p.text for p in Document(io.BytesIO(content)).paragraphs).strip()
        except Exception as e:
            raise HTTPException(400, f"Could not extract DOCX text: {e}")
    if suffix in {".html", ".htm"} or "html" in content_type.lower():
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]): tag.decompose()
        return soup.get_text("\n", strip=True)
    return content.decode("utf-8", errors="replace").strip()

def chunk_text(text: str, size: int = 1200, overlap: int = 180):
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(". ", start, end))
            if boundary > start + int(size * .55): end = boundary + 1
        chunks.append(text[start:end].strip())
        if end >= len(text): break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]

def authority_for_url(url):
    host = urlparse(url).hostname or ""
    for item in REGISTRY["sources"]:
        if host == (urlparse(item["url"]).hostname or ""): return item["authority"]
    return host

def save_document(title, text, source_url=None, authority=None, document_type="unclassified", jurisdiction="Federal",
                  publication_date=None, effective_from=None, status="unverified", verification_status="pending"):
    if len(text.strip()) < 80:
        raise HTTPException(400, "Too little readable text extracted. Scanned files may need OCR.")
    sha = hashlib.sha256(text.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    pieces = chunk_text(text)
    with db() as con:
        old = con.execute("SELECT id FROM documents WHERE sha256=?", (sha,)).fetchone()
        if old: return {"document_id": old["id"], "duplicate": True, "message": "Exact text already indexed."}
        cur = con.execute('''INSERT INTO documents (title,source_url,source_authority,document_type,jurisdiction,
          publication_date,effective_from,status,verification_status,sha256,text_content,ingested_at)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (title,source_url,authority,document_type,jurisdiction,publication_date,
          effective_from,status,verification_status,sha,text,now))
        doc_id = cur.lastrowid
        con.executemany("INSERT INTO chunks(document_id,chunk_index,text_content) VALUES(?,?,?)",
                        [(doc_id,i,c) for i,c in enumerate(pieces)])
    return {"document_id": doc_id, "chunks_created": len(pieces), "duplicate": False,
            "verification_status": verification_status,
            "note": "Indexed for retrieval; verify identity, current force, amendments and commencement before legal reliance."}

@app.get("/")
def root():
    return {"name":"Nigerian Legal AI Backend", "status":"running",
            "note":"Starter only. Source portals are registered; full law documents have not been preloaded."}

@app.get("/health")
def health():
    with db() as con: n = con.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
    return {"ok":True,"indexed_documents":n}

@app.get("/sources")
def sources(): return REGISTRY

@app.get("/documents")
def documents():
    with db() as con:
        rows = con.execute('''SELECT id,title,source_url,source_authority,document_type,jurisdiction,
          publication_date,effective_from,status,verification_status,ingested_at FROM documents ORDER BY id DESC''').fetchall()
    return [dict(r) for r in rows]

@app.post("/ingest/upload")
async def ingest_upload(file: UploadFile = File(...), title: str | None = None, source_url: str | None = None,
                        document_type: str = "unclassified", jurisdiction: str = "Federal",
                        publication_date: str | None = None, effective_from: str | None = None,
                        status: str = "unverified"):
    content = await file.read()
    if len(content) > 25 * 1024 * 1024: raise HTTPException(413, "Maximum upload size is 25 MB.")
    text = extract_text(file.filename or "upload.txt", content, file.content_type or "")
    return save_document(title or Path(file.filename or "Untitled").stem, text, source_url,
                         authority_for_url(source_url) if source_url else None, document_type, jurisdiction,
                         publication_date, effective_from, status, "pending")

@app.post("/ingest/url")
def ingest_url(payload: URLIngest):
    url = str(payload.url); host = urlparse(url).hostname
    if host not in ALLOWED_HOSTS:
        raise HTTPException(400, "Host not allowlisted. Review and add it to source_registry.json first.")
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent":"NigerianLegalAI/0.1; source ingestion"})
        r.raise_for_status()
    except Exception as e: raise HTTPException(400, f"Could not fetch URL: {e}")
    filename = urlparse(url).path.rsplit("/",1)[-1] or "source.html"
    text = extract_text(filename, r.content, r.headers.get("content-type",""))
    return save_document(payload.title or filename, text, url, authority_for_url(url), payload.document_type,
                         payload.jurisdiction, payload.publication_date, payload.effective_from, payload.status, "pending")

def search_chunks(question, top_k):
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{2,}", question)]
    if not terms: return []
    with db() as con:
        rows = con.execute('''SELECT c.id chunk_id,c.text_content,c.chunk_index,d.id document_id,d.title,d.source_url,
          d.source_authority,d.document_type,d.jurisdiction,d.publication_date,d.effective_from,d.status,d.verification_status
          FROM chunks c JOIN documents d ON d.id=c.document_id''').fetchall()
    scored=[]
    for row in rows:
        txt=row["text_content"].lower(); score=sum(txt.count(t) for t in set(terms))
        if score:
            item=dict(row); item["score"]=score; scored.append(item)
    return sorted(scored,key=lambda x:x["score"],reverse=True)[:max(1,min(top_k,12))]

@app.post("/search")
def search(payload: SearchRequest):
    return {"question":payload.question,"results":search_chunks(payload.question,payload.top_k)}

@app.post("/ask")
def ask(payload: SearchRequest):
    hits=search_chunks(payload.question,payload.top_k)
    if not hits:
        return {"answer":"No relevant passage was found in the indexed corpus. Ingest verified legal documents before relying on this assistant. It will not invent authorities.","sources":[],"requires_lawyer_review":True}
    refs=[{k:h.get(k) for k in ("document_id","title","source_url","source_authority","document_type","publication_date","effective_from","status","verification_status","text_content")} for h in hits]
    key=os.getenv("OPENAI_API_KEY")
    if not key:
        return {"answer":"Relevant passages retrieved. Set OPENAI_API_KEY in .env to enable AI-generated answers; inspect the evidence below in the meantime.","sources":refs,"requires_lawyer_review":True}
    try:
        from openai import OpenAI
        client=OpenAI(api_key=key)
        evidence=[]
        for i,h in enumerate(hits,1):
            evidence.append(f"[SOURCE {i}]\nTitle: {h['title']}\nAuthority: {h['source_authority']}\nURL: {h['source_url']}\nType: {h['document_type']}\nPublished: {h['publication_date']}\nEffective from: {h['effective_from']}\nStatus: {h['status']}\nVerification: {h['verification_status']}\nPassage:\n{h['text_content']}")
        system="""You are a Nigerian legal research assistant. Answer only to the extent supported by supplied passages. Never invent statutes, sections, cases, holdings, commencement dates or amendments. Cite material propositions as [SOURCE n] and use the supplied URL. Distinguish legislation, judgments and regulatory guidance. If currency, commencement, repeal, appellate history or applicability is not established, state that it needs verification. Identify missing facts. Always flag that a Nigerian lawyer must review material conclusions."""
        model=os.getenv("OPENAI_MODEL","gpt-4.1-mini")
        result=client.chat.completions.create(model=model,temperature=0.1,messages=[{"role":"system","content":system},{"role":"user","content":f"QUESTION:\n{payload.question}\n\nRETRIEVED EVIDENCE:\n"+'\n\n'.join(evidence)}])
        return {"answer":result.choices[0].message.content,"sources":[{k:h.get(k) for k in ("document_id","title","source_url","source_authority","document_type","publication_date","effective_from","status","verification_status")} for h in hits],"requires_lawyer_review":True}
    except Exception as e: raise HTTPException(502,f"AI provider call failed: {e}")
