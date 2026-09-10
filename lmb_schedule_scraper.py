"""
LMB Scraper — uitslagen + programma in één run, één JSON.
Bron: https://lmb.com.mx/juegos (Liga Mexicana de Beisbol)

BELANGRIJK — waarom dit géén nette JSON-API-scraper is zoals de MLB-
referentie: lmb.com.mx laadt de wedstrijdgegevens zelf via een eigen
endpoint (/juegos/api/calendar), maar de robots.txt van de site verbiedt
crawlers expliciet om dat endpoint (en /posiciones/api, /videos/api,
/estadisticas/api) rechtstreeks te benaderen:

    User-agent: *
    Disallow: /juegos/api
    ...
    Allow: /

De gewone pagina (/juegos) zelf mag wél gecrawld worden. Daarom laadt deze
scraper, in plaats van dat endpoint zelf aan te roepen, de normale pagina
in een echte (headless) browser via Playwright, en leest de daadwerkelijk
gerenderde HTML uit — precies zoals een bezoeker de pagina te zien krijgt.
De browser roept intern natuurlijk wél hetzelfde endpoint aan (net als bij
elke bezoeker), maar dat initiatief komt van de pagina zelf, niet van ons
als crawler die de URL zelf construeert.

Gevolg van deze aanpak (t.o.v. de MLB-referentie):
- Geen team-afkortingen: de gerenderde kaart toont wel volledige
  teamnamen en logo's, maar 3-letter-codes (zoals "TIJ"/"TAB") staan
  alleen terloops bij de werper-info, niet op een manier die betrouwbaar
  aan thuis/uit te koppelen is. Die code laten we daarom weg i.p.v. te
  gokken.
- Geen aftraptijd bij afgeronde wedstrijden: zodra een wedstrijd "Final"
  is, vervangt de site de kloktijd door die tekst, dus dat tijdstip is
  voor gespeelde wedstrijden niet meer uit de pagina te halen.
- Winnende/verliezende/reddende werper (bij gespeelde wedstrijden) en
  verwachte werpers (bij nog te spelen wedstrijden) lezen we uit via hun
  "W:"/"L:"/"S:"/"P:"-voorvoegsel, niet via hun positie in de kaart — die
  positie betekent bij een afgeronde wedstrijd namelijk win/verlies-
  volgorde, en bij een nog te spelen wedstrijd uit/thuis-volgorde. Twee
  verschillende dingen die toevallig hetzelfde HTML-blokje gebruiken.

"Vandaag" en "gisteren" worden bepaald in de tijdzone van Mexico-Stad
(net zoals de MLB-referentie Eastern Time gebruikt voor de Amerikaanse
baseball-dag) — de Playwright-browsercontext krijgt diezelfde tijdzone
mee, zodat de site zelf ook automatisch "vandaag" volgens Mexicaanse tijd
laat zien.
"""
import json
import re
import time
import datetime as dt
from datetime import timezone
from urllib.parse import unquote
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

URL = "https://lmb.com.mx/juegos"
JSON_FILE = "lmb_schedule.json"
MX_TZ = "America/Mexico_City"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def maak_absoluut(src):
    """
    Maakt van een (eventueel relatieve, of door Next.js image-optimalisatie
    ingepakte) src-URL een rechtstreekse, absolute URL.
    """
    if not src:
        return None
    match = re.search(r"[?&]url=([^&]+)", src)
    if match:
        return unquote(match.group(1))
    if src.startswith("http"):
        return src
    return "https://lmb.com.mx" + src


def klik_cookiebanner(page):
    """
    Sluit de cookie-consent-banner (Silktide). We proberen eerst netjes op
    "Aceptar todas" te klikken, maar de consent-manager laadt zijn script
    asynchroon in — soms staat de knop er nog niet als we hier langskomen,
    en soms blijft de onzichtbare "#silktide-backdrop" ook ná een succesvolle
    klik nog "pointer-events" onderscheppen. Om te voorkomen dat die
    backdrop straks de datum-tab-klik in selecteer_dag() blokkeert
    (precies de "subtree intercepts pointer events"-fout die de site liet
    zien), verwijderen we de hele consent-wrapper hoe dan ook uit de DOM,
    ongeacht of de klik zelf lukte.
    """
    try:
        knop = page.locator("#silktide-wrapper button", has_text=re.compile("aceptar", re.I))
        knop.first.click(timeout=8000)
    except Exception:
        pass
    finally:
        verwijder_cookiebanner(page)


def verwijder_cookiebanner(page):
    """Haalt de consent-wrapper (banner + backdrop + cookie-icoon) hard weg."""
    try:
        page.evaluate("document.getElementById('silktide-wrapper')?.remove()")
    except Exception:
        pass


def wacht_op_inhoud(page, timeout_ms=90000, stabiel_ms=20000):
    """
    Wacht tot de dag-inhoud klaar is: ofwel er verschijnt minstens één
    wedstrijdkaart, ofwel de "geen wedstrijden"-melding.

    lmb.com.mx blijkt in de praktijk niet altijd snel of betrouwbaar: de
    pagina blijft soms een halve minuut of langer op de laad-spinner
    hangen (in ieder geval ooit gezien met een 504 van de eigen
    afbeeldingen-CDN erbij, dus dit lijkt een backend-probleem bij hen,
    geen fout in onze code) vóórdat de eerste fetch zelfs maar aanslaat.
    Belangrijker nog: we hebben gezien dat "geen wedstrijden" soms een
    KORTSTONDIGE, onjuiste tussenstand is, die de pagina zelf later
    corrigeert zodra haar eigen periodieke herhaalpoging alsnog de echte
    wedstrijd(en) ophaalt. Als we die eerste lege stand meteen voor waar
    aannemen, scrapen we een fout-negatief resultaat (lege JSON terwijl
    er wél wedstrijden waren — precies wat er gebeurde).

    Daarom: een wedstrijdkaart is meteen genoeg (positief resultaat kan
    niet fout-positief zijn). Maar "geen wedstrijden" accepteren we pas
    als die stand minstens `stabiel_ms` achtereen ONVERANDERD blijft.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    leeg_sinds = None
    while True:
        status = page.evaluate(
            """() => {
                const el = document.querySelector('div.content');
                if (!el) return 'geen-content-div';
                if (el.querySelector('[class*="Game_gameContainer"]')) return 'wedstrijd';
                if (/no hay juegos/i.test(el.innerText || '')) return 'leeg';
                return 'laden';
            }"""
        )
        if status == "wedstrijd":
            return
        if status == "leeg":
            nu = time.monotonic()
            if leeg_sinds is None:
                leeg_sinds = nu
            elif nu - leeg_sinds >= stabiel_ms / 1000:
                return
        else:
            leeg_sinds = None
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Pagina liet na {timeout_ms}ms geen stabiele inhoud zien "
                f"(laatste status: {status})."
            )
        time.sleep(1)


def parse_team_namen_en_logos(kaart):
    """
    Retourneert (uit_naam, uit_logo, thuis_naam, thuis_logo). De volgorde
    "uit eerst, thuis daarna" is consistent bevestigd via de h3-titel
    ("X vs Y"), de teamlogo's, en de teamnaam/record-pars op de kaart.
    """
    namen = kaart.locator('[class*="Game_teamName__"]').all_inner_texts()
    logos = kaart.locator('[class*="Game_teamLogos__"] img')
    logo_urls = [maak_absoluut(logos.nth(i).get_attribute("src")) for i in range(logos.count())]
    uit_naam = namen[0].strip() if len(namen) > 0 else "-"
    thuis_naam = namen[1].strip() if len(namen) > 1 else "-"
    uit_logo = logo_urls[0] if len(logo_urls) > 0 else None
    thuis_logo = logo_urls[1] if len(logo_urls) > 1 else None
    return uit_naam, uit_logo, thuis_naam, thuis_logo


def parse_score(kaart):
    """
    Retourneert (score_uit, score_thuis), of (None, None) als er nog geen
    boxscore is (wedstrijd nog niet gespeeld — de cel bevat dan "_").
    """
    uit_rij = kaart.locator('[class*="Scores_awayTeam__"]')
    thuis_rij = kaart.locator('[class*="Scores_localTeam__"]')
    if uit_rij.count() == 0 or thuis_rij.count() == 0:
        return None, None

    def eerste_cel(rij):
        cellen = rij.locator("td")
        if cellen.count() == 0:
            return None
        tekst = cellen.first.inner_text().strip()
        return int(tekst) if tekst.isdigit() else None

    return eerste_cel(uit_rij), eerste_cel(thuis_rij)


def parse_status(kaart):
    """
    Retourneert (status_tekst, gespeeld, live). De statusregel toont
    "Final" als de wedstrijd afgelopen is, een kloktijd (bv. "19:00")
    als hij nog moet beginnen; alles daartussenin (een inning-aanduiding,
    "Suspendido", ...) beschouwen we als live/onderbroken.
    """
    loc = kaart.locator('[class*="Game_headerStatusContainer__"]')
    tekst = loc.inner_text().strip() if loc.count() else ""
    gespeeld = tekst.lower() == "final"
    is_kloktijd = bool(re.match(r"^\d{1,2}:\d{2}$", tekst))
    live = not gespeeld and not is_kloktijd and tekst != ""
    return tekst, gespeeld, live


def parse_werpers(kaart):
    """
    Werper-info bestaat uit 2 (of 3 bij een save) blokjes met een
    label-voorvoegsel: "W: Naam" (winnend), "L: Naam" (verliezend),
    "S: Naam" (save) bij afgeronde wedstrijden, of "P: Naam" (probable/
    verwacht) x2 bij nog te spelen wedstrijden. We lezen dat voorvoegsel
    uit i.p.v. de positie in de kaart (zie module-docstring).
    """
    resultaat = {
        "winnende_werper": None,
        "verliezende_werper": None,
        "gered_door": None,
        "werper_uit": None,
        "werper_thuis": None,
    }
    blokken = kaart.locator('[class*="Pitcher_pitcher__"]')
    verwacht = []
    for i in range(blokken.count()):
        blok = blokken.nth(i)
        naam_loc = blok.locator('[class*="Pitcher_pitcherName__"]')
        if naam_loc.count() == 0:
            continue
        naam_tekst = naam_loc.inner_text().strip()
        stats_loc = blok.locator('[class*="Pitcher_pitcherStats__"]')
        stats_tekst = stats_loc.inner_text().strip() if stats_loc.count() else ""
        match = re.match(r"^([A-Z]):\s*(.+)$", naam_tekst)
        if not match:
            continue
        label, naam = match.group(1), match.group(2).strip()
        item = {"naam": naam, "stats": stats_tekst or None}
        if label == "W":
            resultaat["winnende_werper"] = item
        elif label == "L":
            resultaat["verliezende_werper"] = item
        elif label == "S":
            resultaat["gered_door"] = item
        elif label == "P":
            verwacht.append(item)
    if len(verwacht) > 0:
        resultaat["werper_uit"] = verwacht[0]
    if len(verwacht) > 1:
        resultaat["werper_thuis"] = verwacht[1]
    return resultaat


def parse_gameid(kaart):
    link = kaart.locator('a[href^="/juegos/"]')
    if link.count() == 0:
        return None
    href = link.first.get_attribute("href") or ""
    match = re.search(r"/juegos/(\d+)", href)
    return int(match.group(1)) if match else None


def parse_kaarten(page, datum_str):
    kaarten = page.locator('[class*="Game_gameContainer__"]')
    resultaten = []
    for i in range(kaarten.count()):
        kaart = kaarten.nth(i)
        uit_naam, uit_logo, thuis_naam, thuis_logo = parse_team_namen_en_logos(kaart)
        score_uit, score_thuis = parse_score(kaart)
        status_tekst, gespeeld, live = parse_status(kaart)
        werpers = parse_werpers(kaart)
        resultaten.append({
            "datum": datum_str,
            "game_id": parse_gameid(kaart),
            "uit": uit_naam,
            "uit_logo": uit_logo,
            "thuis": thuis_naam,
            "thuis_logo": thuis_logo,
            "score_uit": score_uit,
            "score_thuis": score_thuis,
            "status": status_tekst,
            "gespeeld": gespeeld,
            "live": live,
            **werpers,
        })
    return resultaten


def selecteer_dag(page, richting):
    """
    richting: -1 = ga naar de vorige dag t.o.v. de huidige selectie,
    +1 = volgende dag. Klikt op de datumtab links/rechts van de huidige
    selectie, of gebruikt de pijl als die tab niet in het huidige venster
    van dagen zichtbaar is.
    """
    verwijder_cookiebanner(page)
    datums = page.locator('[class*="DateNavigation_date__"]')
    geselecteerd_idx = None
    for i in range(datums.count()):
        klas = datums.nth(i).get_attribute("class") or ""
        if "selected" in klas:
            geselecteerd_idx = i
            break
    doel_idx = (geselecteerd_idx if geselecteerd_idx is not None else 0) + richting
    if geselecteerd_idx is not None and 0 <= doel_idx < datums.count():
        datums.nth(doel_idx).click()
    else:
        pijlen = page.locator('[class*="DateNavigation_arrow__"]')
        pijl = pijlen.first if richting < 0 else pijlen.last
        pijl.click()
    wacht_op_inhoud(page)


def main():
    mx_nu = dt.datetime.now(ZoneInfo(MX_TZ))
    vandaag = mx_nu.date()
    gisteren = vandaag - dt.timedelta(days=1)
    print(f"Nu (Mexico-Stad): {mx_nu.strftime('%Y-%m-%d %H:%M')} — vandaag={vandaag}, gisteren={gisteren}")

    pogingen = 3
    laatste_fout = None
    for poging in range(1, pogingen + 1):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    timezone_id=MX_TZ,
                    locale="es-MX",
                    user_agent=USER_AGENT,
                )
                page = context.new_page()
                print(f"Pagina laden (poging {poging}/{pogingen}): {URL}")
                page.goto(URL, wait_until="domcontentloaded", timeout=60000)
                klik_cookiebanner(page)
                wacht_op_inhoud(page)

                print("Programma van vandaag lezen...")
                programma = parse_kaarten(page, str(vandaag))
                print(f"→ {len(programma)} wedstrijd(en) vandaag")

                print("Naar gisteren navigeren...")
                selecteer_dag(page, -1)
                uitslagen_ruw = parse_kaarten(page, str(gisteren))
                uitslagen = [w for w in uitslagen_ruw if w["gespeeld"]]
                print(f"→ {len(uitslagen)} uitslag(en) gisteren")

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
        "uitslagen": uitslagen,
        "programma": programma,
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n{JSON_FILE}: {len(uitslagen)} uitslagen, {len(programma)} programma")


if __name__ == "__main__":
    main()
