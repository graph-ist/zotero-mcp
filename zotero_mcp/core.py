"""Server-side plumbing shared by the tool modules: the FastMCP instance, the tool decorator,
async HTTP access to Zotero and to public APIs, caches (collections, item templates, DOI index)
and the item helpers every module uses."""
import asyncio
import functools
import html
import logging
import os
import re
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import api, config

log = logging.getLogger("zotero-mcp")

_PUBLIC_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
mcp = FastMCP("zotero", stateless_http=True, transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *_PUBLIC_HOSTS],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                     *(f"https://{h}" for h in _PUBLIC_HOSTS)]))


class ToolError(Exception):
    """A problem the model can act on; its message is returned as the tool's `detail`."""


def tool(fn):
    """Register fn as an MCP tool and turn exceptions into structured error results."""
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except ToolError as e:
            return {"status": "error", "detail": str(e)}
        except Exception as e:  # unexpected: still a structured answer for the model, traceback in the log
            log.exception("%s failed", fn.__name__)
            return {"status": "error", "detail": f"{type(e).__name__}: {e}"[:500],
                    "hint": "unexpected server error; retry once, then report it"}
    return mcp.tool()(wrapper)


# ---- HTTP ----------------------------------------------------------------------------------------
_zot: httpx.AsyncClient | None = None
_pub: httpx.AsyncClient | None = None


def zotero() -> httpx.AsyncClient:
    global _zot
    if _zot is None:
        _zot = httpx.AsyncClient(timeout=30, headers=api.headers(), transport=api.TRANSPORT)
    return _zot


def public() -> httpx.AsyncClient:
    """Separate client, so the Zotero key is never sent to third parties."""
    global _pub
    if _pub is None:
        _pub = httpx.AsyncClient(timeout=30, follow_redirects=True, transport=api.TRANSPORT,
                                 headers={"User-Agent": f"zotero-mcp/{config.VERSION}"})
    return _pub


def reset_clients() -> None:
    """Drop clients and caches (tests, or after a configuration change)."""
    global _zot, _pub
    _zot = _pub = None
    _coll_cache[:] = [0.0, {}]
    _templates.clear()
    _doi_index.update(version=None, items={}, by_key={})


async def req(method: str, path: str, *, params=None, json=None, headers=None) -> httpx.Response:
    for attempt in range(api.RETRIES):
        r = await zotero().request(method, api.url(path), params=params, json=json, headers=headers)
        delay = api.retry_delay(r, attempt)
        if delay is None:
            return r
        await asyncio.sleep(delay)
    return r


fail = api.fail


def key_of(k: str) -> str:
    valid = api.valid_key(k)
    if not valid:
        raise ToolError(f"invalid Zotero key {(k or '').strip().upper()!r}: expected 8 characters A-Z/0-9")
    return valid


# ---- item helpers ---------------------------------------------------------------------------------
def strip_html(s: str | None) -> str | None:
    if not s:
        return s
    s = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def creators(cs) -> list[str]:
    out = []
    for c in cs or []:
        name = c.get("name") or ", ".join(p for p in (c.get("lastName"), c.get("firstName")) if p)
        role = c.get("creatorType", "author")
        out.append(name if role == "author" else f"{name} ({role})")
    return out


def summary(obj: dict, colls: dict[str, dict] | None = None) -> dict:
    d = obj.get("data", obj)
    s = {"key": d.get("key"), "itemType": d.get("itemType"), "title": d.get("title"),
         "creators": creators(d.get("creators")), "date": d.get("date"),
         "venue": d.get("publicationTitle") or d.get("proceedingsTitle") or d.get("bookTitle")
                  or d.get("publisher"),
         "DOI": d.get("DOI"),
         "collections": [colls[k]["path"] if colls and k in colls else k for k in d.get("collections", [])],
         "tags": [t["tag"] for t in d.get("tags", [])],
         "numChildren": (obj.get("meta") or {}).get("numChildren")}
    return {k: v for k, v in s.items() if v not in (None, "", [])}


def year_of(data: dict) -> str:
    m = re.search(r"\d{4}", data.get("date") or "")
    return m.group(0) if m else ""


async def fetch_item(key: str) -> dict:
    r = await req("GET", f"/items/{key_of(key)}")
    if r.status_code != 200:
        raise ToolError(f"item {key}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


async def children(key: str) -> list[dict]:
    r = await req("GET", f"/items/{key}/children")
    return r.json() if r.status_code == 200 else []


async def patch(key: str, version: int, body: dict) -> dict:
    r = await req("PATCH", f"/items/{key}", json=body, headers={"If-Unmodified-Since-Version": str(version)})
    if r.status_code != 204:
        return fail(r)
    return {"status": "ok", "key": key}


async def post_items(items: list[dict]) -> dict:
    r = await req("POST", "/items", json=items)
    if r.status_code != 200:
        return fail(r)
    d = r.json()
    created = [v["key"] if isinstance(v, dict) else v for v in d.get("successful", {}).values()]
    out = {"status": "ok" if created else "error", "created_keys": created}
    if d.get("failed"):
        out["failed"] = {i: f.get("message") for i, f in d["failed"].items()}
    return out


async def post_updates(objs: list[dict]) -> dict:
    """Batch-update existing items; each object carries key, version and the changed fields."""
    ok, unchanged, failed = [], [], {}
    for i in range(0, len(objs), 50):
        r = await req("POST", "/items", json=objs[i:i + 50])
        if r.status_code != 200:
            failed[f"batch {i // 50}"] = f"HTTP {r.status_code} {r.text[:200]}"
            continue
        d = r.json()
        ok += [v["key"] if isinstance(v, dict) else v for v in d.get("successful", {}).values()]
        unchanged += list(d.get("unchanged", {}).values())
        failed |= {objs[i + int(j)]["key"]: f.get("message") for j, f in d.get("failed", {}).items()}
    return {"status": "ok" if not failed else "partial", "updated": len(ok),
            "unchanged": len(unchanged), "failed": failed or None}


async def paginate(path: str, params: dict | None = None, cap: int = 5000) -> list[dict]:
    out, start = [], 0
    while len(out) < cap:
        r = await req("GET", path, params={**(params or {}), "limit": 100, "start": start})
        if r.status_code != 200:
            raise ToolError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        page = r.json()
        out += page
        start += 100
        if not page or start >= int(r.headers.get("Total-Results", "0")):
            break
    return out


async def items_by_keys(keys: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(keys), 50):
        r = await req("GET", "/items", params={"itemKey": ",".join(keys[i:i + 50]), "limit": 50})
        if r.status_code != 200:
            raise ToolError(f"cannot fetch items: HTTP {r.status_code}")
        out += r.json()
    return out


# ---- caches ---------------------------------------------------------------------------------------
_coll_cache: list = [0.0, {}]


async def collections() -> dict[str, dict]:
    """key -> {name, parent, numItems, path}; cached for five minutes."""
    if time.monotonic() - _coll_cache[0] < 300:
        return _coll_cache[1]
    raw = await paginate("/collections")
    by_key = {c["key"]: {"name": c["data"]["name"], "parent": c["data"].get("parentCollection") or None,
                         "numItems": c.get("meta", {}).get("numItems", 0)} for c in raw}

    def path(k, seen=()):
        c = by_key[k]
        p = c["parent"]
        return f"{path(p, seen + (k,))} › {c['name']}" if p in by_key and p not in seen else c["name"]

    for k, c in by_key.items():
        c["path"] = path(k)
    _coll_cache[:] = [time.monotonic(), by_key]
    return by_key


def invalidate_collections() -> None:
    _coll_cache[:] = [0.0, {}]


_templates: dict[str, dict] = {}


async def template(item_type: str) -> dict:
    if item_type not in _templates:
        r = await req("GET", f"{api.API}/items/new", params={"itemType": item_type})
        if r.status_code != 200:
            raise ToolError(f"unknown itemType {item_type!r}")
        _templates[item_type] = r.json()
    return dict(_templates[item_type])


_doi_index: dict[str, Any] = {"version": None, "items": {}, "by_key": {}}


def _index_item(it: dict) -> None:
    dois = api.item_dois(it["data"])
    if dois:
        _doi_index["by_key"][it["key"]] = dois
        for doi in dois:
            _doi_index["items"].setdefault(doi, summary(it))


def _unindex(key: str) -> None:
    for doi in _doi_index["by_key"].pop(key, []):
        if _doi_index["items"].get(doi, {}).get("key") == key:
            del _doi_index["items"][doi]


async def library_dois() -> dict[str, dict]:
    """DOI -> item summary for the whole library. Built once, then updated with the items changed
    or deleted since the last version (two requests), or nothing when the library is unchanged."""
    version = _doi_index["version"]
    headers = {"If-Modified-Since-Version": str(version)} if version else None
    r = await req("GET", "/items/top", params={"limit": 1, "format": "keys"}, headers=headers)
    if r.status_code == 304:
        return _doi_index["items"]
    if r.status_code != 200:
        raise ToolError(f"library not readable (HTTP {r.status_code})")
    new_version = r.headers.get("Last-Modified-Version")
    if version is None:
        _doi_index.update(items={}, by_key={})
        for it in await paginate("/items/top", cap=10 ** 6):
            _index_item(it)
    else:
        changed = await paginate("/items/top", {"since": version, "includeTrashed": 1}, cap=10 ** 6)
        r = await req("GET", "/deleted", params={"since": version})
        gone = (r.json().get("items", []) if r.status_code == 200 else [])
        for key in gone + [it["key"] for it in changed]:
            _unindex(key)
        for it in changed:
            if not it["data"].get("deleted"):
                _index_item(it)
    _doi_index["version"] = new_version
    return _doi_index["items"]
