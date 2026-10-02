# VORA: How It Works

A complete guide to the VORA research engine: the end-to-end workflow, what every
file does and why, how the key problems were solved, and what the system does
**not** do.

> Paths below are relative to the repository root.
> Related document: [`SOURCE_RANKING_PLAN.md`](SOURCE_RANKING_PLAN.md), the design
> record of source ranking, rotation and merging (implemented; later changes are noted there).

---

## Contents

1. [What VORA is](#1-what-vora-is)
2. [The big picture](#2-the-big-picture)
3. [A research run, end to end](#3-a-research-run-end-to-end)
4. [Each stage in depth](#4-each-stage-in-depth)
5. [The frontend](#5-the-frontend)
6. [File-by-file reference](#6-file-by-file-reference)
7. [How we solved the hard parts](#7-how-we-solved-the-hard-parts)
8. [Data model](#8-data-model)
9. [Configuration](#9-configuration)
10. [Running and testing](#10-running-and-testing)
11. [What VORA does not do](#11-what-vora-does-not-do)

---

## 1. What VORA is

You describe the dataset you want in plain language, e.g.

> *"EV car prices over the last 10 years"*

VORA then:

1. **Plans** the minimum it needs: the *measure* (price), the *time window* (2017–2026), and the *subject* (EV, car).
2. **Searches** the web and picks candidate pages. It rations searches and uses a search API
   when one is configured.
3. **Renders** each page in a real Chromium browser, and in parallel **interacts** with pages
   (tabs, "load more", further pages, chart data) and follows a few matching same-site links.
4. **Extracts** raw observations from tables, structured data, cards, sentences and chart data.
5. **Filters out noise** such as menus, cookie banners, bot-challenge pages and page metadata.
6. **Interprets** each observation: which concept each field means, and which time period it describes.
7. **Scores** each observation, and each page block as a whole, and sorts it into *accepted*,
   *partial* or *noise*.
8. **Builds a dataset** where every row carries its source, dates and confidence.
9. **Streams it live** to a web workspace, over a WebSocket, with one graph per parameter.
   All of this runs in time-boxed **batches** (5 minutes, repeated every 5 minutes in live mode).

---

## 2. The big picture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                    WEB APP  (separate project, Frontend)                 │
│  Chats · Review · Sources · Graphs · Activity · Settings · Tools         │
│  Supabase sign-in            ▲ REST + token   ▲ SSE / WebSocket          │
└───────────────────────────────────────┼──────────────────┼───────────────┘
                                        │                  │
┌───────────────────────────────────────┴──────────────────┴───────────────┐
│                          API LAYER  (vora/api/application.py)                 │
│  Instances · Messages · Runs · Dataset · Graphs · Sources · Search · Auth│
└───────────────────────────────────────┬──────────────────────────────────┘
                                        │
┌───────────────────────────────────────┴──────────────────────────────────┐
│                          SERVICES  (services/)                           │
│   coordinator.py  ── batches: plan, search, fast pass, merge, schedule   │
│   deep_lane.py    ── interactive pass + focused crawl (own browser)      │
│   discovery.py    ── search API / engines, rationing → candidate URLs    │
│   live_hub.py     ── live messages → WebSocket clients                   │
│   graphs.py       ── one chart per numeric parameter                     │
│   repository.py   ── SQLite: instances, runs, events, snapshots, cache   │
└───────┬─────────────────────────┬──────────────────────────┬─────────────┘
        │                         │                          │
┌───────┴────────┐   ┌────────────┴─────────────┐   ┌────────┴────────────┐
│  LLM  (llm/)   │   │   EXTRACTION (extraction/)│   │   CORE  (core/)     │
│ vocabulary +   │   │ parser · noise · semantics│   │ BrowserEngine       │
│ header mapping │   │ temporal · requirements · │   │ explore (safe       │
│ (never decides │   │ scoring · blocks ·        │   │  interaction) ·     │
│  requirements) │   │ collector                 │   │ EventBus lifecycle  │
└────────────────┘   └───────────────────────────┘   └─────────────────────┘
                                        │
                              ┌─────────┴─────────┐
                              │ MODELS (models/)  │  shared data contracts
                              └───────────────────┘
```

**Dependency rules** (lower layers never import higher ones):

```
api ──► services ──► core
                 ──► extraction ──► models
                 ──► llm ──► extraction (requirements, temporal) ──► models
core ──► core contracts only
```

**Why this shape:** the browser engine knows nothing about research; extraction
knows nothing about HTTP or the database; the API only translates requests. Each
piece can be tested alone, and the engine can be reused for other tools.

---

## 3. A research run, end to end

### 3.1 The sequence

```
 User types a goal in the composer
          │
          ▼
 POST /api/v1/instances/{id}/messages ──────────────► repository: save message,
          │                                            set goal, create run (queued)
          ▼
 coordinator.submit()  ── cancels any active run of this track, queues a new one
          │
          ▼  (background thread)
┌─────────────────────────────────────────────────────────────────────────────┐
│ 0. BROWSER CHECK coordinator._browser_settings(): no browser → run fails    │
│                  at once with the fix ("set VORA_BROWSER_BINARY")         │
│                                                                             │
│ 1. PLANNING      coordinator._plan(): today's plan of the same goal, or     │
│                  llm.analyze_goal()  (model gets ≤ 20 s, else heuristic)    │
│                  ├─ LLM draft: synonyms, subject terms, search queries      │
│                  ├─ requirements.build_requirements(): minimal concepts,    │
│                  │   subject heads ("hospitals") for count-like requests    │
│                  └─ temporal.resolve_time_window(): deterministic dates     │
│                                                                             │
│      ── the 300 s batch budget starts when the batch gets the browser ──     │
│                                                                             │
│ 2. DISCOVERY     cached results (≤ 60 min, still unread)  or  discover()    │
│                  ├─ search API if configured, else DuckDuckGo → Bing,       │
│                  │   rationed by the search gate; blocked / unrelated       │
│                  │   pages skipped and reported                             │
│                  ├─ up to 40 candidates, queries interleaved                │
│                  └─ no results → old results, earlier useful pages,         │
│                      preferred/suggested sites' home pages                  │
│                                                                             │
│ 3. RANKING       ranking.rank(): preferred, proven, unread first            │
│                                                                             │
│ 4. TWO PASSES IN PARALLEL                                                   │
│   fast pass      engine.execute(url) → collector → parser → scorer          │
│   (batch thread) until 8 usable pages; empty/blocked pages don't count      │
│   interactive    deep_lane: engine.explore(url) on each page read, emptiest │
│   pass           first; chart JSON; ≤ 3 matching same-site links per page   │
│   both           rows → snapshot (single writer) → WebSocket "rows"         │
│                                                                             │
│ 5. DATASETS      linked CSV files (most relevant 2), within the budget      │
│                                                                             │
│ 6. MERGING       re-score all rows with one plan (blocks included),         │
│                  de-duplicate, order                                        │
│                                                                             │
│ 7. COMPLETE      save snapshot, summary + diagnostics, run → succeeded      │
└─────────────────────────────────────────────────────────────────────────────┘
          │
          │  every phase/detail change → repository.update_run()
          │                              → row in run_events → live hub
          ▼
 WS /live/ws ──► browser updates the pipeline, live bar, dataset and graphs as rows land
```

### 3.2 The pipeline stages you see in the UI

| UI label | Internal phase | What really happens |
|---|---|---|
| Understanding request | `planning` | Plan built; detail shows e.g. `Planned: price, period · 2017–2026 (llm:…)` |
| Discovering sources | `discovery` | One event per search query (`Searching: …`), or `Reusing search results from …` |
| Inspecting pages | `rendering` | `Rendering evseekers.com (2/8)`; the `(i/n)` drives the progress bar. Interactive-pass steps are prefixed `[deep]` and never change the phase |
| Extracting observations | `extracting` | Parser runs on the rendered HTML |
| Normalizing & scoring | `scoring` | Concept matching, periods, scores |
| Merging dataset | `merging` | De-duplication and ordering |
| Complete | `complete` | Final counts, preceded by a `Diagnostics: …` event (planner, search, page outcomes, time used) |

Every stage shown in the UI is read from `run_events`, so the UI never shows
activity that didn't happen.

---

## 4. Each stage in depth

### 4.1 Planning: *"what does the user actually need?"*

```
 "EV car prices over the last 10 years"
        │
        ├──► temporal.resolve_time_window()     ──► 2017-01-01 … 2026-12-31
        │        (deterministic, no LLM)              periods: 2017 … 2026 (10)
        │
        ├──► temporal.strip_time_expressions()  ──► "EV car prices"
        │
        ├──► requirements.analyze_words()
        │        "prices" → lexicon class *price*  → REQUIRED measure
        │        "EV", "car" → no class           → SUBJECT terms
        │
        ├──► (optional) LLM draft via vora/research/planning/provider.py
        │        measures: price (+ aliases msrp, cost, …)
        │        subject: electric vehicle, electric car
        │        optional: make, model, trim …  ──► demoted to OPTIONAL
        │        queries:  "… 2015-2025" ──► align_query_years → "2017-2026"
        │
        ▼
 GoalPlan
   required:  price, period
   optional:  entity (make/model), category (trim), …
   subject:   ev, car, electric vehicle, electric car
   time:      2017–2026, "10 calendar years ending with the current year"
```

**Answer shape and sources.** The plan also records `answer_shape` (`quantities`,
`records`, `either`) and `source_mentions`. A measure is required only when the
request asks for one (quantitative cues); a request such as "data from egazette"
asks for records, and the named source is matched against candidate host names.

**Key rule:** a concept becomes *required* only if the user's own words mention it
(checked by `requirements._grounded`). Everything a model invents is optional
enrichment, never a reason to reject data.

**Time policy** (one definition, in `vora/extraction/temporal.py`):

| Phrase | Result (today = 2026-09-29) |
|---|---|
| last 10 years | 2017–2026 (10 years, current year included) |
| last 5 complete years | 2021–2025 |
| last 6 months | 2026-04 … 2026-09 |
| last 4 quarters | 2025-Q4 … 2026-Q3 |
| past 30 days | 2026-08-31 … 2026-09-29 |
| since 2019 | 2019–2026 |
| last year | 2025 |

### 4.2 Discovery: *"where might the data be?"*

`vora/research/discovery/discovery.py` collects candidates **with their title, snippet and rank**:
- Links written in the goal go first.
- **Preferred sites** (your per-track list, then the global list; up to 4) get a site search,
  e.g. `site:cardekho.com EV car prices India`, and only results really on that site are kept.
- Then up to 4 planner queries run on **DuckDuckGo**. If a search page is a verification
  (CAPTCHA) page, a consent screen, or has no results, **Bing** is tried, and what happened is
  recorded ("duckduckgo: blocked by a verification page") for the run's diagnostics.
- When the plan names one country, searches ask for that region (DuckDuckGo `kl`, Bing `cc`).
- Queries are **interleaved**: the first result of each query, then the second of each, and so
  on, so every query contributes different sites.
- Result links are unwrapped (DDG `uddg=`, Bing `u=a1…`) and de-duplicated; search-engine links are dropped.
- Collection stops at `VORA_MAX_CANDIDATES` (40). Searching may use at most 40 % of the batch budget.
- **Search cache:** results are stored per goal. Later batches reuse them for up to
  `VORA_SEARCH_CACHE_MINUTES` (60) while some candidates are still unread, so 5-minute live
  batches work through the queue instead of searching every time. Search engines rate-limit
  frequent automated queries.
- **Rationing** (`SearchGate`): an engine that blocked us rests for
  `VORA_SEARCH_COOLDOWN_MINUTES` (15). The rest doubles on each repeated block, up to 4
  hours, and a normal answer resets it.
  - Browser searches are at least `VORA_SEARCH_INTERVAL_SECONDS` (5) apart.
  - There are at most `VORA_MAX_SEARCHES_PER_HOUR` (30) across all tracks.
  - Querying stops once there are enough candidates.
- **When search is unavailable**, the batch still has work, in this order:
  1. this goal's last search results, however old;
  2. this track's pages that gave data before;
  3. the home pages of preferred and planner-suggested sites (the interactive pass then
     follows their matching links).
- **Search API (recommended for stock browsers):** with `VORA_SEARCH_API` (`brave` or
  `google`) and a key, searches use the official API first. There is no bot check, and
  results are consistent.

### 4.2b Ranking, rotation and merging: *"which pages first, and what about next time?"*

`vora/research/discovery/ranking.py` scores every candidate **before any page is opened**:

| Signal | Score |
|---|---|
| Link in your goal | +110 |
| Preferred site (track or global list) | +100 |
| Proven: gave accepted rows in this track before | +40 |
| Suggested by the planner (`plan.suggested_sources`) | +25 |
| Looks like data (data, statistics, table, price list… / .gov .edu .int .ac) | up to +15 |
| Mentions your subject or measure | up to +10 |
| Not read before in this track | +20 |
| Read before without useful rows | −40 |
| Read within the revisit window (`VORA_REVISIT_HOURS`, 24 h) | −150 |
| Blocked us recently (any track, `VORA_BLOCK_COOLDOWN_DAYS`, 7) | −60 |
| Search position | −1.5 per place |

- **One page per site** first (preferred sites: two); further pages from a site come after every other site.
- **Backfill:** blocked or failed pages don't use up the budget. The coordinator keeps opening the next candidate
  until `VORA_MAX_SOURCES` usable pages are read (hard stop after twice that many attempts).
- **Rotation:** candidates not opened are shown as **Queued for next runs**, with their score and reasons. Because
  recently read pages rank last, the next run reads them.
- **Merging:** a run of the **same goal** adds to the existing dataset. Pages read again replace their old rows,
  everything is re-scored with the current plan, and raw rows are capped at `VORA_MAX_RAW_OBSERVATIONS`
  (5,000, newest kept). A **different goal** starts fresh.
- **History:** every opened page is recorded in `source_history` (domain, status, accepted rows). This feeds
  *Proven*, *Blocked recently* and *rotation*, and the **Source reputation** table in Tools.

Live example, *"EV car prices in India"* with `cardekho.com` preferred: run 1 read CarDekho first (87 accepted rows);
run 3 read five unread sites from the queue; the merged dataset grew **87 → 104 → 218** rows from 10 sources.

**Site adapters** (`extraction/sites/`): CarDekho and CarWale listing cards (ported from vora) are read *in
addition to* the general extractors (method `site_adapter`), and their rows are scored like any other.
`VORA_SITE_ADAPTERS=false` switches them off.

### 4.3 Rendering: *"load the page like a real browser"*

`vora/browser/engine.py` launches the configured Chromium build (`VORA_BROWSER_BINARY`) through
Playwright:

```
execute(url)
  ├─ onExecutionStart
  ├─ new isolated context + page
  ├─ onNetworkRequest / onNetworkResponse (recorded)
  ├─ goto(url, domcontentloaded) → wait for network idle (≤ 5 s)
  ├─ onNetworkIdle
  ├─ ExecutionResult(final_url, title, rendered html, status,
  │                   metadata: fetched_at, date / last-modified headers)
  └─ onExecutionComplete(result)   ── or onExecutionFailed(error)
```

Listeners run through `vora/browser/events.py::EventBus`. A crashing listener is logged and
isolated, so it can't break the browser.

### 4.3b Interactive pass and focused crawl: *"the data is behind a tab"*

Many pages show their numbers only after interaction. Each batch therefore runs **two passes in
parallel**, each on its own thread with its own browser (Playwright's sync API belongs to the
thread that started it):

```
 fast pass (batch thread)                     interactive pass (vora/research/reading/deep_lane.py)
 ─────────────────────────                    ──────────────────────────────────────────
 render page 1 ── rows streamed ──► queue ──►  explore page 1 (emptiest pages first)
 render page 2                                   scroll, open sections, tabs, "load more",
 ...                                             next pages, iframes, shadow DOM,
                                                 JSON/CSV the page loads (charts)
                                               follow ≤ 3 matching same-site links (crawl)
           ◄──────── outbox: finished pages, applied by the batch thread ────────
```

- **`vora/browser/explore.py`** (`BrowserEngine.explore`) performs a bounded list of **safe** actions
  and returns every distinct state of the page. Each state is parsed like a normal render.
  - **Forms:** a form is typed by `vora/browser/forms.py`. Query forms (search, filters,
    date ranges) are filled from the request only and submitted, then their results
    and pagers (including ASP.NET postbacks) are followed. Login, payment, contact,
    subscribe, upload and CAPTCHA forms are never touched (`VORA_FORMS=false`
    disables form use altogether).
  - **Never touched:** links to other sites or to files, and controls labelled like
    account, purchase, download, sharing or deletion actions (`is_safe_action`).
  - **Limits:** at most 25 actions and 30 s per page.
  - **Chart data:** captured JSON/CSV responses become `network_json` rows, so charts on any
    site, not only Our World in Data, can yield data.
- **`vora/research/reading/crawl.py`** ranks a page's same-site links by how well their text and address
  match the request (subject words, concepts, years). Boilerplate pages (about, login, careers,
  privacy…) are skipped. At most `VORA_CRAWL_PER_SITE` (3) links per page are followed, one
  level deep. This is a *focused* crawl, not "every link": that would not fit the 5-minute
  budget, would get the site to block us, and would mostly add irrelevant pages.
- **Politeness:** both passes share a per-host gate, one navigation per host every 2 s.
- **Single writer:** only the batch thread changes the snapshot. The interactive pass
  hands finished pages over through an outbox.
- **Deduplication:** rows seen by both passes are recognised by their content id and
  kept once.
- **Sources view:** it shows what the interactive pass added ("+3 rows · opened tab
  '2023'"). Followed links appear as *Linked page*.

### 4.4 Extraction: *"turn a page into raw observations"*

`vora/extraction/collector.py` listens for `onExecutionComplete` and calls
`vora/extraction/parser.py::parse_rendered_page`:

```
rendered HTML
  │
  ├─ page_provenance(): title, site name, published_at, modified_at,
  │                     fetched_at, temporal coverage  (meta tags, JSON-LD, headers)
  │
  ├─ challenge / login wall?  ──yes──► one "challenge" observation, stop
  │
  ├─ JSON-LD blocks            (method json_ld,         confidence 0.85)
  ├─ HTML tables               (method html_table,      confidence 0.90)
  │     ├─ header detection (th, thead, or a text first row)
  │     ├─ spanning title row → caption (e.g. "…June 2026")
  │     ├─ 2-column property sheet → one observation
  │     └─ period columns (2019 | 2020 | 2021) → unpivot, one row per period
  │
  ├─ remove page chrome: nav, footer, aside, cookie/consent, menus,
  │                      social, popups (never >50% of the page)
  │
  ├─ repeated cards            (method repeated_region, confidence 0.60)
  ├─ prose sentences           (method text_statement,  confidence 0.55)
  │     sentences with a period AND a quantity; each quantity is
  │     classified: level ($108/kWh) · change (down 8%, $3,000 less) · bound (under $20,000)
  │     counts are quantities too: "1,280 hospitals", "2,400 people" (≥ 1,000, never
  │     page counters such as views or comments)
  ├─ undated sentences         (method undated_statement, confidence 0.45; ≤ 15 per page)
  │     the period is inferred from the page dates; the sentence must name what it counts
  │
  ├─ every row is tagged with its block: table#2, cards:…, "tab: 2023/table#1", json:…
  │
  └─ nothing found? → page summary (method page_summary, confidence 0.25)

each observation → noise.assess() → content_role: data | metadata | navigation | challenge | noise

states of an interactively explored page, and the JSON a page loads (method
network_json, confidence 0.85), go through the same parser and scorer.
```

### 4.4b Linked datasets: *"the numbers are in a chart — get the file behind it"*

Many statistics pages (e.g. Our World in Data) draw their numbers as interactive
charts; the HTML holds only chart controls, which are correctly filtered as noise.
Publishers offer the same data as a download, so VORA follows it:

```
rendered page ── extraction/datasets.find_dataset_links()
                   ├─ explicit data files: links ending .csv/.tsv or format=csv
                   └─ embedded charts with a documented CSV endpoint
                      (Our World in Data: /grapher/<slug> and /explorers/<slug> → <same>.csv)
        │
        ▼  after all pages are rendered
 rank_links(): plan subject + measure words in the title/URL, main chart first
        │  top VORA_MAX_LINKED_DATASETS (2)
        ▼
 services/datasets.download(): plain HTTPS GET, public URLs only, ≤ 20 MB, 30 s;
                               publisher metadata (title, units, last update) if offered
        │
        ▼
 extraction/datasets.parse_csv(): keep rows inside the requested window (and named
        places); one observation per measure column; wide files → series + value
        │
        ▼
 dataset_observations() → same scorer as every other row (method linked_dataset, 0.95)
```

Example: *"Agricultural yeild in last 5 years"* with ourworldindata.org/crop-yields.
The page gave 10 noise rows; its two linked CSVs gave **1,996 accepted rows** (crop ×
country × year, 2022–2024) with explicit periods.

### 4.4c Files: *"every file is kept as a link"*

Every file a page links to (or a search returns) is recorded in the track's **file registry**
(`vora/extraction/files.py`, `ResearchSnapshot.files`) and shown under **Sources → Files found**:

| Type | Examples | What VORA does |
|---|---|---|
| Data | CSV, TSV | Link, and the most relevant are **read automatically** |
| Spreadsheets | XLSX, XLSM | Link + **Extract data** on request (XLS/ODS: link only) |
| Documents | PDF | Link + **Extract data** on request (DOCX/PPTX: link only) |
| Structured | JSON, GeoJSON | Link + **Extract data** when it holds a list of records |
| Archives, media | ZIP, images, video | Link only |
| Programs | EXE, MSI, APK, scripts | Link only, **never downloaded** |

Extraction happens **in memory, never on disk**: the file is downloaded (≤ 20 MB, 30 s, public addresses only,
every redirect checked), then:
- **CSV / XLSX / JSON** → one table per file or sheet → window and place filtering → one row per measure;
  title rows become the context, and years across the top are unpivoted.
- **PDF** → each page's tables (with the caption line above them) and text are rendered as simple HTML and
  run through the **same page extractor** as websites (methods `pdf_table`, `pdf_text`). Scanned PDFs report
  "No readable text"; up to 30 pages are read.

A web page served instead of a file, or a file that isn't really a PDF/workbook, is refused with a clear reason.
Up to 50 files are registered per run, most relevant first.

### 4.5 Semantic normalization: *"what does this field mean?"*

`vora/extraction/semantics.py` maps raw field names to requested concepts in layers:

```
 "average_pack_price"
      │  tokens()            → average · pack · price
      │  head_token()        → price   (ignores modifiers, units, words after "per")
      │
      ├─ exact / alias       → price == price               credit 1.00
      ├─ lexicon synonym     → cost, msrp, fare … = price   credit 0.90
      ├─ typo tolerance      → yeild → yield                credit 0.80
      ├─ related class       → value ~ price                credit ≤ 0.55
      ├─ value type          → "$137" in a generic column   credit 0.62
      ├─ table caption       → generic column under "Wheat yield (t/ha)"  credit 0.65
      ├─ prose mention       → sentence says "prices" + $ value           credit 0.70
      └─ model-learned       → header the LLM mapped (bounded, cached)    credit 0.78
```

Guards stop false matches:
- **Unit compatibility:** `14.7%` can never be a *price*; a rate needs `%`.
- **Weak evidence must be strict:** a generic or related header needs the concept's own unit (a bare `1.8 million` is a count, not a price).
- **A header naming another measure wins:** dollars under a `change` column are not a price.
- **Prose isn't a value:** long text mentioning `$20,000` is text, and `under $20,000` is a bound.
- **Typos keep their first letter and length:** `yeild` → yield, but `heading` isn't `reading` and `charging` isn't `charge`.

The lexicon covers only the **generic vocabulary of quantitative data** (time,
money, quantities, rates, geography, identity…). Topic words such as *battery*,
*wheat* or *unemployment* match literally, by stem, or as acronyms (*EV* ↔ *electric
vehicle*, *BEV*/*PHEV* ⊃ *EV*).

### 4.6 Temporal resolution: *"which period does this row describe?"*

`vora/extraction/temporal.py::resolve_observation_period`, in precedence order:

```
 1. a time column in the row        (year, date, period, FY, "2024 Q2" …)   explicit  0.95
 2. a single period in another cell                                        explicit  0.80
 3. a single year in the measured column's name ("2011 census population") explicit  0.85
 4. the table caption / title row   ("…June 2026")                         explicit  0.80
 5. the nearest heading                                                    inferred  0.60
 6. the page title (single period only)                                    inferred  0.50
 7. structured temporalCoverage                                            inferred  0.55
 8. snapshot inference: modified_at → published_at → fetched_at            inferred  0.45 / 0.40 / 0.25
    (only if the row has a value, no period of its own, and the page
     isn't a multi-year series such as "2010–2025 trend")
```

**Page dates are provenance, not data.** A table of 2019–2022 prices on a page
modified in 2026 gives rows dated 2019–2022, not 2026.

Step 3 (basis `column_header`) exists because reference tables often name the year only in a
column header ("2011 census population", "density 2024 per km²").

### 4.7 Scoring: *"how much should we trust this row?"*

`vora/extraction/scoring.py::ObservationScorer`:

```
score = 0.30 × coverage       (required concepts present?)
      + 0.22 × relevance      (subject in the row / caption / page?)
      + 0.18 × temporal fit   (period inside the window? how sure?)
      + 0.12 × extraction     (method reliability, generic headers)
      + 0.10 × completeness   (optional enrichment, field richness)
      + 0.08 × source quality (.gov/.int/.edu, publisher dates present)
      − 0.60 × noise
```

```
                     ┌── role is not "data" ─────────────────► NOISE     (rejected)
 observation ──► ────┤
                     │   gates: measure present + on-subject + inside window
                     ├── gates ✓ and score ≥ 0.72 ─────────────► HIGH      (accepted)
                     ├── gates ✓ and score ≥ 0.55 ─────────────► USABLE    (accepted)
                     ├── score ≥ 0.38, has a value, relevant ──► PARTIAL   (kept for review)
                     └── otherwise ────────────────────────────► LOW       (kept, hidden)
```

For prose, the measure must **belong to the subject**: in "*gasoline* prices are
higher… USD 2" the price belongs to gasoline, so the row is partial. "*BEV pack*
prices… $99/kWh" is accepted because BEV is an EV and "pack" appears in the page's
own heading.

**Subject-named columns.** When a request names no measure word ("number of hospitals
per state", "IPL team wins"), the plan keeps the goal's own nouns as `subject_heads`
(hospital, win, rainfall…). A numeric column named after one ("Hospitals", "Wins",
"Rainfall (mm)") is evidence for the requested quantity, the same as a column called
"value". This never applies to money or rates. Identifier-like columns ("Hospital ID",
"Rank", "Code") never count.

**Blocks.** Rows are also judged per page block (`vora/extraction/blocks.py`). A block is a
table, a group of repeated cards, or the table of one tab.
- **Support:** the mean of three signals over the block's rows: measure evidence,
  relevance, and whether its values share one kind (all money, all percentages…).
- **Weak blocks:** below 0.30, the block's accepted rows are demoted to partial. A
  "related items" table where one row happens to pass no longer contributes.
- **Overlapping blocks:** when most of a block's values (period, series, number)
  already appear in a stronger block of the same site, the repeats are demoted.
  Examples are a table and the chart JSON behind it, or two tabs showing the same year.

Each scored observation gets a **normalized record** (`period · series · price ·
remaining fields`) and a **reasons** list, e.g.
`'price' ← average_pack_price (exact, 1.00)` and `Period 2019 (column 'year')`.

### 4.8 Persistence and live updates

`vora/storage/repository.py` (SQLite, WAL mode):

| Table | Holds |
|---|---|
| `instances` | research tracks (title, goal, archived, live mode) |
| `messages` | conversation (your goals, run summaries) |
| `runs` | each run's status, phase, detail, counts, times |
| `run_events` | every phase/detail change, used for the pipeline and timeline |
| `snapshots` | one JSON snapshot per track: plan, raw/accepted/partial/rejected, sources |
| `webhooks` | webhook subscriptions |

**Live updates** go over a **WebSocket** (`WS /instances/{id}/live/ws`,
`vora/output/live_hub.py`):
- **Run messages:** `run` (progress), `run_event` (each step) and `status`.
- **Rows:** `rows`, the accepted rows of each page, interactive pass or dataset *as it
  lands*, so the dataset and graphs grow during the batch.
- **End of batch:** `batch_complete`. The client then reloads once, because final
  scoring can move rows.

If WebSockets fail twice (e.g. a proxy), the client falls back to the server-sent
events stream (`GET /instances/{id}/live/stream`).

**Batches and live mode:**
- **Budget:** every run is a batch with a work budget of `VORA_BATCH_SECONDS` (300 s).
  Pages not reached are queued, and later batches read unread pages first.
- **Cadence:** with live mode on, a batch starts every `VORA_LIVE_INTERVAL_SECONDS`
  (300 s), counted from the previous batch's **start**, never before it finished. The
  scheduler checks every 5 s.
- **Rotation:** unread candidates rank first, so later batches reach more domains.
  Not every domain is read in the first batch.

**Diagnostics:** each batch ends with one line saying what happened, in the run
events and the chat. For example: `planner heuristic · search duckduckgo: 3 blocked;
bing: 3 ok · pages 4 complete, 2 empty, 1 blocked · 12 rows from the interactive
pass · 287s of 300s budget`.

**Graphs:** `GET /instances/{id}/graph` (`vora/output/graphs.py`) returns one chart per
numeric parameter. A parameter with two or more periods is a line chart, one line per
team, city or model. Anything else is a bar chart, e.g. population by city. The
**Graphs** view shows all parameters by default, with toggles.

**Re-score:** `POST /dataset/rescore` re-applies semantic scoring to stored raw
observations without fetching again. It also upgrades plans saved by older versions.

---

## 5. The frontend

The web app is a separate project (the private VORA repository): React, Vite, Tailwind,
static, deployed on Cloudflare Pages. It signs people in with Supabase (Google), then
calls this API's `/api/v1` routes with the access token in an `Authorization` header.
Its screens: New chat (with a live "how VORA reads this" preview from
`POST /goals/analyze?mode=fast`), a chat with tabs (Results, Review rows, Sources,
Graphs, Activity, Settings), a projects report, Ask database and Tools. Live progress
arrives as `status` events on `/live/stream`, read with `fetch()` because a header
cannot be sent from `EventSource`. See the Frontend repository's `docs/APP.md`.

---

## 6. File-by-file reference

### Entry & configuration

| File | What it does | Why |
|---|---|---|
| `app.py` | Starts Uvicorn with `vora.api.application:app`, prints the effective batch settings, stops cleanly on Ctrl+C | One command to run the app; a leftover `.env` override is visible |
| `vora/settings.py` | Reads `.env` into `AppSettings` (DB path, port, batch budget and cadence, candidates, search rationing and API, interactive pass, LLM keys and time limits) | One place for app-wide configuration |
| `vora/browser/settings.py` | `EngineSettings`: browser binary, headless, timeouts, fingerprint args, lean launch flags, media blocking | Keeps the engine independent of app config |

### Core (browser runtime)

| File | What | Why |
|---|---|---|
| `vora/browser/engine.py` | `BrowserEngine`: owns Chromium, renders a URL in an isolated context (images, video and fonts dropped), records network, emits lifecycle events | The only code that launches Playwright |
| `vora/browser/explore.py` | `explore`: safe, bounded interaction (scroll, tabs, "load more", next pages, query forms, postback pagers, iframes, shadow DOM, chart JSON) returning every page state | Data a plain render never shows |
| `vora/learning/recipes.py` | Recipe model and runner: ready steps, table, column map, ID check (dry run), postback/next paging, links built from the ID, stop at known records | Reads an official listing exactly, without search |
| `vora/browser/forms.py` | Types forms and their fields, classifies purpose, fills query forms from the request | Search-driven portals, without ever touching login/payment/contact forms |
| `vora/browser/events.py` | `EventBus`, `LifecycleEvent` | Lets other layers react without controlling the browser; isolates listener failures |
| `vora/browser/contracts.py` | `ExecutionResult`, `NetworkRecord` (immutable) | Rendered output can't be modified by consumers |

### Models

| File | What | Why |
|---|---|---|
| `vora/shared/contracts.py` | `GoalPlan`, `ConceptRequirement`, `TimeScope`, `Observation`, `SourceOutcome`, `ResearchSnapshot`, request DTOs | Shared contract between all layers; backward-compatible with snapshots saved by older versions |

### LLM

| File | What | Why |
|---|---|---|
| `vora/research/planning/provider.py` | `analyze_goal` (model draft + deterministic requirements), `heuristic_plan`, `map_fields` (cached header→concept mapping), provider fallback (NVIDIA → Gemini) | Models give **vocabulary**; the deterministic planner keeps **authority**. Works fully offline |

### Extraction

| File | What | Why |
|---|---|---|
| `vora/extraction/temporal.py` | Period parsing (years, quarters, months, FY, ranges, dates), request time windows, observation period precedence | One definition of time for planner, extractor and scorer; no LLM date arithmetic |
| `vora/extraction/requirements.py` | Goal → minimal required concepts, subject, time scope; grounding of model suggestions; legacy plan upgrade; query year alignment | Fixes "every desired field is mandatory" |
| `vora/extraction/semantics.py` | Lexicon, tokenizer, typo tolerance, value typing, unit compatibility, `ConceptMatcher` | Concept matching instead of literal field names |
| `vora/extraction/noise.py` | Challenge/login/consent detection, chrome class/role rules, JSON-LD metadata types, UI-widget and link-list rules | Removes garbage before costly scoring |
| `vora/extraction/parser.py` | Provenance, tables (captions, titles, unpivot, property sheets), JSON-LD, cards, prose with level/change/bound roles, legacy `unpivot_wide` | Turns rendered pages into raw observations with everything the scorer needs |
| `vora/extraction/records.py` | Record typing and assessment (naming field, typed attributes, coverage) | Accepts documents, listings and directories without a number |
| `vora/extraction/scoring.py` | Multi-factor score, gates, tiers, measure attribution in prose, normalized records, reasons | Replaces binary accept/reject with explainable grading |
| `vora/extraction/datasets.py` | Finds dataset links and chart embeds, ranks them by relevance, parses CSV into window-filtered rows | Gets the numbers behind charts from the publisher's own files |
| `vora/extraction/files.py` | Classifies every linked file, ranks relevance, parses CSV/XLSX/JSON/PDF in memory | Keeps every file as a reference; extracts on request |
| `extraction/sites/` | CarDekho and CarWale listing adapters (opt-in) | Better listings from sites users trust |
| `vora/extraction/blocks.py` | Block support and overlapping-block suppression | Judges a table or card group as a whole |
| `vora/extraction/numbers.py` | Reads "$54,669", "Rs. 14.49 Lakh", "down 20%" as numbers | Same rules as the interface, for graphs and blocks |
| `vora/extraction/collector.py` | Event listener that parses + scores each completed page; reports stages; optional LLM header learning | Keeps extraction event-driven and outside the engine |

### Services

| File | What | Why |
|---|---|---|
| `vora/research/coordinator.py` | Run lifecycle (submit/cancel/replace), pipeline stages, per-source processing, merging, summaries, live scheduler, rescore, orphan recovery | Orchestrates all layers in one place |
| `vora/output/datasets.py` | Downloads linked datasets (size/time limits, public URLs only) and publisher metadata | Keeps network access out of the extraction layer |
| `vora/research/discovery/ranking.py` | Scores candidates (preferred, proven, gave data in earlier research, suggested, data signals, rotation, blocks, often-blocking sites) with reasons | Reads the most useful unread pages first |
| `vora/research/discovery/discovery.py` | Search API or DuckDuckGo → Bing, `SearchGate` rationing, blocked/consent/unrelated-result detection, region hints, interleaved queries | Finds pages to read without getting the server treated as a bot |
| `vora/research/discovery/search_api.py` | Optional Brave Search / Google Programmable Search client | Dependable search for any browser |
| `vora/storage/repository.py` | SQLite tables, run events (with observers for live updates), active runs, live instances, snapshots, search cache, webhooks | Durable state with no extra infrastructure |
| `vora/learning/source_registry.py` | Loads `data/sources.json`, matches requests to official portals by name, alias or address | "egazette" goes to egazette.gov.in with no search |
| `vora/research/reading/browser_pool.py` | Bounded pool of browser workers: priorities, crash replacement, recycling, per-batch events | Many tracks share a few browsers |
| `vora/shared/cache.py` | `BoundedCache` (LRU + TTL) | No unbounded in-process dict |
| `vora/research/reading/deep_lane.py` | The interactive pass: its own thread and browser, a priority queue, focused crawl, host politeness gate, outbox | Runs beside the fast pass without sharing a browser |
| `vora/research/reading/crawl.py` | Ranks a page's same-site links against the request | Follows the few links that matter |
| `vora/output/live_hub.py` | Thread-safe fan-out of live messages to WebSocket clients | Rows and progress reach the browser as they happen |
| `vora/output/graphs.py` | Numeric parameters and one chart per parameter (line or bar) | The graph endpoints |
| `vora/output/tables.py` | Records → columns/rows, record metadata | One table shape for the dataset, stream and graphs |
| `vora/research/discovery/knowledge.py` | Per-site outcomes from this installation's history plus the committed `data/site_knowledge.json` (one section per installation) | A fresh clone ranks sources with everyone's experience |
| `scripts/export_site_knowledge.py` | Writes this installation's section of the knowledge file | Shares what runs learned, without goals or data |
| `install-browser.ps1` | Unpacks the released browser zip into `.runtime/voraBrowser` and sets `.env` | One command to get the browser on a new machine |
| `vora/shared/regions.py` | Country names → search regions | Region hints and place detection |
| `vora/shared/system.py` | Free memory on Windows, Linux and macOS | Decides whether the interactive pass's second browser can run |
| `vora/shared/urls.py` | `ensure_public_url`: blocks private/local targets, credentials and odd ports | Prevents the browser being pointed at internal networks (SSRF) |

### API

| File | What | Why |
|---|---|---|
| `vora/api/application.py` | All REST routes, dataset shaping (`records` with provenance), exports (+provenance), graphs, search status, WebSocket and SSE live streams, per-user ownership, CORS |
| `vora/api/auth.py` | Verifies a signed-in user's Supabase access token (public keys, issuer, audience, expiry) and returns the user id | Sign-in without holding a secret | Thin boundary: translates HTTP to services |

### Tests

| File | Covers |
|---|---|
| `tests/test_temporal.py` | Exactly-N windows, period formats, precedence, snapshot inference |
| `tests/test_semantics.py` | Aliases, synonyms, typos, units, value kinds, noise rules |
| `tests/test_planning.py` | Minimal requirements, model grounding, domain independence, legacy upgrade |
| `tests/test_extraction.py` | Parsing, provenance, unpivot, captions, prose roles, scoring and regressions from live runs |
| `tests/test_datasets.py` | Link discovery, ranking, CSV window/place filtering, wide files, download limits |
| `tests/test_ranking.py`, `test_discovery.py`, `test_coordinator.py`, `test_files.py` | Ranking and rotation, search parsing and site searches, backfill/merge/fresh runs, file parsing and site adapters |
| `tests/test_api.py` | Dataset records, rescore, rejected, run events, exports, fast planning, WebSocket stream and auth, 503 without a browser |
| `tests/test_bias.py` | Eight unrelated topics accepted without an LLM; ID/rank columns refused; counts in prose; no topic words in generic code |
| `tests/test_graphs.py`, `test_blocks.py`, `test_explore.py` | Graph parameters/lines/bars, block support and overlap, safe interaction (plus a real-browser test when a browser is configured) |
| `tests/test_engine.py`, `test_events.py`, `test_repository.py`, `test_settings.py` | Engine lifecycle, event isolation, storage, engine settings (lean flags) |
| `tests/test_auth.py` | Token checks (expired, wrong audience/issuer/key, unsigned), per-user tracks on every route, legacy owner claim, live-socket sign-in, CORS |

---

## 7. How we solved the hard parts

| Problem (seen in real data) | How it was solved |
|---|---|
| 153 EV observations → **0 accepted** because the plan demanded `make, model, price_usd, trim_level` | Plans hold **concepts**; only concepts in the user's words are required; the rest are optional |
| `average_pack_price` didn't count as *price* | Layered **concept matching** (head noun, synonyms, aliases, typos, value types) |
| LLM turned "last 10 years" into 2015–2025 (11 years) | **Deterministic time windows** in `temporal.py`; model queries re-aligned to the window |
| Page modified in 2026 would date historical rows as 2026 | **Period precedence**: row period first; page dates only for snapshots, marked *inferred* |
| Cloudflare pages, menus, JSON-LD `WebSite` blocks treated as data | **Early noise filter**; rejected rows are visible under *Filtered noise* |
| Wide tables (`1/2020 … 12/2024` columns) lost their meaning | **Unpivot** into one row per period; row labels kept as `series` |
| Table title row became a column header | Spanning single-value rows become the **caption** (and give an explicit period) |
| A whole sentence became the "price" value | Values must be short quantities; prose is text |
| `14.7%` and `1.8 million` counted as prices | **Unit compatibility** + strict evidence for weak headers |
| "$3,000 **less**", "**under** $20,000" treated as prices | Prose quantities classified as **level / change / bound** |
| "gasoline prices… USD 2" on an EV page | **Measure attribution**: the measure word's modifier must be the subject or the page topic |
| `heading` fuzzily matched `reading`; `charging` matched `charge` | Typo matching needs the same first letter and near-equal length |
| Old stored data couldn't benefit from fixes | **Re-score** endpoint + legacy plan upgrade + legacy unpivot |
| Legacy rows had no fetch time | Shown as **"Not recorded"**, never invented |
| SSE stream closed on finished runs, so browsers reconnected every few seconds | Stream stays open with keep-alives; the client backs off |
| Live toggle existed but did nothing | Real **live scheduler** in the coordinator |
| Agriculture run: FAO *production* sentences accepted as *yield* | Generic `value` columns get meaning only from units, row text or the caption |
| A stock headless Chrome got DuckDuckGo's "select all squares containing a duck" check and Bing results about Wimbledon for a hospitals query | Challenge text recognised; result pages unrelated to the query count as blocked; engines rest after a block; optional search API; no-search fallbacks |
| "Hospitals", "Wins", "Rainfall (mm)" columns never accepted; price data always was | **Subject heads**: a numeric column named after what the user counts is the measure (never IDs, ranks or codes); a bias test covers eight topics |
| Many pages showed data only behind tabs and "load more" | **Interactive pass** in parallel, with a safe-action whitelist and focused crawl |
| A cookie banner's settings JSON was accepted as 130 data rows | Captured JSON must mention the request's words and never comes from consent/analytics endpoints; each response is its own block |
| Census 2011 rows dated 2026 from the page date | A single year in the measured column's name is the row's period |
| "There are 3,961 villages" accepted as population | A sentence's counted noun must be what the request measures |
| Planning waited 152 s for a slow model | The model gets 20 s; later batches of the same goal reuse the day's plan |
| Five JavaScript-only pages ended a run with nothing | Pages with no rows are `empty` and don't use up the page budget |
| Two browsers per batch on a machine with 2.9 GB free | Media blocked, lean launch flags (~23 % less per browser), second browser started lazily, skipped when memory is short |

**Approach:** every fix above came from **reading the actual accepted rows** of live
runs, not just counts, then writing a regression test before moving on.

---

## 8. Data model

### Observation (one extracted row)

| Group | Fields |
|---|---|
| Identity | `id`, `source_url`, `method`, `fields` (raw, as extracted) |
| Provenance | `source_domain`, `source_title`, `published_at`, `modified_at`, `fetched_at`, `extraction_confidence`, `context`, `context_kind` |
| Time | `data_period`, `period_start`, `period_end`, `period_granularity`, `time_basis`, `time_inferred`, `temporal_confidence` |
| Judgement | `status` (raw/accepted/partial/rejected), `content_role`, `tier`, `score`, `score_breakdown`, `concept_matches`, `reasons`, `noise_probability` |
| Output | `normalized` (period · series · requested concepts · other fields) |
| Block | `block_id` (`table#2`, `cards:…`, `tab: 2023/table#1`, `json:/api/…`), used by block support |

`method` is one of `html_table`, `json_ld`, `repeated_region`, `text_statement`,
`undated_statement`, `network_json`, `site_adapter`, `linked_dataset`, `spreadsheet`,
`structured_data`, `pdf_table`, `pdf_text`, `page_summary`.

### Source outcome (one per page read or queued)

| Field | Meaning |
|---|---|
| `status` | `complete` (accepted rows) · `partial` · `empty` (read, nothing usable) · `blocked` · `failed` · `skipped` (queued for a later batch) |
| `origin` | `goal` · `preferred` · `search` · `crawl` (followed link) · `linked_dataset` · `file` |
| `rank_score`, `rank_reasons` | Why it was read in this order |
| `deep_accepted`, `deep_notes` | What the interactive pass added and did ("opened tab '2023'") |
| `linked_from`, `run_id` | Where a followed link came from; which batch read it |

### Snapshot (one per track)

`plan` (incl. `subject_heads`, `suggested_sources`) · `raw` · `accepted` · `partial` · `rejected` ·
`sources` · `files` · `candidate_count` · `outcome` · `scored_at` · `updated_at`

---

## 9. Configuration

`.env` (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `VORA_BROWSER_BINARY` | — | Absolute path to a Chrome or Chromium executable (a fingerprint-configured build is blocked least) |
| `VORA_HEADLESS` | `true` | Headless rendering |
| `VORA_NAVIGATION_TIMEOUT_MS` / `VORA_NETWORK_IDLE_TIMEOUT_MS` | 30000 / 5000 | Page load limits |
| `VORA_MAX_CANDIDATES` | 40 | Candidates collected from search (ranked before opening) |
| `VORA_MAX_SOURCES` | 8 | Usable pages read per batch (blocked, failed and empty pages are replaced) |
| `VORA_MAX_PER_DOMAIN` | 1 | Pages per site before other sites (preferred sites: +1) |
| `VORA_REVISIT_HOURS` | 24 | A useful page is re-read only after this long |
| `VORA_BLOCK_COOLDOWN_DAYS` | 7 | How long a site that blocked us stays demoted |
| `VORA_MERGE_RUNS` | true | Runs of the same goal add to the dataset instead of replacing it |
| `VORA_MAX_RAW_OBSERVATIONS` | 5000 | Stored raw rows per track (newest kept) |
| `VORA_SITE_ADAPTERS` | true | CarDekho / CarWale listing adapters |
| `VORA_PORT` / `VORA_HOST` | 8000 / 127.0.0.1 | Server address |
| `VORA_API_KEY` | empty | If set (and `VORA_AUTH` is not `supabase`), callers need it (header or session cookie) |
| `VORA_AUTH` | derived | `supabase`, `api_key` or `none` |
| `SUPABASE_URL` / `SUPABASE_JWT_AUDIENCE` / `SUPABASE_JWT_SECRET` | empty / `authenticated` / empty | Project whose access tokens are accepted (the secret only for older HS256 projects) |
| `VORA_CORS_ORIGINS` | empty | Web origins allowed to call the API |
| `VORA_LEGACY_OWNER` | empty | Supabase user id that owns tracks made before sign-in |
| `LLM_MODEL` / `LLM_FALLBACK_MODEL` | NVIDIA Nemotron / Gemini | Planning vocabulary and header mapping |
| `NVIDIA_API_KEY`, `GEMINI_API_KEY` | — | Provider credentials |
| `LLM_TIMEOUT_SECONDS` | 45 | Model call limit |
| `LLM_COOLDOWN_SECONDS` | 600 | After a model fails or times out, it's skipped this long and the fallback is used directly |
| `VORA_SEMANTIC_LLM` | `true` | Allow model help for unplaceable headers |
| `VORA_LIVE_INTERVAL_SECONDS` | 300 | Live batches start this long after the previous batch started |
| `VORA_BATCH_SECONDS` | 300 | Work budget of one batch; the rest is queued |
| `VORA_SEARCH_CACHE_MINUTES` | 60 | Reuse search results while unread candidates remain |
| `VORA_DEEP_LANE` | true | Interactive pass beside the fast pass (browser from the shared pool) |
| `VORA_FORMS` | true | Let the interactive pass fill and submit query forms |
| `VORA_MAX_CONCURRENT_BATCHES` | 4 | Tracks working at once |
| `VORA_BROWSER_POOL` | 2 | Browsers shared by all tracks |
| `VORA_BROWSER_RECYCLE_AFTER` | 200 | Pages a browser serves before it is restarted |
| `VORA_RETENTION_EVENT_DAYS` / `_RUN_DAYS` / `_HISTORY_DAYS` | 30 / 90 / 180 | How long run events, finished runs and per-page history are kept |
| `VORA_CRAWL_PER_SITE` | 3 | Same-site pages followed per page (one level deep) |
| `VORA_BLOCK_SUPPORT` | true | Demote rows from blocks that don't carry the request |
| `VORA_SEARCH_COOLDOWN_MINUTES` | 15 | Rest for an engine that blocked us (doubles on repeats, ≤ 4 h) |
| `VORA_SEARCH_INTERVAL_SECONDS` | 5 | Minimum gap between browser searches |
| `VORA_MAX_SEARCHES_PER_HOUR` | 30 | Browser searches per hour, all tracks |
| `VORA_SEARCH_API` / `_KEY` / `_CX` | empty | Optional Brave or Google search API, used before browser searches |
| `VORA_LLM_PLAN_SECONDS` | 20 | Planning waits this long for the model, then plans deterministically |
| `VORA_DEEP_LANE_MIN_FREE_MB` | 1500 | Skip the interactive pass when less memory is free |
| `VORA_BLOCK_MEDIA` | true | Don't download images, video, audio or fonts |
| `VORA_LEAN_BROWSER` | true | Fewer background browser services (~20 % less memory per browser) |
| `VORA_SITE_KNOWLEDGE` | `data/site_knowledge.json` | Shared per-site outcomes used by ranking (see `scripts/export_site_knowledge.py`) |
| `VORA_MAX_LINKED_DATASETS` | 2 | CSV datasets downloaded per run from links on rendered pages |
| `DATABASE_PATH` | `./vora.db` | SQLite file |

---

## 10. Running and testing

```powershell
python app.py                                  # http://127.0.0.1:8000  (API docs at /docs)
                                               # prints e.g. "Batches: 40 candidates, 8 pages, 300s budget,
                                               #   live every 300s | interactive pass on | search: browser (max 30/hour)"
                                               # start it this way: Ctrl+C then closes live streams cleanly

python -m unittest discover -s tests           # backend tests (the real-browser test runs when a browser is configured)
```

After upgrading, open a track's **Settings → Re-score stored observations** (or
**Run again**) so older data is judged by the current logic.

---

## 11. What VORA does not do

**By design**
- **It doesn't click anything that could change state:** no sign-ins, payments, contact/subscribe/upload forms, downloads or links to other sites. Search and filter forms are operated, with values taken only from your request.
- **It doesn't bypass Cloudflare, CAPTCHAs or other bot protection.** Challenge
  pages, and search engines' challenges, are detected, marked *Blocked*, and kept out
  of the dataset. A fingerprint-configured browser build is challenged less, but
  access is never guaranteed.
- **It doesn't guarantee facts are true.** *Accepted* means the row matches your
  request and is well-evidenced on its source page, not that the source is correct.
- **It doesn't let the LLM decide requirements or do date maths.** Models only
  suggest vocabulary and queries.
- **It doesn't mix partial rows into the dataset silently.** They appear only in the
  *Partial* view or when you turn on *Include partial*.
- **It doesn't hard-code topic-specific logic.** Nothing says "if EV then…".

**Current limitations**
- **Ranking is lexical:** it uses titles, snippets, URLs and history, not the page contents, so a
  well-titled page can still turn out to have no data (it's then demoted by history).
- **Word, PowerPoint, old Excel (.xls) and ODS files are kept as links only**; scanned PDFs have no
  readable text (no OCR).
- **Charts drawn from data embedded in the page's scripts** (not loaded as JSON/CSV) can't be read; charts that load their data, Our World in Data charts and linked CSVs are handled.
- **The interactive pass is bounded and cautious:** at most 25 safe actions and 30 s per page, never logins, payments or other sites; content behind a login or a CAPTCHA stays out of reach.
- **Site adapters exist only for CarDekho and CarWale**; other sites use the general extractors.
- **Prose understanding is lexical:** subtle sentences can still be misread (one
  live example: a "USD 47 subsidy" sentence was accepted as a price).
- **Webhooks are stored but never delivered**, and **access circuits are
  placeholders** (always empty).
- **The shared cache** is a view of per-track snapshots; there's no freshness TTL
  or reuse across similar goals.
- **Scale is in-process:** tracks run concurrently (`VORA_MAX_CONCURRENT_BATCHES`) on a shared browser pool, with SQLite and in-memory live updates. Thousands of tracks need the queue, database and event redesign described in the architecture plan (not built yet).
- **Search engines and automated browsers:** DuckDuckGo and Bing challenge, or answer with unrelated results, searches from browsers they judge automated. This happens always for a stock headless Chrome, and after heavy use even for a configured build. Rationing, the cache and the no-search fallbacks keep batches working, and blocks are reported. A search API key avoids the problem.
- **Memory:** each browser peaks around 400 MB on heavy pages, even lean and with media blocked, and the pool keeps at most `VORA_BROWSER_POOL` of them. Below `VORA_DEEP_LANE_MIN_FREE_MB` of free memory the interactive pass is skipped.
- **Light theme only**, matching the vora reference design.
