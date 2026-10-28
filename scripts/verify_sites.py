"""
Run the agent's own listing check on candidate sites, exactly as a digest run reads
them (feed -> direct page -> Firecrawl), without touching state or output.

    .venv/bin/python scripts/verify_sites.py candidates.json --out verified.json [--firecrawl]

candidates.json: [{"name": "...", "url": "...", "feed": "..." (optional)}, ...]
Firecrawl is used only with --firecrawl (1 credit per JavaScript-only page).
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.parsing import parse_date  # noqa: E402
from app.services.pipeline import check_sites  # noqa: E402
from app.settings import get_settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("candidates")
    parser.add_argument("--out", default="verified.json")
    parser.add_argument("--firecrawl", action="store_true")
    args = parser.parse_args()
    sites = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
    scans, credits, usage = await check_sites(get_settings(), sites, allow_firecrawl=args.firecrawl)
    rows = []
    for scan in scans:
        dates = [d for d in (parse_date(a.date) for a in scan.articles) if d]
        rows.append({
            "name": scan.site["name"],
            "url": scan.site["url"],
            "feed": scan.site.get("feed"),
            "status": scan.status,
            "method": scan.method,
            "detail": scan.detail,
            "articles": len(scan.articles),
            "newest": max(dates).isoformat() if dates else None,
            "titles": [a.title for a in scan.articles[:3]],
            "links": [a.url for a in scan.articles[:3]],
        })
    Path(args.out).write_text(json.dumps({"firecrawl_credits_used": credits, "openai_cost_usd": round(usage.cost_usd, 4),
                                          "results": rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    ok = sum(1 for r in rows if r["status"] == "ok")
    print(json.dumps({"sites": len(rows), "ok": ok, "firecrawl_credits_used": credits, "openai_cost_usd": round(usage.cost_usd, 4)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
