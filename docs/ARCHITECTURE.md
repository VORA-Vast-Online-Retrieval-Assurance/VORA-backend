# VORA Architecture

## Backend layout (one package, `vora/`)

Dependencies point one way: `api` -> `research` -> (`learning`, `extraction`, `output`, `storage`) -> `browser` -> `shared`.
Nothing below `api` imports it.

| Folder | What belongs there |
|---|---|
| `vora/settings.py` | The one application settings object (`from vora.settings import settings`) |
| `vora/api/` | HTTP only: routes (`application.py`) and sign-in (`auth.py`) |
| `vora/research/` | One run, start to finish: `coordinator.py`; `planning/` (planner, official-site resolver); `discovery/` (search, ranking, source health, shared site knowledge); `reading/` (browser pool, crawl, interactive pass) |
| `vora/learning/` | How a site is learned and replayed: `structure.py`, `api_records.py`, `accessibility.py`, `recipes.py`, `source_registry.py` |
| `vora/browser/` | The headless browser runtime: engine, events, interactive exploration, forms, engine settings |
| `vora/extraction/` | Turning rendered pages and files into scored observations |
| `vora/storage/` | SQLite persistence (`repository.py`) |
| `vora/output/` | What a run produces: datasets, graphs, tables, the live stream |
| `vora/shared/` | Contracts (`contracts.py`), URL safety, caches, regions, system helpers |

Outside the package: `app.py` (starts the server), `tests/`, `data/` (registry and blacklist JSON), `docs/`, `scripts/`.

## Boundaries

- `core/` owns browser-process lifecycle, isolated contexts (media requests
  dropped, lean launch flags), rendering, safe interactive exploration
  (`vora/browser/explore.py`), network observations, and lifecycle events.
- `models/` defines cross-layer DTOs. Core rendering contracts remain
  engine-local and are adapted at the extraction boundary.
- `extraction/` consumes immutable engine results through event listeners and
  does not control Playwright. It owns parsing, noise filtering, semantic
  normalization, temporal resolution and scoring.
- `llm/` contributes planning vocabulary and resolves unplaceable headers, with
  provider fallback. It never decides what is mandatory and never does dates.
- `services/` coordinates batches (fast and interactive passes), search and its
  rationing, persistence, live scheduling, live fan-out and graphs, while `api/`
  remains a request/response, WebSocket/SSE boundary. Sign-in checking
  (`vora/api/auth.py`) lives there too; the web app is a separate project.
- `config/` is the application-wide environment boundary.

## Dependency direction

```text
api -> services -> core
               -> extraction -> models
               -> llm        -> extraction (requirements, temporal) -> models

core -> core contracts only
```

## Research pipeline

```text
goal
 └─ planning        llm.analyze_goal
      ├─ vora.extraction.requirements   minimal required concepts, grounded in the goal
      └─ vora.extraction.temporal       deterministic time window ("last 10 years" = 10 periods)
 └─ discovery       search cache, or vora.research.discovery.discovery (search API, else
                    DuckDuckGo -> Bing, rationed by SearchGate); without
                    results: old results, earlier useful pages, known sites
 └─ rendering       fast pass: core.BrowserEngine.execute -> onExecutionComplete
                    interactive pass (parallel): BrowserEngine.explore -> one
                    onExecutionComplete per page state; chart JSON; focused crawl
 └─ extracting      vora.extraction.parser
      ├─ page provenance (published / modified / fetched, title, publisher)
      ├─ early noise filter (challenge pages, nav/footer/consent chrome, metadata)
      ├─ tables (captions, titles, unpivoted period columns), JSON-LD,
      │  repeated regions, prose statements (level / change / bound, counts)
      └─ every row tagged with its page block
 └─ scoring         vora.extraction.scoring.ObservationScorer
      ├─ vora.extraction.semantics      field -> concept (lexicon, aliases, typos,
      │                            value types, unit compatibility, context)
      ├─ vora.extraction.temporal       row period vs page dates (provenance)
      ├─ multi-factor score -> tier (high / usable / partial / low / noise)
      └─ vora.extraction.blocks        block support and overlapping-block suppression
 └─ merging         re-score with one plan, de-duplication and ordering
```

Each phase change is persisted in `run_events` and streamed to clients (with
new accepted rows) over a WebSocket, so the UI shows only stages that actually
happened and rows as they are found.

## Semantic model

A plan has *concepts*, not columns. Only concepts the user asked for are
required (normally the measure, plus `period` when a time window exists);
everything else a model suggests (make, model, trim, currency, country…) is
optional enrichment and never a rejection reason. Field names are matched to
concepts through a domain-neutral lexicon of quantitative vocabulary, planner
aliases, typo tolerance and value typing. Subject words (“battery”, “wheat”)
match literally, by stem or as acronyms; there is no topic-specific code. When
a request names what it counts rather than a measure word (“hospitals”,
“wins”), those nouns are the plan's *subject heads*: a numeric column named
after one is the requested quantity (identifier columns such as “ID” or
“Rank” never are), and a sentence's counted noun must be one of them.

## Answer shape and records

A plan carries an `answer_shape`: `quantities`, `records` or `either`. Quantitative
cues in the request ("number of", "price", "how many") keep the numeric requirement;
otherwise no measure is invented. A row on the record path
(`vora/extraction/records.py`) is accepted when it has a naming field and at least two
typed attributes (date, url, identifier, text), and a date when a window was asked
for. `source_mentions` are named sources ("from egazette"); ranking matches them
against host names, comparing compact forms so eGazette equals egazette.

## Named sources

A website written in the request (`egazette.com`, `https://data.gov.in/...`) is a named
source: it is opened first (a link in the goal), its own name (`egazette`) becomes a source
mention that matches other addresses of the site (`egazette.gov.in`), and it may fill the
batch while other sites keep the one-page share. Address parts (`com`, `gov`, `www`) never
count as topic words, so unrelated `.com` results are recognised as off-topic and the search
engine is treated as blocked. Rows from a page that never mentions what was asked for are
set aside (rejected) when the request names a source. Code hosts (GitHub and similar) are
ranked down unless the request is about software.

## Source registry and recipes

`data/sources.json` (loaded and validated by `vora/learning/source_registry.py`) maps the
names of official portals to their domains, entry page and optional recipe. The planner
(`llm/provider.apply_registry`) records matched entries as `plan.registry_sources`,
names them as source mentions and suggested sources, and treats a portal with a
`record_type` as a records request. Discovery adds the entry page first
(`origin="registry"`), before goal links and search, so a blocked search engine does
not matter; ranking puts it first and gives the name boost only to the entry's own
domains. The fast pass reads a registry page with `core/recipes.run_recipe` (through
the browser pool) instead of a plain render: ready steps, the table, the column map,
identifiers checked against `id_pattern` (a dry run: a missing table or foreign IDs
report "layout changed" and fall back to the general reader), postback or next-link
paging, links built from the ID, and a stop at the first page whose records are all
known. Once a registry source has answered, other sites in the batch are skipped.

Observations that carry a real identifier (an ID-like label with a reference-shaped
value, `extraction/records.natural_key`) are keyed by domain and identifier, so the
same notice read from two pages or two runs is one row. Pages of JavaScript apps are
also read from the data they embed (`__NEXT_DATA__`, `window.__INITIAL_STATE__`,
`application/json` scripts) as `hydration_json` rows.

## Query forms

`vora/browser/forms.py` types each form (fields, options, purpose). Query forms are filled
from the request only (window dates in the field's own format, keywords, matching
select options) and submitted; account, payment, contact, subscribe, upload and
CAPTCHA forms are refused. `vora/browser/explore.py` waits for navigation after a submit and
pages through results, including `__doPostBack` pagers. Nothing names a site.

## Temporal model

Every observation separates the period it describes (`data_period`,
`period_start/end`, `time_basis`, `time_inferred`, `temporal_confidence`) from
provenance (`published_at`, `modified_at`, `fetched_at`). Precedence: row field →
value in the row → a year in the measured column's name → table caption/title →
nearest heading → page title →
structured metadata → snapshot inference from modified/published/fetched dates.
Page dates only become a period when the row has none and looks like a current
snapshot, and such periods are marked inferred with low confidence.

## Scoring

`score = 0.30 coverage + 0.22 relevance + 0.18 temporal + 0.12 extraction +
0.10 completeness + 0.08 source − 0.60 noise`. Accepted rows (high ≥ 0.72,
usable ≥ 0.55) must also show the requested measure, be on-subject and fall
inside the window. Relevant but incomplete rows are kept as *partial*; chrome
and metadata are *noise*. `POST /dataset/rescore` re-applies scoring to stored
observations without fetching again.

## Batches, passes and streaming

A run is a **batch** with a work budget (`VORA_BATCH_SECONDS`, 300 s) that
starts when the batch begins; batches run concurrently (up to
`VORA_MAX_CONCURRENT_BATCHES`) and borrow browsers from a shared pool.

- **Fast pass** (batch thread, `vora/research/coordinator.py`): renders ranked
  candidates. Pages that yield nothing are `empty` and do not count toward
  `VORA_MAX_SOURCES`.
- **Browser pool** (`vora/research/reading/browser_pool.py`): at most `VORA_BROWSER_POOL`
  Chromium workers, started lazily on their own threads, ordered by priority
  (search, page, explore), replaced after a crash, recycled after
  `VORA_BROWSER_RECYCLE_AFTER` pages. Each task runs with its own batch's event
  bus. The per-host politeness gate is shared by all batches.
- **Interactive pass** (`vora/research/reading/deep_lane.py`, its own thread; browser from the pool):
  - re-opens pages with `vora/browser/explore.py` (safe, bounded actions);
  - captures chart JSON as `network_json` rows;
  - follows up to three matching same-site links (`vora/research/reading/crawl.py`).
- **Single writer:** results return through an outbox, so only the batch thread
  mutates the snapshot, and saves are throttled to one every 5 s.
- **Streaming:** `vora/output/live_hub.py` carries run changes (via a repository
  observer) and new accepted rows to WebSocket clients as they happen.
- **Scheduling:** live batches start every `VORA_LIVE_INTERVAL_SECONDS`,
  counted start to start. Search results are cached per goal, so frequent
  batches work through the queue.
- **Bounded state:** in-process caches are `utils/cache.BoundedCache` (LRU + TTL);
  snapshots keep a denormalized `accepted_count`; `Repository.purge` applies the
  retention settings hourly.
- **Scoring by block:** rows are also scored per page block
  (`vora/extraction/blocks.py`). Blocks that do not support the request, and repeats
  of a stronger block, are demoted to partial.

## Sign-in and ownership

`vora/api/auth.py` checks a signed-in user's Supabase access token (signature against the
project's public keys, issuer, audience, expiry) and returns the user id. The
`authorized` dependency in `vora/api/application.py` runs it per request (mode chosen by
`VORA_AUTH`) and remembers the user for the request. Tracks (`instances.owner_id`)
and webhooks belong to their creator; `require_instance` reports someone else's
track as missing (404), and preferred sources, saved results and metrics are
per user. Cookies are not used with Supabase, so CORS runs without credentials
(`VORA_CORS_ORIGINS`). A WebSocket, which cannot send headers, proves itself with
a first message `{"type": "auth", "token": "..."}`.

## Web app

A separate project (the private VORA repository): React + Vite, static, deployed on
Cloudflare Pages. It reads this API's `/api/v1` routes, turns the responses into what
its screens draw (`src/api/adapt.ts`), and reads the live stream with `fetch()` so the
token travels in a header.

## Sites the request does not name (resolver + learned structure)

When a request names no registered source and no address, `vora/research/planning/source_resolver.py` asks two models for the official
sites; the program keeps only hosts that exist and are public (near-miss domains are repaired, invented ones dropped).
They enter discovery as origin `resolved`. For such a site `vora/learning/structure.py` opens the page in the browser and
learns how its listing is built (a table with an ID column and pager, or a link list), producing the same declarative
`Recipe` the registry uses. Learned recipes are stored per host in `learned_structures` (public-site facts, no owner or
user data), expire after `VORA_LEARNED_TTL_DAYS`, and are dropped after repeated failed reads. A learned read that
yields accepted rows stops lower-ranked search pages for that batch. Models only ever see the request text and table
headers; their answers are validated (hosts, field names) and never executed. Every browser request and link check goes
through the public-address guard (`VORA_NETWORK_GUARD`). A site worth keeping can be promoted into `data/sources.json`.

### How a listing is learned (no site-specific rules)

`vora/learning/structure.py` observes what a page does and keeps only what verifies: it reads the page's tables and role-based
grids, repeated blocks of linked items (cards, lists, feeds), and the data requests the page makes. A record is
identified by whatever is unique (a shaped reference number is preferred; otherwise the leftmost unique column, a number
in the address, or the address itself). Paging is found, not assumed: numbered links, "next" controls, a load-more
control, scrolling, or a paged data service (`vora/learning/api_records.py`, by page/offset parameter or a `next` address); each
guess is tried and kept only when the same listing really grows. Every learned recipe is replayed (two pages, unique
ids, working links) before it is used. `tests/test_no_site_bias.py` fails if any site name appears in program text.

### The rule every site is learned by

1. **Layout geometry** finds repeated items whatever the markup: children of one kind (same tag and classes), else the
   same element type at the same size or with the same left edge and width (`repeatedKids` in `vora/learning/structure.py`).
2. **The accessibility tree** (`vora/learning/accessibility.py`) is used in four places only: page regions (banner, navigation,
   sidebar, footer, search are not data), accessible names of controls, paging controls by role, name and disabled
   state, and elements the browser treats as a table or grid. It never builds a selector.
3. **The DOM** supplies selectors and replay. **Network capture** finds data services (`vora/learning/api_records.py`).
4. **The replay check is the judge**: a learned recipe is used only after it reads two pages with unique ids and
   working links. No site is named anywhere (`tests/test_no_site_bias.py`); `tests/test_site_learning_rule.py` keeps
   the rule honest.

### Search with SearXNG (optional)

Browser searches on single engines get blocked or rate-limited, so section pages of an official site may never reach
discovery. VORA can query a self-hosted SearXNG instead (`vora/research/discovery/search_api.py`: no
per-engine gate, `site:` queries work). A keyed provider (Brave, Google), if configured, takes priority.

Run it with Docker from the backend folder: `docker compose up -d` (stop with `docker compose down`). The compose file
uses `deploy/searxng/settings.yml` (JSON output on, limiter off) and binds to `127.0.0.1:8080` only. Put
`SEARXNG_SECRET=<random hex>` and `VORA_SEARXNG_URL=http://127.0.0.1:8080` in `.env`.
