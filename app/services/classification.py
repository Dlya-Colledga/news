import re
from dataclasses import dataclass, field

EVENT_RULES = [
    ("cancellation", r"\b(cancel(?:led|ed|lation)|not renewed)\b"),
    ("delay", r"\b(delay\w*|postpone\w*|push(?:ed)? back)\b"),
    ("trailer", r"\b(trailer|teaser|preview video)\b"),
    ("release_date", r"\b(release date|premiere date|premieres? on|debuts? on|sets? .*date)\b"),
    ("casting", r"\b(cast(?:ing)?|joins|starring|stars as)\b"),
    ("award", r"\b(award|oscar|emmy|wins|golden globe)\w*\b"),
    ("financial", r"\b(box office|earnings|revenue|million|billion)\b"),
    ("production", r"\b(filming|production|shooting)\b"),
    ("announcement", r"\b(announc\w*|confirm\w*|renew\w*|greenlit|new season)\b"),
    ("premiere", r"\b(premiere|released|now streaming)\b"),
]
EVENT_TYPES = tuple(k for k, _ in EVENT_RULES) + ("other",)


@dataclass
class Classification:
    event_type: str = "other"
    formats: list = field(default_factory=list)
    production_types: list = field(default_factory=list)
    countries: list = field(default_factory=list)
    cultural_categories: list = field(default_factory=list)
    categories: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def classify(title, tags=(), source_key="", excerpt=""):
    text = " ".join([title, *tags]).lower()
    c = Classification()
    for kind, pattern in EVENT_RULES:
        if re.search(pattern, title.lower()):
            c.event_type = kind
            break
    if re.search(r"\b(film|movie|documentary|cinema)\b", text):
        c.formats.append("film")
    if re.search(r"\b(series|season|television|tv|episode)\b", text):
        c.formats.append("series")
    if source_key == "anime_corner" or re.search(r"\banime\b", text):
        c.cultural_categories.append("anime")
        c.production_types.append("animation")
        c.countries.append("JP")
    if "donghua" in text:
        c.cultural_categories.append("donghua")
        c.production_types.append("animation")
        c.countries.append("CN")
    if source_key == "dramabeans" or re.search(r"\b(k-drama|j-drama|c-drama|korean drama)\b", text):
        c.cultural_categories.append("drama")
        c.production_types.append("live_action")
        if source_key == "dramabeans" or "korean" in text:
            c.countries.append("KR")
    if re.search(r"\b(animated|animation|cartoon)\b", text):
        c.production_types.append("animation")
    if "film" in c.formats:
        c.categories.append("movies")
    if "series" in c.formats:
        c.categories.append("series")
    if "anime" in c.cultural_categories:
        c.categories.append("anime")
    if "animation" in c.production_types:
        c.categories.append("animation")
    if "drama" in c.cultural_categories:
        c.categories.append("dramas")
    if c.event_type in {"award", "financial", "casting", "production"} or not c.categories:
        c.categories.append("industry")
    for attr in ("categories", "countries", "production_types"):
        setattr(c, attr, sorted(set(getattr(c, attr))))
    fact_text = (title + " " + excerpt).lower()
    season = re.search(r"\bseason\s+(\d+)\b", fact_text)
    if season:
        c.facts["season"] = season[1]
        renewal = (
            re.search(r"\b(renewed|renewal|greenlit)\b", title.lower())
            or re.search(
                r"\bseason\s+\d+\s+(?:officially\s+)?(?:confirmed|announced|ordered)\b", title.lower()
            )
            or re.search(r"\bannounces?\b.*\bseason\s+\d+[.!?]?$", title.lower())
        )
        if c.event_type == "announcement" and renewal:
            c.facts["announcement_subject"] = "season_renewal"
    if re.search(r"\b(rumou?r|unconfirmed|reportedly|might|could|in talks|not confirmed)\b", fact_text):
        c.facts["certainty"] = "unconfirmed"
    dates = re.findall(
        r"\b\d{4}-\d{2}-\d{2}\b|\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:,?\s+\d{4})?\b",
        fact_text,
    )
    if dates:
        c.facts["dates"] = sorted(set(dates))
    regions = re.findall(r"\b(?:japan|korea|china|north america|united states|uk|worldwide)\b", fact_text)
    if regions:
        c.facts["regions"] = sorted(set(regions))
    return c
