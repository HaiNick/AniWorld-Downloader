"""Kinox's verification wall goes through the captcha browser, not a dead end."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aniworld.models.kinox import series
from aniworld.playwright import captcha

REFERER = "https://kinox.to/Stream/Example.html"
REDIRECT = "https://kinox.to/redirect/abc123"
HOSTER = "https://voe.sx/e/video"


def reply(url, status=200, text=""):
    return SimpleNamespace(url=url, status_code=status, text=text)


@pytest.fixture
def kinox(monkeypatch):
    """Fake kinox: the mirror API points at REDIRECT, replies[] answer it in turn."""
    monkeypatch.setattr(
        series,
        "get_html",
        lambda url, **kwargs: json.dumps({"Stream": '<iframe src="/redirect/abc123">'}),
    )
    replies = []
    session = Mock()
    session.get.side_effect = lambda *args, **kwargs: replies.pop(0)
    monkeypatch.setattr(series, "get_session", lambda: session)
    solve = Mock()
    monkeypatch.setattr(captcha, "solve_captcha", solve)
    return SimpleNamespace(replies=replies, session=session, solve=solve)


def test_a_plain_hoster_needs_no_browser(kinox):
    kinox.replies.append(reply(HOSTER))
    assert series._resolve_embed("rel", REFERER) == HOSTER
    kinox.solve.assert_not_called()


@pytest.mark.parametrize(
    "blocked",
    [
        reply(REDIRECT, status=403),
        reply(REDIRECT, text="<title>Verifizierung</title>"),
    ],
)
def test_the_wall_opens_the_captcha_browser(kinox, blocked):
    kinox.replies.append(blocked)
    kinox.solve.return_value = HOSTER
    assert series._resolve_embed("rel", REFERER) == HOSTER
    kinox.solve.assert_called_once_with(REDIRECT)


def test_a_solve_that_stays_on_kinox_retries_with_its_cookies(kinox):
    kinox.replies.extend([reply(REDIRECT, status=403), reply(HOSTER)])
    kinox.solve.return_value = REDIRECT
    assert series._resolve_embed("rel", REFERER) == HOSTER
    assert kinox.session.get.call_count == 2


def test_still_blocked_after_the_solve_fails_with_the_marker(kinox):
    kinox.replies.extend([reply(REDIRECT, status=403), reply(REDIRECT, status=403)])
    kinox.solve.return_value = REDIRECT
    with pytest.raises(RuntimeError, match=r"\[kinox-captcha\]"):
        series._resolve_embed("rel", REFERER)


def test_an_unsolved_captcha_fails_with_the_marker(kinox):
    kinox.replies.append(reply(REDIRECT, status=403))
    kinox.solve.return_value = None
    with pytest.raises(RuntimeError, match=r"\[kinox-captcha\]"):
        series._resolve_embed("rel", REFERER)


def test_no_browser_keeps_the_old_error(kinox):
    kinox.replies.append(reply(REDIRECT, status=403))
    kinox.solve.side_effect = RuntimeError("no display")
    with pytest.raises(RuntimeError, match=r"\[kinox-captcha\]"):
        series._resolve_embed("rel", REFERER)
