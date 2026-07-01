#!/usr/bin/env python3
"""
axios_filter.py — Re-host the Axios RSS firehose with Politics removed.

Why a classifier?
-----------------
Axios' feed only tags items with <category>top</category> (a prominence label,
not a topic). The real section lives only on each article page, which Cloudflare
403-blocks from CI IPs, and Axios exposes no per-section feeds (all 404/403). So
there is no server-provided topic signal we can reach. We therefore classify
each item from its own title + summary with the cheapest model (Haiku).

Design for accuracy + near-zero cost:
  * Each item is classified AT MOST ONCE and the verdict is cached by <guid>
    (news items never change section), so ongoing cost is only new articles.
  * On any error / missing API key, the item is KEPT (never drop on uncertainty).
  * The feed is re-serialised byte-for-byte minus the dropped <item> blocks, so
    images, content:encoded, authors etc. survive untouched.

Set the API key as the repo secret ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import html
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
except ImportError:
    requests = None

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ITEM_RE = re.compile(r"<item\b.*?</item>", re.DOTALL)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.DOTALL)
GUID_RE = re.compile(r"<guid\b[^>]*>(.*?)</guid>", re.DOTALL)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
DESC_RE = re.compile(r"<description>(.*?)</description>", re.DOTALL)

PROMPT = (
    "You classify Axios news items. Decide whether this item is PRIMARILY about "
    "U.S. politics or politics & policy — e.g. elections, campaigns, Congress, the "
    "White House / administration, courts and SCOTUS rulings, partisan policy "
    "fights, or political figures acting politically. Business, tech, economy, "
    "markets, science, health, climate, sports and culture are NOT politics, even "
    "if a politician is mentioned in passing.\n\n"
    "Headline: {title}\nSummary: {desc}\n\n"
    "Answer with exactly one word: yes or no."
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch(url: str) -> str:
    if requests is None:
        raise RuntimeError("The 'requests' package is required: pip install requests")
    headers = {"User-Agent": UA, "Accept": "application/rss+xml,text/xml,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------- #
# Classification (cheapest model; cached per guid)
# --------------------------------------------------------------------------- #
def make_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key)


def classify_politics(client, model: str, title: str, desc: str) -> bool:
    msg = client.messages.create(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": PROMPT.format(title=title, desc=desc)}],
    )
    out = "".join(getattr(b, "text", "") for b in msg.content).strip().lower()
    return out.startswith("y")


def item_text(block: str) -> tuple[str, str]:
    t = TITLE_RE.search(block)
    d = DESC_RE.search(block)
    title = html.unescape(t.group(1)).strip() if t else ""
    desc = html.unescape(d.group(1)) if d else ""
    desc = re.sub(r"<[^>]+>", " ", desc)            # strip HTML tags
    desc = re.sub(r"\s+", " ", desc).strip()[:400]  # collapse + truncate (cheap)
    return title, desc


# --------------------------------------------------------------------------- #
# Feed surgery (preserve original item bytes exactly)
# --------------------------------------------------------------------------- #
def split_feed(raw: str) -> tuple[str, list[str], str]:
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


def adjust_head(head: str, title: str, feed_self: str) -> str:
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
# State (guid -> is-politics verdict; each item classified at most once)
# --------------------------------------------------------------------------- #
def load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"verdict": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[abort] {path} is corrupt JSON ({exc}); fix or delete it.")
    if not isinstance(data, dict):
        raise SystemExit(f"[abort] {path} is not a JSON object.")
    data.setdefault("verdict", {})
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
        raise SystemExit("[abort] feed had no <item> blocks; leaving output untouched.")

    state = load_state(args.state)
    verdict = state["verdict"]

    client = None
    if args.api_key:
        client = make_client(args.api_key)
    else:
        print("[warn] no ANTHROPIC_API_KEY set — classifier off, keeping ALL items.",
              file=sys.stderr)

    kept: list[str] = []
    classified = 0
    dropped = 0
    for block in items:
        key = item_key(block)
        is_pol = verdict.get(key)
        if is_pol is None and client is not None and classified < args.classify_max:
            title, desc = item_text(block)
            try:
                is_pol = classify_politics(client, args.model, title, desc)
                verdict[key] = is_pol
                classified += 1
                time.sleep(args.delay)
            except Exception as exc:
                print(f"[warn] classify failed ({exc}); keeping: {title[:70]}", file=sys.stderr)
                is_pol = False  # keep on uncertainty
        if is_pol:
            dropped += 1
            continue
        kept.append(block)

    out = adjust_head(head, args.title, args.feed_self) + "".join(kept) + tail

    if args.dry_run:
        print(f"[dry-run] {len(items)} items, kept {len(kept)}, dropped {dropped}, "
              f"classified {classified} this run. Nothing written.")
        return 0

    _atomic_write(args.out, out)
    save_state(args.state, state)
    print(f"Axios (no Politics): kept {len(kept)}/{len(items)} items "
          f"(dropped {dropped}; {classified} newly classified) -> {args.out}")
    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Filter Politics out of the Axios RSS feed via a cheap classifier.")
    p.add_argument("--feed-url", default=_env("AX_FEED_URL", "https://api.axios.com/feed/"))
    p.add_argument("--title", default=_env("AX_TITLE", "Axios (no Politics)"))
    p.add_argument("--feed-self", default=_env("AX_FEED_SELF", ""))
    p.add_argument("--model", default=_env("AX_MODEL", "claude-haiku-4-5-20251001"))
    p.add_argument("--api-key", default=_env("ANTHROPIC_API_KEY", ""))
    p.add_argument("--out", default=_env("AX_OUT", "feed.xml"))
    p.add_argument("--state", default=_env("AX_STATE", "state.json"))
    p.add_argument("--classify-max", type=int, default=int(_env("AX_CLASSIFY_MAX", "150")),
                   help="max classifications per run (bounds the one-time backfill)")
    p.add_argument("--delay", type=float, default=float(_env("AX_DELAY", "0.1")))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p


def main(argv=None) -> int:
    return run(build_argparser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
