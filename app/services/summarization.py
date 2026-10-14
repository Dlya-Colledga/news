"""Conservative, attributed briefs from structured facts; RSS prose never becomes public copy."""

from dataclasses import dataclass
import trafilatura


@dataclass
class Draft:
    title: str
    summary: str = ""
    body: str = ""
    status: str = "needs_review"
    reason: str = (
        "Write an original factual brief and verify it against the linked source before publication."
    )


class SummarizationService:
    def draft(self, title, excerpt):
        # A lone feed description is not a verified factual account.
        return Draft(title=title)

    def corroborated_draft(self, event):
        """A narrow supported template; all other cases need original editor-written prose.

        A specific season number, one verified work, matching announcement type and two
        independent publishers are required. Attribution stays explicit; review is mandatory.
        """
        sources = {a.source_id: a.source.name for a in event.articles}
        season = event.facts.get("season")
        if (
            len(sources) < 2
            or len(event.works) != 1
            or not season
            or event.event_type != "announcement"
            or event.facts.get("announcement_subject") != "season_renewal"
            or event.facts.get("certainty")
        ):
            return None
        work = event.works[0].title
        names = " and ".join(sorted(sources.values()))
        return Draft(
            title=f"{work}: season {season} announced",
            summary=f"{names} report that {work} will return for season {season}.",
            body=f"Season {season} of {work} has been announced, according to reports from {names}.\n"
            "The original reports are linked below for further details.",
            reason="An attributed draft was built from matching season-announcement facts. Verify before publishing.",
        )

    def extract_permitted_document(self, html, *, permission_confirmed=False):
        """Only for material the editor is entitled to process; no network crawling."""
        if not permission_confirmed:
            raise PermissionError("Content extraction requires explicit permission")
        return (
            trafilatura.extract(html, include_comments=False, include_tables=False, output_format="txt") or ""
        )
