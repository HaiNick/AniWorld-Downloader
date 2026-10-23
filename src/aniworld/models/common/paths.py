import os
from collections.abc import Callable
from pathlib import Path


def resolve_download_path(
    selected_path: str | Path | None = None,
    env_key: str | Callable[[], str] | None = None,
) -> Path:
    """Expand model paths beneath home; CLI output paths arrive absolute.

    ``env_key`` names a more specific setting (ANIWORLD_MOVIE_DOWNLOAD_PATH,
    ANIWORLD_SERIES_DOWNLOAD_PATH or ANIWORLD_MANGA_DOWNLOAD_PATH) that wins over
    ANIWORLD_DOWNLOAD_PATH. Pass a
    callable when picking the key needs the page, so an explicit path never
    triggers that fetch.
    """
    value = selected_path
    if not value or (isinstance(value, str) and not value.strip()):
        if callable(env_key):
            # Only worth the page lookup when a split path is actually set
            split_set = any(
                os.getenv(key, "").strip()
                for key in (
                    "ANIWORLD_SERIES_DOWNLOAD_PATH",
                    "ANIWORLD_MOVIE_DOWNLOAD_PATH",
                )
            )
            env_key = env_key() if split_set else None
        value = (
            (os.getenv(env_key, "").strip() if env_key else "")
            or os.getenv("ANIWORLD_DOWNLOAD_PATH", "").strip()
            or "Downloads"
        )
    path = Path(value).expanduser()
    return path if path.is_absolute() else Path.home() / path
