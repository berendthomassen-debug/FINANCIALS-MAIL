#!/usr/bin/env python3
"""Ochtendbrief — uitgebreid dagelijks financieel dashboard.

Gebruik:
    python generate.py            # live data + AI (vereist ANTHROPIC_API_KEY)
    python generate.py --demo     # voorbeelddata, geen netwerk/AI nodig
"""

import argparse, datetime as dt, html, json, os, sys
import yaml

BASE = os.path.dirname(__file__)
CONFIG_PATH = os.path.join(BASE, "config.yaml")
OUTPUT_PATH = os.path.join(BASE, "docs", "index.html")


# ---------------------------------------------------------------- data ophalen

def fetch_quotes(instruments, period="1mo"):
    import yfinance as yf
    rows = []
    for ins in instruments:
        try:
            hist = yf.Ticker(ins["symbol"]).history(period=period, interval="1d")
            closes = [round(float(c), 4) for c in hist["Close"].dropna()]
            if len(closes) < 2:
                continue
            last, prev = closes[-1], closes[-2]
            week_ago = closes[-6] if len(closes) >= 6 else closes[0]
            rows.append({
                "name": ins["name"], "last": last,
                "change_pct": (last - prev) / prev * 100,
                "week_pct": (last - week_ago) / week_ago * 100,
                "spark": closes, "suffix": ins.get("suffix", ""),
            })
        except Exception as e:
            print(f"  ! {ins['symbol']}: {e}", file=sys.stderr)
    return rows


def fetch_crypto(coins):
    """Koersen primair van Bitvavo (realtime beursdata, EUR); CoinGecko als
    reserve en voor sparkline/7d/30d/marktkapitalisatie."""
    import requests

    # -- Bitvavo: realtime laatste prijs + 24u-verandering per handelspaar
    bitvavo = {}
    try:
        r = requests.get("https://api.bitvavo.com/v2/ticker/24h", timeout=15)
        r.raise_for_status()
        bitvavo = {t["market"]: t for t in r.json() if isinstance(t, dict)}
    except Exception as e:
        print(f"  ! Bitvavo niet bereikbaar ({e}) — CoinGecko wordt gebruikt", file=sys.stderr)

    # -- CoinGecko: aanvullende data
    ids = ",".join(c["id"] for c in coins)
    gecko = {}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "eur", "ids": ids, "sparkline": "true",
                    "price_change_percentage": "24h,7d,30d"},
            timeout=20)
        gecko = {c["id"]: c for c in r.json()}
    except Exception as e:
        print(f"  ! CoinGecko niet bereikbaar: {e}", file=sys.stderr)

    rows = []
    for c in coins:
        d = gecko.get(c["id"], {})
        b = bitvavo.get(c.get("market", ""))
        last = change = None
        if b and b.get("last"):
            last = float(b["last"])
            openp = float(b["open"]) if b.get("open") else None
            if openp:
                change = (last - openp) / openp * 100
        if last is None:
            last = d.get("current_price")
        if change is None:
            change = d.get("price_change_percentage_24h_in_currency") or 0
        if last is None:
            print(f"  ! geen koers voor {c['ticker']}", file=sys.stderr)
            continue
        spark = d.get("sparkline_in_7d", {}).get("price", [])
        rows.append({
            "id": c["id"], "ticker": c["ticker"], "market": c.get("market", ""),
            "name": d.get("name", c["ticker"]),
            "aantal": c["aantal"], "last": last, "change_pct": change,
            "week_pct": d.get("price_change_percentage_7d_in_currency") or 0,
            "maand_pct": d.get("price_change_percentage_30d_in_currency") or 0,
            "marketcap": d.get("market_cap") or 0,
            "volume": d.get("total_volume") or 0,
            "spark": spark[::8] or [last] * 2,
        })
    return rows


def fetch_news(feeds, per_feed=5):
    import feedparser
    items = []
    for f in feeds:
        parsed = feedparser.parse(f["url"])
        for e in parsed.entries[:per_feed]:
            items.append({"title": e.get("title", ""), "link": e.get("link", ""),
                          "source": f["source"]})
    for i, n in enumerate(items, 1):
        n["ref"] = i
    return items[:20]


def fetch_fear_greed():
    """Crypto Fear & Greed-index via alternative.me (gratis, geen sleutel)."""
    import requests
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=15)
        d = r.json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception as e:
        print(f"  ! Fear & Greed niet beschikbaar: {e}", file=sys.stderr)
        return None


# Piecewise-benadering van het NY Fed-model: rentecurvespread (10j − 3m, in
# procentpunt) → geschatte recessiekans binnen 12 maanden.
_SPREAD_TABEL = [(1.21, 5), (0.76, 10), (0.46, 15), (0.22, 20), (0.02, 25),
                 (-0.17, 30), (-0.50, 40), (-0.82, 50), (-1.13, 60), (-1.46, 70)]


def recession_from_spread(spread):
    t = _SPREAD_TABEL
    if spread >= t[0][0]:
        return t[0][1]
    if spread <= t[-1][0]:
        return t[-1][1]
    for (s1, p1), (s2, p2) in zip(t, t[1:]):
        if s2 <= spread <= s1:
            return round(p1 + (p2 - p1) * (s1 - spread) / (s1 - s2))
    return 25


def fetch_recession():
    """Recessie-indicator uit de rentecurve (10-jaars minus 3-maands US-rente)."""
    import yfinance as yf
    try:
        y10 = float(yf.Ticker("^TNX").history(period="5d")["Close"].dropna().iloc[-1])
        y3m = float(yf.Ticker("^IRX").history(period="5d")["Close"].dropna().iloc[-1])
        spread = y10 - y3m
        return {"prob": recession_from_spread(spread), "spread": round(spread, 2),
                "y10": round(y10, 2), "y3m": round(y3m, 2)}
    except Exception as e:
        print(f"  ! Recessie-indicator niet beschikbaar: {e}", file=sys.stderr)
        return None


# ------------------------------------------------------------------ AI-analyse

AI_PROMPT = """Je bent hoofdredacteur van een diepgravend Nederlands financieel ochtendrapport.
Vandaag is {date}. Actuele data (JSON):

INDICES: {markets}
CRYPTO (dit is tevens de portfolio van de lezer, met aantallen): {crypto}
VALUTA/RENTE: {fx}
GRONDSTOFFEN: {commodities}
MARKTTHERMOMETER: {thermo}
GENUMMERDE NIEUWSKOPPEN: {news}

SCHRIJFOPDRACHT — dit is een LANG rapport, geen samenvatting. Schrijf uitvoerig,
analytisch en toegankelijk Nederlands: leg elk begrip kort uit alsof de lezer
geinteresseerd maar geen econoom is. Bouw redeneringen op in meerdere stappen
(oorzaak, mechanisme, gevolg, wat het voor de lezer betekent). Vermijd holle
frases; elke alinea moet een concreet inzicht bevatten dat verder gaat dan het
herhalen van de cijfers. Verwijs in lopende tekst naar nieuwskoppen met
bronverwijzingen in de vorm [n]. Scheid alinea's met een enkele newline.

Antwoord UITSLUITEND met JSON (geen markdown), exact dit schema:
{{
  "kernpunten": ["5-7 bondige kernpunten van vandaag, elk 1 zin, het allerbelangrijkste eerst"],
  "hoofdverhaal": "3-4 alinea's over HET belangrijkste marktverhaal van vandaag: wat er speelt, waarom het nu gebeurt en wat de directe gevolgen zijn, met [n]-verwijzingen",
  "samenvatting": "8-10 alinea's zeer uitgebreid marktoverzicht van vanochtend, met [n]-verwijzingen",
  "macro_uitleg": "10-12 alinea's diepgaande uitleg van de macrofactoren die de markt nu sturen. Behandel apart en uitvoerig: (1) beleidsrente en centralebankbeleid, (2) de rentecurve en wat die zegt over recessierisico, (3) inflatie en de componenten daarvan, (4) economische groei en arbeidsmarkt, (5) liquiditeit en kredietvoorwaarden, (6) geopolitiek en handelsbeleid, (7) marktsentiment en positionering. Leg per factor uit WAT er speelt, WAAROM het koersen beweegt via welk mechanisme, en hoe het doorwerkt op aandelen, crypto en de portefeuille van de lezer. Betrek de Fear & Greed-stand en de recessie-indicator uit MARKTTHERMOMETER expliciet.",
  "opmerkelijk": [{{"titel": "korte kop", "tekst": "4-6 zinnen over een opvallende beweging, afwijking, divergentie of trendbreuk in de data van vandaag, met uitleg van mogelijke oorzaken"}}],
  "analyse": [{{"categorie": "Aandelen|Crypto|Rente & valuta|Grondstoffen", "tekst": "6-8 alinea's zeer diepgaande analyse van deze categorie: huidige stand, onderliggende drijfveren, sectorrotatie of marktbreedte, technisch beeld, wat de komende dagen bepalend is, en de betekenis voor de lezer. Met [n]-verwijzingen"}}],
  "macro_agenda": [{{"datum": "wo 22 jul", "gebeurtenis": "...", "impact": "welke assets dit raakt en waarom", "belang": "hoog|middel|laag"}}],
  "scenarios": [{{"naam": "Bull|Basis|Bear", "kans": "bijv. 25%", "tekst": "4-6 zinnen: welke triggers dit scenario in gang zetten, hoe het zich ontvouwt en wat het concreet betekent voor de portefeuille"}}],
  "verwachtingen": [{{"onderwerp": "...", "horizon": "vandaag|deze week|deze maand", "verwachting": "3-4 zinnen met onderbouwing", "vertrouwen": "hoog|middel|laag"}}],
  "risico_radar": [{{"risico": "...", "kans": "hoog|middel|laag", "impact": "hoog|middel|laag", "toelichting": "2-3 zinnen"}}],
  "portfolio_reflectie": "6-8 alinea's over de totale portefeuille. Behandel: samenstelling en gewichten, welke posities het verschil maakten, correlatie tussen de posities, concentratierisico (de portefeuille is 100% crypto, zonder BTC of ETH), gevoeligheid voor de macro-agenda, en welke momenten de komende weken bepalend zijn. Geen koop- of verkoopadvies, alleen duiding.",
  "per_coin": [{{"ticker": "AVAX", "tekst": "5-7 zinnen duiding voor deze positie: koersbeeld, eigen katalysatoren, rol binnen de portefeuille en waar op te letten, met [n]-verwijzing waar relevant"}}],
  "speculatie": "6-8 alinea's expliciet SPECULATIEVE beredenering. Denk hardop door over hoe de komende weken en maanden zich zouden kunnen ontvouwen: welke ketens van gebeurtenissen zijn denkbaar, welke minder voor de hand liggende uitkomsten worden onderschat, welke signalen zouden bevestigen of ontkrachten dat een scenario zich voltrekt. Wees expliciet over de onzekerheid en over welke aannames de redenering dragen. Dit is nadrukkelijk geen voorspelling en geen advies."
}}
Macro_agenda: minimaal 6 gebeurtenissen in de komende ~2 weken.
Verwachtingen: minimaal 6 onderwerpen. Risico_radar: minimaal 5 risico's.
Opmerkelijk: minimaal 4 observaties, uitsluitend gebaseerd op de meegegeven data.
Analyse: alle vier de categorieen verplicht. Per_coin: voor ELKE munt in CRYPTO."""


OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


def query_ollama(prompt):
    """Vraagt het lokale, gratis Ollama-model om JSON. Geeft None terug bij falen
    (netwerk-/parsefout) zodat de aanroeper op het regelgebaseerde vangnet terugvalt."""
    import requests
    try:
        r = requests.post(f"{OLLAMA_HOST}/api/chat", timeout=2400, json={
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json",
            "stream": False,
            "options": {"temperature": 0.4, "num_predict": 16000},
        })
        r.raise_for_status()
        text = r.json()["message"]["content"]
        text = text.strip().removeprefix("```json").removesuffix("```").strip()
        return json.loads(text)
    except Exception as e:
        print(f"  ! Ollama gaf geen bruikbaar resultaat ({e}) — vangnet wordt gebruikt", file=sys.stderr)
        return None


def rule_based_sections(markets, crypto, fx, commodities, news, date_str, fng=None, recession=None):
    """100% deterministieke tekst zonder AI — nooit falend vangnet."""
    def kop(rows):
        if not rows:
            return None, None
        best = max(rows, key=lambda r: r["change_pct"])
        worst = min(rows, key=lambda r: r["change_pct"])
        return best, worst

    b_m, w_m = kop(markets)
    b_c, w_c = kop(crypto)
    totaal = sum(c["aantal"] * c["last"] for c in crypto)

    samenvatting = []
    if b_m:
        samenvatting.append(
            f"Op de aandelenmarkten is {b_m['name']} vandaag de sterkste stijger "
            f"({b_m['change_pct']:+.2f}%), terwijl {w_m['name']} de rij sluit "
            f"({w_m['change_pct']:+.2f}%).")
    if b_c:
        samenvatting.append(
            f"In crypto beweegt {b_c['ticker']} het sterkst ({b_c['change_pct']:+.2f}% "
            f"in 24 uur), tegenover {w_c['ticker']} met {w_c['change_pct']:+.2f}%.")
    if news:
        kopjes = "; ".join(f"[{n['ref']}] {n['title']}" for n in news[:5])
        samenvatting.append(f"Belangrijkste nieuwskoppen van vanochtend: {kopjes}.")
    samenvatting.append(
        "Deze samenvatting is automatisch en cijfermatig opgesteld (geen AI-model "
        "beschikbaar bij het genereren); lees de nieuwskoppen hieronder voor context.")

    analyse = []
    for cat, rows in (("Aandelen", markets), ("Crypto", crypto), ("Rente & valuta", fx),
                       ("Grondstoffen", commodities)):
        if not rows:
            continue
        gem = sum(r["change_pct"] for r in rows) / len(rows)
        analyse.append({"categorie": cat,
            "tekst": f"Gemiddelde 24-uursbeweging in deze categorie: {gem:+.2f}%. "
                     f"Sterkste: {max(rows, key=lambda r: r['change_pct'])['name']}, "
                     f"zwakste: {min(rows, key=lambda r: r['change_pct'])['name']}."})

    per_coin = []
    for c in crypto:
        aandeel = (c["aantal"] * c["last"] / totaal * 100) if totaal else 0
        per_coin.append({"ticker": c["ticker"],
            "tekst": f"24 uur: {c['change_pct']:+.2f}%, 7 dagen: {c['week_pct']:+.2f}%. "
                     f"Dit is {aandeel:.1f}% van de totale portefeuillewaarde."})

    macro_delen = []
    if recession:
        macro_delen.append(
            f"De Amerikaanse rentecurve (10-jaars {recession['y10']}% minus 3-maands "
            f"{recession['y3m']}%) staat op {recession['spread']:+.2f} procentpunt. "
            f"Op basis van het NY Fed-model komt dat overeen met een geschatte "
            f"recessiekans van circa {recession['prob']}% binnen twaalf maanden. "
            "Een vlakke of omgekeerde curve (kortlopende rente hoger dan langlopende) "
            "is historisch een van de betrouwbaarste recessiesignalen.")
    if fng:
        macro_delen.append(
            f"De Fear & Greed-index staat op {fng['value']} ({fng['label']}). Deze "
            "index bundelt volatiliteit, momentum en marktbreedte tot één "
            "sentimentscijfer: extreme hebzucht duidt vaak op oververhitting, "
            "extreme angst juist op capitulatie.")
    macro_delen.append(
        "Voor een uitgebreidere macro-duiding is een AI-model nodig; deze tekst is "
        "cijfermatig samengesteld.")

    opmerkelijk = []
    alle = [(r["name"], r["change_pct"]) for r in markets + fx + commodities] + \
           [(c["ticker"], c["change_pct"]) for c in crypto]
    for naam, ch in sorted(alle, key=lambda x: -abs(x[1]))[:3]:
        opmerkelijk.append({"titel": f"{naam}: {ch:+.2f}%",
            "tekst": f"{naam} laat met {ch:+.2f}% de grootste 24-uursbeweging van "
                     "alle gevolgde instrumenten zien."})

    kernpunten = []
    if b_c:
        kernpunten.append(f"{b_c['ticker']} is met {b_c['change_pct']:+.2f}% de sterkste "
                          f"positie in je portefeuille vandaag.")
        kernpunten.append(f"{w_c['ticker']} blijft achter met {w_c['change_pct']:+.2f}%.")
    kernpunten.append(f"Totale portefeuillewaarde: € {fmt(totaal)}.")
    if b_m:
        kernpunten.append(f"Aandelen: {b_m['name']} {b_m['change_pct']:+.2f}%, "
                          f"{w_m['name']} {w_m['change_pct']:+.2f}%.")
    if fng:
        kernpunten.append(f"Fear & Greed-index staat op {fng['value']} ({fng['label']}).")
    if recession:
        kernpunten.append(f"Recessie-indicator: circa {recession['prob']}% kans binnen 12 maanden.")
    for n in news[:2]:
        kernpunten.append(f"Nieuws: {n['title']}")

    return {
        "kernpunten": kernpunten,
        "hoofdverhaal": "\n".join(samenvatting[:2]) or "Geen hoofdverhaal beschikbaar.",
        "speculatie": "Voor een speculatieve vooruitblik is een AI-model nodig; dit onderdeel "
            "is bij het genereren niet beschikbaar geweest. De cijfermatige secties hierboven "
            "zijn wel volledig.",
        "samenvatting": "\n".join(samenvatting),
        "macro_uitleg": "\n".join(macro_delen),
        "opmerkelijk": opmerkelijk,
        "analyse": analyse,
        "macro_agenda": [{"datum": "—", "gebeurtenis":
            "Geen automatische agenda beschikbaar zonder AI-model.",
            "impact": "Raadpleeg een economische kalender, bijv. investing.com/economic-calendar",
            "belang": "middel"}],
        "scenarios": [],
        "verwachtingen": [],
        "risico_radar": [{"risico": "100% concentratie in crypto", "kans": "hoog",
            "impact": "hoog", "toelichting": "Geen spreiding over andere beleggingscategorieën."}],
        "portfolio_reflectie":
            f"Je portefeuille is nu € {fmt(totaal)} waard, verdeeld over "
            f"{', '.join(c['ticker'] for c in crypto)}. Dit overzicht is cijfermatig "
            "gegenereerd; voor duiding, zie de nieuwskoppen en analyse hierboven.",
        "per_coin": per_coin,
    }


def ai_sections(markets, crypto, fx, commodities, news, date_str, fng=None, recession=None):
    thermo = {"fear_greed": fng, "recessie_indicator": recession}
    prompt = AI_PROMPT.format(
        date=date_str,
        markets=json.dumps(markets, ensure_ascii=False, default=str),
        crypto=json.dumps([{k: c[k] for k in ("ticker", "name", "aantal", "last",
                            "change_pct", "week_pct", "maand_pct")} for c in crypto],
                          ensure_ascii=False),
        fx=json.dumps(markets and fx, ensure_ascii=False, default=str),
        commodities=json.dumps(commodities, ensure_ascii=False, default=str),
        thermo=json.dumps(thermo, ensure_ascii=False),
        news=json.dumps([f"[{n['ref']}] ({n['source']}) {n['title']}" for n in news],
                        ensure_ascii=False))

    result = query_ollama(prompt)
    fallback = rule_based_sections(markets, crypto, fx, commodities, news, date_str,
                                   fng=fng, recession=recession)
    if result is None:
        print("  → volledig regelgebaseerd vangnet gebruikt", file=sys.stderr)
        return fallback

    # Vul ontbrekende of ongeldige velden aan met het vangnet, zodat een klein
    # model dat niet perfect het schema volgt de pagina nooit laat breken.
    for key, default in fallback.items():
        if key not in result or not result[key]:
            result[key] = default
    return result


# ------------------------------------------------------------------- rendering

def sparkline(values, up, w=110, h=30):
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    pts = " ".join(f"{i*w/(len(values)-1):.1f},{h-3-(v-lo)/rng*(h-6):.1f}"
                   for i, v in enumerate(values))
    color = "var(--up)" if up else "var(--down)"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="2" stroke-linejoin="round"/></svg>')


def gauge(value, zones, sublabel):
    """Halfronde meter (0-100) met gekleurde zones en naald, als inline SVG."""
    import math
    w, h, cx, cy, r = 240, 140, 120, 122, 92
    def polar(pct):
        a = math.pi * (1 - pct / 100)          # 180° → 0°
        return cx + r * math.cos(a), cy - r * math.sin(a)
    arcs = []
    for lo, hi, kleur in zones:
        x1, y1 = polar(lo)
        x2, y2 = polar(hi)
        groot = 1 if (hi - lo) > 50 else 0
        arcs.append(f'<path d="M {x1:.1f} {y1:.1f} A {r} {r} 0 {groot} 1 '
                    f'{x2:.1f} {y2:.1f}" stroke="{kleur}" stroke-width="16" '
                    f'fill="none" stroke-linecap="butt"/>')
    nx, ny = polar(max(0, min(100, value)))
    return f'''<svg viewBox="0 0 {w} {h}" class="gauge">
      {''.join(arcs)}
      <line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}" stroke="#141F2D" stroke-width="3.5" stroke-linecap="round"/>
      <circle cx="{cx}" cy="{cy}" r="6" fill="#141F2D"/>
      <text x="{cx}" y="{cy - 26}" text-anchor="middle" class="gauge-waarde">{value:.0f}</text>
      <text x="{cx}" y="{cy + 14}" text-anchor="middle" class="gauge-sub">{html.escape(sublabel)}</text>
    </svg>'''


FNG_ZONES = [(0, 25, "#C24141"), (25, 45, "#D98A3D"), (45, 55, "#9AA7B4"),
             (55, 75, "#6FA982"), (75, 100, "#157A4A")]
REC_ZONES = [(0, 20, "#157A4A"), (20, 40, "#6FA982"), (40, 60, "#D98A3D"),
             (60, 100, "#C24141")]


def fmt(v, dec=2):
    return f"{v:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct_cell(p):
    up = p >= 0
    return f'<span class="delta {"up" if up else "down"}">{"▲" if up else "▼"} {abs(p):.2f}%</span>'


def refs_to_links(text, news):
    """Zet [n] om naar klikbare bronverwijzingen."""
    import re
    by_ref = {str(n["ref"]): n for n in news}
    def sub(m):
        n = by_ref.get(m.group(1))
        if not n:
            return ""
        return (f'<a class="ref" href="{html.escape(n["link"])}" '
                f'title="{html.escape(n["source"])}: {html.escape(n["title"])}">[{m.group(1)}]</a>')
    return re.sub(r"\[(\d+)\]", sub, text)


def paras(text, news):
    return "".join(f"<p>{refs_to_links(html.escape(p), news)}</p>"
                   for p in text.split("\n") if p.strip())


def quote_rows(rows):
    out = []
    for r in rows:
        up = r["change_pct"] >= 0
        out.append(f"""<tr><td class="naam">{html.escape(r['name'])}</td>
          <td class="koers">{fmt(r['last'])}{r.get('suffix','')}</td>
          <td class="delta-cel">{pct_cell(r['change_pct'])}</td>
          <td class="delta-cel dim">{pct_cell(r['week_pct'])}</td>
          <td class="grafiek">{sparkline(r['spark'], up)}</td></tr>""")
    return "".join(out)


def render(cfg, markets, crypto, fx, commodities, news, ai, date_str, fng=None, recession=None):
    badge = {"hoog": "b-hoog", "middel": "b-middel", "laag": "b-laag"}
    coin_teksten = {c["ticker"]: c["tekst"] for c in ai.get("per_coin", [])}

    # -- marktthermometer
    if fng:
        fng_html = gauge(fng["value"], FNG_ZONES, fng["label"])
        fng_voet = "0 = extreme angst · 100 = extreme hebzucht (bron: alternative.me)"
    else:
        fng_html, fng_voet = '<p class="dim">Vandaag niet beschikbaar.</p>', ""
    if recession:
        rec_html = gauge(recession["prob"], REC_ZONES, f"spread {recession['spread']:+.2f} pp")
        rec_voet = (f"Geschatte kans op een VS-recessie binnen 12 maanden, afgeleid uit de "
                    f"rentecurve (10-jaars {recession['y10']}% − 3-maands {recession['y3m']}%), "
                    "naar het model van de New York Fed. Een modelmatige indicatie, geen voorspelling.")
    else:
        rec_html, rec_voet = '<p class="dim">Vandaag niet beschikbaar.</p>', ""

    kernpunten_html = "".join(
        f'<li>{refs_to_links(html.escape(k), news)}</li>' for k in ai.get("kernpunten", []))

    opmerkelijk = "".join(
        f'<div class="kaart"><div class="kaart-kop"><h4>{html.escape(o["titel"])}</h4></div>'
        f'<p>{refs_to_links(html.escape(o["tekst"]), news)}</p></div>'
        for o in ai.get("opmerkelijk", []))

    # -- live tracker rijen + JSON voor de client-side updater
    tracker_rows, holdings = [], []
    for c in crypto:
        up = c["change_pct"] >= 0
        waarde = c["aantal"] * c["last"]
        holdings.append({"id": c["id"], "ticker": c["ticker"], "aantal": c["aantal"],
                         "market": c.get("market", "")})
        tracker_rows.append(f"""<tr data-coin="{c['id']}">
          <td class="naam">{c['ticker']}<span class="sub">{html.escape(c['name'])}</span></td>
          <td class="koers js-prijs">€ {fmt(c['last'], 4 if c['last'] < 5 else 2)}</td>
          <td class="delta-cel js-24u">{pct_cell(c['change_pct'])}</td>
          <td class="delta-cel dim">{pct_cell(c['week_pct'])}</td>
          <td class="delta-cel dim">{pct_cell(c['maand_pct'])}</td>
          <td class="koers dim">€ {fmt(c['marketcap']/1e9, 1)} mld</td>
          <td class="koers js-waarde">€ {fmt(waarde)}</td>
          <td class="grafiek">{sparkline(c['spark'], up)}</td></tr>""")
    totaal = sum(c["aantal"] * c["last"] for c in crypto)

    per_coin = "".join(
        f'<div class="kaart"><div class="kaart-kop"><h4>{html.escape(c["ticker"])} · {html.escape(c["name"])}</h4>'
        f'<span class="badge b-middel js-coin-waarde" data-coin="{c["id"]}">€ {fmt(c["aantal"]*c["last"])}</span></div>'
        f'<p>{refs_to_links(html.escape(coin_teksten.get(c["ticker"], "")), news)}</p></div>'
        for c in crypto)

    analyse_map = {}
    for a in ai.get("analyse", []):
        analyse_map[a.get("categorie", "").lower()] = paras(a.get("tekst", ""), news)

    def analyse_voor(*sleutels):
        for s in sleutels:
            for k, v in analyse_map.items():
                if s in k:
                    return v
        return '<p class="dim">Geen analyse beschikbaar voor dit onderdeel.</p>'

    agenda = "".join(
        f'<tr><td class="ag-datum">{html.escape(a["datum"])}</td>'
        f'<td>{html.escape(a["gebeurtenis"])}<span class="sub">{html.escape(a.get("impact",""))}</span></td>'
        f'<td><span class="badge {badge.get(a["belang"],"b-laag")}">{a["belang"]}</span></td></tr>'
        for a in ai.get("macro_agenda", []))

    scenarios = "".join(
        f'<div class="kaart scen-{s["naam"].lower()}"><div class="kaart-kop"><h4>{html.escape(s["naam"])}</h4>'
        f'<span class="badge b-middel">kans: {html.escape(str(s["kans"]))}</span></div>'
        f'<p>{refs_to_links(html.escape(s["tekst"]), news)}</p></div>'
        for s in ai.get("scenarios", []))

    verwachtingen = "".join(
        f'<div class="kaart"><div class="kaart-kop"><h4>{html.escape(v["onderwerp"])}</h4>'
        f'<span class="badge b-laag">{html.escape(v["horizon"])}</span></div>'
        f'<p>{refs_to_links(html.escape(v["verwachting"]), news)}</p>'
        f'<div class="kaart-voet"><span class="badge {badge.get(v["vertrouwen"],"b-laag")}">vertrouwen: {v["vertrouwen"]}</span></div></div>'
        for v in ai.get("verwachtingen", []))

    risico = "".join(
        f'<tr><td class="naam">{html.escape(r["risico"])}</td>'
        f'<td><span class="badge {badge.get(r["kans"],"b-laag")}">kans: {r["kans"]}</span></td>'
        f'<td><span class="badge {badge.get(r["impact"],"b-laag")}">impact: {r["impact"]}</span></td>'
        f'<td class="dim">{html.escape(r["toelichting"])}</td></tr>'
        for r in ai.get("risico_radar", []))

    nieuws = "".join(
        f'<li id="bron-{n["ref"]}"><span class="refnum">[{n["ref"]}]</span>'
        f'<a href="{html.escape(n["link"])}">{html.escape(n["title"])}</a>'
        f'<span class="bron">{html.escape(n["source"])}</span></li>'
        for n in news)

    craap = "".join(
        f'<tr><td class="naam">{html.escape(s["name"])}<span class="sub">{html.escape(s["type"])}</span></td>'
        + "".join(f'<td class="craap-cel">{s[k]}</td>' for k in ("c", "r", "a", "ac", "p"))
        + f'<td class="craap-cel totaal">{s["c"]+s["r"]+s["a"]+s["ac"]+s["p"]}/25</td>'
        + f'<td class="dim">{html.escape(s["note"])}</td></tr>'
        for s in cfg.get("sources", []))

    with open(os.path.join(BASE, "template.html"), encoding="utf-8") as f:
        tpl = f.read()
    page = (tpl
        .replace("{{DATUM}}", date_str)
        .replace("{{TRACKER}}", "".join(tracker_rows))
        .replace("{{TOTAAL}}", f"€ {fmt(totaal)}")
        .replace("{{HOLDINGS_JSON}}", json.dumps(holdings))
        .replace("{{MARKTEN}}", quote_rows(markets))
        .replace("{{FX}}", quote_rows(fx))
        .replace("{{GRONDSTOFFEN}}", quote_rows(commodities))
        .replace("{{KERNPUNTEN}}", kernpunten_html)
        .replace("{{HOOFDVERHAAL}}", paras(ai.get("hoofdverhaal", ""), news))
        .replace("{{SPECULATIE}}", paras(ai.get("speculatie", ""), news))
        .replace("{{ANALYSE_AANDELEN}}", analyse_voor("aandel"))
        .replace("{{ANALYSE_CRYPTO}}", analyse_voor("crypto"))
        .replace("{{ANALYSE_VALUTA}}", analyse_voor("rente", "valuta"))
        .replace("{{ANALYSE_GRONDSTOFFEN}}", analyse_voor("grondstof"))
        .replace("{{SAMENVATTING}}", paras(ai.get("samenvatting", ""), news))
        .replace("{{FNG_GAUGE}}", fng_html)
        .replace("{{FNG_VOET}}", fng_voet)
        .replace("{{REC_GAUGE}}", rec_html)
        .replace("{{REC_VOET}}", rec_voet)
        .replace("{{MACRO_UITLEG}}", paras(ai.get("macro_uitleg", ""), news))
        .replace("{{OPMERKELIJK}}", opmerkelijk)
        .replace("{{AGENDA}}", agenda)
        .replace("{{SCENARIOS}}", scenarios)
        .replace("{{VERWACHTINGEN}}", verwachtingen)
        .replace("{{RISICO}}", risico)
        .replace("{{REFLECTIE}}", paras(ai.get("portfolio_reflectie", ""), news))
        .replace("{{PER_COIN}}", per_coin)
        .replace("{{NIEUWS}}", nieuws)
        .replace("{{CRAAP}}", craap))
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"✓ geschreven: {OUTPUT_PATH}")

    # -- korte tekst voor de pushmelding (ntfy) en als mailbody bij de bijlage
    import re as _re
    regels = [f"Portefeuille: € {fmt(sum(c['aantal'] * c['last'] for c in crypto))}", ""]
    for k in ai.get("kernpunten", [])[:5]:
        regels.append("• " + _re.sub(r"\[\d+\]", "", str(k)).strip())
    regels += ["", "Open de bijlage voor het volledige rapport — geschikt voor",
               "telefoon en laptop. De koersen zijn live zodra je hem opent."]
    notif_path = os.path.join(os.path.dirname(OUTPUT_PATH), "notificatie.txt")
    with open(notif_path, "w", encoding="utf-8") as f:
        f.write("\n".join(regels))
    print(f"✓ geschreven: {notif_path}")


# ----------------------------------------------------------------------- demo

def demo_data(cfg):
    import random
    random.seed(7)
    def spark(base, drift, n=22):
        vals, v = [], base
        for _ in range(n):
            v *= 1 + random.uniform(-0.009, 0.009) + drift
            vals.append(round(v, 4))
        return vals
    def q(name, last, d, w, drift, suffix=""):
        return {"name": name, "last": last, "change_pct": d, "week_pct": w,
                "spark": spark(last * 0.985, drift), "suffix": suffix}
    markets = [q("AEX", 942.18, 0.64, 1.8, 0.0006), q("S&P 500", 6412.55, -0.21, 0.9, -0.0002),
               q("Dow Jones", 44712.0, 0.10, 0.4, 0.0001), q("Nasdaq", 21830.4, -0.48, 1.2, -0.0004),
               q("DAX", 24512.9, 0.32, 1.1, 0.0004), q("FTSE 100", 9245.3, 0.18, 0.6, 0.0002),
               q("Nikkei 225", 41880.0, -0.75, -0.3, -0.0005)]
    fx = [q("EUR/USD", 1.0942, 0.12, 0.35, 0.0001), q("EUR/GBP", 0.8531, -0.05, 0.1, 0.0),
          q("US 10-jaars rente", 4.31, -0.55, -1.2, -0.0004, " %")]
    commodities = [q("Goud (USD/oz)", 3418.5, 0.42, 1.6, 0.0005),
                   q("Brent-olie (USD)", 71.8, -1.1, -2.4, -0.0009),
                   q("Zilver (USD/oz)", 41.2, 0.8, 2.8, 0.0009)]
    demo_prices = {"avalanche-2": (38.6, 3.1, 8.4, 12.1, 16.3e9, 0.9e9),
                   "ripple": (2.84, -0.9, 1.5, 6.2, 168e9, 4.2e9),
                   "hedera-hashgraph": (0.231, 1.4, 4.8, 11.2, 9.8e9, 0.31e9),
                   "algorand": (0.298, 0.7, 3.1, 8.9, 2.5e9, 0.12e9),
                   "cosmos": (6.42, -1.6, 0.8, 4.1, 2.5e9, 0.18e9),
                   "render-token": (7.85, 2.2, 9.6, 18.4, 4.1e9, 0.24e9),
                   "celestia": (4.12, 4.5, 12.3, 21.7, 2.9e9, 0.35e9),
                   "arweave": (12.4, -2.3, -1.2, 3.8, 0.82e9, 0.05e9),
                   "woo-network": (0.174, 1.1, 5.2, 9.4, 0.33e9, 0.02e9)}
    names = {"avalanche-2": "Avalanche", "ripple": "XRP", "hedera-hashgraph": "Hedera",
             "algorand": "Algorand", "cosmos": "Cosmos", "render-token": "Render",
             "celestia": "Celestia", "arweave": "Arweave", "woo-network": "WOO"}
    crypto = []
    for c in cfg["crypto"]:
        p, d, w, m, mc, vol = demo_prices[c["id"]]
        crypto.append({"id": c["id"], "ticker": c["ticker"], "name": names[c["id"]],
            "market": c.get("market", ""),
            "aantal": c["aantal"], "last": p, "change_pct": d, "week_pct": w,
            "maand_pct": m, "marketcap": mc, "volume": vol,
            "spark": spark(p * 0.96, 0.002 if d > 0 else -0.001)})
    news = [
        {"title": "ECB houdt rente onveranderd, Lagarde hint op verruiming in september", "link": "#", "source": "NOS Economie"},
        {"title": "Chipmakers onder druk na nieuwe Amerikaanse exportregels", "link": "#", "source": "NU.nl Economie"},
        {"title": "Bitcoin nears record high as ETF inflows accelerate", "link": "#", "source": "CNBC Finance"},
        {"title": "XRP rises on new bank-settlement partnership rumours", "link": "#", "source": "CoinDesk"},
        {"title": "Nederlandse inflatie koelt af naar 2,3 procent", "link": "#", "source": "NOS Economie"},
        {"title": "Avalanche foundation announces institutional staking program", "link": "#", "source": "CoinDesk"},
        {"title": "Fed officials signal patience ahead of July meeting", "link": "#", "source": "CNBC Finance"},
        {"title": "ECB publiceert voortgangsrapport digitale euro", "link": "#", "source": "ECB (persberichten)"},
    ]
    for i, n in enumerate(news, 1):
        n["ref"] = i
    ai = {
        "kernpunten": [
            "Je portefeuille staat op € 23.210, een winst van circa 2,1% ten opzichte van gisteren.",
            "Celestia (+4,5%) en Avalanche (+3,1%) zijn de sterkste posities; Arweave (-2,3%) blijft achter.",
            "De ECB hield de rente onveranderd en hintte op verruiming in september [1].",
            "Nederlandse inflatie koelde af naar 2,3%, dicht bij het ECB-doel [5].",
            "Fear & Greed staat op 72 (hebzucht) — veel goed nieuws lijkt ingeprijsd.",
            "De rentecurve is vrijwel vlak: recessiekans circa 26% binnen twaalf maanden.",
            "Het Fed-besluit van 29 juli is het belangrijkste moment voor je portefeuille [7]."],
        "hoofdverhaal":
            "Het dominante verhaal van vanochtend is de draai in het rentebeeld. De ECB liet de rente "
            "ongemoeid maar opende expliciet de deur naar verruiming in september [1], en de Nederlandse "
            "inflatiecijfers van gisteren [5] maken die stap geloofwaardig: met 2,3% zit de prijsstijging "
            "dicht bij het doel van 2%.\n"
            "Voor risicovolle beleggingen is dat rugwind. Een lagere rente betekent dat spaargeld en "
            "obligaties minder opleveren, waardoor beleggers eerder uitwijken naar aandelen en crypto. "
            "Precies dat zie je vanochtend terug: alle negen posities in je portefeuille staan op weekwinst.\n"
            "De kanttekening zit in de timing. De Fed vergadert pas op 29 juli [7] en toont voorlopig "
            "geduld. Zolang de Amerikaanse rente hoog blijft, is er een rem op hoeveel de euro-rente kan "
            "dalen — en blijft het risico dat de markt te ver vooruitloopt op het gunstige scenario.",
        "speculatie":
            "Wat volgt is nadrukkelijk hardop nadenken, geen voorspelling. De meest onderschatte "
            "mogelijkheid lijkt op dit moment niet een crash of een melt-up, maar een derde route: dat de "
            "rentedaling er wél komt maar dat crypto er veel minder van profiteert dan de markt aanneemt.\n"
            "De redenering daarachter: de rally van de afgelopen maanden is grotendeels gedragen door "
            "instroom in Bitcoin-ETF's [3]. Jouw portefeuille bevat echter geen Bitcoin. Altcoins liften "
            "historisch mee op zo'n instroom, maar met vertraging en in afnemende mate naarmate de cyclus "
            "vordert. Als het institutionele geld voornamelijk bij BTC blijft, kan het gebeuren dat de "
            "brede markt stijgt terwijl jouw posities achterblijven — een scenario dat in de gebruikelijke "
            "bull/bear-indeling helemaal niet voorkomt.\n"
            "Een tweede denklijn betreft het tempo van de renteverlagingen. Markten prijzen doorgaans een "
            "geleidelijk pad in. Maar centrale banken verlagen zelden geleidelijk: ze wachten lang en "
            "bewegen dan snel, meestal omdat er iets breekt in de economie. Zou de ECB in september "
            "verrassen met een grotere stap, dan is dat op het eerste gezicht goed nieuws, terwijl het in "
            "werkelijkheid zou signaleren dat de groeicijfers slechter zijn dan gedacht. Zulke momenten "
            "zijn historisch verraderlijk: de eerste reactie is positief, de tweede reactie zelden.\n"
            "Wat zou deze redenering ontkrachten? Als de altcoins de komende weken relatief sterker "
            "blijven presteren dan Bitcoin — de zogeheten marktbreedte — dan is de rotatie naar kleinere "
            "munten daadwerkelijk gaande en is het eerste scenario van tafel. Concreet zou je willen zien "
            "dat TIA, RENDER en AVAX hun voorsprong vasthouden in een week waarin BTC zijwaarts beweegt.\n"
            "Wat zou het juist bevestigen? Een week waarin het bredere cryptonieuws positief is maar jouw "
            "posities per saldo dalen. Dat is het klassieke patroon van kapitaal dat zich concentreert in "
            "de grootste namen wanneer beleggers voorzichtiger worden.\n"
            "De aanname die deze hele redenering draagt, is dat de ETF-instroom de dominante kracht blijft. "
            "Verandert dat — bijvoorbeeld doordat een grote partij een altcoin-product lanceert, of doordat "
            "regelgeving rond XRP definitief opheldert [4] — dan verschuift het hele speelveld en verliest "
            "bovenstaande logica zijn geldigheid.\n"
            "Tot slot een observatie die zelden wordt gemaakt: de Fear & Greed-index van 72 vertelt vooral "
            "iets over het verleden, niet over de toekomst. Hoge standen kunnen weken aanhouden. Het is "
            "geen timingsignaal, hooguit een herinnering dat de foutmarge kleiner wordt naarmate het "
            "sentiment uitbundiger is.",
        "macro_uitleg":
            "De belangrijkste kracht achter de markten is op dit moment de rente. Centrale banken "
            "bepalen met hun beleidsrente hoe duur geld is; als de rente daalt, worden toekomstige "
            "bedrijfswinsten en risicovolle beleggingen zoals aandelen en crypto relatief aantrekkelijker. "
            "De hint van de ECB op een verlaging in september [1] werkt daarom breed positief door — en "
            "verklaart waarom zowel de AEX als goud tegelijk kunnen stijgen.\n"
            "De tweede factor is inflatie. De afkoeling in Nederland naar 2,3% [5] brengt de eurozone dicht "
            "bij het ECB-doel van 2%, wat de renteverlaging geloofwaardig maakt. Zou de inflatie onverwacht "
            "weer oplopen, dan verdwijnt die ruimte en draait het gunstige scenario snel om. Het Amerikaanse "
            "PCE-cijfer van 31 juli is daarom het belangrijkste datapunt van de komende twee weken.\n"
            "De rentecurve — het verschil tussen lang- en kortlopende rente — staat op dit moment vrijwel "
            "vlak (-0,04 procentpunt). Normaal ligt de lange rente hoger dan de korte; als dat omdraait, "
            "is dat historisch een van de betrouwbaarste voorspellers van een recessie. Het NY Fed-model "
            "vertaalt de huidige stand naar een recessiekans van circa 26% binnen twaalf maanden: verhoogd, "
            "maar geen alarmniveau.\n"
            "Voor de economische groei kijkt de markt vooral naar de inkoopmanagersindices (PMI's) van "
            "vrijdag: boven de 50 duidt op groei, eronder op krimp. Zwakke Aziatische vraag drukt intussen "
            "de olieprijs — vervelend voor energieproducenten, maar per saldo desinflatoir en dus gunstig "
            "voor het rentepad [5].\n"
            "Geopolitiek blijft de chipsector het brandpunt: de nieuwe Amerikaanse exportregels [2] laten "
            "zien hoe handelsbeleid in één dag een hele sector kan herprijzen. Voor de AEX, met zijn zware "
            "halfgeleiderweging, is dit de belangrijkste externe risicofactor.\n"
            "Voor jouw portfolio is de optelsom: dalende rente en een Fear & Greed-stand van 72 (hebzucht) "
            "vormen nu rugwind voor crypto, maar diezelfde hebzucht-stand betekent ook dat veel goed nieuws "
            "al is ingeprijsd. Historisch volgen op standen boven de 75 vaker correcties dan doorbraken.",
        "opmerkelijk": [
            {"titel": "Crypto ontkoppelt van tech", "tekst":
             "Opvallend: de Nasdaq daalde (-0,48%) terwijl alle vier je cryptoposities stegen. Normaal "
             "bewegen crypto en techaandelen sterk samen; zo'n divergentie duidt erop dat de ETF-instroom [3] "
             "momenteel zwaarder weegt dan het algemene risicosentiment."},
            {"titel": "Zilver verslaat goud ruim", "tekst":
             "Zilver staat op +2,8% deze week tegenover +1,6% voor goud. Wanneer zilver het voortouw neemt, "
             "wijst dat historisch vaak op speculatievere instroom in edelmetalen — een teken van toenemende "
             "risicobereidheid."},
            {"titel": "AVAX is de stille uitblinker", "tekst":
             "Met +3,1% vandaag en +12,1% deze maand beweegt Avalanche [6] sterker dan Bitcoin, terwijl het "
             "nieuws er minder over gaat. Kleinere munten met hogere bèta versterken zowel de winst als het "
             "risico in je portefeuille."}],
        "samenvatting":
            "Europese beurzen openen naar verwachting licht hoger. De afkoelende Nederlandse inflatie [5] "
            "voedt de verwachting dat de ECB in september ruimte krijgt voor een renteverlaging, iets waar "
            "Lagarde gisteren al op hintte [1]. De rentemarkt reageerde direct: de Amerikaanse tienjaarsrente "
            "zakte verder terug.\n"
            "De uitzondering op het positieve beeld is de chipsector, die opnieuw onder druk staat door "
            "aangescherpte Amerikaanse exportregels [2]. Voor de AEX, met zijn zware weging in halfgeleiders, "
            "is dit de belangrijkste rem op verdere koerswinst.\n"
            "Crypto is de sterkste categorie van het moment. Bitcoin nadert zijn record dankzij aanhoudende "
            "ETF-instroom [3], en ook de altcoins liften mee: XRP profiteert van geruchten over een nieuw "
            "bankenpartnerschap [4] en Avalanche kondigde een institutioneel staking-programma aan [6].\n"
            "In de VS is het beeld afwachtend. Fed-functionarissen tonen geduld richting de vergadering van "
            "eind juli [7], waardoor de dollar per saldo weinig beweegt en goud licht verder oploopt.\n"
            "Grondstoffen laten een gemengd beeld zien: edelmetalen profiteren van de lagere rente, terwijl "
            "olie terugzakt op zorgen over de vraag uit Azië.",
        "analyse": [
            {"categorie": "Aandelen", "tekst":
             "Het Europese aandelenbeeld wordt gedragen door de rente: elke bevestiging van een naderende "
             "ECB-verlaging [1] geeft steun aan waarderingen, vooral bij rentegevoelige sectoren als vastgoed "
             "en nutsbedrijven. De inflatiedaling in Nederland [5] past in dat plaatje.\n"
             "Daartegenover staat de chipsector, die door de nieuwe exportregels [2] geopolitiek risico "
             "herprijst. Voor de AEX is dit relevant: de index leunt zwaar op deze sector, waardoor het "
             "indexbeeld positiever kan ogen dan het onderliggende sentiment.\n"
             "Wall Street beweegt zijwaarts in afwachting van de Fed [7] en de grote kwartaalcijferweek eind juli."},
            {"categorie": "Crypto", "tekst":
             "De ETF-instroom blijft de dominante kracht achter Bitcoin [3]; zolang die aanhoudt is elke dip "
             "tot nu toe gekocht. De marktbreedte verbetert bovendien: waar eerdere rally's vooral BTC-gedreven "
             "waren, doen ETH, XRP [4] en AVAX [6] nu duidelijk mee.\n"
             "Let wel op de kalender: het Fed-besluit van eind juli [7] is historisch een volatiel moment voor "
             "crypto. De hoge samenhang tussen de vier grote munten betekent dat een correctie zelden één munt "
             "spaart."},
            {"categorie": "Rente & valuta", "tekst":
             "Het renteverschil tussen de VS en de eurozone versmalt nu de ECB eerder lijkt te gaan verlagen [1] "
             "dan de Fed [7]. Per saldo houdt dat EUR/USD in een nauwe bandbreedte rond 1,09.\n"
             "De dalende kapitaalmarktrente is de stille motor achter zowel aandelen als goud; een onverwacht "
             "inflatiecijfer kan die motor abrupt stilzetten."},
            {"categorie": "Grondstoffen", "tekst":
             "Goud en zilver profiteren van de lagere reële rente en de aanhoudende centrale-bankaankopen. "
             "Zilver is met bijna 3% weekwinst de uitblinker.\n"
             "Brent-olie beweegt de andere kant op: zwakke Aziatische vraagcijfers drukken de prijs richting "
             "de onderkant van de recente bandbreedte, wat op termijn juist desinflatoir doorwerkt [5]."}],
        "macro_agenda": [
            {"datum": "wo 22 jul", "gebeurtenis": "VS: bestaande woningverkopen (juni)", "impact": "USD, rente", "belang": "middel"},
            {"datum": "do 23 jul", "gebeurtenis": "ECB-rentebesluit + persconferentie Lagarde", "impact": "EUR, Europese aandelen, rente", "belang": "hoog"},
            {"datum": "vr 24 jul", "gebeurtenis": "Eurozone: inkoopmanagersindices (PMI, juli)", "impact": "Europese aandelen, EUR", "belang": "hoog"},
            {"datum": "di 28 jul", "gebeurtenis": "Start big-tech kwartaalcijferweek (VS)", "impact": "Nasdaq, wereldwijd sentiment, crypto", "belang": "hoog"},
            {"datum": "wo 29 jul", "gebeurtenis": "Fed-rentebesluit (FOMC) + persconferentie Powell", "impact": "alles — historisch volatiel voor crypto", "belang": "hoog"},
            {"datum": "do 30 jul", "gebeurtenis": "Eurozone: BBP tweede kwartaal (flash)", "impact": "EUR, Europese aandelen", "belang": "middel"},
            {"datum": "vr 31 jul", "gebeurtenis": "VS: PCE-inflatie (favoriete Fed-maatstaf)", "impact": "USD, rente, goud", "belang": "hoog"}],
        "scenarios": [
            {"naam": "Bull", "kans": "30%", "tekst":
             "De Fed klinkt eind juli duidelijk milder [7] en de ETF-instroom versnelt [3]. Bitcoin breekt door "
             "zijn record en trekt de altcoins mee; de portfolio profiteert over de volle breedte, met AVAX en "
             "XRP als grootste uitschieters door hun hogere bèta."},
            {"naam": "Basis", "kans": "50%", "tekst":
             "Centrale banken houden de kaarten tegen de borst; markten bewegen zijwaarts met af en toe een "
             "uitschieter rond macrocijfers. Crypto consolideert op hoge niveaus. De portfolio beweegt per "
             "saldo weinig, met normale dagelijkse schommelingen van enkele procenten."},
            {"naam": "Bear", "kans": "20%", "tekst":
             "Een tegenvallend inflatiecijfer of hardere Fed-toon zet de rente hoger. Risicovolle activa "
             "corrigeren en crypto, als meest risicovolle categorie, het hardst — correcties van 10-20% zijn "
             "dan niet ongebruikelijk. Door de 100% crypto-allocatie is er geen demping in de portfolio."}],
        "verwachtingen": [
            {"onderwerp": "Bitcoin", "horizon": "deze week", "verwachting": "Consolidatie net onder het record; een doorbraak vergt aanhoudende ETF-instroom [3].", "vertrouwen": "middel"},
            {"onderwerp": "Ethereum", "horizon": "deze week", "verwachting": "Relatieve kracht t.o.v. BTC houdt aan zolang de altcoin-rotatie doorzet.", "vertrouwen": "middel"},
            {"onderwerp": "XRP", "horizon": "deze maand", "verwachting": "Nieuwsgedreven: bevestiging van het bankenpartnerschap [4] is de katalysator, ontkenning het risico.", "vertrouwen": "laag"},
            {"onderwerp": "Europese aandelen", "horizon": "deze week", "verwachting": "Licht positief richting het ECB-besluit; PMI's vrijdag bepalen de slotrichting.", "vertrouwen": "middel"},
            {"onderwerp": "Rente", "horizon": "deze maand", "verwachting": "ECB verlaagt in september; de markt prijst dit al grotendeels in [1].", "vertrouwen": "hoog"},
            {"onderwerp": "Goud", "horizon": "deze maand", "verwachting": "Gesteund zolang de reële rente daalt; PCE-cijfer van 31 juli is het ijkpunt.", "vertrouwen": "middel"}],
        "risico_radar": [
            {"risico": "Concentratie: portfolio is 100% crypto", "kans": "hoog", "impact": "hoog", "toelichting": "Alle vier posities bewegen sterk samen; er is geen spreiding over categorieën."},
            {"risico": "Hardere Fed-toon op 29 juli", "kans": "middel", "impact": "hoog", "toelichting": "Historisch het meest volatiele moment van de maand voor crypto."},
            {"risico": "Escalatie chip-exportregels", "kans": "middel", "impact": "middel", "toelichting": "Raakt vooral Europese indices en het bredere risicosentiment."},
            {"risico": "Regelgeving rond XRP/altcoins", "kans": "middel", "impact": "middel", "toelichting": "Nieuwsgedreven posities zijn gevoelig voor juridische wendingen."},
            {"risico": "Stablecoin- of exchange-incident", "kans": "laag", "impact": "hoog", "toelichting": "Zeldzaam, maar raakt de hele cryptomarkt tegelijk."}],
        "portfolio_reflectie":
            "Je portefeuille bestaat uit negen altcoins, met XRP, Hedera en Avalanche als zwaarste posities. "
            "De week verloopt overwegend positief: vooral de infrastructuur-munten (TIA, RENDER, HBAR) laten "
            "dubbele groeicijfers op maandbasis zien, terwijl Arweave als enige duidelijk achterblijft.\n"
            "Kenmerkend voor deze samenstelling: er zit géén Bitcoin of Ethereum in. Altcoins hebben een "
            "hogere bèta — ze stijgen harder in goede weken, maar dalen ook harder wanneer het sentiment "
            "draait. Bovendien bewegen ze onderling sterk samen, dus de spreiding over negen munten dempt "
            "minder dan het lijkt. Het Fed-besluit van 29 juli [7] is voor deze portefeuille het "
            "spannendste moment van de maand.\n"
            "Ter overweging (geen advies): veel beleggers herijken na een sterke maand hun verhoudingen, "
            "zodat winnaars als Celestia en Render niet ongemerkt een steeds groter deel van het geheel worden.",
        "per_coin": [
            {"ticker": "AVAX", "tekst": "Sterke dag (+3,1%) na de aankondiging van het institutionele staking-programma [6]. Structureel goed nieuws, maar AVAX blijft een van de beweeglijkste grote posities."},
            {"ticker": "XRP", "tekst": "Je grootste positie en tegelijk de meest nieuwsgevoelige: het gerucht over een bankenpartnerschap [4] stuwt de koers, maar zonder bevestiging kan die winst even snel verdampen."},
            {"ticker": "HBAR", "tekst": "Gestage stijger (+11,2% op maandbasis) met relatief lage volatiliteit binnen je selectie. Hedera profiteert van de bredere institutionele interesse in enterprise-blockchains."},
            {"ticker": "ALGO", "tekst": "Beweegt rustig mee met de markt (+0,7% vandaag). Algorand is binnen je portefeuille een van de defensievere altcoin-posities."},
            {"ticker": "ATOM", "tekst": "Zwakke dag (-1,6%) en de maandprestatie blijft achter bij de rest. Cosmos mist momenteel een duidelijke eigen katalysator."},
            {"ticker": "RENDER", "tekst": "Sterke maand (+18,4%) op het AI-thema: vraag naar gedecentraliseerde GPU-rekenkracht. Let op: dit thema is sentimentgevoelig en kan snel draaien."},
            {"ticker": "TIA", "tekst": "Uitblinker van je portefeuille (+21,7% op maandbasis, +4,5% vandaag). Celestia rijdt mee op de modulaire-blockchain-trend, maar is daarmee ook je snelst bewegende positie."},
            {"ticker": "AR", "tekst": "Enige duidelijke achterblijver (-2,3% vandaag, -1,2% op weekbasis). Arweave beweegt momenteel los van de rest; houd in de gaten of dit een tijdelijke adempauze of een trendbreuk is."},
            {"ticker": "WOO", "tekst": "Kleinste positie qua waarde, met een nette week (+5,2%). WOO is sterk gekoppeld aan handelsvolumes op crypto-beurzen en dus aan de algehele marktactiviteit."}],
    }
    return markets, crypto, fx, commodities, news, ai


# ------------------------------------------------------------------------ main

MAANDEN = ["januari","februari","maart","april","mei","juni","juli",
           "augustus","september","oktober","november","december"]
DAGEN = ["maandag","dinsdag","woensdag","donderdag","vrijdag","zaterdag","zondag"]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    now = dt.datetime.now()
    date_str = f"{DAGEN[now.weekday()]} {now.day} {MAANDEN[now.month-1]} {now.year}"
    if args.demo:
        markets, crypto, fx, commodities, news, ai = demo_data(cfg)
        fng = {"value": 72, "label": "Greed"}
        recession = {"prob": 26, "spread": -0.04, "y10": 4.31, "y3m": 4.35}
    else:
        print("Data ophalen…")
        markets = fetch_quotes(cfg["indices"])
        fx = fetch_quotes(cfg["fx_rates"])
        commodities = fetch_quotes(cfg["commodities"])
        crypto = fetch_crypto(cfg["crypto"])
        news = fetch_news(cfg["news_feeds"])
        fng = fetch_fear_greed()
        recession = fetch_recession()
        print("AI-analyse genereren…")
        ai = ai_sections(markets, crypto, fx, commodities, news, date_str,
                         fng=fng, recession=recession)
    render(cfg, markets, crypto, fx, commodities, news, ai, date_str,
           fng=fng, recession=recession)

if __name__ == "__main__":
    main()
