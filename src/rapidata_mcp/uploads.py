"""Asset references, and the asset service's upload sessions that turn local files
into them."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

# The asset service names every stored file "<guid><extension>"; that is the reference.
# Processing can rewrite the extension (.webp, GIF to video), so any extension passes.
_GUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32}"
_REFERENCE = re.compile(rf"(?:{_GUID})(?:\.[^./\\]+)?", re.IGNORECASE)
# The SDK's own URL test: whatever fails it is read as a path on this server's disk.
_HTTP_URL = re.compile(r"https?://", re.IGNORECASE)

_EXAMPLE_REFERENCE = "3f2a9c1e-5b7d-4e2a-9c1f-0d8e7a6b5c4d.png"


def is_http_url(value: str) -> bool:
    return _HTTP_URL.match(value) is not None


def check_reference(value: str, where: str) -> str:
    """Return ``value`` as an asset reference, or raise a ValueError that points
    the caller at upload_media."""
    reference = value.strip()
    if _REFERENCE.fullmatch(reference):
        return reference
    shown = reference if len(reference) <= 80 else f"{reference[:77]}..."
    if is_http_url(reference):
        raise ValueError(
            f"{where} is a URL ({shown!r}), not an asset reference. Import it with "
            "upload_media(urls=[...]) and pass the reference it returns."
        )
    raise ValueError(
        f"{where} ({shown!r}) is not an asset reference. For a local file, call "
        "upload_media() without arguments and follow its next_step; then pass the "
        f"references it gives you, which look like {_EXAMPLE_REFERENCE!r}."
    )


class UploadSessionError(RuntimeError):
    """The Rapidata API refused or failed an upload-session request."""


class UploadSessions:
    """The asset service's upload sessions, called with the caller's api token."""

    def __init__(self, api_url: str, http: httpx.Client | None = None) -> None:
        self.api_url = api_url.rstrip("/")
        self._http = http or httpx.Client(timeout=30.0)

    def create(self, token: str) -> dict[str, Any]:
        return _json(self._send("POST", "/asset/upload-session", token))

    def files(self, token: str, session_id: str) -> list[dict[str, Any]]:
        # page_size 100 equals a session's maxFiles: one page holds the whole session.
        path = f"/asset/upload-session/{_segment(session_id)}/files?page_size=100"
        resp = self._send("GET", path, token)
        if resp.status_code == 403:
            raise UploadSessionError(
                f"Upload session {session_id!r} belongs to another Rapidata account; "
                "only the account that created it can list its files."
            )
        if resp.status_code == 404:
            raise UploadSessionError(
                f"Upload session {session_id!r} not found. Sessions stay readable "
                "for 7 days after upload_media creates them."
            )
        return _json(resp).get("items") or []

    def file_endpoint(self, session_id: str) -> str:
        return f"{self.api_url}/asset/upload-session/{_segment(session_id)}/file"

    def _send(self, method: str, path: str, token: str) -> httpx.Response:
        try:
            return self._http.request(
                method,
                f"{self.api_url}{path}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            raise UploadSessionError(f"Could not reach the Rapidata API: {e}") from e


def _segment(value: str) -> str:
    return quote(value, safe="")


def _json(resp: httpx.Response) -> dict[str, Any]:
    if resp.status_code != 200:
        request = resp.request
        raise UploadSessionError(
            f"The Rapidata API refused {request.method} {request.url.path} "
            f"({_detail(resp)})."
        )
    return resp.json()


def _detail(resp: httpx.Response) -> str:
    parts = [f"HTTP {resp.status_code}"]
    try:
        body = resp.json()
    except ValueError:
        body = resp.text[:200]
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, dict):
            for messages in errors.values():
                items = messages if isinstance(messages, list) else [messages]
                parts += [str(m) for m in items]
        if len(parts) == 1:
            texts = [body.get(k) for k in ("detail", "title")]
            parts += [t for t in texts if isinstance(t, str)][:1]
    elif isinstance(body, str) and body:
        parts.append(body)
    return ": ".join(parts[:4])
