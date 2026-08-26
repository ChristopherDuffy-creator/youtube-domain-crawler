import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.affiliate_links import AFFILIATE_LINKS
from app.main import app


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
    assert "As an Amazon Associate" in client.get("/affiliate-disclosure").text
    assert "does not currently run an email sign-up" in client.get("/privacy").text

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Disallow: /go/" in robots.text
    assert "https://satvic.yoga/sitemap.xml" in robots.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "https://satvic.yoga/about" in sitemap.text


def test_dashboard_host_does_not_expose_public_redirects() -> None:
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/go/adjustable-dumbbells", follow_redirects=False)

    assert response.status_code == 404
