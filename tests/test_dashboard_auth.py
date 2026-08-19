from fastapi.testclient import TestClient

from app.main import (
    DASHBOARD_SESSION_COOKIE,
    _create_dashboard_session,
    _dashboard_session_valid,
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
