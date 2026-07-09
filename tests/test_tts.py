"""StubTts: deterministic, text-keyed audio so tx_log is assertable."""

from radio_server.audio import CANONICAL_FORMAT, AudioFrame
from radio_server.services import StubTts, TtsEngine


def test_render_is_deterministic_for_equal_text():
    tts = StubTts()
    assert tts.render("The time is 14:26 UTC") == tts.render("The time is 14:26 UTC")


def test_render_embeds_the_text():
    assert StubTts().render("hello") == AudioFrame(b"<audio:hello>")


def test_render_returns_a_canonical_format_frame():
    assert StubTts().render("hi").format == CANONICAL_FORMAT


def test_different_text_renders_differently():
    tts = StubTts()
    assert tts.render("one") != tts.render("two")


def test_stub_satisfies_the_engine_protocol():
    assert isinstance(StubTts(), TtsEngine)
