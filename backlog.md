# Backlog Export

## [P1][done] [flipp] Pollen håller skrivlåset medan omslag hämtas över nätet

Observerat i drift 2026-08-20: knappen Import existing files svarar 500 efter exakt 34,5 sekunder, upprepningsbart. Det är SQLite-anslutningens 30-sekunders lock-timeout plus arbetet - alltså database is locked, inte ett fel i importen.

Orsaken är att hela poll_publications körs i EN get_session-transaktion, och _cache_covers ligger inuti den. Den gör upp till ISSUE_COVER_BACKFILL_PER_POLL externa bildhämtningar (nu 2500) medan transaktionen redan skrivit via create_job och sync_publications. Skrivlåset hålls därmed under hela hämtningen, som tar många minuter, och varje annan skrivare - import, manuell nedladdning, cancel, watch - blockeras tills den ger upp.

Problemet fanns latent med 500 omslag och blev fem gånger värre när taket höjdes. Samma familj som TASK-1340, där progress-skrivningar under nedladdning låste databasen.

Acceptanskriterier:
- Omslagshämtningen håller inte skrivlåset medan den väntar på nätverket. Hämta utanför transaktionen, eller committa löpande i små steg.
- Ett samtidighetstest visar att en annan session kan skriva medan omslag hämtas. Testet ska falla mot nuvarande kod.
- Pollens övriga arbete - jobbrad, synk, köläggning - fungerar som förut.
- Importknappen svarar med sin rapport i drift efteråt.

Filer som väntas ändras: flipp_dl/scheduler.py, flipp_dl/db/repository.py, tests/test_scheduler.py.

- ID: `01M0G0KFA693DC1DAGTDYMGKJB`
- Type: bug
- Actor: ai:claude-opus-5

---

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

## [P2][done] [flipp] Notifiera vid ihållande fel, inte bara vid nedladdningar

## Context

Notiser skickas i dag BARA från nedladdningskön, via
`_send_download_notifications` (scheduler.py:223, anropad på rad 682). Allt
annat som går fel är tyst:

- **Token går ut.** Pollningen svarar 401, `poll_publications` fångar
  FlippError, markerar jobbet som error och returnerar (scheduler.py:428-431).
  Sedan kommer inga nya utgåvor. Ingen får veta. Det här är det verkliga
  scenariot - en Flipp-token håller inte för evigt.
- **Komga-synk misslyckas.** 27 sådana låg i jobbloggen 2026-08-22.
- **Utgåveupptäckt och import** kan falla utan att någon märker det.

Kanalerna finns och är verifierade i drift (ntfy och webhook). Det som saknas
är att fler ställen än nedladdningen använder dem.

## Acceptance criteria

- [ ] En pollning som misslyckas skickar en notis - men bara första gången
      efter att den senast lyckats, inte vid varje tick.
- [ ] När pollningen lyckas igen efter att ha misslyckats skickas en notis om
      att det är löst.
- [ ] Ett misslyckat komga_sync-jobb och en misslyckad utgåvekörning
      notifieras enligt samma tillståndsregel.
- [ ] Felnotiser går ut oavsett publikationernas notify_enabled - den flaggan
      styr nedladdningsnotiser, och ett fel hör inte till en publikation.
- [ ] Inga notiser alls när ingen kanal är konfigurerad, och ingen krasch.
- [ ] En enstaka misslyckad nedladdning som köas om automatiskt notifieras
      INTE - den är inte ett fel förrän omförsöken tagit slut.

## Implementation hints

- `build_notify_channels(resolve_notify_settings(session_factory))` ger
  kanalerna; `send_all(channels, title, message)` skickar utan att kasta.
- Tillståndet måste överleva omstart: lägg det i settings-tabellen via
  `repo.get_setting`/`set_setting`, till exempel en nyckel per feltyp som
  håller om senaste körningen misslyckades.
- Se `~/workspace/infra/docs/ntfy-notifieringspolicy.md` för regeln om
  tillståndsövergång vid pollande checkar - det är precis det här fallet.
- Meddelandena ska vara begripliga på svenska för mottagaren, och gå genom
  samma väg som nedladdningsnotiserna (som redan är svenska).

## Verification

- `.venv/bin/python -m pytest tests/test_scheduler.py -q` - lägg tester som
  faller mot dagens kod: en misslyckad pollning notifierar en gång, en andra
  misslyckad pollning notifierar inte igen, och en efterföljande lyckad
  notifierar om att det är löst.
- `grep -n "send_all" flipp_dl/scheduler.py` - ska visa anrop från fler
  ställen än nedladdningskön.
- Manuellt: sätt en ogiltig token i en dev-instans, kör en pollning via
  knappen i Inställningar och kontrollera att en notis kommer fram i ntfy.
  Kör pollningen igen och kontrollera att INGEN andra notis kommer.

- ID: `01M0NEF2MGW7PEDZNBWACCWYGT`
- Type: feature
- Actor: ai:claude-code

---

## [P2][done] [flipp] Utgåvor som bara har ett datum som namn får datumet två gånger i filnamnet

Rasmus 2026-08-22: utgåvorna som PageSuite-upptäckten hittar heter bara ett datum, så filnamnet upprepar datumet i två format:

  /output/91an/91an - 2020-02-20 - 20-02-2020.pdf

PageSuite sätter edition name till "20/02/2020", och issue_path bygger "<publikation> - <issue_date> - <issue_name>.pdf" medan safe_name gör om snedstrecken till bindestreck.

Omfattning (mätt i en torrkörning av --discover-editions mot en kopia av produktionsdatabasen 2026-08-21): ALLA 1056 upptäckta utgåvor har ett namn på formen DD/MM/YYYY. Ingen av dem har ett riktigt utgåvenummer, för det finns inte i PageSuites data.

Förslag: när utgåvenamnet bara är samma datum som issue_date, utelämna namndelen helt - "91an - 2020-02-20.pdf". Vilket format som helst som är entydigt duger, men jämförelsen måste tåla båda skrivsätten (DD/MM/YYYY mot YYYY-MM-DD).

TIMING: det här bör helst göras INNAN de upptäckta utgåvorna laddas ner, annars måste 800+ redan hämtade filer döpas om med --migrate-filenames. Görs det efteråt: verifiera att migreringen klarar dem och att databasens file_path följer med.

Klart när: en utgåva vars namn bara är dess datum får ett filnamn utan upprepning. Test i tests/test_storage.py.

- ID: `01M0K821CYVB4AE6GCX12HQPVP`
- Type: improvement
- Actor: ai:claude-code

---

## [P2][done] [flipp] En Komga-scan per tömning av kön, inte en per utgåva

run_komga_sync_queue skapar ett komga_sync-jobb per färdig nedladdning, och varje jobb triggar en scan av HELA biblioteket och pollar sedan upp till 10 sekunder (KOMGA_WAIT_SECONDS) efter just sin bok.

För en eller två nya utgåvor per pollning är det oproblematiskt. För en bakkatalogshämtning är det illa: 809 nedladdningar ger 809 scan-triggningar, och väntan körs i serie - i värsta fallet drygt två timmar där tickan bara pollar medan Komga scannar om och om igen.

Att göra:
- Trigga scan en gång per bibliotek och tömning, inte per jobb. Alla jobb i kön vid tömningens start rör filer som redan ligger på disk, så en scan täcker dem.
- Behåll metadatapushen per bok - den är per utgåva och ska så vara.
- Hantera att en bok inte hunnit indexeras: trigga om scan och vänta en gång till innan jobbet räknas som misslyckat, annars faller de första jobben medan scanningen fortfarande pågår.

Klart när: en tömning med N jobb mot samma bibliotek gör en scan-triggning, inte N. Verifiera med ett test som räknar anropen mot en fejkad KomgaClient.

- ID: `01M0K5Z7EW8CDSJNZNJ9F754HR`
- Type: improvement
- Actor: ai:claude-code

---

## [P2][done] [flipp] Importera de olistade publikationerna (pubids ur olistade-utgavor.json)

## Context
docs/olistade-utgavor.json innehaller olistade UTGAVOR vars publication_code (pubid) tillhor 27 publikationer som INTE finns i den listade 91-katalogen (docs/alla-publikationer.json). Verifierat 2026-08-21: alla testade svarar mot editionshtml5_json med fulla bakkataloger (Hjemmet Bilag 304 utgavor, Her og Na TV 450, Bornytt 35, Disney Nyheter 27 ...). 12 av 27 gav 1106 utgavor.

Alltsa: signin-katalogen ger 91 listade publikationer, men vi har 27 TILL vars pubid ar kand och fungerar oppet. Tillsammans 118 publikationer atkomliga utan inloggning.

## Vad som ska goras
- Extrahera de 27 distinkta olistade publikationerna (pubid + namn) ur docs/olistade-utgavor.json till en importfil (t.ex. docs/olistade-publikationer.json), namn + customPublicationCode.
- Lat --import-catalog aven ta den filen (eller kor den separat), sa publikationerna laggs till i DB.
- Markera dem delisted direkt (de ar inte i den officiella katalogen) sa de inte forvaxlas med listade - men behall dem sa --discover-editions plockar deras utgavor.
- Sedan tacker flipp-dl 118 publikationer i stallet for 91.

## Grans
Ingen oppen upprakning av pubids finns. De 91 kommer fran signin, de 27 fran Wayback-utgavejakten. Publikationer vi aldrig sett en pubid for kraver fortfarande en extern upptacktskalla. Se doc 01M0GM0G.

- ID: `01M0JAP1P586MHBG16E6DJ0DT3`
- Type: feature
- Actor: ai:claude-code

---

## [P2][done] [flipp] Dedupa publikationer via publicationCode i stallet for namn



## Status 2026-08-21: data-lagret byggt (commit 2a689d7)
KLART: publication_code lagras (kolumn + migration 0015 + index), backfillas via
repository.backfill_publication_codes fran docs/alla-publikationer.json vid
--import-catalog. 91 koder satta. De tva Hjemmet skiljs nu at: DK-HJM / NO-HJE,
synligt i alla queries och display.

KVAR (medvetet inte byggt an - incidentkanslig folder-logik): auto-disambiguera
MAPPNAMN via publication_code vid kollision, i stallet for att pausa bevakning
(_disable_watched_folder_collisions). Risk: att auto-doma om folder_name pa en
publikation som redan har nedladdningar i t.ex. Hjemmet/ foraldralosar de filerna
(samma klass av problem som TASK-1404). Sakert monster nar det byggs: bara
auto-satt "namn (publication_code)" for kollisioner dar ingen av parterna annu
har nedladdningar; annars behall pausa-och-fraga men foresla namn+kod. Kraver
egen genomtankt PR med tester mot befintliga nedladdningar.

- ID: `01M0J5SM8SX6M0WY5EX3WDG9AM`
- Type: improvement
- Actor: ai:claude-code

---

## [P2][done] [flipp] Upptäck utgåvor via PageSuites utgåvelista



## Bevara historiska publicationCodes (aldrig hard-radera)
Nar en publikation forsvinner ur signin-svaret: markera delisted, ta ALDRIG
bort raden. publicationCode + customPublicationCode (= pubid) ar oersattliga -
sa lange vi har dem kan utgavorna fortfarande hamtas via editionshtml5_json
(oppet, kan svara aven for avlistade pub, precis som olistade utgavor).
Samma princip som TASK-1438 for utgavokoder, fast pa publikationsniva. Aterbruka
delisted-monstret fran _mark_missing_publications_delisted.

- ID: `01M0HQZ9JV1NENJVASB3T6RN6T`
- Type: feature
- Actor: ai:claude-code

---

## [P2][done] [flipp] Bevara alla utgåvokoder vi någonsin sett

## Context
1916 utgåvor har redan fallit ur Flipps listning, och deras koder finns bara i vår databas. Det finns ingen väg att lista dem på nytt - det är utrett och besvarat. Faller en kod bort innan vi hunnit se den är den oåtkomlig för alltid, eftersom uppslaget av sidor kräver att man redan känner koden.

Vi ser bara det Flipp listar just nu. Varje poll är en ögonblicksbild av något som krymper, och den enda som bevarar den är vi.

## Beslutat av Rasmus 2026-08-20
Det är KODERNA som ska sparas, inte hela råsvaret. Resten - namn, beskrivningar, omslagsadresser, kategorier - finns redan i databasen och är återskapbart. Koden är det enda oersättliga.

Det gör uppgiften mindre och tryggare: ingen 3,7 megabyte per poll, ingen token att rensa bort, ingen e-postadress att råka spara.

## Acceptance criteria
- [ ] Varje publikationskod och utgåvokod som setts i ett poll-svar bevaras, även när utgåvan senare försvinner ur listningen och även om raden i issues-tabellen skulle rensas.
- [ ] Lagringen innehåller ingen token och inga personuppgifter. Bara koder, med tidpunkt för när de först och senast sågs.
- [ ] Det går att se hur många koder som bevarats och när de senast sågs.
- [ ] Ett tomt eller misslyckat svar bevarar ingenting nytt och tar inte bort något.
- [ ] Bevarandet överlever att databasen byggs om - avgör om det räcker med en egen tabell eller om koderna också ska kunna exporteras till fil, och motivera.

## Att tänka igenom
Utgåvorna finns redan i issues-tabellen, som aldrig rensas i dag. Frågan är alltså vad detta tillför utöver det: skyddet mot att någon framtida rensning, migrering eller ombyggnad tar bort koderna av misstag. Väg det mot att bygga en parallell struktur som kan komma i otakt med issues - motivera valet i rapporten.

Formatet i docs/olistade-utgavor.json är redan etablerat för koder som hittats utanför API:t. Överväg samma format, så att det som bevaras och det som importeras (TASK-1437) talar samma språk.

## Verification
- Tester: koder bevaras vid poll, försvinner inte när utgåvan slutar listas, tomt svar ändrar inget.
- Kontroll mot driftinstansen: antalet bevarade koder stämmer med de 17673 utgåvor databasen känner till.

Detta var spår 5 i TASK-1436 och bröts ut hit eftersom det är ett bygge, inte en utredning.

- ID: `01M0GFG2B5TRQ8VFT1GE9DC73J`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Utgåvor faller ur Flipps listning - visa och bevara dem

## Context
Utgåvor slutar listas av Flipp över tid, inte bara publikationer. Mätt 2026-08-20:

- databasen minns 17673 utgåvor
- Flipp listar just nu 15757
- skillnad: 1916 utgåvor i 21 av 94 publikationer

Värst drabbade: Her og Nå (478 av 955 borta ur listningen), Hjemmet DK (369), Hjemmet NO (318), Norsk Ukeblad (211), Hendes Verden (198).

De är sannolikt fortfarande hämtbara. Samma mekanism som för olistade publikationer (TASK-1426): get_page_groups_from_eid slår upp en utgåva utan att fråga vad kontot erbjuds, så länge utgåvans kod finns kvar. Kontrollerat för en olistad publikation, Stitch nr 3 2026, som gav 44 sidor.

Det gör databasens minne värdefullt: koderna för de 1916 utgåvorna finns bara hos oss. Rensas de går de inte att få tillbaka.

## Acceptance criteria
- [ ] En utgåva som inte längre listas markeras, på samma sätt som publikationer i TASK-1426, med tidpunkt för när den senast sågs.
- [ ] Markeringen syns på publikationens detaljsida och går att filtrera på.
- [ ] Ordvalet säger att den inte längre listas, inte att den är otillgänglig - den går att hämta.
- [ ] sync_publications fortsätter att aldrig radera utgåvor.
- [ ] Ett tomt eller partiellt svar markerar aldrig allt på en gång.
- [ ] Verifiera FÖRST att en olistad utgåva faktiskt går att ladda ner, och skriv in resultatet i tasken. Om den inte går att hämta ändras hela premissen.

## Att tänka igenom
- 1916 markeringar är mycket. Ska de visas per utgåva, eller sammanfattas per publikation?
- Antalet lär växa. Är det värt att kunna hämta hem allt olistat innan det försvinner? En sådan knapp vore stor - notera det, bygg inte utan beslut.

## Verification
- Tester: utgåva försvinner ur svaret och markeras, dyker upp igen och avmarkeras, tomt svar markerar inget, ingen radering.
- Kontroll mot driftinstansen: de 1916 markeras och antalet stämmer med mätningen ovan.

## Premissen är verifierad (2026-08-20)
En olistad UTGÅVA testades skarpt, inte bara en olistad publikation:
Her og Nå, utgåva 5ea9f7f8-fb04-4e81-a03b-e14d5addd48c ("2026-32 - bilag", 2026-06-22), som finns i databasen men inte i Flipps aktuella lista.

  get_page_groups_from_eid -> HTTP 200, 64 sidor
  första sid-PDF:en        -> HTTP 200, 631660 byte

Utgåvan går alltså att ladda ner i sin helhet. Premissen håller.

- ID: `01M0GEE2VHXYS9GZWYDSYMC7JJ`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Låt användaren särskilja publikationer som delar katalognamn

Två publikationer kan heta exakt samma sak och får då samma katalog på disk. Det finns i drift i dag: "Hjemmet" är två skilda publikationer, en norsk och en dansk. Filerna hamnar i samma mapp och ägarskapet blir tvetydigt - importen kan inte avgöra vilken publikation en fil hör till, och sedan TASK-1404 vägrar den därför backfilla dem, vilket är rätt men inte en lösning.

Beslutat av Rasmus 2026-08-20: särskiljningen är användarens val, inte systemets gissning. När man börjar bevaka en publikation vars katalognamn krockar med en annan ska man få frågan och ange ett eget namn - exempelvis "Hjemmet (DK)" och "Hjemmet (NO)".

## Acceptance criteria
- [ ] En publikation kan ha ett eget katalognamn som användaren sätter, skilt från namnet Flipp levererar.
- [ ] När bevakning slås på för en publikation vars katalognamn krockar med en annan publikations, efterfrågas ett eget namn innan något laddas ner.
- [ ] Namnet valideras med samma regler som övriga filnamn (safe_name, OS-säkerhet enligt TASK-1400) och får inte krocka med en annan publikations katalog.
- [ ] publication_folder använder det egna namnet när det finns, annars publikationens namn som förut.
- [ ] Redan nedladdade filer flyttas när ett eget namn sätts, och file_path uppdateras - eller så beskrivs uttryckligen varför de lämnas kvar.
- [ ] Importen slutar rapportera de berörda filerna som tvetydiga när namnen väl är åtskilda.

## Att tänka igenom
- Krocken kan uppstå senare: två publikationer med olika namn kan byta namn så de sammanfaller vid nästa poll. Vad händer då?
- Ska frågan även kunna ställas i efterhand, för de som redan är bevakade? I drift gäller det Hjemmet, som redan finns.

## Verification
- Tester: krock vid bevakning, valideringen, att publication_folder följer det egna namnet, och att importen blir ren efteråt.
- Kontroll mot driftinstansen: de två Hjemmet-publikationerna får skilda kataloger och importknappen rapporterar inga tvetydigheter.

- ID: `01M0G7VDH6EVM6AHT9GB0A7YM1`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Kör om felade nedladdningar automatiskt med backoff

En utgåva som felar stannar som error för alltid. Poll-påfyllningen hoppar medvetet över felade (annars skulle en permanent trasig utgåva köas om var sjätte timme), så enda vägen tillbaka är ett manuellt klick på Retry eller Watch. Övergående fel - nätverksglapp, en låst databas, Flipp som svarar konstigt - läker därmed inte av sig själva.

Driftinstansen har just nu ett sådant jobb liggande sedan juni: en nedladdning som föll på "database is locked" och blev kvar som fel trots att felorsaken är åtgärdad sedan dess.

Acceptanskriterier:
- Ett felat nedladdningsjobb körs om automatiskt ett begränsat antal gånger med växande fördröjning, exempelvis tre försök.
- Antal försök och nästa försökstidpunkt syns på jobbet, så det går att se skillnad på "väntar på omförsök" och "gav upp".
- När försöken tagit slut stannar utgåvan som error och köas inte om av sig själv - det ska fortfarande krävas ett medvetet klick.
- Fel som uppenbart inte är övergående bör inte kosta tre försök om det går att skilja dem åt. Motivera vilken uppdelning som valdes.

Verifiering: riktade tester i tests/test_scheduler.py som simulerar ett fel och kontrollerar att omförsöket sker, att fördröjningen växer, och att det slutar efter taket.

- ID: `01M0DWYFFR7VNEX97CR2HGBXY7`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Visa storleksuppskattning innan en bulk-köläggning

Knappen "Queue missing issues" och Watch säger hur många utgåvor som köas, men inte vad det väger. Med 49 MB som snitt blir 200 utgåvor cirka 10 GB, vilket är värt att veta före klicket.

Underlaget finns nu: 1092 nedladdade filer att räkna median eller snitt på, per publikation där det går och globalt annars. Den ursprungliga TODO:n avbokade en liknande idé, men då fanns ingen nedladdad data att basera uppskattningen på.

Acceptanskriterier:
- Bekräftelsedialogen för bulk-köläggning visar antal utgåvor OCH uppskattad storlek.
- Uppskattningen bygger på faktiska filstorlekar, i första hand för samma publikation, med global median som fallback när publikationen saknar nedladdade filer.
- Saknas underlag helt visas antalet utan storlek, inte en påhittad siffra.
- Kräver att filstorlek finns tillgänglig per utgåva - avgör om den ska läsas från disk vid behov eller sparas i databasen vid nedladdning, och motivera valet.

Verifiering: riktade tester för uppskattningen inklusive fallback-fallet, plus browser-verifiering av dialogen vid 390px och 1280px.

Hänger ihop med tasken om att begränsa bakkatalogen - de rör samma klick.

## Används av TASK-1361
Uppskattningen är förutsättningen för tröskelvarningen när hela bakkatalogen hämtas (5 GB som default). Den här tasken bör därför göras först - utan en siffra finns inget att varna på. Uppskattningen ska gå att anropa för en publikation utan att rendera något, så både dialogen och varningen kan använda samma väg.

- ID: `01M0DWXHZJCQ6JYRVXD6KJ9NXA`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P2][done] [flipp] Skilj på att bevaka framåt och att hämta hela bakkatalogen

Watch köar i dag ALLT som inte är nedladdat, och poll fyller på med samma logik. Det är sällan vad man vill: driftinstansen har 16568 utgåvor som inte är nedladdade, snittet är 49 MB per utgåva (1092 filer väger 53,2 GB), så en full backfill är i storleksordningen 800 GB. Att kryssa Watch ska inte kunna starta det av misstag.

Beslutat av Rasmus 2026-08-20: lösningen är INTE att bara strypa Watch, utan att göra valet explicit med skilda knappar. Bakkatalogsknappen ska dessutom varna när uppskattningen överstiger en tröskel, visa beräknad storlek, och kräva en bekräftelse.

## Acceptanskriterier

- Watch bevakar framåt: nya utgåvor som upptäcks vid poll köas, bakkatalogen rörs inte. Det blir en ofarlig knapp.
- En egen knapp hämtar bakkatalogen. Den befintliga "Queue missing issues" på publikationens detaljsida är den naturliga platsen - den ska då sluta vara en tyst variant av samma sak och i stället bli det uttryckliga valet.
- Överstiger uppskattningen tröskeln visar bekräftelsedialogen antal utgåvor OCH beräknad storlek, och kräver ett aktivt godkännande. Under tröskeln räcker dagens bekräftelse.
- Tröskeln har ett rimligt default (5 GB, motsvarar drygt hundra utgåvor med dagens snitt) och går att ändra via inställning eller env.
- Poll-påfyllningen följer samma uppdelning: den fyller på utgåvor som upptäckts sedan bevakningen slogs på, inte hela bakkatalogen. Annars smyger den in ändå vid nästa poll.
- Befintliga bevakade publikationer påverkas inte i tysthet av utrullningen - beskriv i implementationen vad som händer med dem och varför.

## Implementation hints

Filer som väntas ändras: flipp_dl/db/repository.py (queue_missing_issues tar gränsen som parameter), flipp_dl/db/models.py, flipp_dl/scheduler.py, flipp_dl/web/routes.py, flipp_dl/web/templates/publication_detail.html, flipp_dl/web/templates/publication_row.html, tests/test_repository.py, tests/test_scheduler.py, tests/test_web_routes.py. Sannolikt en ny Alembic-revision efter 0008_komga_read_status om bevakningsstarten behöver lagras.

Bekräftelsedialogen går redan genom den egna modalen i base.html, som läser texten ur hx-confirm via htmx:confirm. En storleksberoende text kräver att attributet sätts serverside eller att modalen får hämta uppskattningen - avgör vilket och motivera.

BEROENDE: storleksuppskattningen kommer från TASK-1362, som därför bör göras först. Utan den finns ingen siffra att varna på.

## Verification

- pytest tests/test_repository.py tests/test_scheduler.py tests/test_web_routes.py -q, med tester för båda knapparna, tröskeln över och under, och att poll inte drar in bakkatalogen.
- Browser: klicka BÅDA knapparna vid 390px och 1280px, kontrollera att varningen visas över tröskeln med rätt siffror och att avbryt inte köar något. Rendering är inte verifiering.

- ID: `01M0DWXHZ3V1T3XH4JKDC8QF2Z`
- Type: feature
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

## [P3][done] [flipp] Köa Komga-synk på nytt för nedladdade utgåvor som saknar metadata

## Context

TASK-1481 gör att ett `komga_sync`-jobb vars bok inte hunnit indexeras köas
om i stället för att dö som `error`. Det räddar framtida jobb men rör inte
det som redan finns i drift:

- 1247 nedladdade utgåvor saknar metadata i Komga för att de aldrig fick
  ett `komga_sync`-jobb alls. Jobb skapas bara när ett download-jobb blir
  klart (`flipp_dl/scheduler.py:735`), och bakkatalogen hämtades innan
  Komga slogs på.
- De 27 jobb som står som `error` är terminala. `schedule_job_retry`
  triggar bara på ett fel som inträffar nu, så ingenting väcker dem.

Utan den här tasken sjunker inte error-siffran och `komga_book_id`-antalet
stiger inte, oavsett hur bra omköandet fungerar.

## Beslutat upplägg (bygg detta, designa inte om)

En **explicit engångsåtgärd**, inte en automatisk backfill: en knapp i
Komga-avsnittet på Inställningar som köar `komga_sync`-jobb för de utgåvor
som saknar metadata. Anledningen till att det inte får bli automatiskt: en
publikation som aldrig matchar en Komga-serie skulle annars få ett nytt
jobb var 30:e sekund för evigt. Att fråga jobbtabellen "har utgåvan
någonsin haft ett synkjobb" håller inte heller, eftersom `purge_old_jobs`
raderar klara jobb efter 30 dagar.

Urvalet: utgåvor med `status = done`, `file_path` satt och
`komga_book_id is null`. Hoppa över en utgåva som redan har ett
`komga_sync`-jobb i `queued`, `running` eller `retry_pending` - det är den
dubblettspärr som krävs. Ett gammalt `error`- eller `done`-jobb för samma
utgåva ska INTE spärra: hela poängen är att ge de 27 döda jobben en ny
chans, och ett nytt jobb är den vägen (försök inte återuppliva de gamla
raderna).

Tak per körning: `KOMGA_BACKFILL_LIMIT = 500` jobb, så en knapptryckning
aldrig lägger tiotusen rader på en gång. Returnera hur många som köades
och hur många som återstår, och skriv ut det i svaret så Rasmus vet om han
ska trycka igen.

Ingen ny kolumn, ingen migration.

## Icke-mål

- Ingen automatisk/schemalagd backfill.
- Ingen CLI-flagga (knappen räcker; appen körs i Docker och Rasmus använder
  webb-UI:t).
- Rör inte `run_komga_sync_queue` eller retry-logiken från TASK-1481.
- Ingen omdesign av `komga_series_id`-matchningen.

## Acceptance criteria

- [ ] Nytt repo-metod `queue_komga_backfill(library_id, limit)` i
      `flipp_dl/db/repository.py` (Jobs-sektionen, nära `create_job`) som
      köar `komga_sync`-jobb med payload `{"library_id": ..., "issue_id": ...}`
      för utgåvor som är `done`, har `file_path` och saknar `komga_book_id`,
      och returnerar `(antal_köade, antal_återstående)`.
- [ ] Utgåvor som redan har ett `komga_sync`-jobb i `queued`, `running`
      eller `retry_pending` hoppas över. Ett tidigare `error`/`done`-jobb
      spärrar inte.
- [ ] Högst `limit` jobb per anrop.
- [ ] Ny POST-route `/settings/komga/backfill` i `flipp_dl/web/routes.py`
      (bredvid `komga_test_connection`, ca rad 1447) med samma
      CSRF-kontroll (`check_csrf_form`) som grannrouterna, som anropar
      metoden och renderar ett svar med antalet köade och återstående.
      Utan sparat `komga_library_id` ska den svara med ett begripligt
      besked på svenska/engelska i stället för att köa något.
- [ ] Knapp i `flipp_dl/web/templates/settings.html` i Komga-avsnittet
      (efter `komga-test-btn`, ca rad 278-291), samma htmx-mönster:
      `hx-post`, `hx-target` mot en egen resultat-div, `hx-indicator`,
      `hx-vals` med `_csrf_token`.
- [ ] Nya UI-strängar finns översatta i den svenska katalogen och den
      kompilerade `.mo`-filen är uppdaterad.

## Implementation hints

- Jobbstatusar: `JobStatus` i `flipp_dl/db/models.py:69` (inkl. den nya
  `RETRY_PENDING`). Utgåvestatus: `IssueStatus`, `DONE`.
- `DbJob.payload` är en JSON-sträng; issue-id plockas ut som i
  `reset_stuck_download_jobs` (`repository.py:1614`) och
  `cancel_issue`. Läs payloads för aktiva `komga_sync`-jobb EN gång och
  bygg ett set, inte en fråga per utgåva.
- CSRF: se `komga_test_connection` (`routes.py:1447`) för mönstret, och
  `_repo(request)` + `repo.session.close()`.
- Renderingen kan återanvända ett litet partial i
  `flipp_dl/web/templates/` (t.ex. `komga_backfill_result.html`) - kolla
  hur `komga_library_select.html` är byggd.
- Översättning: kommandona står i README.md under "Webbgränssnitt -
  Översättningar" (`pybabel extract` / `update` / redigera `.po` /
  `compile`). `.venv/bin/pybabel` finns installerad. Glöm inte
  `pybabel compile`.

## Verification

- `.venv/bin/python -m pytest tests/test_repository.py tests/test_web_routes.py -q`
  ska vara grön, med nya tester som täcker:
  - en utgåva utan `komga_book_id` får ett jobb, en med hoppas över
  - en andra körning direkt efter den första köar 0 (dubblettspärren)
  - en utgåva vars enda tidigare jobb är `error` FÅR ett nytt jobb
  - `limit` respekteras och återstående-siffran stämmer
  - routen: POST utan giltig CSRF ger 400, POST utan sparat bibliotek köar
    inget
- `.venv/bin/ruff check flipp_dl tests` utan nya fel.
- `grep -c "msgstr \"\"" flipp_dl/web/locales/sv/LC_MESSAGES/messages.po`
  ska inte ha ökat för de nya strängarna (tomma msgstr = oöversatt).
- Manuellt/browser (görs av föräldern): starta appen, öppna
  `/settings`, klicka knappen vid 1280px och 390px och se att svaret
  visar antalet köade jobb.

- ID: `01M0NJ7G7SBSYJMQR0NGPGWWXA`
- Type: improvement
- Actor: ai:claude-code

---

## [P3][done] [flipp] Flytta filerna vid destinationsbyte, över volymgräns och omstartbart

## Context

Tasken skrevs som om flipp-dl inte kan flytta sina filer alls. Det stämmer
inte längre: `set_publication_folder_name` (repository.py:477) flyttar redan
filerna vid byte av mappnamn, en i taget, med rollback om något går fel.
Kvar står det som den flytten INTE klarar, plus det som aldrig byggdes.

Kvarvarande luckor, mätt i koden 2026-08-23:

- **Destinationsbyte vägras fortfarande.** `set_publication_destination`
  (repository.py:587) kastar `PublicationDestinationError` så fort någon
  utgåva är `done`. Ingen flytt sker.
- **Flytten klarar inte två volymer.** `source.replace(target)` i
  folder-flytten är ett rename och ger `OSError EXDEV` över en
  filsystemsgräns. Primär och sekundär rot är två Unraid-shares, så exakt
  det fallet är det som destinationsbytet behöver.
- **Flytten är inte omstartbar.** Rollbacken i `set_publication_folder_name`
  kör i minnet; dör processen mitt i finns ingen väg tillbaka och ingen väg
  vidare.
- **Ingen spärr mot pågående nedladdningar.** Byter man mapp medan ett
  download-jobb kör skriver jobbet till den gamla katalogen.

## Icke-mål

- Skriv inte om `set_publication_folder_name` från grunden. Bygg ut den, och
  återanvänd samma flytt för destinationsbytet.
- Ingen ny UI-vy. Befintliga formulär för mappnamn och destination räcker;
  felmeddelanden finns redan (`PublicationFolderMoveError` m.fl.).
- Rör inte `--migrate-filenames` i cli.py. Läs den gärna som mönster, men
  den ligger utanför uppgiften.
- Ingen ny kolumn om du kan undvika det. Behövs en, motivera i rapporten.

## Acceptance criteria

- [ ] En publikation med nedladdade utgåvor kan byta destination mellan
      primär och sekundär rot, och filerna följer med. `file_path` pekar
      rätt efteråt. `PublicationDestinationError` kastas inte längre bara
      för att utgåvor är nedladdade.
- [ ] Flytten fungerar över en filsystemsgräns: när rename ger `EXDEV`
      kopieras filen, kopian verifieras (storlek räcker som kontroll) och
      först därefter raderas källan.
- [ ] En avbruten flytt lämnar aldrig databasen pekande på en fil som inte
      finns. Varje utgåva är antingen flyttad och bokförd, eller orörd.
- [ ] Flytten går att köra om efter ett avbrott och fortsätter där den var -
      en fil som redan ligger på målplatsen räknas som klar, inte som en
      krock.
- [ ] Ett byte vägras med ett begripligt fel medan publikationen har ett
      download-jobb i `queued`, `running` eller `retry_pending`.
- [ ] Både mapp- och destinationsbytet returnerar hur många filer som
      flyttades och vilka som inte gick, och webblagret visar det.

## Implementation hints

- `storage.publication_folder` och `storage.destination_root` avgör var en
  fil ska ligga. Skillnaden mot var den ligger är arbetslistan.
- Sekundärroten läses ur inställningen `secondary_output_root` (se hur
  `run_download_queue` i scheduler.py plockar upp den).
- Aktiva jobb: `DbJob` med `job_type="download"` och status i
  `queued`/`running`/`retry_pending`, issue-id i payloaden. Se
  `list_active_download_jobs` och `reset_stuck_download_jobs` för mönstret
  att läsa payloads en gång i stället för en fråga per utgåva.
- Tiotals gigabyte per publikation: kopiera i block, inte via `read_bytes`.

## Verification

- `.venv/bin/python -m pytest tests/test_repository.py tests/test_web_routes.py -q`
- Testerna ska täcka: flytt inom samma rot, flytt mellan två rötter,
  EXDEV-fallet (monkeypatcha `Path.replace` så den kastar
  `OSError(errno.EXDEV)` och kontrollera att kopiera-verifiera-radera
  används), avbrott mitt i (låt andra filen faila och kontrollera att
  databasen är konsistent), omstart efter avbrott, och att ett byte vägras
  när ett download-jobb är aktivt.
- `.venv/bin/ruff check flipp_dl tests`
- Manuellt (görs av föräldern): byte i gränssnittet på en publikation med
  några utgåvor, och kontroll att filerna ligger på den nya platsen.

- ID: `01M0NEF2N154B8SH51N8JT95AW`
- Type: feature
- Actor: ai:claude-code

---

## [P3][done] [flipp] Köa om Komga-synkar som kom före indexeringen

## Context

Mätt i drift 2026-08-22: 27 komga_sync-jobb står som `error`, alla med
"Book for issue ... not found in Komga series ... after waiting 10s".
Följden är att 771 av 2018 nedladdade utgåvor fått metadata pushad till
Komga - resten inte. Bara 16 av 23 publikationer har ett cachat
`komga_series_id`.

Komga scannar asynkront. TASK-1458 gav varje tömning en scan och lade till
ett extra försök, vilket hjälpte, men tio sekunder räcker inte när Komga
samtidigt hashar tusentals filer efter en bakkatalogshämtning.

En bok som inte hunnit indexeras är inte ett permanent fel utan ett för
tidigt försök.

## Acceptance criteria

- [ ] Ett komga_sync-jobb vars bok inte hittats köas om senare i stället för
      att markeras error.
- [ ] Omförsöken har ett tak; när det nås markeras jobbet error som i dag.
- [ ] Riktiga fel (Komga nere, fel bibliotek, auth) köas INTE om utan
      markeras error direkt.
- [ ] En publikation vars serie ännu inte matchats fortsätter räknas som
      "inte ett fel" - det beteendet finns redan och ska inte ändras.

## Implementation hints

- `_is_book_missing(message)` och konstanten `_BOOK_NOT_INDEXED` i
  scheduler.py gör redan skillnaden mellan "inte indexerad än" och annat.
- Mönstret för fördröjda omförsök finns för nedladdningar: `retry_pending`
  som status, `next_retry_at`, `MAX_AUTO_RETRIES` och `RETRY_DELAYS_MINUTES`
  i repository.py, plus `requeue_due_retries()` som körs i början av
  nedladdningstömningen.
- Jobbtabellen har ingen retry-kolumn; räknaren kan ligga i jobbets payload,
  som redan används för progress och resultat i utgåvekön.

## Verification

- `.venv/bin/python -m pytest tests/test_scheduler.py -q` - testerna ska
  falla mot dagens kod: ett jobb vars bok saknas köas om i stället för att bli
  error, och ett jobb med ett riktigt Komga-fel blir error direkt.
- Manuellt i drift efter utrullning: `select status, count(*) from jobs where
  job_type='komga_sync' group by status` - andelen error ska sjunka, och
  `select count(*) from issues where komga_book_id is not null` ska stiga mot
  antalet nedladdade.

- ID: `01M0NEF2MQT2BTDRYV51MT0M74`
- Type: improvement
- Actor: ai:claude-code

---

## [P3][done] [flipp] README speglar inte vad branchen faktiskt kan

Rasmus 2026-08-22: README ska uppdateras för allt som byggts på claude/review-project-improvements-qdoid.

Tillkommet sedan README skrevs, i grova drag:
- Webb-UI för katalogimport, kodsäkerhetskopia (ner- och uppladdning), utgåveupptäckt och import av olistade utgåvor, allt under Inställningar.
- CLI: --import-catalog, --discover-editions, --import-editions, --export-codes, --import-backup, --migrate-filenames.
- Destination per publikation (sekundär utdatarot) och katalogväljare för sökvägsfältet.
- Notiser per publikation (opt-in), utöver den globala ntfy/webhook-inställningen.
- Kör pollning nu-knapp.
- Inloggning mot Flipp direkt från inställningarna (api/signin), konsolsnutten kvar som alternativ.
- Räknare för utgåvor Flipp slutat lista.
- docs/-filerna (alla-publikationer.json, olistade-publikationer.json, olistade-utgavor.json) och vad de används till.

Att kontrollera samtidigt:
- Miljövariabeltabellen: stämmer den fortfarande? FLIPP_POLL_INTERVAL, FLIPP_WORKERS, KOMGA_*, NTFY_*.
- Unraid-avsnittet säger Path 2 /mnt/user/Downloads/Flipp - i drift är det numera /mnt/user/media/Serier/Manuella/Flipp-dl.
- i18n-avsnittet: extract-kommandot måste lista codes_routes.py, vilket det numera gör.
- Att inget i README beskriver funktioner som inte finns.

- ID: `01M0N2BB28ZB8077ZSRVG5CERJ`
- Type: chore
- Actor: ai:claude-code

---

## [P3][done] [flipp] Ingen knapp triggar en pollning - routen finns men är oåtkomlig

POST /publications/{code}/poll finns och fungerar (verifierat 2026-08-22 genom att anropa den med fetch från en inloggad session: svarar 200 och "Polled ✓"), men INGEN mall anropar den. Det enda som nämner poll i publikationsvyn är formuläret för eget poll-intervall.

Följden: en pollning sker bara var sjätte timme, eller när containern startar om. Det märks särskilt efter en utgåveupptäckt, eftersom omslagen hämtas i pollningen - nyupptäckta utgåvor står utan omslag tills nästa tick.

Att göra:
- En knapp på publikationssidan som postar till routen, i samma HTMX-mönster som "Importera befintliga filer" i inställningarna (hx-post, hx-target, csrf i dolt fält).
- Fundera på om den ska polla ALLA publikationer eller bara den man står på. Routen tar en kod i sökvägen men anropar poll_publications, som pollar allt - det är förvirrande och bör antingen begränsas eller flyttas till en global knapp.
- Pollningen kör synkront i requesten och tar tiotals sekunder när omslagsbackfillen har mycket att göra. Antingen köa den som ett jobb (mönstret finns i editions-kön) eller visa en spinner och räkna med att svaret dröjer.

Klart när: en knapp i gränssnittet startar en pollning och man ser att den kört. Verifiera genom att klicka knappen, inte bara att den renderas.

- ID: `01M0KCE31BTB2292YYD4DMRY34`
- Type: bug
- Actor: ai:claude-code

---

## [P3][done] [flipp] Generera omslag ur PDF:en för utgåvor som saknar cover_url

Rasmus 2026-08-22: de upptäckta utgåvorna visar inget omslag. Vi har ju filen - borde kunna rendera och cachea första sidan.

Läget: cache_covers hämtar omslag från cover_url, som kommer från Flipp-API:et. Utgåvor som upptäckts via PageSuite har ingen cover_url alls, så de får aldrig något omslag. Mätt i torrkörningen 2026-08-21: alla 1056 upptäckta utgåvor saknar cover_cache_path.

Att utreda innan något byggs:
- Rendering kräver ett nytt beroende. requirements.txt har bara pypdf, som inte kan rastrera. PyMuPDF (fitz) är enklast - ett pip-paket, inga systembibliotek. pdf2image kräver poppler i imagen. Det påverkar Dockerfile och imagestorleken.
- Bara för utgåvor med status done och en fil som finns; övriga har inget att rendera ur.
- Var i flödet? Rimligen i cache_covers, som redan äger omslagscachen och kör en gång per pollning, med samma filnamnskonvention (issue-<kod>.jpg).
- Storlek och kvalitet: befintliga cachade omslag kommer från Flipps b600m-varianter, så sikta på motsvarande bredd.

Notera: Komga genererar sina EGNA miniatyrer ur filerna, så det här är för flipp-dl:s egna vyer, inte för Komga.

Klart när: en nedladdad utgåva utan cover_url får ett cachat omslag som syns i utgåvelistan. Verifiera i browser vid 390 och 1280 px.

- ID: `01M0K821D5CWF41Y3EED2JSJ09`
- Type: feature
- Actor: ai:claude-code

---

## [P3][done] [flipp] Logga in mot Flipp direkt från flipp-dl i stället för konsolsnutten

Rasmus 2026-08-21: i stället för att visa en JS-snutt att klistra i webbläsarkonsolen borde flipp-dl kunna logga in mot Flipp självt och hämta token.

Läget: inställningssidan har ett token-fält plus en utfällbar hjälp med snutten som läser flipp_token ur document.cookie på tidningar.flipp.se. Fungerar, men kräver att man är inloggad i rätt webbläsare och kan konsolen.

Att utreda innan något byggs:
- Vilket inloggningsflöde Flipp faktiskt använder. Går det att posta e-post och lösenord mot en endpoint och få token, eller sitter det bakom ett OAuth-/SSO-flöde med redirect? refreshsignintoken-anropet som debug-vyn redan gör är en ledtråd till vad token är värd, men inte till hur den skapas.
- Om det finns MFA, captcha eller enhetsbindning i vägen.
- Var lösenordet i så fall lagras. Databasen har redan hemligheter (Komga-lösenord, ntfy-token) så mönstret finns, men ett Flipp-lösenord är känsligare än en token som ändå går att förnya.
- Alternativ om direktinloggning inte går: en bookmarklet i stället för konsolklistrande, eller en webbläsarextension.

Konsolsnutten kan behållas som fallback oavsett.

- ID: `01M0JVVQJWSVZDJ8VXCZ4XGVH6`
- Type: feature
- Actor: ai:claude-code

---

## [P3][todo] [flipp] API i flipp-dl och prenly-dl så en orkestrerare kan styra båda

Rasmus 2026-08-21: flipp-dl och prenly-dl överlappar delvis - båda hämtar tidningar, båda har publikationer/utgåvor/jobb, och båda har utgåvor som hör hemma i Calibre snarare än Komga. Vore vettigt med API:er i båda så en orkestrerare kan styra dem gemensamt i stället för två separata gränssnitt.

Läget i dag:
- flipp-dl har flipp_dl/web/api_routes.py, men bara läsande JSON-vyer (publikationer, jobb). Inget skrivande API.
- prenly-dl har veterligen inget API alls (att kontrollera).

Att utreda innan något byggs:
- Vad ska orkestreraren faktiskt kunna? Starta pollning, köa nedladdning, läsa jobbstatus, styra destination? Det avgör om det räcker med läsande API plus ett fåtal kommandon.
- Gemensam form eller två olika? Publikation/utgåva/jobb liknar varandra men är inte identiska (prenly har sites och bilagor, flipp har bevakning och Komga).
- Auth: flipp-dl har HTTP Basic mot FLIPP_PASSWORD för /api och OPDS. prenly-dl behöver något motsvarande.
- Är orkestreraren en tredje tjänst eller räcker det att den ena kan anropa den andra?

Relaterat: TASK-1445 (destination per publikation i flipp-dl) och prenly TASK-1369 (Calibre-integration i prenly-dl) löser Calibre-halvan var för sig. Blir det två olika lösningar är det ett argument för att ta den här först.

- ID: `01M0JK3SAMNGXVMQFV69W8PVJ3`
- Type: spike
- Actor: ai:claude-code

---

## [P3][done] [flipp] Destination per publikation: Komga-serier vs rena tidningar

Alla publikationer hör inte hemma på samma ställe. Serietidningar (Bamse, Fantomen, Kalle Anka) hör hemma i Komga; rena tidningar utan seriekaraktär (Scandinavian Retro, Pyssla med prinsessorna, Djurliv) passar bättre i ett Calibre-bibliotek.

Läget i dag: en enda output-rot för allt, och publikationens enda placeringsval är folder_name (migration 0012).

Att göra:
- Ett val per publikation för vart utgåvorna hamnar. Samma ställe som folder_name, dvs en kolumn på publications plus ett fält i publikationsvyn.
- Minst två destinationer, konfigurerbara: en Komga-rot och en för resten.
- Nedladdaren skriver till den valda roten, och import-existing/migrate-filenames måste kunna hitta filer i båda.

Viktigt (utrett 2026-08-21): Calibre går INTE att lösa som "ännu en output-rot". Calibre äger sin egen biblioteksstruktur (Författare/Titel (id)/ plus metadata.db) och läser inte en katalog man bara pekar på - den matas med `calibredb add`. Så antingen blir Calibre-destinationen en inkorg som en separat process plockar från, eller så anropar flipp-dl calibredb. Det valet är inte taget.

Beroende: TASK-1358 (Komga-biblioteket) bör vara på plats först, annars finns ingen Komga-rot att peka på.

- ID: `01M0JJC8B37RXVMYNFMPZKFRX2`
- Type: feature
- Actor: ai:claude-code

---

## [P3][done] [flipp] Exponera katalog-/utgave-/backup-kommandon i webb-UI:t



## Rasmus krav (2026-08-21): JSON ska kunna bade laddas UPP och NED i webben
Backup och liknande: --export-codes -> nedladdning (GET, FileResponse/Streaming
med Content-Disposition attachment), --import-backup och --import-catalog ->
uppladdning (POST multipart-fil). Bada riktningarna, inte bara knappar.

## Tekniska fynd (fran forstudien, sa nasta session slipper aterupptacka)
- Monster: POST /settings/import-existing (routes.py:1392) - CSRF via
  check_csrf_form (auth.py:89) + TemplateResponse-partial som HTMX swappar in.
  Knapp-markup: settings.html:296 (hx-post/hx-target/hx-vals med csrf_token).
- CSRF med multipart: check_csrf_form laser request.form() som funkar aven for
  multipart - filuppladdning behover _csrf_token som ett formfalt bredvid filen.
- INGEN UploadFile/File anvands an i web/ - detta blir forsta filuppladdningen.
  Kraver "from fastapi import File, UploadFile".
- Export = GET som returnerar filen (FileResponse eller StreamingResponse med
  Content-Disposition: attachment; filename=...).
- REFAKTORERA FORST: kärnlogiken ligger i cli.py:_run_export_codes /
  _run_import_backup / _run_import_catalog blandad med argparse + print. Bryt ut
  ateranvandbara funktioner (build_backup_payload(session)->dict,
  restore_backup(repo,payload)->counts, import-katalog-hjalparen _import_
  publication_file finns redan) sa web-routes och CLI delar dem. Ev. ny modul
  flipp_dl/codes.py.
- De tva tunga (discover-editions/import-editions) = bakgrundsjobb, egen
  delleverans (se tasken ovan). Bygg de tre snabba forst.

- ID: `01M0JDMK02MNQ0QMGDXJRHTKM2`
- Type: feature
- Actor: ai:claude-code

---

## [P3][done] [flipp] Systematisk Wayback-svep for att hitta unika olistade utgavor

## Context
editionshtml5_json ar INTE komplett per publikation - den missar genuint unika utgavor. Verifierat 2026-08-21: av 8 stickprovade in-katalog-utgavor som listan missar var 5 genuint unika (Bamse Sagoserier 2018 x2, Classic Motor 2021, Fantomens Skattkammare 2025, Kalle Anka Junior 2019 - olika omslag OCH innehall), bara 3 dubbletter (Bilar-ompublikationer, byte-identiska). Spanner alla ar, inte bara 2024.

Dessa unika hittades via Wayback CDX (sa de 227 i docs/olistade-utgavor.json hittades). editionshtml5_json OCH get_edition_by_date returnerar bara den kanoniska mangden (en per datum) och missar dessa. Enda live-kalla ar Wayback.

## Vad som ska goras
- Kor en systematisk Wayback CDX-svep for ALLA 118 publikationer (91 katalog + 27 olistade), inte bara stickprov. Monster: reader.flipp.se default.aspx?edid= och www.flipp.se/tidningar/ arkiverade sidor.
- Extrahera alla eids, verifiera mot editionshtml5_json-listan, spara de som saknas (unika) till docs/olistade-utgavor.json.
- Filtrera bort dubbletter: jamfor omslag (get_image pnum=1 md5) mot listade utgavors omslag - byte-identisk = ompublikation, hoppa. Behall bara unika.
- Importera de unika som issues (TASK-1438-principen: bevara alla eids vi sett).

## Grans
Ingen live-API ger dessa (editionshtml5_json + get_edition_by_date deduplicerar till kanonisk mangd). Wayback ar enda kallan, och den ar inte uttommande - bara det som arkiverats. Se doc 01M0GM0G.

- ID: `01M0JBHQPBP75G57M8HA7ZTZ3K`
- Type: feature
- Actor: ai:claude-code

---

## [P3][done] [flipp] Importera utgåvor från en lista med kända koder

## Context
Vi hittar utgåvor som Flipps lista inte känner till - tre av Bilar ligger redan i docs/olistade-utgavor.json, verifierade och hämtbara. Fler lär tillkomma (TASK-1436). I dag finns ingen väg att få in dem i flipp-dl: publikationer och utgåvor skapas bara av sync_publications utifrån API-svaret.

## Acceptance criteria
- [ ] En lista med publikationskod och utgåvokod kan läsas in, och de utgåvor som inte redan finns läggs till i databasen.
- [ ] Formatet är det som redan används i docs/olistade-utgavor.json.
- [ ] Utgåvans metadata hämtas där det går. Reader-API:t ger sidantal men inte namn eller datum - avgör vad som ska stå i issue_name och issue_date när Flipp inte längre listar utgåvan, och gör det tydligt att uppgiften är okänd snarare än påhittad.
- [ ] Publikationen måste finnas sedan tidigare. Är publikationskoden okänd ska det rapporteras, inte skapas en publikation utan namn.
- [ ] En importerad utgåva laddas ner som vilken annan som helst, och markeras som olistad enligt TASK-1429.
- [ ] Nästa poll får inte ta bort eller skriva över de importerade utgåvorna.
- [ ] Körs som ett eget uttryckligt steg, i stil med --import-existing och --migrate-filenames i cli.py.

## Att tänka igenom
Det här är en väg in i databasen som kringgår API-synken. Var noga med att en trasig eller påhittad kod inte skapar skräprader som sedan ser ut som riktiga utgåvor - verifiera mot reader-API:t innan något skrivs, och rapportera det som inte gick att verifiera.

## Verification
- Tester: import av känd kod, okänd publikationskod, redan befintlig utgåva, och att en poll efteråt lämnar raderna ifred.
- Skarpt: importera de tre Bilar-utgåvorna i docs/olistade-utgavor.json mot en kopia av driftdatabasen och ladda ner en av dem.

## Premissändring 2026-08-21
TASK-1439 ändrar förutsättningarna. `editionshtml5_json.aspx` listar utgåvorna
per publikation, inklusive de dolda, och ger namn, datum och sidantal.
Kriteriet ovan om att reader-API:t saknar namn och datum stämmer alltså inte
längre, och handhållna kodlistor är inte upptäcktsvägen.

Kvar av den här uppgiften är importmekanismen som sådan: en uttrycklig väg in
i databasen vid sidan av synken. Bygg TASK-1439 först och avgör sedan om detta
fortfarande behövs separat.

- ID: `01M0GFA3B11H5AZMQH8B8229A1`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Leta olistade utgåvor och publikationer utanför Flipps API

## Context
Flipps eget API kan inte lista något som inte redan erbjuds kontot - det är utrett och besvarat i backlog-docen "Går olistat material att upptäcka?". Men utanför API:t finns vägar, och en av dem är redan bevisad.

Wayback Machines CDX-API över reader.flipp.se gav 2000 arkiverade adresser, varav 35 innehöll både pubid och eid. Tre av utgåvekoderna är okända för Flipps aktuella lista, och alla tre går att hämta: 36, 44 och 36 sidor. De ligger i docs/olistade-utgavor.json och i backlog-docen "Olistade utgåvor hittade utanför Flipps API".

Metoden fungerar alltså. Frågan är hur långt den bär.

## Spår att utforska

1. **Wayback Machine, på djupet.** Den första sökningen var enkel: ett mönster, gränsen 2000 rader. Prova fler mönster (edid, andra reader-vägar, tidningar.flipp.se), ta bort gränsen, och gå igenom hela CDX-indexet. Kolla även arkiverade svar - inte bara adresser - eftersom ett arkiverat API-svar kan innehålla en hel publikationslista med koder.

2. **Sökmotorers index.** Reader-länkar som delats publikt kan ligga indexerade. Samma princip som Wayback, annan källa.

3. **PageSuite-plattformen.** Flipp är byggt på PageSuite: reader-vägen använder deras edid-begrepp och sid-PDF:erna ligger på pages.pagesuite.com. Utredningen tittade bara i Flipps app-bundlar, aldrig på plattformens egna publika endpoints. Undersök vad PageSuite exponerar.

4. **Andra marknader.** appId är se.egmontmagasiner.flipp. Egmont driver Flipp i flera länder, och listan innehåller redan norska och danska titlar. Ett annat appId mot samma API kan ge ett annat utbud - kontrollera vad appen skickar och vilka värden som finns.

5. **Spara varje polls råsvar framåt.** Hjälper inte bakåt, men 1916 utgåvor har redan fallit ur listningen. Ett eget arkiv av råsvaren gör att inget mer går förlorat. Detta är det enda spåret som är ett bygge snarare än en utredning - bryt ut det om det ska göras.

## Struket spår
Kontotyp undersöktes och är en återvändsgränd. Rasmus har haft Premium och såg samma utgåvor som med Solo, vilket stämmer med datan: visibleIssuesSolo, visibleIssuesSubscriber och visibleIssuesPremium är identiska för 90 av 91 publikationer.

## Regler
- Gissa aldrig fram koder. De är UUID - ogörligt, och det vore att hamra Egmonts tjänst i onödan.
- Håll anropen få och riktade mot Flipp. Arkiv och sökmotorer tål mer, men var måttfull även där.
- Detta är material Rasmus har konto och tillgång till, inte kringgående av betalvägg.

## Leverans
- Varje ny träff läggs i docs/olistade-utgavor.json i samma format som de tre befintliga, med sidantal verifierat mot reader-API:t.
- En backlog-doc som säger hur långt varje spår bar, inklusive de som inte gav något.

- ID: `01M0GF959KY2NTSGX7XE7XW764`
- Type: spike
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Undersök om olistade publikationer och utgåvor går att upptäcka

## Context
Flipps publikationslista (refreshsignintoken) visar bara vad kontot erbjuds just nu. Databasen känner till tre publikationer som fallit ur listan - Frost Aktivitetspåse, Robot Junior Bag och Stitch - och de går fortfarande att ladda ner: Stitch nr 3 2026 gav 44 sidor från reader-API:t 2026-08-20.

Det väcker frågan: hur mycket mer finns det som aldrig listats för kontot? En publikation som aldrig dykt upp i listan finns inte alls i databasen, och då finns inte heller dess utgåvekoder att slå upp.

## Vad som ska utredas
- get_page_groups_from_eid.aspx kräver ingen autentisering och tar pubid och eid. Finns någon motsvarande oautentiserad väg att LISTA utgåvor eller publikationer, i stil med ett katalog- eller sökanrop?
- Vad returnerar reader-API:t för en giltig pubid men okänd eid, respektive för en pubid som kontot inte har? Skiljer sig felen åt på ett sätt som avslöjar något?
- Innehåller svaret från get_page_groups_from_eid några referenser till andra utgåvor - föregående eller nästa nummer, arkiv, relaterade koder?
- Har Flipps webbapp fler endpoints än de två vi använder? Buntarna ligger på tidningar.flipp.se/flipp/web-app/ och main-bundlen är läsbar. Leta efter API-anrop vi inte känner till.
- Vad säger accountInformation och categories i refreshsignintoken-svaret? Antyder de ett större utbud än publications-listan?

## Icke-mål
Detta är en utredning, inte ett bygge. Ingen kod ska ändras.

Gissa inte fram utgåvekoder genom att prova sig fram - koderna är UUID, det är ogörligt och skulle dessutom innebära att hamra någon annans tjänst.

## Leverans
En backlog-doc som svarar på: går olistat material att upptäcka på något rimligt sätt, och i så fall hur. Om svaret är nej ska det stå tydligt, så frågan inte behöver ställas igen.

Skriv ut vilka anrop som faktiskt gjordes och vad de svarade - inte vad som antas.

- ID: `01M0GCTXPT2XRYSP6WNMTY6MAF`
- Type: spike
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Visa vilka publikationer som inte längre listas av Flipp

## Context
Databasen har 94 publikationer medan Flipp just nu listar 91. Skillnaden är Frost Aktivitetspåse, Robot Junior Bag och Stitch - sammanlagt 16 utgåvor, inga nedladdade.

sync_publications lägger till och uppdaterar men tar aldrig bort, vilket är rätt: en publikation som försvinner ur utbudet ska inte ta med sig nedladdningshistorik och filsökvägar i fallet.

Viktigt att formuleringen blir rätt: de här publikationerna GÅR fortfarande att ladda ner. Kontrollerat 2026-08-20 - Stitch nr 3 2026 gav 44 sidor från reader-API:t. De två API:erna hänger inte ihop: refreshsignintoken returnerar vad kontot erbjuds just nu, medan get_page_groups_from_eid slår upp en utgåva utan att kräva någon autentisering alls. Så länge utgåvans kod finns kvar i databasen är den hämtbar.

Ett märke som antyder att de är otillgängliga vore alltså direkt missvisande. Formuleringen ska vara i stil med "listas inte längre av Flipp, går fortfarande att hämta".

## Acceptance criteria
- [ ] En publikation som inte kom med i senaste pollen markeras som olistad, med tidpunkt för när den sist sågs.
- [ ] Markeringen syns i publikationslistan och går att filtrera på.
- [ ] Ordvalet säger att den inte längre listas, inte att den är borttagen eller otillgänglig.
- [ ] Dyker publikationen upp igen i en senare poll försvinner markeringen automatiskt.
- [ ] Ingenting raderas, och en olistad publikation går fortfarande att bevaka och ladda ner.

## Öppen fråga
En publikation kan saknas i en poll av tillfälliga skäl - ett API-fel eller en ändrad prenumeration. Ska markeringen sättas direkt vid första frånvaron eller först efter flera pollar i rad? Ta ställning och motivera.

## Verification
- Tester: publikation försvinner ur svaret och markeras, dyker upp igen och avmarkeras, och att inget raderas.
- Kontroll mot driftinstansen: de tre kända publikationerna markeras, övriga 91 inte.

- ID: `01M0GCT0Q68JY6Z1XGXQXE92E8`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] En publikations katalog får inte kunna kopplas till en annan publikation

## Context
Varje publikation äger sin katalog under utdatakatalogen, men ingenting upprätthåller det. En fil som hamnar i fel publikations katalog - genom en namnkrock, en handflyttad fil eller en framtida sökvägsändring - kan tolkas som tillhörande fel publikation.

Flipp-dl har inga bilagor: allt under en publikations katalog är utgåvor av just den publikationen. Det gör kravet enkelt och strikt.

## Var det slår igenom
- Diskimporten matchar filer mot utgåvor och måste veta att en fil under fel publikation aldrig är en träff.
- Nedladdningen skriver till publikationens katalog och får aldrig hamna utanför den.
- En framtida sökvägsändring som flyttar filer måste bevara strukturen.

## Acceptance criteria
- [ ] En fil under publikation A kan aldrig kopplas till en utgåva i publikation B, vare sig vid import eller nedladdning.
- [ ] Nedladdning skriver alltid inom rätt publikations katalog, kontrollerat och inte bara antaget.
- [ ] Import rapporterar en fil som ligger under fel publikation som en avvikelse, inte som en träff.

## Verification
- Tester: fil i fel publikations katalog vid import, nedladdning som försöker skriva utanför sin katalog.
- Kontroll mot driftinstansen med import-knappen efteråt: inga nya avvikelser som inte fanns förut.

Motsvarande fråga finns i prenly-dl (TASK-1405), men där kompliceras den av att bilagor är egna publikationer som ska hamna i undermappar. Här finns inget sådant fall.

- ID: `01M0G4D6VSYMRJDX6AZNTMBB9N`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Gör filnamnen OS-säkra, inte bara tecken-filtrerade

safe_name filtrerar bort allt utom en whitelist av tecken: bokstäver, siffror, bindestreck, understreck, punkt, parenteser, mellanslag och åäö. Det räcker för att undvika snedstreck, men täcker inte allt som gör en sökväg problematisk på andra filsystem än ext4.

Att hantera:
- Windows-reserverade namn: CON, PRN, AUX, NUL, COM1-9, LPT1-9 - även med filändelse. En publikation som heter så ger en fil som inte går att skapa.
- Namn som slutar med punkt eller mellanslag - Windows tar tyst bort dem, vilket gör att sökvägen i databasen inte matchar filen på disk.
- Namn som blir tomma efter filtrering, exempelvis en titel som bara består av tecken utanför whitelisten. I dag ger det ett filnamn som bara är ".pdf".
- Total sökvägslängd. Windows har 260 tecken som standardgräns, och publikationsnamn plus utgåvenamn plus datum blir långt. Avgör om namnet ska kortas och hur unikheten då bevaras.
- Unicode-normalisering: åäö kan kodas på två sätt (NFC/NFD), vilket ger olika filnamn för samma titel beroende på var strängen kommer ifrån. macOS normaliserar till NFD.

Verktyget körs i Linux-container i dag, men output-katalogen monteras ofta från en NAS och läses av Windows- och macOS-klienter, och biblioteksprogram som Komga läser samma filer.

Acceptanskriterier:
- Reserverade namn, avslutande punkt eller mellanslag, och tomt resultat efter filtrering hanteras alla med ett förutsägbart namn.
- Befintliga filnamn ändras inte i onödan - en ändrad namnregel får inte göra att redan nedladdade filer inte längre hittas. Bestäm hur det säkras och beskriv det.
- Tester för varje fall ovan.

Filer som väntas ändras: flipp_dl/storage.py, tests/test_storage.py.

Samma todo finns i prenly-dl som TASK-1401. Lösningarna behöver inte vara identiska - flipp-dl filtrerar mot en whitelist medan prenly-dl ersätter otillåtna tecken - men problembilden är densamma.

## Migrering (Rasmus 2026-08-20)
Här FINNS redan nedladdade filer i drift - 1094 stycken. Namnregeln får därför ändras, men då krävs en engångsmigrering som döper om befintliga filer och uppdaterar file_path i databasen. Kör den som ett eget steg, inte som en tyst sidoeffekt av en nedladdning, och gör den återstartbar.

- ID: `01M0G3QEVDPABMP1TQBAJGM7D0`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Settings: ojämna bredder och grupper som inte hänger ihop

Fälten under Import existing files är breda medan grupperna ovanför (Komga, Notifications) är smala, utan att skillnaden betyder något. Ta reda på varifrån bredden kommer - troligen en .form-card med max-width som bara vissa block ligger i - och gör den enhetlig.

Samtidigt: gruppindelningen är värd att se över. Komga, Notifications, Import existing files och Debug är fyra rubriker på samma nivå fast de gör olika saker - integrationer, aviseringar, en engångsåtgärd och felsökning. Fundera på om de ska delas i sektioner eller flyttas dit de hör hemma (importen är snarare något man gör en gång från Library än en inställning).

- ID: `01M0G2TD5ZS5ERJGJDGR16Z7YF`
- Type: improvement
- Actor: human:rasmus

---

## [P3][done] [flipp] Visa miniatyromslag i Recent downloads på dashboarden

Recent downloads listar utgåva, publikation, tidpunkt och filväg som ren text. Publikationslistan och utgåvelistan visar redan miniatyromslag i första kolumnen - dashboarden borde göra likadant.

Underlaget finns: utgåvornas omslag cachas sedan TASK-1345/1374 och serveras från /publications/{code}/issues/{issue_code}/cover. Omslaget ska också gå att klicka för lightbox, som på övriga sidor - lägg data-lightbox-src med /cover/large enligt mönstret i publication_row.html.

Acceptanskriterier:
- Recent downloads har en omslagskolumn längst till vänster, i samma format som övriga listor.
- Saknas cachat omslag döljs bilden som på andra sidor, ingen trasig ikon.
- Omslaget öppnar lightboxen vid klick.
- Ingen extra databasfråga per rad - list_recent_downloads laddar redan publikationen.

Filer som väntas ändras: flipp_dl/web/templates/dashboard.html, eventuellt flipp_dl/web/routes.py.

Verifiering: browser vid 390px och 1280px - dashboarden har redan flera kolumner och måste rymma en till i mobilbredd. Klicka ett omslag och kontrollera lightboxen.

- ID: `01M0G2R84E6WPKMBGZV99MMPHQ`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Öppna omslag i en lightbox

Omslagen i publikationslistan och på detaljsidan ska gå att klicka för att se i större format utan att lämna sidan. En enkel lightbox: klick öppnar, klick utanför eller Escape stänger.

Omslagen cachas via fetch_and_cache_cover i den storlek Flipp levererar (300m-varianten). Avgör om den räcker för en lightbox eller om en större variant ska hämtas.

Motsvarande todo lagd i prenly-dl.

- ID: `01M0G16JNX87KK6XFMKZF7R01P`
- Type: improvement
- Actor: human:rasmus

---

## [P3][done] [flipp] Kodsnutt för konsolen som hämtar ut Flipp-token

Token går numera att spara i gränssnittet (TASK-1342), men att få tag på den kräver fortfarande att man öppnar utvecklarverktygen, hittar rätt anrop och kopierar ur en payload - som README beskriver i fyra steg.

Lägg en färdig kodsnutt att klistra in i webbläsarkonsolen på tidningar.flipp.se, som plockar fram token och skriver ut den kopieringsklar. Visa den på inställningssidan intill token-fältet, med en kopiera-knapp.

Detta är den enkla varianten av TASK-1342:s andra halva - userscriptet som postar token automatiskt är fortfarande en senare fråga. En snutt att klistra in kräver ingen installation och löser samma problem för den som byter token en gång i halvåret.

Att ta reda på under arbetet: var token faktiskt bor i webbläsaren på tidningar.flipp.se - localStorage, cookie eller bara i anropens payload. Det avgör om snutten kan läsa den direkt eller måste haka i ett anrop.

Filer som väntas ändras: flipp_dl/web/templates/settings.html, eventuellt flipp_dl/web/routes.py, locale-filerna.

Verifiering: snutten ska testas i en riktig webbläsare mot tidningar.flipp.se av Rasmus - den går inte att verifiera automatiskt utan ett inloggat konto.

- ID: `01M0G0EJP9BBV3HF83Y45D1NC8`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Formulärfälten på inställningssidan använder inte projektets stil

Inställningssidan ser trasig ut: de flesta fälten är vita och smala utan padding, "Test connection" är en grå systemknapp och rullistan för Komga-bibliotek är ostilad. Kontrollerat i drift 2026-08-20 med skärmdump.

Orsaken är en enda: CSS-regeln i base.html rad 214 gäller bara input[type="number"]. Sidan innehåller i dag 3 number-fält (som ser rätt ut), men också 4 password, 3 url, 2 text, 3 checkbox, en select och fyra button-element - inget av det täcks. Varje sektion som lagts till (token, Komga, notiser, tröskel) har kopierat markup från ett fält som råkade fungera, medan regeln aldrig utvidgades.

Acceptanskriterier:
- Text-, password-, url-, number- och search-fält delar samma stil: mörk bakgrund, padding, rundade hörn, fokusmarkering.
- Select-elementet följer samma formspråk.
- Knappar i formulär använder projektets btn-klasser i stället för webbläsarens standardknapp. "Test connection" är den som sticker ut mest.
- Checkboxar och deras etiketter linjerar med övrig text.
- Fält och hjälptexter har konsekvent avstånd till varandra - i dag ligger vissa hjälprutor tätt mot fältet ovanför.
- Ingen regression på andra sidor: filterraderna på /publications, /library, /jobs och /search använder egna input-regler som inte får överskuggas.

Filer som väntas ändras: flipp_dl/web/templates/base.html, flipp_dl/web/templates/settings.html, eventuellt komga_library_select.html.

Verifiering: skärmdumpar av /settings vid 390px och 1280px före och efter, plus en kontroll av /publications, /library, /jobs och /search vid samma bredder så att de inte påverkats. Klicka Test connection och kontrollera att den fortfarande fungerar.

- ID: `01M0FWWKY02DHS8KJ0EXAKJWAX`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Visa storlek på disk som kolumn i publikationslistan

Publikationslistan visar nedladdade av totalt, men inte vad publikationen väger. Med 94 publikationer och 53 GB på disk är det den siffra som säger var utrymmet tar vägen.

Underlaget finns redan: issues.file_size lagras per utgåva sedan TASK-1362, och _issue_counts_by_publication i repository.py gör redan en grupperad fråga per publikation för antalen. Summan hör hemma i samma fråga - inte i en egen runda och absolut inte genom att stat:a filer under rendering.

Acceptanskriterier:
- Publikationslistan har en kolumn med publikationens sammanlagda storlek på disk.
- Summan hämtas i den befintliga aggregerade frågan, så antalet SQL-satser inte ökar med antalet publikationer.
- Utgåvor som saknar känd storlek - nedladdade före TASK-1362 - får inte visas som 0 byte utan förklaring. Avgör hur det ska visas, exempelvis som ett ungefärtecken eller genom att räkna dem separat, och motivera valet.
- Samma tal visas efter watch/unwatch-swappen, som renderar publication_row.html på nytt.
- Ny text genom gettext, katalogen uppdaterad med pybabel.

Filer som väntas ändras: flipp_dl/db/repository.py, flipp_dl/db/models.py, flipp_dl/web/templates/publications.html, flipp_dl/web/templates/publication_row.html, tests/test_repository.py, tests/test_web_routes.py.

Verifiering: test som kontrollerar summan och att frågeantalet inte växer, plus browser-verifiering vid 390px och 1280px - tabellen har redan sju kolumner, så en till måste rymmas i mobilbredd.

- ID: `01M0FS74CTCMVDSTZSQR8ABK5J`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Utgåvestatus sätts inte när sid-URL:erna misslyckas i downloadern

Hittat under arbetet med TASK-1363. I download_issue ligger anropet till client.fetch_issue_pdf_urls UTANFÖR downloaderns egen try/except, som bara omsluter sidhämtning och PDF-sammanslagning. Fallerar hämtningen av sidlistan - ogiltig token, borttagen utgåva, nätverksfel - körs alltså aldrig mark_issue_error, och utgåvan blir kvar i det läge den hade.

Schemaläggaren kompenserar sedan TASK-1363: dess except-block garanterar att utgåvan hamnar som error oavsett var i download_issue felet uppstod. Men den som anropar IssueDownloader direkt, till exempel CLI:t, får fortfarande det ofullständiga beteendet, och kompensationen döljer att downloadern själv inte håller sitt löfte om att spegla status.

Acceptanskriterier:
- Ett fel vid hämtning av sidlistan markerar utgåvan som error på samma sätt som ett fel senare i nedladdningen.
- Schemaläggarens kompensation kan vara kvar som skyddsnät men ska inte längre vara det enda som får statusen rätt.
- Test som anropar download_issue direkt, med en klient som failar på fetch_issue_pdf_urls, och kontrollerar utgåvans status.

Filer som väntas ändras: flipp_dl/downloader.py, tests/test_downloader.py.

- ID: `01M0FR8MW0AMKYB1Q3EWNT1EEJ`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Se över hur knapparna i Action-kolumnen ser ut och radbryter

Action-kolumnen i utgåvelistan har vuxit under arbetet: Preview, Open, Re-download, Delete, Retry, Download och Cancel, där flera kan visas samtidigt beroende på status. De ärver olika knappklasser (btn-watch, btn-unwatch, btn-primary-soft) som valts en i taget, och radbrytningen är inte genomtänkt - på en skärmdump vid 1280px hamnar Delete på egen rad under Open och Re-download.

Se över helheten: vilka knappar som ska synas samtidigt, vilken som är den primära handlingen per status, färgsättningen, och hur de radbryter i mobilbredd. En knapp som raderar en fil bör inte se ut som den som öppnar den.

Filer som väntas ändras: flipp_dl/web/templates/issue_row.html, flipp_dl/web/templates/base.html (knappklasserna), eventuellt flipp_dl/web/templates/publication_detail.html.

Verifiering: skärmdumpar vid 390px och 1280px för varje status en rad kan ha (inte nedladdad, köad, laddar ner, nedladdad, fel), på både engelska och svenska eftersom knapptexterna är översatta och byter längd.

## Tillkommer efter TASK-1363
Utgåvor som väntar på ett automatiskt omförsök har sedan TASK-1363 en egen status (retry_pending). Utgåvelistan känner inte till den och visar dem som "Not downloaded" med en Download-knapp, vilket är missvisande - jobbsidan visar korrekt "Waiting for retry". Ta med den statusen när kolumnen ses över: den behöver en egen märkning och rimligen inte samma primärknapp.

- ID: `01M0FGJZJDZD60HGGDDXW1NHE7`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Utgåveomslagen saknas för allt som upptäcktes före omslagscachen

Utgåvelistan på /publications/{code} pekar redan på den lokala cachen (issue_row.html rad 13, /publications/{code}/issues/{code}/cover) sedan TASK-1345. Men kolumnen är tom i drift: kontrollerat 2026-08-20 svarar publikationens eget omslag 200 med 45 kB, medan utgåvornas omslag ger 404.

Orsaken är att utgåveomslag bara hämtas EN gång, när utgåvan upptäcks vid poll. Samtliga 17660 kända utgåvor upptäcktes innan den funktionen fanns, och de upptäcks aldrig igen - alltså får de aldrig någon cache. Funktionen fungerar bara för utgåvor som tillkommer framöver.

Acceptanskriterier:
- Utgåvor som saknar cachat omslag kan fylla på det i efterhand, inte bara vid upptäckt.
- Påfyllningen sker i bakgrunden och aldrig synkront under rendering - 17660 utgåvor får inte betyda 17660 hämtningar vid en sidladdning.
- Volymen är medveten: hämta rimligen bara för utgåvor som faktiskt visas eller är nedladdade, eller med tak per körning. Motivera valet.
- Saknas omslag fortfarande döljs bilden som i dag (onerror), ingen trasig ikon.

Verifiering: riktade tester för påfyllningen, plus kontroll i webbläsaren att kolumnen faktiskt visar omslag på en publikationssida, vid 390px och 1280px.

- ID: `01M0FGJZJ7WY7E90GZ17DQ5551`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Sökning över alla utgåvor, inte bara inom en publikation

I dag söker man antingen bland utgåvorna på en publikations detaljsida eller på filnamn i Library. Med 17660 kända utgåvor fördelade på 94 publikationer saknas vägen att hitta något på tvärs: alla nummer från ett visst år, allt som saknas i en titel, eller en utgåva vars publikation man inte minns.

Acceptanskriterier:
- En sökvy som söker över alla utgåvor på utgåvenamn, datum och publikation.
- Går att filtrera på status, minst nedladdad respektive inte nedladdad.
- Sökningen sker i databasen med gräns på antal träffar - den får inte ladda alla 17660 rader och filtrera i Python, och inte heller i JavaScript i webbläsaren.
- Träffarna länkar till publikationen och, för nedladdade utgåvor, direkt till PDF:en.
- Ny användarsynlig text ska gå genom gettext, katalogen uppdateras med pybabel enligt README.

Verifiering: riktade tester i tests/test_web_routes.py inklusive ett som verifierar att antalet SQL-satser inte växer med antalet utgåvor, plus browser-verifiering vid 390px och 1280px där en sökning faktiskt utförs.

- ID: `01M0DWYFG070H35Y33FW3SDEHB`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Slå på Komga-integrationen i flipp-dl

BEROENDE: kräver att TASK-1358 (montera output-katalogen som bibliotek i Komga) är klar först. Utan bibliotek finns inget att välja i rullistan och inget att skanna.

Koden är byggd och utrullad (TASK-1326, 1327, 1328) men allt är avstängt tills det konfigureras. Komga svarar på http://192.168.1.2:8097.

Att göra på /settings i flipp-dl:
- Fyll i Komga-URL och autentisering: användarnamn och lösenord, eller X-API-Key om Komga är 1.8 eller senare.
- Klicka "Test connection" - den hämtar bibliotekslistan och fyller rullistan. Får du inget svar är det URL eller credentials som är fel, inte flipp-dl.
- Välj biblioteket från TASK-1358 och slå på integrationen.

Kontrollera efteråt:
- Ladda ner en utgåva och se att ett komga_sync-jobb dyker upp på /jobs och blir done.
- Kolla i Komga att serien fått titel, beskrivning, förlag Egmont, språk sv och Flipps omslag - inte PDF:ens första sida.
- Läs en utgåva i Komga och se att läst-markeringen dyker upp i flipp-dl inom ett dygn (synken går en gång per dygn).

Vill du inte att ett visst fält skrivs över kan det stängas av för sig: KOMGA_PUSH_TITLE, KOMGA_PUSH_SUMMARY, KOMGA_PUSH_COVER, KOMGA_PUSH_GENRES, KOMGA_PUSH_NUMBER, KOMGA_PUSH_RELEASE_DATE, KOMGA_PUSH_PUBLISHER, KOMGA_PUSH_LANGUAGE, KOMGA_PUSH_ISSUE_TITLE.

- ID: `01M0DM1Z2W9NJ1GCK5X9ATBVY7`
- Type: chore
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Montera flipp-dl:s output-katalog som bibliotek i Komga

Förutsättning för att Komga-integrationen ska kunna slås på. Utan ett bibliotek som faktiskt pekar på flipp-dl:s filer har en scan-trigger inget att skanna, och metadata-pushen hittar ingen serie att skriva till.

Läget: Komga kör redan på TERVO2 (containern "Komga", gotson/komga, host-port 8097 mot 25600 i containern). flipp-dl skriver sina PDF:er till den katalog som är monterad som /output i containern flipp-dl-dev, i drift /mnt/user/Downloads/Flipp.

Att göra:
- Ge Komga läsåtkomst till samma katalog. Antingen genom att montera in den i Komga-containern, eller genom att peka om flipp-dl:s output till en katalog Komga redan ser.
- Skapa ett bibliotek i Komga med den katalogen som rot. Enligt den ursprungliga planen ska det vara ett "Manuella"-bibliotek med extern metadata-matchning AVSTÄNGD - svenska serietidningar finns inte i Comicvine eller GCD, och Komgas providers skulle annars skriva över det flipp-dl pushar.
- Kontrollera att Komga hittar serierna: layouten <publikationsnamn>/<utgåva>.pdf ska tolkas som serie och bok.
- Notera bibliotekets id - det behövs i nästa steg.

Klart när: biblioteket finns i Komga, serierna syns, och bibliotekets id är noterat.

- ID: `01M0DM197KW11YDV4XDX1J2WN0`
- Type: chore
- Actor: ai:claude-opus-5

---

## [P3][doing] [flipp] Verifiera OPDS-feeden mot en riktig läsare

Feeden är byggd och verifierad strukturellt (XML/JSON parsas i tester, och drift svarar 200 på både /api/opds och /api/opds2, även med HTTP Basic). Det som återstår är det enda som inte går att simulera: att en verklig OPDS-klient accepterar katalogen.

Ligger som doing i väntan på att en klient finns att testa med.

Att prova när det blir aktuellt:
- Lägg till http://192.168.1.2:8934/api/opds i klienten, autentisera med samma lösenord som webbgränssnittet via HTTP Basic (användarnamnet spelar ingen roll).
- Kontrollera att publikationerna listas, att omslagen visas, och att en utgåva går att öppna och läsa.
- Prova även 2.0-varianten på /api/opds2 om klienten stöder den.
- Kandidater: KOReader, Panels (iOS), Moon+ Reader, Chunky.

Faller något: notera vilken klient och vilket steg, det avgör om det är feedens struktur, auth-flödet eller filserveringen som behöver justeras.

- ID: `01M0DKT63ANXYK58W6AEJT61HD`
- Type: task
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

## [P3][done] [flipp] Cachea omslag lokalt i stället för att hotlinka pagesuite

## Context
Omslagen hämtas i dag direkt från externa tjänster vid varje sidladdning: issue_row.html pekar på pagesuite (`get_image.aspx?w=100&eid=<issue-kod>`), och publication_row.html/publication_detail.html pekar på `publication.cover_url` (Flipp/pagesuite). Det gör gränssnittet beroende av en extern tjänst, läcker vilka sidor som besöks till en tredje part, och blir långsamt när många rader renderas (t.ex. 17 660 utgåvor).

Alembic: den här tasken äger revision 0005 (down_revision = "0004_job_indexes"). Revisionsnumret är förtilldelat för att flera parallella tasks annars skapar var sin head - numret får inte ändras.

## Acceptance criteria
- [ ] Omslag för publikationer och utgåvor cachas lokalt (disk utanför output_root, eller databasen) i stället för att laddas direkt från pagesuite/Flipp i renderad HTML
- [ ] En egen endpoint serverar de cachade omslagen, och publication_row.html, publication_detail.html och issue_row.html pekar på den i stället för på externa URL:er
- [ ] Publications-sidan visar fortfarande publikationens senaste omslag per rad, inte ett fast/statiskt omslag (dagens beteende bevaras)
- [ ] Hämtning av omslag sker inte synkront under sidrendering - antingen vid poll (`poll_publications` i flipp_dl/scheduler.py) när en utgåva upptäcks, eller lat via en egen jobbtyp
- [ ] Saknas ett cachat omslag hanteras det utan att sidan kraschar - dagens `onerror`-döljning duger som fallback
- [ ] Det finns en rimlig gräns för hur mycket cachedata som sparas (t.ex. bara utgåvor som faktiskt visats, eller en storleksgräns) - inget krav på exakt mekanism, men den ska vara motiverad i koden/commiten
- [ ] Ny Alembic-migration 0005 (down_revision = "0004_job_indexes") lägger till de kolumner/tabeller som krävs om omslagen lagras i databasen; `alembic upgrade head` går igenom rent på en tom databas

## Implementation hints
Filer som väntas ändras: flipp_dl/web/templates/issue_row.html, flipp_dl/web/templates/publication_row.html, flipp_dl/web/templates/publication_detail.html, flipp_dl/db/models.py, flipp_dl/db/repository.py, flipp_dl/db/migrations/versions/0005_*.py (ny fil, down_revision = "0004_job_indexes"), flipp_dl/web/routes.py, flipp_dl/scheduler.py, tests/test_web_routes.py

Dagens kolumn `cover_url` på `DbPublication` (flipp_dl/db/models.py:62) håller kvar den externa URL:en - lägg troligen till en lokal cache-referens (path eller blob) bredvid, alternativt på `DbIssue`, och en ny endpoint i flipp_dl/web/routes.py (mönster: se `serve_issue_file` rad ~434 och `serve_library_file` rad ~507 för hur befintliga fil-serverande endpoints är byggda). `poll_publications` i flipp_dl/scheduler.py (rad ~45) är rätt ställe att trigga hämtning av utgåve-omslag vid upptäckt, i linje med hur nya issues redan upptäcks där.

## Verification
- `.venv/bin/python -m pytest tests/test_web_routes.py` - alla web-route-tester går igenom, inklusive nya/uppdaterade tester för cache-endpointen
- `.venv/bin/python -m alembic upgrade head` (mot en tom testdatabas) - går igenom utan fel, och `alembic history` visar 0005 med down_revision 0004_job_indexes
- `shot http://ubuntu-ai:PORT/publications ut-390.png --width 390 --height 780 --wait networkidle` och samma vid `--width 1280 --height 900` - omslagsbilderna ska synas och peka på den egna endpointen (inte pagesuite-domänen), verifiera med `curl -sI http://ubuntu-ai:PORT/<cache-endpoint>/<kod>` att den lokala endpointen svarar 200
- Klicka in på en publikations detaljsida och verifiera i browser att omslaget där också laddas från den lokala endpointen, inte bara att det renderas

- ID: `01M0CZ3Y538K1Q6DF6Y0D30S2H`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Preview av en utgåva utan att den räknas som nedladdad

## Context
I dag går enda vägen till en utgåvas PDF via en riktig nedladdning som markerar utgåvan `done` i biblioteket. Det finns inget sätt att bara titta på en utgåva utan att den räknas som nedladdad och tar plats i output-katalogen.

## Acceptance criteria
- [ ] En ny endpoint hämtar en utgåvas PDF till en temporär plats UTANFÖR `output_root`, så varken Library-vyn eller den kommande filimporten (TASK-1283) ser filen som en riktig nedladdning.
- [ ] Preview-hämtningen går via en egen väg genom downloadern (inte `download_issue()`/`_target_path()`/`skip_existing` rakt av) - hämta bara de första sidorna i stället för att slå ihop alla sidor, så en preview inte kostar lika mycket bandbredd som en full nedladdning.
- [ ] PDF:en serveras inline i webbläsarens visare (`Content-Disposition: inline`), inte som nedladdningsbar bilaga.
- [ ] Utgåvans status i `issues`-tabellen ändras INTE av en preview - fältet `status` får inte gå via `mark_issue_downloading`/`mark_issue_done`, annars syns previewen felaktigt som en pågående eller klar nedladdning i /jobs och i publikationslistan.
- [ ] Temporärfilen städas efter en TTL, i stil med `purge_old_jobs()` - t.ex. vid nästa poll/scheduler-tick eller vid en explicit "städa gamla previews"-runda.
- [ ] En knapp på publikationens detaljsida (`publication_detail.html`) startar en preview för en given utgåva utan att sidan i övrigt ändrar utseende.

## Implementation hints
Filer som väntas ändras:
- flipp_dl/downloader.py
- flipp_dl/web/routes.py
- flipp_dl/web/templates/issue_row.html
- flipp_dl/web/templates/publication_detail.html
- flipp_dl/scheduler.py
- flipp_dl/db/repository.py
- tests/test_downloader.py
- tests/test_web_routes.py

Läs `_target_path()` (downloader.py rad 161-184) och `download_issue()` (rad 54-135) noga innan du börjar - de använder `self.output_root` för att placera filen och för att avgöra ägarskap mellan utgåvor som delar filnamn (TASK-1349). En preview-fil får inte gå genom `issue_path()`/`publication_folder()` mot `output_root` - använd en separat temp-katalog (t.ex. under `output_root.parent` eller systemets temp-dir, konfigurerbar likt `output_root`). `skip_existing`-parametern och statusuppdateringarna (`mark_issue_downloading`, `mark_issue_done`, `mark_issue_error`) i `download_issue()` hör till den riktiga nedladdningsvägen - preview-vägen ska vara en egen metod som inte anropar dessa. `purge_old_jobs()` (repository.py rad 597) och dess anrop i scheduler.py (rad 95) är mönstret att följa för TTL-städning.

## Verification
- `.venv/bin/python -m pytest tests/test_downloader.py -k preview -v`
- `.venv/bin/python -m pytest tests/test_web_routes.py -k preview -v`
- Manuellt: starta en preview, kontrollera att filen hamnar utanför `output_root`, att `issues.status` i DB är oförändrad, och att filen försvinner efter TTL:en.
- Browser: `shot` av publikationens detaljsida vid 390px och vid 1280px - preview-knappen ska synas per utgåva i båda bredderna, och ett klick ska öppna PDF:en inline utan att raden växlar till nedladdningsläge.

- ID: `01M0CZ0FB389XF2V4FSH45FB0A`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Watched only ska vara förkryssad som default på /publications

## Context
Kryssrutan "Watched only" på /publications är omarkerad vid sidladdning, så listan visar alla 94 publikationer trots att bara 19 är bevakade - vilket normalt är det man vill se. Hänger ihop med TASK-1329 (behåll filtret i URL:en) och ska genomföras tillsammans: en explicit URL-flagga ska alltid vinna över "watched only"-defaulten, så en delad länk utan filter fortfarande kan visa allt.

## Acceptance criteria
- [ ] Kryssrutan "Watched only" är förkryssad vid vanlig sidladdning av /publications (utan URL-flagga)
- [ ] Är URL-flaggan från TASK-1329 explicit satt (t.ex. `?watched=0` eller motsvarande "visa alla"), vinner den över defaulten - kryssrutan blir avmarkerad och alla publikationer visas
- [ ] Räknaren `#pub-count` reflekterar rätt antal direkt vid sidladdning (ingen flimmer av 94 rader innan filtret slår till)
- [ ] Befintliga tester i tests/test_web_routes.py för /publications fortsätter gå igenom

## Implementation hints
Filer som väntas ändras: flipp_dl/web/templates/publications.html

Ändringen görs i publications.html: dels attributet `checked` på `#pub-watched` (rad ~22), dels initieringslogiken i `<script>`-blocket (funktionen `applyFilter`, rad ~61-94) så den läser en ev. URL-flagga innan defaulten sätts. Detta samordnas med TASK-1329 som lägger till själva URL-synkroniseringen - bygg på samma mekanism i stället för att lägga en egen parallell lösning.

## Verification
- `.venv/bin/python -m pytest tests/test_web_routes.py -k publications` - befintliga routetester går igenom
- `shot http://ubuntu-ai:PORT/publications ut-390.png --width 390 --height 780 --wait networkidle` och samma med `--width 1280 --height 900` - kryssrutan "Watched only" ska synas förbockad och listan ska visa bara de bevakade raderna (19 av 94) i räknaren, vid båda bredderna
- Ladda `/publications?<url-flagga-som-visar-alla>` (flaggan från TASK-1329) och verifiera med samma shot-kommando att kryssrutan i stället är avbockad och alla 94 rader visas - klicket/state ska alltså verifieras, inte bara att sidan renderar

- ID: `01M0CYZAAQV6KCRKY8YCVC258H`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Sätt Flipp-token via gränssnittet

## Context
Settings-sidan säger i dag att token bara kan läsas från FLIPP_TOKEN eller token-filen vid uppstart och inte får ändras i gränssnittet "of security reasons". Det resonemanget hörde till CLI-tiden - nu är det en inloggad webbtjänst, och att behöva starta om containern för att byta token är sämre än att kunna klistra in den.

SCOPE: bara gränssnittssidan. Själva userscriptet (Tampermonkey som plockar token från tidningar.flipp.se) byggs senare, men allt som behövs för att ta emot en token utifrån ska finnas på plats här.

Vald väg in (redan avgjord, ändra inte): CSRF-skyddad POST mot befintliga `/settings`-routen, med samma sessionsinloggning och samma `check_csrf_form`-mekanism som resten av gränssnittet redan använder (se `flipp_dl/web/auth.py` och `/publications/{code}/watch` för mönstret). Ingen separat engångsnyckel - `/settings` är redan bakom inloggning när `FLIPP_PASSWORD` är satt, och att lägga till ännu en autentiseringsväg för samma formulär vore en onödig andra mekanism.

## Acceptance criteria
- [ ] Token kan sparas från /settings och används av FlippClient vid nästa anrop utan omstart. FLIPP_TOKEN fortsätter gälla som utgångsvärde när ingen token sparats.
- [ ] Värdet renderas aldrig tillbaka - visa maskerat, spara bara vid ändring, och lämna oförändrat vid tom inmatning.
- [ ] POST till token-fältet kräver giltig CSRF-token (samma `check_csrf_form`-mekanism som övriga formulär), och avvisas annars med samma mönster som t.ex. `/publications/{code}/watch`.
- [ ] Schedulern ska plocka upp en nyligen sparad token utan omstart - i dag skapas FlippClient en gång vid modulladdning i web/main.py.

## Implementation hints
Filer som väntas ändras: flipp_dl/web/routes.py, flipp_dl/web/templates/settings.html, flipp_dl/scheduler.py, flipp_dl/web/main.py, tests/test_web_routes.py

- Lagra token som en vanlig `DbSetting`-rad, t.ex. nyckeln `flipp_token`, via befintliga `repo.get_setting`/`repo.set_setting` (flipp_dl/db/repository.py:334-338) - samma mönster som `poll_interval`/`workers` i `settings_get`/`settings_post` (flipp_dl/web/routes.py:632-672). Ingen ny tabell eller kolumn behövs.
- `settings_post` ska bara anropa `set_setting("flipp_token", ...)` när fältet faktiskt skickats icke-tomt - annars orört, enligt kravet "lämna oförändrat vid tom inmatning".
- `settings.html` (flipp_dl/web/templates/settings.html): byt ut noten "It cannot be changed here..." mot ett formulärfält av typen `password` som aldrig fylls med det riktiga värdet - visa `placeholder="•••"` när en token redan är sparad, annars en hint om att FLIPP_TOKEN/token-filen används.
- Att slippa omstart: `flipp_dl/scheduler.py` (`poll_publications`, `run_download_queue`) och `flipp_dl/web/main.py` bygger i dag en `FlippClient` en gång och återanvänder samma instans i APScheduler-kwargs. `FlippClient.token` är ett vanligt attribut som läses per anrop i `_refresh_sign_in_token` (flipp_dl/api.py:136-140) - enklast är att läsa aktuell token ur DB (fallback `load_token()`) i början av varje `poll_publications`/`run_download_queue`-körning och tilldela `client.token = ...` innan API-anropet, i stället för att bygga om klientobjektet eller ändra funktionssignaturerna.
- `flipp_dl/web/routes.py` har ytterligare två ställen som bygger en `FlippClient` från `load_token()` direkt (`poll_single` runt rad 523, `debug_poll` runt rad 696) - dessa ska också läsa den sparade token-inställningen först, med `load_token()` som fallback.

## Verification
- `.venv/bin/python -m pytest tests/test_web_routes.py` - riktade tester för: spara token, maskering (svaret innehåller aldrig klartextvärdet), tom inmatning lämnar befintlig token orörd, och saknad/felaktig CSRF-token avvisas.
- `shot` av `/settings` vid 390px och 1280px: token-fältet syns maskerat (aldrig klartext), och en sparning visar en bekräftelse utan att avslöja värdet.

- ID: `01M0CYVE7SCF7220W9FPTSWJCM`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Publikationslistan gör en aggregerad räkning i stället för att ladda alla utgåvor

## Context
`/publications` laddar hela `issues`-relationen för alla publikationer (selectinload i `list_publications`) bara för att räkna två tal per rad. På driftinstansen är det 17660 rader som dras in vid varje sidladdning, trots att listvyn bara visar `num_issues` och `num_downloaded`.

## Acceptance criteria
- [ ] `list_publications()` i repository.py laddar inte längre `DbPublication.issues` - antalet utgåvor och antalet nedladdade räknas i databasen (t.ex. en aggregerad query som grupperar per `publication_id` och räknar rader totalt respektive filtrerat på `status = done`).
- [ ] `/publications`-listvyn visar samma tal som i dag i kolumnen Downloaded, för publikationer med och utan utgåvor.
- [ ] `/publications/{code}/watch` och `/publications/{code}/unwatch` (som renderar `publication_row.html` via HTMX) visar också rätt tal efter swap.
- [ ] `/publications/{code}`-detaljsidan är oförändrad och fortsätter ladda utgåvorna på riktigt (relationen får finnas kvar där, `get_publication()` rörs inte).
- [ ] Ingen SQL-fråga i `/publications` laddar issues-tabellens rader (bara aggregatet).

## Implementation hints
Filer som väntas ändras:
- flipp_dl/db/repository.py
- flipp_dl/db/models.py
- flipp_dl/web/routes.py
- tests/test_repository.py
- tests/test_web_routes.py

`DbPublication.num_issues` och `num_downloaded` (flipp_dl/db/models.py, rad ~82-88) är i dag `@property` som läser `self.issues` - det krockar med att sluta ladda relationen. Lösningen måste antingen ge dessa properties ett sätt att läsa ett förberäknat värde (satt av repository/route, likt hur `_annotate_file_exists` i web/routes.py sätter `issue.file_exists` som ett vanligt attribut) utan att skriva över en property utan setter, eller byta ut hur `publications.html`/`publication_row.html` hämtar talen. `list_issues_sharing_files()` (repository.py rad 165) och `count_issues_by_status()` (rad 193) är exempel på existerande gruppera/räkna-i-DB-mönster att följa.

## Verification
- `.venv/bin/python -m pytest tests/test_repository.py -k publications -v`
- `.venv/bin/python -m pytest tests/test_web_routes.py -k publications -v`
- Riktat test som registrerar en SQLAlchemy `before_cursor_execute`-lyssnare runt `GET /publications` och asserterar att ingen exekverad SQL innehåller `FROM issues` (eller motsvarande) - lägg till i tests/test_web_routes.py.

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

## [P3][done] [flipp] Navigeringsraden ger horisontell scroll vid 390px

## Context
Alla sidor har horisontell overflow i mobilbredd: vid 390px viewport blir `document.documentElement.scrollWidth` 553px, uppmätt på /, /jobs, /settings, /publications och /library. Orsaken är navigeringsraden i base.html - länkarna plus spacer-elementet ligger på en rad som är 557px bred och wrappar inte. Bekräftat befintligt fel, inte infört av kövyn i TASK-1330.

## Acceptance criteria
- [ ] `document.documentElement.scrollWidth` är lika med viewport-bredden (ingen horisontell overflow) vid 390px på /, /jobs, /settings, /publications och /library
- [ ] Samma sidor har fortsatt ingen horisontell overflow vid 1280px (regression ska inte införas på desktop)
- [ ] Navigeringslänkarna (Dashboard, Publications, Library, Jobs, Settings, Sign out) förblir alla klickbara och läsbara vid 390px, antingen genom wrapping eller horisontell scroll begränsad till själva nav-raden

## Implementation hints
Filer som väntas ändras: flipp_dl/web/templates/base.html

CSS-reglerna för `nav` ligger i `<style>`-blocket, rad ~14-22 (`nav`, `nav .brand`, `nav a`, `nav .spacer`, `nav .logout`). Navmarkupen är på rad ~215-226. Troligen behövs `flex-wrap: wrap` på `nav`, eventuellt kombinerat med mindre padding/gap vid smala viewports via en media query, eller ett `overflow-x: auto` begränsat till nav-elementet i stället för hela sidan.

## Verification
- Mät med Playwright-scriptet från browser-verify-skillen (`~/.local/share/shot-venv/bin/python`): loopa `[390, 1280]` över `/`, `/jobs`, `/settings`, `/publications`, `/library` och skriv ut `document.documentElement.scrollWidth` per sida/bredd - alla värden ska vara <= viewport-bredden
- `shot http://ubuntu-ai:PORT/ ut-390.png --width 390 --height 780 --wait networkidle` och `shot http://ubuntu-ai:PORT/ ut-1280.png --width 1280 --height 900 --wait networkidle` - ingen horisontell scrollbar ska synas, nav-länkarna ska vara läsbara
- Klicka igenom nav-länkarna i browser vid 390px (inte bara skärmdump) och verifiera att varje länk faktiskt navigerar till rätt sida

- ID: `01M0CRHTW8SEH2362Q0HFDKVA2`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Undersök om nedladdade PDF:er är vattenmärkta

Skanna de nedladdade PDF:erna efter spår som kan knyta filen till kontot: synlig vattenstämpel i sidbilden, osynlig text i textlagret, XMP/DocInfo-metadata, unika objekt-ID:n eller kontospecifika URL:er i sidornas resurser.

Utgå från filerna som redan ligger i output på driftinstansen. Jämför gärna samma utgåva hämtad vid två tillfällen - skiljer bytesekvenserna sig åt på ställen som inte är tidsstämplar är det ett tecken på per-nedladdning-märkning.

Rent utredande task: resultatet avgör om det behövs någon åtgärd alls, och i så fall vilken.

- ID: `01M0CR734QFWN9Z0M72HAVTJ99`
- Type: spike
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Behåll watched-only-filtret i URL:en på /publications

## Context
"Watched only"-kryssrutan på /publications är i dag ren klientside-state (JS-filter över raderna i publications.html:61-94, ingen koppling till URL:en). Filtret nollställs så fort man navigerar bort och tillbaka, t.ex. efter ett besök på en publikationssida, och går inte att bokmärka eller dela. Hänger ihop med TASK-1343 (watched only ska vara förkryssad som default) och ska genomföras tillsammans: en explicit URL-flagga ska alltid vinna över defaulten, så en delad länk utan filter fortfarande kan visa allt.

## Acceptance criteria
- [ ] Watched-only-läget speglas i URL:en (query-param, t.ex. `?watched=1`/`?watched=0`) och läses tillbaka vid sidladdning
- [ ] Att kryssa i/ur "Watched only" uppdaterar URL:en utan full sidomladdning (t.ex. `history.replaceState`)
- [ ] Navigerar man till en annan sida och tillbaka med webbläsarens bakåtknapp, eller laddar en sparad/delad URL med flaggan, återställs filtret till det URL:en anger
- [ ] En explicit URL-flagga vinner alltid över TASK-1343:s "förkryssad som default" - `?watched=0` visar alla publikationer trots defaulten
- [ ] Sökfältet och kategori-filtret på samma sida är antingen inkluderade i samma URL-mekanism, eller så finns ett medvetet beslut dokumenterat i implementationen om varför de lämnas som ren klientstate (avgör vid implementation, inte ny scope)
- [ ] Befintliga tester i tests/test_web_routes.py för /publications fortsätter gå igenom

## Implementation hints
Filer som väntas ändras: flipp_dl/web/templates/publications.html

Allt sker i `<script>`-blocket i publications.html (funktionen `applyFilter` och event-lyssnarna för `searchInput`/`categorySelect`/`watchedOnly`, rad ~57-97). Lägg till läsning av `URLSearchParams` vid sidladdning för att sätta initialt state, och skriv tillbaka till URL:en (`history.replaceState`) i respektive change-lyssnare. Samordnas med TASK-1343 som lägger till `checked`-defaulten på `#pub-watched` - bygg vidare på samma URL-läsning i stället för att duplicera logiken.

## Verification
- `.venv/bin/python -m pytest tests/test_web_routes.py -k publications` - befintliga routetester går igenom
- `shot "http://ubuntu-ai:PORT/publications?watched=1" ut-390.png --width 390 --height 780 --wait networkidle` och samma med `--width 1280 --height 900` - kryssrutan ska vara förbockad och bara bevakade rader synas vid båda bredderna
- `shot "http://ubuntu-ai:PORT/publications?watched=0" ut2-390.png --width 390 --height 780 --wait networkidle` och samma vid 1280px - kryssrutan ska vara avbockad och alla publikationer synas
- Klicka manuellt i kryssrutan i en riktig browser (obscura/Playwright) och verifiera att `location.search` faktiskt ändras efter klicket, inte bara att filtret renderas rätt vid laddning

- ID: `01M0CQJMJ82SZJQWQE73XDM6P0`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Komga nivå 2: pusha metadata och omslag

## Context
Komga visar i dag titel, nummer och omslag gissat ur filnamnet. Svenska serietidningar finns inte i Comicvine/GCD så ingen extern metadataprovider kan fylla i det åt oss - vi måste pusha det vi redan har från Flipp-API:t själva. Detta är nivå 2 av tre Komga-integrationer och förutsätter att nivå 1 (`KomgaClient`, settings, `komga_sync`-jobbet i TASK-1326) är på plats. Se den fästa planen "Genomförandeplan nivå 2" på tasken för de elva genomförandestegen - den här beskrivningen lägger bara till kriterier och verifiering ovanpå den planen, den ersätter den inte.

## Acceptance criteria
- [ ] Ny nullable kolumn `publications.komga_series_id` mappar en publikation mot dess Komga-serie, satt lat vid första lyckade matchning (aldrig omslagen igen).
- [ ] `/publications/{code}` visar Komga-status i headern: "Komga: synkad ✓ · serie #<id>" med länk till serien i Komga när mappning finns, annars "Komga: okänd - söker nästa gång".
- [ ] `komga_sync`-handlern (från TASK-1326) pushar serie-metadata (`title`, `titleSort`, `summary`, `publisher`, `language`, genrer/taggar) och bok-metadata (`title`, `number`/`numberSort`, `releaseDate`) efter en lyckad scan, med bok-uppslag som pollar upp till `KOMGA_WAIT_SECONDS` innan det ger upp med ett tydligt job-error.
- [ ] Fältpush kan stängas av per fält via en feature-flagga, t.ex. `KOMGA_PUSH_SUMMARY=false`, för den som hellre redigerar i Komgas UI.
- [ ] Flipps officiella omslag (`publication.cover_url`) laddas ner och pushas som thumbnail till serien, avstängbart via `KOMGA_PUSH_COVER`.

## Alembic: den här tasken äger revision 0007 (down_revision = "0006"). Revisionsnumret är förtilldelat och får inte ändras.

## Implementation hints
Filer som väntas ändras: flipp_dl/db/migrations/versions/0007_komga_series_id.py (nytt filnamn, revision-strängen är förtilldelad enligt ovan), flipp_dl/db/models.py, flipp_dl/db/repository.py, flipp_dl/komga.py, flipp_dl/scheduler.py, flipp_dl/web/routes.py, flipp_dl/web/templates/publication_detail.html, tests/test_repository.py, tests/test_komga_client.py, tests/test_scheduler.py

- `set_komga_series_id(custom_code, series_id)` och `get_unmapped_publications()` i `flipp_dl/db/repository.py`, i samma stil som befintliga publikations-metoder (`set_watched`, `get_publication`).
- Matchning: `GET /api/v1/series?search=<folder>&library_id=<id>` i `KomgaClient`, exakt match mot `_safe_name(publication.name)` (redan i `flipp_dl/storage.py`).
- Sanering av `publication.description` för `summary` går via befintlig `flipp_dl/web/html_sanitize.py` - återanvänd, uppfinn inte en ny saneringsväg.
- Håll fältmappningarna i en enda dict (`PUBLICATION_METADATA_FIELDS`/`ISSUE_METADATA_FIELDS`) i `flipp_dl/komga.py` så per-fält-flaggorna blir en enkel lookup.

## Verification
- `.venv/bin/python -m pytest tests/test_repository.py tests/test_komga_client.py tests/test_scheduler.py`
- `.venv/bin/python -m alembic upgrade head` körs rent från en tom databas (ny kolumn skapas, ingen krock med parallella revisioner).
- `shot` av `/publications/{code}` vid 390px och 1280px: Komga-statusraden ("synkad ✓ · serie #..." eller "okänd - söker nästa gång") syns i headern.

- ID: `01M0CQ9B9PB268MKEXSA6Q6W9A`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] Komga nivå 1: auto-scan vid ny nedladdning

## Context
Komga hittar i dag nya nedladdningar först vid nästa schemalagda filsystemsscan, vilket kan dröja timmar. Detta är nivå 1 av tre Komga-integrationer och grunden nivå 2 (metadatapush) och nivå 3 (lässtatus) bygger på. Se den fästa planen "Genomförandeplan nivå 1" på tasken för de sju genomförandestegen - den här beskrivningen lägger bara till kriterier och verifiering ovanpå den planen, den ersätter den inte.

## Acceptance criteria
- [ ] `KomgaClient` (ny klass i `flipp_dl/komga.py`) har `list_libraries()` och `scan_library(library_id)`, och stödjer både HTTP Basic-auth och `X-API-Key`-header.
- [ ] Nya inställningar `KOMGA_URL`, `KOMGA_USERNAME`, `KOMGA_PASSWORD`/`KOMGA_API_KEY`, `KOMGA_LIBRARY_ID`, `KOMGA_ENABLED` lagras som `DbSetting`-rader (samma mönster som `poll_interval`/`workers`) och kan overridas av miljövariabler.
- [ ] `/settings` har en Komga-sektion: URL/credential-fält samt en "Test connection"-knapp (HTMX) som anropar `list_libraries()` och fyller en dropdown för `KOMGA_LIBRARY_ID`. Secrets renderas aldrig tillbaka i klartext - tomt fält + `placeholder="•••"` när ett värde redan är sparat, och sparas bara vid faktisk ändring (samma mönster som ska användas i TASK-1342 för Flipp-token).
- [ ] En lyckad nedladdning i `run_download_queue()` köar ett `komga_sync`-jobb (ny jobtyp i `jobs`-tabellen) i stället för att blockera download-loopen.
- [ ] En handler dränerar `komga_sync`-jobb: `POST /api/v1/libraries/{id}/scan` följt av `finish_job`. Ett Komga-fel loggas som job-error men rör aldrig issue-statusen - en nedladdning som lyckades ska förbli `done` även om Komga ligger nere.
- [ ] Är `KOMGA_ENABLED` av (default) görs inget Komga-arbete alls - varken UI-anrop eller jobbköande.

## Implementation hints
Filer som väntas ändras: flipp_dl/komga.py, flipp_dl/db/repository.py, flipp_dl/scheduler.py, flipp_dl/web/routes.py, flipp_dl/web/templates/settings.html, flipp_dl/web/main.py, tests/test_komga_client.py, tests/test_scheduler.py, tests/test_web_routes.py

- `flipp_dl/komga.py` är ny och följer samma stil som `flipp_dl/api.py` (session med retry, `KomgaError`-exception).
- Jobbköet är redan generiskt per `job_type` (`create_job`, `get_oldest_queued_job`, `finish_job` i `flipp_dl/db/repository.py`) - `komga_sync` är bara ytterligare en `job_type`, ingen ny tabell eller kolumn behövs.
- `run_download_queue()` i `flipp_dl/scheduler.py` är stället som köar `komga_sync` efter en lyckad `downloader.download_issue(...)`. En ny funktion (t.ex. `run_komga_sync_queue()`) dränerar den kön - se `_claim_next_download_job` som mall för atomärt claim.
- Både `flipp_dl/scheduler.py` (CLI `build_scheduler`) och `flipp_dl/web/main.py` (in-process `BackgroundScheduler`) behöver koppla in det nya jobbet/den nya draineringen.
- Följ designvalen i den fästa planen (blocking vs fire-and-forget, batchning, error-policy, multi-library) - de är öppna att avgöra under arbetet, dokumentera valet i PR/commit.

## Verification
- `.venv/bin/python -m pytest tests/test_komga_client.py tests/test_scheduler.py tests/test_web_routes.py`
- `shot` av `/settings` vid 390px och 1280px: Komga-sektionen syns med URL/credential-fält, "Test connection"-knapp och en library-dropdown som fylls efter klick. Inga tidigare sparade secrets syns i klartext.

- ID: `01M0CQ9B98BGRYSPCVA6VRM1B6`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P3][done] [flipp] JSON-API-endpoints för publikationer, utgåvor och jobbstatus

## Context
Webbgränssnittet (FastAPI + Jinja2/HTMX) exponerar i dag bara HTML-sidor - inga renodlade REST/JSON-endpoints för programmatisk åtkomst till publikationer, utgåvor eller jobbstatus. `flipp_dl/web/routes.py` är redan 737 rader, så nya JSON-endpoints hör hemma i en egen modul snarare än att växa den filen ytterligare.

## Acceptance criteria
- [ ] `GET /api/publications` returnerar JSON-lista över publikationer (custom_code, name, watched, num_issues, num_downloaded, next_issue_date) - samma data som `/publications`-sidan visar, utan HTML.
- [ ] `GET /api/publications/{code}` returnerar JSON för en enskild publikation inklusive dess utgåvor (custom_code, issue_name, issue_date, status, downloaded_at) eller 404 med JSON-felkropp om koden inte finns.
- [ ] `GET /api/jobs` returnerar JSON-lista över senaste jobb (id, job_type, status, created_at, started_at, finished_at, error_message), med samma `status`-filter som `/jobs`-sidan stödjer.
- [ ] Alla nya endpoints svarar `Content-Type: application/json` och går genom samma `AuthMiddleware` som HTML-sidorna (ingen ny oskyddad yta).
- [ ] Endpoints läggs i en egen modul, inte i den redan 737 rader stora `routes.py`.

## Implementation hints
Filer som väntas ändras:
- flipp_dl/web/api_routes.py (ny fil)
- flipp_dl/web/app.py
- flipp_dl/db/repository.py (om ett nytt frågemönster behövs, t.ex. jobbfilter - `list_jobs`/`count_jobs_by_status` finns redan och kan sannolikt återanvändas rakt av)
- tests/test_web_routes.py (eller ny tests/test_api_routes.py)

Återanvänd befintliga repository-metoder rakt av i stället för att duplicera frågor: `list_publications()` (repository.py rad 93, se även TASK-1338 som ändrar hur den räknar `num_issues`/`num_downloaded`), `get_publication()` (rad 83), `list_jobs()`/`count_jobs_by_status()`. `_dashboard_stats()` i routes.py (rad 133) är ett exempel på hur siffrorna redan paketeras för UI:t - JSON-svaret kan spegla samma fält. Registrera den nya routern i `create_app()` (web/app.py, runt rad 78-102) efter att `AuthMiddleware` lagts till, så skyddet gäller även API:t.

## Verification
- `.venv/bin/python -m pytest tests/test_web_routes.py -k api -v` (eller motsvarande nya testfil)
- `curl -s http://localhost:8000/api/publications | python -m json.tool` mot en lokalt körande instans - kontrollera fälten stämmer mot en publikation som också syns på `/publications`.
- `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/publications/finns-inte` ska ge 404.

- ID: `01M0CPHHZ21BBEMWPE99Z8JJJN`
- Type: feature
- Actor: ai:claude-code

---

## [P3][done] [flipp] Eget pollintervall per publikation

## Context
Alla bevakade publikationer pollas i dag med samma globala intervall (APScheduler). En del publikationer ges ut en gång i månaden och behöver inte kollas lika ofta som en veckotidning - ett eget schema per publikation minskar onödiga körningar och gör det möjligt att lägga tunga publikationer på natten.

## Acceptance criteria
- [ ] En publikation kan få ett eget pollintervall (minuter) som avviker från det globala default-intervallet i `/settings`. Blankt/ej satt = använd det globala intervallet som i dag - ingen ändring i beteende för publikationer utan override.
- [ ] `poll_publications()` köar nya utgåvor (och kör backfill) för en publikation med eget intervall bara när tillräckligt lång tid gått sedan den senast kollades - inte vid varje global tick.
- [ ] Publikationer utan eget intervall beter sig exakt som i dag: kollas vid varje global poll.
- [ ] Metadatasynken (namn, omslag, beskrivning) från den befintliga globala API-hämtningen påverkas inte av per-publikations-schemat - flipp-dl gör fortfarande ett enda API-anrop per global tick, schemat styr bara vilka publikationer som får sina nya utgåvor köade för nedladdning den tick:en.
- [ ] `/publications/{code}` visar och låter användaren ändra publikationens eget pollintervall (blankt fält = använd globalt default).

## Alembic: den här tasken äger revision 0006 (down_revision = "0005"). Revisionsnumret är förtilldelat och får inte ändras. Landar tasken före 0005 ska den ändå behålla sitt nummer och sin down_revision.

## Implementation hints
Filer som väntas ändras: flipp_dl/db/models.py, flipp_dl/db/migrations/versions/0006_publication_poll_interval.py (nytt), flipp_dl/db/repository.py, flipp_dl/scheduler.py, flipp_dl/web/routes.py, flipp_dl/web/templates/publication_detail.html, tests/test_repository.py, tests/test_scheduler.py, tests/test_web_routes.py

- `DbPublication` (flipp_dl/db/models.py) får en nullable `poll_interval_minutes: int | None` och en bokföringskolumn, t.ex. `next_poll_due_at: datetime | None`, för att veta när publikationen senast blev "kollad" oavsett den globala tickens takt.
- `DownloadRepository.mark_polled()` (flipp_dl/db/repository.py:110) finns redan men anropas inte i dag - den är en rimlig utgångspunkt för bokföringen, men kolla att `sync_publications()` (rad 312-328) inte redan skriver över `last_polled_at` på ett sätt som krockar med den nya bokföringen; en separat kolumn är sannolikt tydligare än att återanvända `last_polled_at`.
- `poll_publications()` i `flipp_dl/scheduler.py` (rad 45-100) bygger redan `watched_pub_ids` och loopar över `new_issues` samt kör `queue_missing_issues()` per bevakad publikation - lägg schemakontrollen där, som ett filter på vilka publikations-id:n som räknas som "due" den här ticken, innan köandet och backfillen körs.
- Route-mönster att följa för UI-formuläret: `/publications/{code}/watch` i `flipp_dl/web/routes.py` (rad 216-235) - samma CSRF-check (`check_csrf_form`), samma sätt att hämta repo/session.

## Verification
- `.venv/bin/python -m pytest tests/test_repository.py tests/test_scheduler.py tests/test_web_routes.py`
- `.venv/bin/python -m alembic upgrade head` körs rent från en tom databas.
- `shot` av `/publications/{code}` vid 390px och 1280px: ett fält för eget pollintervall syns och går att spara; sparat värde visas efter reload.

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

## [P3][done] [flipp] Importera befintligt nuläge från nedladdade filer i output, med avvikelserapport i båda riktningarna

## Context
En ny/tom databas kan inte skilja på "aldrig nedladdad" och "redan ligger på disk" - allt måste laddas ner om. Ett svep av driftinstansen (2026-08-18) hittade dessutom två avvikelser som en sådan import skulle ha fångat: en publikation där två utgåvor delade samma fil, och en utgåva som låg på disk men stod som `queued` i databasen. Importen ska alltså både fylla i saknad status och rapportera de avvikelser den ser, i båda riktningarna.

## Acceptance criteria
- [ ] Ett nytt repository-/importsteg skannar `output_root` (samma katalogstruktur som `storage.py`: `<publikationsnamn>/<issue_name>.pdf` via `safe_name`) och matchar filer mot utgåvor i DB via samma namnregel som `issue_path()`.
- [ ] En fil på disk vars namn matchar en utgåva som INTE står som `done` (t.ex. `queued`, `new`, `error`) uppdateras till `done` med `file_path` och `downloaded_at` satta - utan att filen laddas ner igen.
- [ ] Importen rapporterar filer på disk som inte matchar någon utgåva i DB (orphan-filer) - antal och sökväg.
- [ ] Importen rapporterar utgåvor vars `file_path` är satt i DB men filen saknas på disk - antal och issue-identitet (samma riktning som redan täcks delvis av `_annotate_file_exists` i routes.py, men nu som en samlad lista i stället för per-rad-flagga).
- [ ] Två utgåvor som redan delar samma `file_path` (jfr `list_issues_sharing_files()`) flaggas i rapporten i stället för att importen tyst skriver över den ena.
- [ ] Går att köra som ett CLI-flagga/-kommando (t.ex. `--import-existing`) som skriver en sammanfattning till stdout och avslutar.
- [ ] Går att köra som en knapp i webbgränssnittet (t.ex. på settings-sidan) som kör samma logik och visar rapporten i UI:t.

## Implementation hints
Filer som väntas ändras:
- flipp_dl/db/repository.py
- flipp_dl/storage.py
- flipp_dl/cli.py
- flipp_dl/web/routes.py
- flipp_dl/web/templates/settings.html
- tests/test_repository.py
- tests/test_cli.py
- tests/test_web_routes.py

Bygg vidare på befintliga byggstenar i stället för att uppfinna nya: `list_issues_sharing_files()` (repository.py rad 165) är redan en besläktad kontroll att läsa innan du börjar - dela gärna hjälpfunktion för att gruppera issues på `file_path`. `get_issue_by_file_path()` (rad 159) och `mark_issue_done()` (rad 277) finns redan. `_annotate_file_exists()` i web/routes.py (rad 61) gör disk-koll per-rad för visning - importen behöver samma kontroll fast som en batch-rapport. `storage.safe_name()`/`issue_path()` är namnregeln att matcha mot, inklusive `disambiguate`-varianten (två utgåvor kan dela namn - se TASK-1349). cli.py har i dag inga subkommandon, bara flaggor på huvudparsern (`build_parser()`) - följ det mönstret, t.ex. en `--import-existing`-flagga som körs och avslutar innan poll/download-flödet startar.

## Verification
- `.venv/bin/python -m pytest tests/test_repository.py -k import -v`
- `.venv/bin/python -m pytest tests/test_cli.py -k import -v`
- `.venv/bin/python -m pytest tests/test_web_routes.py -k import -v`
- Manuellt: skapa en fil på disk som matchar en `queued`-utgåva, kör importen, kontrollera att utgåvan blir `done` utan nätverksanrop.
- Browser: `shot` av /settings vid 390px och vid 1280px - knappen för att importera nuläge ska synas och vara klickbar i båda bredderna, och efter klick ska rapporten (orphan-filer, saknade filer, delade filer) renderas synligt på sidan.

- ID: `01M0BBY3X4VKXXTRYY9T2EGDP4`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P4][todo] [flipp] Låt inte mappnamn och destination beskriva en flytt som inte är färdig

## Context

Avknoppad från TASK-1482, som Judge godkände med tre observationer. Två av
dem hänger ihop och är värda att städa, ingen av dem bryter mot något
acceptanskriterium.

1. `DownloadRepository._move_publication_files` (repository.py:685) sätter
   `publication.folder_name` respektive `publication.destination` INNAN
   flytten körs, och det värdet committas tillsammans med den första fil
   som lyckas. Misslyckas en senare fil beskriver fältet en plats som inte
   alla filer nått. Varje utgåvas `file_path` är fortfarande korrekt, så
   inget pekar fel - men publikationens fält gör det, tills en omkörning
   blir klar.
2. Samma metod anropar `self.session.commit()` inne i sin loop. Det
   committar HELA sessionen, inte bara metodens egna skrivningar. Alla
   nuvarande anropare öppnar en egen session för just flytten, så det är
   ofarligt i dag, men mönstret är skört: en framtida anropare som köar
   andra ändringar före flytten får dem committade mitt i, utan väg
   tillbaka om flytten sedan faller.

Den tredje observationen (fsync i EXDEV-vägen) är redan åtgärdad i
TASK-1482.

## Acceptance criteria

- [ ] En delvis misslyckad flytt lämnar antingen fältet orört tills alla
      filer nått fram, eller gör avvikelsen synlig för användaren i stället
      för att tyst påstå fel plats.
- [ ] Flyttmetoden committar inte anroparens orelaterade ändringar. Antingen
      dokumenteras kontraktet explicit och kontrolleras, eller så begränsas
      committen till metodens egna rader.
- [ ] Test som visar det valda beteendet vid en flytt där andra filen felar.

## Verification

- `.venv/bin/python -m pytest tests/test_repository.py -q`

- ID: `01M0Q01E210P4YMBCRG2X8WG8Y`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P4][todo] [flipp] Städa efter kvällens omflyttning: papperskorg, dubbletter, testtokens

Praktiska steg som väntar på Rasmus efter omflyttningen 2026-08-21/22. Ingen kodändring.

1. Töm Komgas papperskorg i BÅDA biblioteken (Skannade och Flipp). Där ligger poster för de 121 filer som döptes om, plus de 560 dubbletterna vars filer flyttats till ws. Komga matchar via filhash, så kontrollera först att de nya posterna finns i Flipp-biblioteket - efter tömning går ingen återställning.

2. Radera /mnt/user/media/Serier/ws/_dubbletter, 30 GB. Det är 560 PDF:er som är äldre kopior av det som finns i Manuella/Flipp-dl. Verifierat fil för fil.

3. Ta bort två engångstokens i ntfy som mintades under verifiering: svc-flipp-dl-test och svc-flipp-dl-felnotis. RÖR INTE svc-flipp-dl - den används i drift.
   docker exec Ntfy ntfy token list svcpub
   docker exec Ntfy ntfy token remove svcpub <id>

4. Containern flipp-dl-dev ligger fyra commits efter branchen (felnotiserna, StrEnum, README). Uppdatera via Unraid GraphQL updateContainer när kön är tom.

- ID: `01M0NH9HGSCX8XQ4WG3S49VG5M`
- Type: chore
- Actor: ai:claude-code

---

## [P4][done] [flipp] Överväg StrEnum för IssueStatus och JobStatus

Ruff UP042 flaggar att IssueStatus och JobStatus ärver från både str och Enum, och föreslår StrEnum (3.11+). Regeln är undantagen i pyproject.toml med motivering, men bytet är värt ett eget beslut - därför den här tasken i stället för bara en kodkommentar.

Upptäckt 2026-08-22 när verktygsmålet höjdes från py39 till py311 (commit ffb386d).

Varför det inte gjordes direkt: StrEnum ändrar vad str() och format() ger tillbaka. Med (str, Enum) ger str(JobStatus.QUEUED) strängen "JobStatus.QUEUED"; med StrEnum ger den "queued". Koden har redan en kommentar om precis det i routes.py, där _JOB_STATUSES bygger på .value just för att undvika fällan i URL:er och mallar.

Omfattning: medlemmarna används på ungefär 70 ställen - repository.py 49, routes.py 11, models.py 5, api_routes.py 4, plus opds.py och codes_routes.py. De jämförs dessutom mot strängar som redan ligger i databasen.

Att göra:
- Gå igenom varje jämförelse och varje ställe där en status renderas eller hamnar i en URL.
- Kontrollera att lagrade värden fortsätter matcha (databasen har rader med "done", "queued" osv).
- Ta bort UP042 ur ignore-listan i pyproject.toml när det är gjort.

Klart när: ruff är grön utan UP042-undantaget och hela sviten passerar. Vinsten är främst att en status inte längre kan råka renderas som "JobStatus.QUEUED".

- ID: `01M0N9VE422MA4YQ6QC9W1RKQ6`
- Type: improvement
- Actor: ai:claude-code

---

## [P4][done] [flipp] Väljarna för destination och notiser är inte stylade

Rasmus 2026-08-22: rullgardinerna "Primary output root" (destination) och "Notify on new issues" på publikationssidan ser inte ut som resten av gränssnittet.

Orsak: båda är <select class="inline-input">. Den klassen är skriven för textfält (base.html: bakgrund, ram, padding), och det finns en separat regel för .field select - men de här ligger inte i ett .field-block utan i en inline .detail-actions-rad. Så de faller tillbaka på webbläsarens default-select, vilket syns tydligt mot det mörka temat.

Att göra:
- Ge select en egen stil som matchar inline-input, eller lägg till select i den befintliga .inline-input-regeln i base.html.
- Kontrollera samtidigt de andra inline-väljarna på sidan (statusfilter, läst/oläst) så de blir konsekventa.

Klart när: väljarna ser ut som de intilliggande textfälten i mörkt tema. Verifiera med shot vid 390 OCH 1280 px, se browser-verify-skillen.

- ID: `01M0K6NSYJJ0BJXSE6J9HYTAYX`
- Type: bug
- Actor: ai:claude-code

---

## [P4][done] [flipp] Publikationssidan har horisontell overflow på mobil

Vid 390 px breda viewport är document.documentElement.scrollWidth 412, alltså 22 px horisontell scroll. Mätt 2026-08-21.

Det som spiller över är sidans egen container, inte något enskilt fält: DIV.detail-info, H1 och DIV.detail-actions är alla 396 px breda i en 390 px vy och slutar på x=412.

Verifierat att det INTE är nytt: samma 412 med och utan notisväxeln (mätt genom att stasha publication_detail.html och mäta om). Buggen fanns alltså före TASK-1447.

Klart när: scrollWidth == viewportbredden vid 390 px. Verifiera med Playwright vid 390 OCH 1280 px, se browser-verify-skillen.

- ID: `01M0JW6X8APZ8FACCNE2XXGA23`
- Type: bug
- Actor: ai:claude-code

---

## [P4][done] [flipp] Kopiera-knappen i debug-vyn gör inget över vanlig http

Knappen Copy JSON använder navigator.clipboard, som bara finns i säker kontext. Instansen nås över http på hemnätet, så API:t saknas helt och knappen gör ingenting - felgrenen körs inte ens, anropet kastar direkt.

Samma problem fanns i kodsnutten för token (TASK-1390) och löstes där genom att markera texten och be användaren trycka Ctrl+C när clipboard saknas. Använd samma lösning här.

Verifiering: klicka knappen i webbläsaren mot instansen över http och kontrollera att något faktiskt händer.

- ID: `01M0GC2BWG7A7JZC1W35NRCD50`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P4][done] [flipp] Filträdsväljare för sökvägsfält

## Context
Sökvägar matas in som fritext, vilket betyder att man måste veta exakt hur katalogstrukturen ser ut inifrån containern och skriva rätt på första försöket. Ett stavfel eller en katalog som inte är skrivbar upptäcks först när något går fel.

En sökikon intill fältet ska öppna en enkel filträdsväljare: navigera nedåt, en nivå upp, och välj denna katalog - varpå sökvägen skrivs in i textfältet.

## Säkerhet, avgörande för designen
Detta är en inloggad webbtjänst som skulle kunna lista godtyckliga kataloger. Väljaren ska utgå från en vitlista av rötter, inte från filsystemets rot, och kontrollen ska ligga i endpointen - inte bara i gränssnittet. Samma containment-tänk som resolve_safe_path redan gör för filserveringen.

## Vad användaren ser
Tjänsten kör i container och ser bara sina monterade volymer. Väljaren visar alltså containerns vy, exempelvis /output och /data, medan användaren tänker i värdens sökvägar som /mnt/user/Downloads/Flipp. Säg det tydligt i gränssnittet så ingen letar efter sin NAS-struktur.

## Acceptance criteria
- [ ] En sökikon intill sökvägsfältet öppnar väljaren. Vald katalog skrivs in i fältet.
- [ ] Navigering nedåt i underkataloger och en nivå upp, aldrig ovanför den vitlistade roten.
- [ ] Endpointen vägrar lista kataloger utanför vitlistan även vid handskrivna anrop med .. eller absoluta sökvägar.
- [ ] Varje katalog visar om den är skrivbar. En icke skrivbar katalog går inte att välja, eller varnar tydligt.
- [ ] Tom eller oläsbar katalog visas som tom, inte som ett fel.
- [ ] Ny text går genom gettext.

## Verification
- Tester för traversal-försök: .., absoluta sökvägar, symlänk som pekar ut ur roten.
- Browser: öppna väljaren, navigera ned och upp, välj en katalog och kontrollera att fältet fylls. Vid 390px och 1280px.

## Läget i flipp-dl
Här finns i dag INGET sökvägsfält i gränssnittet - utdatakatalogen sätts med FLIPP_OUTPUT och databasen med FLIPP_DB, båda som miljövariabler. Tasken blir därför aktuell först om eller när någon sökväg ska gå att ställa in i gränssnittet. Prioriterad lägre av det skälet.

Samma task finns i prenly-dl (prenly TASK-1402), där fältet redan finns och behovet är konkret. Bygg där först och återanvänd lösningen här.

## Byte av sökväg ska flytta det som redan finns (Rasmus 2026-08-20)
Att peka om utdatakatalogen får inte lämna kvar filerna på gamla stället. När sökvägen ändras ska befintliga filer flyttas med, och file_path i databasen uppdateras.

Att tänka igenom:
- Flytten kan gälla tiotals gigabyte. Den ska gå att avbryta och återuppta, och en avbruten flytt får inte lämna databasen pekande på filer som inte finns.
- Ligger målet på en annan volym fungerar inte rename - då krävs kopiera och radera, med kontroll att kopian är komplett innan originalet tas bort.
- Under flytten ska nedladdningar inte skriva till den gamla katalogen. Avgör om kön pausas eller om bytet vägras medan jobb pågår.
- Rapportera resultatet: hur många filer som flyttades, och vilka som inte kunde flyttas.

- ID: `01M0G44NDJ25HTQD7HWX21GXMP`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P4][done] [flipp] Uppdatera test_komga_test_connection_shows_error_on_failure efter TASK-1388

Avknoppad från TASK-1388. tests/test_web_routes.py:1641 asserterar att den råa tekniska texten ('connection refused' i str(KomgaError)) syns i svaret från POST /settings/komga/test. TASK-1388 gjorde det medvetet till ett kort, icke-tekniskt meddelande i stället (kategoriserat via KomgaError.reason, se flipp_dl/komga.py och flipp_dl/web/routes.py::_KOMGA_TEST_CONNECTION_MESSAGES). Assertionen är nu obsolet och testet failar.

Acceptanskriterier:
- Uppdatera testet så det konstruerar KomgaError med en explicit reason (t.ex. reason="other" ger 'something went wrong. Check the address and try again.') och assertar det korta meddelandet i stället för råtexten.
- .venv/bin/python -m pytest -q tests/test_web_routes.py -k test_komga_test_connection_shows_error_on_failure går grönt.

Filer som väntas ändras: tests/test_web_routes.py.

- ID: `01M0FYPA6K55CDDAHBWC3MNHV4`
- Type: chore
- Actor: ai:claude-sonnet-5

---

## [P4][done] [flipp] Anslutningsfel mot Komga visas som rå Python-stacktext

Felrutan är stilad sedan TASK-1387, men innehållet är obegripligt för den som bara vill koppla upp sig. Ett anslutningsfel visas i dag som:

  Could not connect to Komga: Komga request to http://127.0.0.1:9/api/v1/libraries failed: HTTPConnectionPool(host=127.0.0.1, port=9): Max retries exceeded with url: /api/v1/libraries (Caused by NewConnectionError(HTTPConnection object: Failed to establish a new connection: [Errno 111] Connection refused))

Det är requests interna undantagstext rakt igenom. Användaren behöver veta vad som är fel och vad hen kan göra: att adressen inte svarar, att inloggningen nekades, eller att svaret inte såg ut som Komga.

Acceptanskriterier:
- Vanliga fall ger ett kort, begripligt meddelande: adressen svarar inte, fel användarnamn eller lösenord, adressen svarade men verkar inte vara en Komga-instans.
- Den tekniska texten kastas inte bort utan loggas, så felsökning fortfarande är möjlig.
- Meddelandet går genom gettext som resten av gränssnittet.

Filer som väntas ändras: flipp_dl/komga.py, flipp_dl/web/routes.py, tests/test_komga_client.py, locale-filerna.

Verifiering: klicka Test connection mot en adress som vägrar anslutning och en som svarar med fel innehåll, och kontrollera texten vid 390px och 1280px.

- ID: `01M0FY1PBK12X701E4ZSBTYYJ4`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P4][done] [flipp] Felrutan vid misslyckad Komga-anslutning är ostilad

Hittat under TASK-1385. Fältstilarna på inställningssidan är åtgärdade, men error-diven som visas när Test connection misslyckas har ingen stil alls - den ritas som ren svart text utan bakgrund eller ram, till skillnad från övriga meddelanden i gränssnittet.

Ge den samma formspråk som andra felmeddelanden, exempelvis .debug-error eller badge-error som redan finns i base.html.

Verifiering: klicka Test connection mot en ogiltig adress och skärmdumpa resultatet vid 390px och 1280px.

- ID: `01M0FXH62B2SSPHYMAV82RQCA5`
- Type: bug
- Actor: ai:claude-opus-5

---

## [P4][done] [flipp] Lägg tröskeln för köstorlek som fält på inställningssidan

Tröskeln som avgör när en bakkatalogshämtning kräver extra bekräftelse (TASK-1361) går att sätta som DbSetting-nyckeln queue_warn_threshold_bytes eller env FLIPP_QUEUE_WARN_THRESHOLD_BYTES, med 5 GiB som default. Den saknar dock fält på /settings, så den går i praktiken bara att ändra genom att sätta en miljövariabel och starta om.

Lägg ett fält bland de övriga inställningarna. Rimligen i gigabyte snarare än bytes, eftersom det är så gränsen diskuteras.

Filer som väntas ändras: flipp_dl/web/templates/settings.html, flipp_dl/web/routes.py, tests/test_web_routes.py, plus pybabel-uppdatering för den nya texten.

Verifiering: test som sparar ett värde och kontrollerar att tröskeln följer med, samt browser-verifiering av fältet vid 390px och 1280px.

- ID: `01M0FDZA9G5SSC7BMMJ2YYRZYH`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P4][done] [flipp] Komga nivå 3: synka lässtatus tillbaka till flipp-dl

## Context
Beror på att TASK-1327 (Komga nivå 2) landar först - den här tasken behöver bok-uppslaget (`book_id`) som nivå 2 bygger upp vid metadatapush. Utan det finns inget att fråga Komga om lässtatus för.

Visa lästa/olästa utgåvor i flipp-dl:s eget gränssnitt genom att hämta läsprogress från Komga, i stället för att behöva öppna Komga för att se vad man redan läst. Valfri och långt fram bland de tre Komga-nivåerna. Se den fästa planen "Genomförandeplan nivå 3" på tasken för de tre genomförandestegen - den här beskrivningen lägger bara till kriterier och verifiering ovanpå den planen, den ersätter den inte.

## Acceptance criteria
- [ ] `KomgaClient.get_book_read_progress(book_id)` returnerar läst/oläst-status, sida och completed-flagga.
- [ ] Lässtatus cachas lokalt (ny kolumn eller separat tabell, se planen) och uppdateras schemalagt en gång per dygn - inte vid varje sidladdning.
- [ ] Issue-tabellen på `/publications/{code}` visar en "Läst"-badge per utgåva som har en Komga-bok-mappning.
- [ ] Filter-baren på samma sida har ett läst/oläst-filter som fungerar tillsammans med befintlig sökning och kategorifilter.
- [ ] Utgåvor utan Komga-bok-mappning (t.ex. innan nivå 2 hunnit synka dem) visar varken badge eller påverkas av filtret - inget krasch, bara frånvaro av status.

## Implementation hints
Filer som väntas ändras: flipp_dl/db/models.py, flipp_dl/db/migrations/versions/ (ny revision, nummer tilldelas när tasken plockas upp eftersom den beror på att TASK-1327 landar först och äger nästa lediga nummer efter 0007), flipp_dl/db/repository.py, flipp_dl/komga.py, flipp_dl/scheduler.py, flipp_dl/web/routes.py, flipp_dl/web/templates/publication_detail.html, flipp_dl/web/templates/issue_row.html, tests/test_repository.py, tests/test_komga_client.py, tests/test_web_routes.py

- Läs `book_id`-mappningen som TASK-1327 bygger upp innan den här tasken påbörjas - utan den finns inget att fråga Komga om.
- Det dagliga jobbet följer samma mönster som `poll_publications`/`run_download_queue` i `flipp_dl/scheduler.py`: en ny funktion, en ny APScheduler-registrering i både `flipp_dl/scheduler.py` (CLI) och `flipp_dl/web/main.py` (web-process).
- Filtret i UI:t byggs vidare på det befintliga filter-bar-mönstret i `flipp_dl/web/templates/publication_detail.html` (samma HTMX-sök/kategori-filter som redan finns där).

## Verification
- `.venv/bin/python -m pytest tests/test_repository.py tests/test_komga_client.py tests/test_web_routes.py`
- `shot` av `/publications/{code}` vid 390px och 1280px: "Läst"-badge syns på utgåvor med lässtatus, och läst/oläst-filtret går att klicka och faktiskt filtrerar listan.

- ID: `01M0CQ9BA2GVHXHYXAHEB3GGFD`
- Type: feature
- Actor: ai:claude-opus-5

---

## [P4][done] [flipp] i18n i webbgränssnittet med gettext

## Context
Webb-UI:t (`flipp_dl/web/templates/*.html`) är idag helt engelskt - `<html lang="en">` och all UI-text hårdkodad i templates. Ägaren skriver och tänker på svenska, vilket gör i18n relevant trots att verktyget bara har en användare.

## Beslut (Rasmus, 2026-08-19)
gettext/Babel, inte Jinja-ordlistor. Det innebär .po-filer, ett extraktionssteg och Babel som beroende - dokumentera kommandot för att extrahera och kompilera i README så det inte blir en tyst rutin. Engelska behålls som fallback-språk, svenska läggs till.


## Acceptance criteria
- [ ] All hårdkodad UI-text i `flipp_dl/web/templates/*.html` går via en översättningsmekanism i stället för att stå direkt i markupen.
- [ ] Språk kan väljas (t.ex. via en `DbSetting`-nyckel eller en query-/session-parameter) och slår igenom på alla sidor, inte bara en delmängd.
- [ ] `<html lang="...">` i `base.html` speglar det aktiva språket.
- [ ] Saknas en översättning för en sträng i det valda språket faller UI:t tillbaka till den andra varianten i stället för att visa en tom sträng eller en nyckel som `missing.key`.
- [ ] Om båda språken (svenska/engelska) implementeras: minst en sida per språk renderas testat och verifierat manuellt fri från kvarvarande hårdkodad text på fel språk.

## Implementation hints
Filer som väntas ändras: samtliga `flipp_dl/web/templates/*.html` (base.html, dashboard.html, publications.html, publication_detail.html, library.html, jobs.html, job_detail.html, settings.html, login.html, issue_row.html, publication_row.html, stats_cards.html, debug_poll_result.html), `flipp_dl/web/app.py` (registrera översättningsfilter/funktion i Jinja-miljön), en ny modul för själva ordlistorna (t.ex. `flipp_dl/web/i18n.py`), samt ev. `flipp_dl/web/routes.py` för språkval/settings. Tester: ny `tests/test_i18n.py`, ev. tillägg i `tests/test_web_routes.py`.

## Verification
- `pytest tests/test_i18n.py tests/test_web_routes.py -q`
- Manuellt: väx mellan språken i UI:t och kontrollera att dashboard, publications och settings visar konsekvent språk, inklusive `<html lang>`.

- ID: `01M0CPHJ034MCD47N7BEXB93EQ`
- Type: feature
- Actor: ai:claude-code

---

## [P4][done] [flipp] Metrics-endpoint (Prometheus)

## Context
flipp-dl har idag ingen extern observerbarhet utöver webb-UI:t (`/`, `/jobs`) och loggarna. En Prometheus-metrics-endpoint gör det möjligt att larma på (t.ex.) en växande felkö eller en stillastående nedladdningsprocess utan att manuellt öppna dashboarden.

## Acceptance criteria
- [ ] `GET /metrics` svarar med `text/plain` i Prometheus text-exposition-format, oskyddat av auth-middlewaren på samma sätt som `/healthz` (så ett Prometheus-scrape inte kräver inloggning) - eller uttryckligen dokumenterat om det ska kräva auth.
- [ ] Exponerar antal utgåvor per status (new/queued/downloading/done/error) som en gauge, byggt ovanpå `DownloadRepository.count_issues_by_status()`.
- [ ] Exponerar antal jobb per status (queued/running/done/error) som en gauge, byggt ovanpå `DownloadRepository.count_jobs_by_status()`.
- [ ] Exponerar totalt antal watchade respektive totalt antal publikationer, byggt ovanpå `DownloadRepository.count_publications()`.
- [ ] Endpointen läggs inte till i OpenAPI-schemat om övriga interna endpoints (`/healthz`) inte heller är det - samma `include_in_schema=False`-mönster.

## Implementation hints
Filer som väntas ändras: `flipp_dl/web/routes.py` (ny `/metrics`-endpoint, troligen med `prometheus-client`-biblioteket eller ett handskrivet text-svar), `requirements.txt` (nytt beroende om `prometheus-client` används). Tester: tillägg i `tests/test_web_routes.py`.

## Verification
- `pytest tests/test_web_routes.py -q -k metrics`
- `curl http://localhost:8000/metrics` och kontrollera att svaret innehåller minst en rad per statusvärde (t.ex. `flipp_issues_total{status="done"} 3`).

- ID: `01M0CPHHZX3Z3C8M5HMPX8YD0E`
- Type: feature
- Actor: ai:claude-code

---

## [P4][done] [flipp] OPDS-feed i både 1.2 och 2.0

## Context
En OPDS-feed låter en PDF-läsare (t.ex. en surfplatta) upptäcka och hämta nya utgåvor automatiskt i stället för att ägaren manuellt kopierar filer. Feeden behöver exponera publikationer/utgåvor och länka till de befintliga filnedladdningsvägarna i `flipp_dl/web/routes.py`.

## Beslut (Rasmus, 2026-08-19)
Båda formaten ska serveras: OPDS 1.2 (Atom-XML) för brett klientstöd och OPDS 2.0 (JSON). Lägg dem på var sin URL och dela all kataloglogik - bara serialiseringen skiljer, så feeden får aldrig byggas två gånger i koden.


## Acceptance criteria
- [ ] En feed-endpoint finns som listar watchade publikationer som OPDS-navigations-poster.
- [ ] Varje publikation länkar till en acquisition-feed som listar dess nedladdade (status DONE) utgåvor, med länk till den faktiska PDF-filen.
- [ ] Feeden svarar med rätt content-type för vald OPDS-version (`application/atom+xml;profile=opds-catalog` för 1.2, eller `application/opds+json` för 2.0).
- [ ] Feeden är skyddad av samma auth-mekanism som resten av UI:t (eller ett separat token-baserat skydd om klienten inte kan hantera sessionscookies - avgörs vid implementation).
- [ ] Feeden validerar mot vald OPDS-version (manuellt eller med en validator) och går att lägga till som katalog i minst en verklig OPDS-klient.

## Implementation hints
Filer som väntas ändras: `flipp_dl/web/routes.py` (nya endpoints, t.ex. `/opds` och `/opds/{code}`), eventuellt en ny modul `flipp_dl/web/opds.py` för feed-generering, samt `flipp_dl/db/repository.py` om en ny frågemetod behövs för "nedladdade utgåvor per publikation". Tester: ny `tests/test_opds.py`.

## Verification
- `pytest tests/test_opds.py -q`
- `curl -u <basic-auth-eller-cookie> http://localhost:8000/opds` och kontrollera att svaret har rätt content-type och innehåller minst en watchad publikation.

- ID: `01M0CPHHZHHC2TF38SJ0DF4Y7Z`
- Type: feature
- Actor: ai:claude-code

---

## [P4][done] [flipp] Notiser vid nya nedladdningar via ntfy och webhook

## Context
Idag syns nya nedladdningar bara om man öppnar webb-UI:t. Ägaren vill kunna få en push-notis när en ny utgåva laddats ner (eller när nedladdning misslyckats), utan att aktivt behöva kolla dashboarden.

## Beslut (Rasmus, 2026-08-19)
Två kanaler ska stödjas: ntfy som förstaval, plus en generisk webhook som POST:ar JSON till valfri URL. Bygg dem bakom ett gemensamt gränssnitt i notify.py så fler kanaler kan läggas till utan att anropsstället ändras. ntfy-token provisioneras enligt den etablerade rutinen och får aldrig hårdkodas - läses från env eller inställning.


## Acceptance criteria
- [ ] En notifieringsmekanism finns som triggas när en utgåva går från nedladdning till status DONE, och en variant (eller samma mekanism) triggas vid ERROR.
- [ ] Notifieringskanalen/kanalerna är konfigurerbara via `DbSetting`-nycklar (samma mönster som `KOMGA_*` i TASK-1326), inte hårdkodade.
- [ ] Ett fel i notifieringssteget (kanalen nere, felaktig token) får aldrig påverka issue- eller job-status - nedladdningen ska räknas som lyckad även om notisen inte gick fram.
- [ ] Om fler kanaler väljs: varje kanal kan aktiveras/inaktiveras oberoende av de andra.
- [ ] Settings-sidan har ett avsnitt för att konfigurera vald kanal, utan att rendera tillbaka existerande secrets i klartext (samma mönster som beskrivs för Komga: tom sträng + placeholder).

## Implementation hints
Filer som väntas ändras: `flipp_dl/scheduler.py`, `flipp_dl/db/repository.py`, `flipp_dl/db/models.py` (om nya settings-nycklar kräver det), `flipp_dl/web/routes.py`, `flipp_dl/web/templates/settings.html`, samt en ny modul (t.ex. `flipp_dl/notify.py`) för själva notifieringsklienten. Tester: en ny `tests/test_notify.py` och tillägg i `tests/test_scheduler.py`.

## Verification
- `pytest tests/test_notify.py tests/test_scheduler.py -q`
- Manuellt: trigga en nedladdning i en testmiljö och bekräfta att notisen kommer fram på vald kanal, samt att en avstängd/felkonfigurerad kanal inte får jobbet att misslyckas (`repo.get_job(job_id).status == "done"`).

- ID: `01M0CPHHZ89METGXNK2ASHZ2SQ`
- Type: feature
- Actor: ai:claude-code

---

