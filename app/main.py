from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pycountry
import requests
from bs4 import BeautifulSoup, Tag, NavigableString
import asyncio
import datetime
import json
import logging
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urljoin

URL = "https://en.wikipedia.org/wiki/List_of_current_world_boxing_champions"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_ENTITY_URL = "https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
FLAGCDN_TEMPLATE = "https://flagcdn.com/w40/{code}.png"
RING_FIGHTER_URL_RE = re.compile(r"^https?://(?:www\.)?ringmagazine\.com/fighters/[A-Za-z0-9-]+/?$")
RING_COUNTRY_CODE_RE = re.compile(r'"countryCode"\s*:\s*"([A-Za-z]{2})"')
RING_FIRST_LAST_RE = re.compile(r'"firstName"\s*:\s*"([^"]+)"\s*,\s*"lastName"\s*:\s*"([^"]+)"')
WIKI_BOXER_CATEGORY_RE = re.compile(
    r"^Category:(.+?)\s+(?:male\s+|female\s+)?boxers$", re.IGNORECASE
)
# Matches records like "34–2 (1) (27 KO)" or "27-0 (19 KO)" -> wins, losses, draws, KOs.
RECORD_RE = re.compile(
    r"(\d+)\s*[-–—]\s*(\d+)(?:\s*\((\d+)\))?\s*\((\d+)\s*KOs?\)", re.IGNORECASE
)
# Boxer-nationality demonym -> ISO 3166-1 alpha-2. Covers common current
# professional-boxing nationalities; unmatched demonyms fall through and the
# boxer remains flagless (add manual ringUrl override in that case).
DEMONYM_TO_ISO: dict[str, str] = {
    "american": "us", "british": "gb", "english": "gb", "scottish": "gb",
    "welsh": "gb", "northern irish": "gb", "irish": "ie",
    "mexican": "mx", "cuban": "cu", "japanese": "jp", "filipino": "ph",
    "australian": "au", "kazakhstani": "kz", "kazakh": "kz",
    "ukrainian": "ua", "russian": "ru", "german": "de", "polish": "pl",
    "nicaraguan": "ni", "puerto rican": "pr", "costa rican": "cr",
    "belgian": "be", "armenian": "am", "guatemalan": "gt",
    "venezuelan": "ve", "dominican": "do", "colombian": "co",
    "uzbek": "uz", "uzbekistani": "uz", "azerbaijani": "az",
    "south african": "za", "cameroonian": "cm", "canadian": "ca",
    "french": "fr", "spanish": "es", "italian": "it", "dutch": "nl",
    "brazilian": "br", "argentine": "ar", "argentinian": "ar",
    "chilean": "cl", "peruvian": "pe", "ecuadorian": "ec",
    "panamanian": "pa", "nigerian": "ng", "ghanaian": "gh",
    "kenyan": "ke", "moroccan": "ma", "turkish": "tr", "iranian": "ir",
    "israeli": "il", "chinese": "cn", "south korean": "kr", "korean": "kr",
    "thai": "th", "vietnamese": "vn", "indonesian": "id",
    "malaysian": "my", "indian": "in", "pakistani": "pk",
    "new zealand": "nz", "new zealander": "nz",
    "bulgarian": "bg", "romanian": "ro", "hungarian": "hu",
    "czech": "cz", "slovak": "sk", "croatian": "hr", "serbian": "rs",
    "bosnian": "ba", "slovenian": "si", "greek": "gr", "norwegian": "no",
    "swedish": "se", "danish": "dk", "finnish": "fi", "icelandic": "is",
    "portuguese": "pt", "swiss": "ch", "austrian": "at", "cypriot": "cy",
    "bolivian": "bo", "paraguayan": "py", "uruguayan": "uy",
    "haitian": "ht", "jamaican": "jm", "trinidadian": "tt",
    "salvadoran": "sv", "honduran": "hn",
    "belarusian": "by", "georgian": "ge", "moldovan": "md",
    "estonian": "ee", "latvian": "lv", "lithuanian": "lt",
    "kyrgyz": "kg", "tajik": "tj", "mongolian": "mn",
    "egyptian": "eg", "algerian": "dz", "tunisian": "tn",
    "ugandan": "ug", "tanzanian": "tz", "zimbabwean": "zw",
    "congolese": "cd", "senegalese": "sn",
}
NATIONALITIES_FILE = Path(os.getenv("NATIONALITIES_FILE", str(Path(__file__).with_name("nationalities.json"))))
# Seconds to wait between nationality lookups (polite to external services).
NATIONALITY_REQUEST_DELAY = float(os.getenv("NATIONALITY_REQUEST_DELAY", "1.5"))

# Wikimedia asks bots to send a distinct, contact-embedded UA per API etiquette.
# https://meta.wikimedia.org/wiki/User-Agent_policy
WMF_HEADERS = {
    "User-Agent": "boxing-champs/1.0 (https://boxing.costa365.site; +https://github.com/) python-requests",
    "Accept-Language": "en-US,en;q=0.9",
}

# In-memory cache: country Q-id -> ISO 3166-1 alpha-2 code (or None).
# Avoids re-fetching Q145 (UK) etc. once per boxer.
_country_iso_cache: dict[str, str | None] = {}

app = FastAPI(title="World Boxing Champions",
              description="Scrapes Wikipedia's 'List of current world boxing champions' and exposes champions per organization and weight class.",
              version="1.0")

# Config: how many days to consider a champion "new" (can be set via env NEW_FLAG_DAYS)
try:
    NEW_FLAG_DAYS = int(os.getenv("NEW_FLAG_DAYS", "14"))
    if NEW_FLAG_DAYS < 0:
        raise ValueError("must be non-negative")
except Exception:
    logging.warning("Invalid NEW_FLAG_DAYS env var %r; falling back to 14", os.getenv("NEW_FLAG_DAYS"))
    NEW_FLAG_DAYS = 14

# Mount static files and templates for the UI
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def country_name(iso_code: str | None) -> str:
    """Return the English name for an ISO 3166-1 alpha-2 code, or the code itself."""
    if not iso_code:
        return ""
    entry = pycountry.countries.get(alpha_2=iso_code.upper())
    if entry is None:
        return iso_code.upper()
    return getattr(entry, "common_name", None) or entry.name


templates.env.globals["country_name"] = country_name


def fetch_page():
    resp = requests.get(URL, headers=HEADERS)
    resp.raise_for_status()
    return resp.text


def _load_nationalities() -> dict:
    """Load the nationality cache from disk. Returns an empty dict on failure."""
    if not NATIONALITIES_FILE.exists():
        return {}
    try:
        return json.loads(NATIONALITIES_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        logging.exception("Failed to read nationalities cache %s", NATIONALITIES_FILE)
        return {}


def _save_nationalities(cache: dict) -> None:
    """Persist the nationality cache to disk atomically."""
    try:
        NATIONALITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = NATIONALITIES_FILE.with_suffix(NATIONALITIES_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        tmp.replace(NATIONALITIES_FILE)
    except Exception:
        logging.exception("Failed to write nationalities cache %s", NATIONALITIES_FILE)


def _title_from_wiki_url(wiki_url: str) -> str | None:
    """Extract the page title (decoded, with underscores) from a Wikipedia URL.

    Tolerant of malformed URLs like ``https://en.wikipedia.org//en.wikipedia.org/wiki/Foo``
    that older cache entries contain — grabs the last ``/wiki/...`` segment.
    """
    if not wiki_url:
        return None
    matches = re.findall(r"/wiki/([^?#]+)", wiki_url)
    if not matches:
        return None
    return unquote(matches[-1])


def _wmf_get(url: str, params: dict | None = None, timeout: int = 15) -> requests.Response:
    """GET a Wikimedia endpoint with the WMF-etiquette UA and a short 429 backoff."""
    for attempt in range(3):
        resp = requests.get(url, params=params, headers=WMF_HEADERS, timeout=timeout)
        if resp.status_code == 429 and attempt < 2:
            retry_after = int(resp.headers.get("Retry-After", "0")) or (2 * (attempt + 1))
            logging.info("WMF 429 on %s; sleeping %ss", url, retry_after)
            time.sleep(retry_after)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()
    return resp


def _wikidata_qid_for_title(title: str) -> str | None:
    """Resolve a Wikipedia page title to its Wikidata Q-id via the MediaWiki API."""
    params = {
        "action": "query",
        "prop": "pageprops",
        "ppprop": "wikibase_item",
        "titles": title,
        "format": "json",
        "redirects": 1,
    }
    resp = _wmf_get(WIKIPEDIA_API, params=params)
    pages = resp.json().get("query", {}).get("pages", {}) or {}
    for page in pages.values():
        qid = (page.get("pageprops") or {}).get("wikibase_item")
        if qid:
            return qid
    return None


def _wikidata_entity(qid: str) -> dict | None:
    resp = _wmf_get(WIKIDATA_ENTITY_URL.format(qid=qid))
    return (resp.json().get("entities") or {}).get(qid)


def _first_claim_value(entity: dict, prop: str):
    """Return the first claim value for `prop`. For item-typed claims returns the Q-id string."""
    for claim in (entity.get("claims") or {}).get(prop, []):
        mainsnak = claim.get("mainsnak") or {}
        if mainsnak.get("snaktype") != "value":
            continue
        value = (mainsnak.get("datavalue") or {}).get("value")
        if isinstance(value, dict) and value.get("id"):
            return value["id"]
        if isinstance(value, str):
            return value
    return None


def _iso_code_for_country(country_qid: str) -> str | None:
    if country_qid in _country_iso_cache:
        return _country_iso_cache[country_qid]
    try:
        entity = _wikidata_entity(country_qid)
    except Exception as e:
        logging.warning("Wikidata country lookup failed for %s: %s", country_qid, e)
        return None
    code = _first_claim_value(entity or {}, "P297") if entity else None
    code = code.lower() if isinstance(code, str) else None
    _country_iso_cache[country_qid] = code
    return code


def _norm_name(s: str) -> str:
    """Lowercase, strip accents, collapse to alphanumerics — for fuzzy name matching."""
    if not s:
        return ""
    decomposed = unicodedata.normalize("NFKD", s)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", stripped.lower())


def _ring_page_matches_name(html: str, expected_name: str) -> bool:
    """Check the Ring page's firstName/lastName roughly match our expected boxer name.

    Guards against DDG returning a wrong first hit for common names.
    """
    if not expected_name:
        return True  # nothing to check against — trust the URL
    expected = _norm_name(expected_name)
    for first, last in RING_FIRST_LAST_RE.findall(html):
        candidate = _norm_name(first + last)
        # Accept if either the candidate contains all of ours or vice versa
        # — handles middle names, "Jr." suffixes, hyphenated last names.
        if candidate and (expected in candidate or candidate in expected):
            return True
    return False


def fetch_nationality_from_ring(ring_url: str, expected_name: str | None = None) -> dict | None:
    """Extract nationality from a Ring Magazine fighter page.

    Ring embeds the fighter payload in the initial HTML (RSC stream) with a
    ``"countryCode":"XX"`` field. We grep it out and turn it into a flagcdn
    URL. When ``expected_name`` is provided, the page's ``firstName``/
    ``lastName`` are checked to guard against a wrong search hit. Returns
    None if the URL is malformed, the fetch fails, the name mismatches, or
    no country code is found.
    """
    if not ring_url or not RING_FIGHTER_URL_RE.match(ring_url):
        return None
    try:
        resp = requests.get(ring_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        logging.warning("Ring fetch failed for %s: %s", ring_url, e)
        return None
    if expected_name and not _ring_page_matches_name(resp.text, expected_name):
        logging.info("Ring page name mismatch for %s (%s) — skipping", expected_name, ring_url)
        return None
    match = RING_COUNTRY_CODE_RE.search(resp.text)
    if not match:
        return None
    code = match.group(1).lower()
    return {"country": code, "flagUrl": FLAGCDN_TEMPLATE.format(code=code), "source": "ring", "ringUrl": ring_url}


def fetch_nationality_from_wiki_categories(wiki_url: str | None) -> dict | None:
    """Infer nationality from a Wikipedia article's categories.

    Every boxer article on English Wikipedia is filed under a category like
    ``Category:English male boxers`` or ``Category:Cuban male boxers``. We
    fetch the article's categories, match ``<Demonym> (male|female)? boxers``,
    then look the demonym up in :data:`DEMONYM_TO_ISO`. Returns None if the
    article has no such category or the demonym isn't in the table.
    """
    title = _title_from_wiki_url(wiki_url) if wiki_url else None
    if not title:
        return None
    try:
        resp = _wmf_get(
            WIKIPEDIA_API,
            params={
                "action": "query",
                "prop": "categories",
                "clshow": "!hidden",
                "cllimit": "max",
                "titles": title,
                "format": "json",
                "redirects": 1,
            },
        )
    except Exception as e:
        logging.warning("Wiki categories fetch failed for %s: %s", title, e)
        return None
    pages = (resp.json().get("query") or {}).get("pages") or {}
    for page in pages.values():
        for cat in page.get("categories") or []:
            m = WIKI_BOXER_CATEGORY_RE.match(cat.get("title", ""))
            if not m:
                continue
            demonym = m.group(1).strip().lower()
            iso = DEMONYM_TO_ISO.get(demonym)
            if iso:
                return {
                    "country": iso,
                    "flagUrl": FLAGCDN_TEMPLATE.format(code=iso),
                    "source": f"wiki-category:{demonym}",
                }
    return None


def fetch_nationality(name: str, wiki_url: str | None) -> dict | None:
    """Look up a boxer's nationality via Wikidata and return {country, flagUrl} or None.

    Steps: Wikipedia page -> Wikidata Q-id (pageprops.wikibase_item) -> P27
    (country of citizenship, first claim) -> P297 (ISO 3166-1 alpha-2) on that
    country. Returns None when any step has no data.
    """
    title = _title_from_wiki_url(wiki_url) if wiki_url else None
    if not title:
        return None
    try:
        qid = _wikidata_qid_for_title(title)
        if not qid:
            return None
        entity = _wikidata_entity(qid)
        if not entity:
            return None
        country_qid = _first_claim_value(entity, "P27")
        if not country_qid:
            return None
        iso_code = _iso_code_for_country(country_qid)
        if not iso_code:
            return None
        return {"country": iso_code, "flagUrl": FLAGCDN_TEMPLATE.format(code=iso_code)}
    except Exception as e:
        logging.warning("Wikidata nationality lookup failed for %s: %s", name, e)
        return None


def parse_champions(html: str):
    soup = BeautifulSoup(html, "html.parser")

    results = []

    tables = soup.find_all("table", class_="wikitable")

    for table in tables:
        title_tag = table.find_previous(["h2", "h3"])
        weight_class = title_tag.get_text().replace("[edit]", "").strip()

        division, weight = weight_class.split(" (")
        weight = weight.rstrip(")") 

        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        rows = []
        orgs = []
        more_champs = []
        os = table.find_all("tr")[0]
        for org in os.find_all("td"):
            orgs.append(org.get_text(strip=True).lower())

        champs = {}

        # Track pending rowspans per organization column. Each entry is either
        # None or a dict: {"remaining": int, "champ": dict}
        pending_rowspans = [None] * len(orgs)

        for row in table.find_all("tr")[1:]:
            cells_iter = iter(row.find_all(["td", "th"]))

            for col_idx, organization in enumerate(orgs):
                # If there's a pending rowspan for this column, skip consuming a
                # cell on this row. We already recorded the champ when we saw
                # the original cell; don't re-append it on subsequent rows.
                if pending_rowspans[col_idx]:
                    pending_rowspans[col_idx]["remaining"] -= 1
                    if pending_rowspans[col_idx]["remaining"] <= 0:
                        pending_rowspans[col_idx] = None
                    continue

                # Otherwise consume the next cell (if any)
                cell = next(cells_iter, None)
                if cell is None:
                    # No cell present and no pending rowspan -> Vacant
                    champs.setdefault(organization, []).append({
                        "name": None,
                        "record": None,
                        "title": "Vacant",
                        "date": None,
                        "wikiUrl": None,
                    })
                    continue

                rowspan = cell.get("rowspan")

                a = cell.find("a")
                if not a:
                        vacant = {
                            "name": None,
                            "record": None,
                            "title": "Vacant",
                            "date": None,
                            "wikiUrl": None,
                        }
                        champs.setdefault(organization, []).append(vacant)
                        # If this cell spans multiple rows, carry the vacant forward
                        try:
                            span = int(rowspan) if rowspan is not None else 1
                        except Exception:
                            span = 1
                        if span > 1:
                            pending_rowspans[col_idx] = {"remaining": span - 1, "champ": vacant.copy()}
                        continue

                href = a.get("href")
                name = a.get_text(strip=True)
                record = a.find_next_sibling(string=True)

                texts = cell.get_text(separator="\n", strip=True).split("\n")

                title = texts[1] if len(texts) > 1 else None
                if title == record:
                    title = None

                date = texts[2] if len(texts) > 2 else None
                if len(texts) > 3:
                    date = texts[3]

                champ = {
                    "name": name,
                    "record": record,
                    "recordDisplay": format_record(record),
                    "recordParts": parse_record(record),
                    "date": date,
                    "recent": False,
                    "wikiUrl": urljoin("https://en.wikipedia.org/", href),
                }

                try:
                    parsed = _try_parse_date(date)
                    if parsed:
                        today = datetime.datetime.now(datetime.timezone.utc).date()
                        delta = (today - parsed).days
                        if 0 <= delta <= NEW_FLAG_DAYS:
                            champ["recent"] = True
                except Exception:
                    pass

                if title:
                    champ["type"] = title.replace(" champion", "")

                # If the cell spans multiple rows, remember its champ for next rows
                try:
                    span = int(rowspan) if rowspan is not None else 1
                except Exception:
                    span = 1

                if span > 1:
                    pending_rowspans[col_idx] = {"remaining": span - 1, "champ": champ.copy()}

                champs.setdefault(organization, []).append(champ)

        results.append ({
            "name": division,
            "weight": weight,
            **champs
        })
    return results


def _iter_champions(data):
    """Yield (champ_dict, key) for every named champion in the parsed data."""
    for division in data:
        for org_key, champs in division.items():
            if org_key in ("name", "weight"):
                continue
            for champ in champs:
                name = champ.get("name")
                if not name:
                    continue
                key = champ.get("wikiUrl") or name
                yield champ, key


def _apply_cached_flags(data, cache: dict) -> None:
    """Populate champ['flagUrl'] and champ['country'] from the cache where available."""
    for champ, key in _iter_champions(data):
        entry = cache.get(key)
        if entry and entry.get("flagUrl"):
            champ["flagUrl"] = entry["flagUrl"]
            if entry.get("country"):
                champ["country"] = entry["country"]


def parse_record(record: str | None) -> dict | None:
    """Turn a raw Wikipedia record like '34–2 (1) (27 KO)' into
    {"wins": "34", "losses": "2", "draws": "1", "kos": "27"} for structured
    rendering. Returns None if it doesn't match the expected "W-L (D) (N KO)"
    shape, so callers can fall back to the raw string instead of losing it.
    """
    if not record:
        return None
    m = RECORD_RE.search(record)
    if not m:
        return None
    wins, losses, draws, kos = m.groups()
    return {"wins": wins, "losses": losses, "draws": draws, "kos": kos}


def format_record(record: str | None) -> str | None:
    """Plain-text fallback rendering of a record, e.g. 'W:34 L:2 D:1 KO:27'."""
    parts_dict = parse_record(record)
    if not parts_dict:
        return record.strip() if record else None
    parts = [f"W:{parts_dict['wins']}", f"L:{parts_dict['losses']}"]
    if parts_dict["draws"]:
        parts.append(f"D:{parts_dict['draws']}")
    parts.append(f"KO:{parts_dict['kos']}")
    return " ".join(parts)


def _try_parse_date(date_str: str):
    """Try to parse a variety of common date formats into a datetime.date.

    Returns a date or None if parsing fails.
    """
    if not date_str:
        return None

    # Trim parenthetical annotations and whitespace, and remove ordinals.
    s = date_str.split("(")[0].strip()
    import re
    s = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s)

    try:
        # Expect format like: "December 6, 2025"
        return datetime.datetime.strptime(s, "%B %d, %Y").date()
    except Exception:
        return None


@app.get("/")
async def root(request: Request):
    """Render the web UI showing champions by weight division using cached data."""
    data = getattr(app.state, "champions_data", None)
    if not data:
        # Data not yet fetched
        raise HTTPException(status_code=503, detail="Champion data not ready. Try again shortly.")
    return templates.TemplateResponse(request, "index.html", {"divisions": data})

@app.get("/champions")
async def champions():
    data = getattr(app.state, "champions_data", None)
    if data is None:
        raise HTTPException(status_code=503, detail="Champion data not ready. Try again shortly.")
    return JSONResponse(content=data)


async def _refresh_loop(interval_seconds: int = 8 * 3600):
    """Background loop that refreshes the cached champions every `interval_seconds`.

    Uses `asyncio.to_thread` to run blocking network and parse work in a threadpool.
    """
    while True:
        try:
            html = await asyncio.to_thread(fetch_page)
            data = await asyncio.to_thread(parse_champions, html)
            _apply_cached_flags(data, app.state.nationalities)
            app.state.champions_data = data
            app.state.last_updated = datetime.datetime.utcnow()
            logging.info("Champions cache refreshed at %s", app.state.last_updated)
        except Exception:
            logging.exception("Failed to refresh champions cache")
        await asyncio.sleep(interval_seconds)


async def _enrich_nationalities_loop(poll_seconds: int = 6 * 3600):
    """Background loop that fills in missing nationality flags via BoxRec.

    Runs continuously. After each pass over all current champions it sleeps
    for `poll_seconds` before checking again (so newly crowned champions
    discovered on the next Wikipedia refresh get picked up). Cache misses are
    recorded as `{flagUrl: None}` to avoid retrying endlessly.
    """
    while True:
        data = getattr(app.state, "champions_data", None)
        cache = app.state.nationalities
        if data:
            dirty = False
            for champ, key in _iter_champions(data):
                existing = cache.get(key) or {}
                # Already resolved (from Wikidata, Ring, or manually) -> reuse.
                if existing.get("flagUrl"):
                    champ["flagUrl"] = existing["flagUrl"]
                    if existing.get("country"):
                        champ["country"] = existing["country"]
                    continue

                name = champ.get("name")
                wiki_url = champ.get("wikiUrl")
                manual_ring_url = existing.get("ringUrl")

                result = None
                # 1) Manual ringUrl override (user pinned a specific Ring page).
                if manual_ring_url:
                    try:
                        result = await asyncio.to_thread(
                            fetch_nationality_from_ring, manual_ring_url, None
                        )
                    except Exception:
                        logging.exception("Manual Ring lookup failed for %s", name)
                # 2) Wikidata (fast, structured).
                if not result and wiki_url:
                    try:
                        result = await asyncio.to_thread(fetch_nationality, name, wiki_url)
                    except Exception:
                        logging.exception("Wikidata lookup failed for %s", name)
                # 3) Wikipedia categories fallback (e.g. "English male boxers" -> gb).
                if not result and wiki_url:
                    try:
                        result = await asyncio.to_thread(
                            fetch_nationality_from_wiki_categories, wiki_url
                        )
                    except Exception:
                        logging.exception("Wiki-category lookup failed for %s", name)

                # Preserve any manual override; also persist an auto-discovered ringUrl.
                new_entry = {"name": name}
                if manual_ring_url:
                    new_entry["ringUrl"] = manual_ring_url
                new_entry.update(result or {"flagUrl": None})
                cache[key] = new_entry

                if result and result.get("flagUrl"):
                    champ["flagUrl"] = result["flagUrl"]
                    if result.get("country"):
                        champ["country"] = result["country"]
                    logging.info(
                        "Resolved nationality for %s -> %s (via %s)",
                        name, result.get("country"), result.get("source", "wikidata"),
                    )
                else:
                    logging.info("No nationality found for %s", name)
                dirty = True
                await asyncio.sleep(NATIONALITY_REQUEST_DELAY)
            if dirty:
                await asyncio.to_thread(_save_nationalities, cache)
        await asyncio.sleep(poll_seconds)


@app.on_event("startup")
async def _startup_fetch_and_schedule():
    """Fetch immediately on startup and start the periodic refresh task."""
    # initialize state
    app.state.champions_data = None
    app.state.last_updated = None
    app.state.nationalities = await asyncio.to_thread(_load_nationalities)

    # initial fetch (run in thread)
    try:
        html = await asyncio.to_thread(fetch_page)
        data = await asyncio.to_thread(parse_champions, html)
        _apply_cached_flags(data, app.state.nationalities)
        app.state.champions_data = data
        app.state.last_updated = datetime.datetime.utcnow()
        logging.info("Initial champions cache populated at %s", app.state.last_updated)
    except Exception:
        logging.exception("Initial champions fetch failed; cache remains empty")

    # start background refresh tasks
    asyncio.create_task(_refresh_loop())
    asyncio.create_task(_enrich_nationalities_loop())
