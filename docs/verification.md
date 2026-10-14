# Проверка HTML-скрапера и предварительной модерации

Проверено 2026-10-14 в текущем рабочем дереве. Тестовые публикации существуют только в изолированных БД/схемах; права реальным издателям не назначались и статьи не публиковались.

## Выполненные проверки

| Проверка | Результат |
|---|---|
| Полный pytest с `TEST_DATABASE_URL` PostgreSQL | **108 passed**, включая все четыре теста PostgreSQL-конкурентности |
| `ruff check --isolated --select E4,E7,E9,F app tests scripts` | PASS |
| `scripts/check_migrations.py` | Upgrade старой схемы → сохранение тестовых старых текстов → upgrade head/check → downgrade → upgrade head/check; PASS |
| `scripts/check_translation.py` | Установленные Argos EN→RU / EN→KY; перевод HTML, alt/подписей, неизменные DOM и iframe src, сохранение после нового подключения PostgreSQL; PASS |
| `scripts/check_browser.py` | Chromium, готовые EN/RU/KY предпросмотры, богатый редактор, запрет публикации до одобрения, admin approval, изображения/галереи/таблица/iframe, 390 px без горизонтального переполнения; PASS |

Машинные отчёты: [migration-check.json](migration-check.json), [translation-check.json](translation-check.json), [browser-check.json](browser-check.json). Скриншоты: `.local/screenshots/rich-editor-desktop.png`, `rich-preview-mobile.png`, `rich-public-desktop.png`. Браузерные переводы явно синтетические для проверки интерфейса; проверка настоящего Argos вынесена в отдельный скрипт. Все внешние запросы браузерного сценария перехватываются локальными HTTP-фикстурами. Каждый iframe дополнительно прокручивается в видимую область и проверяется загрузка sandbox-документа.

Pytest выдаёт предупреждение Starlette о deprecated httpx TestClient. Оно не подавляется; падений тестов нет. `pytest` не требует внешних новостных сайтов, моделей или каталога Argos. В режиме SQLite четыре теста блокировок PostgreSQL пропускаются; полный указанный результат получен на PostgreSQL без пропусков.

## Матрица требований задания

| Требование | Реализация и непосредственная проверка |
|---|---|
| URL автоматически приходит из RSS | `run_source` → import → processing; `test_rss_to_preview_approval_publication_and_exact_reimport` использует настоящий парсер RSS с локальным HTTP transport |
| Полное содержимое и смысловая структура | `ArticleScraperService`, `rich_article.html`, `test_rich_article_and_order`: H2/H3, несколько абзацев, strong/em, списки, цитаты, таблица, вложение, видео/аудио |
| Четыре адаптера и общий fallback | `SCRAPERS`, GenericArticleScraper; параметризованные `test_specialized_container_wins_over_main`, JSON-LD, trafilatura и восстановление malformed DOM |
| CSS-конфигурация нового источника | Защищённая форма создания источника и policy form; `test_generic_source_can_be_configured_without_code` |
| Изображения, порядок, lazy, srcset/picture, галереи, подписи/авторство | `ArticleAsset`; `test_rich_article_and_order`, `test_metadata_cover_and_placeholder_srcset`; браузер загружает локальные изображения, проверяет DOM и мобильную ширину |
| YouTube, X/Twitter, Instagram/TikTok и расширяемый registry | `PROVIDERS`, `ArticleEmbed`; `test_embeds_are_canonical_and_no_source_scripts`, `test_registry_accepts_new_constrained_widget`; sandbox iframe загружаются в Chromium |
| Запрещённые iframe и ссылки вместо них | Канонические provider URLs, жёсткая проверка путей/доменов, fallback link; `test_iframe_path_allowlist`, hostile-host и srcdoc случаи |
| Полный HTML и локализованные версии в БД | SourceArticle fragment/text/time/version/hash/status/rights; NewsTranslation.content_html/body; media tables; SQL migrations/check и end-to-end persistence assertions |
| Безопасная SSR-вставка | `sanitize_html` на каждом преобразовании и Jinja-фильтр, CSP registry, только собственные scripts; параметризованные XSS/mutation/SVG/scheme tests и SSR malicious-storage test |
| Перевод DOM EN/RU/KY и контекст inline-узлов | `TranslationService.translate_html`; `test_dom_translation_preserves_structure_and_technical_attributes`, `test_inline_segments_share_context_without_sending_html`, повреждённые границы сегментов → проверенный fallback, реальный Argos JSON-отчёт |
| Имена и названия | Glossary, WorkAlias и сохранение названий студий; `test_auto_glossary_protects_work_names`, word boundary/spacing tests |
| Отсутствующая модель не выдаётся за готовый KY | `test_missing_models_stay_processing_and_retry`, missing original→EN model сохраняет извлечённый HTML; существующие pivot/OOM/manual-protection tests |
| Права на текст, цитаты и медиа независимы | `publication_copy`, `factual_brief.extract_facts`, rights snapshots, gate каждого media URL; tests facts mode, media independence, revocation и внедрение нелицензированного URL редактором |
| Нет пустой «готовой» карточки вместо статьи | `test_facts_mode_does_not_mark_headline_link_card_as_ready`; недостаток поддерживаемых фактов без разрешённого фрагмента даёт ошибку обработки |
| Только администратор публикует | Проверка роли в HTTP route и `set_publication`; anonymous/editor tests, CSRF и all-languages publication gate |
| Переходы статусов, rejection/retry | End-to-end pipeline, missing-model retry, reject/reprocess, invalid HTML/empty-data gate, recovery worker; READY только после общего readiness check |
| Атомарное и идемпотентное одобрение | `test_concurrent_approval_is_idempotent`: две заранее закешировавшие старое состояние сессии → один timestamp и один publish audit |
| Предварительный URL/GUID dedup | Existing unique constraints + `test_idempotent_and_exact_identifiers`, parallel imports, end-to-end второй RSS не запрашивает статью |
| Content/event dedup, независимые первоисточники | Content hash + консервативные факты/названия/даты; `test_content_dedup_preserves_published_copy`, paraphrased season, conflicting dates/regions/works/rumours tests |
| Без перезаписи одобренного/ручного текста | Protected statuses и manual_edit, проверка на повторной обработке/переводе; source merge test сравнивает все опубликованные версии до/после; `test_rejection_between_scrape_and_translation_is_not_overwritten` проверяет решение редактора между транзакциями |
| Редакционная очередь и preview | `dashboard.html` показывает превью/источник/дату оригинала/получения/категорию/переводы/дубли; preview использует `news.html`, три языка; браузерная проверка |
| Редактор сохраняет медиа и очищает правки | `editor.js`, HTML fallback без JS, sanitize on save; browser unchanged save сохраняет iframe; edit test сохраняет медиа и удаляет injected script |
| Ограничения доступа не обходятся | `test_robots_disabled_sources_and_http_errors`, `test_redirect_cannot_bypass_article_robots`, paywall fixture; HTTP retry/403/redirect/size/gzip tests |
| Повторная обработка не скачивает сохранённую статью | `test_retry_uses_stored_html_without_redownload` устанавливает HTTP transport, падающий при любом запросе |
| Обновление существующих данных | `check_migrations.py` создаёт старые опубликованные версии, проверяет сохранность текста/ручных флагов, экранирование HTML и переход DISCOVERED; не переносит старое EN-only одобрение |

## Границы проверки и эксплуатации

Live-HTML Variety/Deadline/Anime Corner/Dramabeans не загружался без настроенных разрешений. Адаптеры и safety gates проверены локальными фикстурами, а прежние RSS HTTP-наблюдения находятся в `integration-check.json` и не выдаются за новую проверку HTML. CSS сайта-источника может измениться — для этого предусмотрены настраиваемые селекторы и fallback.

Фактическое изложение основано на консервативных правилах и не генерирует неподтверждённых подробностей. Неподдерживаемая статья без разрешённых фрагментов не попадает в READY. Для расширения покрытия можно добавить factual rule/адаптер или настроить действительно имеющееся разрешение. Полное воспроизведение не ограничено этими factual rules.

Отображение реального внешнего видео/соцпоста зависит от наличия публикации, разрешения автора на embed и ограничений провайдера. Проверены безопасные iframe-компоненты и их SSR-загрузка, а не live-доступность всех провайдеров; рядом остаются ссылки на оригиналы. Для YouTube используется referrer policy, передающий origin, согласно [описанию ошибки 153](https://developers.google.com/youtube/iframe_api_reference#onError). Санитизация использует allowlist API [nh3](https://nh3.readthedocs.io/en/latest/).

Миграции проверены на отдельной временной PostgreSQL-базе и затем применены к локальной базе проекта после резервного копирования; количество прежних событий, оригиналов и переводов сохранено ([local-migration-check.json](local-migration-check.json)). Локальная база не наполнялась фикстурами. Для обновления другой установки: `uv sync --locked --extra translation`, `uv run alembic upgrade head`, затем перезапуск web и worker. Миграция сбрасывает старое одобрение; по умолчанию разрешения на новые способы использования контента не выдаются.

## Переводы и обложки · 14.10.2026

- Полный pytest в отдельной PostgreSQL-базе: 126 passed.
- Повторно подготовлены 12 автоматических RU/KY-версий настоящим Argos; ошибок обработки нет. Ручные версии защищены тестом восстановления.
- Заполнены обложки 101 существующего материала. Для готовых статей повторно проверен OG источника.
- Playwright проверил публичную статью №101 на EN/RU/KY: HTTP 200, отсутствие HTML-сущностей и служебных токенов, загрузка обложки (naturalWidth 1000). При ширине 390px горизонтального переполнения нет.
- Локальные артефакты: `.local/translation-cover-tests.log`, `.local/translation-cover-browser.json`, `.local/screenshots/translation-cover-desktop.png`, `.local/screenshots/translation-cover-mobile.png`.
