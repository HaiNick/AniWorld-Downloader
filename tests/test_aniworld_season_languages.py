"""Language badges on an aniworld season page, read without opening episodes.

Auto-Sync uses them to hold back an episode that is not out in the language a
series is synced in yet.
"""

from aniworld.models import AniworldSeason

SEASON = "https://aniworld.to/anime/stream/goblin-slayer/staffel-1"

ROWS = """
<tbody id="season1">
<tr class="" data-episode-id="2311" data-episode-season-id="1" itemprop="episode" itemscope="" itemtype="http://schema.org/Episode">
    <td class="season1EpisodeID">
        <meta itemprop="episodeNumber" content="1"><a itemprop="url" href="/anime/stream/goblin-slayer/staffel-1/episode-1"> Folge 1 </a>
    </td>
    <td class="editFunctions"><a href="/anime/stream/goblin-slayer/staffel-1/episode-1"> <img class="flag" src="/public/img/german.svg" title="Deutsch/German"> <img class="flag" src="/public/img/japanese-german.svg" title="Mit deutschem Untertitel"> </a></td>
</tr>
<tr class="" data-episode-id="2312" data-episode-season-id="2" itemprop="episode" itemscope="" itemtype="http://schema.org/Episode">
    <td class="season1EpisodeID">
        <meta itemprop="episodeNumber" content="2"><a itemprop="url" href="/anime/stream/goblin-slayer/staffel-1/episode-2"> Folge 2 </a>
    </td>
    <td class="editFunctions"><a href="/anime/stream/goblin-slayer/staffel-1/episode-2"> <img class="flag" src="/public/img/japanese-english.svg" title="Englisch"> <img class="flag" src="/public/img/english.svg" title="Englisch"> </a></td>
</tr>
<tr class="" data-episode-id="2313" data-episode-season-id="3" itemprop="episode" itemscope="" itemtype="http://schema.org/Episode">
    <td class="season1EpisodeID">
        <meta itemprop="episodeNumber" content="3"><a itemprop="url" href="/anime/stream/goblin-slayer/staffel-1/episode-3"> Folge 3 </a>
    </td>
    <td class="editFunctions"><a href="/anime/stream/goblin-slayer/staffel-1/episode-3"> </a></td>
</tr>
</tbody>
"""


def _season(html, url=SEASON):
    season = AniworldSeason(url)
    season._AniworldSeason__html = html
    return season


def test_every_flag_is_read_per_episode():
    assert _season(ROWS).episode_languages == {
        1: ("German Dub", "German Sub"),
        2: ("English Sub", "English Dub"),
    }


def test_an_episode_without_flags_is_left_out():
    assert 3 not in _season(ROWS).episode_languages


def test_flags_of_one_row_never_leak_into_the_next():
    """Episode 2 carries no German flag, whatever the row before it had."""
    assert "German Dub" not in _season(ROWS).episode_languages[2]


def test_a_page_without_episodes_has_no_flags():
    assert _season("<html></html>").episode_languages == {}


def test_movies_are_not_read():
    movies = _season(ROWS, "https://aniworld.to/anime/stream/goblin-slayer/filme")
    assert movies.episode_languages == {}
