# De Ochtendbrief ☀️

Elke werkdag om 07:00 automatisch een visueel marktoverzicht: indices, crypto,
valuta/rente, nieuws, macro-agenda, een verwachtingenrubriek en een reflectie
op je eigen portfolio — gepubliceerd als webpagina. **Volledig gratis**: de
tekst wordt geschreven door een open-source AI-model (Llama 3.2) dat gratis
binnen GitHub Actions zelf draait — geen API-sleutel, geen rekening.

Als het model een keer geen geldige uitvoer geeft, valt het script automatisch
terug op een cijfermatige, regelgebaseerde samenvatting, zodat je brief nooit
leeg of kapot is.

## Zo zet je hem live (±10 minuten, eenmalig)

1. **Maak een GitHub-repository** (mag privé) en upload alle bestanden uit deze map
   (`generate.py`, `config.yaml`, `template.html`, `requirements.txt`, `README.md`
   en de map `.github`).
2. **GitHub Pages aanzetten:** ga in de repo naar *Settings → Pages* en kies bij
   **Source**: **GitHub Actions** (níet "Deploy from a branch"). Meer is het niet.
3. **Testen:** tabblad *Actions → Ochtendbrief genereren → Run workflow*.
   Deze run duurt de eerste keer ~3-5 minuten (het model wordt gedownload);
   daarna staat je brief op `https://<gebruikersnaam>.github.io/<repo-naam>/`.

Daarna draait hij vanzelf elke werkdag om 07:00 (NL-zomertijd). Zet die URL als
bladwijzer op je telefoon. Geen API-sleutel of secret nodig.

## Aanpassen

- **`config.yaml`** — hier staan je indices, crypto, nieuwsfeeds en portfolio.
  Portfolio-wijzigingen worden automatisch meegenomen in de reflectie.
- **`template.html`** — het uiterlijk (kleuren, lettertypes, secties).
- **`.github/workflows/daily.yml`** — het tijdstip (cron staat in UTC;
  wintertijd = 07:00 NL is `0 6 * * 1-5`).


## Elke ochtend in je mailbox (optioneel, gratis)

De workflow mailt de brief automatisch als je drie secrets instelt onder
*Settings → Secrets and variables → Actions → New repository secret*:

| Secret | Waarde |
|---|---|
| `MAIL_USERNAME` | je Gmail-adres, bijv. `jij@gmail.com` |
| `MAIL_PASSWORD` | een **app-wachtwoord** (niet je gewone wachtwoord) |
| `MAIL_TO` | het adres waar de brief heen moet |

Een app-wachtwoord maak je aan op `myaccount.google.com/apppasswords`
(vereist tweestapsverificatie). Gebruik je een andere provider dan Gmail, pas dan
`server_address` en `server_port` aan in `.github/workflows/daily.yml`.

Stel je deze secrets niet in, dan wordt de mailstap simpelweg overgeslagen en
verschijnt de brief alleen op de webpagina.

## Zwaarder AI-model kiezen

Standaard draait `qwen2.5:7b` — beter in lange Nederlandse teksten dan het
kleinere llama3.2. Wil je een ander model, zet dan onder *Settings → Secrets and
variables → Actions → Variables* een variabele `OLLAMA_MODEL` met bijvoorbeeld
`llama3.1:8b` (zwaarder, trager) of `llama3.2:3b` (lichter, sneller).

## Lokaal testen

```bash
pip install -r requirements.txt
python generate.py --demo         # voorbeelddata, geen internet nodig

# Voor een lokale test met echte data + het gratis AI-model:
#   1. installeer Ollama: https://ollama.com/download
#   2. ollama pull llama3.2:3b
#   3. ollama serve
#   4. python generate.py

open docs/index.html
```

## Kosten

**€ 0,-.** GitHub Actions en Pages zijn gratis voor dit gebruik (je verbruikt
slechts enkele minuten per dag van de 2.000 gratis maandminuten). Het
AI-model (Llama 3.2, open-source) draait gratis binnen diezelfde Actions-runner
— er is geen API-sleutel of extern account meer nodig.

De schrijfkwaliteit is iets eenvoudiger dan een groot commercieel model, en de
eerste run per dag duurt iets langer (het model wordt telkens opnieuw
gedownload, ~2 GB, binnen de gratis Actions-runner). Wil je later toch naar
een krachtiger, betaald model overstappen, dan kan dat weer met een paar
regels aanpassing.

*De inhoud is automatisch gegenereerd en is geen beleggingsadvies.*
