import re
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.main as main_module
from app.affiliate_links import AFFILIATE_LINKS
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import ContactMessage, EmailSubscriber

SITE_EXPECTATIONS = {
    "craftsheaven.club": ("Crafts Heaven", "crafts"),
    "satvic.yoga": ("Satvic Yoga", "satvic"),
    "teamgerardiperformance.com": ("Team Gerardi Performance", "gerardi"),
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
                ("https://www.amazon.co.uk/", "https://amzn.to/")
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
        assert 'href="/contact"' in response.text
        assert 'href="mailto:info@expandosaurus.com">Contact' not in response.text


def test_satvic_primary_cta_uses_plain_language() -> None:
    client = TestClient(app, base_url="https://satvic.yoga")

    response = client.get("/")

    assert "Explore the essentials" in response.text
    assert "Explore the edit" not in response.text


def test_subscription_is_stored_once_per_site() -> None:
    Base.metadata.create_all(bind=engine)
    email = f"site-test-{uuid4().hex}@example.com"
    client = TestClient(app, base_url="https://craftsheaven.club")

    first = client.post(
        "/subscribe",
        data={"email": email, "consent": "yes", "website": ""},
        follow_redirects=False,
    )
    second = client.post(
        "/subscribe",
        data={"email": email.upper(), "consent": "yes", "website": ""},
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
            "website": "",
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


def test_dashboard_host_does_not_expose_public_redirects() -> None:
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/go/adjustable-dumbbells", follow_redirects=False)

    assert response.status_code == 404
