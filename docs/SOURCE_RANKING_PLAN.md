# VORA: Trusted Sources, Smarter Ranking, Rotation & Merging

*Implementation plan for the VORA research engine.*

> **Status: implemented (2026-09-29).** Differences from this plan, found while testing live:
> - A **revisit window** was added (`VORA_REVISIT_HOURS`, 24 h): a page read recently ranks last (−150), otherwise
>   proven and preferred pages kept winning and the queue was never read. Proven still boosts a site's *other* pages.
>   Unread pages still come first.
> - The file registry from the "store every file as a link" discussion was built at the same time (see
>   `VORA_WORKFLOW.md` §4.4c).
> - Live result: with `cardekho.com` preferred, *"EV car prices in India"* read CarDekho first; later runs read the
>   queued sites and the merged dataset grew 87 → 104 → 218 accepted rows.
>
> **Later changes (batches, interactive pass, search rationing):**
> - **Batches:** runs became 5-minute batches, and live batches start every 5 minutes.
>   - Defaults rose to `VORA_MAX_CANDIDATES=40` and `VORA_MAX_SOURCES=8`.
>   - Pages with no usable rows are `empty` and no longer count toward the page budget.
> - **Search results** are cached per goal (60 min) while unread candidates remain.
>   - Engines are rationed: they rest after a block, searches are spaced, and there is an
>     hourly cap. An optional search API can replace browser searches.
>   - Without search results, a batch starts from old results, earlier useful pages, then
>     preferred sites' home pages.
> - **Interactive pass:** it follows up to 3 matching same-site links per page. These appear
>   as origin `crawl` and are never the ranked candidates themselves.
> - **"price" was removed from the ranking's data words** (topic neutrality).
>
> See `VORA_WORKFLOW.md` §4.2 and §4.3b. The settings table below shows the values when this plan was implemented.

---

## 1. The problem

When VORA researches a goal, it searches the web and then opens a handful of pages
with its browser. Today that selection is naive.

**Current behaviour** (`vora/research/discovery/discovery.py` and `vora/research/coordinator.py`):

1. It collects up to **12** URLs, in the exact order the search engine returned them.
2. It opens the **first 5** and lists the rest as "Skipped candidates — Beyond the source budget".
3. It **replaces** the whole dataset with the results of that run.

**What goes wrong:**

| Problem | Example from real runs |
|---|---|
| Search order ≠ data quality | A news article can outrank a page with a price table |
| No trust | Sites users rely on (e.g. **cardekho.com**) get no preference |
| Same site takes several slots | `nass.usda.gov` was opened twice in one agriculture run |
| Blocked sites are picked again | `iea.org` showed a Cloudflare challenge in *every* run |
| A blocked page uses up a slot | One Cloudflare page leaves only 4 useful pages out of 5 |
| Skipped sites are forgotten | `iseecars.com` supplied most of the rows in one run and was skipped in the next |
| Every run starts from zero | Data from earlier runs is thrown away |

## 2. The goal

VORA should open **the most useful pages first**, learn from experience, and build a
dataset that **grows** over runs:

- Sites **you trust** are read first.
- Sites the **planner knows are authoritative** for the topic get a boost.
- Sites that **produced good data before** are preferred, and sites that **blocked us** are pushed down.
- Each run reads pages **not read before**, and results are **merged** into the existing dataset.
- **CarDekho and CarWale** get dedicated extractors (ported from `vora`) so their listings are captured well.

This does not bypass Cloudflare or CAPTCHAs. Blocked pages are detected, marked
**Blocked**, demoted in future runs, and replaced with the next candidate.

---

## 3. How it will work

### 3.1 Where trust comes from

```
                      ┌─────────────────────────────┐
  You (Settings/Tools)│ Preferred sources           │  +100
                      │  • Global list (all tracks) │
                      │  • Per-track list           │
                      ├─────────────────────────────┤
  Past runs           │ Proven in this track        │  +40
                      │ (produced accepted rows)    │
                      ├─────────────────────────────┤
  Planner (LLM)       │ Suggested for this topic    │  +25
                      │ e.g. FAO for crop yields    │
                      ├─────────────────────────────┤
  Search result text  │ Looks like data             │  up to +15
                      │ ("statistics", "price list",│
                      │  .gov / .int / .edu …)      │
                      │ Mentions subject / measure  │  up to +10
                      ├─────────────────────────────┤
  Rotation            │ Never read in this track    │  +20
                      │ Read before, gave nothing   │  −40
                      ├─────────────────────────────┤
  Reputation          │ Blocked us recently         │  −60
                      ├─────────────────────────────┤
  Search engine       │ Its own rank position       │  −1.5 per place
                      └─────────────────────────────┘
```

**Trusted means read first, not accepted blindly.** Every row from every site still
passes VORA's semantic scoring (measure, subject, time period, noise checks). A
trusted site with no useful data for your goal will not pollute the dataset.

### 3.2 Preferred sources: global + per track

- **Global list** (Tools page): applies to every track, e.g. `fao.org`, `ourworldindata.org`.
- **Per-track list** (Settings page): only for that track, e.g. `cardekho.com`, `carwale.com` for car-price research.
- A track uses **global ∪ its own list**.

For each preferred site, VORA runs a **site-restricted search**
(`site:cardekho.com EV car prices …`), so it finds the *relevant page* on that site
rather than its homepage.

### 3.3 Planner suggestions

The language model already writes search queries. It will also return **well-known
sites that publish this kind of data for the region** (as plain domains, max 6).
These are only a ranking boost, are shown in the UI, and can be added to your
preferred list with one click.

### 3.4 Learning from experience (source history)

Every opened page is recorded: domain, outcome (complete / partial / blocked /
failed), and how many rows were accepted. This gives VORA a memory:

- **Proven sources** (per track): domains that produced accepted rows before.
- **Blocked recently** (global): a domain that showed a challenge page within the
  last 7 days is demoted for every track.
- **Visited URLs** (per track): used for rotation.

### 3.5 Diversity and backfill

- **One page per site per run** by default (two for preferred sites), so a single
  domain can't take all the slots.
- **Backfill:** if a page is **Blocked** or **Failed**, VORA opens the next ranked
  candidate. It keeps going until it has **5 usable pages**, with a hard stop after
  10 attempts so a run where everything is blocked still ends quickly.

### 3.6 Rotation and merging

- Candidates not opened this run stay listed as **"Queued for next runs"**, with their
  score and reasons, and are favoured next time because they're unread.
- **Same goal, new run → merge.** New rows are added to the existing dataset. Pages
  re-read this run replace their old rows (fresh data wins). Everything is then
  re-scored with the current plan so all rows are judged the same way.
- **Different goal → fresh dataset**, same as today.
- **Clear dataset** still resets everything.
- Size cap: 5,000 raw observations (oldest dropped first).

Over a few runs (manually or with **Live mode**) the dataset grows to cover all the
good sources instead of just whichever five ranked highest that day.

### 3.7 CarDekho & CarWale adapters

The `vora` project already has dedicated extractors for these sites
(`vora/extractors/sites/cardekho.py`, `carwale.py`). They will be ported to VORA as
**optional site adapters**:

- They run **only** on those domains, **in addition to** VORA's general extractors.
- They produce rows like `brand · model · price · range`.
- Indian number formats (`₹12.49 Lakh`, `Rs. 1.2 Cr`) are understood for sorting and charts.
- They can be switched off with `VORA_SITE_ADAPTERS=false`.
- Their rows still go through normal scoring.

---

## 4. What you'll see in the app

| Screen | New |
|---|---|
| **Settings** | "Preferred sources" editor for this track (add/remove domains); global ones shown read-only |
| **Tools** | "Global preferred sources" editor; **Source reputation** table (domain, runs, accepted rows, last blocked) |
| **Sources** | Each page card shows *why* it was chosen: badges **Preferred / Proven / Suggested / Blocked before**, plus reasons like "Proven: 20 rows last run" |
| **Sources** | "Skipped candidates" becomes **"Queued for next runs"**, sorted by score, with **Add to preferred** |
| **Overview → Research pulse** | "Suggested sources" from the planner, each with **Add to preferred** |

---

## 5. Implementation steps

| # | Area | Files | What changes |
|---|---|---|---|
| 1 | Storage | `vora/storage/repository.py` | New tables `preferred_sources` (global or per track) and `source_history` (every opened page). Created automatically on startup; existing databases keep working. |
| 2 | Search results | `vora/research/discovery/discovery.py` | Parse **title + snippet + rank** from DuckDuckGo/Bing (not just URLs); run `site:` searches for preferred domains; collect ~20 candidates. |
| 3 | Ranking | **new** `vora/research/discovery/ranking.py` | Pure scoring function (table in 3.1), domain diversity, human-readable reasons. |
| 4 | Planner | `vora/research/planning/provider.py`, `vora/shared/contracts.py`, `vora/shared/urls.py` | LLM returns `suggested_sources`; domains normalized and validated. |
| 5 | Run loop | `vora/research/coordinator.py` | Use ranking; backfill blocked/failed pages; record history; merge with the previous dataset when the goal is unchanged. |
| 6 | Site adapters | **new** `extraction/sites/` + `vora/extraction/parser.py` | Port CarDekho/CarWale extractors; run them alongside the general ones. |
| 7 | API | `vora/api/application.py` | `GET/PUT /api/v1/sources/preferred`, `GET/PUT /api/v1/instances/{id}/preferred-sources`, `GET /api/v1/sources/reputation`. |
| 8 | Frontend | `api/static/src/...` | Domain-list editor component; Settings/Tools/Sources/Overview updates; `₹ Lakh/Crore` number parsing. |

### New settings (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `VORA_MAX_CANDIDATES` | `20` (now `40`) | Search results collected per run (was 12) |
| `VORA_MAX_SOURCES` | `5` (now `8`) | Usable pages opened per run |
| `VORA_MAX_PER_DOMAIN` | `1` | Pages per site per run (preferred sites: 2) |
| `VORA_BLOCK_COOLDOWN_DAYS` | `7` | How long a blocking site stays demoted |
| `VORA_MERGE_RUNS` | `true` | Merge runs of the same goal instead of replacing |
| `VORA_SITE_ADAPTERS` | `true` | Enable the CarDekho/CarWale adapters |

---

## 6. Worked example

Goal: **"EV car prices in India"**, track preferred sources: `cardekho.com`, `carwale.com`.

**Run 1**
1. Planner: required *price* (+ period if asked); suggests `cardekho.com`, `carwale.com`, `autocarindia.com`.
2. Discovery: `site:cardekho.com EV car prices…`, `site:carwale.com …`, then normal queries → ~20 candidates.
3. Ranking picks CarDekho (+100 preferred, +25 suggested), CarWale, then the best-looking data pages; one per site.
4. One page is Cloudflare-blocked → marked **Blocked**, next candidate opened instead.
5. Dataset: CarDekho/CarWale listings (brand, model, ₹ price) + other accepted rows, each with its source and period.

**Run 2 (same goal)**
- CarDekho/CarWale are **Proven** (+40) and still preferred, so they're refreshed.
- Last run's queued candidates are **unread** (+20) and get opened.
- The blocked site is **demoted** (−60).
- New rows are **merged**; the dataset grows.

---

## 7. Testing

**Automated**
- `tests/test_ranking.py`: preferred beats search order, one page per site, blocked demoted, unread beats unproductive, proven boost, reasons present.
- `tests/test_discovery.py`: DuckDuckGo/Bing result parsing (title/snippet/URL) and `site:` queries for preferred domains.
- `tests/test_coordinator.py`: blocked page backfilled; same-goal run merges; new goal starts fresh.
- `tests/test_sites.py`: CarDekho/CarWale sample pages produce accepted `model + price` rows.
- `tests/test_api.py`: preferred-source endpoints (global and per track), bad-domain validation, reputation.
- Frontend: `toNumber("₹12.49 Lakh")` = 1,249,000.

Run with:
```powershell
python -m unittest discover -s tests
node --test tests/frontend/utils.test.mjs
```

**Live check** (on a copy of the database, port 8001)
1. Add `cardekho.com` to a track's preferred sources and run "EV car prices in India".
2. Confirm Sources shows CarDekho first with "Preferred source", and CarDekho rows in the dataset.
3. Click **Run again**: queued candidates get opened, the dataset grows, and a previously blocked site shows "Blocked recently".
4. Check the Settings/Tools editors and Sources badges with no browser console errors.

---

## 8. What this plan does *not* do

- It doesn't bypass Cloudflare, CAPTCHAs or other bot protection. Blocked pages are
  respected, recorded and routed around.
- It doesn't hard-code topic-specific sites into the engine. Specific sites enter only
  through **your** preferred lists, **planner suggestions** (boost only), **measured
  history**, and the two opt-in **site adapters** you approved.
