# VORA API

The authoritative live schema is available at `/openapi.json` and the
interactive documentation at `/docs`. Who may call is set by `VORA_AUTH`:

- `supabase`: `Authorization: Bearer <Supabase access token>` on every protected
  route (401 otherwise). Each track, webhook, saved result and favoured-site list
  belongs to the signed-in user; someone else's track answers 404. A WebSocket
  authenticates with a first message `{"type": "auth", "token": "..."}`.
- `api_key`: `X-API-Key`, or an HTTP-only session through `/api/v1/auth/session`
  (its GET reports `authentication_required` and `mode`).
- `none`: no check; tracks are shared.

Browsers on another origin need it listed in `VORA_CORS_ORIGINS`. `GET /` returns
a small JSON description; the web app is a separate site.

## Operations

- Operations: `/health`, `/ready`, `/version`, `/metrics`, `/api/v1/capabilities`
  (includes `pipeline_phases`, `extractors`, `source_statuses` and every batch,
  search and interactive-pass setting)
- Readiness: `/ready` returns 200 or 503 with `reason`, plus `search` (`browser`,
  `brave` or `google`), `free_memory_mb`, `interactive_pass` (false when memory is
  below `VORA_DEEP_LANE_MIN_FREE_MB`), `browsers` (the shared pool: `size`, `started`,
  `busy`, `queued`, `tasks_done`, `recycled`, `restarts`, `active_batches`,
  `max_concurrent_batches`) and the configured language models
- Search: `GET /api/v1/search/status`: the search API in use, each browser engine's
  `resting_seconds` after a block and `blocks_in_a_row`, `searches_last_hour` of
  `max_searches_per_hour`, spacing and cooldown (shared by all tracks)
- Instances: create, list, read, update, delete, duplicate, and dashboard
- Messages: list and submit; submitting starts a background research run and
  replaces any active run of the same instance
- Runs: create, list, read, cancel, retry, refresh, live state, WebSocket and SSE
  streams, and `GET /instances/{id}/runs/{run_id}/events` (the recorded pipeline
  steps). Starting or retrying a run returns `503` with the reason when no usable
  browser is configured
- Data: accepted dataset, raw / partial / rejected observations, provenance,
  schema, statistics, deletion, `POST /dataset/rescore`, CSV/JSON/JSONL export, and
  graphs (`/graph/parameters`, `/graph`)
- Research: goal analysis (`?mode=fast` skips the language model), sources,
  source detail (with its observations), preferred sources (global and per track),
  source reputation, files (list and on-request extraction), report, and freshness
- Extractors: list, inspect, test against supplied HTML, and regenerate metadata
- Cache: list, inspect, invalidate, and delete persisted research snapshots
- Access circuits: list and clear compatibility controls
- Webhooks: create, list, read, update, delete, and delivery history
- Authentication: create, inspect, and delete a browser session

The `/api` instance routes remain available as compatibility aliases. New
clients should use `/api/v1`.

## Dataset shape

`GET /api/v1/instances/{id}/dataset` returns `columns`, `rows` and an aligned
`records` array. Rows hold normalized values (`period`, `series`, the requested
concepts, then remaining fields). Each record holds the interpretation and
provenance of its row: `tier`, `score`, `score_breakdown`, `concept_matches`,
`reasons`, `data_period`, `time_basis`, `time_inferred`, `temporal_confidence`,
`source_url`, `source_title`, `published_at`, `modified_at`, `fetched_at`,
`method`, `extraction_confidence` and `block_id` (the table, card group, tab state
or loaded JSON the row came from). `time_basis` may be `column_header` when the
period is a year in the measured column's name ("2011 census population").

## Sources

`GET /api/v1/instances/{id}/sources` lists every page a batch read or queued:

- **`status`:** `complete`, `partial`, `empty` (read, nothing usable), `blocked`,
  `failed` or `skipped` (queued for a later batch).
- **`origin`:** `goal`, `preferred`, `search`, `crawl` (a same-site link followed by
  the interactive pass), `linked_dataset` or `file`.
- **Interactive pass:** `deep_accepted` (rows it added) and `deep_notes` (what it
  did, e.g. "opened tab '2023'").
- **Ranking and origin:** `rank_score`, `rank_reasons`, `linked_from` and `run_id`. `include_partial=true` adds partial rows
explicitly. Exports accept `provenance=true` to append provenance columns.

## Live updates

### WebSocket (preferred)

`WS /api/v1/instances/{id}/live/ws` sends JSON text messages:

| `type` | Payload | When |
|---|---|---|
| `hello` | `state` (live state), `events` (latest run's steps so far) | On connect |
| `status` | `state` | The latest run changed status, and at the end of a batch |
| `run` | `run` (progress counters, phase, detail) | Every run update |
| `run_event` | `event` (`id`, `run_id`, `phase`, `detail`, `created_at`) | Each pipeline step |
| `rows` | `run_id`, `lane` (`fast`, `deep`, `dataset`), `source_url`, `columns`, `rows`, `records`, `partial_added`, `accepted_total` | Accepted rows as each page, interactive pass or dataset lands |
| `batch_complete` | `run_id`, `refetch: true` | The batch ended. Reload the dataset: final scoring can move streamed rows |
| `resync` | `reason` | The client fell behind; reload |
| `ping` | — | Every 20 seconds |

Live state includes `interval_seconds` (live batches start this long after the
previous batch started) and `batch_seconds` (each batch's work budget). With
`VORA_API_KEY` set, sockets authenticate with the session cookie or
`?api_key=`; an invalid key closes the socket with code 1008.

### Server-sent events (fallback)

`GET /api/v1/instances/{id}/live/stream` sends `status` events (live state and
the latest run) whenever they change, `run_event` events for each pipeline
step, and keep-alive comments. It stays open across runs for up to 15 minutes;
clients reconnect afterwards.

## Graphs

`GET /api/v1/instances/{id}/graph/parameters` lists every numeric parameter of
the dataset: `name`, `unit`, `points`, `x_kind` (`period` when rows span two or
more periods, else `category`), `period_min`, `period_max`, `series_count`, and
the text `dimensions` usable for grouping.

`GET /api/v1/instances/{id}/graph` returns one chart per parameter:

- **Parameters:** `parameters=a,b` (comma-separated, default all), `include_partial`,
  `from` and `to` (period prefixes, e.g. `2020`), and `series` (series or bar labels
  to keep).
- **Chart fields:** `parameter`, `unit`, `type` (`line` or `bar`), `x`, `group`, and
  `series: [{name, points: [{x, value, raw, observation_id, source_url}]}]`.
- **Counts:** `omitted` (series or bars not shown) and `duplicates` (repeated
  values for one point, kept once).
- **Chart types:**
  - A **line** chart has one line per value of the best grouping column (team,
    city, model…), at most 8.
  - A **bar** chart ranks values by that column, at most 30.

## Data guarantees

`dataset/raw` contains observations immediately after parsing rendered output.
`dataset/partial` contains relevant observations that lack enough evidence
(missing measure, outside the requested period, weak relevance).
`dataset/rejected` contains page noise filtered before semantic scoring.
`dataset` and exports contain accepted observations only unless
`include_partial=true` is requested. An accepted record passed semantic scoring;
it is not a guarantee that the source's factual claim is independently correct.
