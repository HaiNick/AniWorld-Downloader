"""Searching every site at once: which site wins and what counts as a duplicate."""

import threading
import time

import pytest

from aniworld.web import sitesearch


def hit(title, site):
    slug = title.lower().replace(" ", "-")
    return {"title": title, "url": f"https://{site}.test/{slug}", "poster": ""}


@pytest.fixture
def sites(monkeypatch):
    """Fake sites: fill sites[name] with titles, read asked for the calls made."""
    catalog = {}
    asked = []
    lock = threading.Lock()

    def fake_search(site, keyword):
        with lock:
            asked.append(site)
        return [hit(title, site) for title in catalog.get(site, [])]

    monkeypatch.setattr(sitesearch, "search", fake_search)
    catalog["asked"] = asked
    return catalog


def found(results):
    return [(r["title"], r["site"], r["type"]) for r in results]


def test_a_title_on_aniworld_is_anime_and_the_s_to_copy_goes(sites):
    sites["aniworld"] = ["Naruto"]
    sites["sto"] = ["Naruto", "Breaking Bad"]
    assert found(sitesearch.search_all("x")) == [
        ("Naruto", "aniworld", "anime"),
        ("Breaking Bad", "sto", "series"),
    ]


def test_the_s_to_copy_goes_even_with_a_year_on_it(sites):
    sites["aniworld"] = ["Naruto"]
    sites["sto"] = ["Naruto (2002)", "Naruto Shippuden"]
    assert found(sitesearch.search_all("x", "series")) == [
        ("Naruto Shippuden", "sto", "series"),
    ]


def test_titles_match_regardless_of_case_and_quotes(sites):
    sites["aniworld"] = ["Frieren: Beyond Journey’s End"]
    sites["sto"] = ["frieren: beyond journey's end"]
    assert found(sitesearch.search_all("x", "series")) == []


def test_anime_only_asks_aniworld_and_s_to(sites):
    sites["aniworld"] = ["Naruto"]
    sites["megakino"] = ["Naruto The Movie"]
    assert found(sitesearch.search_all("x", "anime")) == [
        ("Naruto", "aniworld", "anime"),
    ]
    assert sorted(sites["asked"]) == ["aniworld", "sto"]


def test_series_skips_the_anime_it_found_on_aniworld(sites):
    sites["aniworld"] = ["One Piece"]
    sites["sto"] = ["One Piece", "One Piece (2023)", "Dark"]
    assert found(sitesearch.search_all("x", "series")) == [("Dark", "sto", "series")]


def test_the_other_series_sites_are_only_asked_when_s_to_has_nothing(sites):
    sites["sto"] = ["Dark"]
    sitesearch.search_all("x", "series")
    assert not set(sites["asked"]) & set(sitesearch.SERIES_FALLBACK_SITES)


def test_s_to_with_nothing_falls_back_to_the_other_series_sites(sites):
    sites["burningseries"] = ["Dark"]
    sites["moflix"] = ["Dark Matter"]
    assert found(sitesearch.search_all("x", "series")) == [
        ("Dark", "burningseries", "series"),
        ("Dark Matter", "moflix", "series"),
    ]


def test_the_fallback_still_drops_anime(sites):
    sites["aniworld"] = ["Naruto"]
    sites["kinox"] = ["Naruto", "Narcos"]
    assert found(sitesearch.search_all("x", "series")) == [
        ("Narcos", "kinox", "series"),
    ]


def test_movies_come_from_every_movie_site_with_their_site(sites):
    sites["megakino"] = ["Dune"]
    sites["filmpalast"] = ["Dune"]
    sites["sto"] = ["Dune: Prophecy"]
    assert found(sitesearch.search_all("x", "movie")) == [
        ("Dune", "megakino", "movie"),
        ("Dune", "filmpalast", "movie"),
    ]
    assert set(sites["asked"]) == set(sitesearch.MOVIE_SITES)


def test_all_lists_anime_then_series_then_movies(sites):
    sites["megakino"] = ["Naruto The Movie"]
    sites["sto"] = ["Naruto Abridged"]
    sites["aniworld"] = ["Naruto"]
    assert found(sitesearch.search_all("x")) == [
        ("Naruto", "aniworld", "anime"),
        ("Naruto Abridged", "sto", "series"),
        ("Naruto The Movie", "megakino", "movie"),
    ]


def test_an_anime_listed_on_a_movie_site_stays_with_the_anime(sites):
    sites["aniworld"] = ["Boruto: Naruto Next Generations"]
    sites["kinox"] = ["Boruto: Naruto Next Generations", "Naruto Shippuden - The Movie"]
    sites["sto"] = ["Dark"]
    assert found(sitesearch.search_all("x")) == [
        ("Boruto: Naruto Next Generations", "aniworld", "anime"),
        ("Dark", "sto", "series"),
        ("Naruto Shippuden - The Movie", "kinox", "movie"),
    ]


def test_titles_come_back_without_html_entities(monkeypatch):
    monkeypatch.setattr(
        sitesearch,
        "SITE_SEARCH",
        {
            "aniworld": lambda keyword: [
                {
                    "name": "Rock Lee &amp; His Ninja Pals",
                    "link": "/anime/stream/rock-lee",
                }
            ]
        },
    )
    assert sitesearch.search("aniworld", "x")[0]["title"] == "Rock Lee & His Ninja Pals"


def test_a_link_already_shown_as_a_series_is_not_repeated_as_a_movie(sites):
    """kinox carries both, so its hits can come back from two searches."""
    sites["kinox"] = ["Dark"]
    assert found(sitesearch.search_all("x")) == [("Dark", "kinox", "series")]


def test_the_fallback_does_not_ask_a_site_twice(sites):
    sites["kinox"] = ["Dark"]
    sitesearch.search_all("x")
    assert sites["asked"].count("kinox") == 1
    assert sites["asked"].count("moflix") == 1


def test_one_slow_site_does_not_hold_up_the_rest(monkeypatch):
    release = threading.Event()

    def fake_search(site, keyword):
        if site == "sto":
            release.wait(5)
        return [hit("Naruto", site)] if site == "aniworld" else []

    monkeypatch.setattr(sitesearch, "search", fake_search)
    started = time.monotonic()
    try:
        results = sitesearch.search_all("x", "anime", timeout=0.2)
    finally:
        release.set()
    assert time.monotonic() - started < 2
    assert found(results) == [("Naruto", "aniworld", "anime")]


# ---------------------------------------------------------------------------
# POST /api/search with site "all"
# ---------------------------------------------------------------------------
def test_the_api_returns_site_and_type_for_each_hit(client, sites):
    sites["aniworld"] = ["Naruto"]
    sites["sto"] = ["Dark"]
    response = client.post(
        "/api/search", json={"keyword": "x", "site": "all", "type": "all"}
    )
    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [(r["title"], r["site"], r["type"]) for r in results] == [
        ("Naruto", "aniworld", "anime"),
        ("Dark", "sto", "series"),
    ]
    assert set(results[0]) == {"title", "url", "site", "type", "poster_url"}


def test_the_api_defaults_to_every_type(client, sites):
    sites["megakino"] = ["Dune"]
    response = client.post("/api/search", json={"keyword": "x", "site": "all"})
    assert found(response.get_json()["results"]) == [("Dune", "megakino", "movie")]


def test_the_api_rejects_an_unknown_type(client, sites):
    response = client.post(
        "/api/search", json={"keyword": "x", "site": "all", "type": "manga"}
    )
    assert response.status_code == 400
    assert sites["asked"] == []


def test_one_site_searches_still_work(client, sites):
    sites["sto"] = ["Dark"]
    response = client.post("/api/search", json={"keyword": "x", "site": "sto"})
    assert [r["title"] for r in response.get_json()["results"]] == ["Dark"]
