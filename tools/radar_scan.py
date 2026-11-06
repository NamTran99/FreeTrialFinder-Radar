#!/usr/bin/env python3
"""
AI Deal & Billing Glitch Radar v3.0 (Cloud Cron Edition with Deduplication)
Scans 10 global Tier-0 sources in parallel, deduplicates against data/seen_radar.json,
and dispatches brand-new hot deals (score >= 85) to Telegram via env secrets.
"""

import concurrent.futures
import html as html_lib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

SSL_CTX = ssl._create_unverified_context()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, application/rss+xml, text/xml, */*;q=0.8",
}

TRIGGER_PATTERN = re.compile(
    r"(白嫖|羊毛|0元|免费|上车|神车|漏洞|Bug价|免绑卡|福利|叠加|车|"
    r"ChatGPT|OpenAI|Claude|Cursor|Gemini|Perplexity|Copilot|Codex|Google\s*One|Google\s*AI|"
    r"Business|Team|Plus|Pro|Ultra|promo|coupon|credit|trial|free\s*seat|"
    r"glitch|loophole|100%\s*off|\$0|халява|абуз|промокод)",
    re.IGNORECASE,
)

HIGH_GLITCH_PATTERN = re.compile(
    r"(白嫖|薅羊毛|0元|神车|漏洞|Bug价|免绑卡|叠加|上车|"
    r"glitch|price\s*error|loophole|100%\s*off|(?<!Delivery\s\()\$0\b|free\s*seat|promoCode|chatgpt\.com/p/|Ngbm|"
    r"халява|абуз|промокод)",
    re.IGNORECASE,
)

PATCHED_PATTERN = re.compile(
    r"(翻车|封车|砍单|已死|失效|薅秃|patched|dead\s*deal|expired|clawback|прикрыли|фикс)",
    re.IGNORECASE,
)


def score_hit(title: str, source: str) -> tuple[int, str]:
    score = 50
    if HIGH_GLITCH_PATTERN.search(title):
        score += 35
    if re.search(r"(Google\s*AI|ChatGPT|Claude|Cursor|Perplexity|Gemini|Pro|Business|24个月|两年|48\s*months)", title, re.I):
        score += 15
    if any(k in source for k in ("Welfare", "羊毛", "GitHub", "GreasyFork", "Freebies", "Gratuit", "eFreebies")):
        score += 20
    if PATCHED_PATTERN.search(title):
        score -= 30
        badge = "⚠️ [NGHI ĐÃ FIX]"
    elif score >= 85:
        badge = "🔥 [KÈO NÓNG / GLITCH 0Đ]"
    else:
        badge = "💡 [TIN AI / DEAL]"
    return score, badge


def fetch_url(url: str, timeout: int = 10) -> bytes:
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return resp.read()
    except Exception:
        out = subprocess.check_output(
            ["curl", "-ksL", "--max-time", str(timeout), "-A", HEADERS["User-Agent"], url]
        )
        return out


def parse_rss_items(raw: bytes, source_label: str, limit: int = 30, require_trigger: bool = True) -> list[dict]:
    results = []
    root = ET.fromstring(raw)
    items = root.findall(".//item")
    if items:
        for item in items[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not require_trigger or TRIGGER_PATTERN.search(title):
                results.append({"source": source_label, "title": title, "url": link})
    else:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns)[:limit]:
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.attrib.get("href", "") if link_el is not None else ""
            if not require_trigger or TRIGGER_PATTERN.search(title):
                results.append({"source": source_label, "title": title, "url": link})
    return results


def scan_nodeseek() -> list[dict]:
    try:
        return parse_rss_items(fetch_url("https://rss.nodeseek.com/"), "NodeSeek", 35)
    except Exception:
        return []


def scan_v2ex() -> list[dict]:
    results = []
    for label, endpoint in [
        ("V2EX Hot", "https://www.v2ex.com/api/topics/hot.json"),
        ("V2EX Latest", "https://www.v2ex.com/api/topics/latest.json"),
    ]:
        try:
            topics = json.loads(fetch_url(endpoint).decode("utf-8"))
            for t in topics:
                title = t.get("title", "")
                url = t.get("url", "")
                replies = t.get("replies", 0)
                if TRIGGER_PATTERN.search(title):
                    results.append({"source": f"{label} ({replies}💬)", "title": title, "url": url})
        except Exception:
            pass
    return results


def scan_ozbargain() -> list[dict]:
    try:
        return parse_rss_items(fetch_url("https://www.ozbargain.com.au/cat/computing/feed"), "OzBargain [AU]", 20, True)
    except Exception:
        return []


def scan_hackernews() -> list[dict]:
    results = []
    try:
        url = "https://hn.algolia.com/api/v1/search_by_date?query=free+credits&tags=story&hitsPerPage=15"
        data = json.loads(fetch_url(url, timeout=8).decode("utf-8"))
        for hit in data.get("hits", []):
            title = hit.get("title") or ""
            story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
            results.append({"source": "HackerNews [YC]", "title": title, "url": story_url})
    except Exception:
        pass
    return results


def scan_github_leaks() -> list[dict]:
    results = []
    queries = [
        "ChatGPT Business promo",
        "免费额度 AI",
        "白嫖 API",
        "2api",
        "Cursor free trial",
        "free-llm-intel",
    ]
    for q in queries:
        try:
            encoded = urllib.parse.quote(q)
            url = f"https://api.github.com/search/repositories?q={encoded}&sort=updated&order=desc&per_page=5"
            data = json.loads(fetch_url(url, timeout=10).decode("utf-8"))
            for repo in data.get("items", []):
                full_name = repo.get("full_name", "")
                if "Google_AI_Pro_2Year_Free" in full_name:
                    continue
                desc = (repo.get("description") or "").strip()
                html_url = repo.get("html_url", "")
                stars = repo.get("stargazers_count", 0)
                results.append(
                    {
                        "source": f"GitHub Repo (⭐{stars})",
                        "title": f"{full_name}: {desc[:110]}",
                        "url": html_url,
                    }
                )
        except Exception:
            pass
    return results


def main():
    state_path = Path(os.environ.get("RADAR_STATE_FILE", "data/seen_radar.json"))
    seen_radar: dict = {}
    if state_path.exists():
        try:
            seen_radar = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            seen_radar = {}

    scanners = [scan_nodeseek, scan_v2ex, scan_github_leaks, scan_ozbargain, scan_hackernews]
    all_hits = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for fut in concurrent.futures.as_completed([pool.submit(fn) for fn in scanners]):
            try:
                all_hits.extend(fut.result())
            except Exception:
                pass

    new_hot_hits = []
    for hit in all_hits:
        url = hit.get("url", "")
        if not url or url in seen_radar:
            continue
        sc, badge = score_hit(hit.get("title", ""), hit.get("source", ""))
        hit["score"] = sc
        hit["badge"] = badge
        if sc >= 85:
            new_hot_hits.append(hit)
            seen_radar[url] = {
                "title": hit.get("title", ""),
                "source": hit.get("source", ""),
                "score": sc,
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }

    new_hot_hits.sort(key=lambda x: x["score"], reverse=True)
    print(f"Radar v3.0: {len(new_hot_hits)} new hot hits (total tracked: {len(seen_radar)})")

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if new_hot_hits and bot_token and chat_id:
        lines = [f"<b>🎯 AI Deal & Glitch Radar v3.0 ({len(new_hot_hits)} Kèo Mới)</b>\n"]
        for i, h in enumerate(new_hot_hits[:10], 1):
            t = html_lib.escape(h["title"][:95])
            u = html_lib.escape(h["url"])
            s = html_lib.escape(h["source"])
            lines.append(f"{i}. <b>[{h['score']}]</b> ({s}) <a href=\"{u}\">{t}</a>")
        payload = json.dumps(
            {
                "chat_id": chat_id,
                "text": "\n".join(lines),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
        ).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10, context=SSL_CTX)
            print("Sent new radar hits to Telegram!")
        except Exception as e:
            print(f"Telegram push error: {e}")

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(seen_radar, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
