# Backlog Export

## [P2][done] [flipp] Hantera stallade jobb som fastnar i queued

Jobb blir kvar med status queued utan att något händer. Observerat i drift (körs på TERVO2:8934).

Misstänkt orsak vid läsning: _claim_next_download_job hämtar repo.list_jobs(limit=200) - nyaste först - och letar sedan bland just de 200 med reversed(). Finns det fler än 200 jobb nyare än ett queued jobb hamnar det utanför fönstret och plockas aldrig upp. En bulk-köläggning (poll som köar många utgåvor, eller manuella downloads) räcker för att passera 200. purge_old_jobs rör aldrig queued/running, så raderna ligger kvar för alltid.

Sekundärt att kolla:
- Jobb i RUNNING nollställs inte vid uppstart. Kraschar/omstartas processen mitt i en nedladdning ligger jobbet kvar som running och utgåvan som downloading, vilket dessutom blockerar manuell omköning (routes.py:281 hoppar över utgåvor i queued/downloading).
- Ingen timeout eller max-antal-försök per jobb.
- Utan FLIPP_TOKEN startar schedulern inte alls (web/main.py) - då köas jobb av webben men ingen dränerar kön.

Rätt fix är troligen en riktig claim-fråga mot DB (SELECT ... WHERE status=queued ORDER BY created_at ASC LIMIT 1) i stället för att filtrera i Python över ett fönster, plus en uppstädning vid uppstart som återställer running-jobb till queued.

- ID: `01M0BBXMEPZZWZVNYZR3SF7RWF`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Komga: utreda PATCH mot redan handeditad metadata

Från TODO_KOMGA.md, avsnitt Öppna frågor till Komga-API:t. Hur reagerar "Manuella"-library på PATCH:ad metadata när användaren redan editerat fältet i UI:t? Måste testas innan vi övertrampar handeditering. Relevant innan metadata-push i Nivå 2 aktiveras i produktion.

- ID: `01M0CPKH8R2CAXBVN2YS74TBDW`
- Type: spike
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: utreda titleSort-regler

Från TODO_KOMGA.md, avsnitt Öppna frågor till Komga-API:t. Exakta reglerna för titleSort vid serier som "Kalle Anka & Co" vs. "Bamse" - Komgas egen sort-policy ska inte krocka med våra värden. Relevant för patch_series_metadata i Nivå 2.

- ID: `01M0CPKH8D3B7APW0HEFJPM2ZN`
- Type: spike
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: utreda snabbare uppslag än polling efter scan

Från TODO_KOMGA.md, avsnitt Öppna frågor till Komga-API:t. Finns det ett snabbare sätt än att polla /api/v1/series/{id}/books för att se när boken dykt upp efter scan? Kolla om det finns en webhook/SSE-ström i Komgas API. Relevant för bok-uppslaget i Nivå 2.

- ID: `01M0CPKH86WD18Z08PQRQBYZ3A`
- Type: spike
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: designval - multi-library-stöd

Från TODO_KOMGA.md, avsnitt Designval som behöver bekräftas. Behöver en användare kunna synka olika publikationer till olika Komga-libraries? Initialt nej enligt TODO:n - allt går till KOMGA_LIBRARY_ID. Per-publication-override är en senare iteration.

- ID: `01M0CPKH80BQ8ZFTN6SHB8JYEK`
- Type: chore
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: designval - error-policy vid upprepade fel

Från TODO_KOMGA.md, avsnitt Designval som behöver bekräftas. Ska upprepade Komga-fel pausa komga_sync-jobben helt (backoff) eller bara logga och fortsätta? Förmodligen backoff med exponentiell delay, maxa vid 30 minuter enligt TODO:n. Beslutas innan/under Nivå 1-arbetet med komga_sync-handlern.

- ID: `01M0CPKH7SVCRWWEQE1GB9MH8D`
- Type: chore
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: designval - batchning av scan-jobb

Från TODO_KOMGA.md, avsnitt Designval som behöver bekräftas. Vid bulk-downloads (t.ex. första polling för ny watched-publikation) skulle vi trigga en scan per issue. Debounce: om det redan finns ett queued komga_sync-jobb för samma library, merge:a dem. Kräver en flagga på jobbet eller en lookup i list_jobs(). Nice-to-have, inte blocker enligt TODO:n.

- ID: `01M0CPKH7K76BWADXXVBKACEZW`
- Type: chore
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: designval - scan blocking vs. fire-and-forget

Från TODO_KOMGA.md, avsnitt Designval som behöver bekräftas. Ska komga_sync-jobbet vänta på att scan:en blir klar (enklare för metadata-push, men kan hänga länge vid en stor initial scan) eller bara trigga scan och stanna där (nivå 1 blir trivialt, metadata-push måste schemaläggas separat)? Lutning i TODO:n: vänta med kort timeout för metadata-push, fire-and-forget när KOMGA_PUSH_METADATA är av. Beslutas innan/under Nivå 2-arbetet.

- ID: `01M0CPKH7EP608P4RFV43PVQR4`
- Type: chore
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: feature-flagga KOMGA_PUSH_COVER

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Omslag. Feature-flagga KOMGA_PUSH_COVER så cover-uppladdningen kan slås av för användare som föredrar Komgas egna thumbnails. Beror på uppladdningen av omslag som thumbnail.

- ID: `01M0CPJTVZVZ3HEMTC6SQE60NJ`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: ladda upp omslag som thumbnail

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Omslag. POST /api/v1/series/{id}/thumbnails?selected=true med multipart, så Komga visar Flipps officiella omslag direkt i stället för första sidan av PDF:en. Beror på nedladdning av cover_url och series-mappningen.

- ID: `01M0CPJTVS3S55X4XN52RWSV90`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: ladda ner cover_url i KomgaClient

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Omslag. Ladda ner publication.cover_url till bytes i KomgaClient (behöver ingen auth, pagesuite-CDN). Beror på grundläggande KomgaClient från Nivå 1, i övrigt fristående från metadata-push-arbetet.

- ID: `01M0CPJTVHPJQP01CQM8G3K4V5`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: konfigurerbara metadata-fält per setting

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Metadata-fält. PUBLICATION_METADATA_FIELDS/ISSUE_METADATA_FIELDS-kopplingar hålls i en enda dict så det är enkelt att stänga av enskilda fält via settings (t.ex. KOMGA_PUSH_SUMMARY=false) för användare som vill redigera själva i Komga-UI:t. Beror på patch_series_metadata och patch_book_metadata.

- ID: `01M0CPJTVAAJMX7F7PM1MJTYP9`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: bok-uppslag efter scan

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Metadata-fält. Efter scan: GET /api/v1/series/{id}/books och matcha på filnamnet (Path(file_path).stem). Om inte hittad - poll upp till KOMGA_WAIT_SECONDS (default 10) eftersom Komga-scannen är asynkron. Timeout = finish_job med error och uppmaning att köra om manuellt. Beror på series-mappningen och komga_sync-jobbet från Nivå 1.

- ID: `01M0CPJTV37BGWE4W3636NF1MF`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: patch_book_metadata i KomgaClient

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Metadata-fält. KomgaClient.patch_book_metadata(book_id, **fields) som PATCH:ar /api/v1/books/{id}/metadata: title=issue.issue_name, number + numberSort parsat ur "Nr 12" (regex r"(?:nr\.?\s*)?(\d+)", fall tillbaka på lexikografisk ordning om det misslyckas), releaseDate=issue.issue_date. Beror på bok-uppslaget efter scan (för att hitta rätt book_id).

- ID: `01M0CPJTTWVYP2BY26YQN54WRF`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: patch_series_metadata i KomgaClient

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Metadata-fält. KomgaClient.patch_series_metadata(series_id, **fields) som PATCH:ar /api/v1/series/{id}/metadata. Sätt bara de fält vi har: title, titleSort, summary (från publication.description, redan HTML-saniterad via flipp_dl/web/html_sanitize.py), publisher="Egmont", language="sv", genres/tags från Flipp-kategorierna. Beror på series-mappningen (komga_series_id).

- ID: `01M0CPJTTMBX054K94XZN7ZZG9`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: UI-indikator för series-mappning

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Mappning publikation <-> Komga-serie. På /publications/{code} visa "Komga: synkad ✓ · serie #1234" i headern när mappning finns, eller "Komga: okänd – söker nästa gång" annars, med en liten länk till Komga-serien. Beror på auto-matchningen (komga_series_id måste finnas för att visas).

- ID: `01M0CPJTTD88CMKRVAQSH33JRX`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: auto-matchning publikation mot Komga-serie

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Mappning publikation <-> Komga-serie. Matchning vid första sync: GET /api/v1/series?search=<folder>&library_id=<id>, välj träffen där metadata.title eller name-fältet exakt matchar _safe_name(publication.name). Träffen ska cachas i komga_series_id-kolumnen så vi aldrig söker om den. Beror på repository-metoderna för series-mappning.

- ID: `01M0CPJTT6D4TN5MD9RPN2RWMZ`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: repository-metoder för series-mappning

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Mappning publikation <-> Komga-serie. Repository: set_komga_series_id(custom_code, series_id) + get_unmapped_publications(). Beror på migrationen som lägger till publications.komga_series_id.

- ID: `01M0CPJTSZ6MM2ZKV7C9EN7AAP`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: migration för komga_series_id

Från TODO_KOMGA.md, Nivå 2 - Metadata-push, avsnitt Mappning publikation <-> Komga-serie. Alembic-migration 0004_publication_komga.py som lägger till publications.komga_series_id INTEGER NULL. Nullable eftersom mappningen byggs upp lat. Beror på Nivå 1 (KomgaClient m.m.) vara på plats.

- ID: `01M0CPJTSR3VXSPPJG10DBVY8Q`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: tester för KomgaClient

Från TODO_KOMGA.md, Nivå 1 - Auto-scan. Test: tests/test_komga_client.py med mockade HTTP-svar för list_libraries, scan_library och auth-headers (både HTTP Basic och X-API-Key). Beror på KomgaClient-implementationen.

- ID: `01M0CPJ2KTND777TRR3XQDSR4V`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: feature-flagga KOMGA_ENABLED

Från TODO_KOMGA.md, Nivå 1 - Auto-scan. Feature-flagga KOMGA_ENABLED-setting; all Komga-funktionalitet ska vara no-op när den är av, så befintliga användare utan Komga inte märker skillnad. Bör gälla för hela Nivå 1-arbetet (KomgaClient, settings-sektion, komga_sync-jobbet).

- ID: `01M0CPJ2KKX2WN7ZR7E979DFHS`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: komga_sync-handler (scan + finish_job)

Från TODO_KOMGA.md, Nivå 1 - Auto-scan. komga_sync-handler: POST /api/v1/libraries/{id}/scan + finish_job. Fel ska loggas som job-error men får inte påverka issue-status - Komga-nere ska aldrig röd-flagga en lyckad nedladdning. Beror på jobtypen komga_sync och KomgaClient.

- ID: `01M0CPJ2KCQ25QH34YJ2AMMZ66`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: jobtyp komga_sync

Från TODO_KOMGA.md, Nivå 1 - Auto-scan. Ny jobtyp komga_sync. run_download_queue() ska köa ett komga_sync-jobb när en nedladdning lyckats, i stället för att blocka download-loopen. Beror på KomgaClient och Komga-DbSetting-nycklarna.

- ID: `01M0CPJ2K39N7M2B47KMMF4VAV`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: settings-sektion med Test connection

Från TODO_KOMGA.md, Nivå 1 - Auto-scan. På /settings: ny "Komga"-sektion med URL/credentials-fält och en "Test connection"-knapp som anropar list_libraries() och populerar en dropdown för library-valet. Inga secrets ska renderas tillbaka (skicka tom sträng + placeholder="•••"). Beror på KomgaClient och Komga-DbSetting-nycklarna.

- ID: `01M0CPJ2JWEKEF79HRP20GY46X`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: nya DbSetting-nycklar för anslutning

Från TODO_KOMGA.md, Nivå 1 - Auto-scan. Lägg till nya DbSetting-nycklar (env-overridable): KOMGA_URL, KOMGA_USERNAME, KOMGA_PASSWORD/KOMGA_API_KEY, KOMGA_LIBRARY_ID. Beror på KomgaClient (se separat task 'Komga: KomgaClient med list_libraries och scan_library').

- ID: `01M0CPJ2JMYAGVX649NN5GYH2D`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Komga: KomgaClient med list_libraries och scan_library

Från TODO_KOMGA.md, Nivå 1 - Auto-scan. Bygg flipp_dl/komga.py: KomgaClient(url, auth) med list_libraries() och scan_library(library_id). Ska stödja både HTTP Basic (äldre Komga) och X-API-Key-header (Komga >= 1.8). Detta är grundbyggstenen för alla övriga Komga-tasks - beroendefri, ska göras först.

- ID: `01M0CPJ2JDDVRJENAT1RAMKPWG`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Fler REST/JSON API-endpoints

Från TODO.md, avsnitt P9 (Webbgränssnitt). Webbgränssnittet är byggt med FastAPI + Jinja2/HTMX och exponerar idag bara HTML-sidor, inga renodlade REST/JSON-endpoints utöver HTML-vyerna. Lägg till JSON-API-endpoints som komplement till HTML-sidorna, för programmatisk åtkomst (t.ex. publikationer, utgåvor, jobbstatus).

- ID: `01M0CPHHZ21BBEMWPE99Z8JJJN`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] Stöd för per-publikation-schema

Från TODO.md, avsnitt P8 (Schemaläggning / bakgrundsjobb). Idag pollas alla bevakade publikationer med samma globala intervall (APScheduler). Lägg till stöd för att sätta ett eget schema per publikation, t.ex. "kolla varje natt kl 03", i stället för ett enda gemensamt poll-intervall.

- ID: `01M0CPHHYVM6V22EQ1KRWYXERE`
- Type: feature
- Actor: ai:claude-code

---

## [P3][done] [flipp] Migrera projektets TODO.md och TODO_KOMGA.md till backlog

Repot har två egna todo-dokument som lever parallellt med backlog: TODO.md (P0-P11, mestadels avbockat, kvar: per-publikation-schema, fler JSON/REST-endpoints, notiser, OPDS, Komga/Kavita/Calibre, Prometheus-metrics, i18n) och TODO_KOMGA.md (trestegsplan: auto-scan, metadata-push, read-state-sync).

Flytta in de OGJORDA punkterna som tasks i backlog-projektet flipp. De avbockade punkterna är projekthistorik - de hör hemma i en backlog doc, inte som done-tasks.

Beslutat av Rasmus: när migreringen är klar TAS TODO.md och TODO_KOMGA.md bort ur repot. Därefter är backlog sanningskällan och repot får den vanliga speglingen (backlog export --project flipp --format md -o backlog.md), som redan finns på plats.

- ID: `01M0BBZ1YJY4N7W83KE738NHDS`
- Type: chore
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Importera befintligt nuläge från nedladdade filer i output

Skanna output_root och matcha filerna mot issues i DB, så att utgåvor som redan ligger på disk markeras som done (file_path + downloaded_at) i stället för att köas om. Gör det möjligt att bygga upp ett korrekt nuläge i en ny/tom databas utan att ladda ner om allt.

Layouten är <publikationsnamn>/<issue_name>.pdf via storage.py:s safe_name, så matchningen kan gå via samma namnregel. routes.py har redan _annotate_file_exists som gör en liknande koll för visning - den logiken finns alltså delvis.

Bör kunna köras både som CLI-kommando och som knapp i webbgränssnittet.

- ID: `01M0BBY3X4VKXXTRYY9T2EGDP4`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P4][todo] [flipp] Komga: Läst-badge i issue-tabellen

Från TODO_KOMGA.md, Nivå 3 - Read-state-sync (valfri, långt fram). UI: liten "Läst"-badge i issue-tabellen på /publications/{code}; filter i filter-baren. Beror på den cachade lässtatusen.

- ID: `01M0CPKH799X6ACF53H2BCBJT6`
- Type: feature
- Actor: ai:claude-code

---

## [P4][todo] [flipp] Komga: cacha lässtatus lokalt

Från TODO_KOMGA.md, Nivå 3 - Read-state-sync (valfri, långt fram). Cacha i ny kolumn issues.komga_read eller separat issue_read_state-tabell. Uppdateras schemalagt en gång om dagen, inte vid varje request. Beror på get_book_read_progress.

- ID: `01M0CPKH735ZVSEGZTA341PSR4`
- Type: feature
- Actor: ai:claude-code

---

## [P4][todo] [flipp] Komga: get_book_read_progress i KomgaClient

Från TODO_KOMGA.md, Nivå 3 - Read-state-sync (valfri, långt fram). KomgaClient.get_book_read_progress(book_id) -> {read: bool, page: int, completed: bool}. Beror på bok-uppslaget från Nivå 2 (behöver book_id).

- ID: `01M0CPKH6WG7TV9RQN2NAK47AC`
- Type: feature
- Actor: ai:claude-code

---

## [P4][todo] [flipp] i18n i webbgränssnittet

Från TODO.md, avsnitt P11 (Trevligt att ha). Internationalisering av webb-UI:t, åtminstone svenska och engelska.

- ID: `01M0CPHJ034MCD47N7BEXB93EQ`
- Type: feature
- Actor: ai:claude-code

---

## [P4][todo] [flipp] Metrics-endpoint (Prometheus)

Från TODO.md, avsnitt P11 (Trevligt att ha). Lägg till en Prometheus-metrics-endpoint för antal nedladdningar, fel, köstorlek m.m.

- ID: `01M0CPHHZX3Z3C8M5HMPX8YD0E`
- Type: feature
- Actor: ai:claude-code

---

## [P4][todo] [flipp] Integrering med Calibre/Kavita/Komga

Från TODO.md, avsnitt P11 (Trevligt att ha). Integrering med Calibre, Kavita eller Komga som post-processing-steg efter nedladdning. Se även TODO_KOMGA.md-tasks för den mer utarbetade Komga-varianten av detta (nivå 1-3).

- ID: `01M0CPHHZQD0P9HEJTK71YX2CM`
- Type: feature
- Actor: ai:claude-code

---

## [P4][todo] [flipp] OPDS-feed

Från TODO.md, avsnitt P11 (Trevligt att ha). Lägg till en OPDS-feed så att PDF-läsare kan plocka upp nya utgåvor automatiskt.

- ID: `01M0CPHHZHHC2TF38SJ0DF4Y7Z`
- Type: feature
- Actor: ai:claude-code

---

## [P4][todo] [flipp] Notiser vid nya nedladdningar

Från TODO.md, avsnitt P11 (Trevligt att ha). Skicka notiser när nya utgåvor har laddats ner, via webhook, ntfy, Discord eller e-post.

- ID: `01M0CPHHZ89METGXNK2ASHZ2SQ`
- Type: feature
- Actor: ai:claude-code

---

