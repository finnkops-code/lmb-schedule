"""
LMB Scraper — statistieken van individuele spelers (bateo + pitcheo).
Bron: https://lmb.com.mx/estadisticas (Liga Mexicana de Beisbol)

Zelfde robots.txt-beperking als bij de andere twee LMB-scrapers
(lmb_schedule_scraper.py en lmb_standings_scraper.py, zie die docstrings
voor het volledige verhaal): lmb.com.mx verbiedt /estadisticas/api
expliciet voor crawlers.

Deze pagina laadt zijn EERSTE pagina met statistieken server-side (net
als /posiciones), maar elke volgende paginering-klik ("Próx"),
tabwisseling (Bateo/Pitcheo) of filterwijziging (bv. "Todos los
Jugadores" i.p.v. de standaard "Jugadores Calificados") laat de site
zelf, client-side, een extra call doen naar precies dat verboden
/estadisticas/api/player-endpoint. Net als bij /juegos roepen we dat
endpoint dus nooit zelf aan: we simuleren de klikken die een echte
bezoeker ook zou doen op de wél-toegestane pagina, en lezen steeds de
resulterende, ververste tabel uit de DOM.

We gebruiken bewust het "Todos los Jugadores" (ALL) spelerfilter i.p.v.
de standaard "Jugadores Calificados" (QUALIFIED): de gebruiker vroeg
expliciet om alle statistieken van individuele spelers, niet alleen de
kwalificatielijst. Dat levert wel veel meer pagina's op (tientallen per
categorie), maar de paginering zelf is simpel: we klikken net zo lang op
"Próx" tot die knop niet meer bestaat (op de laatste pagina verdwijnt
hij uit de paginator).

lmb.com.mx is, net als bij de andere twee scrapers gebleken, af en toe
traag of tijdelijk instabiel (504's op los-laadbare onderdelen); vandaar
weer een ruime aanlooptijd voor de eerste laadbeurt per categorie en een
retry-met-verse-browser-wrapper in main().
"""
import json
import re
import time
import datetime as dt
from datetime import timezone
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

URL = "https://lmb.com.mx/estadisticas"
JSON_FILE = "lmb_player_stats.json"
MX_TZ = "America/Mexico_City"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Kolomvolgorde ná de spelerkolom, per categorie — komt exact overeen met
# de kolomvolgorde in de tabel op de site zelf (zie ook de glossary-
# tooltips op de pagina voor de volledige omschrijving per kolom).
BATEO_VELDEN = [
    "juegos", "turnos", "carreras", "hits", "dobles", "triples", "jonrones",
    "impulsadas", "boletos", "ponches", "bases_robadas", "atrapado_robando",
    "promedio", "obp", "slg", "ops",
]
PITCHEO_VELDEN = [
    "juegos_ganados", "juegos_perdidos", "efectividad", "juegos", "aperturas",
    "juegos_completos", "blanqueadas", "salvados", "oportunidades_salvado",
    "entradas_lanzadas", "hits_permitidos", "carreras_permitidas",
    "carreras_limpias", "jonrones_permitidos", "golpeados",
    "boletos_otorgados", "ponches", "whip", "promedio_bateo_rival",
]

# lmb.com.mx rendert de statistiekentabel, net als de standenpagina,
# redundant in meerdere responsive lay-outs tegelijk (phone/tablet/
# desktop), elk met een EIGEN <table>-element en een net iets andere
# klassenaam (bv. "StatsTable_entityTable__tDPxk" binnen de (soms
# verborgen) tablet-laag versus "StatsLayout_entityTable__eKIac" binnen
# de desktop-laag) — beide bevatten wel exact dezelfde 10 rijen. Welke
# van de twee daadwerkelijk zichtbaar is hangt af van de viewportbreedte
# op het moment van laden. In plaats van op één specifieke klassenaam te
# gokken (wat op smallere/andere breedtes een onzichtbare of verouderde
# kopie kan opleveren), matchen we op het gedeelde substring
# "entityTable__" en pakken we altijd expliciet de op dat moment
# zichtbare tabel.
TABEL_SELECTOR = 'table[class*="entityTable__"]:visible'
MAX_PAGINAS = 300  # veiligheidslimiet tegen een eventuele oneindige lus
API_PATROON = re.compile(r"/estadisticas/api/")


def actieve_tabel(page):
    """Geeft de op dit moment zichtbare statistiekentabel terug (zie de
    toelichting bij TABEL_SELECTOR hierboven)."""
    return page.locator(TABEL_SELECTOR).first


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


def open_filters(page):
    """Klikt de 'Mostrar Filtros'-schakelaar aan zodat de filter-selects
    (Jugador/Equipo/Posición/...) daadwerkelijk in de DOM te bedienen
    zijn — maar alleen als het paneel nog niet open staat, anders klapt
    een tweede klik (bv. bij de tweede categorie) het weer dicht i.p.v.
    open te houden. Dit klapt alleen een paneel open (geen databehoefte),
    dus hier hoeft niet op een API-call gewacht te worden."""
    paneel = page.locator(".StatFilters_filterWrapper__v5bMw").first
    if paneel.count() and paneel.is_visible():
        return
    verwijder_cookiebanner(page)
    knop = page.locator(".StatFilters_toggle__ZREHg:visible").first
    try:
        knop.click(timeout=15000)
    except Exception:
        pass


def wacht_op_tabel(page, timeout_ms=90000):
    """Wacht tot de zichtbare tabel (zie actieve_tabel()) een eerste rij
    met echte inhoud toont. ":visible" is een Playwright-eigen
    pseudo-klasse (niet bruikbaar in ruwe DOM-JS), dus we gebruiken
    hiervoor uitsluitend Locator-methodes, niet page.evaluate/
    querySelector."""
    rij = actieve_tabel(page).locator("tbody tr").first
    rij.wait_for(state="visible", timeout=timeout_ms)

    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if rij.inner_text().strip():
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Tabelrij bleef leeg binnen {timeout_ms}ms."
            )
        time.sleep(0.3)


def klik_en_wacht_op_data(page, actie, timeout_ms=90000):
    """Voert actie() uit (een klik, een select-wijziging, ...) en wacht
    tot de site ZELF, client-side, klaar is met de bijbehorende aanroep
    naar /estadisticas/api/... — wij roepen dat verboden (robots.txt)
    endpoint dus nooit zelf aan, we wachten alleen tot de browser (net
    als bij een echte bezoeker) de eigen aanroep heeft afgerond, en geven
    React vervolgens een fractie van een seconde om de nieuwe tabel te
    tekenen.

    Verwijdert vlak vóór elke actie ook opnieuw de cookiebanner: die kan,
    net als bij lmb_schedule_scraper.py gebleken, later in de sessie
    terugkomen (of pas na de eerste, vroege verwijdering alsnog
    ingeladen worden) en dan met zijn backdrop precies de knop blokkeren
    waar we op willen klikken."""
    verwijder_cookiebanner(page)
    with page.expect_response(
        lambda r: API_PATROON.search(r.url) is not None, timeout=timeout_ms
    ):
        actie()
    page.wait_for_timeout(400)


def kies_alle_spelers(page):
    """Zet het 'Jugador'-filter op 'Todos los Jugadores' (ALL) i.p.v. de
    standaard 'Jugadores Calificados' (QUALIFIED). Slaat de wijziging
    over als het filter al op ALL staat (bv. na een eerdere categorie)."""
    select = page.locator(
        '.StatFilters_selectWrapper__oZeQT:has(option[value="QUALIFIED"]) select'
    ).first
    if select.input_value() == "ALL":
        return
    klik_en_wacht_op_data(page, lambda: select.select_option("ALL", timeout=15000))


def kies_categorie(page, label):
    """Klikt op de Bateo/Pitcheo-tab en wacht — als de tab niet al actief
    was — tot de site de nieuwe statistieken heeft opgehaald."""
    tab = page.locator(".TabMenu_tab__FG08G:visible", has_text=label).first
    reeds_actief = "TabMenu_selected" in (tab.get_attribute("class") or "")
    if reeds_actief:
        return
    klik_en_wacht_op_data(page, lambda: tab.click(timeout=15000))


def volgende_pagina_knop(page):
    knop = page.locator('.StatsLayout_pageNext__hrU6k:visible', has_text="Próx")
    return knop if knop.count() else None


def eerste_pagina(page):
    """Klikt op 'First' als die knop bestaat (dus als we niet al op
    pagina 1 staan) en wacht op de bijbehorende databron-aanroep."""
    knop = page.locator('.StatsLayout_pageNext__hrU6k:visible', has_text="First")
    if knop.count():
        klik_en_wacht_op_data(page, lambda: knop.first.click(timeout=15000))


def lees_rij(rij, veld_namen):
    link = rij.locator("a").first
    href = link.get_attribute("href") or ""
    match = re.search(r"/jugador/(\d+)", href)
    speler_id = match.group(1) if match else None

    volledige_tekst = link.inner_text().strip()
    positie = None
    pos_el = link.locator("i").first
    if pos_el.count():
        positie = pos_el.inner_text().strip()
        naam = volledige_tekst[: -(len(positie))].strip() if positie else volledige_tekst
    else:
        naam = volledige_tekst

    img = rij.locator("img").first
    if img.count():
        ruwe_team = img.get_attribute("alt") or ""
        # Genormaliseerd (trim + interne witruimte samengevoegd): de site
        # levert soms een team-naam met een verdwaalde spatie (bv. een
        # trailing space), waardoor bateo- en pitcheo-rijen van hetzelfde
        # team anders als twee verschillende teams worden gezien door alle
        # code die de teamnaam als exacte matching-/groeperingssleutel
        # gebruikt (o.a. de PHP-widget).
        team = " ".join(ruwe_team.split()) or None
    else:
        team = None
    team_logo = maak_absoluut(img.get_attribute("src")) if img.count() else None

    cellen = rij.locator("td")
    stats = {}
    for idx, veld in enumerate(veld_namen):
        cel = cellen.nth(idx + 1)
        stats[veld] = cel.inner_text().strip() if cel.count() else None

    resultaat = {
        "speler_id": speler_id,
        "naam": naam,
        "positie": positie,
        "team": team,
        "team_logo": team_logo,
    }
    resultaat.update(stats)
    return resultaat


def lees_huidige_pagina(page, veld_namen):
    rijen = actieve_tabel(page).locator("tbody tr")
    return [lees_rij(rijen.nth(i), veld_namen) for i in range(rijen.count())]


def haal_categorie_op(page, label, veld_namen):
    kies_categorie(page, label)
    open_filters(page)
    kies_alle_spelers(page)
    eerste_pagina(page)
    wacht_op_tabel(page)

    spelers = []
    pagina = 1
    while True:
        spelers.extend(lees_huidige_pagina(page, veld_namen))
        print(f"  {label}: pagina {pagina} gelezen ({len(spelers)} spelers tot nu toe)")

        volgende = volgende_pagina_knop(page)
        if not volgende or pagina >= MAX_PAGINAS:
            break
        klik_en_wacht_op_data(page, lambda: volgende.first.click(timeout=15000))
        pagina += 1

    return spelers


def main():
    seizoen = dt.datetime.now(dt.timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo(MX_TZ)
    ).year

    pogingen = 3
    laatste_fout = None
    bateo, pitcheo = [], []
    gelukt = False

    # Eén sync_playwright()-driver voor de hele run (niet per poging
    # opnieuw aanmaken): herhaaldelijk een nieuwe sync_playwright()-
    # instantie starten in hetzelfde proces bleek in de praktijk
    # (GitHub Actions) tot een corrupte asyncio-status te kunnen leiden
    # zodra een eerdere poging vroegtijdig faalde ("This event loop is
    # already running" / "Please use the Async API instead"), omdat de
    # browser dan niet netjes werd afgesloten vóórdat de driver zelf
    # stopte. We herstarten daarom alleen de BROWSER per poging, en
    # sluiten die altijd af via try/finally — ook als er onderweg een
    # fout optreedt.
    with sync_playwright() as p:
        for poging in range(1, pogingen + 1):
            browser = None
            try:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="es-MX",
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()
                print(f"Pagina laden (poging {poging}/{pogingen}): {URL}")
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                verwijder_cookiebanner(page)
                wacht_op_tabel(page)

                print("Bateo-statistieken lezen...")
                bateo = haal_categorie_op(page, "Bateo", BATEO_VELDEN)

                print("Pitcheo-statistieken lezen...")
                pitcheo = haal_categorie_op(page, "Pitcheo", PITCHEO_VELDEN)

                gelukt = True
            except Exception as e:
                laatste_fout = e
                print(f"Poging {poging} mislukt: {e}")
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

            if gelukt:
                break
            if poging < pogingen:
                time.sleep(5)

    if not gelukt:
        raise RuntimeError(f"Alle {pogingen} pogingen mislukt: {laatste_fout}")

    output = {
        "bijgewerkt": dt.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bron": URL,
        "seizoen": seizoen,
        "bateo": bateo,
        "pitcheo": pitcheo,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(
        f"\n{JSON_FILE} geschreven: {len(bateo)} bateo-spelers, "
        f"{len(pitcheo)} pitcheo-spelers."
    )


if __name__ == "__main__":
    main()
