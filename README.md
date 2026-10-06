# NXP News Agent

Automated pipeline that monitors ~75 semiconductor and technology company newsrooms, finds new articles, extracts their opening text with GPT-6 Luna, and produces a formatted PDF digest with clickable article links — plus a JSON export and a run report.

It is built to run 3–4 times a day on the **Firecrawl free plan** (1,000 credits/month).

---

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) package manager
- An OpenAI API key
- A Firecrawl API key (free plan is enough) — [firecrawl.dev](https://firecrawl.dev)

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

Make sure `OPENAI_MODEL=gpt-6-luna` in your `.env` (an older `.env` may still say `gpt-4o-mini`).

---

## Running

### Option 1 — Web UI (recommended for the team)

Double-click **`Start News Digest UI.bat`**, or run:

```bash
uv run app/serve.py                  # opens http://localhost:8000
uv run app/serve.py --host 0.0.0.0   # also reachable from colleagues' computers on the network
```

| Tab | What you can do |
|---|---|
| **Dashboard** | **Run now** button, live progress log, last run summary (new articles, failed sources, OpenAI cost, Firecrawl credits left), open the latest PDF, problems from the last run |
| **Articles** | Search and filter all recent articles (last 24 h – 1 year) by source; copy one or many (title, link, summary), or download a selection as JSON (e.g. for press-release drafting) |
| **Runs & digests** | Every run with duration, new articles, failures, Firecrawl credits and exact OpenAI cost; monthly totals; links to every PDF / JSON / run report |
| **Sources** | All newsrooms with how they are read (RSS / direct / Firecrawl), last result and last success; **Preview** any site (nothing is saved) |
| **Settings & costs** | Current configuration and how costs arise |

Automatic runs: set `AUTO_RUN_TIMES=08:00,12:00,16:00,20:00` in `.env` — runs then start by themselves while the UI is running. A UI run and a command-line run can never overlap (the second one is refused).

### Option 2 — Command line

```bash
uv run app/run.py
```

Each run that finds new articles writes, into `output_docs/`:

| File | Contents |
|---|---|
| `News_Digest_DD_MM_YYYY_HHMM.pdf` | The digest (time in the name, so several runs a day never overwrite each other) |
| `News_Digest_DD_MM_YYYY_HHMM.json` | Same articles as structured data, incl. full cleaned article text |
| `reports/Run_Report_DD_MM_YYYY_HHMMSS.txt` | Written on **every** run: failed sources, articles without content, skipped sources |

Exit codes: `0` success, `1` error (e.g. the PDF could not be written), `2` another run is already in progress.

### Option 3 — HTTP API only

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- `GET http://localhost:8000/api/v1/run` — trigger a run (returns `409` if one is already running)
- `GET http://localhost:8000/health` — health check

### Scheduling (Windows Task Scheduler, 4× a day)

```bat
schtasks /Create /TN "NXP News Digest" /SC DAILY /ST 08:00 /RI 240 /DU 16:00 ^
  /TR "cmd /c cd /d C:\path\to\nxp-agent && uv run app/run.py"
```

Only one run can use the state file at a time; a second run started meanwhile exits with code `2`.

---

## How it works

1. **Read each newsroom with the cheapest method that works**
   1. **RSS feed** (when configured in `app/sites.py`) — free, exact titles/links/dates, no LLM.
   2. **Direct fetch** with real-browser fingerprints (Chrome/Safari/Firefox) — free. Works for most sites.
   3. **Firecrawl** — only for JavaScript-only or blocked pages, within a credit budget (see below).

   The method that worked is remembered per site; sites needing Firecrawl are re-checked with the free method weekly.

2. **Extract articles accurately.** The page is turned into text where every link is a numbered ID. GPT-6 Luna returns the *IDs* of the article links plus the headline; the URL is always taken from the page itself (never written by the model), and each headline is verified to appear on the page.

3. **Skip unchanged pages.** If a listing page has no links that weren't there last run, the LLM call is skipped.

4. **Only new articles.** Seen URLs are tracked in `scraper_state.json`. A newly added site is *baselined* on its first scan (its existing articles are recorded silently), so the digest only contains genuinely new items from the next run on. Articles older than `LOOKBACK_DAYS` are never included.

5. **Article pages** are fetched (free first, Firecrawl if needed and affordable); GPT-6 Luna extracts the opening ~300 words verbatim. If an article's content can't be read, it is still included with **title + link**, and the reason is written to the run report.

6. **State is saved atomically**, and articles are only marked as seen after the PDF is written — a failed run loses nothing.

### Firecrawl credits

- Every Firecrawl call uses the `basic` proxy = **exactly 1 credit**.
- At the start of each run the live balance is read (free). The run may spend `(remaining − reserve) ÷ (runs left in the billing period)`, capped at `FIRECRAWL_MAX_CREDITS_PER_RUN`. Spending is therefore spread evenly over the month and never runs out early.
- Newsrooms that need Firecrawl are re-scraped at most every `FIRECRAWL_LISTING_COOLDOWN_HOURS`; when credits are short, the sites that waited longest go first.
- Typical usage: ~2–10 credits per run.

---

## Running costs

Every run records its exact OpenAI token usage and cost (shown in the UI, the run report and the log). Measured with `gpt-6-luna` at $0.10 / $0.50 per 1M input / output tokens, 76 sources:

| Run type | OpenAI calls | Cost |
|---|---|---|
| Repeat run, few pages changed (typical for runs a few hours apart) | ~5–30 | **$0.004 – $0.03** |
| Busy run: every listing page changed + ~55 new articles | 109 | **$0.08** |
| Preview of one site | 1 | < $0.001 |

At 4 runs a day that is roughly **$1–4 per month** for OpenAI (worst case, every run busy: ~$10/month). Firecrawl stays within the free 1,000 credits/month. Unchanged pages and RSS feeds cost nothing, so cost grows with the amount of *new* news, not with the number of runs. If prices change, update `OPENAI_PRICE_*` in `.env`.

---

## Adding or changing sites

Edit `app/sites.py` (`name`, `url`, optional `feed`). Then preview what will be extracted — in the UI (**Sources → Preview**) or on the command line. No state change, no PDF, no Firecrawl credits:

```bash
uv run app/run.py --check "Intel,Qualcomm"     # sites whose name/URL contains these words
uv run app/run.py --check all                  # every site
uv run app/run.py --check Qualcomm --allow-firecrawl   # also try Firecrawl (uses credits)
```

Tips:
- If a newsroom offers an RSS feed with the same items, add it as `feed` — it is free, exact and needs no LLM.
- Duplicate entries (same URL or name) are ignored with a warning.
- New sites produce no digest entries on their first run (baseline) — this is expected.

---

## Problems and failures

Every run logs to the console and `scraper.log`, and writes `output_docs/reports/Run_Report_*.txt` listing:

- **Failed sources** — a newsroom that could not be read at all (with the reason, e.g. `HTTP 404`, `bot challenge page`).
- **Articles without content** — included in the digest with title + link only (with the reason).
- **Skipped sources** — Firecrawl sites in their cooldown window or beyond this run's credit budget (retried automatically).

---

## Output for downstream tools

The JSON export (`News_Digest_*.json`) contains, per article: `title`, `url`, `source_name`, `source_url`, `published_date`, `image_url`, `summary` (opening ~300 words, verbatim), `body_text` (cleaned full article text) and `fetched_via`. It is intended as the input for downstream automation such as press-release drafting.

---

## Tests

```bash
uv run --with pytest --with pytest-asyncio pytest
```
