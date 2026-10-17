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
    """Feed fetch -> fixture; classifier -> desk 'politics' when it is in the title."""
    af.fetch = lambda url: FIXTURE
    af.make_client = lambda api_key: "DUMMY_CLIENT"
    af.classify = lambda client, model, title, desc, keep: (
        ("politics" if "politics" in title.lower() else "technology"), 1)


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
    out = af.adjust_head(head, "Axios (Tech)", "https://x.github.io/axios/feed.xml")
    assert "lastBuildDate" not in out
    assert "<title>Axios (Tech)</title>" in out
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
    assert v["https://www.axios.com/2026/07/01/sample-politics"]["desk"] == "politics"
    assert v["https://www.axios.com/2026/07/01/sample-tech"]["desk"] == "technology"
    assert v["https://www.axios.com/2026/07/01/sample-tech"]["v"] == af.PROMPT_VERSION


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
# Classifier contract (v5, 17.10.2026: desk whitelist on Haiku 5.5)
# --------------------------------------------------------------------------- #
import json as _json


class _Block:
    def __init__(self, text, type_="text"): self.text, self.type = text, type_


class _FakeMessages:
    """Replies are consumed in order; the last one repeats. A reply is either a
    (desk, borderline) tuple, a raw string, or ('STOP', stop_reason)."""
    def __init__(self, *replies):
        self.replies, self.calls, self.kwargs = list(replies), 0, None
    def create(self, **kwargs):
        self.kwargs = kwargs
        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        stop, blocks = "end_turn", [_Block("", "thinking")]   # thinking block first, as on 5.5
        if isinstance(reply, tuple) and reply[0] == "STOP":
            stop = reply[1]
        elif isinstance(reply, tuple):
            blocks.append(_Block(_json.dumps({"desk": reply[0], "borderline": reply[1]})))
        else:
            blocks.append(_Block(reply))
        return type("M", (), {"content": blocks, "stop_reason": stop})()


class _FakeClient:
    def __init__(self, *replies): self.messages = _FakeMessages(*replies)


# Captured at import time: other tests swap af.classify for a stub.
_REAL_CLASSIFY = af.classify
_REAL_HIRES = af._hires
KEEP = set(af.DEFAULT_KEEP.split(","))


def test_keep_desk_is_final_after_one_answer():
    c = _FakeClient(("ai", False))
    assert _REAL_CLASSIFY(c, "m", "t", "d", KEEP) == ("ai", 1)


def test_clear_drop_is_final_after_one_answer():
    c = _FakeClient(("politics", False))
    assert _REAL_CLASSIFY(c, "m", "t", "d", KEEP) == ("politics", 1)


def test_borderline_drop_is_asked_again_and_keep_wins():
    c = _FakeClient(("politics", True), ("economy", False))
    assert _REAL_CLASSIFY(c, "m", "t", "d", KEEP) == ("economy", 2)


def test_borderline_drop_confirmed_stays_dropped():
    c = _FakeClient(("society", True), ("society", True))
    assert _REAL_CLASSIFY(c, "m", "t", "d", KEEP) == ("society", 2)


def test_two_refusals_become_refused():
    c = _FakeClient(("STOP", "refusal"))
    assert _REAL_CLASSIFY(c, "m", "t", "d", KEEP) == ("refused", 2)


def test_one_refusal_then_answer():
    c = _FakeClient(("STOP", "refusal"), ("business", False))
    assert _REAL_CLASSIFY(c, "m", "t", "d", KEEP) == ("business", 2)


def test_max_tokens_stop_raises_and_is_not_cached():
    import pytest
    with pytest.raises(ValueError):
        _REAL_CLASSIFY(_FakeClient(("STOP", "max_tokens")), "m", "t", "d", KEEP)


def test_classify_rejects_anything_else():
    """An empty, garbled or off-list reply must never become a cached verdict."""
    import pytest
    for bad in ("", "maybe", '{"desk": "sports", "borderline": false}', '{"desk": "ai"}'):
        with pytest.raises(ValueError):
            _REAL_CLASSIFY(_FakeClient(bad), "m", "t", "d", KEEP)


def test_request_shape():
    """Structured output with the full desk enum, low effort, room for thinking,
    the item as the last user turn, and no sampling parameters (400 / TypeError)."""
    c = _FakeClient(("ai", False))
    _REAL_CLASSIFY(c, "claude-haiku-5-5", "Headline", "Sum", KEEP)
    kw = c.messages.kwargs
    assert kw["model"] == "claude-haiku-5-5"
    assert not {"temperature", "top_p", "top_k"} & set(kw)
    assert kw["max_tokens"] >= 1024
    assert kw["output_config"]["effort"] == "low"
    schema = kw["output_config"]["format"]["schema"]
    assert schema["properties"]["desk"]["enum"] == list(af.DESKS)
    assert kw["messages"][-1]["role"] == "user" and "<item>" in kw["messages"][-1]["content"]


def test_sdk_accepts_output_config():
    """The pinned SDK must know output_config, otherwise every call raises
    TypeError and the run aborts fail-closed forever."""
    import inspect
    from anthropic.resources.messages import Messages
    params = inspect.signature(Messages.create).parameters
    assert "output_config" in params and "temperature" not in params


def test_keep_set_is_validated():
    import pytest
    for bad in ("", "ai,busines", " , "):
        with pytest.raises(SystemExit):
            af.parse_keep(bad)
    assert af.parse_keep("ai, business") == {"ai", "business"}


def _run_with(classify_fn, extra=()):
    import tempfile, os
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    _install_fakes()
    af.classify = classify_fn
    af.main(["--feed-url", "x", "--api-key", "test", "--out", out, "--state", state, *extra])
    return out, state


def test_partial_failure_aborts():
    """One of two items failing is above the 30 % threshold: nothing is written."""
    import os, pytest
    def flaky(client, model, title, desc, keep):
        if "politics" in title.lower():
            raise ValueError("boom")
        return "technology", 1
    with pytest.raises(SystemExit):
        out, _ = _run_with(flaky)
    # a lenient threshold publishes, and the failed item is left out, not kept
    out, state = _run_with(flaky, ["--max-fail-ratio", "0.9"])
    result = open(out).read()
    assert "sample-tech" in result and "sample-politics" not in result
    assert "sample-politics" not in open(state).read()     # not cached -> retried


def test_empty_result_aborts():
    import pytest
    with pytest.raises(SystemExit):
        _run_with(lambda c, m, t, d, k: ("politics", 1))


def test_v4_verdicts_are_reclassified():
    """A v4 'pol: false' says nothing about the whitelist."""
    import tempfile, os, json
    d = tempfile.mkdtemp()
    out, state = os.path.join(d, "feed.xml"), os.path.join(d, "state.json")
    Path(state).write_text(json.dumps({"verdict": {
        "https://www.axios.com/2026/07/01/sample-politics": {"pol": False, "v": "v4", "seen": "2099-01-01"}}}))
    _install_fakes()
    af.main(["--feed-url", "x", "--api-key", "test", "--out", out, "--state", state])
    assert "sample-politics" not in Path(out).read_text()
    rec = json.loads(Path(state).read_text())["verdict"]["https://www.axios.com/2026/07/01/sample-politics"]
    assert rec["desk"] == "politics" and rec["v"] == af.PROMPT_VERSION


def test_model_change_invalidates_cache():
    rec = {"desk": "ai", "v": af.PROMPT_VERSION, "m": "model-a", "seen": "2099-01-01"}
    assert af._cached_desk(rec, "model-a") == "ai"
    assert af._cached_desk(rec, "model-b") is None


def test_keep_set_change_needs_no_reclassification():
    """keep/drop is derived from the cached desk on every run."""
    out, state = _run_with(lambda c, m, t, d, k: ("politics" if "politics" in t.lower() else "technology", 1))
    calls = []
    def counting(c, m, t, d, k):
        calls.append(t); return "technology", 1
    af.classify = counting
    af.main(["--feed-url", "x", "--api-key", "test", "--out", out, "--state", state,
             "--keep-desks", "politics,technology"])
    assert calls == [] and "sample-politics" in Path(out).read_text()


def test_hires_decision_is_remembered():
    """The HEAD check runs once per chart URL; later runs reuse the answer, so the
    published bytes cannot flip between fallback.png and full.png."""
    url = "https://datawrapper.dwcdn.net/abc/fallback.png"
    heads = []
    class _R:
        status_code = 200
    def head(u, **kw):
        heads.append(u); return _R()
    old = af.requests
    af.requests = type("Req", (), {"head": staticmethod(head)})
    try:
        af._HIRES_CACHE.clear()
        assert _REAL_HIRES(url).endswith("/full.png")
        assert _REAL_HIRES(url).endswith("/full.png")
        assert len(heads) == 1
    finally:
        af.requests = old


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
