"""Playlist and segment checks that keep audio lined up with video.

The parallel downloader concatenates segments into one file and lets FFmpeg
remux it afterwards, so anything that shifts the timeline inside that file
(a spliced-in ad break, a segment that arrived short) reaches the finished
episode as drifting audio. These tests pin the two guards that stop it.
"""

import pytest

from aniworld.models.common.common import _video_output_kwargs
from aniworld.models.common.hls import (
    HLSUnsupported,
    _fetch_bytes,
    _parse_media_playlist,
    expected_body_length,
    playlist_has_discontinuity,
)

BASE_URL = "https://cdn.example/hls/index.m3u8"

PLAIN_PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.000,
seg0.ts
#EXTINF:4.000,
seg1.ts
#EXT-X-ENDLIST
"""

SPLICED_PLAYLIST = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.000,
seg0.ts
#EXT-X-DISCONTINUITY
#EXTINF:4.000,
seg1.ts
#EXT-X-ENDLIST
"""


# ---------------------------------------------------------------------------
# Spliced timelines
# ---------------------------------------------------------------------------
def test_a_plain_playlist_carries_no_discontinuity():
    assert playlist_has_discontinuity(PLAIN_PLAYLIST) is False


def test_a_splice_tag_is_reported():
    assert playlist_has_discontinuity(SPLICED_PLAYLIST) is True


def test_the_discontinuity_sequence_header_is_not_a_splice():
    playlist = PLAIN_PLAYLIST.replace(
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-DISCONTINUITY-SEQUENCE:2",
    )
    assert playlist_has_discontinuity(playlist) is False


def test_a_plain_playlist_still_parses():
    segments, init_uri = _parse_media_playlist(PLAIN_PLAYLIST, BASE_URL)
    assert [uri for uri, _key, _seq in segments] == [
        "https://cdn.example/hls/seg0.ts",
        "https://cdn.example/hls/seg1.ts",
    ]
    assert init_uri is None


def test_a_spliced_playlist_goes_back_to_ffmpeg():
    with pytest.raises(HLSUnsupported):
        _parse_media_playlist(SPLICED_PLAYLIST, BASE_URL)


# ---------------------------------------------------------------------------
# Short segments
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, content, headers):
        self.content = content
        self.headers = headers

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = 0

    def get(self, url, headers=None, timeout=None):
        self.calls += 1
        return self._response


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"Content-Length": "1024"}, 1024),
        ({}, None),
        ({"Content-Length": "not a number"}, None),
        ({"Content-Length": "1024", "Content-Encoding": "gzip"}, None),
        ({"Content-Length": "1024", "Content-Encoding": "identity"}, 1024),
    ],
)
def test_only_a_comparable_content_length_counts(headers, expected):
    assert expected_body_length(_FakeResponse(b"", headers)) == expected


def test_a_short_segment_is_rejected_instead_of_concatenated(monkeypatch):
    response = _FakeResponse(b"12345", {"Content-Length": "4096"})
    session = _FakeSession(response)
    monkeypatch.setattr("aniworld.models.common.hls._session", lambda: session)
    monkeypatch.setattr("aniworld.models.common.hls.time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="truncated segment"):
        _fetch_bytes("https://cdn.example/hls/seg0.ts", {})

    assert session.calls == 3


def test_a_complete_segment_is_accepted(monkeypatch):
    response = _FakeResponse(b"12345", {"Content-Length": "5"})
    session = _FakeSession(response)
    monkeypatch.setattr("aniworld.models.common.hls._session", lambda: session)

    assert _fetch_bytes("https://cdn.example/hls/seg0.ts", {}) == b"12345"
    assert session.calls == 1


# ---------------------------------------------------------------------------
# Frame timing
# ---------------------------------------------------------------------------
def test_a_stream_copy_needs_no_timing_flag():
    assert _video_output_kwargs("copy") == {"vcodec": "copy"}


def test_a_re_encode_passes_the_source_timestamps_through():
    kwargs = _video_output_kwargs("libx264")
    assert kwargs["vcodec"] == "libx264"
    timing = {key: value for key, value in kwargs.items() if key != "vcodec"}
    assert timing in ({"fps_mode": "passthrough"}, {"vsync": "passthrough"})
