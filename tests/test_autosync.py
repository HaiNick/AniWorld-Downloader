"""AutoSync: the series you ticked, where they start, and when they are checked.

No site is ever contacted: series objects are stand-ins.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from aniworld.web import autosync, db

SERIES = "https://aniworld.to/anime/stream/naruto"
EP = SERIES + "/staffel-{}/episode-{}"


def track(series_url=SERIES, **fields):
    options = {"title": "Naruto", "language": "German Dub"}
    options.update(fields)
    return db.save_autosync_series(series_url, **options)


# ---------------------------------------------------------------------------
# Stand-ins for a series on a site
# ---------------------------------------------------------------------------
class _Episode:
    def __init__(self, season, number, url=None):
        self.url = url or EP.format(season, number)
        self.episode_number = number


class _Season:
    def __init__(self, number, count, flags=None):
        self.season_number = number
        self._episodes = [_Episode(number, n) for n in range(1, count + 1)]
        self.episode_languages = flags or {}
        self.opened = False

    @property
    def episodes(self):
        self.opened = True
        return self._episodes


class _Series:
    def __init__(self, title, seasons):
        self.title = title
        self.seasons = seasons


class _Provider:
    def __init__(self, series):
        self._series = series

    def series_cls(self, url):
        return self._series


@pytest.fixture
def site(monkeypatch):
    """Make every series URL resolve to a stand-in.

    seasons maps a season number to its episode count, flags maps a season
    number to the language badges by episode number.
    """

    def use(seasons, title="Naruto", flags=None):
        flags = flags or {}
        series = _Series(
            title,
            [
                _Season(number, count, flags.get(number))
                for number, count in seasons.items()
            ],
        )
        monkeypatch.setattr(autosync, "resolve_provider", lambda url: _Provider(series))
        return series

    return use


def _queued_urls():
    return [url for item in db.get_queue() for url in json.loads(item["episodes"])]


# ---------------------------------------------------------------------------
# The list of synced series
# ---------------------------------------------------------------------------
def test_nothing_is_synced_to_begin_with():
    assert db.get_autosync_series() == []
    assert db.get_autosync_series_by_url(SERIES) is None


def test_ticking_a_series_stores_its_settings():
    row = track(start_season=2, start_episode=5, cadence="weekly", weekday=4)
    assert row["series_url"] == SERIES
    assert row["language"] == "German Dub"
    assert (row["start_season"], row["start_episode"]) == (2, 5)
    assert (row["cadence"], row["weekday"]) == ("weekly", 4)


def test_the_defaults_start_at_the_first_episode_on_the_global_schedule():
    row = track()
    assert (row["start_season"], row["start_episode"]) == (1, 1)
    assert row["cadence"] == "global"
    assert row["run_time"] == "22:00"


def test_saving_again_updates_the_same_row():
    track(start_episode=3)
    row = track(cadence="daily")
    assert len(db.get_autosync_series()) == 1
    assert row["start_episode"] == 3, "fields left out are kept"
    assert row["cadence"] == "daily"


def test_unknown_fields_are_ignored():
    row = track(last_result="forged", series_url_2="nope")
    assert row["last_result"] is None


def test_a_series_can_be_unticked_by_url():
    track()
    db.remove_autosync_series(series_url=SERIES)
    assert db.get_autosync_series() == []


def test_a_series_can_be_unticked_by_id():
    row = track()
    db.remove_autosync_series(series_id=row["id"])
    assert db.get_autosync_series() == []


def test_unticking_a_missing_series_is_harmless():
    db.remove_autosync_series(series_url="https://nope")
    db.remove_autosync_series(series_id=4242)


def test_synced_series_are_listed_alphabetically():
    track("https://b", title="Zebra")
    track("https://a", title="apple")
    assert [row["title"] for row in db.get_autosync_series()] == ["apple", "Zebra"]


def test_a_check_is_recorded_on_the_row():
    row = track()
    db.record_autosync_check(
        row["id"], "2026-01-01T00:00:00+00:00", '{"status": "queued"}'
    )
    row = db.get_autosync_series_by_id(row["id"])
    assert row["last_checked"] == "2026-01-01T00:00:00+00:00"
    assert json.loads(row["last_result"]) == {"status": "queued"}


# ---------------------------------------------------------------------------
# Validating what the UI sends
# ---------------------------------------------------------------------------
def test_valid_settings_come_through_cleaned():
    cleaned = autosync.clean_settings(
        {
            "language": "English Sub",
            "start_season": "2",
            "start_episode": 5,
            "cadence": "Weekly",
            "weekday": "6",
            "run_time": "7:05",
            "custom_path_id": "",
            "something_else": 1,
        }
    )
    assert cleaned == {
        "language": "English Sub",
        "start_season": 2,
        "start_episode": 5,
        "cadence": "weekly",
        "weekday": 6,
        "run_time": "07:05",
        "custom_path_id": None,
    }


@pytest.mark.parametrize(
    "data",
    [
        {"language": ""},
        {"language": "Klingon Dub"},
        {"start_season": -1},
        {"start_episode": 0},
        {"start_episode": "five"},
        {"cadence": "hourly"},
        {"weekday": 7},
        {"weekday": "monday"},
        {"run_time": "25:00"},
        {"run_time": "10pm"},
        {"custom_path_id": 4242},
    ],
)
def test_bad_settings_are_refused_with_a_sentence(data):
    with pytest.raises(autosync.SettingsError) as error:
        autosync.clean_settings(data)
    message = str(error.value)
    assert message[0].isupper() and message.endswith("."), message


def test_a_known_custom_path_is_accepted(tmp_path):
    path_id = db.add_custom_path("Anime", str(tmp_path))
    assert autosync.clean_settings({"custom_path_id": str(path_id)}) == {
        "custom_path_id": path_id
    }


def test_season_zero_is_a_valid_start():
    """Season 0 holds the movies, starting there takes them along."""
    assert autosync.clean_settings({"start_season": 0}) == {"start_season": 0}


# ---------------------------------------------------------------------------
# Remembering the last run
# ---------------------------------------------------------------------------
def test_state_starts_empty():
    assert db.get_autosync_state() == {}


def test_state_round_trips():
    db.set_autosync_state(last_run="2026-01-01T00:00:00", last_report='{"queued": 1}')
    state = db.get_autosync_state()
    assert state["last_run"] == "2026-01-01T00:00:00"
    assert state["last_report"] == '{"queued": 1}'


def test_state_is_overwritten_not_appended():
    db.set_autosync_state(last_run="first")
    db.set_autosync_state(last_run="second")
    assert db.get_autosync_state()["last_run"] == "second"


def test_state_values_are_stored_as_text():
    db.set_autosync_state(count=5, missing=None)
    state = db.get_autosync_state()
    assert state["count"] == "5"
    assert state["missing"] is None


# ---------------------------------------------------------------------------
# What a check queues
# ---------------------------------------------------------------------------
def test_everything_from_the_start_point_on_is_wanted(site):
    series = site({1: 12, 2: 8})
    wanted, _ = autosync.wanted_episodes(series, set(), (2, 5), "German Dub")
    assert wanted == [EP.format(2, n) for n in (5, 6, 7, 8)]


def test_seasons_before_the_start_are_never_opened(site):
    """Each one is a request to the site, and nothing in them is wanted."""
    series = site({1: 12, 2: 12, 3: 4})
    autosync.wanted_episodes(series, set(), (3, 1), "German Dub")
    assert [season.opened for season in series.seasons] == [False, False, True]


def test_episodes_on_disk_are_not_wanted(site):
    series = site({1: 4})
    wanted, _ = autosync.wanted_episodes(series, {(1, 1), (1, 3)}, (1, 1), "German Dub")
    assert wanted == [EP.format(1, 2), EP.format(1, 4)]


def test_episodes_not_out_in_the_language_are_held_back(site):
    flags = {
        1: {1: ("German Dub", "German Sub"), 2: ("German Sub",), 3: ("German Sub",)}
    }
    series = site({1: 3}, flags=flags)
    wanted, not_out_yet = autosync.wanted_episodes(series, set(), (1, 1), "German Dub")
    assert wanted == [EP.format(1, 1)]
    assert not_out_yet == 2


def test_without_flags_every_episode_is_wanted(site):
    """A site that shows no badges on its season page cannot be filtered."""
    series = site({1: 3})
    wanted, not_out_yet = autosync.wanted_episodes(series, set(), (1, 1), "German Dub")
    assert len(wanted) == 3
    assert not_out_yet == 0


def test_movies_are_left_out_unless_the_start_is_season_zero(site):
    series = site({0: 2, 1: 2})
    from_one, _ = autosync.wanted_episodes(series, set(), (1, 1), "German Dub")
    from_zero, _ = autosync.wanted_episodes(series, set(), (0, 1), "German Dub")
    assert len(from_one) == 2
    assert len(from_zero) == 4


def test_an_episode_without_a_number_falls_back_to_its_url(site):
    series = site({1: 0})
    series.seasons[0]._episodes = [_Episode(1, None, url=EP.format(1, 7))]
    wanted, _ = autosync.wanted_episodes(series, set(), (1, 5), "German Dub")
    assert wanted == [EP.format(1, 7)]


def test_a_check_queues_the_missing_episodes(site, downloads):
    site({1: 3})
    folder = downloads / "Naruto"
    folder.mkdir()
    (folder / "Naruto S01E01.mkv").write_bytes(b"x")

    result = autosync.sync_series(track(), "VOE")
    assert result["status"] == "queued"
    assert result["episodes"] == 2
    item = db.get_queue_item(result["queue_id"])
    assert item["language"] == "German Dub"
    assert item["source"] == "autosync"
    assert _queued_urls() == [EP.format(1, 2), EP.format(1, 3)]


def test_a_series_not_on_disk_yet_gets_everything_from_the_start(site):
    site({1: 3})
    result = autosync.sync_series(track(start_episode=2), "VOE")
    assert result["episodes"] == 2


def test_a_check_lands_in_the_custom_path_it_was_ticked_with(site, tmp_path):
    other = tmp_path / "anime"
    other.mkdir()
    path_id = db.add_custom_path("Anime", str(other))
    site({1: 1})

    result = autosync.sync_series(track(custom_path_id=path_id), "VOE")
    assert db.get_queue_item(result["queue_id"])["custom_path_id"] == path_id
    assert result["where"] == "Anime"


def test_only_the_folder_of_its_own_language_counts(site, downloads, monkeypatch):
    """The English copy having an episode must not keep it from the German one."""
    monkeypatch.setenv("ANIWORLD_LANG_SEPARATION", "1")
    english = downloads / "english-dub" / "Naruto"
    english.mkdir(parents=True)
    (english / "Naruto S01E01.mkv").write_bytes(b"x")
    site({1: 1})

    result = autosync.sync_series(track(), "VOE")
    assert result["status"] == "queued"
    assert result["where"] == "Default / german-dub"


def test_the_folder_of_its_own_language_is_read(site, downloads, monkeypatch):
    monkeypatch.setenv("ANIWORLD_LANG_SEPARATION", "1")
    german = downloads / "german-dub" / "Naruto (2002)"
    german.mkdir(parents=True)
    (german / "Naruto S01E01.mkv").write_bytes(b"x")
    site({1: 1})

    assert autosync.sync_series(track(), "VOE")["status"] == "up-to-date"


def test_a_sequel_folder_does_not_count_as_the_series(site, downloads):
    shippuden = downloads / "Naruto Shippuden"
    shippuden.mkdir()
    (shippuden / "Naruto Shippuden S01E01.mkv").write_bytes(b"x")
    site({1: 1})

    assert autosync.sync_series(track(), "VOE")["status"] == "queued"


def test_nothing_missing_is_up_to_date(site, downloads):
    folder = downloads / "Naruto"
    folder.mkdir()
    (folder / "Naruto S01E01.mkv").write_bytes(b"x")
    site({1: 1})

    result = autosync.sync_series(track(), "VOE")
    assert result["status"] == "up-to-date"
    assert "reason" not in result
    assert db.get_queue() == []


def test_up_to_date_says_what_is_not_out_yet(site):
    site({1: 2}, flags={1: {1: ("German Sub",), 2: ("German Sub",)}})
    result = autosync.sync_series(track(), "VOE")
    assert result["status"] == "up-to-date"
    assert result["reason"] == "2 more episodes are not out in German Dub yet."


def test_one_episode_not_out_yet_reads_as_singular(site):
    site({1: 1}, flags={1: {1: ("German Sub",)}})
    result = autosync.sync_series(track(), "VOE")
    assert result["reason"] == "1 more episode is not out in German Dub yet."


def test_a_series_already_in_the_queue_is_skipped(site, queue_item):
    site({1: 1})
    queue_item(title="Naruto", series_url=SERIES, language="German Dub")
    result = autosync.sync_series(track(), "VOE")
    assert result["status"] == "skipped"
    assert result["reason"] == "This series is already in the queue."


def test_the_same_series_queued_in_another_language_does_not_block(site, queue_item):
    site({1: 1})
    queue_item(title="Naruto", series_url=SERIES, language="English Sub")
    assert autosync.sync_series(track(), "VOE")["status"] == "queued"


def test_the_title_from_the_site_wins(site):
    site({1: 1}, title="Naruto (Remastered)")
    result = autosync.sync_series(track(title="naruto"), "VOE")
    assert result["title"] == "Naruto (Remastered)"


def test_every_reason_reads_as_a_sentence(site, queue_item):
    """They are printed verbatim on the page, so they read as sentences."""
    site({1: 1}, flags={1: {1: ("German Sub",)}})
    reasons = [autosync.sync_series(track(), "VOE")["reason"]]
    queue_item(title="Naruto", series_url=SERIES, language="German Dub")
    reasons.append(autosync.sync_series(track(), "VOE")["reason"])
    for reason in reasons:
        assert reason[0].isupper() or reason[0].isdigit(), reason
        assert reason.endswith("."), reason


# ---------------------------------------------------------------------------
# A whole run
# ---------------------------------------------------------------------------
def test_a_run_with_nothing_synced():
    report = autosync.run_cycle()
    assert report["checked"] == 0
    assert report["queued"] == 0
    assert db.get_autosync_state()["last_run"]


def test_a_run_checks_every_synced_series(site):
    site({1: 1})
    track()
    track("https://aniworld.to/anime/stream/bleach", title="Bleach")
    report = autosync.run_cycle()
    assert report["checked"] == 2
    assert report["queued"] == 2


def test_a_run_records_the_result_on_each_series(site):
    site({1: 1})
    row = track()
    autosync.run_cycle()
    row = db.get_autosync_series_by_id(row["id"])
    assert row["last_checked"]
    assert json.loads(row["last_result"])["status"] == "queued"


def test_one_broken_series_does_not_sink_the_run(monkeypatch):
    def explode(url):
        raise RuntimeError("series page is down")

    monkeypatch.setattr(autosync, "resolve_provider", explode)
    row = track()
    report = autosync.run_cycle()
    assert report["results"][0]["status"] == "error"
    assert "series page is down" in report["results"][0]["reason"]
    assert db.get_autosync_series_by_id(row["id"])["last_checked"], (
        "a failure counts as checked, or it is retried on every tick"
    )


def test_a_run_of_one_series_leaves_the_global_clock_alone(site):
    site({1: 1})
    row = track()
    autosync.run_cycle([row], mark_global=False)
    assert "last_run" not in db.get_autosync_state()
    assert db.get_autosync_state()["last_report"]


# ---------------------------------------------------------------------------
# A schedule of its own
# ---------------------------------------------------------------------------
def test_a_global_series_has_no_schedule_of_its_own():
    row = track()
    assert autosync.series_schedule(row) is None
    assert autosync.series_next_run(row) == autosync.next_run_at()


def test_daily_runs_every_day_at_its_time():
    row = track(cadence="daily", run_time="07:30")
    assert autosync.series_schedule(row).expression == "30 7 * * *"
    assert autosync.series_schedule_description(row, "en") == "Every day at 07:30"


@pytest.mark.parametrize(
    "weekday,cron_day,name",
    [(0, 1, "Monday"), (2, 3, "Wednesday"), (6, 0, "Sunday")],
)
def test_weekly_runs_on_its_day(weekday, cron_day, name):
    """The UI counts from Monday, cron from Sunday."""
    row = track(cadence="weekly", weekday=weekday)
    assert autosync.series_schedule(row).expression == f"0 22 * * {cron_day}"
    assert autosync.series_schedule_description(row, "en") == f"On {name} at 22:00"


def test_a_preset_does_not_fire_the_moment_it_is_ticked():
    row = track(cadence="daily")
    assert autosync._series_due(row) is False
    assert autosync.series_next_run(row) > autosync._now()


def _checked_ago(row, days):
    db.record_autosync_check(
        row["id"], (datetime.now(UTC) - timedelta(days=days)).isoformat(), None
    )
    return db.get_autosync_series_by_id(row["id"])


def test_a_preset_is_due_once_its_time_has_passed():
    row = _checked_ago(track(cadence="daily"), 2)
    assert autosync._series_due(row) is True


def test_a_preset_counts_from_its_last_check():
    row = _checked_ago(track(cadence="daily", run_time="22:00"), 0)
    upcoming = autosync.series_next_run(row)
    assert upcoming > autosync._now()
    assert upcoming - autosync._now() <= timedelta(days=1)
    assert autosync._local(upcoming).hour == 22


def test_a_last_check_from_a_wrong_clock_is_ignored():
    row = _checked_ago(track(cadence="daily"), -900)
    upcoming = autosync.series_next_run(row)
    assert upcoming - autosync._now() <= timedelta(days=1)


def test_a_global_series_never_comes_due_on_its_own():
    assert autosync._series_due(track()) is False


@pytest.fixture
def checked(monkeypatch):
    """Record which series a tick picks instead of checking them."""
    calls = []
    monkeypatch.setattr(
        autosync,
        "run_cycle",
        lambda rows=None, mark_global=True: calls.append(
            ([row["series_url"] for row in rows], mark_global)
        ),
    )
    return calls


def test_a_due_global_schedule_checks_the_global_series(checked):
    track()
    _checked_ago(track("https://b", title="Daily", cadence="daily"), 0)
    autosync._tick()
    assert checked == [([SERIES], True)]


def test_a_due_preset_is_checked_alone(checked):
    db.set_autosync_state(last_run=datetime.now(UTC).isoformat())
    track()
    _checked_ago(track("https://b", title="Daily", cadence="daily"), 2)
    autosync._tick()
    assert checked == [(["https://b"], False)], "and the global clock is left alone"


def test_both_come_due_together(checked):
    track()
    _checked_ago(track("https://b", title="Daily", cadence="daily"), 2)
    autosync._tick()
    assert checked == [(["https://b", SERIES], True)]


def test_a_due_global_schedule_is_recorded_with_nothing_to_check(checked):
    autosync._tick()
    assert checked == [([], True)]


def test_nothing_due_runs_nothing(checked):
    db.set_autosync_state(last_run=datetime.now(UTC).isoformat())
    track()
    autosync._tick()
    assert checked == []


def test_the_nap_shrinks_for_a_preset_series(monkeypatch):
    monkeypatch.setenv("ANIWORLD_ENABLE_AUTOSYNC", "1")
    db.set_autosync_state(last_run=datetime.now(UTC).isoformat())
    soon = autosync._local(datetime.now(UTC) + timedelta(minutes=2))
    track(cadence="daily", run_time=soon.strftime("%H:%M"))
    assert autosync._nap_seconds() <= 120


def test_status_lists_the_synced_series(monkeypatch):
    monkeypatch.setenv("ANIWORLD_ENABLE_AUTOSYNC", "1")
    track(cadence="weekly", weekday=0)
    series = autosync.status()["series"]
    assert len(series) == 1
    assert series[0]["schedule"] == "On Monday at 22:00"
    assert series[0]["where"] == "Default"
    assert series[0]["next_run"]


# ---------------------------------------------------------------------------
# When it runs
#
# The schedule itself is tested in test_schedule.py, this is about what
# Auto-Sync does with it.
# ---------------------------------------------------------------------------
def _ran(hours_ago):
    db.set_autosync_state(
        last_run=(datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()
    )


def test_an_install_that_never_ran_is_due_right_away():
    """The behaviour from before the schedule was configurable."""
    assert autosync._due() is True


def test_the_default_interval_is_a_day():
    _ran(hours_ago=2)
    assert autosync._due() is False
    _ran(hours_ago=25)
    assert autosync._due() is True


def test_the_interval_can_be_shortened(monkeypatch):
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_INTERVAL", "90m")
    _ran(hours_ago=2)
    assert autosync._due() is True


def test_the_interval_can_be_lengthened(monkeypatch):
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_INTERVAL", "7d")
    _ran(hours_ago=48)
    assert autosync._due() is False


def test_a_broken_interval_falls_back_to_a_day(monkeypatch):
    """A hand-edited .env must not take the worker down with it."""
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_INTERVAL", "whenever")
    _ran(hours_ago=2)
    assert autosync._due() is False
    _ran(hours_ago=25)
    assert autosync._due() is True


@pytest.fixture
def fixed_times(monkeypatch):
    """Switch Auto-Sync over to a cron schedule."""

    def use(expression):
        monkeypatch.setenv("ANIWORLD_AUTOSYNC_MODE", "cron")
        monkeypatch.setenv("ANIWORLD_AUTOSYNC_CRON", expression)

    return use


def test_a_fixed_time_is_read_as_local_time(fixed_times):
    """A cron line means 22:00 on the wall, whatever the machine's timezone."""
    fixed_times("0 22 * * *")
    _ran(hours_ago=2)
    upcoming = autosync._local(autosync.next_run_at())
    assert (upcoming.hour, upcoming.minute) == (22, 0)


def test_the_next_fixed_time_follows_the_last_run(fixed_times):
    fixed_times("every day at 08:00, 22:30")
    _ran(hours_ago=2)
    upcoming = autosync._local(autosync.next_run_at())
    assert (upcoming.hour, upcoming.minute) in {(8, 0), (22, 30)}


def test_a_fixed_time_does_not_fire_the_moment_it_is_turned_on(fixed_times):
    """Counted from now, so enabling it never sets a download going at once."""
    fixed_times("* * * * *")
    assert autosync._due() is False
    assert autosync.next_run_at() > autosync._now()


def test_a_missed_fixed_time_is_caught_up_on(fixed_times):
    """The machine was off at 22:00, so it runs as soon as it is back."""
    fixed_times("0 22 * * *")
    _ran(hours_ago=24 * 7)
    assert autosync._due() is True


def test_a_last_run_without_a_timezone_does_not_stall_the_worker():
    """One of these in the database raised on every tick, and Auto-Sync then
    never ran again: the error was caught and logged, so nothing said why."""
    db.set_autosync_state(last_run="2026-01-01T00:00:00")
    assert autosync._due() is True, "a year ago, so it is due"
    assert autosync.next_run_at() is not None


def test_switching_it_on_is_what_starts_the_clock(monkeypatch, fixed_times):
    """The worker keeps the anchor fresh while Auto-Sync is off.

    Without that, a server up since Monday would count Friday's switch-on from
    Monday, find a fixed time long past, and queue downloads on the spot.
    """
    fixed_times("0 3 * * *")
    monkeypatch.setattr(autosync, "_anchored_at", datetime.now(UTC) - timedelta(days=4))
    assert autosync._due() is True, "the stale anchor is what causes it"

    autosync._reset_anchor()
    assert autosync._due() is False
    assert autosync._local(autosync.next_run_at()).hour == 3


def test_a_last_run_from_a_wrong_clock_is_ignored():
    """A box whose clock was years ahead once would otherwise never run again."""
    db.set_autosync_state(
        last_run=(datetime.now(UTC) + timedelta(days=900)).isoformat()
    )
    assert autosync._due() is True, "back to a fresh install, not parked in 2028"


def test_a_run_a_minute_ahead_is_left_alone():
    """Clocks drift, and a cycle stamps its start before it stamps anything."""
    db.set_autosync_state(
        last_run=(datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    )
    assert autosync._due() is False


def test_a_broken_cron_line_falls_back_to_the_default(fixed_times):
    fixed_times("every blursday at half past nonsense")
    assert autosync.status()["cron"] == "0 3 * * *"


def test_the_nap_never_outlasts_a_tick(monkeypatch, fixed_times):
    """However far off the next run is, settings changes still get picked up."""
    monkeypatch.setenv("ANIWORLD_ENABLE_AUTOSYNC", "1")
    fixed_times("0 4 1 1 *")
    assert autosync._nap_seconds() == autosync.TICK_SECONDS


def test_the_nap_shrinks_to_hit_a_fixed_time(monkeypatch, fixed_times):
    """A five minute tick would otherwise make 22:00 mean "22:00 give or take"."""
    monkeypatch.setenv("ANIWORLD_ENABLE_AUTOSYNC", "1")
    fixed_times("* * * * *")
    assert autosync.MIN_TICK_SECONDS <= autosync._nap_seconds() <= 60


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def test_status_of_a_fresh_install():
    report = autosync.status()
    assert report["running"] is False
    assert report["last_run"] is None


def test_status_reports_the_last_run():
    db.set_autosync_state(
        last_run="2026-01-01T00:00:00+00:00", last_report='{"queued": 2}'
    )
    report = autosync.status()
    assert report["last_run"] == "2026-01-01T00:00:00+00:00"
    assert report["last_report"]["queued"] == 2


def test_status_describes_an_interval_schedule(monkeypatch):
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_INTERVAL", "6h")
    report = autosync.status()
    assert report["mode"] == "interval"
    assert report["interval"] == "6h"
    assert report["interval_seconds"] == 6 * 3600
    assert report["interval_hours"] == 6
    assert report["cron"] is None
    assert report["schedule"] == "Every 6 hours"


def test_a_sub_hour_interval_is_still_reported_in_hours(monkeypatch):
    """It used to floor to 0, which read as "no interval at all"."""
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_INTERVAL", "90m")
    assert autosync.status()["interval_hours"] == 1.5


def test_status_describes_fixed_times(monkeypatch):
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_MODE", "cron")
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_CRON", "every monday and friday at 10pm")
    report = autosync.status()
    assert report["mode"] == "cron"
    assert report["cron"] == "0 22 * * 1,5"
    assert report["schedule"] == "On Monday and Friday at 22:00"


def test_status_describes_the_schedule_in_the_ui_language(monkeypatch):
    monkeypatch.setenv("ANIWORLD_UI_LANGUAGE", "de")
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_MODE", "cron")
    monkeypatch.setenv("ANIWORLD_AUTOSYNC_CRON", "0 22 * * 1")
    assert autosync.status()["schedule"] == "Jeden Montag um 22:00"


# ---------------------------------------------------------------------------
# The API behind the download modal and the Auto-Sync page
# ---------------------------------------------------------------------------
@pytest.fixture
def api(client, monkeypatch):
    monkeypatch.setenv("ANIWORLD_ENABLE_AUTOSYNC", "1")
    return client


def test_everything_is_hidden_while_auto_sync_is_off(client):
    assert client.get(f"/api/autosync/tracked?url={SERIES}").status_code == 404
    assert client.get("/api/autosync/series").status_code == 404


def test_an_untracked_series_says_so(api):
    data = api.get(f"/api/autosync/tracked?url={SERIES}").get_json()
    assert data == {"tracked": False, "series": None}


def test_ticking_sync_this_saves_the_settings(api):
    response = api.post(
        "/api/autosync/tracked",
        json={
            "series_url": SERIES,
            "title": "Naruto",
            "tracked": True,
            "language": "German Sub",
            "start_season": 2,
            "start_episode": 5,
            "cadence": "daily",
            "run_time": "06:00",
        },
    )
    assert response.status_code == 200
    row = db.get_autosync_series_by_url(SERIES)
    assert row["language"] == "German Sub"
    assert (row["start_season"], row["start_episode"]) == (2, 5)
    assert (row["cadence"], row["run_time"]) == ("daily", "06:00")

    data = api.get(f"/api/autosync/tracked?url={SERIES}").get_json()
    assert data["tracked"] is True
    assert data["series"]["schedule"] == "Every day at 06:00"


def test_ticking_needs_a_language(api):
    response = api.post(
        "/api/autosync/tracked", json={"series_url": SERIES, "tracked": True}
    )
    assert response.status_code == 400
    assert db.get_autosync_series() == []


def test_changing_one_setting_keeps_the_language(api):
    track(language="English Dub")
    response = api.post(
        "/api/autosync/tracked",
        json={"series_url": SERIES, "tracked": True, "start_episode": 9},
    )
    assert response.status_code == 200
    row = db.get_autosync_series_by_url(SERIES)
    assert (row["language"], row["start_episode"]) == ("English Dub", 9)


def test_unticking_removes_it(api):
    track()
    api.post("/api/autosync/tracked", json={"series_url": SERIES, "tracked": False})
    assert db.get_autosync_series() == []


def test_a_bad_setting_is_refused(api):
    response = api.post(
        "/api/autosync/tracked",
        json={
            "series_url": SERIES,
            "tracked": True,
            "language": "German Dub",
            "weekday": 9,
        },
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "Pick a day of the week."
    assert db.get_autosync_series() == []


def test_the_page_lists_and_edits_series(api):
    row = track()
    listed = api.get("/api/autosync/series").get_json()["series"]
    assert [entry["id"] for entry in listed] == [row["id"]]

    response = api.put(f"/api/autosync/series/{row['id']}", json={"cadence": "weekly"})
    assert response.status_code == 200
    assert db.get_autosync_series_by_id(row["id"])["cadence"] == "weekly"


def test_editing_a_missing_series_is_a_404(api):
    response = api.put("/api/autosync/series/4242", json={"cadence": "daily"})
    assert response.status_code == 404
    assert api.post("/api/autosync/series/4242/run").status_code == 404


def test_removing_from_the_page(api):
    row = track()
    assert api.delete(f"/api/autosync/series/{row['id']}").status_code == 200
    assert db.get_autosync_series() == []


def test_the_page_has_the_synced_list(api):
    body = api.get("/autosync").get_data(as_text=True)
    assert 'id="syncedList"' in body
    assert "autosync.excluded_title" not in body
