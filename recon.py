#!/usr/bin/env python3
"""One-off recon: which Axios feed URLs are reachable FROM THIS RUNNER, and do
any of them carry real per-item topic <category> values (vs. just 'top')?
Run via the 'Axios feed recon' workflow (workflow_dispatch)."""
import re
import requests

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SECTIONS = ["technology", "business", "economy", "world", "science", "health",
            "energy-climate", "politics-policy"]
URLS = ["https://api.axios.com/feed/", "https://www.axios.com/feeds/feed.rss"]
for s in SECTIONS:
    URLS += [f"https://api.axios.com/feed/{s}",
             f"https://api.axios.com/feed/{s}/",
             f"https://www.axios.com/{s}/feed",
             f"https://www.axios.com/feeds/{s}.rss"]

print("STATUS  items  distinct <category> values (first 8)                URL")
for url in URLS:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        body = r.text if "xml" in r.headers.get("content-type", "") or "<rss" in r.text[:400] else ""
        cats = re.findall(r"<category>(.*?)</category>", body or r.text)
        distinct = sorted(set(c.strip() for c in cats))
        nitems = (body or r.text).count("<item")
        print(f"{r.status_code:>4}  {nitems:>5}  {str(distinct[:8]):<55}  {url}")
    except Exception as e:
        print(f" ERR  {'':>5}  {type(e).__name__}: {str(e)[:40]:<40}  {url}")
