# Nigerian Legal AI Backend

A FastAPI starter for **source-registered Nigerian legal research** and retrieval-grounded AI answers.

> **Not** a complete verified law database and **not** a substitute for a Nigerian lawyer.  
> Documents start as `pending` verification. Always confirm identity, force, amendments, commencement and applicability.

## Features

- Official-first source registry (`source_registry.json`)
- Upload / extract PDF, DOCX, HTML, plain text
- URL ingestion restricted to allowlisted official hosts
- SQLite storage + simple lexical chunk search
- `POST /ask` — retrieve passages and (optionally) generate an answer with OpenAI
- Pending-verification status and lawyer-review flags on every answer

## Quick start (local)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Optional: put your OpenAI key in .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Status |
| GET | `/health` | Indexed document count |
| GET | `/sources` | Source registry |
| GET | `/documents` | Indexed document metadata |
| POST | `/ingest/upload` | Upload & index a file |
| POST | `/ingest/url` | Fetch a direct URL from an allowlisted host |
| POST | `/search` | Lexical passage retrieval |
| POST | `/ask` | Retrieve + optional AI answer |

Example `/ask` body:

```json
{"question": "What does the uploaded Act say about directors?", "top_k": 6}
```

## Deploy to Render (from GitHub)

### 1. Push this repo to GitHub

```bash
# If you haven't already:
git init
git add .
git commit -m "Nigerian Legal AI Backend — ready for Render"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/nigerian-legal-ai-backend.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username (or create the empty repo first on GitHub).

### 2. Create a Web Service on Render

**Option A — Blueprint (recommended)**  
1. Go to [https://dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**  
2. Connect the GitHub repo  
3. Render will read `render.yaml` and create the service  
4. In the service → **Environment** set `OPENAI_API_KEY` (and optionally change `OPENAI_MODEL`)

**Option B — Manual Docker service**  
1. **New** → **Web Service** → connect the repo  
2. Runtime: **Docker**  
3. Health Check Path: `/health`  
4. Add env vars: `OPENAI_API_KEY`, `OPENAI_MODEL` (optional, defaults to `gpt-4.1-mini`)

### 3. After deploy

- Public URL will look like `https://nigerian-legal-ai-backend.onrender.com`  
- Interactive docs: `https://…/docs`  
- Free tier services sleep after inactivity; first request can take ~30–60 s.

### Important notes about data on free Render

- SQLite lives on the **ephemeral filesystem**.  
- Indexed documents are **lost on every redeploy / restart**.  
- For real use, either:
  - attach a **persistent disk** (paid), or  
  - migrate the store to **PostgreSQL** (Render has a free Postgres tier you can wire later).

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `OPENAI_API_KEY` | No* | Enables AI-generated answers on `/ask`. Without it you still get retrieved passages. |
| `OPENAI_MODEL` | No | Defaults to `gpt-4.1-mini` |
| `PORT` | Auto | Set by Render; Dockerfile already respects it |

\* Required only if you want the model to write answers.

## How to populate the legal corpus

The registry lists official portals; it does **not** ship the full legislation/judgment corpus.

1. Download the actual Act, regulation, circular or judgment from the official page.  
2. `POST /ingest/upload` the file, **or** `POST /ingest/url` with a direct link on an allowlisted host.  
3. Record title, type, jurisdiction, publication/commencement dates where known.  
4. A lawyer/editor must verify identity, completeness, current force, amendments and applicability before legal reliance.

## Production work still required

1. Auth, roles, matter-level permissions  
2. PostgreSQL + hybrid keyword/vector retrieval, OCR for scanned PDFs, versioning  
3. Encryption, secure storage, malware scanning, audit logs, backups  
4. Legal editorial workflow for currency, commencement, amendments, repeal, jurisdiction  
5. Review each site’s terms / licences before bulk indexing  
6. Review AI provider data-handling policy for confidential client documents  

## Official source starting points

- National Assembly: https://nass.gov.ng/documents/magazine  
- Supreme Court: https://www.supremecourt.gov.ng/judgment  
- Court of Appeal: https://courtofappeal.gov.ng/new_decisions.php  
- CAC: https://www.cac.gov.ng/resources  
- SEC: https://sec.gov.ng/our-mandate/regulation/  
- CBN: https://www.cbn.gov.ng/supervision/  
- Federal Ministry of Justice (law reporting): https://justice.gov.ng/law-reporting/

---

**Disclaimer**: This is a research/retrieval starter. It is not legal advice and must not be relied on as a complete or verified statement of Nigerian law.
