#!/usr/bin/env python3
"""
axios_filter.py — Re-host the Axios RSS firehose with one or more sections removed
(by default: Politics).

Why this exists
---------------
The Axios feed (api.axios.com/feed/) is a single firehose. Its per-item
<category> is only "top" (a prominence label), so neither the feed nor a reader
like Tapestry can filter by topic. The REAL section, however, lives on each
article page as `<meta name="category" content="Politics & Policy">` (with a
matching breadcrumb link to axios.com/<section>). So this tool:

  1. fetches the Axios feed,
  2. for each item, fetches the article page once and reads its section
     (cached by <guid> so every article is fetched at most once),
  3. drops items whose section matches a drop-term (default "politics"),
  4. re-serialises the feed byte-for-byte minus the dropped <item> blocks
     (so images, content:encoded, authors etc. are preserved exactly).

Output is a normal RSS feed you host on GitHub Pages and subscribe to in any
reader. Deterministic + free: no AI, no API key.

Usage:
    python axios_filter.py                       # normal run
    python axios_filter.py --report              # also list all sections seen
    python axios_filter.py --drop politics,world # drop several sections
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

try:
    import requests
except ImportError:  # keeps the module importable for tests without requests
    requests = None

from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ITEM_RE = re.compile(r"<item\b.*?</item>", re.DOTALL)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.DOTALL)
GUID_RE = re.compile(r"<guid\b[^>]*>(.*?)</guid>", re.DOTALL)
SECTION_HREF_RE = re.compile(r"^https?://(?:www\.)?axios\.com/([a-z][a-z-]+)/?$")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url: str) -> str:
    if requests is None:
        raise RuntimeError("The 'requests' package is required: pip install requests")
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


def article_section(url: str) -> str:
    """Return an article's section, e.g. 'Politics & Policy'. '' if not found."""
    soup = BeautifulSoup(fetch(url), "html.parser")
    m = soup.find("meta", attrs={"name": "category"})
    if m and m.get("content"):
        return m["content"].strip()
    # fallback: a top-of-page breadcrumb link to a section landing page
    for a in soup.find_all("a", href=True):
        if SECTION_HREF_RE.match(a["href"]):
            text = a.get_text(strip=True)
            if text:
                return text
    return ""


# --------------------------------------------------------------------------- #
# Feed surgery (preserve original item bytes exactly)
# --------------------------------------------------------------------------- #
def split_feed(raw: str) -> tuple[str, list[str], str]:
    """(head, [item blocks], tail). head/tail keep the channel metadata verbatim."""
    items = ITEM_RE.findall(raw)
    if not items:
        return raw, [], ""
    first = raw.index("<item")
    last = raw.rindex("</item>") + len("</item>")
    return raw[:first], items, raw[last:]


def item_key(block: str) -> str:
    g = GUID_RE.search(block)
    if g and g.group(1).strip():
        return g.group(1).strip()
    l = LINK_RE.search(block)
    return l.group(1).strip() if l else block[:80]


def item_link(block: str) -> str:
    l = LINK_RE.search(block)
    return l.group(1).strip() if l else ""


def is_dropped(section: str, drop_terms: list[str]) -> bool:
    s = (section or "").lower()
    return any(t in s for t in drop_terms)


def adjust_head(head: str, title: str, feed_self: str) -> str:
    # Remove upstream lastBuildDate so identical item sets produce identical
    # output (no commit churn when only Axios' build timestamp moved).
    head = re.sub(r"<lastBuildDate>.*?</lastBuildDate>", "", head, flags=re.DOTALL)
    if title:
        head = re.sub(r"<title>.*?</title>", lambda _m: f"<title>{escape(title)}</title>",
                      head, count=1, flags=re.DOTALL)
    if feed_self:
        head = re.sub(r"<atom:link\b[^>]*\brel=\"self\"[^>]*/>",
                      lambda _m: f'<atom:link href="{escape(feed_self)}" rel="self" type="application/rss+xml"/>',
                      head)
    return head


# --------------------------------------------------------------------------- #
# State (guid -> section cache; each article fetched at most once)
# --------------------------------------------------------------------------- #
def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"section": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[abort] {path} is corrupt JSON ({exc}); fix or delete it.")
    if not isinstance(data, dict):
        raise SystemExit(f"[abort] {path} is not a JSON object.")
    data.setdefault("section", {})
    return data


def _atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def save_state(path: str, state: dict) -> None:
    _atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(args) -> int:
    raw = fetch(args.feed_url)
    head, items, tail = split_feed(raw)
    if not items:
        raise SystemExit("[abort] feed had no <item> blocks (fetch/format problem); leaving output untouched.")

    state = load_state(args.state)
    cache = state["section"]
    counts: dict[str, int] = {}
    kept: list[str] = []
    fetched = 0

    for block in items:
        key = item_key(block)
        section = cache.get(key)
        if section is None:
            if fetched < args.fetch_max:
                link = item_link(block)
                try:
                    section = article_section(link)
                    cache[key] = section
                    fetched += 1
                    time.sleep(args.delay)
                except Exception as exc:
                    print(f"[warn] could not read section for {link}: {exc}", file=sys.stderr)
                    section = ""  # unknown -> keep (never drop on uncertainty)
            else:
                section = ""  # over the per-run budget -> keep this run, resolve next run
        label = section or "(unknown)"
        counts[label] = counts.get(label, 0) + 1
        if is_dropped(section, args.drop):
            continue
        kept.append(block)

    if args.report or args.debug:
        print("Sections seen this run (count · section):")
        for sec, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
            flag = "DROP" if is_dropped(sec, args.drop) else "keep"
            print(f"  {n:>3}  [{flag}]  {sec}")

    out = adjust_head(head, args.title, args.feed_self) + "".join(kept) + tail

    if args.dry_run:
        print(f"[dry-run] {len(items)} items, kept {len(kept)}, dropped {len(items) - len(kept)}; nothing written.")
        return 0

    _atomic_write(args.out, out)
    save_state(args.state, state)
    print(f"Filtered Axios feed: kept {len(kept)}/{len(items)} items "
          f"(dropped {len(items) - len(kept)}; drop-terms={args.drop}) -> {args.out}")
    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Filter the Axios RSS feed by article section.")
    p.add_argument("--feed-url", default=_env("AX_FEED_URL", "https://api.axios.com/feed/"))
    p.add_argument("--drop", default=_env("AX_DROP", "politics"),
                   help="comma-separated section substrings to remove (default 'politics')")
    p.add_argument("--title", default=_env("AX_TITLE", "Axios (filtered)"))
    p.add_argument("--feed-self", default=_env("AX_FEED_SELF", ""))
    p.add_argument("--out", default=_env("AX_OUT", "feed.xml"))
    p.add_argument("--state", default=_env("AX_STATE", "state.json"))
    p.add_argument("--fetch-max", type=int, default=int(_env("AX_FETCH_MAX", "120")),
                   help="max article-page fetches per run (bounds the first backfill)")
    p.add_argument("--delay", type=float, default=float(_env("AX_FETCH_DELAY", "0.4")),
                   help="seconds between article fetches (politeness)")
    p.add_argument("--report", action="store_true", help="print the section distribution")
    p.add_argument("--dry-run", action="store_true", help="don't write, just report")
    p.add_argument("--debug", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)
    args.drop = [t.strip().lower() for t in args.drop.split(",") if t.strip()]
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
