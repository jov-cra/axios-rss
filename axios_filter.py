#!/usr/bin/env python3
"""
axios_filter.py — Re-host the Axios RSS firehose with Politics + World removed.

Axios' feed only tags items <category>top</category> (prominence, not topic); the
real section lives only on the article page, which Cloudflare 403-blocks from CI,
and Axios exposes no per-section feeds. So we classify each item from its own
title + summary with the cheapest model (Haiku).

Robustness (QA-hardened):
  * Each item is classified AT MOST ONCE per prompt version; verdicts cached by
    <guid> in state.json -> ongoing cost is only new articles (cents/month).
  * FAIL-CLOSED: no API key, or a systematic classifier failure, ABORTS the run
    without overwriting -> the last good filtered feed stays and GitHub alerts.
    (We never silently ship the unfiltered firehose.)
  * Single transient failures keep that one item (fail-safe), client auto-retries.
  * The assembled feed is XML-validated before writing (guards the byte-surgery).
  * FORCE_KEEP / FORCE_DROP give a manual override for any misclassification.
  * state.json is pruned so it can't grow without bound.

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
import xml.dom.minidom as minidom
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

try:
    import requests
except ImportError:
    requests = None

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
ITEM_RE = re.compile(r"<item\b.*?</item>", re.DOTALL)
LINK_RE = re.compile(r"<link>(.*?)</link>", re.DOTALL)
GUID_RE = re.compile(r"<guid\b[^>]*>(.*?)</guid>", re.DOTALL)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
DESC_RE = re.compile(r"<description>(.*?)</description>", re.DOTALL)

PROMPT_VERSION = "v2"   # bump to force re-classification of cached verdicts
PROMPT = (
    "You classify Axios news items. Answer whether the item is PRIMARILY about "
    "politics, government, policy, or foreign affairs.\n\n"
    "Count as POLITICS (yes): U.S. elections and campaigns; Congress and legislation "
    "as a partisan fight; the White House / administration acting politically; courts "
    "and SCOTUS rulings; political appointments; partisan policy battles; a political "
    "figure's political conduct; AND world / foreign affairs — wars, armed conflicts, "
    "diplomacy, foreign governments and leaders, international relations, geopolitics.\n\n"
    "Do NOT count as politics (no): business, companies, markets, the economy, the "
    "Federal Reserve and interest rates, corporate or antitrust / regulatory news framed "
    "as a business story, technology, science, health and medicine, climate and energy as "
    "science/industry, sports, media and culture — EVEN IF a politician, agency, or "
    "government body is mentioned. Trade and immigration count as politics only when the "
    "item is primarily about the political / policy fight itself, not its economic or "
    "business impact.\n\n"
    "Judge the PRIMARY subject, not incidental mentions. If it is genuinely borderline "
    "or you are unsure, answer no.\n\n"
    "Headline: {title}\nSummary: {desc}\n\n"
    "Answer with exactly one word, lowercase: yes or no."
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
# Classification (cheapest model; cached per guid + prompt version)
# --------------------------------------------------------------------------- #
def make_client(api_key: str):
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=4)


def classify_politics(client, model: str, title: str, desc: str) -> bool:
    msg = client.messages.create(
        model=model,
        max_tokens=5,
        messages=[{"role": "user", "content": PROMPT.format(title=title, desc=desc)}],
    )
    out = "".join(getattr(b, "text", "") for b in msg.content).strip().lower()
    return out.startswith("yes")


def item_text(block: str) -> tuple[str, str]:
    t = TITLE_RE.search(block)
    d = DESC_RE.search(block)
    title = html.unescape(t.group(1)).strip() if t else ""
    desc = html.unescape(d.group(1)) if d else ""
    desc = re.sub(r"<[^>]+>", " ", desc)
    desc = re.sub(r"\s+", " ", desc).strip()[:400]
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


def _forced(text: str, terms: list[str]) -> bool:
    t = text.lower()
    return any(term in t for term in terms)


CHART_HOSTS = ("datawrapper", "dwcdn", "flourish", "infogram")
MEDIA_CONTENT_RE = re.compile(r"<media:content\b([^>]*)>(.*?)</media:content>", re.DOTALL)
THUMB_RE = re.compile(r"<media:thumbnail\b[^>]*>", re.DOTALL)


def _media_content(block: str) -> tuple[str, str]:
    """Return (url, inner-description) of the item's first <media:content>."""
    m = MEDIA_CONTENT_RE.search(block)
    if not m:
        return "", ""
    u = re.search(r'url="([^"]+)"', m.group(1))
    return (u.group(1) if u else ""), m.group(2)


def _is_chart(url: str, desc: str) -> bool:
    """Axios credits charts as 'Data: … ; Chart: …' / 'Map: …' but photos as
    'Photo: …' — so the media:description tells charts from photos reliably."""
    d = desc.lower()
    if any(k in d for k in ("chart", "data:", "map:", "table:", "graphic")):
        return True
    return any(h in url for h in CHART_HOSTS)


def _hires(url: str) -> str:
    """Datawrapper's fallback.png is low-res (pixelates when scaled up); full.png
    is crisp. Upgrade if it actually exists (fail-safe to the original)."""
    if "dwcdn.net" in url and url.endswith("/fallback.png"):
        cand = url[: -len("fallback.png")] + "full.png"
        try:
            if requests.head(cand, timeout=10, allow_redirects=True).status_code == 200:
                return cand
        except Exception:
            pass
    return url


def _drop_media_content(block: str) -> str:
    """Remove the chart's <media:content> once it's inline in the body, so the
    reader can't render the same chart a second time (top + bottom)."""
    return MEDIA_CONTENT_RE.sub("", block, count=1)


def inject_chart(block: str) -> str:
    """Charts live in <media:content> (a static PNG), not inline in the body, so
    readers that ignore <media:content> (e.g. Tapestry) don't show them. If the
    item's media is a chart, prepend a crisp <img> to content:encoded AND drop the
    <media:content> enclosure so the chart appears exactly once, at the top.
    Photos are left untouched. Idempotent."""
    url, desc = _media_content(block)
    if not url or not _is_chart(url, desc):
        return block
    ce = re.search(r"<content:encoded><!\[CDATA\[(.*?)\]\]></content:encoded>", block, re.DOTALL)
    if not ce:
        return block
    img = _hires(url)
    if img in ce.group(1) or url in ce.group(1):
        return _drop_media_content(block)   # already inline -> just remove the duplicate enclosure
    new_ce = f'<content:encoded><![CDATA[<p><img src="{img}" alt="Chart"/></p>{ce.group(1)}]]></content:encoded>'
    block = block[:ce.start()] + new_ce + block[ce.end():]
    return _drop_media_content(block)


def strip_thumbnail(block: str) -> str:
    """Drop the tiny <media:thumbnail> some readers show as a small trailing image."""
    return THUMB_RE.sub("", block)


# --------------------------------------------------------------------------- #
# State
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
    _atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


def prune_state(verdict: dict, keep_days: int) -> int:
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    stale = [k for k, v in verdict.items() if isinstance(v, dict) and v.get("seen", "") < cutoff]
    for k in stale:
        del verdict[k]
    return len(stale)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run(args) -> int:
    raw = fetch(args.feed_url)
    head, items, tail = split_feed(raw)
    if not items:
        raise SystemExit("[abort] feed had no <item> blocks; leaving output untouched.")

    if not args.api_key:
        raise SystemExit("[abort] ANTHROPIC_API_KEY not set — refusing to publish the "
                         "unfiltered firehose. Add the secret and re-run.")
    client = make_client(args.api_key)

    state = load_state(args.state)
    verdict = state["verdict"]
    today = date.today().isoformat()

    kept: list[str] = []
    dropped = attempted = failed = 0
    for block in items:
        key = item_key(block)
        title, desc = item_text(block)
        hay = f"{key} {title}"

        if _forced(hay, args.force_keep):            # manual override: always keep
            is_pol = False
        elif _forced(hay, args.force_drop):          # manual override: always drop
            is_pol = True
        else:
            rec = verdict.get(key)
            if isinstance(rec, dict) and rec.get("v") == PROMPT_VERSION:
                is_pol = bool(rec["pol"])            # cached hit
                rec["seen"] = today                 # refresh marker (for pruning)
            elif attempted < args.classify_max:
                attempted += 1
                try:
                    is_pol = classify_politics(client, args.model, title, desc)
                    print(f"[classify] {'POL ' if is_pol else 'keep'}  {title[:80]}")
                    verdict[key] = {"pol": is_pol, "v": PROMPT_VERSION, "seen": today}
                except Exception as exc:
                    failed += 1
                    print(f"[warn] classify failed ({exc}); keeping: {title[:70]}", file=sys.stderr)
                    is_pol = False                  # keep on uncertainty; NOT cached -> retried next run
            else:
                is_pol = bool(rec["pol"]) if isinstance(rec, dict) else False  # over budget -> best effort

        if is_pol:
            dropped += 1
            continue
        if args.inject_charts:
            block = strip_thumbnail(inject_chart(block))
        kept.append(block)

    # FAIL-CLOSED: if every classification attempt failed, don't overwrite the feed.
    if attempted > 0 and failed == attempted:
        raise SystemExit(f"[abort] all {attempted} classification(s) failed "
                         "(bad key / model / API outage?); leaving the feed untouched.")

    out = adjust_head(head, args.title, args.feed_self) + "".join(kept) + tail

    # validate before writing (protects against byte-surgery corruption)
    try:
        minidom.parseString(out)
    except Exception as exc:
        raise SystemExit(f"[abort] assembled feed is not well-formed XML ({exc}); not writing.")

    pruned = prune_state(verdict, args.prune_days)

    if args.dry_run:
        print(f"[dry-run] {len(items)} items, kept {len(kept)}, dropped {dropped}, "
              f"classified {attempted} ({failed} failed), pruned {pruned}. Nothing written.")
        return 0

    _atomic_write(args.out, out)
    save_state(args.state, state)
    print(f"Axios (no Politics/World): kept {len(kept)}/{len(items)} "
          f"(dropped {dropped}; {attempted} classified, {failed} failed; pruned {pruned}) -> {args.out}")
    return 0


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Filter Politics + World out of the Axios RSS feed.")
    p.add_argument("--feed-url", default=_env("AX_FEED_URL", "https://api.axios.com/feed/"))
    p.add_argument("--title", default=_env("AX_TITLE", "Axios (no Politics)"))
    p.add_argument("--feed-self", default=_env("AX_FEED_SELF", ""))
    p.add_argument("--model", default=_env("AX_MODEL", "claude-haiku-4-5-20251001"))
    p.add_argument("--api-key", default=_env("ANTHROPIC_API_KEY", ""))
    p.add_argument("--out", default=_env("AX_OUT", "feed.xml"))
    p.add_argument("--state", default=_env("AX_STATE", "state.json"))
    p.add_argument("--classify-max", type=int, default=int(_env("AX_CLASSIFY_MAX", "150")))
    p.add_argument("--prune-days", type=int, default=int(_env("AX_PRUNE_DAYS", "45")))
    p.add_argument("--force-keep", default=_env("AX_FORCE_KEEP", ""),
                   help="comma-separated substrings (guid/title) to always KEEP")
    p.add_argument("--force-drop", default=_env("AX_FORCE_DROP", ""),
                   help="comma-separated substrings (guid/title) to always DROP")
    p.add_argument("--inject-charts", dest="inject_charts", action="store_true",
                   default=_env("AX_INJECT_CHARTS", "1") not in ("0", "false", "False", ""),
                   help="inline chart images (Datawrapper etc.) into the body (default on)")
    p.add_argument("--no-inject-charts", dest="inject_charts", action="store_false")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_argparser().parse_args(argv)
    args.force_keep = [s.strip().lower() for s in args.force_keep.split(",") if s.strip()]
    args.force_drop = [s.strip().lower() for s in args.force_drop.split(",") if s.strip()]
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
