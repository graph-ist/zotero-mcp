"""Zotero Web API v3, the part shared by the MCP server (async), the OCR worker and the library
snapshot (sync): base URL and headers, retry policy, error hints, key and DOI helpers, and a
small synchronous client. Only this module knows how to talk to api.zotero.org."""
import re
import time

import httpx

from . import config

API = "https://api.zotero.org"
KEY_RE = re.compile(r"[A-Z0-9]{8}")
RETRIES = 3
TRANSPORT: httpx.BaseTransport | None = None  # tests inject an httpx.MockTransport here
_DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.I)
_EXTRA_DOI = re.compile(r"^DOI:\s*(\S+)", re.M | re.I)


def base() -> str:
    return f"{API}/users/{config.USER_ID}"


def headers() -> dict[str, str]:
    return {"Zotero-API-Version": "3", "Zotero-API-Key": config.API_KEY}


def url(path: str) -> str:
    return path if path.startswith("http") else base() + path


def retry_delay(r: httpx.Response, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None when the response is final."""
    if r.status_code in (429, 503) and attempt < RETRIES - 1:
        return min(float(r.headers.get("Retry-After", "2") or 2), 10)
    return None


def fail(r: httpx.Response) -> dict:
    """Structured error for a failed API response, with a hint on how to recover when known."""
    hint = {403: "API key lacks permission", 404: "not found",
            412: "item was modified elsewhere; fetch it again and retry"}.get(r.status_code)
    out = {"status": "error", "http_status": r.status_code, "detail": r.text[:300]}
    if hint:
        out["hint"] = hint
    return out


def valid_key(k: str) -> str | None:
    k = (k or "").strip().upper()
    return k if KEY_RE.fullmatch(k) else None


def norm_doi(doi: str) -> str:
    return _DOI_PREFIX.sub("", doi.strip()).strip().rstrip(".").lower()


def item_dois(data: dict) -> list[str]:
    """DOIs of an item: the DOI field, then "DOI: ..." lines in Extra (types without a DOI field)."""
    found = [data.get("DOI") or ""] + _EXTRA_DOI.findall(data.get("extra") or "")
    return [d for d in map(norm_doi, found) if d]


class Client:
    """Synchronous client for scripts: retries on 429/503, raises on transport errors."""

    def __init__(self, timeout: float = 60):
        self._c = httpx.Client(timeout=timeout, headers=headers(), transport=TRANSPORT)

    def request(self, method: str, path: str, *, params=None, json=None, headers=None) -> httpx.Response:
        for attempt in range(RETRIES):
            r = self._c.request(method, url(path), params=params, json=json, headers=headers)
            delay = retry_delay(r, attempt)
            if delay is None:
                return r
            time.sleep(delay)
        return r

    def get(self, path: str, **kw) -> httpx.Response:
        return self.request("GET", path, **kw)

    def paginate(self, path: str, params: dict | None = None, cap: int | None = None) -> tuple[list[dict], int]:
        """All objects of a listing and the library version they reflect."""
        out, start, version = [], 0, 0
        while cap is None or len(out) < cap:
            r = self.get(path, params={**(params or {}), "limit": 100, "start": start})
            if r.status_code != 200:
                raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
            version = int(r.headers.get("Last-Modified-Version", version) or version)
            page = r.json()
            out += page
            start += len(page)
            if len(page) < 100 or start >= int(r.headers.get("Total-Results", start)):
                break
        return out, version

    def changed_since(self, version: int) -> bool:
        """False when the library is still at `version` (one conditional request)."""
        r = self.get("/items", params={"limit": 1, "format": "keys"},
                     headers={"If-Modified-Since-Version": str(version)})
        return r.status_code != 304

    def deleted_since(self, version: int) -> dict:
        r = self.get("/deleted", params={"since": version})
        return r.json() if r.status_code == 200 else {}

    def close(self) -> None:
        self._c.close()
