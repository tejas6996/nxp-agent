# NXP News Agent

Automated daily pipeline that monitors semiconductor and technology company newsrooms, extracts new articles, summarizes them using GPT, and produces a formatted Word document digest.

## Setup

```bash
uv venv .venv
uv sync --all-extras
```

## Usage

```bash
uv run uvicorn app.main:app --reload
```
