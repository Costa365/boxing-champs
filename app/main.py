from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
from bs4 import BeautifulSoup, Tag, NavigableString
import asyncio
import datetime
import logging

URL = "https://en.wikipedia.org/wiki/List_of_current_world_boxing_champions"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

app = FastAPI(title="World Boxing Champions",
              description="Scrapes Wikipedia's 'List of current world boxing champions' and exposes champions per organization and weight class.",
              version="1.0")

# Mount static files and templates for the UI
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

def fetch_page():
    resp = requests.get(URL, headers=HEADERS)
    resp.raise_for_status()
    return resp.text


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

        org_count = 0
        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])

            for cell in cells:
                if org_count >= len(orgs):
                    organization = more_champs[org_count - len(orgs)]
                else:
                    organization = orgs[org_count]
                rowspan = cell.get("rowspan")
                if rowspan == None:
                    more_champs.append(organization)

                a = cell.find("a")
                if not a:
                    org_count += 1
                    champs.setdefault(organization, []).append({
                        "name": None,
                        "record": None,
                        "title": "Vacant",
                        "date": None,
                        "wikiUrl": None,
                    })
                    continue
                href = a.get("href")

                name = a.get_text(strip=True)            
                record = a.find_next_sibling(string=True)                

                texts = cell.get_text(separator="\n", strip=True).split("\n")

                title = texts[1]
                if title == record:
                    title = None

                date = texts[2]
                if(len(texts)>3):
                    date = texts[3]

                champ = {
                    "name": name,
                    "record": record,
                    "date": date,
                    "wikiUrl": "https://en.wikipedia.org" + href,
                }

                if title:
                    champ["type"] = title.replace(" champion", "")

                champs.setdefault(organization, []).append(champ)
                
                org_count += 1

        results.append ({
            "name": division,
            "weight": weight,
            **champs
        })
    return results

@app.get("/")
async def root(request: Request):
    """Render the web UI showing champions by weight division using cached data."""
    data = getattr(app.state, "champions_data", None)
    if not data:
        # Data not yet fetched
        raise HTTPException(status_code=503, detail="Champion data not ready. Try again shortly.")
    return templates.TemplateResponse("index.html", {"request": request, "divisions": data})

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
            app.state.champions_data = data
            app.state.last_updated = datetime.datetime.utcnow()
            logging.info("Champions cache refreshed at %s", app.state.last_updated)
        except Exception:
            logging.exception("Failed to refresh champions cache")
        await asyncio.sleep(interval_seconds)


@app.on_event("startup")
async def _startup_fetch_and_schedule():
    """Fetch immediately on startup and start the periodic refresh task."""
    # initialize state
    app.state.champions_data = None
    app.state.last_updated = None

    # initial fetch (run in thread)
    try:
        html = await asyncio.to_thread(fetch_page)
        data = await asyncio.to_thread(parse_champions, html)
        app.state.champions_data = data
        app.state.last_updated = datetime.datetime.utcnow()
        logging.info("Initial champions cache populated at %s", app.state.last_updated)
    except Exception:
        logging.exception("Initial champions fetch failed; cache remains empty")

    # start background refresh task
    asyncio.create_task(_refresh_loop())
