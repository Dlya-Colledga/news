"""Grounded, deterministic news prose from explicit relations in the extracted article.

Only supported relations are rewritten. Unmatched source sentences are never
silently copied as original copy; uncertainty/negation prevents affirmative claims.
"""

from dataclasses import dataclass
import re
from app.services.article_html import dom


@dataclass(frozen=True)
class Fact:
    text: str
    evidence: str


# Capture bounded factual arguments, not long publisher passages.
ARG = r"([^.!?;\n]{2,120}?)"
RULES = (
    (
        rf"^(.{{2,80}}?) (?:is|was|has been) directed by {ARG}[.!]?$",
        lambda m: f"The director of {m[1]} is {m[2]}.",
    ),
    (
        rf"^(.{{2,80}}?) (?:is|was|has been) written by {ARG}[.!]?$",
        lambda m: f"Writing credits for {m[1]} go to {m[2]}.",
    ),
    (
        rf"^(.{{2,80}}?) (?:was|has been) (?:shot|filmed) in {ARG}[.!]?$",
        lambda m: f"Filming for {m[1]} took place in {m[2]}.",
    ),
    (
        rf"^(.{{2,80}}?) (?:will premiere|premieres|will debut|debuts|will be released|is scheduled to premiere|is scheduled for release) on {ARG}[.!]?$",
        lambda m: f"The announced debut for {m[1]} is on {m[2]}.",
    ),
    (rf"^Its release is scheduled for {ARG}[.!]?$", lambda m: f"The release is planned for {m[1]}."),
    (
        rf"^(.{{2,80}}?) (?:stars|features a cast including) {ARG}[.!]?$",
        lambda m: f"The cast of {m[1]} includes {m[2]}.",
    ),
    (
        rf"^(.{{2,80}}?) (?:has joined|joins) the cast of {ARG}[.!]?$",
        lambda m: f"A casting addition for {m[2]} is {m[1]}.",
    ),
    (
        rf"^(.{{2,80}}?) (?:has renewed|renewed) {ARG} for (?:a |its )?(?:season (\d+)|(\d+)(?:st|nd|rd|th) season)[.!]?$",
        lambda m: f"A renewal from {m[1]} brings {m[2]} to season {m[3] or m[4]}.",
    ),
    (
        rf"^(.{{2,80}}?) (?:has released|released|unveiled) (?:the |an? )?(?:official )?(trailer|teaser) for {ARG}[.!]?$",
        lambda m: f"A {m[2]} for {m[3]} is now available from {m[1]}.",
    ),
    (
        rf"^(?:Production|Filming) (?:on|for) {ARG} (?:has begun|has started|began|started)[.!]?$",
        lambda m: f"{m[1]} has entered production.",
    ),
    (
        rf"^(?:Production|Filming) (?:on|for) {ARG} (?:has wrapped|has finished|is complete)[.!]?$",
        lambda m: f"Production work on {m[1]} is finished.",
    ),
)
UNCERTAIN = re.compile(
    r"\b(not|never|may|might|could|reportedly|rumou?r|unconfirmed|in talks|expected)\b", re.I
)


def extract_facts(html):
    facts, seen = [], set()
    # Keep paragraph context; do not join headings, figure captions or unrelated cells.
    for block in dom(html).find_all("p"):
        text = re.sub(r"\s+([.,!?;:])", r"\1", block.get_text(" ", strip=True))
        if block.find_parent(["blockquote", "figure"]):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
            if UNCERTAIN.search(sentence):
                continue
            for pattern, render in RULES:
                match = re.fullmatch(pattern, sentence, re.I)
                if match:
                    rendered = render(match)
                    if rendered.casefold() not in seen:
                        facts.append(Fact(rendered, sentence))
                        seen.add(rendered.casefold())
                    break
    return facts
