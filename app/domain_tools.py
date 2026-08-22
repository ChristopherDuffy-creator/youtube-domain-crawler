from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import tldextract

_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# YouTube descriptions often contain genuine bare domains without http:// or
# www., so keep them. We filter ambiguous bare tokens after parsing instead of
# requiring a scheme.
URL_RE = re.compile(
    r"(?i)(?<![@\w])(?:https?://|www\.)[^\s<>\[\]{}\"']+"
    r"|(?<![@\w])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?:/[^\s<>\[\]{}\"']*)?"
)

TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"
EXPLICIT_URL_RE = re.compile(r"(?i)^(?:https?://|www\.)")

# Bare filename/prose collisions that are common in technical video text. An
# explicit URL is always allowed, and a bare token with a path is strong URL
# evidence, so only the ambiguous no-path form is rejected.
_BARE_FILELIKE_SUFFIXES = {
    "py", "js", "ts", "sh", "rb", "go", "java", "c", "h", "cpp",
    "cs", "rs", "php", "sql", "md", "txt", "json", "xml", "csv",
    "yml", "yaml",
}

EXCLUDED_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "google.com",
    "googleusercontent.com",
    "g.co",
    "facebook.com",
    "fb.com",
    "fb.me",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "linkedin.com",
    "pinterest.com",
    "reddit.com",
    "snapchat.com",
    "threads.net",
    "discord.com",
    "discord.gg",
    "t.me",
    "telegram.me",
    "whatsapp.com",
    "wa.me",
    "patreon.com",
    "ko-fi.com",
    "buymeacoffee.com",
    "amazon.com",
    "amazon.co.uk",
    "amzn.to",
    "ebay.com",
    "etsy.com",
    "apple.com",
    "play.google.com",
    "spotify.com",
    "soundcloud.com",
    "bandcamp.com",
    "github.com",
    "wikipedia.org",
    "linktr.ee",
    "beacons.ai",
    "bio.link",
    "allmylinks.com",
    "bit.ly",
    "tinyurl.com",
    "goo.gl",
    "ow.ly",
    "buff.ly",
    "rebrand.ly",
    "shorturl.at",
    "geni.us",
    "smarturl.it",
    "clickbank.net",
    "jvzoo.com",
    "shareasale.com",
    "awin1.com",
}

CTA_PHRASES = (
    "visit",
    "go to",
    "click",
    "download",
    "sign up",
    "signup",
    "learn more",
    "buy",
    "shop",
    "get the",
    "get your",
    "book",
    "join",
    "course",
    "website",
    "more information",
    "free guide",
    "resources",
    "full tutorial",
)


@dataclass(frozen=True)
class ExtractedLink:
    domain: str
    suffix: str
    raw_url: str
    normalized_url: str
    position: float
    context: str
    has_cta: bool
    clickable: bool


def registrable_domain(hostname: str) -> tuple[str, str] | None:
    host = hostname.strip().strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    extracted = _extractor(host)
    if not extracted.domain or not extracted.suffix:
        return None
    return f"{extracted.domain}.{extracted.suffix}", extracted.suffix


def is_excluded(domain: str) -> str | None:
    for blocked in EXCLUDED_DOMAINS:
        if domain == blocked or domain.endswith("." + blocked):
            return "social/platform/redirector"
    return None


def _normalise_url(raw: str) -> tuple[str, str, str] | None:
    cleaned = html.unescape(raw).strip().rstrip(TRAILING_PUNCTUATION)
    candidate = cleaned if re.match(r"(?i)^https?://", cleaned) else "https://" + cleaned
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    host = parsed.hostname or ""
    result = registrable_domain(host)
    if not result:
        return None
    domain, suffix = result
    if is_excluded(domain):
        return None
    normalized_host = host.lower().strip(".")
    if normalized_host.startswith("www."):
        normalized_host = normalized_host[4:]
    normalized = urlunsplit(("https", normalized_host, parsed.path or "", parsed.query or "", ""))
    return domain, suffix, normalized


def _plausible_youtube_raw_link(raw: str, domain: str, suffix: str) -> bool:
    """Keep genuine bare domains while rejecting obvious prose/file collisions."""
    cleaned = html.unescape(raw).strip().rstrip(TRAILING_PUNCTUATION)
    if EXPLICIT_URL_RE.match(cleaned):
        return True

    registrant = domain[: -(len(suffix) + 1)] if suffix else domain
    has_path = "/" in cleaned

    # High-volume false positives seen in the live index: B.Tech, 3.how,
    # 5.how, m.ch. Explicit forms such as https://b.tech remain valid.
    if len(registrant) < 2 or registrant.isdigit():
        return False

    # manage.py-style filenames should not become opportunities merely because
    # the suffix is registrable. A path makes the token URL-like enough to keep.
    if suffix.lower() in _BARE_FILELIKE_SUFFIXES and not has_path:
        return False

    return True


def is_plausible_youtube_link(raw: str) -> bool:
    """Validate a raw/stored YouTube link token using the same extraction rules."""
    normalized = _normalise_url(raw)
    if not normalized:
        return False
    domain, suffix, _ = normalized
    return _plausible_youtube_raw_link(raw, domain, suffix)


def extract_links(description: str) -> list[ExtractedLink]:
    if not description:
        return []

    results: list[ExtractedLink] = []
    seen: set[tuple[str, str]] = set()
    length = max(len(description), 1)

    for match in URL_RE.finditer(description):
        raw = match.group(0).rstrip(TRAILING_PUNCTUATION)
        normalized = _normalise_url(raw)
        if not normalized:
            continue
        domain, suffix, normalized_url = normalized
        if not _plausible_youtube_raw_link(raw, domain, suffix):
            continue
        key = (domain, normalized_url)
        if key in seen:
            continue
        seen.add(key)

        start = max(0, match.start() - 120)
        end = min(len(description), match.end() + 120)
        context = " ".join(description[start:end].split())
        context_lower = context.lower()
        results.append(
            ExtractedLink(
                domain=domain,
                suffix=suffix,
                raw_url=raw,
                normalized_url=normalized_url,
                position=round(match.start() / length, 4),
                context=context,
                has_cta=any(phrase in context_lower for phrase in CTA_PHRASES),
                clickable=bool(EXPLICIT_URL_RE.match(raw)),
            )
        )
    return results


def extract_domain_names(text: str) -> list[str]:
    """Extract valid, non-platform domains from dropped-domain text/CSV."""
    names: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text or ""):
        normalized = _normalise_url(match.group(0))
        if not normalized:
            continue
        domain = normalized[0]
        if domain not in seen:
            seen.add(domain)
            names.append(domain)
    return names
