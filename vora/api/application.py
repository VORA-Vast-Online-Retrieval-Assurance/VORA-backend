"""FastAPI surface for research orchestration and datasets."""

from __future__ import annotations

import asyncio
import threading
import csv
import hashlib
import io
import json
import time
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Any

from fastapi import (
    APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket,
    WebSocketDisconnect, WebSocketException, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.requests import HTTPConnection

from vora.settings import settings
from vora.api.auth import AuthError, bearer_token, current_user, verifier
from vora.browser.contracts import ExecutionResult
from vora.extraction.parser import parse_rendered_page
from vora.research.planning.provider import analyze_goal
from vora.extraction.files import EXTRACTABLE
from vora.shared.contracts import CreateInstanceRequest, DomainList, Observation, SendMessageRequest, UpdateInstanceRequest, WebhookCreate, WebhookUpdate
from vora.storage.repository import Repository
from vora.research.coordinator import ResearchCoordinator
from vora.research.coordinator import PHASES, TERMINAL
from vora.research.discovery import search_api
from vora.research.discovery.discovery import search_gate
from vora.output.graphs import graph_data, graph_parameters
from vora.output.tables import build_table, record_meta
from vora.shared.urls import ensure_public_url, normalize_domain

repository = Repository(settings.resolved_database_path())
# Set when the server is asked to stop, so live streams close instead of
# holding the shutdown open (see app.py).
shutting_down = threading.Event()
coordinator = ResearchCoordinator(repository)
VERSION = "0.2.0"
EXTRACTORS = ("html_table", "json_ld", "repeated_region", "text_statement", "undated_statement",
              "network_json", "hydration_json", "recipe", "page_summary")
PROVENANCE_COLUMNS = ("source_url", "data_period", "time_inferred", "temporal_confidence",
                      "tier", "score", "published_at", "modified_at", "fetched_at")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.legacy_owner:
        repository.claim_legacy(settings.legacy_owner)
    coordinator.recover_orphans()
    coordinator.start_scheduler()
    yield
    coordinator.shutdown()


app = FastAPI(title="VORA", description="Vast Online Retrieval & Assurance", version=VERSION, docs_url="/docs", lifespan=lifespan)
router = APIRouter()


def install_cors(target: FastAPI, origins: tuple[str, ...]) -> None:
    """Let the web app (another origin, e.g. Cloudflare Pages) call the API from a browser.

    Identity travels in the Authorization header, never in cookies, so credentials stay off.
    """
    if origins:
        target.add_middleware(
            CORSMiddleware, allow_origins=list(origins), allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key"],
            expose_headers=["Content-Disposition"], max_age=600)


install_cors(app, settings.cors_origins)

# What a CDN in front of the API (e.g. Cloudflare) may keep. Everything a user owns is private and never stored;
# only facts that are the same for everyone may be cached at the edge for a few minutes.
PUBLIC_PATHS = {"/version": 300}


@app.middleware("http")
async def cache_policy(request: Request, call_next):
    response = await call_next(request)
    if "cache-control" not in response.headers:
        seconds = PUBLIC_PATHS.get(request.url.path)
        response.headers["Cache-Control"] = (f"public, max-age={seconds}" if seconds and request.method == "GET"
                                             else "private, no-store")
    return response


async def authorized(request: HTTPConnection, x_api_key: str | None = Header(default=None)) -> None:
    """Who may call, by ``VORA_AUTH``.

    * ``none``: anyone.
    * ``api_key``: one shared key (header or session cookie; a WebSocket may pass ``?api_key=``).
    * ``supabase``: a signed-in user's access token in ``Authorization: Bearer``. The user is
      remembered for the rest of the request (see ``owner_scope``). A WebSocket cannot send
      headers, so it proves itself with a first message instead (see ``live_socket``).
    """
    mode = settings.auth_mode
    if mode == "none":
        return
    is_socket = request.scope["type"] == "websocket"
    if mode == "supabase":
        if is_socket:
            return
        try:
            user = await asyncio.to_thread(
                verifier.verify, bearer_token(request.headers.get("authorization")), settings)
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}) from exc
        current_user.set(user)
        return
    supplied = {x_api_key, request.cookies.get("vora_session")}
    if is_socket:
        supplied.add(request.query_params.get("api_key"))
        if settings.api_key not in supplied:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid API key")
    elif settings.api_key not in supplied:
        raise HTTPException(status_code=401, detail="Invalid API key")


def owner_scope() -> str | None:
    """The user whose tracks this request may see, or None when tracks are shared (no user auth)."""
    if settings.auth_mode != "supabase":
        return None
    user = current_user.get()
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user.id


def preferred_scope() -> str:
    owner = owner_scope()
    return f"user:{owner}" if owner else "global"


def require_instance(instance_id: str) -> dict[str, Any]:
    """The track, or 404. Someone else's track is reported as missing, not forbidden."""
    instance = repository.get_instance(instance_id)
    owner = owner_scope()
    if not instance or (owner is not None and repository.instance_owner(instance_id) != owner):
        raise HTTPException(status_code=404, detail="Instance not found")
    return instance


def page(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    return {"rows": items[offset:offset + limit], "row_count": len(items),
            "limit": limit, "offset": offset}


def dataset(instance_id: str, include_partial: bool = False) -> tuple[list[str], list[list[str]], list[Observation]]:
    snapshot = repository.get_snapshot(instance_id)
    records = list(snapshot.accepted) if snapshot else []
    if include_partial and snapshot:
        records += snapshot.partial
    columns, rows = build_table(records, snapshot.plan if snapshot else None)
    return columns, rows, records


@app.get("/health")
@app.get("/api/v1/health")
def health() -> dict:
    return {"status": "ok", "service": "VORA"}


@app.get("/ready")
@app.get("/api/v1/ready")
def ready() -> JSONResponse:
    result = coordinator.readiness()
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@app.get("/version")
@app.get("/api/v1/version")
def version() -> dict:
    return {"name": "VORA", "version": VERSION, "api": "v1"}


@app.get("/metrics")
@app.get("/api/v1/metrics")
def metrics(_: None = Depends(authorized)) -> dict:
    instances = repository.list_instances(10000, 0, False, owner_scope())
    return {"instances": len(instances), "accepted_rows": sum(i["dataset_row_count"] for i in instances)}


@router.get("/instances")
def list_instances(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                   archived: bool = False) -> list[dict]:
    return repository.list_instances(limit, offset, archived, owner_scope())


@router.post("/instances", status_code=201)
def create_instance(body: CreateInstanceRequest) -> dict:
    return repository.create_instance(body.title, body.goal, owner_scope())


@router.get("/instances/{instance_id}")
def get_instance(instance_id: str) -> dict:
    return require_instance(instance_id)


@router.patch("/instances/{instance_id}")
def update_instance(instance_id: str, body: UpdateInstanceRequest) -> dict:
    require_instance(instance_id)
    return repository.update_instance(instance_id, **body.model_dump(exclude_none=True))


@router.delete("/instances/{instance_id}", status_code=204)
def delete_instance(instance_id: str) -> Response:
    require_instance(instance_id)
    for run in repository.active_runs(instance_id):
        coordinator.cancel(run["id"], "Instance deleted")
    if not repository.delete_instance(instance_id):
        raise HTTPException(status_code=404, detail="Instance not found")
    return Response(status_code=204)


@router.post("/instances/{instance_id}/duplicate", status_code=201)
def duplicate_instance(instance_id: str) -> dict:
    source = require_instance(instance_id)
    return repository.create_instance(f"{source['title']} copy", source["goal"], owner_scope())


@router.get("/instances/{instance_id}/messages")
def messages(instance_id: str, limit: int = Query(100, ge=1, le=500),
             offset: int = Query(0, ge=0)) -> list[dict]:
    require_instance(instance_id)
    return repository.list_messages(instance_id, limit, offset)


@router.post("/instances/{instance_id}/messages", status_code=202)
def send_message(instance_id: str, body: SendMessageRequest) -> dict:
    require_instance(instance_id)
    message = repository.add_message(instance_id, "user", body.content)
    repository.update_instance(instance_id, goal=body.content, title=body.content[:100])
    run = coordinator.submit(instance_id)
    return {"message": message, "run": run,
            "status_url": f"/api/v1/instances/{instance_id}/runs/{run['id']}"}


@router.get("/instances/{instance_id}/runs")
def runs(instance_id: str, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    require_instance(instance_id)
    return repository.list_runs(instance_id, limit)


def require_browser() -> None:
    """503 with the fix when no usable browser is configured."""
    state = coordinator.readiness()
    if not state["ready"]:
        raise HTTPException(status_code=503, detail=f"Browser not available: {state['reason']}")


@router.post("/instances/{instance_id}/runs", status_code=202)
def start_run(instance_id: str, fresh: bool = Query(False, description="Read every source again instead of "
                                                       "reusing a recent shared read")) -> dict:
    instance = require_instance(instance_id)
    if not instance["goal"].strip():
        raise HTTPException(status_code=409, detail="Set a research goal before starting a run")
    require_browser()
    return coordinator.submit(instance_id, fresh=fresh)


@router.get("/instances/{instance_id}/runs/{run_id}")
def get_run(instance_id: str, run_id: str) -> dict:
    require_instance(instance_id)
    run = repository.get_run(run_id)
    if not run or run["instance_id"] != instance_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/instances/{instance_id}/runs/{run_id}/events")
def run_events(instance_id: str, run_id: str, after: int = Query(0, ge=0)) -> dict:
    run = get_run(instance_id, run_id)
    return {"run": run, "events": repository.list_run_events(run_id, after), "phases": list(PHASES)}


@router.post("/instances/{instance_id}/runs/{run_id}/cancel")
def cancel_run(instance_id: str, run_id: str) -> dict:
    get_run(instance_id, run_id)
    return coordinator.cancel(run_id)


@router.post("/instances/{instance_id}/runs/{run_id}/retry", status_code=202)
def retry_run(instance_id: str, run_id: str) -> dict:
    get_run(instance_id, run_id)
    require_browser()
    return coordinator.submit(instance_id)


@router.get("/instances/{instance_id}/dataset")
def get_dataset(instance_id: str, limit: int = Query(100, ge=1, le=5000),
                offset: int = Query(0, ge=0), include_partial: bool = False) -> dict:
    require_instance(instance_id)
    columns, rows, records = dataset(instance_id, include_partial)
    window = slice(offset, offset + limit)
    return {"columns": columns, "rows": rows[window], "row_count": len(rows),
            "limit": limit, "offset": offset, "include_partial": include_partial,
            "records": [record_meta(item) for item in records[window]]}


@router.delete("/instances/{instance_id}/dataset", status_code=204)
def clear_dataset(instance_id: str) -> Response:
    require_instance(instance_id)
    repository.clear_snapshot(instance_id)
    return Response(status_code=204)


@router.post("/instances/{instance_id}/dataset/rescore")
def rescore_dataset(instance_id: str) -> dict:
    require_instance(instance_id)
    if any(True for _ in repository.active_runs(instance_id)):
        raise HTTPException(status_code=409, detail="Wait for the active run to finish before re-scoring")
    snapshot = coordinator.rescore(instance_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="No stored observations to re-score")
    return {"accepted": len(snapshot.accepted), "partial": len(snapshot.partial),
            "rejected": len(snapshot.rejected), "raw": len(snapshot.raw),
            "plan": snapshot.plan.model_dump()}


def _observation_page(instance_id: str, name: str, limit: int, offset: int) -> dict:
    require_instance(instance_id)
    snapshot = repository.get_snapshot(instance_id)
    items = getattr(snapshot, name) if snapshot else []
    return page([item.model_dump() for item in items], limit, offset)


@router.get("/instances/{instance_id}/dataset/raw")
def raw_dataset(instance_id: str, limit: int = Query(100, ge=1, le=1000),
                offset: int = Query(0, ge=0)) -> dict:
    return _observation_page(instance_id, "raw", limit, offset)


@router.get("/instances/{instance_id}/dataset/partial")
def partial_dataset(instance_id: str, limit: int = Query(100, ge=1, le=1000),
                    offset: int = Query(0, ge=0)) -> dict:
    return _observation_page(instance_id, "partial", limit, offset)


@router.get("/instances/{instance_id}/dataset/rejected")
def rejected_dataset(instance_id: str, limit: int = Query(100, ge=1, le=1000),
                     offset: int = Query(0, ge=0)) -> dict:
    return _observation_page(instance_id, "rejected", limit, offset)


@router.get("/instances/{instance_id}/dataset/schema")
@router.get("/instances/{instance_id}/schema")
def schema(instance_id: str) -> dict:
    require_instance(instance_id)
    columns, rows, _ = dataset(instance_id)
    fields = [{"name": name, "nullable": any(not row[index] for row in rows),
               "observed_type": "number" if rows and all(str(row[index]).replace(",", "").replace(".", "", 1).isdigit() for row in rows if row[index]) else "string"}
              for index, name in enumerate(columns)]
    snapshot = repository.get_snapshot(instance_id)
    return {"fields": fields, "plan": snapshot.plan.model_dump() if snapshot else None}


@router.get("/instances/{instance_id}/dataset/stats")
def stats(instance_id: str) -> dict:
    require_instance(instance_id)
    columns, rows, records = dataset(instance_id)
    empty = {column: sum(1 for row in rows if not row[index]) for index, column in enumerate(columns)}
    total = max(1, len(rows) * max(1, len(columns)))
    snapshot = repository.get_snapshot(instance_id)
    tiers: dict[str, int] = {}
    for item in [*(snapshot.accepted if snapshot else []), *(snapshot.partial if snapshot else []),
                 *(snapshot.rejected if snapshot else [])]:
        tiers[item.tier] = tiers.get(item.tier, 0) + 1
    periods = sorted({item.data_period for item in records if item.data_period})
    starts = sorted(item.period_start for item in records if item.period_start)
    ends = sorted(item.period_end for item in records if item.period_end)
    return {"row_count": len(rows), "column_count": len(columns),
            "fill_rate": 1 - sum(empty.values()) / total,
            "duplicate_rows": len(rows) - len({tuple(row) for row in rows}),
            "empty_cells_by_column": empty,
            "tier_counts": tiers,
            "inferred_periods": sum(1 for item in records if item.time_inferred),
            "explicit_periods": sum(1 for item in records if item.data_period and not item.time_inferred),
            "distinct_periods": len(periods),
            "period_min": starts[0] if starts else None, "period_max": ends[-1] if ends else None,
            "mean_score": round(sum(item.score for item in records) / len(records), 3) if records else None,
            "source_count": len({item.source_url for item in records})}


@router.get("/instances/{instance_id}/dataset/provenance")
def provenance(instance_id: str, limit: int = Query(100, ge=1, le=1000),
               offset: int = Query(0, ge=0)) -> dict:
    require_instance(instance_id)
    snapshot = repository.get_snapshot(instance_id)
    rows = [record_meta(item) for item in snapshot.accepted] if snapshot else []
    return page(rows, limit, offset)


@router.get("/instances/{instance_id}/dataset/export")
def export_dataset(instance_id: str, format: str = "csv", provenance: bool = False,
                   include_partial: bool = False) -> Response:
    require_instance(instance_id)
    columns, rows, records = dataset(instance_id, include_partial)
    if provenance:
        extra = list(PROVENANCE_COLUMNS)
        columns = [*columns, *extra]
        rows = [[*row, *(("" if getattr(item, name) is None else str(getattr(item, name))) for name in extra)]
                for row, item in zip(rows, records)]
    if format == "json":
        return JSONResponse([dict(zip(columns, row)) for row in rows])
    if format == "jsonl":
        return Response("\n".join(json.dumps(dict(zip(columns, row))) for row in rows),
                        media_type="application/x-ndjson",
                        headers={"Content-Disposition": "attachment; filename=dataset.jsonl"})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(columns)
    writer.writerows(rows)
    return Response(output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=dataset.csv"})


def _domains(body: DomainList) -> list[str]:
    """Normalize a submitted domain list; a 422 names the first invalid entry."""
    domains = []
    for value in body.domains:
        try:
            domains.append(normalize_domain(value))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return list(dict.fromkeys(domains))


@router.get("/instances/{instance_id}/preferred-sources")
def track_preferred(instance_id: str) -> dict:
    require_instance(instance_id)
    return {"domains": repository.get_preferred(instance_id),
            "global": repository.get_preferred(preferred_scope())}


@router.put("/instances/{instance_id}/preferred-sources")
def set_track_preferred(instance_id: str, body: DomainList) -> dict:
    require_instance(instance_id)
    return {"domains": repository.set_preferred(instance_id, _domains(body)),
            "global": repository.get_preferred(preferred_scope())}


@router.get("/instances/{instance_id}/files")
def files(instance_id: str) -> dict:
    """Every file found for this track, kept as references (most relevant first)."""
    require_instance(instance_id)
    snapshot = repository.get_snapshot(instance_id)
    items = sorted(snapshot.files, key=lambda item: item.relevance, reverse=True) if snapshot else []
    counts: dict[str, int] = {}
    for item in items:
        counts[item.file_type] = counts.get(item.file_type, 0) + 1
    return {"files": [item.model_dump() for item in items], "counts": counts,
            "extractable_formats": sorted(EXTRACTABLE)}


@router.post("/instances/{instance_id}/files/{file_id}/extract")
def extract_file(instance_id: str, file_id: str) -> dict:
    """Download one file into memory, parse it and add its rows to the dataset."""
    require_instance(instance_id)
    try:
        item = coordinator.extract_file(instance_id, file_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not extract this file: {exc}") from exc
    return item.model_dump()


@router.get("/instances/{instance_id}/sources")
def sources(instance_id: str) -> dict:
    require_instance(instance_id)
    snapshot = repository.get_snapshot(instance_id)
    if not snapshot:
        return {"candidate_count": 0, "sources": [], "raw_count": 0, "partial_count": 0,
                "accepted_count": 0, "rejected_count": 0, "research_outcome": "pending"}
    return {"candidate_count": snapshot.candidate_count,
            "sources": [item.model_dump() for item in snapshot.sources],
            "raw_count": len(snapshot.raw), "partial_count": len(snapshot.partial),
            "accepted_count": len(snapshot.accepted), "rejected_count": len(snapshot.rejected),
            "research_outcome": snapshot.outcome}


@router.get("/instances/{instance_id}/sources/{item_id}")
def source_detail(instance_id: str, item_id: str) -> dict:
    data = sources(instance_id)
    for item in data["sources"]:
        if item["id"] == item_id:
            snapshot = repository.get_snapshot(instance_id)
            observations = [item_ for item_ in [*snapshot.accepted, *snapshot.partial, *snapshot.rejected]
                            if item_.source_url == item["url"]] if snapshot else []
            return {**item, "observations": [record_meta(obs) for obs in observations[:200]]}
    raise HTTPException(status_code=404, detail="Source not found")


def _graph_records(instance_id: str, include_partial: bool) -> list[Observation]:
    require_instance(instance_id)
    snapshot = repository.get_snapshot(instance_id)
    if not snapshot:
        return []
    return [*snapshot.accepted, *(snapshot.partial if include_partial else [])]


def _names(value: str | None) -> list[str] | None:
    names = [item.strip() for item in (value or "").split(",") if item.strip()]
    return names or None


@router.get("/instances/{instance_id}/graph/parameters")
def graph_parameter_list(instance_id: str, include_partial: bool = False) -> dict:
    """Numeric parameters that can be charted, with unit, size and chart kind."""
    return graph_parameters(_graph_records(instance_id, include_partial))


@router.get("/instances/{instance_id}/graph")
def graph(instance_id: str, parameters: str | None = Query(None, description="Comma-separated; default all"),
          include_partial: bool = False, start: str | None = Query(None, alias="from"),
          end: str | None = Query(None, alias="to"),
          series: str | None = Query(None, description="Comma-separated series or bar labels to keep")) -> dict:
    """One chart per parameter: a line per series over periods, or bars by group."""
    return graph_data(_graph_records(instance_id, include_partial), _names(parameters),
                      start=start, end=end, series=_names(series))


@router.get("/instances/{instance_id}/report")
def report(instance_id: str) -> dict:
    require_instance(instance_id)
    snapshot = repository.get_snapshot(instance_id)
    if not snapshot:
        return {"status": "pending", "report": None}
    return {"status": snapshot.outcome,
            "report": snapshot.model_dump(exclude={"raw", "accepted", "partial", "rejected"}) | {
                "counts": {"raw": len(snapshot.raw), "accepted": len(snapshot.accepted),
                           "partial": len(snapshot.partial), "rejected": len(snapshot.rejected)}}}


@router.get("/instances/{instance_id}/dashboard")
def dashboard(instance_id: str) -> dict:
    instance = require_instance(instance_id)
    columns, rows, _ = dataset(instance_id)
    return {"instance": instance, "rows_total": len(rows),
            "table": {"columns": columns, "rows": rows, "row_count": len(rows)}}


@router.get("/instances/{instance_id}/freshness")
def freshness(instance_id: str) -> dict:
    instance = require_instance(instance_id)
    snapshot = repository.get_snapshot(instance_id)
    next_cycle = coordinator.next_cycle_at(instance_id, instance["live_enabled"])
    return {"state": "fresh" if snapshot else "missing", "observed_at": snapshot.updated_at if snapshot else None,
            "scored_at": snapshot.scored_at if snapshot else None,
            "row_count": len(snapshot.accepted) if snapshot else 0,
            "next_cycle_at": next_cycle}


@router.post("/instances/{instance_id}/refresh", status_code=202)
def refresh(instance_id: str) -> dict:
    return start_run(instance_id)


def live_state(instance_id: str) -> dict:
    instance = require_instance(instance_id)
    latest = repository.latest_run(instance_id)
    return {"enabled": instance["live_enabled"], "latest_run": latest,
            "interval_seconds": settings.live_interval_seconds, "batch_seconds": settings.batch_seconds,
            "next_cycle_at": coordinator.next_cycle_at(instance_id, instance["live_enabled"]),
            "rows_total": instance["dataset_row_count"]}


@router.get("/instances/{instance_id}/live")
def live(instance_id: str) -> dict:
    return live_state(instance_id)


@router.patch("/instances/{instance_id}/live")
async def toggle_live(instance_id: str, request: Request) -> dict:
    require_instance(instance_id)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail="Body must be JSON: {\"enabled\": true}")
    repository.update_instance(instance_id, live_enabled=bool(body.get("enabled")))
    return live_state(instance_id)


@router.get("/instances/{instance_id}/live/stream")
async def live_stream(instance_id: str, request: Request) -> StreamingResponse:
    """Server-sent events: `status` whenever the latest run or dataset changes,
    `run_event` for each pipeline step, and comment keep-alives. The stream
    stays open across runs; clients reconnect after the 15 minute cap."""
    require_instance(instance_id)

    async def events():
        previous = None
        run_id, last_event = None, 0
        started = time.monotonic()
        yield "retry: 4000\n\n"
        while time.monotonic() - started < 900 and not shutting_down.is_set():
            if await request.is_disconnected():
                break
            state = await asyncio.to_thread(live_state, instance_id)
            latest = state["latest_run"]
            if latest and latest["id"] != run_id:
                run_id, last_event = latest["id"], 0
            if run_id:
                fresh = await asyncio.to_thread(repository.list_run_events, run_id, last_event)
                for item in fresh:
                    last_event = item["id"]
                    yield f"event: run_event\ndata: {json.dumps(item, default=str)}\n\n"
            encoded = json.dumps(state, default=str)
            if encoded != previous:
                previous = encoded
                yield f"event: status\ndata: {encoded}\n\n"
            else:
                yield ": keep-alive\n\n"
            active = latest and latest["status"] not in TERMINAL
            # Sleep in short steps so the stream ends promptly on server shutdown.
            deadline = time.monotonic() + (1.0 if active else 4.0)
            while time.monotonic() < deadline and not shutting_down.is_set():
                await asyncio.sleep(0.25)
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.websocket("/instances/{instance_id}/live/ws")
async def live_socket(websocket: WebSocket, instance_id: str) -> None:
    """Live updates over a WebSocket, as JSON text messages:

    * ``hello``: current live state and the latest run's events so far
    * ``status``: live state whenever the latest run changes status
    * ``run``: the latest run after each update (progress counters)
    * ``run_event``: one pipeline step
    * ``rows``: accepted rows as each page, interactive pass or dataset lands
    * ``batch_complete``: the batch ended; reload the dataset (final scoring
      may have moved rows)
    * ``ping``: keep-alive every 20 seconds
    """
    if settings.auth_mode == "supabase":
        # No headers on a browser WebSocket: the first message is {"type": "auth", "token": "..."}.
        await websocket.accept()
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=5)
            user = await asyncio.to_thread(verifier.verify, str((hello or {}).get("token", "")), settings)
        except (AuthError, asyncio.TimeoutError, ValueError, WebSocketDisconnect, KeyError, AttributeError):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Sign in required")
            return
        current_user.set(user)
        try:
            require_instance(instance_id)
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Instance not found")
            return
    else:
        if not await asyncio.to_thread(repository.get_instance, instance_id):
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Instance not found")
        await websocket.accept()
    subscription = coordinator.hub.subscribe(instance_id)

    async def send(message: dict) -> None:
        await websocket.send_text(json.dumps(message, default=str))

    try:
        state = await asyncio.to_thread(live_state, instance_id)
        latest = state["latest_run"]
        events = await asyncio.to_thread(repository.list_run_events, latest["id"], 0) if latest else []
        await send({"type": "hello", "state": state, "events": events})
        last_status = latest["status"] if latest else None
        last_ping = time.monotonic()
        while not shutting_down.is_set():
            message = await subscription.get(timeout=1.0)
            if message is None:
                if time.monotonic() - last_ping >= 20:
                    await send({"type": "ping"})
                    last_ping = time.monotonic()
                continue
            if message["type"] == "run":
                await send(message)
                if message["run"].get("status") != last_status:
                    last_status = message["run"].get("status")
                    await send({"type": "status", "state": await asyncio.to_thread(live_state, instance_id)})
                continue
            if message["type"] == "batch_complete":
                await send({"type": "status", "state": await asyncio.to_thread(live_state, instance_id)})
            await send(message)
        await websocket.close(code=status.WS_1001_GOING_AWAY)
    except (WebSocketDisconnect, RuntimeError):
        pass  # the client went away
    finally:
        subscription.close()


@app.post("/api/v1/goals/analyze")
async def goal_analysis(request: Request, mode: str = "full", _: None = Depends(authorized)) -> dict:
    """Preview a plan. ``mode=fast`` skips the language model (for live typing)."""
    body = await request.json()
    goal = str(body.get("goal") or body.get("query") or "").strip()
    if not goal:
        raise HTTPException(status_code=422, detail="goal is required")
    plan = await asyncio.to_thread(analyze_goal, goal, use_llm=mode != "fast")
    return plan.model_dump()


@app.get("/api/v1/capabilities")
def capabilities(_: None = Depends(authorized)) -> dict:
    return {"headless": True, "event_driven": True, "raw_before_integrity": True,
            "semantic_scoring": True, "deterministic_time_windows": True,
            "max_candidates": settings.max_candidates, "max_sources": settings.max_sources,
            "llm_model": settings.llm_model, "llm_fallback_model": settings.llm_fallback_model,
            "semantic_llm": settings.semantic_llm,
            "live_interval_seconds": settings.live_interval_seconds, "batch_seconds": settings.batch_seconds,
            "deep_lane": settings.deep_lane, "deep_lane_min_free_mb": settings.deep_lane_min_free_mb,
            "crawl_per_site": settings.crawl_per_site, "block_support": settings.block_support,
            "search_api": search_api.configured(), "search_cache_minutes": settings.search_cache_minutes,
            "max_searches_per_hour": settings.max_searches_per_hour,
            "search_interval_seconds": settings.search_interval_seconds,
            "search_cooldown_minutes": settings.search_cooldown_minutes,
            "llm_plan_seconds": settings.llm_plan_seconds,
            "source_statuses": ["complete", "partial", "empty", "blocked", "failed", "skipped"],
            "live_transport": ["websocket", "sse"],
            "max_linked_datasets": settings.max_linked_datasets,
            "max_per_domain": settings.max_per_domain, "site_adapters": settings.site_adapters,
            "merge_runs": settings.merge_runs, "extractable_formats": sorted(EXTRACTABLE),
            "pipeline_phases": list(PHASES), "extractors": list(EXTRACTORS),
            "version": VERSION}


@app.get("/api/v1/sources/preferred")
def global_preferred(_: None = Depends(authorized)) -> dict:
    return {"domains": repository.get_preferred(preferred_scope())}


@app.put("/api/v1/sources/preferred")
def set_global_preferred(body: DomainList, _: None = Depends(authorized)) -> dict:
    return {"domains": repository.set_preferred(preferred_scope(), _domains(body))}


@app.get("/api/v1/search/status")
def search_status(_: None = Depends(authorized)) -> dict:
    """How searching is going: the search API in use, engines resting after a
    block, and browser searches used this hour (shared by every track)."""
    return {"api": search_api.configured(), **search_gate.status()}


@app.get("/api/v1/sources/reputation")
def source_reputation(limit: int = Query(100, ge=1, le=500), _: None = Depends(authorized)) -> list[dict]:
    """What past runs recorded about each site: attempts, useful rows, blocks."""
    return repository.reputation(limit)


@app.get("/api/v1/access/circuits")
def circuits(_: None = Depends(authorized)) -> list:
    return []


@app.delete("/api/v1/access/circuits/{domain}")
def clear_circuit(domain: str, _: None = Depends(authorized)) -> dict:
    return {"domain": domain, "cleared": True}


@app.get("/api/v1/extractors")
def extractors(_: None = Depends(authorized)) -> list:
    return [{"id": item, "kind": "built_in"} for item in EXTRACTORS]


@app.get("/api/v1/extractors/{extractor_id}")
def extractor_detail(extractor_id: str, _: None = Depends(authorized)) -> dict:
    item = next((item for item in extractors() if item["id"] == extractor_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Extractor not found")
    return {**item, "domain_neutral": True, "event": "onExecutionComplete"}


@app.post("/api/v1/extractors/{extractor_id}/test")
async def test_extractor(extractor_id: str, request: Request,
                         _: None = Depends(authorized)) -> dict:
    extractor_detail(extractor_id)
    body = await request.json()
    html = str(body.get("html") or "")
    if not html:
        raise HTTPException(status_code=422, detail="html is required")
    result = ExecutionResult(
        execution_id="extractor-test", requested_url="about:blank", final_url="about:blank",
        title="Extractor test", html=html, status=200, elapsed_seconds=0,
        network_idle_reached=True, metadata=MappingProxyType({}),
    )
    rows = [row.model_dump() for row in parse_rendered_page(result)
            if row.method == extractor_id]
    return {"extractor_id": extractor_id, "rows": rows, "row_count": len(rows)}


@app.post("/api/v1/extractors/regenerate")
def regenerate_extractors(_: None = Depends(authorized)) -> dict:
    return {"status": "ready", "extractors": extractors()}


@app.delete("/api/v1/extractors/{extractor_id}")
def delete_extractor(extractor_id: str, _: None = Depends(authorized)) -> Response:
    extractor_detail(extractor_id)
    raise HTTPException(status_code=409, detail="Built-in extractors cannot be deleted")


@app.get("/api/v1/cache")
def cache(_: None = Depends(authorized)) -> dict:
    items = []
    for instance in repository.list_instances(500, 0, False, owner_scope()):
        snapshot = repository.get_snapshot(instance["id"])
        if not snapshot:
            continue
        signature = hashlib.sha256(snapshot.plan.normalized_goal.lower().encode()).hexdigest()[:24]
        items.append({"goal_signature": signature, "instance_id": instance["id"],
                      "goal": snapshot.plan.normalized_goal, "row_count": len(snapshot.accepted),
                      "observed_at": snapshot.updated_at})
    return {"items": items}


def cached(signature: str) -> tuple[dict, Any]:
    for item in cache()["items"]:
        if item["goal_signature"] == signature:
            return item, repository.get_snapshot(item["instance_id"])
    raise HTTPException(status_code=404, detail="Cache entry not found")


@app.get("/api/v1/cache/{goal_signature}")
def cache_detail(goal_signature: str, include_dataset: bool = False,
                 _: None = Depends(authorized)) -> dict:
    item, snapshot = cached(goal_signature)
    return {**item, "snapshot": snapshot.model_dump() if include_dataset else None}


@app.post("/api/v1/cache/{goal_signature}/invalidate")
def invalidate_cache(goal_signature: str, _: None = Depends(authorized)) -> dict:
    item, _ = cached(goal_signature)
    repository.clear_snapshot(item["instance_id"])
    return {"goal_signature": goal_signature, "invalidated": True}


@app.delete("/api/v1/cache/{goal_signature}", status_code=204)
def delete_cache(goal_signature: str, _: None = Depends(authorized)) -> Response:
    item, _ = cached(goal_signature)
    repository.clear_snapshot(item["instance_id"])
    return Response(status_code=204)


@app.get("/api/v1/webhooks")
def webhooks(_: None = Depends(authorized)) -> list[dict]:
    return repository.list_webhooks(owner_scope())


@app.post("/api/v1/webhooks", status_code=201)
def create_webhook(body: WebhookCreate, _: None = Depends(authorized)) -> dict:
    try:
        ensure_public_url(body.url)
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return repository.create_webhook(body.url, body.events, body.enabled, owner_scope())


@app.get("/api/v1/webhooks/{webhook_id}")
def webhook_detail(webhook_id: str, _: None = Depends(authorized)) -> dict:
    item = repository.get_webhook(webhook_id, owner_scope())
    if not item:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return item


@app.patch("/api/v1/webhooks/{webhook_id}")
def update_webhook(webhook_id: str, body: WebhookUpdate,
                   _: None = Depends(authorized)) -> dict:
    changes = body.model_dump(exclude_none=True)
    if "url" in changes:
        try:
            ensure_public_url(changes["url"])
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    item = repository.update_webhook(webhook_id, owner_scope(), **changes)
    if not item:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return item


@app.get("/api/v1/webhooks/{webhook_id}/deliveries")
def webhook_deliveries(webhook_id: str, _: None = Depends(authorized)) -> list:
    webhook_detail(webhook_id)
    return []


@app.delete("/api/v1/webhooks/{webhook_id}", status_code=204)
def delete_webhook(webhook_id: str, _: None = Depends(authorized)) -> Response:
    if not repository.delete_webhook(webhook_id, owner_scope()):
        raise HTTPException(status_code=404, detail="Webhook not found")
    return Response(status_code=204)


@app.get("/api/v1/auth/session")
def session_status(request: Request) -> dict:
    # Cookie sessions belong to the shared-key mode; with Supabase the web app sends a bearer token.
    return {"authenticated": settings.auth_mode != "api_key" or request.cookies.get("vora_session") == settings.api_key,
            "authentication_required": settings.auth_mode != "none", "mode": settings.auth_mode}


@app.post("/api/v1/auth/session")
async def create_session(request: Request) -> Response:
    body = await request.json()
    if settings.api_key and body.get("api_key") != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    response = JSONResponse({"authenticated": True})
    if settings.api_key:
        response.set_cookie("vora_session", settings.api_key, httponly=True, samesite="strict")
    return response


@app.delete("/api/v1/auth/session")
def delete_session() -> Response:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("vora_session")
    return response


app.include_router(router, prefix="/api/v1", dependencies=[Depends(authorized)])
app.include_router(router, prefix="/api", dependencies=[Depends(authorized)], include_in_schema=False)



@app.get("/", include_in_schema=False)
def root() -> dict:
    """VORA is an API only; the web app is a separate site (the private VORA repository)."""
    return {"name": "VORA", "version": VERSION, "api": "/api/v1", "docs": "/docs"}
