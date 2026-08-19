# Backlog Export

## [P1][done] [flipp] Två utgåvor kan dela filnamn, den ena går förlorad

Hittat i drift: 91:an visar 107 nedladdade utgåvor men mappen innehåller 106 filer. Två utgåvor pekar på exakt samma fil, /output/91an/91an - 2022-02-25 - Nr 6 2022.pdf:

  ab2041c2-bb4d-412a-b567-0e01883d085c  Nr 6 2022  2022-02-25
  6410d950-be8e-4d05-a9f3-c7184c5801c3  Nr 6 2022  2022-02-25

De är INTE dubbletter hos Flipp. Båda har 52 sidor men helt olika sid-URL:er, alltså två skilda utgåvor med samma namn och datum. Filnamnet byggs av publikation, issue_date och issue_name (issue_path i storage.py), vilket inte är unikt. Den andra nedladdningen träffade skip_existing, hoppades över och markerades ändå som done med den första utgåvans sökväg. Innehållet i den andra utgåvan finns alltså inte på disk någonstans.

Acceptanskriterier:
- Två utgåvor med samma namn och datum får skilda filnamn, exempelvis med issue-koden som suffix vid krock. Befintliga filnamn ändras inte när ingen krock finns.
- Nedladdningen markerar aldrig en utgåva som done med en fil som tillhör en annan utgåva. skip_existing ska bara hoppa över när filen hör till samma utgåva.
- Ett sätt att hitta redan drabbade rader: utgåvor som delar file_path ska gå att lista, och den förlorade ska kunna köas om.

Verifiering: tester i tests/test_storage.py och tests/test_downloader.py för krockfallet, plus kontroll mot driftinstansen att 91:an går från 107/106 till 107/107.

- ID: `01M0D08ZEA0MBSX6NDVHHB9CTQ`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Köa missade utgåvor när en publikation börjar bevakas

Att kryssa Watch köar ingenting. Poll köar bara NYUPPTÄCKTA utgåvor (new_issues i poll_publications), så allt som redan fanns i databasen när bevakningen slogs på laddas aldrig ner. I praktiken får man leta upp publikationer där Downloaded X/Y har X != Y och trycka download manuellt på varje rad.

Acceptanskriterier:
- Watch på en publikation köar alla dess utgåvor som inte är nedladdade, inklusive tidigare felade, och skapar ett jobb per utgåva.
- Redan köade eller pågående utgåvor dubbelköas inte.
- Poll fyller på bevakade publikationer med utgåvor som aldrig laddats ner, så en missad utgåva självläker vid nästa poll. Felade utgåvor köas INTE om automatiskt - en utgåva som alltid failar ska inte köas om var sjätte timme.
- Unwatch köar ingenting och avbryter inget pågående.

Verifiering: riktade tester i tests/test_repository.py, tests/test_scheduler.py och tests/test_web_routes.py, plus klick på Watch i webbläsaren med kontroll av att kön faktiskt fylls.

- ID: `01M0CZ6BXJP7W28D2YR2MNSCRZ`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Utgåvor fastnar i queued utan jobb och går inte att köa om

Observerat i drift: Hälge "Nr 7 2026" står som Queued sedan 2026-06-25, men jobbtabellen har noll köade jobb. Utgåvans status och jobbtabellen har alltså glidit isär - troligen en containeromstart mitt i, eller ett jobb som felade efter att issue-statusen satts.

Det går inte att ta sig ur läget från gränssnittet: issue_row.html visar en inaktiverad knapp för queued/downloading, och download_issue_manual i routes.py hoppar dessutom över utgåvor i de statusarna för att inte skapa dubbletter. Raden är därmed permanent låst.

Acceptanskriterier:
- Vid uppstart återställs utgåvor i queued/downloading som saknar ett aktivt jobb (queued eller running) till not downloaded, tillsammans med den befintliga återställningen av hängande running-jobb.
- Gränssnittet har en väg ut för en rad som står i queued/downloading: en knapp som avbryter och nollställer utgåvan.
- Ett aktivt jobb för utgåvan avslutas som avbrutet, så det inte plockas upp efteråt.

Verifiering: riktade tester i tests/test_scheduler.py och tests/test_web_routes.py, plus browser-verifiering av knappen (klicka den, inte bara rendera den).

- ID: `01M0CYTVJZDKYKPFG27M6RG981`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] database is locked när progress skrivs under nedladdning

Jobb 1016 på driftinstansen (Agent X9, Nr 7 2023, 2026-06-24) dog med:

  (sqlite3.OperationalError) database is locked
  [SQL: UPDATE issues SET progress_current=? WHERE issues.id = ?]

update_issue_progress körs en gång per nedladdad sida från schedulertråden, medan webbtråden läser i samma SQLite-fil. Skrivlåset räcker inte till och hela sessionen rullas tillbaka, så jobbet failar trots att nedladdningen i sig kan ha gått bra.

Att titta på:
- timeout på anslutningen i db/session.py (SQLite default är 5 sekunder, och WAL hjälper läsare men inte två skrivare)
- skriv inte progress per sida - skriv var N:e sida eller max en gång per sekund, det är ändå bara till för UI-räknaren
- fånga OperationalError kring progress-skrivningen specifikt: en missad progressuppdatering får inte fälla hela nedladdningsjobbet

Reproducera genom att ladda ner en publikation samtidigt som man klickar runt i webbgränssnittet.

- ID: `01M0CYHH20D3DAKF8FSWNJFJDT`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Visa tider i lokal tidszon i stället för UTC

Alla tidsstämplar lagras som naiv UTC (_now() i db/repository.py) och renderas rakt av i mallarna, så gränssnittet visar UTC. Sommartid gör att tiderna ligger två timmar fel mot svensk klocka, vilket är förvirrande på jobbsidan där man jämför mot när något faktiskt hände.

Konvertera vid rendering: ett Jinja-filter som tolkar värdet som UTC och skriver ut i konfigurerad tidszon. Tidszonen ska gå att styra med env (FLIPP_TZ), med Europe/Stockholm som default. Databasen fortsätter lagra UTC - det är rätt lagringsform och ska inte röras.

Berörda mallar: dashboard.html, jobs.html, job_detail.html, library.html. Dokumentera variabeln i .env.example, docker-compose.yml och README.

Verifiering: test som renderar en känd UTC-tid och kontrollerar att utskriften är förskjuten rätt, inklusive ett vinterdatum och ett sommardatum så DST täcks.

- ID: `01M0CY63XA8A4TN8CG5XG9NHJ5`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Kövy: statusfilter på /jobs, kökort och live-uppdaterad dashboard

Jobbsidan hämtar list_jobs(limit=100) sorterat nyast först, utan statusfilter. Ligger det 100 färska jobb överst blir en kö med äldre queued-jobb osynlig i gränssnittet, även när schedulern plockar dem korrekt (jämför TASK-1282, där samma fönstertänk var själva buggen). Med 17660 kända utgåvor räcker en bulk-köläggning för att det ska hända.

Acceptanskriterier:
- /jobs har ett statusfilter (queued, running, done, error, alla) som filtrerar i DATABASEN, inte i JS över de redan hämtade raderna.
- Antalet queued respektive running visas oberoende av hur många rader som listas, så köns djup syns även när den är större än sidstorleken.
- Dashboarden har ett kökort med antal queued och running som länkar till motsvarande filter på /jobs.
- Dashboardens siffror uppdateras asynkront (HTMX-polling mot en partial) så sidan inte behöver laddas om för att visa att kön krymper.
- Repository får en räknemetod för jobb per status i stället för att sidan räknar på en hämtad lista.

Verifiering: riktade tester i tests/test_web_routes.py och tests/test_repository.py, plus browser-verifiering vid 390px och 1280px enligt browser-verify-skillen.

- ID: `01M0CR3XE1AF5T0EDE1ZGSVY8G`
- Type: improvement
- Actor: ai:claude-opus-5

---

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

## [P3][done] [flipp] Byt webbläsardialoger mot egna modaler

Gränssnittet använder webbläsarens inbyggda dialoger på fem ställen: hx-confirm i issue_row.html (Cancel, Re-download, Delete) och publication_detail.html (Queue missing issues), plus alert("Copy failed") i debug_poll_result.html. De ser ut som systemdialoger, går inte att styla, och texten prefixas av webbläsaren med sidans adress.

Ersätt med en egen modal i base.html byggd på <dialog>. HTMX fyrar htmx:confirm innan varje förfrågan med hx-confirm - fånga eventet, visa modalen, och kör evt.detail.issueRequest() när användaren bekräftar. Då behöver inga knappar ändras.

alert-fallet blir en kort inline-text i stället för en dialog.

Acceptanskriterier:
- Ingen confirm/alert/prompt kvar i mallarna.
- Modalen följer sidans mörka tema, går att stänga med Escape och genom att klicka utanför, och avbryter då förfrågan.
- Bekräfta kör samma HTMX-anrop som förut.
- Fokus hamnar i modalen när den öppnas.

Verifiering: klicka igenom Cancel, Re-download, Delete och Queue missing i webbläsaren - både bekräfta och avbryt - och kontrollera att avbryt inte skickar något anrop. Skärmdumpar vid 390px och 1280px.

- ID: `01M0CZKQ35MP4YXWNKQWZA820M`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Knapp för att köa saknade utgåvor utan att röra bevakningen

I dag är unwatch följt av watch enda sättet att köa om en publikations saknade utgåvor manuellt. Det är en omväg, och mellan klicken är publikationen faktiskt obevakad - landar en poll där missas nya utgåvor.

Lägg en knapp på publikationens detaljsida som köar allt som inte är nedladdat, inklusive felade, utan att ändra watched-flaggan. Använder repo.queue_missing_issues som redan finns. Ska visa hur många som köades.

- ID: `01M0CZDPNXRAD3N9GGHQNCMFDD`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Cachea omslag lokalt i stället för att hotlinka pagesuite

Omslagen hämtas i dag direkt från pagesuite vid varje sidladdning: issue_row.html pekar på https://edition.pagesuite-professional.co.uk/get_image.aspx?w=100&eid=<issue-kod> och publikationsraden på publication.cover_url. Det gör gränssnittet beroende av en extern tjänst, läcker vilka sidor som besöks, och blir långsamt när många rader renderas.

Cachea i stället lokalt för både publikationer och utgåvor: hämta bilden en gång, spara på disk (utanför output_root så den inte förväxlas med nedladdade PDF:er) eller i databasen, och servera från en egen endpoint. Utgåvornas omslag kan hämtas när utgåvan upptäcks vid poll.

Behåll dagens beteende på Publications: raden visar publikationens senaste omslag, inte ett fast.

Att tänka igenom:
- Hämtning får inte ske synkront i renderingen - det skulle göra sidan lika långsam som den externa tjänsten. Antingen vid poll, eller lat med en jobbtyp.
- Rensning: omslagen för 17660 utgåvor blir en del data. Rimligen bara utgåvor som faktiskt visas, eller en storleksgräns.
- Fallback när bilden saknas: dagens onerror-döljning duger.

- ID: `01M0CZ3Y538K1Q6DF6Y0D30S2H`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Preview av en utgåva utan att den räknas som nedladdad

Kunna titta på en utgåva utan att den hamnar i biblioteket: hämta PDF:en till en temporär plats, servera den inline i webbläsarens visare, och låt utgåvans status stå kvar som inte nedladdad. Filen städas efter en tid eller efter visning.

Att tänka igenom:
- Temporärkatalogen får inte ligga under output_root, annars plockar Library och den kommande filimporten (TASK-1283) upp den som en riktig nedladdning.
- En preview kostar lika mycket bandbredd som en vanlig nedladdning. Rimligt att bara hämta de första sidorna? Isåfall blir det en egen väg genom downloadern, inte samma merge-av-alla-sidor.
- Städning: enklast en TTL som röjs vid nästa poll, i stil med purge_old_jobs.
- Statusen får inte gå via issues-tabellens status-fält, då blir den synlig som en pågående nedladdning i kön.

- ID: `01M0CZ0FB389XF2V4FSH45FB0A`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Watched only ska vara förkryssad som default

Kryssrutan Watched only på /publications är omarkerad vid sidladdning, så listan visar alla 94 publikationer trots att bara 19 är bevakade. Bevakade är det man normalt vill se.

Gör den förkryssad som default. Hänger ihop med TASK-1329 (behåll filtret i URL:en) - en explicit URL-flagga ska vinna över defaulten, så en delad länk utan filter fortfarande kan visa allt. Ta de två tillsammans.

- ID: `01M0CYZAAQV6KCRKY8YCVC258H`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Sätt Flipp-token via gränssnittet, med userscript som hämtar den

Settings-sidan säger i dag att token bara kan läsas från FLIPP_TOKEN eller token-filen vid uppstart och inte får ändras i gränssnittet "of security reasons". Det resonemanget hörde till CLI-tiden - nu är det en inloggad webbtjänst, och att behöva starta om containern för att byta token är sämre än att kunna klistra in den.

Två delar:
1. Token blir en inställning som kan sparas från /settings, och som klienten läser vid nästa anrop utan omstart. Env-variabeln fortsätter gälla som utgångsvärde. Rendera aldrig tillbaka värdet - visa maskerat och spara bara vid ändring. Fundera på lagring: klartext i settings-tabellen är samma nivå som dagens token-fil, men det bör vara ett medvetet val.
2. Ett userscript (Tampermonkey) som körs på tidningar.flipp.se, plockar tokenen ur sidans anrop eller lagring, och postar den till flipp-dl:s /settings. Länken till skriptet ligger lämpligen på settings-sidan tillsammans med en kort instruktion, så flödet blir: installera skriptet, logga in på Flipp, klicka knappen.

Mottagningen av token från userscriptet behöver en egen genomtänkt väg in: CSRF-skyddad POST med samma inloggning som resten av gränssnittet, eller en engångsnyckel som visas på settings-sidan.

- ID: `01M0CYVE7SCF7220W9FPTSWJCM`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Publikationslistan laddar alla utgåvor för att räkna två tal

list_publications() gör selectinload på DbPublication.issues, så en sidladdning av /publications drar in varje utgåva i databasen - 17660 rader på driftinstansen. Allt som faktiskt används per rad är två tal: antal utgåvor och antal nedladdade (num_issues och num_downloaded i db/models.py).

Ersätt med en aggregerad fråga som räknar per publikation i databasen, i stil med select(publication_id, count(*), count(*) filter (where status = done)) group by publication_id, och mata radmallen med de talen i stället för hela issues-relationen.

Rör även /publications/{code}-detaljsidan, som rimligen behöver utgåvorna på riktigt - där ska relationen vara kvar.

Acceptanskriterier:
- /publications laddar inte längre issues-relationen för listvyn.
- Kolumnen Downloaded visar samma tal som i dag.
- Watch/unwatch-swappen (publication_row.html via HTMX) visar också rätt tal, den renderar samma partial.

Verifiering: riktade tester i tests/test_web_routes.py och tests/test_repository.py. Mät gärna före och efter genom att räkna SQL-satser med en SQLAlchemy-event-lyssnare i testet.

- ID: `01M0CY4MQDY7X4E87NJ2V69G9S`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Visa vad ett jobb gäller: målkolumn på /jobs och detaljvy

Jobbtabellen visar bara id, typ, status, tider och ett avhugget felmeddelande. Vilken publikation eller utgåva jobbet gäller står bara som issue_id inuti payload-JSON:en, så raden är i praktiken oläsbar.

Acceptanskriterier:
- /jobs har en kolumn som visar publikation och utgåva för download-jobb, länkad till publikationssidan. Poll-jobb visar ett neutralt streck.
- Uppslaget sker i en batchad fråga för hela sidan, inte en fråga per rad.
- Detaljvy /jobs/{id} visar hela payloaden, alla tidsstämplar, hela felmeddelandet och länkar till utgåvan och publikationen.
- Ett jobb vars issue har raderats, eller vars payload är trasig, renderar utan att spränga sidan.

Verifiering: riktade tester i tests/test_web_routes.py, plus browser-verifiering vid 390px och 1280px.

- ID: `01M0CRP8MHXHH7MY4194WE11G9`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Navigeringsraden ger horisontell scroll vid 390px

Alla sidor har horisontell overflow i mobilbredd: vid 390px viewport blir document.documentElement.scrollWidth 553px. Mätt på /, /jobs, /settings, /publications och /library, alltså befintligt och inte infört av kövyn (TASK-1330).

Orsaken är navigeringsraden i base.html - länkarna plus spacer-elementet ligger på en rad som är 557px bred och wrappar inte. Verifiera med Playwright-mätningen i browser-verify-skillen: scrollWidth ska vara lika med viewport-bredden vid både 390px och 1280px.

- ID: `01M0CRHTW8SEH2362Q0HFDKVA2`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Undersök om nedladdade PDF:er är vattenmärkta

Skanna de nedladdade PDF:erna efter spår som kan knyta filen till kontot: synlig vattenstämpel i sidbilden, osynlig text i textlagret, XMP/DocInfo-metadata, unika objekt-ID:n eller kontospecifika URL:er i sidornas resurser.

Utgå från filerna som redan ligger i output på driftinstansen. Jämför gärna samma utgåva hämtad vid två tillfällen - skiljer bytesekvenserna sig åt på ställen som inte är tidsstämplar är det ett tecken på per-nedladdning-märkning.

Rent utredande task: resultatet avgör om det behövs någon åtgärd alls, och i så fall vilken.

- ID: `01M0CR734QFWN9Z0M72HAVTJ99`
- Type: spike
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Behåll watched-only-filtret i URL:en

Watched only-kryssrutan på /publications är i dag ren klientside-state (JS-filter över raderna, publications.html:61-94). Den nollställs så fort man navigerar bort och tillbaka, till exempel efter ett besök på en publikationssida.

Lägg filtret i URL:en som en flagga (hash eller query-param) och läs tillbaka den vid sidladdning, så valet överlever navigering och går att bokmärka/dela. Samma resonemang gäller rimligen sökfältet och kategori-filtret på samma sida - ta ställning till om de ska med i samma mekanism.

- ID: `01M0CQJMJ82SZJQWQE73XDM6P0`
- Type: improvement
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

