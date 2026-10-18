"""AutoSync.

Only series you ticked "Sync this" for are synced. Each one remembers the
language and download folder to sync into, the episode to start from, and when
it is checked:

  * "global" follows the schedule from the settings, an interval (every 24
    hours by default) or fixed times in cron form. See schedule.py.
  * "daily" and "weekly" are presets of its own, "every day at 22:00" or
    "every monday at 22:00".

A check reads the series page itself, so it works for every site that has
series and does not depend on a feed that has scrolled on by the time a weekly
series is due. Every episode from the start point on that is out in the
series' language and not on disk yet is queued.
"""

import json
import re
import threading
import time
from datetime import UTC, datetime, timedelta

from ..logger import get_logger
from ..providers import resolve_provider
from . import db, paths, schedule
from .media import folder_matches_title
from .settings_store import (
    autosync_cron_schedule,
    autosync_enabled,
    autosync_interval_seconds,
    autosync_mode,
    autosync_schedule_description,
    ui_language,
)

logger = get_logger(__name__)

# How long the thread sleeps at most. A run that is due sooner shortens the nap
# so a fixed time is hit on the minute, and waking up regardless keeps a
# schedule changed in the settings from needing a restart.
TICK_SECONDS = 300
MIN_TICK_SECONDS = 5

# How far ahead of now a stored "last run" may be before it is treated as junk
_CLOCK_SLACK = timedelta(minutes=5)

CADENCES = ("global", "daily", "weekly")

# Monday first, the way the UI lists them. Cron counts from Sunday.
WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_run_lock = threading.Lock()
_started = False
_start_lock = threading.Lock()

# Set the first time the schedule is looked at, see _anchor()
_anchored_at = None


def _now():
    return datetime.now(UTC)


def _local(moment):
    """A UTC instant as naive local wall-clock time, which is what cron means."""
    return moment.astimezone().replace(tzinfo=None)


def _utc(wall_clock):
    """Naive local wall-clock time back to UTC."""
    return wall_clock.astimezone(UTC)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is None:
        # Hand-edited, or written by a version that stored it without one.
        # sqlite's datetime('now') is UTC as well, and comparing the two kinds
        # raises.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _sane(moment):
    """A stored timestamp, unless it sits in the future.

    Written while the clock was wrong, a dead CMOS battery say. Counting from it
    would park the next run in that future and never run again, so ignore it:
    the next cycle writes a sane one and it heals itself.
    """
    if moment is not None and moment - _now() > _CLOCK_SLACK:
        logger.warning("AutoSync: %s is in the future, ignoring it", moment)
        return None
    return moment


# ---------------------------------------------------------------------------
# Per-series settings
# ---------------------------------------------------------------------------
class SettingsError(ValueError):
    """A per-series setting that cannot be used. The message is shown as is."""


def clean_settings(data):
    """Validate the per-series fields from a request, keeping only known ones."""
    cleaned = {}

    if "language" in data:
        language = str(data["language"] or "").strip()
        if language not in paths.LANG_FOLDERS:
            raise SettingsError("Pick the language to sync in.")
        cleaned["language"] = language

    if "title" in data:
        cleaned["title"] = str(data["title"] or "").strip()

    if "custom_path_id" in data:
        raw = data["custom_path_id"]
        if raw in (None, "", 0, "0"):
            cleaned["custom_path_id"] = None
        else:
            try:
                path_id = int(raw)
            except (TypeError, ValueError):
                raise SettingsError("Unknown download folder.") from None
            if not db.get_custom_path(path_id):
                raise SettingsError("Unknown download folder.")
            cleaned["custom_path_id"] = path_id

    for field, lowest in (("start_season", 0), ("start_episode", 1)):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                raise SettingsError(
                    "The start point needs a season and an episode number."
                ) from None
            if value < lowest:
                raise SettingsError(
                    "The start season cannot be below 0."
                    if field == "start_season"
                    else "The start episode cannot be below 1."
                )
            cleaned[field] = value

    if "cadence" in data:
        cadence = str(data["cadence"] or "").strip().lower()
        if cadence not in CADENCES:
            raise SettingsError("Unknown schedule, use global, daily or weekly.")
        cleaned["cadence"] = cadence

    if "weekday" in data:
        try:
            weekday = int(data["weekday"])
        except (TypeError, ValueError):
            weekday = -1
        if not 0 <= weekday <= 6:
            raise SettingsError("Pick a day of the week.")
        cleaned["weekday"] = weekday

    if "run_time" in data:
        match = _TIME_RE.match(str(data["run_time"] or "").strip())
        if not match:
            raise SettingsError("The time has to look like 22:00.")
        cleaned["run_time"] = f"{int(match.group(1)):02d}:{match.group(2)}"

    return cleaned


def series_schedule(row):
    """The fixed times of a series with a preset, or None when it follows the global one."""
    if row.get("cadence") not in ("daily", "weekly"):
        return None
    hour, minute = (int(part) for part in row.get("run_time", "22:00").split(":"))
    if row["cadence"] == "daily":
        return schedule.parse(f"{minute} {hour} * * *")
    weekday = (int(row.get("weekday") or 0) + 1) % 7
    return schedule.parse(f"{minute} {hour} * * {weekday}")


def series_schedule_description(row, language=None):
    fixed = series_schedule(row)
    if fixed is None:
        return autosync_schedule_description(language)
    return fixed.describe(language or ui_language())


def series_next_run(row):
    """When this series is checked next, in UTC, or None if never."""
    fixed = series_schedule(row)
    if fixed is None:
        return next_run_at()
    # Counted from when it was ticked, so ticking "every day at 22:00" at 23:00
    # waits for tomorrow instead of checking on the spot.
    since = _sane(_parse(row.get("last_checked"))) or _parse(row.get("created_at"))
    upcoming = fixed.next_run(_local(since or _now()))
    return _utc(upcoming) if upcoming else None


def _series_due(row):
    if row.get("cadence") not in ("daily", "weekly"):
        return False
    upcoming = series_next_run(row)
    return upcoming is not None and _now() >= upcoming


# ---------------------------------------------------------------------------
# What is on disk
# ---------------------------------------------------------------------------
def _bases(row):
    """Directories the series' own folder can sit in."""
    custom_path_id = row.get("custom_path_id")
    bases = [paths.base_for(custom_path_id)]
    if not custom_path_id:
        # The downloader writes series there when it is set and nothing else is
        override = paths.series_download_path()
        if override and override not in bases:
            bases.append(override)
    if paths.lang_separation_enabled():
        bases = [base / paths.lang_folder_for(row["language"]) for base in bases]
    return bases


def episodes_in_folder(folder):
    """(season, episode) numbers sitting in this one folder.

    media.downloaded_episodes() unions every root and language folder, which is
    right for the "already downloaded" badge but wrong here: a German copy would
    look complete because the English copy next to it has the episode, and would
    then never be filled in.
    """
    from .media import EPISODE_RE

    found = set()
    try:
        files = folder.rglob("*")
    except OSError:
        return found

    for file in files:
        try:
            if not file.is_file():
                continue
        except OSError:
            continue
        match = EPISODE_RE.search(file.name)
        if match:
            found.add((int(match.group(1)), int(match.group(2))))
    return found


def episodes_on_disk(row, titles):
    """Every (season, episode) of this series already in its folder.

    Matching is done on folder names rather than by rendering the naming
    template, so it keeps working after the template changes. A series that is
    not on disk at all simply has nothing yet.
    """
    titles = [title for title in titles if title]
    found = set()
    for base in _bases(row):
        try:
            children = list(base.iterdir()) if base.is_dir() else []
        except OSError:
            continue
        for folder in children:
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            if any(folder_matches_title(folder.name, title) for title in titles):
                found |= episodes_in_folder(folder)
    return found


def _where(row):
    """Human label for the folder a series syncs into."""
    custom_path_id = row.get("custom_path_id")
    entry = db.get_custom_path(custom_path_id) if custom_path_id else None
    parts = [entry["name"] if entry else "Default"]
    if paths.lang_separation_enabled():
        parts.append(paths.lang_folder_for(row["language"]))
    return " / ".join(parts)


# ---------------------------------------------------------------------------
# What to queue
# ---------------------------------------------------------------------------
_EPISODE_NUMBERS = re.compile(r"/staffel-(\d+)/episode-(\d+)")


def _numbers(episode_url):
    """(season, episode) read straight off the URL, or None if it has neither."""
    match = _EPISODE_NUMBERS.search(episode_url)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _season_number(season):
    try:
        return int(season.season_number)
    except (TypeError, ValueError):
        return None


def wanted_episodes(series, have, start, language):
    """Episode URLs from the start point on that are out in the language and missing.

    Returns (wanted, not_out_yet). Seasons before the start are never opened,
    so a long series costs one request per season that is still relevant.
    """
    wanted = []
    not_out_yet = 0
    for season in series.seasons:
        season_number = _season_number(season)
        if season_number is None or season_number < start[0]:
            continue

        # Sites that read the flags off the season page, others queue blind
        flags = getattr(season, "episode_languages", None) or {}

        for episode in season.episodes:
            number = episode.episode_number
            if number is None:
                numbers = _numbers(episode.url)
                number = numbers[1] if numbers else None
            if number is None:
                continue
            if (season_number, number) < start:
                continue
            if (season_number, number) in have:
                continue
            if flags and language not in flags.get(number, ()):
                not_out_yet += 1
                continue
            wanted.append(episode.url)
    return wanted, not_out_yet


def _default_provider():
    from ..config import get_provider_fallback_order
    from .media import WORKING_PROVIDERS

    order = list(get_provider_fallback_order(WORKING_PROVIDERS))
    return order[0] if order else "VOE"


def sync_series(row, provider_name):
    """Check one series and queue what it is missing. Returns a report row.

    Reasons are shown verbatim on the Auto-Sync page, so they read as sentences.
    """
    series_url = row["series_url"]
    language = row["language"]
    report = {
        "id": row.get("id"),
        "title": row.get("title") or series_url,
        "series_url": series_url,
        "language": language,
        "where": _where(row),
    }

    if db.is_copy_queued_or_running(series_url, language, row.get("custom_path_id")):
        return {
            **report,
            "status": "skipped",
            "reason": "This series is already in the queue.",
        }

    series = resolve_provider(series_url).series_cls(url=series_url)
    title = getattr(series, "title", None) or row.get("title")
    report["title"] = title or report["title"]

    have = episodes_on_disk(row, [title, row.get("title")])
    start = (int(row.get("start_season") or 0), int(row.get("start_episode") or 1))
    wanted, not_out_yet = wanted_episodes(series, have, start, language)

    if not wanted:
        result = {**report, "status": "up-to-date"}
        if not_out_yet:
            result["reason"] = (
                f"{not_out_yet} more episode{'s are' if not_out_yet != 1 else ' is'} "
                f"not out in {language} yet."
            )
        return result

    queue_id = db.add_to_queue(
        title=report["title"],
        series_url=series_url,
        episodes=wanted,
        language=language,
        provider=provider_name,
        custom_path_id=row.get("custom_path_id"),
        source="autosync",
    )
    return {
        **report,
        "status": "queued",
        "episodes": len(wanted),
        "queue_id": queue_id,
    }


# ---------------------------------------------------------------------------
# Running a cycle
# ---------------------------------------------------------------------------
def run_cycle(rows=None, mark_global=True):
    """One pass over some tracked series, all of them when rows is None.

    mark_global records the pass as the run of the global schedule, which is
    what "Sync now" and a due global schedule are. A preset series coming due
    on its own leaves the global clock alone.

    Returns a report dict, also stored for the Auto-Sync page.
    """
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("A sync is already running")

    started = _now()
    try:
        if rows is None:
            rows = db.get_autosync_series()
        provider_name = _default_provider()

        results = []
        for row in rows:
            try:
                result = sync_series(row, provider_name)
            except Exception as exc:
                logger.error("AutoSync failed for %s: %s", row["series_url"], exc)
                result = {
                    "id": row.get("id"),
                    "title": row.get("title") or row["series_url"],
                    "series_url": row["series_url"],
                    "language": row.get("language"),
                    "where": _where(row),
                    "status": "error",
                    "reason": str(exc)[:200],
                }
            # A failure counts as checked too, or a broken series would be
            # retried on every tick until its next scheduled time.
            if row.get("id") is not None:
                db.record_autosync_check(row["id"], started.isoformat(), _dump(result))
            results.append(result)

        report = {
            "started_at": started.isoformat(),
            "finished_at": _now().isoformat(),
            "checked": len(rows),
            "queued": sum(1 for row in results if row["status"] == "queued"),
            "results": results,
        }
        state = {"last_report": _dump(report)}
        if mark_global:
            state["last_run"] = started.isoformat()
        db.set_autosync_state(**state)
        logger.info(
            "AutoSync finished: %d checked, %d queued",
            report["checked"],
            report["queued"],
        )

        if report["queued"]:
            from . import worker

            worker.ensure_started()
        return report
    finally:
        _run_lock.release()


def _dump(report):
    return json.dumps(report)


def _load(raw):
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def is_running():
    return _run_lock.locked()


def series_status(row):
    """One tracked series the way the API and the Auto-Sync page show it."""
    upcoming = series_next_run(row) if autosync_enabled() else None
    return {
        "id": row["id"],
        "series_url": row["series_url"],
        "title": row["title"],
        "language": row["language"],
        "custom_path_id": row["custom_path_id"],
        "where": _where(row),
        "start_season": row["start_season"],
        "start_episode": row["start_episode"],
        "cadence": row["cadence"],
        "weekday": row["weekday"],
        "run_time": row["run_time"],
        "schedule": series_schedule_description(row),
        "last_checked": row["last_checked"],
        "next_run": upcoming.isoformat() if upcoming else None,
        "last_result": _load(row["last_result"]),
    }


def status():
    """What the Auto-Sync page shows."""
    state = db.get_autosync_state()
    last_run = _parse(state.get("last_run"))

    fixed = _fixed_times()
    interval_seconds = autosync_interval_seconds()
    upcoming = next_run_at()

    return {
        "enabled": autosync_enabled(),
        "running": _run_lock.locked(),
        "mode": autosync_mode(),
        "interval": schedule.format_interval(interval_seconds),
        "interval_seconds": interval_seconds,
        # Kept for anything that read the status before the schedule was
        # settable, when it was always 24. Whole hours no longer cover it.
        "interval_hours": round(interval_seconds / 3600, 4),
        "cron": fixed.expression if fixed else None,
        "schedule": autosync_schedule_description(),
        "last_run": last_run.isoformat() if last_run else None,
        "next_run": upcoming.isoformat() if upcoming else None,
        "last_report": _load(state.get("last_report")),
        "series": [series_status(row) for row in db.get_autosync_series()],
    }


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------
def _fixed_times():
    """The parsed fixed times, or None for interval mode and a broken schedule."""
    try:
        return autosync_cron_schedule()
    except schedule.ScheduleError as exc:
        # settings_store already falls back to the default, so this is only
        # reachable if that default itself stopped parsing.
        logger.error(
            "AutoSync: unusable schedule, falling back to the interval: %s", exc
        )
        return None


def _anchor():
    """Stands in for "last run" while Auto-Sync has never run.

    Fixed times are counted from the moment Auto-Sync was switched on, so
    turning it on at 23:00 with "every day at 22:00" waits for tomorrow
    instead of queueing a pile of downloads on the spot.
    """
    global _anchored_at
    if _anchored_at is None:
        _anchored_at = _now()
    return _anchored_at


def _reset_anchor():
    """While Auto-Sync is off there is nothing to count from, so the anchor
    follows the clock and freezes at the moment it is switched on."""
    global _anchored_at
    _anchored_at = _now()


def next_run_at():
    """When the global schedule is due next, in UTC, or None if it never fires."""
    last_run = _sane(_parse(db.get_autosync_state().get("last_run")))
    fixed = _fixed_times()

    if fixed is None:
        # An interval install that has never run is due right away, which is
        # what Auto-Sync did before the schedule was configurable.
        if last_run is None:
            return _anchor()
        return last_run + timedelta(seconds=autosync_interval_seconds())

    upcoming = fixed.next_run(_local(last_run or _anchor()))
    return _utc(upcoming) if upcoming else None


def _due():
    """Whether the global schedule is due."""
    upcoming = next_run_at()
    return upcoming is not None and _now() >= upcoming


def _tick():
    """Run whatever is due: the global schedule, series with a preset, or both."""
    rows = db.get_autosync_series()
    global_due = _due()
    picked = [
        row
        for row in rows
        if (row["cadence"] == "global" and global_due) or _series_due(row)
    ]
    # A due global schedule is recorded even with nothing to check, or the
    # worker would find it due again on every tick.
    if global_due or picked:
        run_cycle(picked, mark_global=global_due)


def _next_wakeup():
    """The soonest of the global schedule and every preset series."""
    moments = [next_run_at()]
    for row in db.get_autosync_series():
        if row["cadence"] != "global":
            moments.append(series_next_run(row))
    moments = [moment for moment in moments if moment is not None]
    return min(moments) if moments else None


def _nap_seconds():
    """How long to sleep: long enough to stay cheap, short enough to be on time."""
    if not autosync_enabled():
        return TICK_SECONDS
    try:
        upcoming = _next_wakeup()
    except Exception:
        logger.exception("AutoSync: could not work out the next run")
        return TICK_SECONDS
    if upcoming is None:
        return TICK_SECONDS
    seconds = (upcoming - _now()).total_seconds()
    return max(MIN_TICK_SECONDS, min(TICK_SECONDS, seconds))


def _loop():
    while True:
        try:
            # Re-read the settings every tick so a change takes effect
            if autosync_enabled():
                _tick()
            else:
                _reset_anchor()
        except Exception:
            logger.exception("AutoSync worker error")
        time.sleep(_nap_seconds())


def ensure_started():
    """Start the scheduling thread once per process."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    _anchor()
    threading.Thread(target=_loop, name="aniworld-autosync", daemon=True).start()
