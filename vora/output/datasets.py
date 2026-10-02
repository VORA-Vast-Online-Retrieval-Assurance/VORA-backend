"""Download datasets that rendered pages link to (see ``vora.extraction.datasets``).

Files are fetched with plain HTTP from the publisher's download endpoint:
they are data files, not pages, so no browser is needed. Downloads are bounded
by size and time and use the same public-URL guard as page rendering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from vora.extraction.datasets import DatasetLink
from vora.extraction.temporal import iso_date
from vora.shared.urls import ensure_public_url

MAX_BYTES = 20 * 1024 * 1024
TIMEOUT_SECONDS = 30
USER_AGENT = "VORA/0.2 (+research dataset collector)"


class DatasetError(RuntimeError):
    pass


class DatasetTooLarge(DatasetError):
    pass


@dataclass(slots=True)
class DownloadedDataset:
    link: DatasetLink
    text: str
    title: str
    context: str
    modified_at: str | None
    fetched_at: str
    http_status: int


@dataclass(slots=True)
class _Fetched:
    status: int
    content: bytes
    encoding: str | None
    headers: httpx.Headers

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


def _get(client: httpx.Client, url: str) -> _Fetched:
    """GET with a hard size limit, streaming so oversized files are never held.

    Redirects are followed manually so every hop passes the public-URL check.
    """
    for _ in range(5):
        ensure_public_url(url)
        with client.stream("GET", url) as response:
            if response.is_redirect and response.headers.get("location"):
                url = str(response.url.join(response.headers["location"]))
                continue
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise DatasetTooLarge(f"File larger than {MAX_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
            return _Fetched(response.status_code, b"".join(chunks), response.encoding, response.headers)
    raise DatasetError("Too many redirects")


def _metadata(client: httpx.Client, link: DatasetLink) -> tuple[str, str, str | None]:
    """Title, description and last update from a publisher metadata file, if any."""
    if not link.metadata_url:
        return link.title, "", None
    try:
        fetched = _get(client, link.metadata_url)
        payload = json.loads(fetched.text) if fetched.status == 200 else {}
    except (httpx.HTTPError, ValueError, DatasetTooLarge):
        return link.title, "", None
    chart = payload.get("chart", {}) if isinstance(payload, dict) else {}
    columns = payload.get("columns", {}) if isinstance(payload, dict) else {}
    updated = next((column.get("lastUpdated") for column in columns.values()
                    if isinstance(column, dict) and column.get("lastUpdated")), None)
    return chart.get("title") or link.title, chart.get("subtitle") or "", iso_date(updated)


def download(link: DatasetLink, transport: httpx.BaseTransport | None = None) -> DownloadedDataset:
    """Fetch a dataset and its metadata. ``transport`` is for tests."""
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport,
                      headers={"User-Agent": USER_AGENT, "Accept": "text/csv,text/plain,*/*"}) as client:
        fetched = _get(client, link.url)
        if fetched.status >= 400:
            raise DatasetError(f"HTTP {fetched.status} from the dataset URL")
        title, subtitle, updated = _metadata(client, link)
    modified = iso_date(fetched.headers.get("last-modified")) or updated
    context = f"{title}. {subtitle}".strip(". ") if subtitle else title
    return DownloadedDataset(
        link=link, text=fetched.text, title=title, context=context, modified_at=modified,
        fetched_at=datetime.now(UTC).isoformat(), http_status=fetched.status,
    )


@dataclass(slots=True)
class DownloadedFile:
    url: str
    content: bytes
    content_type: str
    modified_at: str | None
    fetched_at: str


def download_file(url: str, transport: httpx.BaseTransport | None = None) -> DownloadedFile:
    """Fetch any file into memory (never to disk) for on-demand extraction."""
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False, transport=transport,
                      headers={"User-Agent": USER_AGENT, "Accept": "*/*"}) as client:
        fetched = _get(client, url)
    if fetched.status >= 400:
        raise DatasetError(f"HTTP {fetched.status} from the file URL")
    return DownloadedFile(
        url=url, content=fetched.content,
        content_type=fetched.headers.get("content-type", "").split(";")[0].strip(),
        modified_at=iso_date(fetched.headers.get("last-modified")),
        fetched_at=datetime.now(UTC).isoformat(),
    )
