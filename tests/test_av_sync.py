"""The guards that keep audio lined up with the picture.

Four things move a track against the other: a playlist that splices in its own
timeline, a segment that arrived short, a re-encode that retimes the video, and
a separate audio rendition whose offset is lost when FFmpeg rebases each input
to zero. Each one reaches the finished episode as audio that is off, so each
one is pinned here.
"""

import ffmpeg
import pytest

from aniworld.models.common import common
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


# ---------------------------------------------------------------------------
# Rendition offsets
# ---------------------------------------------------------------------------
def _probe(start_times):
    """An ffmpeg.probe stand-in keyed by path, holding {codec_type: start}."""

    def probe(path):
        streams = [
            {"codec_type": codec_type, "start_time": str(start)}
            for codec_type, start in start_times[str(path)].items()
        ]
        return {"streams": streams, "format": {}}

    return probe


def test_the_packagers_offset_between_renditions_survives(monkeypatch):
    monkeypatch.setattr(
        common.ffmpeg,
        "probe",
        _probe({"video.ts": {"video": 10.0}, "audio.ts": {"audio": 10.2}}),
    )
    assert common._rendition_audio_offset("video.ts", "audio.ts") == pytest.approx(0.2)


def test_renditions_already_on_one_clock_are_left_alone(monkeypatch):
    monkeypatch.setattr(
        common.ffmpeg,
        "probe",
        _probe({"video.ts": {"video": 10.0}, "audio.ts": {"audio": 10.0}}),
    )
    assert common._rendition_audio_offset("video.ts", "audio.ts") == pytest.approx(0.0)


def test_unrelated_clocks_are_not_treated_as_an_offset(monkeypatch):
    monkeypatch.setattr(
        common.ffmpeg,
        "probe",
        _probe({"video.ts": {"video": 10.0}, "audio.mp4": {"audio": 0.0}}),
    )
    assert common._rendition_audio_offset("video.ts", "audio.mp4") == pytest.approx(0.0)


def test_a_missing_ffprobe_leaves_the_timestamps_alone(monkeypatch):
    def explode(_path):
        raise OSError("ffprobe not found")

    monkeypatch.setattr(common.ffmpeg, "probe", explode)
    assert common._rendition_audio_offset("video.ts", "audio.ts") == pytest.approx(0.0)


def test_a_muxed_stream_has_no_rendition_to_offset(monkeypatch):
    monkeypatch.setenv("ANIWORLD_AUDIO_OFFSET", "0.5")
    assert common._rendition_audio_offset("video.ts", None) == pytest.approx(0.5)


def test_the_manual_offset_adds_to_the_measured_one(monkeypatch):
    monkeypatch.setenv("ANIWORLD_AUDIO_OFFSET", "-0.1")
    monkeypatch.setattr(
        common.ffmpeg,
        "probe",
        _probe({"video.ts": {"video": 10.0}, "audio.ts": {"audio": 10.2}}),
    )
    assert common._rendition_audio_offset("video.ts", "audio.ts") == pytest.approx(0.1)


@pytest.mark.parametrize("raw", ["", "half a second", "0,5"])
def test_an_unreadable_manual_offset_is_ignored(monkeypatch, raw):
    monkeypatch.setenv("ANIWORLD_AUDIO_OFFSET", raw)
    assert common.get_audio_offset() == pytest.approx(0.0)


def test_an_offset_worth_applying_reaches_ffmpeg():
    args = ffmpeg.compile(
        common._audio_rendition_input("audio.ts", 0.2).output("out.mkv")
    )
    assert "-itsoffset" in args
    assert args[args.index("-itsoffset") + 1] == "0.2"


def test_no_offset_leaves_the_command_untouched():
    args = ffmpeg.compile(
        common._audio_rendition_input("audio.ts", 0.0).output("out.mkv")
    )
    assert "-itsoffset" not in args
