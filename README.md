# World Boxing Champions

Minimal FastAPI project that scrapes Wikipedia's "List of current world boxing champions" and exposes a small web UI showing current champions by weight division.

Available at [https://boxing.costa365.site](https://boxing.costa365.site).


## Quick start (docker compose)


```bash
docker compose up --build
```

Then open `http://127.0.0.1:8022/` in your browser.


### Notes
- The app scrapes live data from Wikipedia and therefore requires outbound internet access from the host or container.
- The `templates/` and `static/` directories are included in the Docker image so the UI is served by the FastAPI app.
- Wikipedia page structure can change; if the parser fails, the HTML structure likely changed and parser needs tweaks.

## Configuration

- NEW_FLAG_DAYS: (optional) Number of days to mark a champion as "new" next to their `Since:` date. Defaults to `14`. You can set it in both development and production docker-compose files (`docker-compose.yml` and `docker-compose-prod.yml`) under the `environment:` section for the `wboxing_api` service.
- NATIONALITIES_FILE: (optional) Path to the JSON file used to cache each champion's nationality flag. Defaults to `app/nationalities.json`. The cache is populated lazily by a background task keyed by Wikipedia URL. Lookup order per boxer:
  1. **Manual `ringUrl` override** — if the entry has a `ringUrl` field set by hand, fetch that [ringmagazine.com](https://www.ringmagazine.com) fighter page and read the embedded `countryCode`.
  2. **Wikidata** — resolve the Wikipedia page to a Wikidata entity, read `P27` (country of citizenship) and that country's `P297` (ISO 3166-1 alpha-2 code).
  3. **Wikipedia categories** — read the Wikipedia article's categories, match `Category:<Demonym> (male|female)? boxers` (e.g. *English male boxers*, *Cuban male boxers*), and map the demonym to an ISO code via a curated table in `main.py`. Handles boxers whose Wikidata entry has no P27 set.

  Flag images are served by [flagcdn.com](https://flagcdn.com). Cache misses are stored as `flagUrl: null` and re-tried on every enrichment pass. To pin a specific Ring page for a boxer, add `ringUrl` to their entry:
  ```json
  "https://en.wikipedia.org/wiki/Hamzah_Sheeraz": {
    "name": "Hamzah Sheeraz",
    "flagUrl": null,
    "ringUrl": "https://www.ringmagazine.com/fighters/hamzah-sheeraz-abc123"
  }
  ```
- NATIONALITY_REQUEST_DELAY: (optional) Seconds to wait between nationality lookups. Defaults to `0.5`.
