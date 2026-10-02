---
title: VORA
emoji: 🌊
colorFrom: purple
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
short_description: Vast Online Retrieval & Assurance (API)
---

# VORA backend — Vast Online Retrieval & Assurance

FastAPI service that turns a one-sentence request into a checked dataset: it plans the request, finds and verifies
official sources, learns how each site lists its records, reads them with a headless Chromium, scores and
de-duplicates the rows, and keeps tracking them live. The web app lives in `../Frontend`.

## Layout (`vora/`)

Dependencies point one way: `api` → `research` → (`learning`, `extraction`, `output`, `storage`) → `browser` → `shared`.

| Folder | What belongs there |
|---|---|
| `settings.py` | The one settings object, read from environment variables / `.env` |
| `api/` | HTTP only: routes (`application.py`), sign-in (`auth.py`), cache headers |
| `research/` | One run, start to finish: `coordinator.py`; `planning/` (planner, official-site resolver); `discovery/` (search via SearXNG, ranking, reachability + universal blacklist); `reading/` (browser pool, crawl, interactive pass, **shared source reads**) |
| `learning/` | How a site is learned: page structure, data services, accessibility signals, recipes, source registry |
| `browser/` | Headless Chromium runtime: engine, network guard, events, exploration, forms |
| `extraction/` | Rendered pages and files → scored observations |
| `storage/` | SQLite (`repository.py`): tracks, runs, rows (one per line), shared reads, query cache |
| `output/` | Datasets, graphs, tables, the live stream (WebSocket + SSE) |
| `shared/` | Contracts, URL safety, caches |

Outside the package: `app.py` (starts the server), `deploy/` (container start, backup, SearXNG settings),
`data/` (exported registry and blacklist), `tests/`, `docs/`.

## Run locally

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
copy .env.example .env          # then fill it in (table below)
docker compose up -d            # SearXNG on 127.0.0.1:8080
python app.py                   # http://127.0.0.1:8000  (API docs at /docs)
```

The browser: set `VORA_BROWSER_BINARY` to a Chrome/Chromium executable (or install one with
`install-browser.ps1`). In Docker the bundled Playwright Chromium is used automatically.

Run the production image instead: `docker compose --profile app up -d --build` → http://127.0.0.1:7860.

## Configuration (environment variables)

| Variable | Needed | Meaning |
|---|---|---|
| `VORA_BROWSER_BINARY` | local only | Path to Chrome/Chromium (set automatically in Docker) |
| `GROQ_API_KEY`, `GEMINI_API_KEY`, `NVIDIA_API_KEY` | at least one | Language models for planning and finding official sites |
| `VORA_AUTH` | yes | `supabase` in production, `none` for local testing |
| `SUPABASE_URL` | with supabase auth | Your Supabase project URL (tokens are checked against its public keys) |
| `VORA_CORS_ORIGINS` | production | Comma-separated web origins allowed to call the API, e.g. `https://awdax.pages.dev` |
| `VORA_SEARXNG_URL` | recommended | SearXNG address, `http://127.0.0.1:8080` |
| `SEARXNG_SECRET` | with SearXNG | Random hex for SearXNG |
| `DATABASE_PATH` | optional | SQLite file, default `./vora.db` |
| `HF_TOKEN`, `VORA_BACKUP_REPO`, `VORA_BACKUP_MINUTES` | Hugging Face | Database snapshots to a private dataset (see `docs/DEPLOY.md`) |
| `VORA_SOURCE_CACHE` | optional | Share reads of public sources between tracks (default `true`) |
| `VORA_SOURCE_TTL_LISTING_MINUTES`, `VORA_SOURCE_TTL_PAGE_MINUTES` | optional | How long a shared read stays fresh (30 / 180) |
| `VORA_QUERY_CACHE_MINUTES` | optional | How long a shared plan, site list and search result is reused (720) |
| `VORA_BATCH_SECONDS`, `VORA_LIVE_INTERVAL_SECONDS` | optional | Run time budget (300) and live cadence (300) |

Every other setting has a safe default; see `vora/settings.py`.

## Work shared between users

- **Source reads**: a read of a public page or listing is stored once (`source_reads`, `source_rows`) and reused by
  any track while fresh; each track scores the shared rows with its own plan. A second request for a source that is
  being read right now waits for that read instead of opening the site again. A live track only accepts reads newer
  than its own cadence; `POST /api/v1/instances/{id}/runs?fresh=true` reads everything again.
- **Answers** for the same request (plan, official sites, search results) are shared through `query_cache`, keyed by
  the request's words (case and punctuation ignored, word order kept).
- **Learned site structures and the blacklist** are universal (`learned_structures`, `blacklisted_sources`).
- Nothing private is shared: these tables have no owner, goal or conversation column (tested).

## Storage

One SQLite file. Rows are stored one per line (`snapshot_rows`) and a save writes only rows that changed. Old run
events, runs, history and shared reads are purged on a schedule. On Hugging Face the file is restored on start and
snapshotted to a private dataset (`deploy/backup.py`).

## Tests

```powershell
python -m unittest discover -s tests        # ~340 tests, a few minutes (browser tests need VORA_BROWSER_BINARY)
```

Guards worth knowing: `test_no_site_bias.py` (no site names in code), `test_no_secrets.py` (no keys in files),
`test_shared_sources.py` (sharing, single flight, privacy of shared tables), `test_network_guard.py` (no private
addresses).

## Deploy

See [docs/DEPLOY.md](docs/DEPLOY.md) (Hugging Face Docker Space). Architecture details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md);
API: [docs/API.md](docs/API.md).
