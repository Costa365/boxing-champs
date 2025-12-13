# World Boxing Champions

Minimal FastAPI project that scrapes Wikipedia's "List of current world boxing champions" and exposes a small web UI showing current champions by weight division.

Available at [https://notes365.costa365.site](https://boxing.costa365.site).


## Quick start (docker compose)


```bash
docker compose up --build
```

Then open `http://127.0.0.1:8022/` in your browser.


### Notes
- The app scrapes live data from Wikipedia and therefore requires outbound internet access from the host or container.
- The `templates/` and `static/` directories are included in the Docker image so the UI is served by the FastAPI app.
- Wikipedia page structure can change; if the parser fails, the HTML structure likely changed and parser needs tweaks.
