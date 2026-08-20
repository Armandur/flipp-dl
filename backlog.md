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

## [P2][doing] [flipp] Kör om felade nedladdningar automatiskt med backoff

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

## [P3][todo] [flipp] Se över hur knapparna i Action-kolumnen ser ut och radbryter

Action-kolumnen i utgåvelistan har vuxit under arbetet: Preview, Open, Re-download, Delete, Retry, Download och Cancel, där flera kan visas samtidigt beroende på status. De ärver olika knappklasser (btn-watch, btn-unwatch, btn-primary-soft) som valts en i taget, och radbrytningen är inte genomtänkt - på en skärmdump vid 1280px hamnar Delete på egen rad under Open och Re-download.

Se över helheten: vilka knappar som ska synas samtidigt, vilken som är den primära handlingen per status, färgsättningen, och hur de radbryter i mobilbredd. En knapp som raderar en fil bör inte se ut som den som öppnar den.

Filer som väntas ändras: flipp_dl/web/templates/issue_row.html, flipp_dl/web/templates/base.html (knappklasserna), eventuellt flipp_dl/web/templates/publication_detail.html.

Verifiering: skärmdumpar vid 390px och 1280px för varje status en rad kan ha (inte nedladdad, köad, laddar ner, nedladdad, fel), på både engelska och svenska eftersom knapptexterna är översatta och byter längd.

- ID: `01M0FGJZJDZD60HGGDDXW1NHE7`
- Type: improvement
- Actor: ai:claude-opus-5

---

## [P3][todo] [flipp] Utgåveomslagen saknas för allt som upptäcktes före omslagscachen

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

## [P3][todo] [flipp] Sökning över alla utgåvor, inte bara inom en publikation

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

## [P3][todo] [flipp] Slå på Komga-integrationen i flipp-dl

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

## [P3][todo] [flipp] Montera flipp-dl:s output-katalog som bibliotek i Komga

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

