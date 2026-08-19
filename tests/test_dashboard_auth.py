from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.testclient import TestClient

from app.main import (
    DASHBOARD_SESSION_COOKIE,
    _create_dashboard_session,
    _dashboard_session_valid,
    _dashboard_visit_window,
    _next_link_hunter_slot,
    _safe_next_path,
    app,
    settings,
)


def test_dashboard_session_is_signed_and_expires() -> None:
    token = _create_dashboard_session(expires_at=2_000)

    assert _dashboard_session_valid(token, now=1_999) is True
    assert _dashboard_session_valid(token, now=2_000) is False
    assert _dashboard_session_valid(token[:-1] + ("A" if token[-1] != "A" else "B"), now=1_999) is False


def test_login_and_logout_use_a_secure_http_only_cookie() -> None:
    client = TestClient(app, base_url="https://testserver")

    protected = client.get("/?view=youtube&tier=priority", follow_redirects=False)
    assert protected.status_code == 303
    assert protected.headers["location"].startswith("/login?next=")

    login = client.post(
        "/login",
        data={"username": "admin", "password": settings.dashboard_password, "next": "/"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/"
    assert DASHBOARD_SESSION_COOKIE in login.headers["set-cookie"]
    assert "HttpOnly" in login.headers["set-cookie"]
    assert "SameSite=lax" in login.headers["set-cookie"]

    logout = client.post("/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"] == "/login"
    assert "Max-Age=0" in logout.headers["set-cookie"]


def test_login_rejects_bad_credentials_and_external_redirects() -> None:
    client = TestClient(app, base_url="https://testserver")

    response = client.post(
        "/login",
        data={"username": "admin", "password": "wrong", "next": "https://attacker.test"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "Incorrect username or password" in response.text
    assert _safe_next_path("https://attacker.test") == "/"
    assert _safe_next_path("//attacker.test") == "/"
    assert _safe_next_path("/?view=web&tier=priority") == "/?view=web&tier=priority"


def _request_with_cookie(cookie: str = "") -> Request:
    headers = [(b"cookie", cookie.encode())] if cookie else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "query_string": b"",
            "headers": headers,
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("127.0.0.1", 1234),
        }
    )


def test_visit_window_rolls_forward_only_after_an_idle_gap() -> None:
    now = datetime(2026, 8, 19, 18, 0, tzinfo=UTC)
    now_timestamp = int(now.timestamp())

    first_since, first_baseline, _ = _dashboard_visit_window(_request_with_cookie(), now=now)
    assert first_since == now - timedelta(days=1)
    assert first_baseline == now_timestamp - 24 * 60 * 60

    prior_baseline = now_timestamp - 6 * 60 * 60
    recent_activity = now_timestamp - 10 * 60
    recent_cookie = (
        f"expandosaurus_visit_baseline={prior_baseline}; "
        f"expandosaurus_last_activity={recent_activity}"
    )
    _, retained_baseline, _ = _dashboard_visit_window(
        _request_with_cookie(recent_cookie), now=now
    )
    assert retained_baseline == prior_baseline

    old_activity = now_timestamp - 3 * 60 * 60
    old_cookie = (
        f"expandosaurus_visit_baseline={prior_baseline}; "
        f"expandosaurus_last_activity={old_activity}"
    )
    _, advanced_baseline, _ = _dashboard_visit_window(
        _request_with_cookie(old_cookie), now=now
    )
    assert advanced_baseline == old_activity


def test_next_link_hunter_slot_matches_the_two_hour_schedule() -> None:
    assert _next_link_hunter_slot(datetime(2026, 8, 19, 18, 42, tzinfo=UTC)).strftime(
        "%H:%M"
    ) == "18:43"
    assert _next_link_hunter_slot(datetime(2026, 8, 19, 18, 44, tzinfo=UTC)).strftime(
        "%H:%M"
    ) == "20:43"
