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
- [ ] Lägg till `LICENSE`.

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

- [ ] Välj databas. Förslag: **SQLite** för enkelhet (en fil i volymen),
      med möjlighet att byta till Postgres senare via SQLAlchemy.
- [ ] Schema (första utkast):
  - `publications` (id, custom_code, name, categories, last_checked_at)
  - `issues` (id, publication_id, custom_code, issue_name, issue_date,
    discovered_at, downloaded_at, file_path, status)
  - `watchlist` (publication_id, enabled, schedule)
  - `settings` (token, output_path, poll_interval, …)
  - `jobs` (id, type, payload, status, started_at, finished_at, error)
- [ ] Lägg till **Alembic**-migrationer.
- [ ] Avdubblera nedladdningskontrollen via DB i stället för
      `os.path.isfile`.

## P8 – Schemaläggning / bakgrundsjobb

- [ ] Lägg till en återkommande poll-job som hämtar publikationslistan
      och upptäcker nya utgåvor.
- [ ] Köa nedladdningar som jobb. Alternativ:
  - **APScheduler** (enkelt, körs in-process)
  - **Celery + Redis** (mer komplexitet, bättre skalning)
  - **RQ** (mellanting)
- [ ] Stöd för per-publikation-schema (t.ex. "kolla varje natt kl 03").
- [ ] Retry-logik vid misslyckade nedladdningar med exponential backoff.
- [ ] Logga jobbhistorik i DB så att webgränssnittet kan visa den.

## P9 – Webbgränssnitt

- [ ] Välj ramverk. Förslag: **FastAPI** + **Jinja2/HTMX** för minimal
      frontend, alternativt FastAPI + React om SPA önskas.
- [ ] Sidor / vyer:
  - Dashboard: senaste nedladdningar, nästa schemalagda körning, fel.
  - Publikationer: lista, sök/filter på kategori, toggla bevakning.
  - Utgåvor: status per utgåva, manuell nedladdning, länk till PDF.
  - Inställningar: token, utdatakatalog, poll-intervall.
  - Logg/jobb: historik med status och felmeddelanden.
- [ ] Autentisering (även ett enkelt lösenord räcker – tjänsten är tänkt
      att självhostas).
- [ ] CSRF-skydd och säker hantering av token (kryptera i DB eller läs
      från miljövariabel).
- [ ] API-endpoints så att det går att automatisera utan UI.

## P10 – Docker och deploy

- [ ] `Dockerfile` baserad på `python:3.12-slim`:
  - Installera beroenden, kopiera källa, kör som icke-root-användare.
  - Exponera webbport (t.ex. 8000).
  - Entrypoint startar både web och scheduler (via `supervisord`,
    `honcho` eller en inbyggd async-loop).
- [ ] `docker-compose.yml` med volymer för:
  - `./output` – nedladdade PDF:er
  - `./data` – SQLite-fil och konfig
- [ ] Healthcheck-endpoint (`/healthz`).
- [ ] Miljövariabler dokumenterade i `README.md` och `.env.example`.
- [ ] GitHub Actions som bygger och publicerar image till GHCR vid tag.
- [ ] Överväg multi-arch-build (amd64 + arm64) så det kan köras på
      t.ex. Raspberry Pi / Synology.

## P11 – Trevligt att ha

- [ ] Notiser när nya utgåvor laddats ner (webhook / ntfy / Discord /
      e-post).
- [ ] OPDS-feed så att PDF-läsare kan plocka upp nya utgåvor automatiskt.
- [ ] Integrering med **Calibre** / **Kavita** / **Komga** som
      post-processing-steg.
- [ ] Metrics-endpoint (Prometheus) för antal nedladdningar, fel,
      köstorlek m.m.
- [ ] i18n – åtminstone svenska och engelska i UI:t.
