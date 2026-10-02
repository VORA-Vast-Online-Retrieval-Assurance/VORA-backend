"""Small SQLite repository with JSON snapshots and atomic checkpoints."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from vora.shared.contracts import ResearchSnapshot

logger = logging.getLogger("vora.repository")

# Called after a run changes, with the run and the event row it appended (if any).
RunObserver = Callable[[dict[str, Any], dict[str, Any] | None], None]


# The row lists of a snapshot, stored in ``snapshot_rows``.
ROW_TIERS = ("raw", "accepted", "partial", "rejected")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Repository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._observers: list[RunObserver] = []
        self._initialize()

    # --- learned site structures: facts about public sites (no owner, no user data), shared by every track

    # --- universal blacklist: hosts that cannot be reached (a fact about a public host; no owner, no user data)

    def blacklisted_hosts(self) -> set[str]:
        with self.connect() as db:
            return {row["host"] for row in db.execute("SELECT host FROM blacklisted_sources")}

    def blacklist(self, host: str, reason: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO blacklisted_sources (host, reason, listed_at) VALUES (?, ?, ?)",
                       (host, reason[:200], _now()))

    def unblacklist(self, host: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM blacklisted_sources WHERE host = ?", (host,))

    def list_blacklist(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT host, reason, listed_at FROM blacklisted_sources ORDER BY host")]

    def list_learned(self) -> list[dict[str, Any]]:
        """Learned recipes that have not failed since they were last read cleanly."""
        with self.connect() as db:
            rows = db.execute("SELECT host, url, recipe FROM learned_structures "
                              "WHERE recipe IS NOT NULL AND failures = 0 ORDER BY host").fetchall()
        found = [{"host": row["host"], "url": row["url"], "recipe": json.loads(row["recipe"])} for row in rows]
        return [row for row in found if row["recipe"].get("kind") != "sections"]      # a pointer, not a recipe

    def get_learned(self, host: str, ttl_days: float) -> dict[str, Any] | None:
        """A stored learning for ``host`` still inside its lifetime (a failed one lives one day), else None."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM learned_structures WHERE host = ?", (host,)).fetchone()
        if row is None:
            return None
        age = datetime.now(UTC) - datetime.fromisoformat(row["learned_at"])
        if age > timedelta(days=ttl_days if row["recipe"] else 1):
            return None
        return {"url": row["url"], "recipe": json.loads(row["recipe"]) if row["recipe"] else None,
                "failures": row["failures"]}

    def save_learned(self, host: str, url: str, recipe: dict | None) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO learned_structures (host, url, recipe, failures, learned_at) "
                       "VALUES (?, ?, ?, 0, ?)", (host, url, json.dumps(recipe) if recipe else None, _now()))

    def touch_learned(self, host: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE learned_structures SET failures = 0 WHERE host = ?", (host,))

    def fail_learned(self, host: str, max_failures: int) -> None:
        """Count a failed read; after ``max_failures`` in a row the learning is dropped so the site is learned again."""
        with self.connect() as db:
            db.execute("UPDATE learned_structures SET failures = failures + 1 WHERE host = ?", (host,))
            db.execute("DELETE FROM learned_structures WHERE host = ? AND failures >= ?", (host, max_failures))

    def observe_runs(self, observer: RunObserver) -> None:
        """Register a callback fired after each committed run change."""
        self._observers.append(observer)

    def _notify(self, run: dict[str, Any] | None, event: dict[str, Any] | None) -> None:
        if run is None:
            return
        for observer in list(self._observers):
            try:
                observer(run, event)
            except Exception:  # an observer must never break run bookkeeping
                logger.exception("Run observer failed")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS instances (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '',
              archived INTEGER NOT NULL DEFAULT 0, live_enabled INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (
              id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
              role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
              status TEXT NOT NULL, phase TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
              rows_total INTEGER NOT NULL DEFAULT 0, rows_added INTEGER NOT NULL DEFAULT 0,
              error TEXT, cancellation_requested INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
              instance_id TEXT PRIMARY KEY REFERENCES instances(id) ON DELETE CASCADE,
              data TEXT NOT NULL, updated_at TEXT NOT NULL);
            -- One collected row per line: saving a snapshot writes only the rows that changed.
            CREATE TABLE IF NOT EXISTS snapshot_rows (
              instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
              tier TEXT NOT NULL, position INTEGER NOT NULL, row_id TEXT NOT NULL, digest TEXT NOT NULL,
              data TEXT NOT NULL, PRIMARY KEY (instance_id, tier, position));
            -- Shared reads of public sources (see research/reading/source_cache.py): no owner, no goal, no track.
            CREATE TABLE IF NOT EXISTS source_reads (
              key TEXT PRIMARY KEY, url TEXT NOT NULL, final_url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
              status INTEGER NOT NULL DEFAULT 0, challenge INTEGER NOT NULL DEFAULT 0, meta TEXT NOT NULL DEFAULT '{}',
              fetched_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_rows (
              key TEXT NOT NULL REFERENCES source_reads(key) ON DELETE CASCADE,
              position INTEGER NOT NULL, row_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (key, position));
            CREATE INDEX IF NOT EXISTS source_reads_fetched ON source_reads(fetched_at);
            -- Shared answers keyed by a normalised request: plans, resolved official sites, search results.
            CREATE TABLE IF NOT EXISTS query_cache (
              kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY (kind, key));
            CREATE TABLE IF NOT EXISTS webhooks (
              id TEXT PRIMARY KEY, url TEXT NOT NULL, events TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS run_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
              phase TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id, id);
            CREATE INDEX IF NOT EXISTS runs_instance ON runs(instance_id, created_at);
            CREATE TABLE IF NOT EXISTS preferred_sources (
              scope TEXT NOT NULL, domain TEXT NOT NULL, created_at TEXT NOT NULL,
              PRIMARY KEY (scope, domain));
            CREATE TABLE IF NOT EXISTS source_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
              run_id TEXT, url TEXT NOT NULL, domain TEXT NOT NULL, status TEXT NOT NULL,
              extracted INTEGER NOT NULL DEFAULT 0, accepted INTEGER NOT NULL DEFAULT 0,
              fetched_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS source_history_track ON source_history(instance_id, domain);
            CREATE INDEX IF NOT EXISTS source_history_domain ON source_history(domain, status);
            CREATE TABLE IF NOT EXISTS blacklisted_sources (
              host TEXT PRIMARY KEY, reason TEXT NOT NULL, listed_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS learned_structures (
              host TEXT PRIMARY KEY, url TEXT NOT NULL, recipe TEXT, failures INTEGER NOT NULL DEFAULT 0,
              learned_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS search_cache (
              instance_id TEXT NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
              goal_key TEXT NOT NULL, results TEXT NOT NULL, engines TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL, PRIMARY KEY (instance_id, goal_key));
            CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
            CREATE INDEX IF NOT EXISTS instances_live ON instances(live_enabled, archived);
            CREATE INDEX IF NOT EXISTS source_history_fetched ON source_history(fetched_at);
            CREATE INDEX IF NOT EXISTS run_events_created ON run_events(created_at);
            """)
            # The accepted-row count lives beside the snapshot, so listing tracks and
            # checking one no longer parses every snapshot's JSON.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(snapshots)")}
            if "accepted_count" not in columns:
                db.execute("ALTER TABLE snapshots ADD COLUMN accepted_count INTEGER")
                db.execute("UPDATE snapshots SET accepted_count = json_array_length(data, '$.accepted')")
            # Tracks and webhooks belong to the user who made them (a Supabase user id).
            for table in ("instances", "webhooks"):
                names = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if "owner_id" not in names:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN owner_id TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS instances_owner ON instances(owner_id, archived, updated_at)")

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_instance(self, title: str, goal: str = "", owner: str | None = None) -> dict[str, Any]:
        identifier, now = str(uuid.uuid4()), _now()
        with self.connect() as db:
            db.execute("""INSERT INTO instances (id, title, goal, archived, live_enabled, created_at, updated_at, owner_id)
                VALUES (?,?,?,?,?,?,?,?)""", (identifier, title, goal, 0, 0, now, now, owner))
        return self.get_instance(identifier)

    def list_instances(self, limit: int, offset: int, archived: bool = False,
                       owner: str | None = None) -> list[dict[str, Any]]:
        """Tracks, newest first. ``owner`` limits the list to one user's tracks."""
        with self.connect() as db:
            # Count accepted rows in SQL instead of deserialising every snapshot.
            rows = db.execute(f"""SELECT i.*, COALESCE(s.accepted_count, 0) dataset_row_count
                FROM instances i LEFT JOIN snapshots s ON s.instance_id=i.id
                WHERE i.archived=? {"AND i.owner_id=?" if owner is not None else ""}
                ORDER BY i.updated_at DESC LIMIT ? OFFSET ?""",
                (int(archived), *([owner] if owner is not None else []), limit, offset)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item.pop("owner_id", None)
            item["archived"], item["live_enabled"] = bool(item["archived"]), bool(item["live_enabled"])
            result.append(item)
        return result

    def instance_owner(self, instance_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT owner_id FROM instances WHERE id=?", (instance_id,)).fetchone()
        return row["owner_id"] if row else None

    def preferred_scope(self, instance_id: str) -> str:
        """Where a track's owner keeps their default preferred sites: their own scope, or "global"."""
        owner = self.instance_owner(instance_id)
        return f"user:{owner}" if owner else "global"

    def claim_legacy(self, owner: str) -> dict[str, int]:
        """Give tracks and webhooks made before sign-in existed to ``owner`` (idempotent)."""
        with self.connect() as db:
            tracks = db.execute("UPDATE instances SET owner_id=? WHERE owner_id IS NULL", (owner,)).rowcount
            hooks = db.execute("UPDATE webhooks SET owner_id=? WHERE owner_id IS NULL", (owner,)).rowcount
            scope = f"user:{owner}"
            if not db.execute("SELECT 1 FROM preferred_sources WHERE scope=? LIMIT 1", (scope,)).fetchone():
                db.execute("UPDATE preferred_sources SET scope=? WHERE scope='global'", (scope,))
        return {"instances": tracks, "webhooks": hooks}

    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            item = self._dict(db.execute("""SELECT i.*, COALESCE(s.accepted_count, 0) dataset_row_count
                FROM instances i LEFT JOIN snapshots s ON s.instance_id=i.id WHERE i.id=?""",
                (instance_id,)).fetchone())
        if item:
            item.pop("owner_id", None)
            item["archived"], item["live_enabled"] = bool(item["archived"]), bool(item["live_enabled"])
        return item

    def update_instance(self, instance_id: str, **changes: Any) -> dict[str, Any] | None:
        allowed = {key: value for key, value in changes.items()
                   if key in {"title", "goal", "archived", "live_enabled"} and value is not None}
        if allowed:
            allowed["updated_at"] = _now()
            clause = ",".join(f"{key}=?" for key in allowed)
            with self.connect() as db:
                db.execute(f"UPDATE instances SET {clause} WHERE id=?", (*allowed.values(), instance_id))
        return self.get_instance(instance_id)

    def delete_instance(self, instance_id: str) -> bool:
        with self.connect() as db:
            db.execute("DELETE FROM preferred_sources WHERE scope=?", (instance_id,))
            return db.execute("DELETE FROM instances WHERE id=?", (instance_id,)).rowcount > 0

    def add_message(self, instance_id: str, role: str, content: str) -> dict[str, Any]:
        item = {"id": str(uuid.uuid4()), "instance_id": instance_id, "role": role,
                "content": content, "created_at": _now()}
        with self.connect() as db:
            db.execute("INSERT INTO messages VALUES (?,?,?,?,?)", tuple(item.values()))
            db.execute("UPDATE instances SET updated_at=? WHERE id=?", (_now(), instance_id))
        return item

    def list_messages(self, instance_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM messages WHERE instance_id=? ORDER BY created_at LIMIT ? OFFSET ?",
                (instance_id, limit, offset)).fetchall()]

    def create_run(self, instance_id: str) -> dict[str, Any]:
        identifier, now = str(uuid.uuid4()), _now()
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (identifier, instance_id, "queued", "queued", "Waiting for a research worker",
                        0, 0, None, 0, now, None, None, now))
            cursor = db.execute("INSERT INTO run_events (run_id, phase, detail, created_at) VALUES (?,?,?,?)",
                                (identifier, "queued", "Waiting for a research worker", now))
            event = {"id": cursor.lastrowid, "run_id": identifier, "phase": "queued",
                     "detail": "Waiting for a research worker", "created_at": now}
        run = self.get_run(identifier)
        self._notify(run, event)
        return run

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            item = self._dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        if item:
            item["cancellation_requested"] = bool(item["cancellation_requested"])
        return item

    def list_runs(self, instance_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT * FROM runs WHERE instance_id=? ORDER BY created_at DESC LIMIT ?",
                (instance_id, limit)).fetchall()]
        for row in rows:
            row["cancellation_requested"] = bool(row["cancellation_requested"])
        return rows

    def latest_run(self, instance_id: str) -> dict[str, Any] | None:
        runs = self.list_runs(instance_id, limit=1)
        return runs[0] if runs else None

    def active_runs(self, instance_id: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM runs WHERE status IN ('queued','running')", ()
        if instance_id:
            query, params = query + " AND instance_id=?", (instance_id,)
        with self.connect() as db:
            return [dict(row) for row in db.execute(query + " ORDER BY created_at", params).fetchall()]

    def live_instances(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT id, goal FROM instances WHERE live_enabled=1 AND archived=0 AND goal<>''").fetchall()]

    def update_run(self, run_id: str, **changes: Any) -> dict[str, Any] | None:
        """Update a run. Each phase or detail change is also appended to the
        run's event log so clients can replay what actually happened."""
        allowed_names = {"status", "phase", "detail", "rows_total", "rows_added", "error",
                         "cancellation_requested", "started_at", "finished_at"}
        allowed = {key: value for key, value in changes.items() if key in allowed_names}
        allowed["updated_at"] = now = _now()
        event = None
        with self.connect() as db:
            if "phase" in allowed or "detail" in allowed:
                current = db.execute("SELECT phase, detail FROM runs WHERE id=?", (run_id,)).fetchone()
                if current is not None:
                    phase = allowed.get("phase", current["phase"])
                    detail = allowed.get("detail", current["detail"]) or ""
                    if (phase, detail) != (current["phase"], current["detail"]):
                        cursor = db.execute(
                            "INSERT INTO run_events (run_id, phase, detail, created_at) VALUES (?,?,?,?)",
                            (run_id, phase, detail, now))
                        event = {"id": cursor.lastrowid, "run_id": run_id, "phase": phase,
                                 "detail": detail, "created_at": now}
            clause = ",".join(f"{key}=?" for key in allowed)
            db.execute(f"UPDATE runs SET {clause} WHERE id=?", (*allowed.values(), run_id))
        run = self.get_run(run_id)
        self._notify(run, event)
        return run

    def list_run_events(self, run_id: str, after_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                (run_id, after_id, limit)).fetchall()]

    def save_snapshot(self, instance_id: str, snapshot: ResearchSnapshot) -> None:
        """Store a track's dataset. The rows live one per line in ``snapshot_rows`` and only rows whose content or
        place changed are written, so a save during a long run costs the new rows, not the whole dataset (and the
        database backup ships only those changes). The rest of the snapshot is one small JSON document."""
        snapshot.updated_at = datetime.now(UTC)
        head = snapshot.model_dump_json(exclude=set(ROW_TIERS))
        with self.connect() as db:
            db.execute("""INSERT INTO snapshots (instance_id, data, updated_at, accepted_count) VALUES (?,?,?,?)
                ON CONFLICT(instance_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at,
                  accepted_count=excluded.accepted_count""",
                (instance_id, head, _now(), len(snapshot.accepted)))
            stored = {(r["tier"], r["position"]): r["digest"] for r in db.execute(
                "SELECT tier, position, digest FROM snapshot_rows WHERE instance_id=?", (instance_id,))}
            writes = []
            for tier in ROW_TIERS:
                rows = getattr(snapshot, tier)
                for position, item in enumerate(rows):
                    data = item.model_dump_json()
                    digest = hashlib.sha1(data.encode()).hexdigest()
                    if stored.get((tier, position)) != digest:
                        writes.append((instance_id, tier, position, item.id, digest, data))
                db.execute("DELETE FROM snapshot_rows WHERE instance_id=? AND tier=? AND position>=?",
                           (instance_id, tier, len(rows)))
            if writes:
                db.executemany("""INSERT INTO snapshot_rows (instance_id, tier, position, row_id, digest, data)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(instance_id, tier, position) DO UPDATE SET
                    row_id=excluded.row_id, digest=excluded.digest, data=excluded.data""", writes)

    def get_snapshot(self, instance_id: str) -> ResearchSnapshot | None:
        with self.connect() as db:
            row = db.execute("SELECT data FROM snapshots WHERE instance_id=?", (instance_id,)).fetchone()
            if row is None:
                return None
            rows = db.execute("SELECT tier, data FROM snapshot_rows WHERE instance_id=? ORDER BY tier, position",
                              (instance_id,)).fetchall()
        payload = json.loads(row["data"])
        if rows or not any(payload.get(tier) for tier in ROW_TIERS):
            # Rows stored one per line (a snapshot saved before the rows table carries them inside its JSON).
            for tier in ROW_TIERS:
                payload[tier] = []
            for item in rows:
                payload[item["tier"]].append(json.loads(item["data"]))
        return ResearchSnapshot.model_validate(payload)

    def clear_snapshot(self, instance_id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM snapshot_rows WHERE instance_id=?", (instance_id,))
            db.execute("DELETE FROM snapshots WHERE instance_id=?", (instance_id,))
            db.execute("DELETE FROM search_cache WHERE instance_id=?", (instance_id,))

    # --- shared reads of public sources

    def get_source_read(self, key: str, rows: bool = True) -> dict[str, Any] | None:
        with self.connect() as db:
            head = db.execute("SELECT * FROM source_reads WHERE key=?", (key,)).fetchone()
            if head is None:
                return None
            found = dict(head)
            found["meta"] = json.loads(found["meta"] or "{}")
            found["rows"] = [r["data"] for r in db.execute(
                "SELECT data FROM source_rows WHERE key=? ORDER BY position", (key,))] if rows else []
        return found

    def put_source_read(self, key: str, *, url: str, final_url: str, title: str, status: int, challenge: bool,
                        rows: list[tuple[str, str]], meta: dict[str, Any], merge: bool = False) -> None:
        """Store a read. ``rows`` are (row id, observation JSON). With ``merge`` the rows join what is stored (new
        ids only) and the read counts as fresh again; otherwise they replace it."""
        with self.connect() as db:
            db.execute("""INSERT INTO source_reads (key, url, final_url, title, status, challenge, meta, fetched_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET url=excluded.url, final_url=excluded.final_url,
                title=excluded.title, status=excluded.status, challenge=excluded.challenge, meta=excluded.meta,
                fetched_at=excluded.fetched_at""",
                (key, url, final_url, title[:300], int(status), int(challenge), json.dumps(meta), _now()))
            if merge:
                known = {r["row_id"] for r in db.execute("SELECT row_id FROM source_rows WHERE key=?", (key,))}
                start = db.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM source_rows WHERE key=?",
                                   (key,)).fetchone()[0]
                fresh = [(row_id, data) for row_id, data in rows if row_id not in known]
            else:
                db.execute("DELETE FROM source_rows WHERE key=?", (key,))
                start, fresh = 0, rows
            db.executemany("INSERT INTO source_rows (key, position, row_id, data) VALUES (?,?,?,?)",
                           [(key, start + i, row_id, data) for i, (row_id, data) in enumerate(fresh)])

    # --- shared answers keyed by a normalised request

    def get_query_cache(self, kind: str, key: str, max_age_minutes: float) -> Any | None:
        with self.connect() as db:
            row = db.execute("SELECT value, created_at FROM query_cache WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row is None:
            return None
        if datetime.now(UTC) - datetime.fromisoformat(row["created_at"]) > timedelta(minutes=max_age_minutes):
            return None
        return json.loads(row["value"])

    def put_query_cache(self, kind: str, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO query_cache (kind, key, value, created_at) VALUES (?,?,?,?)",
                       (kind, key, json.dumps(value), _now()))

    def get_search_cache(self, instance_id: str, goal_key: str,
                         max_age_minutes: float | None) -> dict[str, Any] | None:
        """Search results stored for this goal, if younger than ``max_age_minutes``
        (``None``: however old)."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM search_cache WHERE instance_id=? AND goal_key=?",
                             (instance_id, goal_key)).fetchone()
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        if max_age_minutes is not None and datetime.now(UTC) - created > timedelta(minutes=max_age_minutes):
            return None
        return {"results": json.loads(row["results"]), "engines": row["engines"], "created_at": row["created_at"]}

    def set_search_cache(self, instance_id: str, goal_key: str, results: list[dict[str, Any]],
                         engines: str) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO search_cache VALUES (?,?,?,?,?)
                ON CONFLICT(instance_id, goal_key) DO UPDATE SET results=excluded.results,
                engines=excluded.engines, created_at=excluded.created_at""",
                (instance_id, goal_key, json.dumps(results), engines, _now()))

    def list_webhooks(self, owner: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM webhooks {'WHERE owner_id=?' if owner is not None else ''} ORDER BY created_at DESC",
                (owner,) if owner is not None else ()).fetchall()
        return [{**{k: v for k, v in dict(row).items() if k != "owner_id"},
                 "events": json.loads(row["events"]), "enabled": bool(row["enabled"])} for row in rows]

    def create_webhook(self, url: str, events: list[str], enabled: bool,
                       owner: str | None = None) -> dict[str, Any]:
        identifier, now = str(uuid.uuid4()), _now()
        with self.connect() as db:
            db.execute("""INSERT INTO webhooks (id, url, events, enabled, created_at, updated_at, owner_id)
                VALUES (?,?,?,?,?,?,?)""", (identifier, url, json.dumps(events), int(enabled), now, now, owner))
        return next(item for item in self.list_webhooks() if item["id"] == identifier)

    def get_webhook(self, webhook_id: str, owner: str | None = None) -> dict[str, Any] | None:
        return next((item for item in self.list_webhooks(owner) if item["id"] == webhook_id), None)

    def update_webhook(self, webhook_id: str, owner: str | None = None, **changes: Any) -> dict[str, Any] | None:
        current = self.get_webhook(webhook_id, owner)
        if not current:
            return None
        url = changes.get("url") if changes.get("url") is not None else current["url"]
        events = changes.get("events") if changes.get("events") is not None else current["events"]
        enabled = changes.get("enabled") if changes.get("enabled") is not None else current["enabled"]
        with self.connect() as db:
            db.execute("UPDATE webhooks SET url=?,events=?,enabled=?,updated_at=? WHERE id=?",
                       (url, json.dumps(events), int(enabled), _now(), webhook_id))
        return self.get_webhook(webhook_id)

    def delete_webhook(self, webhook_id: str, owner: str | None = None) -> bool:
        with self.connect() as db:
            if owner is not None:
                return db.execute("DELETE FROM webhooks WHERE id=? AND owner_id=?", (webhook_id, owner)).rowcount > 0
            return db.execute("DELETE FROM webhooks WHERE id=?", (webhook_id,)).rowcount > 0


    # -- preferred sources ----------------------------------------------------

    def get_preferred(self, scope: str) -> list[str]:
        """Preferred domains for ``scope``: "global", ``user:<id>`` or an instance id."""
        with self.connect() as db:
            return [row["domain"] for row in db.execute(
                "SELECT domain FROM preferred_sources WHERE scope=? ORDER BY created_at, domain", (scope,))]

    def set_preferred(self, scope: str, domains: list[str]) -> list[str]:
        unique = list(dict.fromkeys(domains))
        now = _now()
        with self.connect() as db:
            db.execute("DELETE FROM preferred_sources WHERE scope=?", (scope,))
            db.executemany("INSERT INTO preferred_sources VALUES (?,?,?)",
                           [(scope, domain, now) for domain in unique])
        return self.get_preferred(scope)

    # -- source history -------------------------------------------------------

    def record_source(self, instance_id: str, run_id: str | None, url: str, domain: str, status: str,
                      extracted: int, accepted: int) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO source_history
                (instance_id, run_id, url, domain, status, extracted, accepted, fetched_at)
                VALUES (?,?,?,?,?,?,?,?)""", (instance_id, run_id, url, domain, status, extracted, accepted, _now()))

    def source_signals(self, instance_id: str, cooldown_days: int) -> dict[str, Any]:
        """What past runs say about sources, for ranking the next run.

        * ``visited``: URL -> {"accepted": rows, "at": time} of this track's last read
        * ``proven``: domain -> accepted rows it gave this track (latest visit per URL)
        * ``blocked``: domain -> last time any track saw it blocked, within the cooldown
        """
        since = (datetime.now(UTC) - timedelta(days=cooldown_days)).isoformat()
        with self.connect() as db:
            visited = {row["url"]: {"accepted": row["accepted"], "at": row["fetched_at"]} for row in db.execute(
                """SELECT url, accepted, fetched_at FROM source_history WHERE id IN (
                     SELECT MAX(id) FROM source_history WHERE instance_id=? GROUP BY url)""", (instance_id,))}
            proven = {row["domain"]: row["rows"] for row in db.execute(
                """SELECT domain, SUM(accepted) AS rows FROM source_history WHERE id IN (
                     SELECT MAX(id) FROM source_history WHERE instance_id=? GROUP BY url)
                   GROUP BY domain HAVING SUM(accepted) > 0""", (instance_id,))}
            blocked = {row["domain"]: row["at"] for row in db.execute(
                """SELECT domain, MAX(fetched_at) AS at FROM source_history
                   WHERE status='blocked' AND fetched_at>=? GROUP BY domain""", (since,))}
        return {"visited": visited, "proven": proven, "blocked": blocked}

    def purge(self, *, event_days: int = 30, run_days: int = 90, history_days: int = 180,
              keep_runs: int = 20) -> dict[str, int]:
        """Delete history nothing needs any more. Returns how many rows went from each table.

        * run events of finished runs older than ``event_days``;
        * finished runs older than ``run_days`` beyond each track's newest ``keep_runs``
          (their events go with them);
        * source history older than ``history_days``, except the latest read of every
          page (ranking uses that);
        * search results cached more than a week ago;
        * shared source reads and shared answers older than two days.
        """
        now = datetime.now(UTC)
        cutoff = lambda days: (now - timedelta(days=days)).isoformat()  # noqa: E731
        with self.connect() as db:
            events = db.execute(
                """DELETE FROM run_events WHERE created_at < ? AND run_id IN
                   (SELECT id FROM runs WHERE status IN ('succeeded','failed','cancelled'))""",
                (cutoff(event_days),)).rowcount
            runs = db.execute(
                """DELETE FROM runs WHERE id IN (
                     SELECT id FROM (SELECT id, created_at, status,
                         ROW_NUMBER() OVER (PARTITION BY instance_id ORDER BY created_at DESC) AS newest FROM runs)
                     WHERE newest > ? AND created_at < ? AND status IN ('succeeded','failed','cancelled'))""",
                (keep_runs, cutoff(run_days))).rowcount
            history = db.execute(
                """DELETE FROM source_history WHERE fetched_at < ? AND id NOT IN
                   (SELECT MAX(id) FROM source_history GROUP BY instance_id, url)""",
                (cutoff(history_days),)).rowcount
            searches = db.execute("DELETE FROM search_cache WHERE created_at < ?", (cutoff(7),)).rowcount
            # Shared reads and answers are only useful while fresh; a day is far beyond any lifetime.
            shared = db.execute("DELETE FROM source_reads WHERE fetched_at < ?", (cutoff(2),)).rowcount
            db.execute("DELETE FROM source_rows WHERE key NOT IN (SELECT key FROM source_reads)")
            answers = db.execute("DELETE FROM query_cache WHERE created_at < ?", (cutoff(2),)).rowcount
        return {"run_events": events, "runs": runs, "source_history": history, "search_cache": searches,
                "source_reads": shared, "query_cache": answers}

    def installation_id(self) -> str:
        """A random id for this database, used to keep shared knowledge sections apart."""
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='installation_id'").fetchone()
            if row:
                return row["value"]
            # Two batches starting together may both get here: the first insert wins and
            # both read that one back.
            db.execute("INSERT OR IGNORE INTO meta VALUES ('installation_id', ?)", (uuid.uuid4().hex[:12],))
            return db.execute("SELECT value FROM meta WHERE key='installation_id'").fetchone()["value"]

    def site_knowledge(self) -> dict[str, dict[str, int]]:
        """Per-site outcomes across every track: reads, useful reads, accepted rows, blocks."""
        with self.connect() as db:
            rows = db.execute("""SELECT domain, COUNT(*) AS reads,
                    SUM(CASE WHEN accepted > 0 THEN 1 ELSE 0 END) AS useful_reads,
                    SUM(accepted) AS accepted_rows,
                    SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END) AS blocked_reads
                FROM source_history WHERE domain <> '' GROUP BY domain""").fetchall()
        return {row["domain"].casefold(): {"reads": row["reads"], "useful_reads": row["useful_reads"] or 0,
                                           "accepted_rows": row["accepted_rows"] or 0,
                                           "blocked_reads": row["blocked_reads"] or 0} for row in rows}

    def reputation(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                """SELECT domain, COUNT(*) AS attempts,
                          SUM(CASE WHEN accepted > 0 THEN 1 ELSE 0 END) AS productive,
                          SUM(accepted) AS accepted_rows,
                          SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                          MAX(CASE WHEN status='blocked' THEN fetched_at END) AS last_blocked_at,
                          MAX(fetched_at) AS last_seen_at
                   FROM source_history GROUP BY domain
                   ORDER BY accepted_rows DESC, attempts DESC LIMIT ?""", (limit,))]
