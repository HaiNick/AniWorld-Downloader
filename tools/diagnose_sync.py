"""Collect everything needed to explain an out-of-sync download.

Run it on a finished episode, on the master playlist the episode came from, or
on both. It prints what FFprobe and the playlists say about how audio and video
line up, and names the signature it matches. Nothing here writes or downloads
anything.

    python tools/diagnose_sync.py "Season 1/Show S01E03.mkv"
    python tools/diagnose_sync.py https://host.example/master.m3u8
    python tools/diagnose_sync.py good.mkv bad.mkv

Get the playlist URL out of a download by running it with ANIWORLD_DEBUG_MODE=1
and looking for the m3u8 in the log.
"""

import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)

# Below this, a start-time difference is encoder delay rather than a fault.
NEGLIGIBLE = 0.005


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def fetch_text(target):
    """Read a playlist from a URL or from a file saved off disk."""
    if not str(target).lower().startswith(("http://", "https://")):
        return Path(target).read_text(encoding="utf-8", errors="replace")

    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    try:
        import niquests

        response = niquests.get(target, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text
    except ImportError:
        import urllib.request

        request = urllib.request.Request(target, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Media files
# ---------------------------------------------------------------------------
def probe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_format",
            "-show_streams",
            "-print_format",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def number(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def ratio(raw):
    """Turn FFprobe's `24000/1001` into 23.976."""
    try:
        top, _, bottom = str(raw).partition("/")
        return float(top) / float(bottom or 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def show(label, value, unit="s"):
    if value is None:
        return f"{label} ?"
    return f"{label} {value:.3f}{unit}"


def report_file(path):
    print(f"\nFILE  {path}")
    try:
        data = probe(path)
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"  could not probe it: {error}")
        return

    container = data.get("format") or {}
    print(
        f"  container   {container.get('format_name', '?')}   "
        f"{show('start', number(container.get('start_time')))}   "
        f"{show('duration', number(container.get('duration')))}"
    )

    first = {}
    for stream in data.get("streams", []):
        kind = stream.get("codec_type", "?")
        tags = stream.get("tags") or {}
        start = number(stream.get("start_time"))
        duration = number(stream.get("duration")) or number(
            tags.get("DURATION-eng") or tags.get("DURATION")
        )
        line = (
            f"  {kind[:1]}:{stream.get('index', '?')} "
            f"{stream.get('codec_name', '?'):<10} "
            f"{tags.get('language', '---'):<4} "
            f"{show('start', start)}  {show('duration', duration)}"
        )
        if kind == "video":
            average = ratio(stream.get("avg_frame_rate"))
            base = ratio(stream.get("r_frame_rate"))
            line += f"  fps {average:.3f}" if average else "  fps ?"
            if average and base and abs(average - base) > 0.01:
                line += f" against a {base:.3f} base, so VARIABLE"
        if kind == "audio":
            line += f"  {stream.get('sample_rate', '?')} Hz"
        print(line)
        first.setdefault(kind, (start, duration))

    video_start, video_duration = first.get("video", (None, None))
    audio_start, audio_duration = first.get("audio", (None, None))

    if video_start is not None and audio_start is not None:
        delta = audio_start - video_start
        if abs(delta) < NEGLIGIBLE:
            print("  -> the tracks start together")
        else:
            direction = "after" if delta > 0 else "before"
            print(
                f"  -> the audio starts {abs(delta) * 1000:.0f} ms {direction} the video"
            )

    if video_duration is not None and audio_duration is not None:
        delta = audio_duration - video_duration
        if abs(delta) > 0.5:
            direction = "longer" if delta > 0 else "shorter"
            print(
                f"  -> the audio runs {abs(delta):.3f}s {direction} than the video, "
                "so the two are not the same cut"
            )


# ---------------------------------------------------------------------------
# Playlists
# ---------------------------------------------------------------------------
def read_media_playlist(text):
    """Segment count, playing time and container of one media playlist."""
    segments = 0
    seconds = 0.0
    splices = 0
    fragmented = False
    encrypted = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            seconds += number(line.split(":", 1)[1].split(",", 1)[0]) or 0.0
        elif line.startswith("#EXT-X-MAP:"):
            fragmented = True
        elif line.startswith("#EXT-X-KEY:"):
            encrypted = line.split("METHOD=", 1)[-1].split(",", 1)[0]
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith(
            "#EXT-X-DISCONTINUITY-SEQUENCE"
        ):
            splices += 1
        elif not line.startswith("#"):
            segments += 1

    return {
        "segments": segments,
        "seconds": seconds,
        "splices": splices,
        "container": "fmp4" if fragmented else "ts",
        "encryption": encrypted,
    }


def attributes(line):
    import re

    return {
        key: value.strip('"')
        for key, value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line)
    }


def read_master_playlist(text, base_url):
    variants = []
    renditions = []
    lines = [line.strip() for line in text.splitlines()]

    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:") and attributes(line).get("TYPE") == "AUDIO":
            attrs = attributes(line)
            uri = attrs.get("URI")
            renditions.append(
                {
                    "uri": urljoin(base_url, uri) if uri else None,
                    "group": attrs.get("GROUP-ID", ""),
                    "language": attrs.get("LANGUAGE", ""),
                    "name": attrs.get("NAME", ""),
                    "default": attrs.get("DEFAULT", "").upper() == "YES",
                }
            )
        elif line.startswith("#EXT-X-STREAM-INF:"):
            attrs = attributes(line)
            uri = next(
                (
                    item
                    for item in lines[index + 1 :]
                    if item and not item.startswith("#")
                ),
                None,
            )
            if uri:
                variants.append(
                    {
                        "uri": urljoin(base_url, uri),
                        "bandwidth": int(number(attrs.get("BANDWIDTH", 0)) or 0),
                        "group": attrs.get("AUDIO", ""),
                        "resolution": attrs.get("RESOLUTION", "?"),
                    }
                )

    return variants, renditions


def describe(label, url, summary):
    detail = (
        f"  {label:<6} {summary['segments']} segments, "
        f"{summary['seconds']:.3f}s, {summary['container']}"
    )
    if summary["splices"]:
        splices = summary["splices"]
        detail += f", {splices} SPLICE{'S' if splices > 1 else ''}"
    if summary["encryption"]:
        detail += f", encrypted {summary['encryption']}"
    print(detail)
    print(f"         {url}")


def report_playlist(target):
    print(f"\nPLAYLIST  {target}")
    try:
        text = fetch_text(target)
    except Exception as error:  # noqa: BLE001 - whatever went wrong, say so and move on
        print(f"  could not fetch it: {error}")
        return

    if "#EXT-X-STREAM-INF" not in text:
        describe("media", target, read_media_playlist(text))
        return

    variants, renditions = read_master_playlist(text, target)
    if not variants:
        print("  master playlist with no variants")
        return

    print(f"  {len(variants)} variants, {len(renditions)} audio renditions")
    for variant in sorted(variants, key=lambda item: item["bandwidth"], reverse=True):
        group = variant["group"] or "(audio muxed in)"
        print(f"    {variant['bandwidth']:>9} bps  {variant['resolution']:<10} {group}")
    for rendition in renditions:
        flag = " default" if rendition["default"] else ""
        print(
            f"    audio  group {rendition['group']!r}  lang {rendition['language']!r}  "
            f"name {rendition['name']!r}{flag}"
        )

    chosen = max(variants, key=lambda item: item["bandwidth"])
    print("\n  what the downloader picks:")
    try:
        video = read_media_playlist(fetch_text(chosen["uri"]))
    except Exception as error:  # noqa: BLE001
        print(f"  could not fetch the video playlist: {error}")
        return
    describe("video", chosen["uri"], video)

    partners = [
        rendition
        for rendition in renditions
        if rendition["uri"] and rendition["group"] == chosen["group"]
    ]
    if not chosen["group"] or not partners:
        print("  the video variant carries its own audio, nothing is muxed in")
        return

    for rendition in partners:
        try:
            audio = read_media_playlist(fetch_text(rendition["uri"]))
        except Exception as error:  # noqa: BLE001
            print(f"  could not fetch the {rendition['language']} audio: {error}")
            continue
        describe(f"a:{rendition['language'] or '?'}", rendition["uri"], audio)
        gap = audio["seconds"] - video["seconds"]
        if abs(gap) > 0.5:
            print(
                f"  -> this audio rendition is {abs(gap):.3f}s "
                f"{'longer' if gap > 0 else 'shorter'} than the video, "
                "so it is a different cut and no timestamp fix can align it"
            )
        elif audio["segments"] != video["segments"]:
            print(
                f"  -> same length but {audio['segments']} audio segments against "
                f"{video['segments']} video ones"
            )
        else:
            print(
                "  -> this audio rendition matches the video length segment for segment"
            )


# ---------------------------------------------------------------------------
def main(argv):
    if not argv:
        print(__doc__)
        return 2

    for target in argv:
        lowered = str(target).lower()
        if lowered.startswith(("http://", "https://")) or lowered.endswith(".m3u8"):
            report_playlist(target)
        else:
            report_file(target)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
