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

## [P3][todo] [flipp] Komga nivå 2: pusha metadata och omslag

Få Komga att visa korrekt titel, nummer, utgivningsdatum, omslag och beskrivning från Flipp-API:t i stället för gissningar ur filnamnet. Svenska serietidningar finns inte i Comicvine/GCD, så ingen extern provider fyller i detta åt oss.

Beror på nivå 1. Stegen ligger i den fästa planen.

- ID: `01M0CQ9B9PB268MKEXSA6Q6W9A`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Komga nivå 1: auto-scan vid ny nedladdning

Få Komga att se nya nedladdningar direkt i stället för att vänta på nästa schemalagda filsystemsscan. Minst arbete och störst nytta av de tre Komga-nivåerna, och grunden de andra två bygger på.

Utgångsläget är att output_root redan är monterat som rotkatalog för ett 'Manuella'-bibliotek i Komga, där extern metadata-matching är avstängd och användarredigerade fält behålls. Layouten <publikationsnamn>/<issue_name>.pdf matchar Komgas default-tolkning av serie och bok.

Stegen ligger i den fästa planen. Fullständig ursprungstext finns i backlog-docen 'TODO-historik (migrerad från TODO.md)'.

- ID: `01M0CQ9B98BGRYSPCVA6VRM1B6`
- Type: feature
- Actor: ai:claude-opus-5

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

## [P4][todo] [flipp] Komga nivå 3: synka lässtatus tillbaka till flipp-dl

Visa lästa/olästa utgåvor i flipp-dl:s eget gränssnitt genom att hämta läsprogress från Komga. Valfri och långt fram - beror på bok-uppslaget i nivå 2.

- ID: `01M0CQ9BA2GVHXHYXAHEB3GGFD`
- Type: feature
- Actor: ai:claude-opus-5

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

## [P4][todo] [flipp] Integrering med Calibre eller Kavita

Post-processing mot andra bibliotekstjänster än Komga: lägga in nedladdade PDF:er i Calibre eller Kavita.

Från TODO.md, P11 - trevligt att ha. Komga-delen av den ursprungliga punkten har brutits ut till TASK-1326/1327/1328 (nivå 1-3), som är genomarbetade var för sig. Den här tasken är alltså bara Calibre/Kavita, och bör bygga på samma jobb- och hook-mönster som Komga-integrationen landar i.

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

