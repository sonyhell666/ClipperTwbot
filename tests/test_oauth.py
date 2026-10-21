"""Регрессионные тесты OAuth URL и callback-параметров."""

from urllib.parse import parse_qs, urlsplit

from bot.handlers import _oauth_query, _oauth_url


def test_oauth_url_encodes_localhost_redirect_exactly() -> None:
    url = _oauth_url("client-id", "http://localhost", "tg_123_abcd")
    query = parse_qs(urlsplit(url).query)

    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == ["http://localhost"]
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["clips:edit"]
    assert query["state"] == ["tg_123_abcd"]


def test_oauth_url_preserves_registered_path_and_port() -> None:
    url = _oauth_url("id", "http://localhost:8080/callback", "state")
    query = parse_qs(urlsplit(url).query)

    assert query["redirect_uri"] == ["http://localhost:8080/callback"]


def test_oauth_query_reads_full_callback_url() -> None:
    assert _oauth_query("http://localhost/?code=code123&state=tg_123_abcd") == {
        "code": "code123",
        "state": "tg_123_abcd",
    }


def test_oauth_query_reads_error_url() -> None:
    params = _oauth_query(
        "http://localhost/?error=redirect_mismatch&error_description=redirect+does+not+match&state=tg_123_abcd"
    )

    assert params["error"] == "redirect_mismatch"
    assert params["state"] == "tg_123_abcd"
