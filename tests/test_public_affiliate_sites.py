import re
from pathlib import Path
from time import time
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.main as main_module
from app.affiliate_links import AFFILIATE_LINKS, GOOGLE_ANALYTICS_MEASUREMENT_ID
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import ContactMessage, EmailSubscriber, PilotSiteEvent

SITE_EXPECTATIONS = {
    "craftsheaven.club": ("Crafts Heaven", "crafts"),
    "satvic.yoga": ("Satvic Yoga", "satvic"),
    "teamgerardiperformance.com": ("Team Gerardi Performance", "gerardi"),
}


def protected_form_fields(site_key: str, form_kind: str) -> dict[str, str]:
    return {
        "form_token": main_module._issue_public_form_token(
            site_key,
            form_kind,
            issued_at=int(time()) - main_module._PUBLIC_FORM_MIN_AGE_SECONDS - 1,
        ),
        "form_guard": "ready",
        "website": "",
        "fax_number": "",
    }


def test_each_public_domain_serves_its_amazon_first_guide() -> None:
    for host, (name, _) in SITE_EXPECTATIONS.items():
        client = TestClient(app, base_url=f"https://{host}")

        response = client.get("/")

        assert response.status_code == 200
        assert name in response.text
        assert "As an Amazon Associate we earn from qualifying purchases" in response.text
        assert "betterdailyguide" not in response.text
        assert "checkout-ds24" not in response.text


def test_every_template_recommendation_has_a_host_scoped_redirect() -> None:
    template_directory = Path(__file__).parents[1] / "app" / "templates"

    for host, (_, site_key) in SITE_EXPECTATIONS.items():
        template = (template_directory / f"{site_key}.html").read_text()
        slugs = set(re.findall(r'href="/go/([^"#?]+)"', template))

        assert slugs
        assert slugs <= AFFILIATE_LINKS[site_key].keys()

        client = TestClient(app, base_url=f"https://{host}")
        for slug in slugs:
            response = client.get(f"/go/{slug}", follow_redirects=False)
            assert response.status_code == 302
            assert response.headers["location"].startswith(
                (
                    "https://www.amazon.co.uk/",
                    "https://amzn.to/",
                    "https://www.awin1.com/",
                    "https://trk.udemy.com/",
                )
            )
            assert response.headers["cache-control"] == "no-store"


def test_redirects_are_not_shared_between_sites() -> None:
    client = TestClient(app, base_url="https://craftsheaven.club")

    response = client.get("/go/natural-yoga-mat", follow_redirects=False)

    assert response.status_code == 404


def test_public_information_and_search_engine_files_are_available() -> None:
    client = TestClient(app, base_url="https://satvic.yoga")

    assert client.get("/about").status_code == 200
    assert client.get("/contact").status_code == 200
    assert "info@expandosaurus.com" in client.get("/contact").text
    assert "As an Amazon Associate" in client.get("/affiliate-disclosure").text
    assert "If you join the email list" in client.get("/privacy").text

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /go/" in robots.text
    assert "https://satvic.yoga/sitemap.xml" in robots.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "https://satvic.yoga/about" in sitemap.text
    assert "https://satvic.yoga/contact" in sitemap.text


def test_each_site_has_email_capture_and_working_contact_navigation() -> None:
    for host in SITE_EXPECTATIONS:
        client = TestClient(app, base_url=f"https://{host}")

        response = client.get("/")

        assert 'action="/subscribe"' in response.text
        assert 'name="consent"' in response.text
        assert 'name="form_token"' in response.text
        assert 'name="form_guard"' in response.text
        assert 'name="fax_number"' in response.text
        assert 'data-public-form="subscribe"' in response.text
        assert 'href="/contact"' in response.text
        assert 'href="mailto:info@expandosaurus.com">Contact' not in response.text
        assert '<script src="/static/pilot.js" defer></script>' in response.text


def test_each_site_uses_automatic_google_analytics_on_every_page() -> None:
    for host in SITE_EXPECTATIONS:
        client = TestClient(app, base_url=f"https://{host}")

        for path in ("/", "/about", "/contact", "/privacy"):
            response = client.get(path)
            assert response.status_code == 200
            assert GOOGLE_ANALYTICS_MEASUREMENT_ID in response.text
            assert "googletagmanager.com/gtag/js" in response.text
            assert "window.gtag" in response.text
            assert "data-analytics-settings" not in response.text

        privacy = client.get("/privacy").text
        assert "Google Analytics loads with the site" in privacy
        assert "Advertising signals and personalisation remain disabled" in privacy

    analytics_partial = Path("app/templates/_analytics.html").read_text()
    assert "allow_google_signals: false" in analytics_partial
    assert "allow_ad_personalization_signals: false" in analytics_partial

    pilot_script = TestClient(app).get("/static/pilot.js").text
    assert 'recordGoogleEvent("affiliate_click"' in pilot_script
    assert 'recordGoogleEvent("newsletter_signup")' in pilot_script
    assert 'recordGoogleEvent("contact_submit")' in pilot_script


def test_satvic_primary_cta_uses_plain_language() -> None:
    client = TestClient(app, base_url="https://satvic.yoga")

    response = client.get("/")

    assert "Explore the essentials" in response.text
    assert "Explore the edit" not in response.text


def test_public_sites_use_image_led_cards_and_approved_partner_links() -> None:
    for host in SITE_EXPECTATIONS:
        client = TestClient(app, base_url=f"https://{host}")
        response = client.get("/")

        assert response.status_code == 200
        assert 'class="product-visual"' in response.text
        assert 'href="/static/affiliate-v2.css"' in response.text
        assert "How we choose" in response.text

    crafts = TestClient(app, base_url="https://craftsheaven.club")
    machine_mart = crafts.get("/go/machine-mart", follow_redirects=False)
    assert machine_mart.headers["location"].startswith("https://www.awin1.com/")
    assert "awinmid=3131" in machine_mart.headers["location"]
    assert "awinaffid=3059057" in machine_mart.headers["location"]
    assert crafts.get("/go/tooled-up", follow_redirects=False).headers[
        "location"
    ].startswith("https://www.awin1.com/")
    assert crafts.get("/go/udemy-woodworking", follow_redirects=False).headers[
        "location"
    ].startswith("https://trk.udemy.com/")


def test_subscription_is_stored_once_per_site() -> None:
    Base.metadata.create_all(bind=engine)
    email = f"site-test-{uuid4().hex}@example.com"
    client = TestClient(app, base_url="https://craftsheaven.club")

    first = client.post(
        "/subscribe",
        data={
            "email": email,
            "consent": "yes",
            **protected_form_fields("crafts", "subscribe"),
        },
        follow_redirects=False,
    )
    second = client.post(
        "/subscribe",
        data={
            "email": email.upper(),
            "consent": "yes",
            **protected_form_fields("crafts", "subscribe"),
        },
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert first.headers["location"] == "/?subscribed=1#newsletter"
    assert second.status_code == 303
    with SessionLocal() as db:
        rows = db.scalars(
            select(EmailSubscriber).where(EmailSubscriber.email == email)
        ).all()
        assert len(rows) == 1
        assert rows[0].site_key == "crafts"
        assert rows[0].status == "active"
        db.execute(delete(EmailSubscriber).where(EmailSubscriber.email == email))
        db.commit()


def test_contact_form_stores_the_message(monkeypatch) -> None:
    Base.metadata.create_all(bind=engine)
    email = f"contact-test-{uuid4().hex}@example.com"
    monkeypatch.setattr(main_module, "send_email", lambda *args, **kwargs: "test-id")
    client = TestClient(app, base_url="https://teamgerardiperformance.com")

    response = client.post(
        "/contact",
        data={
            "name": "Website Tester",
            "email": email,
            "message": "Please send more information about the guide.",
            **protected_form_fields("gerardi", "contact"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/contact?sent=true"
    with SessionLocal() as db:
        stored = db.scalar(select(ContactMessage).where(ContactMessage.email == email))
        assert stored is not None
        assert stored.site_key == "gerardi"
        assert stored.status == "new"
        db.execute(delete(ContactMessage).where(ContactMessage.email == email))
        db.commit()


def test_public_form_tokens_require_human_time_and_match_the_site() -> None:
    now = int(time())
    token = main_module._issue_public_form_token(
        "crafts",
        "contact",
        issued_at=now,
    )

    assert not main_module._public_form_token_valid(
        token, "crafts", "contact", now=now
    )
    assert main_module._public_form_token_valid(
        token,
        "crafts",
        "contact",
        now=now + main_module._PUBLIC_FORM_MIN_AGE_SECONDS,
    )
    assert not main_module._public_form_token_valid(
        token,
        "satvic",
        "contact",
        now=now + main_module._PUBLIC_FORM_MIN_AGE_SECONDS,
    )
    assert not main_module._public_form_token_valid(
        token,
        "crafts",
        "contact",
        now=now + main_module._PUBLIC_FORM_MAX_AGE_SECONDS + 1,
    )


def test_promotional_contact_bot_is_silently_discarded(monkeypatch) -> None:
    Base.metadata.create_all(bind=engine)
    email = f"seo-bot-{uuid4().hex}@example.com"
    deliveries: list[str] = []
    monkeypatch.setattr(
        main_module,
        "send_email",
        lambda *args, **kwargs: deliveries.append("sent"),
    )
    client = TestClient(app, base_url="https://teamgerardiperformance.com")

    response = client.post(
        "/contact",
        data={
            "name": "SEO Sales Bot",
            "email": email,
            "message": (
                "We provide SEO, AEO and GEO services to rank higher on Google and "
                "AI-powered search. May I send a quote and price list?"
            ),
            **protected_form_fields("gerardi", "contact"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/contact?sent=true"
    assert deliveries == []
    with SessionLocal() as db:
        stored = db.scalar(select(ContactMessage).where(ContactMessage.email == email))
        assert stored is None


def test_automated_subscription_without_browser_guard_is_discarded() -> None:
    Base.metadata.create_all(bind=engine)
    email = f"signup-bot-{uuid4().hex}@example.com"
    client = TestClient(app, base_url="https://satvic.yoga")

    response = client.post(
        "/subscribe",
        data={"email": email, "consent": "yes"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?subscribed=1#newsletter"
    with SessionLocal() as db:
        stored = db.scalar(select(EmailSubscriber).where(EmailSubscriber.email == email))
        assert stored is None


def test_dashboard_host_does_not_expose_public_redirects() -> None:
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/go/adjustable-dumbbells", follow_redirects=False)

    assert response.status_code == 404


def test_anonymous_site_events_are_recorded_once_and_strip_referrer_paths() -> None:
    Base.metadata.create_all(bind=engine)
    session_id = uuid4().hex
    client = TestClient(app, base_url="https://satvic.yoga")
    payload = {
        "event_type": "interest_click",
        "path": "/?private=value",
        "session_id": session_id,
        "referrer": "https://example.com/private/search?q=sensitive",
        "offer_id": "natural-yoga-mat",
    }

    first = client.post("/track/site-event", json=payload)
    second = client.post("/track/site-event", json=payload)

    assert first.status_code == 204
    assert second.status_code == 204
    with SessionLocal() as db:
        rows = db.scalars(
            select(PilotSiteEvent).where(PilotSiteEvent.session_id == session_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].domain == "satvic.yoga"
        assert rows[0].path == "/"
        assert rows[0].referrer == "example.com"
        assert rows[0].offer_id == "natural-yoga-mat"
        db.execute(
            delete(PilotSiteEvent).where(PilotSiteEvent.session_id == session_id)
        )
        db.commit()
