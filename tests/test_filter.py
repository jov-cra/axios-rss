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
             "--out", out, "--state", state])
    result = Path(out).read_text(encoding="utf-8")

    assert "sample-tech" in result and "sample-politics" not in result
    assert result.count("<item>") == 1
    # fidelity preserved for the kept item
    assert "<![CDATA[" in result and "media:content" in result and "dc:creator" in result
    # verdict cached (each item classified at most once)
    v = json.loads(Path(state).read_text())["verdict"]
    assert v["https://www.axios.com/2026/07/01/sample-politics"]["pol"] is True
    assert v["https://www.axios.com/2026/07/01/sample-tech"]["pol"] is False


def test_no_api_key_aborts():
    import tempfile, os
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    _install_fakes()
    try:
        af.main(["--feed-url", "https://api.axios.com/feed/", "--api-key", "",
                 "--out", out, "--state", state])
        assert False, "missing key must abort (fail-closed, never ship the firehose)"
    except SystemExit:
        pass
    assert not os.path.exists(out)           # nothing written


def test_force_overrides_beat_classifier():
    import tempfile, os
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    _install_fakes()   # classifier would call the tech item 'keep', politics 'drop'
    af.main(["--feed-url", "https://api.axios.com/feed/", "--api-key", "test",
             "--out", out, "--state", state,
             "--force-keep", "sample-politics", "--force-drop", "sample-tech"])
    result = Path(out).read_text(encoding="utf-8")
    assert "sample-politics" in result and "sample-tech" not in result


def test_run_is_deterministic_no_churn():
    import tempfile, os, hashlib
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    _install_fakes()
    args = ["--feed-url", "https://api.axios.com/feed/", "--api-key", "test",
            "--out", out, "--state", state]
    af.main(args)
    h1 = hashlib.md5(Path(out).read_bytes()).hexdigest()
    af.main(args)                        # second run, verdicts cached
    h2 = hashlib.md5(Path(out).read_bytes()).hexdigest()
    assert h1 == h2                      # identical bytes -> no commit churn


def test_inject_chart_by_description_and_strip_thumbnail():
    af._hires = lambda u: u   # no network in tests
    base = ('<item><title>Econ</title>'
            '<link>https://www.axios.com/2026/07/01/econ</link>'
            '<content:encoded><![CDATA[<p>body</p>]]></content:encoded>'
            '<media:content medium="image" type="image/jpeg" url="URL" width="600">'
            '<media:description>DESC</media:description></media:content>'
            '<media:thumbnail height="128" url="https://images.axios.com/t.jpg" width="128"/>'
            '<guid>g</guid></item>')
    # Axios-hosted CHART, detected via 'Chart:' in the media:description -> injected
    chart = base.replace("URL", "https://images.axios.com/chart.png").replace("DESC", "Data: X; Chart: Y/Axios")
    out = af.inject_chart(chart)
    assert '<img src="https://images.axios.com/chart.png"' in out
    assert out.count("<content:encoded>") == 1
    assert "media:content" not in out                     # enclosure removed -> chart appears once
    assert af.inject_chart(out) == out                    # idempotent
    # a normal photo -> NOT injected (avoid duplicating the hero)
    photo = base.replace("URL", "https://images.axios.com/p.jpg").replace("DESC", "Photo: Getty Images")
    assert af.inject_chart(photo) == photo
    # trailing thumbnail stripped
    assert "media:thumbnail" not in af.strip_thumbnail(chart)


# --------------------------------------------------------------------------- #
# Chart injection reaches BOTH bodies (regression, 27.08.2026)
# --------------------------------------------------------------------------- #
CHART_ITEM = (
    "<item><title>Chart story</title>"
    "<guid>https://example.com/a</guid>"
    "<description>&lt;p&gt;Body text&lt;/p&gt;</description>"
    "<content:encoded><![CDATA[<p>Body text</p>]]></content:encoded>"
    '<media:content url="https://datawrapper.dwcdn.net/abcde/fallback.png" medium="image">'
    "<media:description>Data: X; Chart: Y/Axios Visuals</media:description>"
    "</media:content></item>"
)


def test_inject_chart_reaches_description_and_content_encoded():
    """Readers render one body or the other — Readwise uses <description>,
    Tapestry <content:encoded>. Injecting into only one hides the chart from
    half of them, which is exactly what shipped until 27.08.2026."""
    af._hires = lambda url: url          # no network in tests
    out = af.inject_chart(CHART_ITEM)
    assert '<p><img src="https://datawrapper.dwcdn.net/abcde/fallback.png" alt="Chart"/></p>' in out
    assert "&lt;p&gt;&lt;img src=" in out and "alt=&quot;Chart&quot;" not in out
    assert "alt=\"Chart\"/&gt;" in out          # escaped copy inside <description>
    assert "<media:content" not in out           # enclosure removed -> chart shows once


def test_inject_chart_is_idempotent():
    af._hires = lambda url: url
    once = af.inject_chart(CHART_ITEM)
    assert af.inject_chart(once) == once


def test_inject_chart_leaves_photos_alone():
    photo = CHART_ITEM.replace("https://datawrapper.dwcdn.net/abcde/fallback.png",
                               "https://images.axios.com/x.jpg").replace(
                               "Data: X; Chart: Y/Axios Visuals", "Photo: Someone/Getty")
    assert af.inject_chart(photo) == photo


# --------------------------------------------------------------------------- #
# Classifier contract (regression, 27.08.2026)
# --------------------------------------------------------------------------- #
class _Block:
    def __init__(self, text): self.text = text


class _FakeMessages:
    def __init__(self, reply): self.reply, self.kwargs = reply, None
    def create(self, **kwargs):
        self.kwargs = kwargs
        return type("M", (), {"content": [_Block(self.reply)]})()


class _FakeClient:
    def __init__(self, reply): self.messages = _FakeMessages(reply)


# Captured at import time: other tests in this file swap af.classify_politics
# for a stub via _install_fakes(), so the real function has to be held onto here.
_REAL_CLASSIFY = af.classify_politics


def test_classify_is_deterministic():
    """The verdict is cached forever, so a coin flip would be frozen for good."""
    c = _FakeClient("no")
    _REAL_CLASSIFY(c, "m", "t", "d")
    assert c.messages.kwargs["temperature"] == 0


def test_classify_accepts_yes_and_no():
    assert _REAL_CLASSIFY(_FakeClient("yes"), "m", "t", "d") is True
    assert _REAL_CLASSIFY(_FakeClient("No."), "m", "t", "d") is False


def test_classify_rejects_anything_else():
    """An empty or garbled reply used to become a silent, permanent 'keep'."""
    import pytest
    for bad in ("", "maybe", "I think yes"):
        with pytest.raises(ValueError):
            _REAL_CLASSIFY(_FakeClient(bad), "m", "t", "d")


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
