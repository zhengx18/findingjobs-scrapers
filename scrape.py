#!/usr/bin/env python3
"""Render & extract job listings from CN tech career sites + Lagou aggregator.

Runs in GitHub Actions (US/EU egress, real-browser JS rendering with stealth args
to bypass basic anti-bot).
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
from urllib.parse import urljoin, quote

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

OUT_DIR = Path(__file__).parent / "data"
RAW_DIR = OUT_DIR / "raw"
OUT_DIR.mkdir(parents=True, exist_ok=True)
RAW_DIR.mkdir(parents=True, exist_ok=True)


# ── Stealth helpers ────────────────────────────────────────────────
STEALTH_INIT_JS = """
// hide webdriver flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
// fake plugins (some bot checks count plugins)
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en-US','en']});
// chrome runtime
window.chrome = {runtime: {}};
// permissions API often probed
const oq = window.navigator.permissions && window.navigator.permissions.query;
if (oq) {
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : oq(p);
}
"""


@dataclass
class Target:
    slug: str
    company: str
    url: str
    interact: Optional[Callable[[Page], None]] = None
    extract: Optional[Callable[[str, "Target", Page], List[Dict]]] = None
    wait_timeout_ms: int = 15000
    tags: List[str] = field(default_factory=list)
    market: str = "cn"
    pool: str = "watch"


# ── Extractors ─────────────────────────────────────────────────────
def _jd(company: str, slug: str, title: str, url: str, location: str = "",
        description: str = "", tags: List[str] = None, market="cn", pool="watch") -> Dict:
    return {
        "company": company, "title": title, "url": url, "location": location,
        "description": description or title,
        "source": f"gh-relay/{slug}",
        "tags": ["github-relay", slug, *(tags or [])],
        "market": market, "pool": pool,
    }


def _ubiquant_extract(html: str, target: Target, page: Page) -> List[Dict]:
    """After dismiss + tab click, extract via DOM query on the live page (more reliable than HTML)."""
    jobs: List[Dict] = []
    try:
        # Try a few common selectors for job items
        candidates = page.evaluate("""() => {
            const sel = ['.job-item','.position-card','[class*="job"]','[class*="position"]','.list-item'];
            for (const s of sel) {
                const els = document.querySelectorAll(s);
                if (els.length >= 2) {
                    return Array.from(els).map(el => ({
                        text: (el.innerText || '').slice(0, 400),
                        href: (el.querySelector('a')||el).href || '',
                    }));
                }
            }
            // fallback: any <a> whose text has 工程师/算法/研究员
            return Array.from(document.querySelectorAll('a')).filter(a => {
                const t = (a.innerText||'').trim();
                return t.length > 5 && t.length < 100 &&
                       /工程师|算法|研究员|engineer|scientist/i.test(t);
            }).map(a => ({text: a.innerText.trim(), href: a.href||''}));
        }""")
        seen = set()
        for c in candidates or []:
            text = c.get("text", "").strip()
            href = c.get("href", "") or target.url
            if not text or len(text) < 4:
                continue
            # First line is usually title
            first = text.split("\n")[0].strip()
            if (first, href) in seen:
                continue
            seen.add((first, href))
            loc_match = re.search(r"(北京|上海|杭州|深圳|新加坡|远程|广州)", text)
            jobs.append(_jd(
                target.company, target.slug, first[:120], href,
                location=loc_match.group(0) if loc_match else "",
                description=text[:500], tags=target.tags,
                market=target.market, pool=target.pool,
            ))
    except Exception as e:
        print(f"  [warn] ubiquant DOM extract failed: {e}", file=sys.stderr)
    return jobs


def _ubiquant_interact(page: Page) -> None:
    """Dismiss 合格投资者 disclaimer, then click 社会招聘 tab."""
    # Disclaimer "接受" button
    for txt in ("接受", "确认", "Accept", "I Agree"):
        try:
            btn = page.get_by_text(txt, exact=True).first
            btn.click(timeout=2500)
            page.wait_for_timeout(1000)
            break
        except Exception:
            continue
    # Now click 社招 tab
    for txt in ("社会招聘", "社招", "Social Recruitment"):
        try:
            page.get_by_text(txt, exact=False).first.click(timeout=3000)
            page.wait_for_timeout(3500)  # wait for jobs XHR
            break
        except Exception:
            continue


def _lagou_extract(html: str, target: Target, page: Page) -> List[Dict]:
    """Lagou search results. After page mount + scroll, extract from rendered cards."""
    try:
        # Wait for either job cards OR captcha
        page.wait_for_timeout(4500)
        # Detect captcha
        body_text = page.evaluate("() => document.body.innerText || ''") or ""
        if "访问验证" in body_text or "拖动" in body_text or "captcha" in body_text.lower():
            print(f"  [captcha] {target.slug}", file=sys.stderr)
            return []
        items = page.evaluate("""() => {
            // Lagou job cards typically have class 'item__10RTO' or similar; selectors vary.
            const candidates = document.querySelectorAll('div.item__10RTO, .position-list .con-job, .position-card, a[href*="/jobs/"]');
            return Array.from(candidates).slice(0, 40).map(el => {
                const a = el.matches('a') ? el : (el.querySelector('a[href*="/jobs/"]') || el.querySelector('a'));
                return {
                    text: (el.innerText||'').slice(0,400),
                    href: a ? a.href : '',
                };
            });
        }""")
        jobs = []
        seen = set()
        for it in items or []:
            text = it.get("text", "").strip()
            href = it.get("href", "")
            if not text or not href:
                continue
            first = text.split("\n")[0].strip()
            if (first, href) in seen:
                continue
            seen.add((first, href))
            loc = ""
            comp = ""
            for line in text.split("\n"):
                if not loc and re.search(r"北京|上海|杭州|深圳|新加坡|广州", line):
                    loc = line.strip()
                if not comp and any(c in line for c in ["有限", "公司", "AI", "Inc", "Corp"]):
                    comp = line.strip()
            jobs.append(_jd(
                comp or "Lagou-listed", target.slug, first[:120], href,
                location=loc, description=text[:500],
                tags=target.tags, market=target.market, pool=target.pool,
            ))
        return jobs
    except Exception as e:
        print(f"  [warn] lagou extract: {e}", file=sys.stderr)
        return []


# ── Target registry ─────────────────────────────────────────────────
TARGETS: List[Target] = [
    Target(
        slug="ubiquant",
        company="Ubiquant",
        url="https://www.ubiquant.com/website/career",
        interact=_ubiquant_interact,
        extract=_ubiquant_extract,
        tags=["quant", "china"],
        pool="watch",
    ),
    # Lagou keyword aggregators — covers all companies as long as captcha doesn't trigger
    Target(
        slug="lagou-llm-bj",
        company="Lagou-search",
        url=f"https://www.lagou.com/wn/jobs?kd={quote('大模型 算法')}&city={quote('北京')}",
        extract=_lagou_extract,
        tags=["aggregator", "lagou", "llm"],
    ),
    Target(
        slug="lagou-agent-bj",
        company="Lagou-search",
        url=f"https://www.lagou.com/wn/jobs?kd={quote('AI Agent')}&city={quote('北京')}",
        extract=_lagou_extract,
        tags=["aggregator", "lagou", "agent"],
    ),
    Target(
        slug="lagou-post-train-bj",
        company="Lagou-search",
        url=f"https://www.lagou.com/wn/jobs?kd={quote('后训练 LLM')}&city={quote('北京')}",
        extract=_lagou_extract,
        tags=["aggregator", "lagou", "post-training"],
    ),
    Target(
        slug="lagou-llm-sh",
        company="Lagou-search",
        url=f"https://www.lagou.com/wn/jobs?kd={quote('大模型 算法')}&city={quote('上海')}",
        extract=_lagou_extract,
        tags=["aggregator", "lagou", "llm"],
    ),
    Target(
        slug="lagou-llm-hz",
        company="Lagou-search",
        url=f"https://www.lagou.com/wn/jobs?kd={quote('大模型 算法')}&city={quote('杭州')}",
        extract=_lagou_extract,
        tags=["aggregator", "lagou", "llm"],
    ),
]


def render(page: Page, target: Target) -> Optional[str]:
    page.set_default_navigation_timeout(30000)
    try:
        page.goto(target.url, wait_until="domcontentloaded")
    except PWTimeout:
        print(f"  [warn] goto timeout: {target.slug}", file=sys.stderr)
    page.wait_for_timeout(3000)
    if target.interact:
        try:
            target.interact(page)
        except Exception as e:
            print(f"  [warn] interact failed for {target.slug}: {e}", file=sys.stderr)
    # Scroll a bit for lazy loading
    try:
        for _ in range(5):
            page.evaluate("window.scrollBy(0, 900)")
            page.wait_for_timeout(500)
    except Exception:
        pass
    return page.content()


def run() -> int:
    ts = datetime.now(timezone.utc).isoformat()
    summary: Dict = {"generated_at": ts, "targets": {}}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN",
            viewport={"width": 1440, "height": 900},
        )
        context.add_init_script(STEALTH_INIT_JS)

        for tgt in TARGETS:
            print(f"== {tgt.slug} ==")
            page = context.new_page()
            try:
                html = render(page, tgt) or ""
                (RAW_DIR / f"{tgt.slug}.html").write_text(html, encoding="utf-8")
                try:
                    text = page.evaluate("() => document.body.innerText") or ""
                except Exception:
                    text = ""
                (RAW_DIR / f"{tgt.slug}.txt").write_text(text, encoding="utf-8")
                extractor = tgt.extract
                jobs = extractor(html, tgt, page) if extractor else []
                (OUT_DIR / f"{tgt.slug}.json").write_text(json.dumps({
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
