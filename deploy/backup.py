"""Keep VORA's SQLite database alive on a host whose disk is wiped on restart (a free Hugging Face Space).

    python deploy/backup.py restore   # before VORA starts: download the last snapshot, if any
    python deploy/backup.py loop      # beside VORA: upload a snapshot every VORA_BACKUP_MINUTES when it changed
    python deploy/backup.py once      # after VORA stops: one last snapshot

Snapshots go to a private Hugging Face dataset (VORA_BACKUP_REPO, e.g. "you/vora-db") with HF_TOKEN. The copy is made
with SQLite's own backup API, so it is consistent even while VORA is writing. Without a token or repo every command
does nothing, so the same image runs fine on a host with a real disk.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DB = Path(os.getenv("DATABASE_PATH", "vora.db"))
REPO = os.getenv("VORA_BACKUP_REPO", "").strip()
TOKEN = os.getenv("HF_TOKEN", "").strip()
MINUTES = max(1.0, float(os.getenv("VORA_BACKUP_MINUTES", "5")))
NAME = "vora.db.gz"
# Every upload is a commit; old versions are squashed away once a day so the dataset does not grow without end.
SQUASH_SECONDS = 24 * 3600


def enabled() -> bool:
    if not (REPO and TOKEN):
        print("backup: VORA_BACKUP_REPO or HF_TOKEN not set; snapshots are off", flush=True)
        return False
    return True


def restore() -> None:
    if not enabled():
        return
    if DB.exists() and DB.stat().st_size > 0:
        print(f"backup: {DB} already present; not restoring", flush=True)
        return
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError, RepositoryNotFoundError

    try:
        path = hf_hub_download(REPO, NAME, repo_type="dataset", token=TOKEN)
    except (EntryNotFoundError, RepositoryNotFoundError):
        print("backup: no snapshot yet; starting with an empty database", flush=True)
        return
    DB.parent.mkdir(parents=True, exist_ok=True)
    temporary = DB.with_suffix(".restoring")
    with gzip.open(path, "rb") as source, open(temporary, "wb") as target:
        shutil.copyfileobj(source, target)
    sqlite3.connect(temporary).execute("PRAGMA integrity_check").fetchone()
    temporary.replace(DB)
    print(f"backup: restored {DB} ({DB.stat().st_size // 1024} KB)", flush=True)


def snapshot(last_digest: str | None) -> str | None:
    """Upload a consistent copy when the database changed. Returns the digest of what is now stored."""
    if not DB.exists():
        return last_digest
    from huggingface_hub import HfApi

    with tempfile.TemporaryDirectory() as folder:
        copy = Path(folder) / "vora.db"
        source = sqlite3.connect(DB, timeout=30)
        target = sqlite3.connect(copy)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        digest = hashlib.sha256(copy.read_bytes()).hexdigest()
        if digest == last_digest:
            return digest
        packed = Path(folder) / NAME
        with open(copy, "rb") as raw, gzip.open(packed, "wb", compresslevel=6) as out:
            shutil.copyfileobj(raw, out)
        HfApi(token=TOKEN).upload_file(path_or_fileobj=str(packed), path_in_repo=NAME, repo_id=REPO,
                                       repo_type="dataset", commit_message="VORA database snapshot")
        print(f"backup: uploaded snapshot ({packed.stat().st_size // 1024} KB)", flush=True)
        return digest


def squash() -> None:
    from huggingface_hub import HfApi

    try:
        HfApi(token=TOKEN).super_squash_history(repo_id=REPO, repo_type="dataset")
        print("backup: squashed old snapshot versions", flush=True)
    except Exception as exc:  # noqa: BLE001 - housekeeping only
        print(f"backup: squash skipped ({type(exc).__name__})", flush=True)


def loop() -> None:
    if not enabled():
        return
    digest = None
    last_squash = time.monotonic()
    while True:
        time.sleep(MINUTES * 60)
        try:
            digest = snapshot(digest)
            if time.monotonic() - last_squash > SQUASH_SECONDS:
                squash()
                last_squash = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - try again next round
            print(f"backup: snapshot failed ({type(exc).__name__}: {exc})", flush=True)


def once() -> None:
    if enabled():
        snapshot(None)


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    actions = {"restore": restore, "loop": loop, "once": once}
    if command not in actions:
        sys.exit("usage: backup.py restore|loop|once")
    actions[command]()
