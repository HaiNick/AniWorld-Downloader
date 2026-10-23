"""Series, movie and manga downloads can each have their own root, anime keeps the main one."""

from pathlib import Path

import pytest

from aniworld.models.aniworld_to.episode import AniworldEpisode
from aniworld.models.burningseries.series import BurningSeriesEpisode
from aniworld.models.common.paths import resolve_download_path
from aniworld.models.filmo_to.episode import FilmoEpisode
from aniworld.models.filmpalast_to.episode import FilmPalastEpisode
from aniworld.models.mangafire_to.series import MangaFireToChapter
from aniworld.models.megakino.series import MegaKinoEpisode
from aniworld.models.s_to.episode import SerienstreamEpisode

SERIES = "ANIWORLD_SERIES_DOWNLOAD_PATH"
MOVIE = "ANIWORLD_MOVIE_DOWNLOAD_PATH"
MANGA = "ANIWORLD_MANGA_DOWNLOAD_PATH"
CHAPTER = "https://mangafire.to/title/x/chapter/1"


@pytest.fixture
def roots(monkeypatch, tmp_path):
    main, series, movies = tmp_path / "main", tmp_path / "series", tmp_path / "movies"
    monkeypatch.setenv("ANIWORLD_DOWNLOAD_PATH", str(main))
    monkeypatch.setenv(SERIES, str(series))
    monkeypatch.setenv(MOVIE, str(movies))
    return main, series, movies


@pytest.mark.parametrize(
    ("cls", "url", "root"),
    [
        (AniworldEpisode, "https://aniworld.to/anime/stream/x/staffel-1/episode-1", 0),
        (SerienstreamEpisode, "https://s.to/serie/x/staffel-1/episode-1", 1),
        (BurningSeriesEpisode, "https://bs.to/serie/x/1/1-x/de", 1),
        (FilmPalastEpisode, "https://filmpalast.to/stream/x", 2),
        (FilmoEpisode, "https://filmo.to/movies/x", 2),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_each_site_lands_in_its_root(roots, cls, url, root):
    assert Path(cls(url).selected_path) == roots[root]


def test_split_falls_back_to_main_path_when_unset(roots, monkeypatch):
    monkeypatch.delenv(MOVIE)
    url = "https://filmo.to/movies/x"
    assert Path(FilmoEpisode(url).selected_path) == roots[0]


def test_explicit_path_beats_the_split(roots, tmp_path):
    chosen = tmp_path / "chosen"
    url = "https://s.to/serie/x/staffel-1/episode-1"
    assert Path(SerienstreamEpisode(url, selected_path=chosen).selected_path) == chosen


def test_mixed_sites_pick_the_root_by_title_type(roots, monkeypatch):
    url = "https://megakino.example/serials/123-x.html"
    monkeypatch.setattr(MegaKinoEpisode, "is_series", property(lambda self: False))
    assert Path(MegaKinoEpisode(url).selected_path) == roots[2]
    monkeypatch.setattr(MegaKinoEpisode, "is_series", property(lambda self: True))
    assert Path(MegaKinoEpisode(url).selected_path) == roots[1]


def test_title_type_is_only_looked_up_when_needed(monkeypatch, tmp_path):
    monkeypatch.delenv(SERIES, raising=False)
    monkeypatch.delenv(MOVIE, raising=False)
    monkeypatch.setenv("ANIWORLD_DOWNLOAD_PATH", str(tmp_path))

    def lookup():
        raise AssertionError("no split path is set, so the type does not matter")

    assert resolve_download_path(None, lookup) == tmp_path
    assert resolve_download_path(tmp_path / "x", lookup) == tmp_path / "x"


def test_manga_land_in_their_own_root(roots, monkeypatch, tmp_path):
    monkeypatch.setenv(MANGA, str(tmp_path / "manga"))
    assert Path(MangaFireToChapter(CHAPTER).selected_path) == tmp_path / "manga"


def test_manga_fall_back_to_the_main_path_when_unset(roots, monkeypatch):
    monkeypatch.delenv(MANGA, raising=False)
    assert Path(MangaFireToChapter(CHAPTER).selected_path) == roots[0]


def test_an_explicit_path_beats_the_manga_root(roots, monkeypatch, tmp_path):
    monkeypatch.setenv(MANGA, str(tmp_path / "manga"))
    chosen = tmp_path / "chosen"
    assert (
        Path(MangaFireToChapter(CHAPTER, selected_path=chosen).selected_path) == chosen
    )
