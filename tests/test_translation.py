from sqlalchemy import select
from app.db.models import NewsTranslation, GlossaryEntry, TranslationJob
from app.services.translation import TranslationService, ArgosEngine, MissingModel, enqueue_translations
from app.services.editorial import edit_translation


class FakeEngine:
    def path(self, a, b):
        return [a, b]

    def translate(self, text, a, b):
        return f"[{b}] {text}"


class NoModels(FakeEngine):
    def path(self, a, b):
        raise MissingModel("No installed route")


def test_create_persist_and_idempotent(db, sessions, published):
    t = next(t for t in published.translations if t.language == "ky")
    t.manual_edit = False
    t.status = "pending"
    t.title = t.summary = t.body = ""
    result = TranslationService(FakeEngine()).localize(db, published, "ky")
    db.commit()
    assert result.status == "ready" and result.title.startswith("[ky]")
    with sessions() as other:
        saved = other.scalar(select(NewsTranslation).where(NewsTranslation.id == t.id))
        assert saved.body == result.body and saved.engine == "argos:en→ky"
    assert TranslationService(NoModels()).localize(db, published, "ky").status == "ready"


def test_missing_model_is_not_english_fallback(db, published):
    t = next(t for t in published.translations if t.language == "ky")
    t.manual_edit = False
    t.status = "pending"
    t.title = t.body = t.summary = ""
    result = TranslationService(NoModels()).localize(db, published, "ky")
    assert result.status == "missing_model" and result.title == "" and result.body == ""


def test_memory_error_and_manual_protection(db, published):
    class Broken(FakeEngine):
        def translate(self, *args):
            raise MemoryError("Not enough RAM")

    t = next(t for t in published.translations if t.language == "ru")
    original = t.title
    assert TranslationService(Broken()).localize(db, published, "ru").title == original
    t.manual_edit = False
    t.status = "pending"
    assert TranslationService(Broken()).localize(db, published, "ru").status == "failed"


def test_supported_pivot_and_missing(monkeypatch):
    engine = ArgosEngine()
    monkeypatch.setattr(engine, "installed_pairs", lambda: [("en", "ru"), ("ru", "ky")])
    assert engine.path("en", "ky") == ["en", "ru", "ky"]
    import pytest

    with pytest.raises(MissingModel):
        engine.path("ky", "en")


def test_glossary_preserves_names():
    glossary = [GlossaryEntry(term="Wednesday", language="ru", replacement="Уэнсдей")]
    text = TranslationService(FakeEngine()).translate_text("Wednesday has a trailer", "en", "ru", glossary)
    assert text.startswith("Уэнсдей") and "Wednesday" not in text


def test_canonical_edit_invalidates_but_preserves_manual(db, published, admin):
    original = next(t for t in published.translations if t.language == "ky").body
    edit_translation(
        db, published.id, "en", "Revised fixture", "Revised summary", "Revised original body", True, admin.id
    )
    db.commit()
    ky = next(t for t in published.translations if t.language == "ky")
    assert ky.body == original and ky.status == "needs_review" and ky.manual_edit
    assert TranslationService(FakeEngine()).localize(db, published, "ky").body == original


def test_queue_idempotent(db, published):
    for t in published.translations:
        if t.language != "en":
            t.manual_edit = False
            t.status = "pending"
    enqueue_translations(db, published)
    db.flush()
    enqueue_translations(db, published)
    db.commit()
    assert len(db.scalars(select(TranslationJob)).all()) == 2


def test_glossary_word_boundaries_and_spacing():
    class Stripping(FakeEngine):
        def translate(self, text, a, b):
            return text.strip().upper()

    glossary = [GlossaryEntry(term="It", language="ru", replacement="Оно")]
    result = TranslationService(Stripping()).translate_text("It returns with a trailer", "en", "ru", glossary)
    assert result == "Оно RETURNS WITH A TRAILER"


def test_inline_segments_share_context_without_sending_html():
    from app.services.article_html import dom

    calls = []

    class ContextEngine:
        def translate(self, text, source, target):
            calls.append(text)
            assert "<" not in text and "https://" not in text
            assert "new " in text and "season" in text and "announced" in text
            return "⟦0⟧Анонсирован ⟦/0⟧⟦1⟧новый сезон⟦/1⟧⟦2⟧.⟦/2⟧"

    html = TranslationService(ContextEngine()).translate_html(
        "<p>A new <strong>season</strong> is announced.</p>", "en", "ru"
    )
    assert len(calls) == 1
    assert dom(html).p.get_text() == "Анонсирован новый сезон."
    assert dom(html).strong.get_text() == "новый сезон"


def test_damaged_context_boundaries_fall_back_without_dom_corruption():
    from app.services.article_html import dom

    class Damaged:
        def translate(self, text, source, target):
            if "⟦" in text:
                return "⟦1⟧Reordered⟦/1⟧⟦0⟧Incorrect⟦/0⟧"
            return text.upper()

    html = TranslationService(Damaged()).translate_html("<p>New <b>season</b></p>", "en", "ru")
    assert dom(html).p.get_text() == "NEW SEASON"
    assert dom(html).b.get_text() == "SEASON"
    assert "⟦" not in html and "Incorrect" not in html


def test_encoded_publisher_text_is_decoded_before_translation():
    calls = []

    class Recording(FakeEngine):
        def translate(self, text, a, b):
            calls.append(text)
            return text

    result = TranslationService(Recording()).translate_text(
        "Mark &amp;amp; Michael on &amp;#8216;NCIS&amp;#8217;: 25% in 2026", "en", "ru"
    )
    assert result == "Mark & Michael on ‘NCIS’: 25% in 2026"
    assert calls == [result]


def test_argos_receives_no_artificial_segment_tokens():
    calls = []

    class Recording(ArgosEngine):
        def translate(self, text, a, b):
            assert "⟦" not in text
            calls.append(text)
            return text.upper()

    html = TranslationService(Recording()).translate_html("<p>New <b>season</b>.</p>", "en", "ru")
    assert calls == ["New", "season", "."]
    assert "<b>SEASON</b>" in html


def test_translation_quality_guard_preserves_real_numbers_and_percentages():
    from app.services.text_normalization import translation_defect, model_text

    assert model_text("Gillis ’ “news” &amp; film…") == 'Gillis\' "news" & film...'
    assert translation_defect("News", "Новость <unk>")
    assert translation_defect("News", "Новость & #8216;")
    assert translation_defect("News", "Новость 42%")
    assert translation_defect("A new film", "Это новая история про кино " * 5)
    assert translation_defect("Revenue rose 25% in 2026", "Доход вырос на 25% в 2026 году") is None
    assert translation_defect("Revenue rose 25 percent", "Доход вырос на 25%") is None


def test_publisher_names_are_not_translated(db, published):
    entries = TranslationService().glossary(db, published, "ky")
    source = published.articles[0].source
    assert any(g.term == source.name and g.replacement == source.name for g in entries)


async def test_existing_content_repair_preserves_manual_translation_and_publication(
    db, sessions, published, monkeypatch
):
    from scripts import repair_article_content as repair
    from app.db.models import ArticleAsset

    article = published.articles[0]
    article.featured_image = "https://animecorner.me/cover.jpg"
    article.assets.append(
        ArticleAsset(
            position=0,
            kind="img",
            url=article.featured_image,
            srcset="",
            alt="",
            title="",
            caption="Cover credit",
            credit="",
            permitted=False,
        )
    )
    en = next(t for t in published.translations if t.language == "en")
    en.manual_edit = False
    en.title = "Film &#8216;News&#8217;"
    ru = next(t for t in published.translations if t.language == "ru")
    ru.manual_edit = False
    ru.title = "Old & #8216; text"
    ky = next(t for t in published.translations if t.language == "ky")
    manual_title = ky.title
    db.commit()
    monkeypatch.setattr(repair, "SessionLocal", sessions)
    monkeypatch.setattr(repair, "TranslationService", lambda: TranslationService(FakeEngine()))
    await repair.repair()
    db.expire_all()
    assert en.title == "Film ‘News’"
    assert ru.title == "[ru] Film ‘News’"
    assert ky.title == manual_title and ky.manual_edit
    assert published.status == "PUBLISHED"
    assert published.image_url == article.featured_image
    assert published.image_credit == "Cover credit"
