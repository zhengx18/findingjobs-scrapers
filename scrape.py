#!/usr/bin/env python3
"""Render target career pages with playwright; emit JSON + raw HTML.

Runs in GitHub Actions (US/EU egress, full JS rendering).
Output:
  data/raw/{slug}.html       — full rendered HTML for offline inspection
  data/raw/{slug}.txt        — visible text fallback
  data/{slug}.json           — structured job list (best-effort heuristic + per-site extractors)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

OUT_DIR = Path(__file__).parent / "data"
RAW_DIR = OUT_DIR / "raw"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Target:
    slug: str
    company: str
    url: str
    # post-load action (clicks, waits) — receives Page
    interact: Optional[Callable[[Page], None]] = None
    # extractor — receives rendered HTML, returns list of jobs (dicts)
    extract: Optional[Callable[[str, "Target"], List[Dict]]] = None
    wait_selector: Optional[str] = None
    wait_timeout_ms: int = 15000
    tags: List[str] = field(default_factory=list)
    market: str = "cn"
    pool: str = "watch"


# ── Per-site extractors ─────────────────────────────────────────────
JOB_KEYWORDS = ("工程师", "engineer", "算法", "研究员", "scientist", "researcher", "developer")


def _generic_extract(html: str, target: Target) -> List[Dict]:
    """Fallback: scan for <a> tags whose text contains job keywords."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    for a in soup.find_all("a", href=True):
        txt = a.get_text(" ", strip=True)
        if not txt or len(txt) > 120 or len(txt) < 4:
            continue
        if not any(k in txt.lower() for k in JOB_KEYWORDS):
            continue
        href = a["href"]
        full = urljoin(target.url, href)
        key = (txt, full)
        if key in seen:
            continue
        seen.add(key)
        jobs.append({
            "company": target.company,
            "title": txt,
            "url": full,
            "location": "",
            "description": txt,
            "source": f"gh-relay/{target.slug}",
            "tags": ["github-relay", target.slug, *target.tags],
            "market": target.market,
            "pool": target.pool,
        })
    return jobs


def _ubiquant_extract(html: str, target: Target) -> List[Dict]:
    """Ubiquant uses tabs (社招/校招). Look for job list under tabs."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    seen = set()
    # Ubiquant typically renders jobs into elements with classes like .job-item or .position-card
    for tag in soup.find_all(["li", "div", "a"]):
        cls = " ".join(tag.get("class", []) or [])
        if not any(k in cls.lower() for k in ("job", "position", "career", "post")):
            continue
        title_el = tag.find(["h1", "h2", "h3", "h4", "h5", "span", "p"])
        title = (title_el.get_text(strip=True) if title_el else tag.get_text(" ", strip=True))[:120]
        if not title or len(title) < 4:
            continue
        href = tag.get("href") or (tag.find("a", href=True) or {}).get("href", "")
        url = urljoin(target.url, href) if href else target.url
        loc_el = tag.find(string=re.compile(r"北京|上海|杭州|深圳|新加坡|远程"))
        location = str(loc_el).strip() if loc_el else ""
        key = (title, url)
        if key in seen:
            continue
        seen.add(key)
        jobs.append({
            "company": target.company,
            "title": title,
            "url": url,
            "location": location,
            "description": title,
            "source": f"gh-relay/{target.slug}",
            "tags": ["github-relay", target.slug, *target.tags],
            "market": target.market,
            "pool": target.pool,
        })
    return jobs or _generic_extract(html, target)


def _ubiquant_interact(page: Page) -> None:
    # Click "社会招聘" tab and wait for jobs to load
    for label in ("社会招聘", "社招", "Social Recruitment"):
        try:
            page.get_by_text(label, exact=False).first.click(timeout=3000)
            page.wait_for_timeout(2500)
            return
        except PWTimeout:
            continue
        except Exception:
            continue


def _lagou_search_url(keyword: str, city: str = "全国") -> str:
    from urllib.parse import quote
    return f"https://www.lagou.com/jobs/list_{quote(keyword)}?city={quote(city)}&first=true&px=new"


# ── Target registry ─────────────────────────────────────────────────
TARGETS: List[Target] = [
    Target(
        slug="baichuan",
        company="Baichuan",
        url="https://www.baichuan-ai.com/careers",
        extract=_generic_extract,
        tags=["llm", "china", "tier1"],
        pool="watch",
    ),
    Target(
        slug="ubiquant",
        company="Ubiquant",
        url="https://www.ubiquant.com/website/career",
        interact=_ubiquant_interact,
        extract=_ubiquant_extract,
        tags=["quant", "china"],
        pool="watch",
    ),
    Target(
        slug="webank",
        company="WeBank",
        url="https://www.webank.com/careers/",
        extract=_generic_extract,
        tags=["fintech", "china"],
        pool="watch",
    ),
    Target(
        slug="aixcoder",
        company="aiXcoder",
        url="https://www.aixcoder.com/",
        extract=_generic_extract,
        tags=["code-ai", "china"],
        pool="watch",
    ),
    # Lagou aggregator searches by keyword
    Target(
        slug="lagou-llm-bj",
        company="Lagou-search",
        url=_lagou_search_url("大模型 算法", "北京"),
        extract=_generic_extract,
        tags=["aggregator", "lagou", "llm"],
        pool="watch",
    ),
    Target(
        slug="lagou-agent-bj",
        company="Lagou-search",
        url=_lagou_search_url("AI Agent", "北京"),
        extract=_generic_extract,
        tags=["aggregator", "lagou", "agent"],
        pool="watch",
    ),
]


def render(page: Page, target: Target) -> Optional[str]:
    page.set_default_navigation_timeout(30000)
    try:
        page.goto(target.url, wait_until="domcontentloaded")
    except PWTimeout:
        print(f"  [warn] goto timeout: {target.slug}", file=sys.stderr)
    # Give SPA bundle a chance to mount
    page.wait_for_timeout(3500)
    if target.wait_selector:
        try:
            page.wait_for_selector(target.wait_selector, timeout=target.wait_timeout_ms, state="attached")
        except PWTimeout:
            pass
    if target.interact:
        try:
            target.interact(page)
        except Exception as e:
            print(f"  [warn] interact failed for {target.slug}: {e}", file=sys.stderr)
    # Scroll a bit so lazy-loaded items appear
    try:
        for _ in range(4):
            page.evaluate("window.scrollBy(0, 800)")
            page.wait_for_timeout(600)
    except Exception:
        pass
    return page.content()


def run() -> int:
    ts = datetime.now(timezone.utc).isoformat()
    summary: Dict = {"generated_at": ts, "targets": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        for tgt in TARGETS:
            print(f"== {tgt.slug} ==")
            page = context.new_page()
            try:
                html = render(page, tgt) or ""
                raw_html_path = RAW_DIR / f"{tgt.slug}.html"
                raw_html_path.write_text(html, encoding="utf-8")
                # text snapshot
                try:
                    text = page.evaluate("() => document.body.innerText") or ""
                except Exception:
                    text = ""
                (RAW_DIR / f"{tgt.slug}.txt").write_text(text, encoding="utf-8")
                # extractor
                extractor = tgt.extract or _generic_extract
                jobs = extractor(html, tgt)
                out_path = OUT_DIR / f"{tgt.slug}.json"
                out_path.write_text(json.dumps({
                    "generated_at": ts,
                    "target": asdict(tgt) | {"interact": None, "extract": None},
                    "jobs": jobs,
                }, ensure_ascii=False, indent=2))
                summary["targets"][tgt.slug] = {
                    "company": tgt.company, "url": tgt.url,
                    "html_bytes": len(html), "text_bytes": len(text), "jobs": len(jobs),
                }
                print(f"  ok: html={len(html)} text={len(text)} jobs={len(jobs)}")
            except Exception as e:
                print(f"  FAIL: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                summary["targets"][tgt.slug] = {"error": str(e)[:300]}
            finally:
                page.close()
        browser.close()
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(run())
