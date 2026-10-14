from collections import deque
from bs4 import NavigableString, Comment
from app.services.article_html import dom, sanitize_html, content_text, text_html, validate_html
import logging
import re
from sqlalchemy import select
from langdetect import detect, DetectorFactory, LangDetectException
from app.db.models import NewsTranslation, TranslationJob, GlossaryEntry
from app.services.text_normalization import readable_text, model_text, translation_defect

DetectorFactory.seed = 0
log = logging.getLogger(__name__)
LANGUAGES = ("en", "ru", "ky")


class MissingModel(RuntimeError):
    pass


def detect_language(text, hint=None):
    if hint and hint not in ("unknown", "und"):
        return hint.lower().split("-")[0]
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"


class ArgosEngine:
    # These models are not trained to preserve artificial DOM boundary tokens.
    supports_segment_markers = False

    def installed_pairs(self):
        try:
            import argostranslate.package

            return [(p.from_code, p.to_code) for p in argostranslate.package.get_installed_packages()]
        except ImportError:
            return []

    def path(self, source, target):
        if source == target:
            return [source]
        pairs = self.installed_pairs()
        queue, visited = deque([[source]]), {source}
        while queue:
            route = queue.popleft()
            for a, b in pairs:
                if a != route[-1] or b in visited:
                    continue
                if b == target:
                    return [*route, b]
                visited.add(b)
                queue.append([*route, b])
        raise MissingModel(f"No installed Argos route: {source} → {target}")

    def translate(self, text, source, target):
        route = self.path(source, target)
        if len(route) == 1:
            return text
        import argostranslate.translate

        langs = {lang.code: lang for lang in argostranslate.translate.get_installed_languages()}
        for a, b in zip(route, route[1:]):
            text = model_text(text)
            translator = langs[a].get_translation(langs[b])
            result = readable_text(translator.translate(text))
            defect = translation_defect(text, result)
            if defect:
                for hypothesis in translator.hypotheses(text, num_hypotheses=4):
                    candidate = readable_text(hypothesis.value)
                    if not translation_defect(text, candidate):
                        result, defect = candidate, None
                        break
            if defect:
                raise ValueError(f"Translation needs review: {defect}")
            text = result
        return text


class TranslationService:
    def __init__(self, engine=None):
        self.engine = engine or ArgosEngine()

    def translate_text(self, text, source, target, glossary=()):
        text = readable_text(text)
        if not text:
            return ""
        # Translate spans independently around protected names: no fragile placeholder tokens.
        terms = sorted((g for g in glossary if g.language == target), key=lambda g: len(g.term), reverse=True)
        if not terms:
            return self.engine.translate(text, source, target)
        pattern = re.compile(
            r"(?<!\w)(" + "|".join(re.escape(g.term) for g in terms) + r")(?!\w)", re.IGNORECASE
        )
        replacements = {g.term.casefold(): g.replacement for g in terms}
        pieces = pattern.split(text)

        def translate_span(piece):
            if not piece.strip():
                return piece
            leading = piece[: len(piece) - len(piece.lstrip())]
            trailing = piece[len(piece.rstrip()) :]
            return leading + self.engine.translate(piece.strip(), source, target).strip() + trailing

        return "".join(
            replacements[piece.casefold()] if piece.casefold() in replacements else translate_span(piece)
            for piece in pieces
        )

    def translate_segments(self, values, source, target, glossary=()):
        """Translate inline nodes together as plain text, with verified node boundaries.

        The engine never receives HTML. If it changes, drops or reorders a boundary,
        discard that result and translate the individual nodes. Model output never
        determines the DOM structure or which node receives a segment.
        """
        if source == target:
            return list(values)
        if (
            getattr(self.engine, "supports_segment_markers", True)
            and 1 < len(values) <= 64
            and sum(map(len, values)) <= 4000
            and not any("⟦" in v for v in values)
        ):
            marked = "".join(f"⟦{i}⟧{value}⟦/{i}⟧" for i, value in enumerate(values))
            translated = self.translate_text(marked, source, target, glossary)
            pattern = (
                r"\A\s*"
                + r"\s*".join(
                    re.escape(f"⟦{i}⟧") + "(.*?)" + re.escape(f"⟦/{i}⟧") for i in range(len(values))
                )
                + r"\s*\Z"
            )
            match = re.fullmatch(pattern, translated, re.DOTALL)
            if match and all(
                "⟦" not in result and (not value.strip() or result.strip())
                for value, result in zip(values, match.groups())
            ):
                return list(match.groups())
        results = []
        for value in values:
            if not value.strip():
                results.append(value)
                continue
            leading = value[: len(value) - len(value.lstrip())]
            trailing = value[len(value.rstrip()) :]
            translated = self.translate_text(value.strip(), source, target, glossary)
            if not translated.strip():
                raise ValueError("Translation returned an empty text segment")
            results.append(leading + translated.strip() + trailing)
        return results

    def translate_html(self, html, source, target, glossary=()):
        soup = dom(sanitize_html(html))
        # Text within each semantic block shares sentence context, even when inline
        # emphasis or links split it into several DOM nodes. Technical data stays out.
        blocks = {}
        for node in soup.find_all(string=True):
            if isinstance(node, Comment) or node.find_parent(["iframe", "code", "pre"]):
                continue
            block = node.find_parent(
                [
                    "p",
                    "li",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "figcaption",
                    "td",
                    "th",
                    "blockquote",
                    "div",
                    "section",
                ]
            )
            blocks.setdefault(id(block) if block else id(node), []).append(node)
        for nodes in blocks.values():
            if not any(str(node).strip() for node in nodes):
                continue
            translated = self.translate_segments([str(node) for node in nodes], source, target, glossary)
            for node, value in zip(nodes, translated, strict=True):
                node.replace_with(NavigableString(value))
        for tag in soup.find_all(True):
            for attr in ("alt", "title"):
                if tag.get(attr):
                    tag[attr] = self.translate_text(tag[attr], source, target, glossary)
        result = sanitize_html(str(soup))
        if not validate_html(result):
            raise ValueError("Translated HTML is invalid")
        return result

    def glossary(self, db, event, language):
        entries = list(db.scalars(select(GlossaryEntry).where(GlossaryEntry.language == language)))
        protected = {g.term.casefold() for g in entries}
        for work in event.works:
            preferred = next(
                (a.name for a in work.aliases if a.language == language), work.original_title or work.title
            )
            for name in (work.title, work.original_title, *(a.name for a in work.aliases)):
                if name and name.casefold() not in protected:
                    entries.append(GlossaryEntry(term=name, language=language, replacement=preferred))
                    protected.add(name.casefold())
        for name in (
            *(a.source.name for a in event.articles),
            "Netflix",
            "Disney",
            "Pixar",
            "Warner Bros.",
            "Sony Pictures",
            "Universal Pictures",
            "Marvel Studios",
        ):
            if name.casefold() not in protected:
                entries.append(GlossaryEntry(term=name, language=language, replacement=name))
        return entries

    def localize(self, db, event, language):
        existing = next((t for t in event.translations if t.language == language), None)
        if existing and (existing.manual_edit or existing.status == "ready"):
            return existing
        target = existing or NewsTranslation(event=event, language=language)
        db.add(target)
        canonical = next((t for t in event.translations if t.language == "en" and t.status == "ready"), None)
        if not canonical:
            target.status, target.error = (
                "needs_review",
                "An approved English editorial version is required first",
            )
            return target
        try:
            self.engine.path("en", language)
            glossary = self.glossary(db, event, language)
            target.title, target.summary = [
                self.translate_text(value, "en", language, glossary)
                for value in (canonical.title, canonical.summary)
            ]
            target.content_html = self.translate_html(
                canonical.content_html or text_html(canonical.body), "en", language, glossary
            )
            target.body = content_text(target.content_html)
            target.status, target.engine, target.error = (
                "ready",
                "argos:" + "→".join(self.engine.path("en", language)),
                None,
            )
        except MissingModel as exc:
            target.status, target.error, target.engine = "missing_model", str(exc), "argos"
        except Exception as exc:
            log.exception("Translation failed for event %s / %s", event.id, language)
            target.status, target.error, target.engine = "failed", str(exc)[:1000], "argos"
        from app.services.moderation import refresh_readiness

        refresh_readiness(event)
        return target


def enqueue_translations(db, event):
    for language in ("ru", "ky"):
        t = next((t for t in event.translations if t.language == language), None)
        if t and (t.manual_edit or t.status == "ready"):
            continue
        job = db.scalar(
            select(TranslationJob).where(
                TranslationJob.event_id == event.id, TranslationJob.language == language
            )
        )
        if job:
            job.status, job.error = "pending", None
        else:
            db.add(TranslationJob(event_id=event.id, language=language))
