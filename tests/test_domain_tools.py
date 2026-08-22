from app.domain_tools import extract_domain_names, extract_links, registrable_domain
from app.youtube import exact_domain_in_description


def test_extracts_exact_commercial_links_and_context() -> None:
    description = (
        "Download the full course and visit "
        "https://www.CakeDecoratingInstructor.com/classes/start?ref=video. "
        "Follow me at https://instagram.com/example."
    )
    links = extract_links(description)

    assert len(links) == 1
    assert links[0].domain == "cakedecoratinginstructor.com"
    assert links[0].normalized_url == "https://cakedecoratinginstructor.com/classes/start?ref=video"
    assert links[0].has_cta is True
    assert links[0].clickable is True


def test_youtube_links_require_explicit_url_not_bare_prose() -> None:
    description = (
        "B.Tech students can use manage.py and read 3.how examples. "
        "Bare resource andygrabertraining.com/program is not clickable. "
        "Real links: http://example.com/a and www.seconddomain.net/start. "
        "Email info@example.com or use https://bit.ly/abc."
    )
    links = extract_links(description)

    assert [link.domain for link in links] == ["example.com", "seconddomain.net"]
    assert all(link.clickable for link in links)


def test_handles_multilevel_public_suffix() -> None:
    assert registrable_domain("courses.example.co.uk") == ("example.co.uk", "co.uk")


def test_dropped_text_still_accepts_bare_domains_and_deduplicates() -> None:
    text = "example.com\nhttps://example.com/path, seconddomain.net\nexample.com"
    assert extract_domain_names(text) == ["example.com", "seconddomain.net"]


def test_exact_domain_match_has_boundaries() -> None:
    description = "Get it at https://example.com/course"
    assert exact_domain_in_description("example.com", description)
    assert not exact_domain_in_description("ample.com", description)
    assert not exact_domain_in_description("example.co", description)
