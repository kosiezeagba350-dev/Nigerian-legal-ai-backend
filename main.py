import os
import re
import json
import hashlib
from io import BytesIO
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from bs4 import BeautifulSoup
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import BaseModel, HttpUrl
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware


# --------------------------------------------------
# 1. CONFIGURATION
# --------------------------------------------------

load_dotenv()

ROOT = Path(__file__).resolve().parent
REGISTRY_PATH = ROOT / "source_registry.json"

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is missing. Configure it in your hosting environment."
    )

if not REGISTRY_PATH.exists():
    raise RuntimeError("source_registry.json was not found.")

with open(REGISTRY_PATH, encoding="utf-8") as f:
    REGISTRY = json.load(f)

ALLOWED_HOSTS = {
    urlparse(source["url"]).hostname
    for source in REGISTRY["sources"]
}


# --------------------------------------------------
# 2. FASTAPI APPLICATION
# --------------------------------------------------

app = FastAPI(
    title="Nigerian Legal AI Backend",
    version="0.2.0",
    description=(
        "A source-registered Nigerian legal research backend "
        "with document ingestion and retrieval-grounded AI."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------
# 3. NEON POSTGRESQL CONNECTION
# --------------------------------------------------

@contextmanager
def db():
    con = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=RealDictCursor,
        sslmode="require",
        connect_timeout=10,
    )

    try:
        yield con
        con.commit()

    except Exception:
        con.rollback()
        raise

    finally:
        con.close()


# --------------------------------------------------
# 4. DATABASE TABLES
# --------------------------------------------------

def init_db():
    with db() as con:
        with con.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_url TEXT,
                    source_authority TEXT,
                    document_type TEXT,
                    jurisdiction TEXT,
                    publication_date TEXT,
                    effective_from TEXT,
                    status TEXT DEFAULT 'unverified',
                    verification_status TEXT DEFAULT 'pending',
                    sha256 TEXT NOT NULL UNIQUE,
                    text_content TEXT NOT NULL,
                    ingested_at TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                    id BIGSERIAL PRIMARY KEY,
                    document_id BIGINT NOT NULL
                        REFERENCES documents(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    text_content TEXT NOT NULL
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_chunks_document_id
                ON chunks(document_id)
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_documents_sha256
                ON documents(sha256)
            """)


# Create tables when the application starts.
init_db()


# --------------------------------------------------
# 5. REQUEST MODELS
# --------------------------------------------------

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


# --------------------------------------------------
# 6. DOCUMENT TEXT EXTRACTION
# --------------------------------------------------

def extract_text(
    filename: str,
    content: bytes,
    content_type: str = "",
) -> str:

    suffix = Path(filename).suffix.lower()

    if suffix == ".pdf" or "pdf" in content_type.lower():
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content))

            return "\n".join(
                page.extract_text() or ""
                for page in reader.pages
            ).strip()

        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Could not extract PDF text. "
                    f"Scanned PDFs may require OCR. Details: {e}"
                ),
            )

    if suffix == ".docx":
        try:
            from docx import Document

            document = Document(BytesIO(content))

            return "\n".join(
                paragraph.text
                for paragraph in document.paragraphs
            ).strip()

        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Could not extract DOCX text: {e}",
            )

    if suffix in {".html", ".htm"} or "html" in content_type.lower():
        soup = BeautifulSoup(content, "html.parser")

        for tag in soup([
            "script",
            "style",
            "nav",
            "footer",
            "header",
        ]):
            tag.decompose()

        return soup.get_text("\n", strip=True)

    return content.decode(
        "utf-8",
        errors="replace",
    ).strip()


# --------------------------------------------------
# 7. SPLIT DOCUMENTS INTO SEARCHABLE CHUNKS
# --------------------------------------------------

def chunk_text(
    text: str,
    size: int = 1200,
    overlap: int = 180,
):

    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    chunks = []
    start = 0

    while start < len(text):

        end = min(len(text), start + size)

        if end < len(text):

            boundary = max(
                text.rfind("\n", start, end),
                text.rfind(". ", start, end),
            )

            if boundary > start + int(size * 0.55):
                end = boundary + 1

        piece = text[start:end].strip()

        if piece:
            chunks.append(piece)

        if end >= len(text):
            break

        start = max(
            end - overlap,
            start + 1,
        )

    return chunks


# --------------------------------------------------
# 8. IDENTIFY THE SOURCE AUTHORITY
# --------------------------------------------------

def authority_for_url(url):

    host = urlparse(url).hostname or ""

    for source in REGISTRY["sources"]:

        registered_host = (
            urlparse(source["url"]).hostname or ""
        )

        if host == registered_host:
            return source["authority"]

    return host


# --------------------------------------------------
# 9. SAVE DOCUMENTS TO NEON POSTGRESQL
# --------------------------------------------------

def save_document(
    title,
    text,
    source_url=None,
    authority=None,
    document_type="unclassified",
    jurisdiction="Federal",
    publication_date=None,
    effective_from=None,
    status="unverified",
    verification_status="pending",
):

    if len(text.strip()) < 80:
        raise HTTPException(
            status_code=400,
            detail=(
                "Too little readable text was extracted. "
                "Scanned files may need OCR."
            ),
        )

    sha = hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()

    now = datetime.now(
        timezone.utc
    ).isoformat()

    pieces = chunk_text(text)

    with db() as con:

        with con.cursor() as cur:

            # Check whether this exact document already exists.
            cur.execute(
                """
                SELECT id
                FROM documents
                WHERE sha256 = %s
                """,
                (sha,),
            )

            old = cur.fetchone()

            if old:
                return {
                    "document_id": old["id"],
                    "duplicate": True,
                    "message": "Exact text already indexed.",
                }

            # Save the document and obtain its database ID.
            cur.execute(
                """
                INSERT INTO documents (
                    title,
                    source_url,
                    source_authority,
                    document_type,
                    jurisdiction,
                    publication_date,
                    effective_from,
                    status,
                    verification_status,
                    sha256,
                    text_content,
                    ingested_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    title,
                    source_url,
                    authority,
                    document_type,
                    jurisdiction,
                    publication_date,
                    effective_from,
                    status,
                    verification_status,
                    sha,
                    text,
                    now,
                ),
            )

            doc_id = cur.fetchone()["id"]

            # Save the searchable text chunks.
            cur.executemany(
                """
                INSERT INTO chunks (
                    document_id,
                    chunk_index,
                    text_content
                )
                VALUES (%s, %s, %s)
                """,
                [
                    (doc_id, i, piece)
                    for i, piece in enumerate(pieces)
                ],
            )

    return {
        "document_id": doc_id,
        "chunks_created": len(pieces),
        "duplicate": False,
        "verification_status": verification_status,
        "note": (
            "Document indexed. Verify its identity, current legal "
            "status, amendments, commencement and applicability "
            "before relying on it."
        ),
    }


# --------------------------------------------------
# 10. HOME ENDPOINT
# --------------------------------------------------

@app.get("/")
def root():

    return {
        "name": "Nigerian Legal AI Backend",
        "status": "running",
        "database": "PostgreSQL",
        "note": (
            "The backend is running. Legal documents must be "
            "ingested before the system can retrieve them."
        ),
    }


# --------------------------------------------------
# 11. HEALTH CHECK
# --------------------------------------------------

@app.get("/health")
def health():

    with db() as con:

        with con.cursor() as cur:

            cur.execute(
                "SELECT COUNT(*) AS n FROM documents"
            )

            count = cur.fetchone()["n"]

    return {
        "ok": True,
        "database": "connected",
        "indexed_documents": count,
    }


# --------------------------------------------------
# 12. VIEW REGISTERED SOURCES
# --------------------------------------------------

@app.get("/sources")
def sources():

    return REGISTRY


# --------------------------------------------------
# 13. VIEW INDEXED DOCUMENTS
# --------------------------------------------------

@app.get("/documents")
def documents():

    with db() as con:

        with con.cursor() as cur:

            cur.execute("""
                SELECT
                    id,
                    title,
                    source_url,
                    source_authority,
                    document_type,
                    jurisdiction,
                    publication_date,
                    effective_from,
                    status,
                    verification_status,
                    ingested_at
                FROM documents
                ORDER BY id DESC
            """)

            rows = cur.fetchall()

    return [dict(row) for row in rows]


# --------------------------------------------------
# 14. UPLOAD A LEGAL DOCUMENT
# --------------------------------------------------

@app.post("/ingest/upload")
async def ingest_upload(
    file: UploadFile = File(...),
    title: str | None = None,
    source_url: str | None = None,
    document_type: str = "unclassified",
    jurisdiction: str = "Federal",
    publication_date: str | None = None,
    effective_from: str | None = None,
    status: str = "unverified",
):

    content = await file.read()

    if len(content) > 25 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="Maximum upload size is 25 MB.",
        )

    filename = file.filename or "upload.txt"

    text = extract_text(
        filename,
        content,
        file.content_type or "",
    )

    return save_document(
        title or Path(filename).stem,
        text,
        source_url,
        authority_for_url(source_url) if source_url else None,
        document_type,
        jurisdiction,
        publication_date,
        effective_from,
        status,
        "pending",
    )


# --------------------------------------------------
# 15. INGEST A DOCUMENT FROM A REGISTERED URL
# --------------------------------------------------

@app.post("/ingest/url")
def ingest_url(payload: URLIngest):

    url = str(payload.url)

    host = urlparse(url).hostname

    if host not in ALLOWED_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Host is not allowlisted. Review and add it "
                "to source_registry.json first."
            ),
        )

    try:

        response = requests.get(
            url,
            timeout=25,
            headers={
                "User-Agent": "NigerianLegalAI/0.2"
            },
        )

        response.raise_for_status()

    except Exception as e:

        raise HTTPException(
            status_code=400,
            detail=f"Could not fetch URL: {e}",
        )

    filename = (
        urlparse(url).path.rsplit("/", 1)[-1]
        or "source.html"
    )

    text = extract_text(
        filename,
        response.content,
        response.headers.get("content-type", ""),
    )

    return save_document(
        payload.title or filename,
        text,
        url,
        authority_for_url(url),
        payload.document_type,
        payload.jurisdiction,
        payload.publication_date,
        payload.effective_from,
        payload.status,
        "pending",
    )


# --------------------------------------------------
# 16. SEARCH DOCUMENT CHUNKS
# --------------------------------------------------

def search_chunks(question, top_k):

    terms = [
        term.lower()
        for term in re.findall(
            r"[A-Za-z0-9][A-Za-z0-9'-]{2,}",
            question,
        )
    ]

    if not terms:
        return []

    with db() as con:

        with con.cursor() as cur:

            cur.execute("""
                SELECT
                    c.id AS chunk_id,
                    c.text_content,
                    c.chunk_index,
                    d.id AS document_id,
                    d.title,
                    d.source_url,
                    d.source_authority,
                    d.document_type,
                    d.jurisdiction,
                    d.publication_date,
                    d.effective_from,
                    d.status,
                    d.verification_status
                FROM chunks c
                JOIN documents d
                    ON d.id = c.document_id
            """)

            rows = cur.fetchall()

    scored = []

    for row in rows:

        item = dict(row)

        content = item["text_content"].lower()

        score = sum(
            content.count(term)
            for term in set(terms)
        )

        if score > 0:
            item["score"] = score
            scored.append(item)

    scored.sort(
        key=lambda item: item["score"],
        reverse=True,
    )

    limit = max(1, min(top_k, 12))

    return scored[:limit]


# --------------------------------------------------
# 17. SEARCH ENDPOINT
# --------------------------------------------------

@app.post("/search")
def search(payload: SearchRequest):

    results = search_chunks(
        payload.question,
        payload.top_k,
    )

    return {
        "question": payload.question,
        "results": results,
    }


# --------------------------------------------------
# 18. AI LEGAL RESEARCH ENDPOINT
# --------------------------------------------------

@app.post("/ask")
def ask(payload: SearchRequest):

    hits = search_chunks(
        payload.question,
        payload.top_k,
    )

    if not hits:

        return {
            "answer": (
                "No relevant passage was found in the indexed "
                "legal documents. Add relevant legal materials "
                "before relying on this assistant. It will not "
                "invent legal authorities."
            ),
            "sources": [],
            "requires_lawyer_review": True,
        }

    refs = [
        {
            key: hit.get(key)
            for key in (
                "document_id",
                "title",
                "source_url",
                "source_authority",
                "document_type",
                "publication_date",
                "effective_from",
                "status",
                "verification_status",
                "text_content",
            )
        }
        for hit in hits
    ]

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:

        return {
            "answer": (
                "Relevant passages were retrieved. Configure "
                "OPENAI_API_KEY to enable AI-generated answers. "
                "Review the retrieved evidence below in the meantime."
            ),
            "sources": refs,
            "requires_lawyer_review": True,
        }

    try:

        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_BASE_URL") or None,
        )

        evidence = []

        for index, hit in enumerate(hits, 1):

            evidence.append(
                f"""
[SOURCE {index}]
Title: {hit["title"]}
Authority: {hit["source_authority"]}
URL: {hit["source_url"]}
Document type: {hit["document_type"]}
Published: {hit["publication_date"]}
Effective from: {hit["effective_from"]}
Status: {hit["status"]}
Verification: {hit["verification_status"]}

Passage:
{hit["text_content"]}
"""
            )

        system_prompt = """
You are a Nigerian legal research assistant.

Answer only to the extent supported by the supplied passages.

Never invent statutes, statutory sections, court cases, holdings,
commencement dates, amendments or legal authorities.

Cite material propositions using [SOURCE 1], [SOURCE 2], etc.
Use the supplied source URLs where relevant.

Distinguish legislation, judgments and regulatory guidance.

If the evidence does not establish whether a law is current,
commenced, amended, repealed, binding or applicable, say so.

Identify material missing facts and uncertainties.

Do not treat retrieved text as proof that a legal authority
remains in force.

Always state that material legal conclusions require review
by a qualified Nigerian lawyer.
"""

        model = os.getenv(
            "OPENAI_MODEL",
            "gpt-4.1-mini",
        )

        result = client.chat.completions.create(
            model=model,
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        f"QUESTION:\n{payload.question}\n\n"
                        "RETRIEVED EVIDENCE:\n"
                        + "\n\n".join(evidence)
                    ),
                },
            ],
        )

        answer = result.choices[0].message.content

        return {
            "answer": answer,
            "sources": [
                {
                    key: hit.get(key)
                    for key in (
                        "document_id",
                        "title",
                        "source_url",
                        "source_authority",
                        "document_type",
                        "publication_date",
                        "effective_from",
                        "status",
                        "verification_status",
                    )
                }
                for hit in hits
            ],
            "requires_lawyer_review": True,
        }

    except Exception:
        raise HTTPException(
            status_code=502,
            detail=(
                "The AI provider request failed. Check the "
                "provider configuration and deployment logs."
            ),
        )
