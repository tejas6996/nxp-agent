"""
Check a list of newsroom URLs before adding them to app/sites.py.

    .venv/bin/python scripts/discover_sites.py urls.txt --out report.json

For every URL (one per line; blank lines and # comments ignored) it works out the
cheapest way the agent can read it, WITHOUT using Firecrawl:

1. Fetch the page (same browser-like fetcher as the agent). Dead or moved pages
   (DNS failure, 404, redirect to the home page) are looked up again from the site's
   home page: the best "press releases / newsroom / news" link is tried.
2. Look for an RSS/Atom feed: <link rel="alternate">, feed links on the page, and the
   usual locations (WordPress /feed/, investor-relations platforms, /rss.xml ...).
   A feed is used only if it has items with recent dates. Feeds are free and need
   no AI at all.
3. No feed: read the listing page exactly as a run does (link digest + the agent's
   listing extraction, one small AI call). JavaScript-only or blocked pages are
   reported as "needs_firecrawl" - nothing is spent on them here.

Every result records the newest article date, so sites that stopped publishing can be
left out. The report is JSON (one entry per input URL) plus a short summary on stdout.
Nothing in the agent's state or output folder is touched.
"""

import argparse
import asyncio
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infra.http import HttpFetcher  # noqa: E402
from app.infra.openai import OpenAIClient  # noqa: E402
from app.services.discovery import check_url  # noqa: E402
from app.settings import get_settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("urls", help="text file with one URL per line")
    parser.add_argument("--out", default="site_discovery.json")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--resume", action="store_true", help="keep results already in --out and check only the rest")
    args = parser.parse_args()

    urls = [ln.strip() for ln in Path(args.urls).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    settings = get_settings()
    llm = OpenAIClient(settings)
    sem = asyncio.Semaphore(max(1, settings.max_concurrent_openai))
    gate = asyncio.Semaphore(max(1, args.concurrency))
    today = date.today()
    out_path = Path(args.out)
    previous: dict[str, dict[str, Any]] = {}
    if args.resume and out_path.exists():
        try:
            previous = {r["input"]: r for r in json.loads(out_path.read_text(encoding="utf-8"))["results"]}
        except (OSError, ValueError, KeyError):
            previous = {}
    results: list[dict[str, Any]] = [previous[u] for u in urls if u in previous]
    todo = [u for u in urls if u not in previous]
    print(f"{len(results)} already checked, {len(todo)} to go", flush=True)
    done = 0

    def save() -> None:
        # Written as it goes, so a restart (or the server's nightly reboot) loses nothing.
        report = {"checked_at": datetime.now().isoformat(timespec="seconds"),
                  "openai_cost_usd": round(llm.usage.cost_usd, 4), "results": results}
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(out_path)

    async with HttpFetcher(settings) as http:
        async def one(u: str) -> dict[str, Any]:
            nonlocal done
            async with gate:
                try:
                    r = await asyncio.wait_for(check_url(http, llm, sem, u, today), timeout=240)
                except asyncio.TimeoutError:
                    r = {"input": u, "verdict": "dead", "why": "timed out", "notes": []}
                except Exception as exc:  # noqa: BLE001
                    r = {"input": u, "verdict": "error", "why": str(exc)[:200], "notes": []}
            done += 1
            results.append(r)
            if done % 10 == 0:
                save()
            if done % 25 == 0:
                print(f"  {done}/{len(todo)} checked", flush=True)
            return r

        await asyncio.gather(*(one(u) for u in todo))
    save()

    counts: dict[str, int] = {}
    for r in results:
        counts[r.get("verdict", "?")] = counts.get(r.get("verdict", "?"), 0) + 1
    print(json.dumps({"summary": counts, "openai_cost_usd": round(llm.usage.cost_usd, 4)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
