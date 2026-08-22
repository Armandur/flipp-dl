# flipp-dl

En liten kommandorads-utility för att ladda ner publikationer från
[Flipp](https://tidningar.flipp.se/) (Egmont) som PDF.

> **Disclaimer:** Det här verktyget är tänkt för personligt bruk, för att
> ladda ner publikationer som du redan har laglig tillgång till via ditt
> Flipp-konto. Respektera Egmonts användarvillkor och upphovsrätt – sprid
> inte nedladdat material vidare.

## Funktioner

- Hämtar listan över publikationer som ditt konto har tillgång till.
- Filtrerar på kategori (förvalt: `52` – Serietidningar).
- Laddar ner alla utgåvor för valda publikationer och slår ihop sidorna
  till en PDF per utgåva.
- Hoppar över utgåvor som redan finns på disk.
- Webbgränssnitt med databas, jobbkö och schemalagd pollning.
- Hittar utgåvor som Flipp-appen döljer, via PageSuites utgåvelista.
- Säkerhetskopierar publikations- och utgåvekoderna till JSON, och
  återställer från samma fil. Koderna är det oersättliga - allt annat går
  att hämta igen.
- Skickar utgåvorna vidare till [Komga](https://komga.org/) med metadata
  och omslag, om du vill.
- Notiser via ntfy eller webhook, valbart per publikation.
- OPDS-feed så en läsapp kan hämta direkt från flipp-dl.

## Krav

- Python 3.11 eller senare (CI kör 3.11 och 3.12)
- Beroenden: se `requirements.txt` (`requests`, `pypdf`, SQLAlchemy,
  Alembic, FastAPI, APScheduler med flera)

## Installation

```bash
git clone https://github.com/Armandur/flipp-dl.git
cd flipp-dl
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Skaffa en token

Flipp-API:t använder en token som identifierar ditt konto.

**Enklast, om du kör webbgränssnittet:** fyll i e-post och lösenord under
Inställningar och tryck *Logga in och hämta token*. flipp-dl loggar in mot
Flipp åt dig och sparar tokenen. Lösenordet används för det enda anropet
och sparas aldrig.

Kör du bara CLI:t får du hämta tokenen ur webbläsaren. Den ligger i en
cookie som heter `flipp_token` - klistra in det här i konsolen (F12) på
inloggad sida, så skrivs den ut direkt:

```js
decodeURIComponent(document.cookie).split(";").map(c => c.trim()).find(c => c.startsWith("flipp_token="))?.slice(12)
```

Samma kodsnutt finns att kopiera på Inställningar i webbgränssnittet.

Spara token antingen som miljövariabel:

```bash
export FLIPP_TOKEN="din-token-här"
```

...eller i en fil som heter `token` bredvid `app.py`. Filen är redan
listad i `.gitignore` så den checkas inte in av misstag.

## Användning

```bash
# Standardbeteende: ladda ner allt i kategori 52 (Serietidningar)
python -m flipp_dl

# Specifika kategorier (kan upprepas)
python -m flipp_dl --category 52 --category 7

# En eller flera specifika publikationer via customPublicationCode
python -m flipp_dl --publication KA --publication FAN

# Bara lista vad som finns tillgängligt
python -m flipp_dl --list-categories
python -m flipp_dl --list-publications --category 52

# Kontroll över utdata, parallellism och verbositet
python -m flipp_dl --output ~/flipp --workers 8 -v

# Tvinga om-nedladdning av redan hämtade utgåvor
python -m flipp_dl --no-skip-existing
```

### Katalog, utgåvor och koder

Flipp-API:t listar bara de utgåvor appen visar. PageSuite, som levererar
själva filerna, har hela bakkatalogen - och de koderna går att hämta utan
inloggning. De här kommandona rör inget på disk, bara databasen:

```bash
# Lägg till publikationer ur den medföljande katalogen. Tar även upp
# docs/olistade-publikationer.json om den ligger bredvid: publikationer
# Flipp inte listar men vars kod vi känner till.
python -m flipp_dl --import-catalog docs/alla-publikationer.json

# Fråga PageSuite om varje publikations fulla utgåvelista och spara
# koderna för det vi saknar. Ett anrop per publikation.
python -m flipp_dl --discover-editions

# Importera utgåvekoder som ingen listning returnerar alls. Varje kod
# slås upp för att hitta sin publikation.
python -m flipp_dl --import-editions docs/olistade-utgavor.json

# Säkerhetskopiera alla publikations- och utgåvekoder till JSON, och
# återställ från samma fil. En förlorad databas kan bygga upp sig igen
# från den här - koderna är det enda som inte går att återskapa.
python -m flipp_dl --export-codes koder.json
python -m flipp_dl --import-backup koder.json
```

Samma fem finns i webbgränssnittet under **Inställningar → Katalog och
koder**, där filerna laddas upp respektive ner i webbläsaren.

### Underhåll

```bash
# Stäm av databasen mot filerna på disk utan att ladda ner något: en fil
# som redan finns markerar sin utgåva som hämtad. Allt skanningen inte kan
# förklara rapporteras i stället för att fixas tyst.
python -m flipp_dl --import-existing

# Döp om nedladdade filer till den nuvarande namnkonventionen och
# uppdatera sökvägarna i databasen. Går att köra om.
python -m flipp_dl --migrate-filenames
```

`python app.py` fungerar också som bakåtkompatibel shim.

Nedladdade PDF:er hamnar i `Output/<Publikationens namn>/` som default,
eller i katalogen som anges med `--output`. Namngivningen och de val som
går att göra per publikation beskrivs under
[Var filerna hamnar](#var-filerna-hamnar).

### Schedulerläge

```bash
# Kör som långkörande tjänst (pollar var 6:e timme, laddar ner automatiskt)
python -m flipp_dl --scheduler --db flipp.db --poll-interval 360
```

Markera publkationer som bevakade via webbgränssnittet (se nedan) så
laddas nya utgåvor ner automatiskt.

## Docker (rekommenderat för självhosting)

```bash
cp .env.example .env
# Fyll i FLIPP_TOKEN i .env
docker compose up -d
```

Webbgränssnittet nås på `http://localhost:8000`.

Volymer:
- `./data/` – SQLite-databas (`flipp.db`)
- `./output/` – nedladdade PDF:er

### Miljövariabler

| Variabel            | Default     | Beskrivning                                    |
|---------------------|-------------|------------------------------------------------|
| `FLIPP_TOKEN`       | –           | Din Flipp API-token. Kan också sparas i gränssnittet under Inställningar, vilket har företräde. |
| `FLIPP_DB`          | `flipp.db`  | Sökväg till SQLite-filen.                      |
| `FLIPP_OUTPUT`      | `Output`    | Katalog där PDF:er sparas.                     |
| `FLIPP_TZ`          | `Europe/Stockholm` | Tidszon som tider visas i. Databasen lagrar UTC. |
| `FLIPP_METRICS_PUBLIC` | –      | `1` gör `/metrics` läsbar utan inloggning (för Prometheus). |
| `FLIPP_POLL_INTERVAL` | `360`     | Minuter mellan API-polls (default 6 h).        |
| `FLIPP_WORKERS`     | `4`         | Parallella sidnedladdningar per utgåva.        |
| `FLIPP_SECRET_KEY`  | –           | Hemlighet för sessions (byt i produktion).     |
| `FLIPP_PASSWORD`    | –           | Lösenord för inloggning. Tom = auth inaktiv. Fungerar även som HTTP Basic-lösenord för API och OPDS. |
| `FLIPP_COVER_CACHE` | `flipp-dl-covers` bredvid databasen | Katalog för cachade omslag. Ligger aldrig under utdatakatalogen - biblioteksvyn skannar den efter PDF:er. |

Komga och notiser går att ställa in i gränssnittet, men kan också sättas
med miljövariabler - de vinner då över det som är sparat i databasen:

| Variabel | Beskrivning |
|---|---|
| `KOMGA_ENABLED`, `KOMGA_URL` | Slå på och peka ut Komga. |
| `KOMGA_USERNAME`, `KOMGA_PASSWORD`, `KOMGA_API_KEY` | Autentisering. API-nyckel är att föredra. |
| `KOMGA_LIBRARY_ID` | Biblioteket flipp-dl skriver till. |
| `KOMGA_WAIT_SECONDS` | Hur länge en sync väntar på att Komga indexerat boken (default 10). |
| `NTFY_ENABLED`, `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` | Push-notiser via ntfy. |
| `NOTIFY_WEBHOOK_ENABLED`, `NOTIFY_WEBHOOK_URL` | POSTar `{"title": …, "message": …}` per ny utgåva. |

Notiser är dessutom **opt-in per publikation**: en kanal här betyder bara
att den *kan* skicka. Välj vilka publikationer som ska annonseras under
Notiser på publikationens egen sida.

### Unraid

Installera via **Settings → Docker → Add Container** (eller Community
Applications om du hellre söker på "flipp-dl").

| Fält | Värde |
|---|---|
| **Name** | `flipp-dl` |
| **Repository** | `ghcr.io/armandur/flipp-dl:latest` |
| **Network type** | Bridge |
| **Port** | Host `8000` → Container `8000` (TCP) |
| **Path 1** | Host `/mnt/user/appdata/flipp-dl` → Container `/data` (Read/Write) |
| **Path 2** | Host-katalogen där PDF:erna ska hamna → Container `/output` (Read/Write) |

Miljövariabler att fylla i under **"Add another Path / Port / Variable"**:

| Nyckel | Värde | Obligatorisk |
|---|---|---|
| `FLIPP_TOKEN` | Din Flipp-token (se "Skaffa en token" ovan) | Ja |
| `FLIPP_SECRET_KEY` | Lång slumpmässig sträng | Ja |
| `FLIPP_PASSWORD` | Valfritt lösenord för webbgränssnittet | Nej |
| `FLIPP_TZ` | `Europe/Stockholm` | Nej |
| `FLIPP_POLL_INTERVAL` | `360` | Nej |
| `FLIPP_WORKERS` | `4` | Nej |

> **Behörighetsproblem?** Containern kör som en icke-root-användare.
> Om Unraid klagar på skrivrättigheter, öppna Unraid-terminalen och kör:
> ```bash
> chmod -R 777 /mnt/user/appdata/flipp-dl
> chmod -R 777 /mnt/user/Downloads/Flipp
> ```
> Alternativt: sätt **Extra Parameters** till `--user 99:100` i
> containerinställningarna (99:100 är Unraids inbyggda
> `nobody`/`users`-konto).

Webbgränssnittet nås sedan på `http://<unraid-ip>:8000`.

## Webbgränssnitt

| Sida              | URL              | Beskrivning                           |
|-------------------|------------------|---------------------------------------|
| Dashboard         | `/`              | Stats, senaste nedladdningar och jobb |
| Publikationer     | `/publications`  | Lista, watch/unwatch, destination och notiser per publikation |
| Sök               | `/search`        | Sökning över alla utgåvor             |
| Bibliotek         | `/library`       | Filerna på disk, och avstämning mot databasen |
| Jobb              | `/jobs`          | Jobblogg med statusfärger             |
| Inställningar     | `/settings`      | Token, pollning, Komga, notiser, katalog och koder |
| Healthcheck       | `/healthz`       | `{"status":"ok"}` för Docker          |
| Metrics           | `/metrics`       | Prometheus-format, se `FLIPP_METRICS_PUBLIC` |
| JSON-API          | `/api/…`         | `publications`, `publications/{code}`, `jobs` |
| OPDS              | `/api/opds`, `/api/opds2` | Atom 1.2 respektive JSON 2.0, för PDF-läsare |

Under Inställningar finns även åtgärderna: kör en pollning direkt, skicka en
testnotis, testa Komga-anslutningen, importera katalog och koder, och starta
utgåveupptäckten. De tunga körningarna blir bakgrundsjobb med en statusruta
som uppdaterar sig själv tills de är klara.

### Var filerna hamnar

PDF:erna hamnar i `<utdatarot>/<publikationens namn>/` med filnamn på formen
`<publikation> - <ÅÅÅÅ-MM-DD> - <utgåvenamn>.pdf`. Filnamnen är OS-säkra:
Windows-reserverade namn, avslutande punkter och för långa sökvägar hanteras
utan att unikheten går förlorad.

En publikation kan få ett **eget mappnamn** (två publikationer kan heta
likadant) och en **egen destination**, om du satt en sekundär utdatarot under
Inställningar. Det senare är till för publikationer som inte hör hemma med de
övriga - en tidning som passar bättre i ett e-boksbibliotek än i ett
seriebibliotek. Att byta destination flyttar inte befintliga filer, så bytet
vägras för en publikation som redan har nedladdade utgåvor.

### Översättningar

UI-texten går via gettext/Babel: engelska är alltid källtexten (`msgid`)
och fallback-språket, svenska är en `.po`-katalog under
`flipp_dl/web/locales/sv/LC_MESSAGES/`. Språket väljs via `/language/en`
respektive `/language/sv` (växlaren i toppnavigeringen), sparas i
sessionen, och slår igenom på `<html lang="…">` och all översatt text.
Saknas en sträng i den svenska katalogen visas den engelska originaltexten
i stället för en tom sträng eller en `msgid`.

Kompilerade `.mo`-filer är incheckade och används direkt i produktion -
`pybabel` (från `Babel`-paketet i `requirements.txt`) behövs bara när du
ändrar UI-text:

```bash
# 1. Extrahera alla _("...")-strängar ur templates till en .pot-mall
pybabel extract -F flipp_dl/web/locales/babel.cfg \
  -o flipp_dl/web/locales/messages.pot \
  flipp_dl/web/templates flipp_dl/web/routes.py flipp_dl/web/codes_routes.py

# 2. Slå ihop nya/ändrade strängar in i den befintliga svenska katalogen
#    (behåller redan gjorda översättningar; nya strängar får tom msgstr)
pybabel update -i flipp_dl/web/locales/messages.pot \
  -d flipp_dl/web/locales -l sv

# 3. Redigera flipp_dl/web/locales/sv/LC_MESSAGES/messages.po för hand,
#    fyll i msgstr för det som är nytt eller ändrat

# 4. Kompilera .po till .mo - detta är filen som faktiskt läses i drift
pybabel compile -d flipp_dl/web/locales -l sv
```

Glöm inte steg 4 - `.po`-ändringar utan omkompilering syns aldrig i UI:t.

## Utveckling

```bash
pip install -r requirements-dev.txt
ruff check .
black --check .
pytest
```

CI kör samma kommandon på Python 3.11 och 3.12 via GitHub Actions.

## Medföljande datafiler

`docs/` innehåller det som gör bakkatalogen åtkomlig:

| Fil | Innehåll |
|---|---|
| `alla-publikationer.json` | Hela publikationslistan Flipp levererar. |
| `olistade-publikationer.json` | Publikationer Flipp inte listar men vars kod är känd. Importeras som avlistade så de inte ser ut att ingå i katalogen. |
| `olistade-utgavor.json` | Utgåvekoder som ingen listning returnerar alls, hittade via web.archive.org. |

## Roadmap

Öppna punkter spåras i backlog-verktyget och speglas till
[`backlog.md`](backlog.md).

## Licens

MIT - se [`LICENSE`](LICENSE).
