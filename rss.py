#!/usr/bin/env python3
"""Fetch RSS feeds defined in feeds.yaml; emit per-feed + consolidated JSON.

Each item becomes a job-shaped dict:
  { company, title, url, location, description, source, tags, market, pool }

Per-feed optional filter `include_keywords` filters items by title+description match.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "data"
RSS_OUT = OUT_DIR / "rss"
RSS_OUT.mkdir(parents=True, exist_ok=True)


def fetch_feed(url: str, timeout: int = 30):
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    })
    r.raise_for_status()
    return feedparser.parse(r.content)


def keep(item_text: str, keywords: List[str]) -> bool:
    if not keywords:
        return True
    lo = item_text.lower()
    return any(kw.lower() in lo for kw in keywords)


def extract_location(text: str) -> str:
    m = re.search(r"(北京|上海|杭州|深圳|广州|成都|新加坡|香港|远程|remote|Singapore|Hong Kong|Bay Area|London|New York)", text, re.I)
    return m.group(0) if m else ""


def main() -> int:
    cfg = yaml.safe_load((ROOT / "feeds.yaml").read_text())
    feeds = cfg.get("feeds") or []

    ts = datetime.now(timezone.utc).isoformat()
    summary: Dict = {"generated_at": ts, "feeds": {}}
    all_items: List[Dict] = []

    for f in feeds:
        slug = f["slug"]
        url = f["url"]
        name = f.get("name") or slug
        market = f.get("market", "global")
        pool = f.get("pool", "watch")
        tags = f.get("tags") or []
        kw = f.get("include_keywords") or []

        print(f"== {slug} -> {url}")
        try:
            parsed = fetch_feed(url)
        except Exception as e:
            print(f"  FAIL fetch: {e}", file=sys.stderr)
            summary["feeds"][slug] = {"error": str(e)[:300]}
            continue

        entries = parsed.entries or []
        kept: List[Dict] = []
        for e in entries:
            title = (e.get("title") or "").strip()
            link = e.get("link") or ""
            desc = (e.get("summary") or e.get("description") or "").strip()
            text = f"{title}\n{desc}"
            if not keep(text, kw):
                continue
            kept.append({
                "company": name,
                "title": title[:200],
                "url": link,
                "location": extract_location(text),
                "description": desc[:1500],
                "source": f"gh-relay/{slug}",
                "tags": ["github-relay", "rss", slug, *tags],
                "market": market,
                "pool": pool,
                "published": e.get("published", ""),
            })

        out = {"generated_at": ts, "feed": f, "items": kept, "total_entries": len(entries)}
        (RSS_OUT / f"{slug}.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        all_items.extend(kept)
        summary["feeds"][slug] = {
            "name": name, "url": url,
            "raw": len(entries), "kept": len(kept),
        }
        print(f"  ok: raw={len(entries)} kept={len(kept)}")

    consolidated = {"generated_at": ts, "jobs": all_items}
    (OUT_DIR / "all_rss_jobs.json").write_text(json.dumps(consolidated, ensure_ascii=False, indent=2))
    (OUT_DIR / "rss_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nTotal kept items across feeds: {len(all_items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
