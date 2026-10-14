from dataclasses import dataclass
from datetime import timedelta, timezone
import logging
import re
import unicodedata
from rapidfuzz import fuzz
from sqlalchemy import select, or_
from sqlalchemy.orm import selectinload
from app.core.config import get_settings
from app.db.models import NewsEvent, SourceArticle, Work

log = logging.getLogger(__name__)


def normalized(text):
    return re.sub(r"[^\w\s]", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass
class Decision:
    outcome: str
    event_id: int | None = None
    score: float = 0
    reason: str = "No matching event"


class DeduplicationService:
    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self._semantic = None

    def matching_works(self, db, title):
        text = f" {normalized(title)} "
        # Alias inventory is local and fetched in two queries, never one per work.
        works = db.scalars(select(Work).options(selectinload(Work.aliases))).all()
        return [
            w
            for w in works
            if any(
                f" {normalized(a)} " in text
                for a in [w.title, w.original_title, *(x.name for x in w.aliases)]
                if len(a) >= 3
            )
        ]

    def exact(self, db, source_id, guid, url):
        return db.scalar(
            select(SourceArticle).where(
                or_(
                    SourceArticle.canonical_url == url,
                    (SourceArticle.source_id == source_id) & (SourceArticle.guid == guid),
                )
            )
        )

    def compare(self, title, classification, published_at, candidate, works=()):
        if classification.event_type != candidate.event_type:
            return Decision("separate", reason="Different event types")
        if (
            abs(
                (
                    aware(published_at)
                    - aware(
                        candidate.articles[0].published_at if candidate.articles else candidate.created_at
                    )
                ).total_seconds()
            )
            > self.settings.dedup_window_days * 86400
        ):
            return Decision("separate", reason="Outside event time window")
        for key in ("season", "dates", "regions", "announcement_subject"):
            a, b = classification.facts.get(key), candidate.facts.get(key)
            if a and b and a != b:
                return Decision("separate", reason=f"Conflicting {key}")
        if classification.facts.get("certainty") != candidate.facts.get("certainty"):
            return Decision("separate", reason="Different certainty of the reported event")
        a, b = normalized(title), normalized(candidate.canonical_title)
        score = fuzz.token_sort_ratio(a, b)
        shared_work = bool(set(w.id for w in works) & set(w.id for w in candidate.works))
        if works and candidate.works and not shared_work:
            return Decision("separate", reason="Different identified works")
        same_title = (
            a == b
            and classification.facts == candidate.facts
            and (shared_work or (not works and not candidate.works and len(a.split()) >= 4))
        )
        # A work name is supporting evidence, never a reason to merge by itself.
        facts_match = bool(classification.facts) and classification.facts == candidate.facts
        if (
            shared_work
            and len(works) == len(candidate.works) == 1
            and facts_match
            and classification.event_type == "announcement"
            and classification.facts.get("season")
            and classification.facts.get("announcement_subject") == "season_renewal"
            and not classification.facts.get("certainty")
        ):
            # A specific season renewal for one verified title is a strong event fingerprint.
            score = max(score, 96)
        if shared_work and facts_match and score >= 65:
            score = min(100, score + 12)
        if self.settings.semantic_enabled and shared_work and score >= 55:
            score = max(score, self.semantic_similarity(title, candidate.canonical_title))
        known_type = classification.event_type != "other"
        if known_type and (
            same_title or (shared_work and facts_match and score >= self.settings.dedup_auto_threshold)
        ):
            return Decision(
                "merge", candidate.id, 100 if same_title else score, "Matching event, work and facts"
            )
        if score >= self.settings.dedup_review_threshold or (shared_work and facts_match):
            return Decision(
                "review", candidate.id, score, "Similar wording; editor must verify event identity"
            )
        return Decision("separate", reason="Insufficient matching facts")

    def semantic_similarity(self, a, b):
        try:
            if not self.settings.semantic_model_path:
                return 0
            if self._semantic is None:
                from sentence_transformers import SentenceTransformer

                self._semantic = SentenceTransformer(self.settings.semantic_model_path, local_files_only=True)
            vectors = self._semantic.encode([a, b], normalize_embeddings=True)
            return float(vectors[0] @ vectors[1]) * 100
        except Exception:
            log.exception("Semantic matching unavailable; lexical matching remains active")
            return 0

    def decide(self, db, article, classification, works, exclude_id=None, processed_only=False):
        candidates = db.scalars(
            select(NewsEvent)
            .where(
                NewsEvent.articles.any(
                    (
                        SourceArticle.published_at
                        >= article.published_at - timedelta(days=self.settings.dedup_window_days)
                    )
                    & (
                        SourceArticle.published_at
                        <= article.published_at + timedelta(days=self.settings.dedup_window_days)
                    )
                ),
                NewsEvent.event_type == classification.event_type,
                NewsEvent.id != exclude_id if exclude_id is not None else True,
                NewsEvent.articles.any(SourceArticle.scrape_status == "ready") if processed_only else True,
            )
            .options(selectinload(NewsEvent.works), selectinload(NewsEvent.articles))
        ).all()
        decisions = [
            self.compare(article.title, classification, article.published_at, c, works) for c in candidates
        ]
        matches = [d for d in decisions if d.outcome == "merge"]
        reviews = [d for d in decisions if d.outcome == "review"]
        if len(matches) > 1:
            d = max(matches, key=lambda x: x.score)
            return Decision("review", d.event_id, d.score, "Multiple plausible events")
        return max(matches or reviews, key=lambda x: x.score, default=Decision("separate"))
