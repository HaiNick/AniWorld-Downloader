"""Site search plumbing shared by the web search API and the Discord bot.

Each site's search returns a slightly different shape, this normalises them all
to {title, url} and filters out season/episode links so only series-level hits
come back.
"""

import html
import re
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeout

from ..logger import get_logger
from ..search import (
    fetch_aniworld_genres,
    fetch_burningseries_genres,
    fetch_filmo_genres,
    fetch_filmpalast_genres,
    fetch_hanime_genres,
    fetch_kinox_genres,
    fetch_mangafire_genres,
    fetch_megakino_genres,
    fetch_moflix_genres,
    fetch_s_to_genres,
    query_aniworld,
    query_burningseries,
    query_filmo,
    query_filmpalast,
    query_hanime,
    query_hentai_tv,
    query_hentaihaven,
    query_kinox,
    query_mangafire,
    query_megakino,
    query_moflix,
    query_s_to,
)
from .media import folder_matches_title

logger = get_logger(__name__)

SITE_SEARCH = {
    "moflix": query_moflix,
    "aniworld": query_aniworld,
    "sto": query_s_to,
    "megakino": query_megakino,
    "kinox": query_kinox,
    "filmpalast": query_filmpalast,
    "filmo": query_filmo,
    "burningseries": query_burningseries,
    "mangafire": query_mangafire,
    "htv": query_hanime,
    "hentaitv": query_hentai_tv,
    "hentaihaven": query_hentaihaven,
}

# aniworld/serienstream return relative `/.../<slug>` links, everything else
# returns an absolute `url`. These two also need series-only filtering.
_RELATIVE_SITES = {
    "aniworld": (
        "https://aniworld.to",
        re.compile(r"^/anime/stream/[a-zA-Z0-9\-]+/?$", re.IGNORECASE),
    ),
    "sto": (
        "https://serienstream.to",
        re.compile(r"^/serie/(stream/)?[a-zA-Z0-9\-]+/?$", re.IGNORECASE),
    ),
}

_ABSOLUTE_BASES = {"mangafire": "https://mangafire.to"}

# hanime results are identified by a slug instead of a link of any kind.
_SLUG_URLS = {"htv": "https://hanime.tv/videos/hentai/{slug}"}

# Sites checked for a Discord request, in priority order. Kinox carries both
# movies and series, so it appears in both lists.
SERIES_SITES = ("sto", "burningseries", "aniworld", "kinox", "moflix")
MOVIE_SITES = ("megakino", "filmpalast", "filmo", "kinox", "moflix")


def sites_for(media_type):
    return MOVIE_SITES if media_type == "movie" else SERIES_SITES


def _clean_title(value, fallback=""):
    title = value or fallback or "Unknown"
    # aniworld sends "Rock Lee &amp; His Ninja Pals"; every caller escapes again
    return html.unescape(title.replace("<em>", "").replace("</em>", "")).strip()


def _poster(item):
    for key in ("poster_url", "cover_url", "image", "poster"):
        value = item.get(key)
        if value:
            return value
    return ""


def _normalise(site, raw, fallback=""):
    """Turn one site's raw hits into [{title, url, poster}], dropping the rest."""
    if isinstance(raw, dict):
        raw = [raw]

    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = _resolve_url(site, item)
        if not url:
            continue
        results.append(
            {
                "title": _clean_title(item.get("title") or item.get("name"), fallback),
                "url": url,
                "poster": _poster(item),
            }
        )
    return results


def search(site, keyword):
    """Run one site's search and return normalised [{title, url, poster}]."""
    query = SITE_SEARCH.get(site)
    if not query:
        return []

    try:
        raw = query(keyword) or []
    except Exception as exc:
        logger.warning("Search on %s failed for '%s': %s", site, keyword, exc)
        return []
    return _normalise(site, raw, keyword)


def _resolve_url(site, item):
    slug_url = _SLUG_URLS.get(site)
    if slug_url and item.get("slug"):
        return slug_url.format(slug=item["slug"])

    url = item.get("url")
    if url:
        base = _ABSOLUTE_BASES.get(site)
        if base and not url.startswith("http"):
            return base + url
        return url

    link = item.get("link")
    if not link:
        return None
    base, pattern = _RELATIVE_SITES.get(site, (None, None))
    if base is None:
        return link
    # Skip season/episode links, only series pages belong in results
    return base + link if pattern.match(link) else None


# Every site whose genre listing the Web UI can offer.
GENRE_LISTS = {
    "aniworld": fetch_aniworld_genres,
    "sto": fetch_s_to_genres,
    "burningseries": fetch_burningseries_genres,
    "megakino": fetch_megakino_genres,
    "kinox": fetch_kinox_genres,
    "filmpalast": fetch_filmpalast_genres,
    "filmo": fetch_filmo_genres,
    "htv": fetch_hanime_genres,
    "mangafire": fetch_mangafire_genres,
    "moflix": fetch_moflix_genres,
}

GENRE_SITES = tuple(GENRE_LISTS)

# AniWorld is missing here on purpose: the site pages its own genre listing, so
# it goes through fetch_genre_animes(). Everything else returns one flat list
# that limit cuts off, and the caller slices it into pages.
GENRE_QUERIES = {
    "sto": lambda genre, limit: query_s_to(genre=genre, limit=limit),
    "burningseries": lambda genre, limit: query_burningseries(genre=genre, limit=limit),
    "megakino": lambda genre, limit: query_megakino(genre=genre, limit=limit),
    "kinox": lambda genre, limit: query_kinox(genre=genre, limit=limit),
    "filmpalast": lambda genre, limit: query_filmpalast(genre=genre, limit=limit),
    "filmo": lambda genre, limit: query_filmo(genre=genre, limit=limit),
    "htv": lambda genre, limit: query_hanime(genre=genre, limit=limit),
    "mangafire": lambda genre, limit: query_mangafire(genre=genre, limit=limit),
    "moflix": lambda genre, limit: query_moflix(genre=genre, limit=limit),
}


def genres(site):
    """The genre chips for a site, as [{name, slug}]."""
    fetch = GENRE_LISTS.get(site)
    return fetch() if fetch else []


def genre_results(site, genre, limit):
    """Browse one site's genre, normalised like a search result.

    Returns (results, has_more). The flag counts the site's own hits, before
    normalising drops the ones the app cannot open afterwards: serienstream
    lists the odd title under a slug no provider pattern accepts, and a single
    one of those would otherwise read as the end of the listing.

    Failures propagate on purpose: an empty genre is worth telling apart from a
    site that would not answer, which a browse row cannot do.
    """
    query = GENRE_QUERIES.get(site)
    if not query:
        return [], False
    raw = query(genre, limit) or []
    if isinstance(raw, dict):
        raw = [raw]
    return _normalise(site, raw), len(raw) >= limit


def aggregate(title, media_type, per_site=8, limit=25):
    """Search every site of a media type and return combined, site-tagged hits."""
    combined = []
    for site in sites_for(media_type):
        for item in search(site, title)[:per_site]:
            combined.append({**item, "site": site})
            if len(combined) >= limit:
                return combined
    return combined


# ---------------------------------------------------------------------------
# Searching every site at once, for the Jellyfin plugin
#
# aniworld only hosts anime, so a title found there is anime and the s.to copy
# of it is dropped. Whatever s.to has beyond that is a series. The other series
# sites are only asked when s.to has nothing at all.
# ---------------------------------------------------------------------------
SEARCH_TYPES = ("all", "anime", "series", "movie")
ANIME_SITE = "aniworld"
SERIES_SITE = "sto"
SERIES_FALLBACK_SITES = ("burningseries", "kinox", "moflix")

# Seconds each site gets from its own start. They all run at the same time, so
# one slow site costs at most this long instead of holding up the others.
SITE_TIMEOUT = 10

_TYPE_ORDER = {"anime": 0, "series": 1, "movie": 2}
_SITE_ORDER = {
    site: index
    for index, site in enumerate(
        (ANIME_SITE, SERIES_SITE, *SERIES_FALLBACK_SITES, *MOVIE_SITES)
    )
}


def _same_title(a, b):
    # Either side may carry a year or an ID the other lacks, "Naruto (2002)"
    return folder_matches_title(a, b) or folder_matches_title(b, a)


def _is_anime(item, anime):
    return any(_same_title(item["title"], hit["title"]) for hit in anime)


def search_stream(keyword, media_type="all", timeout=SITE_TIMEOUT):
    """Search across sites with the anime/series/movie priority rules, site by site.

    Yields one {"site", "results", "pending"} per site as soon as that site has
    answered, so a slow site no longer holds up the rest. "results" are
    [{title, url, poster, site, type}]; "pending" lists the sites still running.
    A site that misses its timeout yields once with no results and
    "timed_out": True.

    aniworld and s.to come first, because they decide the rest: what aniworld
    has is anime, so its copies elsewhere are dropped, and the other series
    sites are only asked when s.to has nothing. Both answer quickly; the movie
    sites already run in the meantime. A link is only listed once, under the
    first type that claimed it.
    """
    want_anime = media_type in ("all", "anime")
    want_series = media_type in ("all", "series")
    want_movies = media_type in ("all", "movie")
    shows = want_anime or want_series

    pool = ThreadPoolExecutor(max_workers=len(_SITE_ORDER), thread_name_prefix="search")
    running = {}  # site -> (future, started)

    def start(site):
        if site not in running:
            running[site] = (pool.submit(search, site, keyword), time.monotonic())

    def answer(site):
        future, started = running[site]
        try:
            return future.result(
                timeout=max(0, started + timeout - time.monotonic())
            ), False
        except FutureTimeout:
            logger.warning("Search on %s timed out for '%s'", site, keyword)
            return [], True

    seen = set()
    done = set()
    anime = []

    def event(site, kind, items, timed_out=False):
        done.add(site)
        results = []
        for item in items:
            if item["url"] in seen or (kind != "anime" and _is_anime(item, anime)):
                continue
            seen.add(item["url"])
            results.append({**item, "site": site, "type": kind})
        reply = {
            "site": site,
            "results": results,
            "pending": [s for s in running if s not in done],
        }
        if timed_out:
            reply["timed_out"] = True
        return reply

    try:
        if shows:
            # Series need aniworld too, to know which s.to hits are really anime
            start(ANIME_SITE)
            start(SERIES_SITE)
        if want_movies:
            for site in MOVIE_SITES:
                start(site)

        series_sites = ()
        if shows:
            anime, late = answer(ANIME_SITE)
            if want_anime:
                yield event(ANIME_SITE, "anime", anime, late)
            else:
                done.add(ANIME_SITE)

            series, late = answer(SERIES_SITE)
            if want_series and series:
                yield event(SERIES_SITE, "series", series, late)
            else:
                done.add(SERIES_SITE)
                if want_series:
                    # kinox and moflix may already run for the movies; that
                    # search is reused, and its hits count as series then
                    series_sites = SERIES_FALLBACK_SITES
                    for site in series_sites:
                        start(site)

        while True:
            waiting = {
                future: site
                for site, (future, _) in running.items()
                if site not in done
            }
            if not waiting:
                break

            # Wake for whichever comes first: an answer or the next deadline
            next_deadline = min(running[site][1] + timeout for site in waiting.values())
            finished, _ = wait(
                waiting,
                timeout=max(0, next_deadline - time.monotonic()),
                return_when=FIRST_COMPLETED,
            )
            now = time.monotonic()
            for future, site in waiting.items():
                if future in finished or now >= running[site][1] + timeout:
                    items, late = answer(site)
                    kind = "series" if site in series_sites else "movie"
                    yield event(site, kind, items, late)
    finally:
        # Late sites finish on their own; nothing waits for them
        pool.shutdown(wait=False)


def search_all(keyword, media_type="all", timeout=SITE_TIMEOUT):
    """Search across sites with the anime/series/movie priority rules.

    The same as search_stream(), collected: [{title, url, poster, site, type}],
    anime first, then series, then movies, each in site order.
    """
    results = [
        item
        for event in search_stream(keyword, media_type, timeout)
        for item in event["results"]
    ]
    # Stable, so each site keeps its own ranking
    results.sort(
        key=lambda item: (_TYPE_ORDER[item["type"]], _SITE_ORDER[item["site"]])
    )
    return results
