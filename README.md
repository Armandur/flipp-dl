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
python app.py
```

Nedladdade PDF:er hamnar i `Output/<Publikationens namn>/`.

För närvarande är kategori-ID hårdkodat till `52` (Serietidningar). Vill
du ladda ner en annan kategori eller specifik publikation får du tills
vidare ändra i slutet av `app.py`. En riktig CLI med argument är på
[TODO-listan](TODO.md).

## Roadmap

Se [`TODO.md`](TODO.md) för en prioriterad lista över planerade
förbättringar. På sikt är målet att göra `flipp-dl` till en självhostad
tjänst med webbgränssnitt, databas och schemalagd nedladdning, paketerad
som en Docker-container.

## Licens

Ingen licens är satt ännu – tills vidare gäller "all rights reserved"
enligt tysta defaulten. Kommer att formaliseras längre fram.
