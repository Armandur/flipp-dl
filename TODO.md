# TODO – flipp-dl

Prioriterad lista över förbättringar och nya funktioner. Ordnad ungefär så
att tidigare punkter bör göras före senare, men grupperna kan i praktiken
överlappa.

## Vision

Förvandla `flipp-dl` från ett engångsskript till en självhostad tjänst:

- Körs som en **Docker-container** (eller `docker compose`).
- **Webbgränssnitt** för att logga in, bläddra bland publikationer, välja
  vad som ska bevakas och manuellt trigga nedladdningar.
- **Databas** som håller reda på publikationer, utgåvor, nedladdningsstatus
  och användar-/token-konfiguration.
- **Schemaläggning** som regelbundet pollar Flipp-API:t efter nya utgåvor
  och laddar ner dem automatiskt till en monterad volym.

---

## P0 – Buggar och korrekthet

- [x] **`app.py:131`** – `merger.close` anropas aldrig. Ska vara
      `merger.close()`.
- [x] **`app.py:128`** – Byt `os.mkdir` mot
      `os.makedirs(outputFolder, exist_ok=True)` så att `OUTPUTPATH`
      skapas vid första körningen och `os.path.exists`-checken kan tas bort.
- [x] **`app.py:96`** – `getIssuePDFs` kraschar med `KeyError` om API:t
      svarar med fel (t.ex. ogiltig token). Validera svaret och kasta ett
      tydligt fel.
- [x] **`app.py:104`** – `readPdf` kastar generisk `Exception`. Använd
      `response.raise_for_status()` eller en domänspecifik exception.
- [x] **`app.py:119` vs `app.py:145`** – dubblerad "file exists"-kontroll.
      Ta bort den i `writePdf` eller gör den till den enda.
- [x] Lägg till `if __name__ == "__main__":`-guard så att skriptet kan
      importeras utan att köras.

## P1 – Paketering och beroenden

- [x] Lägg till `requirements.txt` (eller `pyproject.toml`) med minst
      `requests` och `pypdf`.
- [x] Byt ut **PyPDF2** (deprecated) mot **pypdf**. API:t är nästan
      identiskt.
- [x] Ta bort oanvänd import `pprint` (`app.py:2`).

## P2 – Konfiguration och UX

- [x] Läs token från miljövariabel (`FLIPP_TOKEN`) eller från `token`-filen
      som redan finns i `.gitignore` – inte hårdkodad i källkoden.
- [x] Gör kategori-ID konfigurerbart; magisk `52` är nu bara default i
      `flipp_dl/cli.py` och kan överskridas med `--category`.
- [x] Lägg till `argparse` med flaggor som `--token`, `--output`,
      `--category`, `--publication`, `--list-categories`,
      `--list-publications`, `--workers`, `--no-skip-existing`, `-v`.
- [x] Ersätt `print` med `logging` så att nivåer kan styras.

## P3 – Robusthet

- [x] Lägg till `timeout` på alla `requests`-anrop.
- [x] Använd `requests.Session` + `HTTPAdapter` med `Retry` för 5xx och
      anslutningsfel (se `build_session()` i `flipp_dl/api.py`).
- [x] Streama större nedladdningar via `stream=True` + `iter_content`
      i stället för att läsa hela PDF:en i minnet i ett svep.
- [x] Parallellisera sid-nedladdningar med
      `concurrent.futures.ThreadPoolExecutor` (konfigurerbart via
      `--workers`, default 4).

## P4 – Kodkvalitet

- [x] Åtgärda O(n²) i `getPublicationsInfo` – läs `issues` direkt från
      `publication`-objektet i stället för att anropa `getIssuesIds`.
- [x] Konvertera till konsekvent **snake_case** för både funktioner och
      variabler (PEP 8) – gjort i nya `flipp_dl/`-paketet.
- [x] Byt tabbar mot 4 spaces (PEP 8) – gäller nya paketet.
- [x] Lägg till typannoteringar på publika funktioner – gäller nya paketet.
- [x] Låt uppslagsfunktioner returnera `Optional[...]` explicit – ersatt
      av dataklassmetoder på `Publication`.
- [x] Plocka bort den globala `OUTPUTPATH`; skickas nu som argument till
      `IssueDownloader`.
- [x] Lägg till grundläggande tester (`tests/test_storage.py`,
      `tests/test_models.py`, `tests/test_cli.py`). 18 tester i nuläget.
- [x] Sätt upp linting/formatering (`ruff` + `black`) via `pyproject.toml`
      och en GitHub Actions-workflow (`.github/workflows/ci.yml`) som kör
      ruff, black och pytest på py3.9–3.12.

## P5 – Dokumentation

- [x] Skriv om `README.md`:
  - Syfte och disclaimer om Egmonts användarvillkor.
  - Installation (`pip install -r requirements.txt`).
  - Hur man skaffar en token (flödet som commit `6dbfb6d` antyder).
  - CLI-exempel.
  - Docker-/web-instruktioner när de finns på plats.
- [x] Lägg till `LICENSE` (MIT).

---

## P6 – Omstrukturering inför tjänst-läget

Innan Docker/web/schemaläggning tillkommer bör koden brytas isär i
återanvändbara moduler, t.ex.:

```
flipp_dl/
├── __init__.py
├── api.py          # Flipp-API-klient (token, publications, issues, pdfs)
├── downloader.py   # Nedladdning + sammanslagning av PDF:er
├── models.py       # Dataklasser: Publication, Issue, DownloadJob
├── storage.py      # Filnamnsregler, utdatakatalog, safeName
├── db.py           # SQLAlchemy-modeller + migrationer
├── scheduler.py    # Polling och jobb
├── cli.py          # argparse-entrypoint
└── web/
    ├── app.py      # FastAPI/Flask-app
    ├── routes.py
    └── templates/  # Jinja2 eller en SPA-frontend
```

- [x] Flytta `app.py`-logiken in i `flipp_dl/` och behåll `app.py` (eller
      `python -m flipp_dl`) som tunt CLI-entry.
- [x] Definiera tydliga abstraktioner: `FlippClient`, `IssueDownloader`.
      `DownloadRepository` kommer i P7 tillsammans med databasen.

## P7 – Databas

- [x] Välj databas: **SQLite** via SQLAlchemy 2.0 (WAL-mode + foreign
      keys enabled). Schema kan byta till Postgres utan kodändringar.
- [x] Schema implementerat i `flipp_dl/db/models.py`:
  - `publications` (id, custom_code, name, watched, last_polled_at)
  - `publication_categories` (denormaliserat, publication_id, category_id,
    category_name)
  - `issues` (id, publication_id, custom_code, issue_name, issue_date,
    status, discovered_at, downloaded_at, file_path, error_message)
  - `settings` (key, value) – enkelt nyckel-värde-lager
  - `jobs` (id, job_type, payload, status, created_at, started_at,
    finished_at, error_message)
- [x] `flipp_dl/db/session.py` – `make_engine()`, `make_session_factory()`,
      `get_session()`-kontexthanterare; `create_all` vid uppstart.
- [x] `flipp_dl/db/repository.py` – `DownloadRepository` med metoder för
      upsert/query av publications, issues, settings och jobs, plus
      `sync_publications()` som returnerar nyupptäckta utgåvor.
- [x] `IssueDownloader` accepterar valfri `repository=` och uppdaterar
      issue-status (downloading → done | error) under nedladdningen.
- [x] 50 tester (repository, storage, modeller, CLI och web-routes med
      traversal-skydd).
- [x] **Alembic**-migrationer på plats – baseline i
      `flipp_dl/db/migrations/versions/0001_baseline.py`,
      `_ensure_schema()` i `db/session.py` stämplar pre-Alembic-DB:er
      automatiskt eller kör `upgrade head` vid uppstart.

## P8 – Schemaläggning / bakgrundsjobb

- [x] Lägg till en återkommande poll-job (`poll_publications`) som
      hämtar publikationslistan, uppdaterar DB och köar nya utgåvor
      för bevakade publikationer.
- [x] Köa nedladdningar som jobb via **APScheduler** (BackgroundScheduler
      i web-processen, BlockingScheduler i CLI-scheduler-läget).
- [x] `run_download_queue()` dränerar hela kön per tick i stället för
      ett jobb åt gången, så manuella bulk-köer plockas upp direkt.
- [ ] Stöd för per-publikation-schema (t.ex. "kolla varje natt kl 03").
- [x] Retry-logik inbyggd via `build_session()` (HTTPAdapter + Retry).
- [x] Jobbhistorik loggas till `jobs`-tabellen och visas i webgränssnittet.

## P9 – Webbgränssnitt

- [x] **FastAPI** + **Jinja2/HTMX** – ingen tung JS-frontend.
- [x] Sidor / vyer implementerade:
  - `GET /` Dashboard: stats-kort, senaste nedladdningar, senaste jobb.
  - `GET /publications` Publikationer: lista med HTMX watch/unwatch,
    live-sök, kategori-filter och cover-thumbnails.
  - `GET /publications/{code}` Detaljsida: beskrivning, nästa nummer,
    issue-tabell med manuella downloads, re-download och delete.
  - `GET /library` Library: listar alla PDF:er som ligger under
    `output_root`, grupperat per katalog med totalstorlek och
    live-sök.
  - `GET /publications/{code}/issues/{issue_code}/file` +
    `GET /library/file/{rel_path:path}` – serverar nedladdade PDF:er
    inline till webbläsarens inbyggda viewer (traversal-skyddat via
    `_safe_output_file`).
  - `GET /jobs` Jobblogg: alla jobb med statusfärger.
  - `GET /settings`, `POST /settings` Inställningar: poll-intervall,
    antal workers; token läses från env/fil av säkerhetsskäl.
  - `POST /settings/debug-poll` Manuell poll + inbäddad JSON-viewer.
  - `GET /healthz` Healthcheck.
- [x] Autentisering: `FLIPP_PASSWORD` env aktiverar loginskärm;
      inaktivt som default (trusted-network-läge). Timeout-säkra
      lösenordsjämförelser via `hmac.compare_digest`.
- [x] CSRF-skydd: `SameSite=strict` cookie + per-sessions-token
      validerat på alla state-mutating POST-anrop. HTMX-knappar
      skickar token via `hx-vals`.
- [x] HTML-sanering av tredjeparts-blurb via `flipp_dl/web/html_sanitize.py`
      innan Jinja renderar den som `| safe`.
- [ ] Fler REST/JSON API-endpoints utöver HTML-sidorna – återstår.

## P10 – Docker och deploy

- [x] `Dockerfile` baserad på `python:3.12-slim` med non-root-användare,
      volymer `/data` och `/output`, healthcheck mot `/healthz`.
- [x] `docker-compose.yml` med volymer `./data` och `./output` och alla
      env-variabler via `.env`-fil.
- [x] `.env.example` med alla konfigurerbara variabler dokumenterade.
- [x] `flipp_dl/web/main.py` startar Uvicorn + APScheduler (BackgroundScheduler)
      i en process; `python -m flipp_dl.web.main` är Docker-entrypoint.
- [x] GitHub Actions `.github/workflows/docker.yml`: bygger och
      publicerar till `ghcr.io/armandur/flipp-dl` vid `v*`-taggar,
      multi-arch (linux/amd64 + linux/arm64) via QEMU + Buildx,
      GHA cache för snabbare builds.

## P11 – Trevligt att ha

- [ ] Notiser när nya utgåvor laddats ner (webhook / ntfy / Discord /
      e-post).
- [ ] OPDS-feed så att PDF-läsare kan plocka upp nya utgåvor automatiskt.
- [ ] Integrering med **Calibre** / **Kavita** / **Komga** som
      post-processing-steg.
- [ ] Metrics-endpoint (Prometheus) för antal nedladdningar, fel,
      köstorlek m.m.
- [ ] i18n – åtminstone svenska och engelska i UI:t.
- [ ] Storleksuppskattning per issue/publication: spara `file_size`
      efter nedladdning och visa median/p90 som estimat för ännu inte
      nedladdade nummer (utforskat men avbokat).
- [ ] Realtidsprogress under download (SSE eller HTMX-polling) i
      stället för bara `queued → downloading → done` vid reload.
- [ ] Rensning/retention av `jobs`-tabellen (den växer obegränsat idag).
- [ ] Index på `issues.status` och `issues.publication_id` – listor
      filtrerar på båda men saknar index.
- [ ] Logga varning vid uppstart om `FLIPP_SECRET_KEY` fortfarande är
      default (`dev-secret-change-me`).
