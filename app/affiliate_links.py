from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PublicSite:
    key: str
    name: str
    canonical_url: str
    tagline: str


PUBLIC_SITES = {
    "craftsheaven.club": PublicSite(
        key="crafts",
        name="Crafts Heaven",
        canonical_url="https://craftsheaven.club",
        tagline="Make · Learn · Create",
    ),
    "www.craftsheaven.club": PublicSite(
        key="crafts",
        name="Crafts Heaven",
        canonical_url="https://craftsheaven.club",
        tagline="Make · Learn · Create",
    ),
    "satvic.yoga": PublicSite(
        key="satvic",
        name="Satvic Yoga",
        canonical_url="https://satvic.yoga",
        tagline="Breathe · Move · Rest",
    ),
    "www.satvic.yoga": PublicSite(
        key="satvic",
        name="Satvic Yoga",
        canonical_url="https://satvic.yoga",
        tagline="Breathe · Move · Rest",
    ),
    "teamgerardiperformance.com": PublicSite(
        key="gerardi",
        name="Team Gerardi Performance",
        canonical_url="https://teamgerardiperformance.com",
        tagline="Stronger · Steadier · Consistent",
    ),
    "www.teamgerardiperformance.com": PublicSite(
        key="gerardi",
        name="Team Gerardi Performance",
        canonical_url="https://teamgerardiperformance.com",
        tagline="Stronger · Steadier · Consistent",
    ),
}


# Amazon search-result links are used where the account does not yet have Product
# Advertising API access. This avoids copying live prices, reviews or Amazon-hosted
# product imagery and keeps each recommendation useful when individual stock changes.
AFFILIATE_LINKS: dict[str, dict[str, str]] = {
    "crafts": {
        "tooled-up": (
            "https://www.awin1.com/cread.php?awinmid=496&awinaffid=3059057"
            "&clickref=craftsheaven-home-retailer"
        ),
        "udemy-woodworking": "https://trk.udemy.com/c/7685541/3193860/39854",
        "starter-hand-tools": (
            "https://www.amazon.co.uk/s?k=beginner+woodworking+hand+tool+set"
            "&tag=expandosaurus-21"
        ),
        "woodworking-chisels": "https://www.amazon.co.uk/s?k=woodworking+chisels+set&tag=expandosaurus-21",
        "hand-saw": "https://www.amazon.co.uk/s?k=woodworking+hand+saw+tenon+saw&tag=expandosaurus-21",
        "combination-square": (
            "https://www.amazon.co.uk/s?k=combination+square+woodworking"
            "&tag=expandosaurus-21"
        ),
        "woodworking-clamps": "https://www.amazon.co.uk/s?k=woodworking+clamps+set&tag=expandosaurus-21",
        "beginner-book": (
            "https://www.amazon.co.uk/s?k=beginner+woodworking+book+hand+tools"
            "&tag=expandosaurus-21"
        ),
    },
    "satvic": {
        "udemy-yoga": "https://trk.udemy.com/c/7685541/3193860/39854",
        "natural-yoga-mat": "https://amzn.to/4zJBvfG",
        "restorative-support": "https://amzn.to/4y3jg39",
        "iyengar-blanket": "https://amzn.to/4ccmDMI",
        "restorative-blanket": "https://amzn.to/4gtrD0z",
        "power-of-breathing": "https://amzn.to/4hWbrr7",
        "light-on-pranayama": "https://amzn.to/4gCkgnO",
        "book-of-asanas": "https://amzn.to/4ghS62u",
        "meditation-accessory": "https://amzn.to/4zForI0",
        "incense-holder": "https://amzn.to/3Uk7d2N",
        "incense-set": "https://amzn.to/4c7u08e",
        "backflow-incense": "https://amzn.to/4gTQZGo",
        "meditation-ornament": "https://amzn.to/3Ulw2LN",
    },
    "gerardi": {
        "udemy-fitness": "https://trk.udemy.com/c/7685541/3193860/39854",
        "adjustable-dumbbells": "https://www.amazon.co.uk/s?k=adjustable+dumbbells+set&tag=expandosaurus-21",
        "hex-dumbbells": "https://www.amazon.co.uk/s?k=hex+dumbbell+set+home+gym&tag=expandosaurus-21",
        "adjustable-bench": (
            "https://www.amazon.co.uk/s?k=foldable+adjustable+weight+bench"
            "&tag=expandosaurus-21"
        ),
        "resistance-bands": "https://www.amazon.co.uk/s?k=resistance+bands+set&tag=expandosaurus-21",
        "exercise-mat": "https://www.amazon.co.uk/s?k=exercise+mat+home+workout&tag=expandosaurus-21",
        "workout-book": "https://www.amazon.co.uk/s?k=beginner+dumbbell+workout+book&tag=expandosaurus-21",
    },
}


def public_site_for_host(host: str) -> PublicSite | None:
    return PUBLIC_SITES.get(host.split(":", 1)[0].lower())
