"""
LMB Scraper — standen (posiciones) per zone.
Bron: https://lmb.com.mx/posiciones (Liga Mexicana de Beisbol)

Zelfde robots.txt-beperking als bij de programma/uitslagen-scraper
(lmb_schedule_scraper.py, zie die docstring voor het volledige verhaal):
lmb.com.mx verbiedt /posiciones/api expliciet voor crawlers, dus ook hier
lezen we de normale, wél toegestane pagina uit in plaats van dat endpoint
zelf aan te roepen.

In tegenstelling tot /juegos staat de standenlijst hier al kant-en-klaar
in de server-gerenderde HTML — geen client-side fetch, dus ook geen
periodieke-herhaalpoging-gedoe zoals bij de programma-pagina. Wel
rendert de pagina de tabel drie keer naast elkaar, voor drie responsive
lay-outs (telefoon/tablet/desktop): de telefoon-lay-out toont maar één
zone tegelijk (je moet daar op "Norte"/"Sur" klikken om te wisselen),
maar de tablet-lay-out toont Noord én Zuid al meteen allebei zonder
enige klik. Daarom lezen we specifiek die tablet-lay-out uit, ongeacht
het echte viewport van de headless browser (het gaat om de aanwezige
HTML, niet om wat er zichtbaar op het scherm staat).

De zone-namen ("Norte"/"Sur") lezen we uit de tab-knoppen op de pagina
zelf i.p.v. ze hard te coderen, mocht de site ooit een derde zone of een
andere naam toevoegen.
"""
import json
import re
import time
import datetime as dt
from datetime import timezone
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

URL = "https://lmb.com.mx/posiciones"
JSON_FILE = "lmb_standings.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Volgorde van de kolommen zoals ze ná de team-kolom in de tabel staan.
# Betekenis (Spaanse glossary op de site zelf): G/P = gewonnen/verloren,
# PCT = winstpercentage, DIF = achterstand op de leider ("-" voor de
# leider zelf), U10 = laatste 10 wedstrijden, RACHA = reeks (bv. "W3"),
# CA/CP = carreras anotadas/permitidas (gescoorde/toegestane runs),
# DIF C' = run-verschil, X-G/P = verwachte W-L o.b.v. runs (Pythagoras),
# CASA/VISITANTE = thuis-/uitrecord, >.500 = record tegen ploegen boven
# de .500.
KOLOM_VELDEN = [
    "w", "l", "pct", "verschil", "laatste10", "reeks",
    "ca", "cp", "run_verschil", "verwacht_wl", "thuis", "uit", "tegen_boven_500",
]

TABLET_LAYOUT_SELECTOR = '[class*="StandingsLayout_tabletLayout__"]'
TABEL_SELECTOR = '[class*="StatsTable_entityTable__"]'


def maak_absoluut(src):
    if not src:
        return None
    match = re.search(r"[?&]url=([^&]+)", src)
    if match:
        return unquote(match.group(1))
    if src.startswith("http"):
        return src
    return "https://lmb.com.mx" + src


def verwijder_cookiebanner(page):
    """Haalt de consent-wrapper (banner + backdrop + cookie-icoon) hard weg."""
    try:
        page.evaluate("document.getElementById('silktide-wrapper')?.remove()")
    except Exception:
        pass


def wacht_op_tabellen(page, timeout_ms=90000):
    """
    Wacht tot de tablet-layout minstens 2 standentabellen bevat.
    lmb.com.mx is in de praktijk soms traag om te laden (zie ook
    lmb_schedule_scraper.py), dus een ruime timeout.
    """
    page.wait_for_function(
        f"""() => document.querySelectorAll(
            '{TABLET_LAYOUT_SELECTOR} {TABEL_SELECTOR}'
        ).length >= 2""",
        timeout=timeout_ms,
    )


def haal_zone_namen(page, aantal_zones):
    """
    Leest de zone-namen uit de tab-knoppen (bv. "Norte"/"Sur"). Die
    tab-knoppen zitten alleen in de telefoon-layout (de tablet-layout
    toont alle zones al zonder tabs), dus zoeken we breed in de pagina.
    """
    labels = page.locator('[class*="TabMenu_tab__"] span')
    namen = [labels.nth(i).inner_text().strip() for i in range(labels.count())]
    namen = [n for n in namen if n]
    if len(namen) >= aantal_zones:
        return namen[:aantal_zones]
    # Terugval als de tab-knoppen onverwacht ontbreken of anders heten.
    fallback = ["Norte", "Sur", "Zone 4", "Zone 5"]
    return fallback[:aantal_zones]


def parse_tabel(tabel):
    """Zet één <table> (één zone) om naar een lijst van rij-dicts."""
    rijen = tabel.locator("tbody tr")
    resultaat = []
    for i in range(rijen.count()):
        rij = rijen.nth(i)
        cellen = rij.locator("td")
        team_cel = cellen.first
        naam_loc = team_cel.locator("span")
        naam = naam_loc.inner_text().strip() if naam_loc.count() else "-"
        img = team_cel.locator("img")
        logo = maak_absoluut(img.get_attribute("src")) if img.count() else None

        waarden = {}
        for veld_idx, veld_naam in enumerate(KOLOM_VELDEN):
            cel = cellen.nth(veld_idx + 1)
            waarden[veld_naam] = cel.inner_text().strip() if cel.count() else None

        def als_getal(sleutel):
            waarde = waarden.get(sleutel) or ""
            return int(waarde) if waarde.lstrip("-").isdigit() else None

        w = als_getal("w")
        l = als_getal("l")
        resultaat.append({
            "positie": i + 1,
            "team": naam,
            "team_logo": logo,
            "gespeeld": (w + l) if (w is not None and l is not None) else None,
            "w": w,
            "l": l,
            "pct": waarden["pct"],
            "verschil": waarden["verschil"],
            "laatste10": waarden["laatste10"],
            "reeks": waarden["reeks"],
            "ca": als_getal("ca"),
            "cp": als_getal("cp"),
            "run_verschil": waarden["run_verschil"],
            "verwacht_wl": waarden["verwacht_wl"],
            "thuis": waarden["thuis"],
            "uit": waarden["uit"],
            "tegen_boven_500": waarden["tegen_boven_500"],
        })
    return resultaat


def main():
    pogingen = 3
    laatste_fout = None
    standen = {}
    for poging in range(1, pogingen + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=USER_AGENT)
                page = context.new_page()
                print(f"Pagina laden (poging {poging}/{pogingen}): {URL}")
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                verwijder_cookiebanner(page)
                wacht_op_tabellen(page)

                tablet_layout = page.locator(TABLET_LAYOUT_SELECTOR).first
                tabellen = tablet_layout.locator(TABEL_SELECTOR)
                aantal = tabellen.count()
                print(f"→ {aantal} standentabel(len) gevonden")

                zone_namen = haal_zone_namen(page, aantal)
                standen = {}
                for i in range(aantal):
                    zone = zone_namen[i] if i < len(zone_namen) else f"Zone {i + 1}"
                    rijen = parse_tabel(tabellen.nth(i))
                    standen[zone] = rijen
                    print(f"  {zone}: {len(rijen)} teams")

                browser.close()
            break
        except Exception as e:
            laatste_fout = e
            print(f"Poging {poging} mislukt: {e}")
            if poging < pogingen:
                time.sleep(5)
    else:
        raise RuntimeError(f"Alle {pogingen} pogingen mislukt: {laatste_fout}")

    output = {
        "bijgewerkt": dt.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bron": URL,
        "standen": standen,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n{JSON_FILE} geschreven.")


if __name__ == "__main__":
    main()
