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

## Krav

- Python 3.9 eller senare
- Beroenden: `requests`, `pypdf` (se `requirements.txt`)

## Installation

```bash
git clone https://github.com/Armandur/flipp-dl.git
cd flipp-dl
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Skaffa en token

Flipp-API:t använder en token som identifierar ditt konto. Så här får du
tag i din egen:

1. Logga in på <https://tidningar.flipp.se/> i en webbläsare.
2. Öppna utvecklarverktygen (F12) och gå till fliken **Network**.
3. Ladda om sidan och leta efter anropet `refreshsignintoken`.
4. Kopiera värdet på fältet `token` från request-payloaden (eller från
   motsvarande cookie / localStorage-nyckel).

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

`python app.py` fungerar också som bakåtkompatibel shim.

Nedladdade PDF:er hamnar i `Output/<Publikationens namn>/` som default,
eller i katalogen som anges med `--output`.

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
| `FLIPP_TOKEN`       | –           | **Krävs.** Din Flipp API-token.                |
| `FLIPP_DB`          | `flipp.db`  | Sökväg till SQLite-filen.                      |
| `FLIPP_OUTPUT`      | `Output`    | Katalog där PDF:er sparas.                     |
| `FLIPP_TZ`          | `Europe/Stockholm` | Tidszon som tider visas i. Databasen lagrar UTC. |
| `FLIPP_POLL_INTERVAL` | `360`     | Minuter mellan API-polls (default 6 h).        |
| `FLIPP_WORKERS`     | `4`         | Parallella sidnedladdningar per utgåva.        |
| `FLIPP_SECRET_KEY`  | –           | Hemlighet för sessions (byt i produktion).     |
| `FLIPP_PASSWORD`    | –           | Lösenord för inloggning. Tom = auth inaktiv.   |

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
| **Path 2** | Host `/mnt/user/Downloads/Flipp` → Container `/output` (Read/Write) |

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
| Publikationer     | `/publications`  | Lista, watch/unwatch per publikation  |
| Jobb              | `/jobs`          | Jobblogg med statusfärger             |
| Inställningar     | `/settings`      | Poll-intervall, workers               |
| Healthcheck       | `/healthz`       | `{"status":"ok"}` för Docker          |

## Utveckling

```bash
pip install -r requirements-dev.txt
ruff check .
black --check .
pytest
```

CI kör samma kommandon på Python 3.11 och 3.12 via GitHub Actions.

## Roadmap

Öppna punkter spåras i backlog-verktyget och speglas till
[`backlog.md`](backlog.md). På sikt är målet att göra `flipp-dl` till en
självhostad tjänst med webbgränssnitt, databas och schemalagd nedladdning,
paketerad som en Docker-container.

## Licens

MIT - se [`LICENSE`](LICENSE).
