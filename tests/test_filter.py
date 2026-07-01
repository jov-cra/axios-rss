"""
Unit tests for axios_filter. All offline — the classifier is monkeypatched, so
no API key or network is needed.
Run:  python tests/test_filter.py   (or)   python -m pytest -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import axios_filter as af  # noqa: E402

FIXTURE = (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_text(encoding="utf-8")


def _install_fakes():
    """Feed fetch -> fixture; classifier -> True when 'politics' is in the title."""
    af.fetch = lambda url: FIXTURE
    af.make_client = lambda api_key: "DUMMY_CLIENT"
    af.classify_politics = lambda client, model, title, desc: "politics" in title.lower()


# --------------------------------------------------------------------------- #
# Feed surgery
# --------------------------------------------------------------------------- #
def test_split_feed():
    head, items, tail = af.split_feed(FIXTURE)
    assert len(items) == 2
    assert "<channel>" in head and "<title>Axios</title>" in head
    assert tail.strip().endswith("</channel></rss>")


def test_item_key_and_text():
    _, items, _ = af.split_feed(FIXTURE)
    assert af.item_key(items[0]) == "https://www.axios.com/2026/07/01/sample-politics"
    title, desc = af.item_text(items[0])
    assert title == "Placeholder politics headline"
    assert desc == "placeholder"          # HTML unescaped + tags stripped


def test_adjust_head():
    head, _, _ = af.split_feed(FIXTURE)
    out = af.adjust_head(head, "Axios (no Politics)", "https://x.github.io/axios/feed.xml")
    assert "lastBuildDate" not in out
    assert "<title>Axios (no Politics)</title>" in out
    assert '<atom:link href="https://x.github.io/axios/feed.xml" rel="self"' in out


# --------------------------------------------------------------------------- #
# End-to-end
# --------------------------------------------------------------------------- #
def test_run_drops_politics_keeps_rest():
    import tempfile, os, json
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    _install_fakes()
    af.main(["--feed-url", "https://api.axios.com/feed/", "--api-key", "test",
             "--delay", "0", "--out", out, "--state", state])
    result = Path(out).read_text(encoding="utf-8")

    assert "sample-tech" in result and "sample-politics" not in result
    assert result.count("<item>") == 1
    # fidelity preserved for the kept item
    assert "<![CDATA[" in result and "media:content" in result and "dc:creator" in result
    # verdict cached (each item classified at most once)
    v = json.loads(Path(state).read_text())["verdict"]
    assert v["https://www.axios.com/2026/07/01/sample-politics"] is True
    assert v["https://www.axios.com/2026/07/01/sample-tech"] is False


def test_run_keeps_all_without_api_key():
    import tempfile, os
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    _install_fakes()
    af.main(["--feed-url", "https://api.axios.com/feed/", "--api-key", "",
             "--out", out, "--state", state])
    result = Path(out).read_text(encoding="utf-8")
    assert result.count("<item>") == 2      # no key -> nothing dropped


def test_run_is_deterministic_no_churn():
    import tempfile, os, hashlib
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    _install_fakes()
    args = ["--feed-url", "https://api.axios.com/feed/", "--api-key", "test",
            "--delay", "0", "--out", out, "--state", state]
    af.main(args)
    h1 = hashlib.md5(Path(out).read_bytes()).hexdigest()
    af.main(args)                        # second run, verdicts cached
    h2 = hashlib.md5(Path(out).read_bytes()).hexdigest()
    assert h1 == h2                      # identical bytes -> no commit churn


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
