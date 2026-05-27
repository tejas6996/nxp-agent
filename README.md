# NXP News Agent

Automated daily pipeline that monitors 19 semiconductor and technology company newsrooms, extracts new articles, summarizes them using GPT-4o-mini, and produces a formatted PDF digest with clickable article links.

---

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- A Firecrawl API key — [firecrawl.dev](https://firecrawl.dev)
- An OpenAI API key

---

## Setup

**1. Install dependencies**

```bash
uv venv .venv
uv sync
```

**2. Configure environment**

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
```

---

## How it works

- **First run** — scrapes all listing pages, saves discovered article URLs to `scraper_state.json`, exits without building a PDF. This establishes the baseline.
- **Subsequent runs** — finds URLs not yet in state, scrapes and summarises them, builds the PDF digest, saves new URLs to state.

---

## Running

### Option 1 — Standalone script (recommended for daily use)

```bash
uv run app/run.py
```

Output PDF is saved to `output_docs/News_Digest_DD_MM_YYYY.pdf`.

### Option 2 — FastAPI server

Start the server:

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Trigger a pipeline run via HTTP:

```
GET http://localhost:8000/api/v1/run
```

Check server health:

```
GET http://localhost:8000/health
```

---
