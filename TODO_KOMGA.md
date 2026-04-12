# TODO – Komga-integration

Feature-specifik TODO för att få flipp-dl att prata med en
[Komga](https://komga.org/)-instans. Separat från huvud-`TODO.md` eftersom
det är en större, fristående arbetsinsats som kan göras i flera etapper.

## Kontext

Svenska serietidningar är inte indexerade i Comicvine/MyAnimeList/GCD, så de
hamnar i ett **"Manuella"-library** i Komga där extern metadata-matching är
avstängd. Komga behåller användarredigerade fält i sådana bibliotek, vilket
gör dem till en bra måltavla för flipp-dl att skriva i utan att bli
överskriven av Komgas metadata-providers.

Utgångsläget är att `output_root` redan är monterat som rotkatalogen för
"Manuella"-biblioteket (eller en underkatalog av det) så att filerna i sig
redan syns för Komga efter en scan. Layouten
`<publication_name>/<issue_name>.pdf` matchar Komgas default-tolkning av
serie/bok.

## Mål

1. **Nivå 1 – Auto-scan**: Komga ska se nya nedladdningar direkt istället
   för att vänta på nästa schemalagda filsystemsscan.
2. **Nivå 2 – Metadata-push**: Komga ska visa korrekt titel, nummer,
   utgivningsdatum, omslag och beskrivning från Flipp-API:t istället för
   filnamns-gissning.
3. **Nivå 3 – Read-state-sync** (valfri, långt fram): Visa lästa/olästa
   issues i flipp-dl:s UI.

---

## Nivå 1 – Auto-scan (minst arbete, störst nytta)

- [ ] `flipp_dl/komga.py`: `KomgaClient(url, auth)` med `list_libraries()`
      och `scan_library(library_id)`. Stöd både HTTP Basic (äldre) och
      `X-API-Key`-header (Komga ≥ 1.8).
- [ ] Nya `DbSetting`-nycklar (env-overridable):
      `KOMGA_URL`, `KOMGA_USERNAME`, `KOMGA_PASSWORD`/`KOMGA_API_KEY`,
      `KOMGA_LIBRARY_ID`.
- [ ] `/settings`: ny "Komga"-sektion med URL/credentials + en
      "Test connection"-knapp som anropar `list_libraries()` och
      populerar en dropdown för library-valet. Inga secrets renderas
      tillbaka (skicka tom string + `placeholder="•••"`).
- [ ] Ny jobtyp `komga_sync`. `run_download_queue()` köar ett
      `komga_sync`-jobb när en nedladdning lyckats, istället för att
      blocka download-loopen.
- [ ] `komga_sync`-handler: `POST /api/v1/libraries/{id}/scan` +
      `finish_job`. Fel loggas som job-error men påverkar *inte*
      issue-status – Komga-nere får aldrig röd-flagga en lyckad
      nedladdning.
- [ ] Feature-flagga `KOMGA_ENABLED`-setting; allt ovan är no-op när den
      är av så befintliga användare utan Komga inte märker skillnad.
- [ ] Test: `tests/test_komga_client.py` med mockade HTTP-svar för
      `list_libraries`, `scan_library`, auth-headers.

## Nivå 2 – Metadata-push

### Mappning publikation ↔ Komga-serie

- [ ] Alembic-migration `0004_publication_komga.py` som lägger till
      `publications.komga_series_id INTEGER NULL`. Nullable för att
      mappningen byggs upp lat.
- [ ] Repository: `set_komga_series_id(custom_code, series_id)` +
      `get_unmapped_publications()`.
- [ ] Matchning vid första sync: `GET /api/v1/series?search=<folder>&library_id=<id>`,
      välj träffen där `metadata.title` eller `name`-fältet exakt
      matchar `_safe_name(publication.name)`. Träffen cache:as i
      kolumnen så vi aldrig söker om den.
- [ ] UI: på `/publications/{code}` visa `Komga: synkad ✓ · serie #1234`
      i headern när mappning finns, eller `Komga: okänd – söker nästa
      gång` annars. Lägg en liten länk till Komga-serien direkt.

### Metadata-fält

- [ ] `KomgaClient.patch_series_metadata(series_id, **fields)` som
      `PATCH`:ar `/api/v1/series/{id}/metadata`. Sätt bara de fält vi
      har: `title`, `titleSort`, `summary` (från `publication.description`,
      redan HTML-saniterad via `flipp_dl/web/html_sanitize.py`),
      `publisher="Egmont"`, `language="sv"`, `genres`/`tags` från
      Flipp-kategorierna.
- [ ] `KomgaClient.patch_book_metadata(book_id, **fields)` som
      `PATCH`:ar `/api/v1/books/{id}/metadata`: `title=issue.issue_name`,
      `number` + `numberSort` parsat ur `"Nr 12"` (regex
      `r"(?:nr\.?\s*)?(\d+)"`, fall tillbaka på lexikografisk ordning
      om det misslyckas), `releaseDate=issue.issue_date`.
- [ ] Bok-uppslag efter scan: `GET /api/v1/series/{id}/books` och matcha
      på filnamnet (`Path(file_path).stem`). Om inte hittad – poll upp
      till `KOMGA_WAIT_SECONDS` (default 10) eftersom Komga-scannen är
      asynkron. Timeout = finish_job med error och uppmaning att köra
      om manuellt.
- [ ] `PUBLICATION_METADATA_FIELDS`/`ISSUE_METADATA_FIELDS`-kopplingar
      hålls i en enda dict så det är enkelt att stänga av enskilda
      fält via settings (`KOMGA_PUSH_SUMMARY=false` osv.) för
      användare som vill redigera själva i Komga-UI:t.

### Omslag

- [ ] Ladda ner `publication.cover_url` till bytes i `KomgaClient` (
      behöver ingen auth, pagesuite-CDN).
- [ ] `POST /api/v1/series/{id}/thumbnails?selected=true` med
      multipart – gör så att Komga visar Flipps officiella omslag
      direkt istället för första sidan av PDF:en.
- [ ] Feature-flagga `KOMGA_PUSH_COVER` så det kan slås av för
      användare som föredrar Komgas egna thumbnails.

## Nivå 3 – Read-state (valfri, långt fram)

- [ ] `KomgaClient.get_book_read_progress(book_id)` →
      `{read: bool, page: int, completed: bool}`.
- [ ] Cacha i ny kolumn `issues.komga_read` eller separat
      `issue_read_state`-tabell. Uppdateras schemalagt en gång om
      dagen, inte vid varje request.
- [ ] UI: liten "Läst"-badge i issue-tabellen på
      `/publications/{code}`; filter i filter-baren.

---

## Designval som behöver bekräftas

- [ ] **Scan blocking vs. fire-and-forget**: ska `komga_sync`-jobbet
      *vänta* på att scan:en blir klar (enklare för metadata-push, men
      kan hänga länge vid en stor initial scan) eller bara trigga scan
      och stanna där (nivå 1 blir trivialt, metadata-push måste
      schemaläggas separat)? Lutning: **vänta med kort timeout** för
      metadata-push, **fire-and-forget** när `KOMGA_PUSH_METADATA` är
      av.
- [ ] **Batchning**: vid bulk-downloads (t.ex. första polling för ny
      watched-pub) skulle vi trigga en scan per issue. Debounce: om
      det redan finns ett queued `komga_sync`-jobb för samma library,
      merge:a dem. Kräver en flagga på jobbet eller en lookup i
      `list_jobs()`. Nice-to-have, inte blocker.
- [ ] **Error-policy**: ska upprepade Komga-fel pausa `komga_sync`-
      jobben helt (backoff) eller bara logga och fortsätta? Förmodligen
      backoff med exponentiell delay, maxa vid 30 minuter.
- [ ] **Multi-library**: behöver en användare kunna synka olika
      publikationer till olika Komga-libraries? Initialt **nej** –
      allt går till `KOMGA_LIBRARY_ID`. Per-publication-override är en
      senare iteration.

## Öppna frågor till Komga-API:t

- [ ] Finns det ett snabbare sätt än att polla
      `/api/v1/series/{id}/books` för att se när boken dykt upp efter
      scan? (Kolla om det finns en webhook/SSE-ström.)
- [ ] Exakta reglerna för `titleSort` vid serier som "Kalle Anka & Co"
      vs. "Bamse" – Komgas egen sort-policy ska inte krocka med våra
      värden.
- [ ] Hur reagerar "Manuella"-library på `PATCH`:ad metadata när
      användaren redan editerat fältet i UI:t? (Måste testas innan
      vi övertrampar handeditering.)

## Ordning att implementera

1. **Nivå 1** helt – settings, client, scan-jobb, tester, feature-flagga.
   Pushar klient-koden bakom `KOMGA_ENABLED=false` som default. Lätt
   att rulla ut, ger direkt nytta.
2. **Nivå 2 – mappning**. Migration + UI-indikator + auto-matchning på
   folder-namn. Ingen metadata-push än.
3. **Nivå 2 – metadata-push utan cover**. Series + book patching.
4. **Nivå 2 – cover-upload**. Egen feature-flagga.
5. **Nivå 3 – read-state**. Långt fram, bara om det känns värt.

## Referensimplementationer / dokumentation

- Komga REST API: `https://komga.org/docs/openapi/`
- Komga "Media-aware library" kontra "User-managed": vi siktar på det
  senare.
- HTML-sanering finns redan i `flipp_dl/web/html_sanitize.py` – samma
  filter ska användas innan `summary` skickas till Komga, annars
  smuggar vi in javascript från Flipp-API-svaret.
