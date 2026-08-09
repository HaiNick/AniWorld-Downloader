import html as html_module
import os
import re
from pathlib import Path
from urllib.parse import quote

try:
    from ...config import (
        GLOBAL_SESSION,
        NAMING_TEMPLATE,
        Audio,
        Subtitles,
        build_provider_attempt_order,
        logger,
    )
    from ...extractors import provider_functions
    from ..common import ProviderData, check_downloaded, movie_folder_enabled
    from ..common.common import clean_title
    from ..common.common import (
        download as episode_download,
    )
    from ..common.common import (
        syncplay as episode_syncplay,
    )
    from ..common.common import (
        watch as episode_watch,
    )
    from ..common.provider_map import host_to_provider
except ImportError:
    from aniworld.config import (
        GLOBAL_SESSION,
        NAMING_TEMPLATE,
        Audio,
        Subtitles,
        build_provider_attempt_order,
        logger,
    )
    from aniworld.extractors import provider_functions
    from aniworld.models.common import (
        ProviderData,
        check_downloaded,
        clean_title,
        movie_folder_enabled,
    )
    from aniworld.models.common import (
        download as episode_download,
    )
    from aniworld.models.common import (
        syncplay as episode_syncplay,
    )
    from aniworld.models.common import (
        watch as episode_watch,
    )
    from aniworld.models.common.provider_map import host_to_provider

FILMO_BASE = "https://filmo.to"

FILMO_EPISODE_PATTERN = re.compile(
    r"^https?://(?:www\.)?filmo\.to/movies/[^/?#]+/?$", re.IGNORECASE
)

# Language rows are labelled with a flag class plus a localised caption. The
# flag survives a UI language switch, the caption does not, so match on it
# first and only fall back to the text.
_FLAG_AUDIO = {
    "de": Audio.GERMAN,
    "at": Audio.GERMAN,
    "ch": Audio.GERMAN,
    "gb": Audio.ENGLISH,
    "us": Audio.ENGLISH,
    "jp": Audio.JAPANESE,
}

_LABEL_AUDIO = {
    "deutsch": Audio.GERMAN,
    "german": Audio.GERMAN,
    "english": Audio.ENGLISH,
    "englisch": Audio.ENGLISH,
    "japanese": Audio.JAPANESE,
    "japanisch": Audio.JAPANESE,
}

_AUDIO_LABEL = {
    Audio.GERMAN: "German Dub",
    Audio.ENGLISH: "English Dub",
    Audio.JAPANESE: "Japanese Dub",
}


class FilmoEpisode:
    """
    Represents a single movie on Filmo.

    Filmo carries movies only, so - like FilmPalast - one class covers the
    series/season/episode roles and there is no season model.

    Parameters:
        url:                Required. The Filmo URL for this movie, e.g.,
                            https://filmo.to/movies/the-dark-knight-rises
        selected_path:      Optional. The chosen path; provided in cases such as using a menu.
        selected_language:  Optional. The chosen language; provided in cases such as using a menu.
        selected_provider:  Optional. The chosen provider; provided in cases such as using a menu.

    Attributes (Example):
        url:                    "https://filmo.to/movies/the-dark-knight-rises"
        title_de:               "The Dark Knight Rises"
        release_year:           2012
        runtime_min:            165
        genres:                 ["Action", "Krimi", "Drama", "Thriller"]
        description:            "Nach dem Tod des Staatsanwalts Harvey Dent [...]"
        image_url:              "https://filmo.to/img/backdrop/hero-sx1080/q5L9iAS2WPwZI8vTTf7wTn7M"
        director:               "Christopher Nolan"
        actors:                 ["Christian Bale", "Gary Oldman", "Tom Hardy"]
        imdb_rating:            8.4
        age_rating:             "Ab 12"

        provider_data:          {('English', 'None'): {'VOE': 'eyJpdiI6[...]'},
                                 ('German', 'None'): {'VOE': 'eyJpdiI6[...]'}}

        redirect_url:           https://filmo.to/n/1xtpTF5FgFLmOcHq1sAYelhxEjD3hXBI
        provider_url:           https://voe.sx/e/zvu8exzrthtj?default_audio_language=en
        stream_url:             https://[...]/master.m3u8?[...]

        selected_path:          "Downloads"
        selected_language:      "German Dub"
        selected_provider:      "VOE"

    Methods:
        download()
        watch()
        syncplay()
    """

    def __init__(
        self,
        url: str,
        selected_path: str = None,
        selected_language: str = None,
        selected_provider: str = None,
    ):
        if not self.__is_valid_filmo_episode_url(url):
            raise ValueError(f"Invalid Filmo episode URL: {url}")

        self.url = url
        self.__title_de = None
        self.__release_year = None
        self.__runtime_min = None
        self.__genres = None
        self.__description = None
        self.__image_url = None
        self.__director = None
        self.__actors = None
        self.__imdb_rating = None
        self.__age_rating = None

        self.__selected_path_param = selected_path
        self.__selected_language_param = selected_language
        self.__selected_provider_param = selected_provider

        self.__provider_data = None

        self.__selected_path = None
        self.__selected_language = None
        self.__selected_provider = None

        self.__redirect_url = None
        self.__provider_url = None

        self.__csrf_token = None

        # https://jellyfin.org/docs/general/server/media/shows/#organization
        self.__base_folder = None
        self.__folder_path = None
        self.__file_name = None
        self.__file_extension = None
        self.__episode_path = None

        self.__is_downloaded = None

        self.__html = None
        self.__meta_labels = None
        self.__entry_blocks = None

    # -----------------------------
    # STATIC METHODS
    # -----------------------------

    @staticmethod
    def __is_valid_filmo_episode_url(url):
        return bool(FILMO_EPISODE_PATTERN.match(url))

    @staticmethod
    def __strip_tags(value):
        return html_module.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()

    # -----------------------------
    # PUBLIC PROPERTIES (lazy load)
    # -----------------------------

    @property
    def title_de(self):
        if self.__title_de is None:
            self.__extract_title_de()
        return self.__title_de

    @property
    def release_year(self):
        if self.__release_year is None:
            self.__extract_release_year()
        return self.__release_year

    @property
    def runtime_min(self):
        if self.__runtime_min is None:
            self.__extract_runtime_min()
        return self.__runtime_min

    @property
    def genres(self):
        if self.__genres is None:
            self.__extract_genres()
        return self.__genres

    @property
    def description(self):
        if self.__description is None:
            self.__extract_description()
        return self.__description

    @property
    def image_url(self):
        if self.__image_url is None:
            self.__extract_image_url()
        return self.__image_url

    @property
    def director(self):
        if self.__director is None:
            self.__extract_director()
        return self.__director

    @property
    def actors(self):
        if self.__actors is None:
            self.__extract_actors()
        return self.__actors

    @property
    def imdb_rating(self):
        if self.__imdb_rating is None:
            self.__extract_imdb_rating()
        return self.__imdb_rating

    @property
    def age_rating(self):
        if self.__age_rating is None:
            self.__extract_age_rating()
        return self.__age_rating

    @property
    def provider_data(self):
        if self.__provider_data is None:
            self.__provider_data = self.__extract_provider_data()
        return self.__provider_data

    @property
    def selected_path(self):
        if self.__selected_path is None:
            raw_path = self.__selected_path_param or os.getenv(
                "ANIWORLD_MOVIE_DOWNLOAD_PATH"
            ) or os.getenv(
                "ANIWORLD_DOWNLOAD_PATH", str(Path.home() / "Downloads")
            )

            path = Path(raw_path).expanduser()

            if not path.is_absolute():
                path = Path.home() / path

            self.__selected_path = str(path)
        return self.__selected_path

    @selected_path.setter
    def selected_path(self, value):
        self.__selected_path_param = value
        self.__selected_path = None
        self.__base_folder = None
        self.__folder_path = None
        self.__episode_path = None

    @property
    def selected_language(self):
        if self.__selected_language is None:
            self.__selected_language = self._normalize_language(
                self.__selected_language_param
                or os.getenv("ANIWORLD_LANGUAGE", "German Dub")
            )
        return self.__selected_language

    @selected_language.setter
    def selected_language(self, value):
        self.__selected_language_param = value
        self.__selected_language = None
        self.__redirect_url = None
        self.__provider_url = None
        self.__is_downloaded = None
        self.__base_folder = None
        self.__folder_path = None
        self.__episode_path = None
        self.__file_name = None

    @property
    def selected_provider(self):
        if self.__selected_provider is None:
            self.__selected_provider = self.__selected_provider_param or os.getenv(
                "ANIWORLD_PROVIDER", "VOE"
            )
        return self.__selected_provider

    @selected_provider.setter
    def selected_provider(self, value):
        self.__selected_provider_param = value
        self.__selected_provider = None
        self.__redirect_url = None
        self.__provider_url = None

    @property
    def title(self):
        return self.title_de or ""

    @property
    def title_cleaned(self):
        return clean_title(self.title_de or "")

    @property
    def poster_url(self):
        return self.image_url

    @property
    def redirect_url(self):
        """Mint a one-shot player URL for the selected language/provider.

        Filmo hands the browser an encrypted chip payload instead of the
        hoster link. Exchanging it at ``/n`` yields a short-lived token whose
        page 302s to the real embed, so this is resolved per download rather
        than cached on the page.
        """
        if self.__redirect_url is None:
            payload = self.provider_link(self.selected_language, self.selected_provider)
            if payload is None:
                raise ValueError(
                    f"Language '{self.selected_language}' with provider "
                    f"'{self.selected_provider}' is not available for "
                    f"episode: {self.url}"
                )
            self.__redirect_url = self.__mint_player_url(payload)
        return self.__redirect_url

    @property
    def provider_url(self):
        if self.__provider_url is None:
            self.__provider_url = GLOBAL_SESSION.get(
                self.redirect_url, headers={"Referer": self.url}
            ).url
        return self.__provider_url

    @property
    def stream_url(self):
        try:
            stream_url = provider_functions[
                f"get_direct_link_from_{self.selected_provider.lower()}"
            ](self.provider_url)
        except KeyError:
            raise ValueError(
                f"The provider '{self.selected_provider}' is not yet implemented."
            )

        return stream_url

    @property
    def _movie_basename(self):
        year = self.release_year
        base = self.title_cleaned or "Movie"
        return f"{base} ({year})" if year else base

    @property
    def _base_folder(self):
        if self.__base_folder is None:
            if movie_folder_enabled():
                self.__base_folder = Path(self.selected_path) / self._movie_basename
            else:
                self.__base_folder = Path(self.selected_path)
        return self.__base_folder

    @property
    def _folder_path(self):
        if self.__folder_path is None:
            self.__folder_path = self._base_folder
        return self.__folder_path

    @property
    def _file_name(self):
        if self.__file_name is None:
            self.__file_name = self._movie_basename
        return self.__file_name

    @property
    def _file_extension(self):
        if self.__file_extension is None:
            naming_template = os.getenv("ANIWORLD_NAMING_TEMPLATE", NAMING_TEMPLATE)
            try:
                file_part = naming_template.split("/")[-1]
                if "." in file_part:
                    ext = file_part.rsplit(".", 1)[-1]
                    self.__file_extension = ext if ext else "mkv"
                else:
                    self.__file_extension = "mkv"
            except IndexError:
                self.__file_extension = "mkv"
        return self.__file_extension

    @property
    def _episode_path(self):
        if self.__episode_path is None:
            self.__episode_path = (
                self._folder_path / f"{self._file_name}.{self._file_extension}"
            )
        return self.__episode_path

    # END

    @property
    def is_downloaded(self):
        if self.__is_downloaded is None:
            self.__is_downloaded = check_downloaded(self._episode_path)
        return self.__is_downloaded

    @property
    def _html(self):
        if self.__html is None:
            if not self.url:
                raise ValueError("Episode URL is missing for HTML fetch.")
            logger.debug(f"fetching ({self.url})...")
            resp = GLOBAL_SESSION.get(
                self.url,
                headers={
                    "Accept-Encoding": "gzip, deflate",
                    "Referer": f"{FILMO_BASE}/",
                },
            )
            self.__html = resp.text
        return self.__html

    @property
    def _meta_labels(self):
        """Text of the meta strip under the synopsis.

        Order is genres, IMDb score, runtime, year, age rating - but the
        captions are localised and optional, so callers match on shape
        rather than position.
        """
        if self.__meta_labels is None:
            self.__meta_labels = [
                self.__strip_tags(span)
                for span in re.findall(
                    r'<span[^>]*ft-meta-label[^>]*>(.*?)</span>', self._html, re.DOTALL
                )
            ]
        return self.__meta_labels

    @property
    def _entry_blocks(self):
        """(heading_html, description_html) pairs of the detail definition list."""
        if self.__entry_blocks is None:
            self.__entry_blocks = re.findall(
                r'<dt class="entry-title[^"]*">(.*?)</dt>\s*'
                r'<dd class="entry-description">(.*?)</dd>',
                self._html,
                re.DOTALL,
            )
        return self.__entry_blocks

    @property
    def _csrf_token(self):
        if self.__csrf_token is None:
            match = re.search(
                r'<meta name="csrf-token" content="([^"]+)"', self._html
            )
            if not match:
                raise ValueError(f"No CSRF token found on Filmo page: {self.url}")
            self.__csrf_token = match.group(1)
        return self.__csrf_token

    # -----------------------------
    # PRIVATE EXTRACTION FUNCTIONS
    # -----------------------------

    def __extract_title_de(self):
        match = re.search(r"<h1[^>]*>(.*?)</h1>", self._html, re.DOTALL)
        if match:
            self.__title_de = self.__strip_tags(match.group(1))
            return
        match = re.search(r'<meta property="og:title" content="([^"]+)"', self._html)
        if match:
            # "The Dark Knight Rises jetzt kostenlos streamen – Filmo"
            title = html_module.unescape(match.group(1))
            self.__title_de = re.split(r"\s+jetzt\s|\s+–\s+Filmo", title)[0].strip()

    def __extract_release_year(self):
        for label in self._meta_labels:
            match = re.fullmatch(r"((?:19|20)\d{2})", label.strip())
            if match:
                self.__release_year = int(match.group(1))
                return

    def __extract_runtime_min(self):
        # "2 h 45 min", "108 min"
        for label in self._meta_labels:
            match = re.fullmatch(
                r"(?:(\d+)\s*h)?\s*(?:(\d+)\s*min)?", label.strip(), re.IGNORECASE
            )
            if not match or not any(match.groups()):
                continue
            total = int(match.group(1) or 0) * 60 + int(match.group(2) or 0)
            if total:
                self.__runtime_min = total
                return

    def __extract_genres(self):
        # Genre links also appear in the header nav and footer, so keep the
        # first occurrence of each and drop the repeats.
        seen = {}
        for name in re.findall(
            r'href="https://filmo\.to/genres/[^"]+"[^>]*>(.*?)</a>', self._html
        ):
            cleaned = self.__strip_tags(name)
            if cleaned:
                seen.setdefault(cleaned, None)
        self.__genres = list(seen)

    def __extract_description(self):
        match = re.search(
            r'<p class="[^"]*movie-detail-synopsis[^"]*">(.*?)</p>',
            self._html,
            re.DOTALL,
        )
        if match:
            self.__description = self.__strip_tags(match.group(1))
            return
        match = re.search(
            r'<meta name="description" content="([^"]*)"', self._html
        )
        if match:
            self.__description = html_module.unescape(match.group(1)).strip()

    def __extract_image_url(self):
        match = re.search(r'<meta property="og:image" content="([^"]+)"', self._html)
        if match:
            self.__image_url = match.group(1).strip()

    def __people_blocks(self):
        """Credit blocks as (is_cast, names). Headings are localised, so the
        directors row is told apart from the cast row by its heading class."""
        blocks = []
        for heading, description in self._entry_blocks:
            names = [
                self.__strip_tags(name)
                for name in re.findall(
                    r'href="https://filmo\.to/people/[^"]+"[^>]*>(.*?)</a>',
                    description,
                    re.DOTALL,
                )
            ]
            names = [name for name in names if name]
            if names:
                blocks.append(("section-headline" in heading, names))
        return blocks

    def __extract_director(self):
        blocks = self.__people_blocks()
        directors = [names for is_cast, names in blocks if not is_cast]
        if directors:
            self.__director = directors[0][0]
        elif blocks:
            self.__director = blocks[0][1][0]

    def __extract_actors(self):
        blocks = self.__people_blocks()
        cast = [names for is_cast, names in blocks if is_cast]
        if cast:
            self.__actors = cast[0]
        else:
            self.__actors = blocks[1][1] if len(blocks) > 1 else []

    def __extract_imdb_rating(self):
        for label in self._meta_labels:
            match = re.search(r"IMDb\s*([\d.]+)\s*/\s*10", label)
            if match:
                self.__imdb_rating = float(match.group(1))
                return

    def __extract_age_rating(self):
        # "12+", "Ab 12", "FSK 16", "R"
        for label in self._meta_labels:
            text = label.strip()
            if re.fullmatch(r"(?:\d{1,2}\+|(?:Ab|FSK)\s*\d{1,2}|[A-Z]{1,5}-?\d*)", text):
                self.__age_rating = text
                return

    def __extract_provider_data(self):
        """Collect the encrypted chip payloads, grouped by audio language.

        Layout is one ``provider-row`` per language, each holding one chip per
        mirror. Several chips can share a hoster (different rips), so the one
        the site auto-selects wins and the first chip is the fallback.
        """
        data = {}

        for row in re.split(r'<div class="provider-row"', self._html)[1:]:
            audio = self.__row_audio(row)
            if audio is None:
                continue

            providers = data.setdefault((audio, Subtitles.NONE), {})

            for chip in re.findall(r"<div[^>]*data-provider-chip[^>]*>", row):
                label = re.search(r'aria-label="([^"]*)"', chip)
                payload = re.search(r'data-p="([^"]+)"', chip)
                if not label or not payload:
                    continue

                provider = host_to_provider(label.group(1))
                if not provider:
                    continue

                preferred = "data-auto-select" in chip
                if provider in providers and not preferred:
                    continue
                providers[provider] = payload.group(1)

        data = {key: value for key, value in data.items() if value}
        if not data:
            return None

        return ProviderData(data)

    def __row_audio(self, row_html):
        flag = re.search(r'class="fi fi-([a-z]{2})"', row_html)
        if flag:
            audio = _FLAG_AUDIO.get(flag.group(1))
            if audio:
                return audio

        label = re.search(r'provider-row__lang">([^<]+)</span>', row_html)
        if label:
            return _LABEL_AUDIO.get(label.group(1).strip().lower())

        return None

    def __mint_player_url(self, payload):
        """Exchange an encrypted chip payload for a short-lived player URL."""
        resp = GLOBAL_SESSION.post(
            f"{FILMO_BASE}/n",
            json={"p": payload},
            headers={
                "X-CSRF-TOKEN": self._csrf_token,
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json",
                "Referer": self.url,
            },
            timeout=15,
        )
        resp.raise_for_status()

        try:
            token = (resp.json() or {}).get("x")
        except ValueError:
            token = None

        if not token:
            raise ValueError(f"Filmo did not return a player token for {self.url}")

        return f"{FILMO_BASE}/n/{quote(str(token), safe='')}"

    def _normalize_language(self, language):
        text = str(language or "").strip().lower()
        if text in {"english", "englisch", "english dub"}:
            return "English Dub"
        if text in {"japanese", "japanisch", "japanese dub"}:
            return "Japanese Dub"
        return "German Dub"

    def _language_key(self, language=None):
        """Resolve a language label to an available (Audio, Subtitles) key."""
        provider_data = self.provider_data
        if not isinstance(provider_data, ProviderData):
            return None

        wanted = self._normalize_language(language or self.selected_language)
        order = [audio for audio, label in _AUDIO_LABEL.items() if label == wanted]
        # Fall back to German, then anything the page actually offers.
        order += [Audio.GERMAN, Audio.ENGLISH, Audio.JAPANESE]

        for audio in order:
            if provider_data.get((audio, Subtitles.NONE)):
                return (audio, Subtitles.NONE)
        return None

    def provider_link(self, language=None, provider=None):
        if provider is None:
            provider = self.selected_provider

        key = self._language_key(language)
        if key is None:
            return None

        provider_dict = self.provider_data.get(key)
        if not provider_dict:
            return None

        name = str(provider).strip()
        return provider_dict.get(name) or provider_dict.get(name.upper())

    def available_languages(self):
        provider_data = self.provider_data
        if not isinstance(provider_data, ProviderData):
            return tuple()
        return tuple(
            _AUDIO_LABEL[audio]
            for audio in (Audio.GERMAN, Audio.ENGLISH, Audio.JAPANESE)
            if provider_data.get((audio, Subtitles.NONE))
        )

    def available_providers(self, language=None):
        key = self._language_key(language)
        if key is None:
            return tuple()
        provider_dict = self.provider_data.get(key)
        return tuple(provider_dict.keys()) if provider_dict else tuple()

    def provider_attempt_order(self):
        return build_provider_attempt_order(
            self.selected_provider,
            self.available_providers(),
        )

    # -----------------------------
    # PUBLIC METHODS
    # -----------------------------

    download = episode_download
    watch = episode_watch
    syncplay = episode_syncplay


if __name__ == "__main__":
    episode = FilmoEpisode("https://filmo.to/movies/the-dark-knight-rises")
    print(episode.url)
    print(episode.title_de)
    print(episode.release_year)
    print(episode.runtime_min)
    print(episode.genres)
    print(episode.description)
    print(episode.image_url)
    print(episode.director)
    print(episode.actors)
    print(episode.imdb_rating)
    print(episode.age_rating)
    print(episode.available_languages())
    print(episode.provider_data)
