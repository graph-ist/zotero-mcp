"""MCP server: search, read and edit a Zotero library through the Zotero Web API v3.

Env:
  ZOTERO_USER_ID, ZOTERO_API_KEY  required
  ZOTERO_WEBDAV_DIR               WebDAV storage with KEY.zip files, default /srv/zotero-webdav/zotero
  MCP_TRANSPORT                   'stdio' (default) or 'http' (streamable HTTP on 127.0.0.1)
  PORT                            HTTP port, default 3003
  MCP_AUTH_TOKEN                  required in http mode, as Bearer header or ?token= query
  MCP_ALLOWED_HOSTS               comma-separated public host names allowed behind a proxy
"""

import asyncio
import functools
import hmac
import html
import json
import os
import re
import time
import zipfile
from typing import Annotated, Any, Literal
from urllib.parse import parse_qs

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

VERSION = "0.3.0"
USER_ID = os.environ.get("ZOTERO_USER_ID", "")
API_KEY = os.environ.get("ZOTERO_API_KEY", "")
WEBDAV_DIR = os.environ.get("ZOTERO_WEBDAV_DIR", "/srv/zotero-webdav/zotero")
API = "https://api.zotero.org"
BASE = f"{API}/users/{USER_ID}"
KEY_RE = re.compile(r"[A-Z0-9]{8}")

_PUBLIC_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
mcp = FastMCP("zotero", stateless_http=True, transport_security=TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *_PUBLIC_HOSTS],
    allowed_origins=["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
                     *(f"https://{h}" for h in _PUBLIC_HOSTS)]))

_zot: httpx.AsyncClient | None = None
_pub: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _zot
    if _zot is None:
        _zot = httpx.AsyncClient(timeout=30, headers={
            "Zotero-API-Version": "3", "Zotero-API-Key": API_KEY})
    return _zot


def _public() -> httpx.AsyncClient:
    # Separate client so the Zotero key is never sent to third parties
    global _pub
    if _pub is None:
        _pub = httpx.AsyncClient(timeout=30, follow_redirects=True,
                                 headers={"User-Agent": f"zotero-mcp/{VERSION}"})
    return _pub


class ToolError(Exception):
    pass


def _key(k: str) -> str:
    k = (k or "").strip().upper()
    if not KEY_RE.fullmatch(k):
        raise ToolError(f"invalid Zotero key {k!r}: expected 8 characters A-Z/0-9")
    return k


async def _req(method: str, path: str, *, params=None, json=None, headers=None) -> httpx.Response:
    url = path if path.startswith("http") else BASE + path
    for attempt in range(3):
        r = await _http().request(method, url, params=params, json=json, headers=headers)
        if r.status_code in (429, 503) and attempt < 2:
            await asyncio.sleep(min(float(r.headers.get("Retry-After", "2")), 10))
            continue
        return r
    return r


def _fail(r: httpx.Response) -> dict:
    hint = {403: "API key lacks permission", 404: "not found",
            412: "item was modified elsewhere; fetch it again and retry"}.get(r.status_code)
    out = {"status": "error", "http_status": r.status_code, "detail": r.text[:300]}
    if hint:
        out["hint"] = hint
    return out


def _tool(fn):
    """Turn ToolError into a structured error result instead of an exception."""
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except ToolError as e:
            return {"status": "error", "detail": str(e)}
    return wrapper


def _strip_html(s: str | None) -> str | None:
    if not s:
        return s
    s = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", s)
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _creators(cs) -> list[str]:
    out = []
    for c in cs or []:
        name = c.get("name") or ", ".join(p for p in (c.get("lastName"), c.get("firstName")) if p)
        role = c.get("creatorType", "author")
        out.append(name if role == "author" else f"{name} ({role})")
    return out


_coll_cache: tuple[float, dict[str, dict]] = (0.0, {})


async def _collections() -> dict[str, dict]:
    global _coll_cache
    if time.monotonic() - _coll_cache[0] < 300:
        return _coll_cache[1]
    raw, start = [], 0
    while True:
        r = await _req("GET", "/collections", params={"limit": 100, "start": start})
        if r.status_code != 200:
            raise ToolError(f"cannot list collections: HTTP {r.status_code}")
        page = r.json()
        raw += page
        start += 100
        if start >= int(r.headers.get("Total-Results", "0")) or not page:
            break
    by_key = {c["key"]: {"name": c["data"]["name"], "parent": c["data"].get("parentCollection") or None,
                         "numItems": c.get("meta", {}).get("numItems", 0)} for c in raw}

    def path(k, seen=()):
        c = by_key[k]
        p = c["parent"]
        return f"{path(p, seen + (k,))} › {c['name']}" if p in by_key and p not in seen else c["name"]

    for k, c in by_key.items():
        c["path"] = path(k)
    _coll_cache = (time.monotonic(), by_key)
    return by_key


def _invalidate_collections() -> None:
    global _coll_cache
    _coll_cache = (0.0, {})


def _summary(obj: dict, colls: dict[str, dict] | None = None) -> dict:
    d = obj.get("data", obj)
    s = {"key": d.get("key"), "itemType": d.get("itemType"), "title": d.get("title"),
         "creators": _creators(d.get("creators")), "date": d.get("date"),
         "venue": d.get("publicationTitle") or d.get("proceedingsTitle") or d.get("bookTitle")
                  or d.get("publisher"),
         "DOI": d.get("DOI"),
         "collections": [colls[k]["path"] if colls and k in colls else k for k in d.get("collections", [])],
         "tags": [t["tag"] for t in d.get("tags", [])],
         "numChildren": (obj.get("meta") or {}).get("numChildren")}
    return {k: v for k, v in s.items() if v not in (None, "", [])}


async def _get_item(key: str) -> dict:
    r = await _req("GET", f"/items/{_key(key)}")
    if r.status_code != 200:
        raise ToolError(f"item {key}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


async def _patch(key: str, version: int, body: dict) -> dict:
    r = await _req("PATCH", f"/items/{key}", json=body,
                   headers={"If-Unmodified-Since-Version": str(version)})
    if r.status_code != 204:
        return _fail(r)
    return {"status": "ok", "key": key}


async def _post_items(items: list[dict]) -> dict:
    r = await _req("POST", "/items", json=items)
    if r.status_code != 200:
        return _fail(r)
    d = r.json()
    created = [v["key"] if isinstance(v, dict) else v for v in d.get("successful", {}).values()]
    out = {"status": "ok" if created else "error", "created_keys": created}
    if d.get("failed"):
        out["failed"] = {i: f.get("message") for i, f in d["failed"].items()}
    return out


_templates: dict[str, dict] = {}


async def _template(item_type: str) -> dict:
    if item_type not in _templates:
        r = await _req("GET", f"{API}/items/new", params={"itemType": item_type})
        if r.status_code != 200:
            raise ToolError(f"unknown itemType {item_type!r}")
        _templates[item_type] = r.json()
    return dict(_templates[item_type])


def _pdf_bytes(att_key: str) -> bytes | None:
    path = os.path.join(WEBDAV_DIR, f"{att_key}.zip")
    if not os.path.isfile(path):
        return None
    with zipfile.ZipFile(path) as z:
        return next((b for b in (z.read(n) for n in z.namelist()) if b[:5] == b"%PDF-"), None)


def _pdf_text_from_webdav(att_key: str) -> str | None:
    data = _pdf_bytes(att_key)
    if data is None:
        return None
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return "\n\n".join(p.get_text() for p in doc).strip()


def _norm_word(w: str) -> str:
    return re.sub(r"\W+", "", w.lower())


def _locate(data: bytes, text: str, page: int | None, occurrence: int) -> dict | None:
    """Find `text` in the PDF; return page index/label, rects in PDF user space and sort index."""
    import pymupdf
    needle = [t for t in map(_norm_word, text.split()) if t]
    if not needle:
        return None
    seen = 0
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for i in ([page - 1] if page else range(doc.page_count)):
            if not 0 <= i < doc.page_count:
                continue
            p = doc[i]
            words = p.get_text("words", sort=True)
            # Stream of (token, [word rects]); a line-final "experi-" is joined with "ence"
            stream, j = [], 0
            while j < len(words):
                w = words[j]
                parts, txt = [w], w[4]
                while (txt.endswith("-") and j + 1 < len(words)
                       and (words[j + 1][5], words[j + 1][6]) != (w[5], w[6])):
                    j += 1
                    w = words[j]
                    parts.append(w)
                    txt = txt[:-1] + w[4]
                tok = _norm_word(txt)
                if tok:
                    stream.append((tok, parts))
                j += 1
            n = len(needle)
            for s in range(len(stream) - n + 1):
                if [t for t, _ in stream[s:s + n]] != needle:
                    continue
                if seen < occurrence:
                    seen += 1
                    continue
                lines: dict[tuple, pymupdf.Rect] = {}
                for _, parts in stream[s:s + n]:
                    for w in parts:
                        r = pymupdf.Rect(w[:4])
                        k = (w[5], w[6])
                        lines[k] = lines[k] | r if k in lines else r
                to_pdf = ~p.transformation_matrix
                rects = []
                for r in lines.values():
                    q = r * to_pdf
                    rects.append([round(min(q.x0, q.x1), 3), round(min(q.y0, q.y1), 3),
                                  round(max(q.x0, q.x1), 3), round(max(q.y0, q.y1), 3)])
                offset = sum(len(t) + 1 for t, _ in stream[:s])
                top = int(min(r.y0 for r in lines.values()))
                return {"pageIndex": i, "pageLabel": p.get_label() or str(i + 1), "rects": rects,
                        "sortIndex": f"{i:05d}|{offset:06d}|{top:05d}"}
    return None


@mcp.tool()
@_tool
async def annotate_pdf(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    text: Annotated[str, Field(description="Exact passage to mark, as it appears in the PDF")],
    type: Literal["highlight", "underline"] = "highlight",
    comment: Annotated[str | None, Field(description="Comment shown with the annotation")] = None,
    color: Annotated[str, Field(description="Hex color; Zotero palette: #ffd400 yellow, #ff6666 red, "
                                "#5fb236 green, #2ea8e5 blue, #a28ae5 purple, #e56eee magenta, "
                                "#f19837 orange, #aaaaaa gray")] = "#ffd400",
    page: Annotated[int | None, Field(ge=1, description="1-based page to search; default all pages")] = None,
    occurrence: Annotated[int, Field(ge=0, description="Which match to use if the text appears more than once")] = 0,
    tags: list[str] | None = None,
    dry_run: Annotated[bool, Field(description="Only locate the text, do not create the annotation")] = False,
) -> dict:
    """Highlight or underline a passage in a PDF; it appears in Zotero's reader on every device.

    The PDF file itself is not modified. Needs the PDF on WebDAV and selectable text (no scans)."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ToolError("color must be #RRGGBB")
    it = await _get_item(key)
    if it["data"]["itemType"] == "attachment":
        att = it
    else:
        r = await _req("GET", f"/items/{it['key']}/children")
        pdfs = [c for c in (r.json() if r.status_code == 200 else [])
                if c["data"]["itemType"] == "attachment" and c["data"].get("contentType") == "application/pdf"]
        if not pdfs:
            raise ToolError("no PDF attachment for this item")
        att = pdfs[0]
    data = await asyncio.to_thread(_pdf_bytes, att["key"])
    if data is None:
        raise ToolError("PDF not found on WebDAV: sync files from Zotero desktop first")
    loc = await asyncio.to_thread(_locate, data, text, page, occurrence)
    if loc is None:
        raise ToolError("text not found in the PDF; copy it exactly (try a shorter passage, "
                        "avoid words hyphenated across lines) or check the page number")
    if dry_run:
        return {"status": "found", "attachment_key": att["key"], "page": loc["pageLabel"],
                "lines": len(loc["rects"])}
    ann = {"itemType": "annotation", "parentItem": att["key"], "annotationType": type,
           "annotationText": " ".join(text.split()), "annotationComment": comment or "",
           "annotationColor": color.lower(), "annotationPageLabel": loc["pageLabel"],
           "annotationSortIndex": loc["sortIndex"],
           "annotationPosition": json.dumps({"pageIndex": loc["pageIndex"], "rects": loc["rects"]},
                                            separators=(",", ":")),
           "tags": [{"tag": t} for t in tags or []]}
    out = await _post_items([ann])
    out |= {"attachment_key": att["key"], "page": loc["pageLabel"]}
    return out


@mcp.tool()
@_tool
async def search_items(
    query: Annotated[str, Field(description="Search text; empty lists the most recently modified items")] = "",
    mode: Annotated[Literal["titleCreatorYear", "everything"],
                    Field(description="'everything' also searches all fields and indexed PDF full text")]
        = "titleCreatorYear",
    collection_key: Annotated[str | None, Field(description="Restrict to one collection (see list_collections)")] = None,
    tag: Annotated[str | None, Field(description="Restrict to items with this tag")] = None,
    item_type: Annotated[str | None, Field(description="e.g. journalArticle, book, conferencePaper")] = None,
    sort: Literal["dateModified", "dateAdded", "title", "creator", "date"] = "dateModified",
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
    start: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Search top-level items (no attachments or notes) in the Zotero library."""
    path = f"/collections/{_key(collection_key)}/items/top" if collection_key else "/items/top"
    params: dict[str, Any] = {"limit": limit, "start": start, "sort": sort,
                              "direction": "asc" if sort in ("title", "creator") else "desc"}
    if query:
        params |= {"q": query, "qmode": mode}
    if tag:
        params["tag"] = tag
    if item_type:
        params["itemType"] = item_type
    r = await _req("GET", path, params=params)
    if r.status_code != 200:
        return _fail(r)
    colls = await _collections()
    return {"total": int(r.headers.get("Total-Results", "0")), "start": start,
            "items": [_summary(it, colls) for it in r.json()]}


@mcp.tool()
@_tool
async def get_item(key: Annotated[str, Field(description="8-character item key")]) -> dict:
    """Full metadata of one item, plus its attachments and notes."""
    it = await _get_item(key)
    r = await _req("GET", f"/items/{it['key']}/children")
    children = r.json() if r.status_code == 200 else []
    colls = await _collections()
    data = {k: v for k, v in it["data"].items() if v not in ("", [], {}) and k != "relations"}
    data["collections"] = [{"key": k, "path": colls.get(k, {}).get("path")} for k in data.get("collections", [])]
    atts = [{"key": c["key"], "title": c["data"].get("title"), "filename": c["data"].get("filename"),
             "contentType": c["data"].get("contentType"), "linkMode": c["data"].get("linkMode")}
            for c in children if c["data"]["itemType"] == "attachment"]
    notes = [{"key": c["key"], "text": (_strip_html(c["data"].get("note")) or "")[:1000]}
             for c in children if c["data"]["itemType"] == "note"]
    return {"item": data, "attachments": atts, "notes": notes}


@mcp.tool()
@_tool
async def get_fulltext(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    offset: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=100000)] = 30000,
) -> dict:
    """Text of an item's PDF: Zotero's full-text index first, else extracted from the WebDAV copy.

    Long texts are paginated: call again with offset = previous offset + returned length."""
    it = await _get_item(key)
    if it["data"]["itemType"] == "attachment":
        atts = [it]
    else:
        r = await _req("GET", f"/items/{it['key']}/children")
        atts = [c for c in (r.json() if r.status_code == 200 else [])
                if c["data"]["itemType"] == "attachment" and c["data"].get("contentType") == "application/pdf"]
    if not atts:
        raise ToolError("no PDF attachment for this item")
    att = atts[0]["key"]
    text, source = None, None
    r = await _req("GET", f"/items/{att}/fulltext")
    if r.status_code == 200 and r.json().get("content"):
        text, source = r.json()["content"], "zotero_fulltext_index"
    else:
        text = await asyncio.to_thread(_pdf_text_from_webdav, att)
        source = "webdav_pdf"
    if not text:
        raise ToolError("PDF not indexed in Zotero and not found on WebDAV (sync files first)")
    chunk = text[offset:offset + max_chars]
    return {"attachment_key": att, "source": source, "total_chars": len(text), "offset": offset,
            "returned_chars": len(chunk), "more": offset + len(chunk) < len(text), "text": chunk}


@mcp.tool()
@_tool
async def list_collections() -> dict:
    """All collections with key, full path ("Parent › Child") and number of items."""
    colls = await _collections()
    rows = [{"key": k, "path": c["path"], "numItems": c["numItems"]} for k, c in colls.items()]
    return {"collections": sorted(rows, key=lambda c: c["path"].lower())}


@mcp.tool()
@_tool
async def create_collection(
    name: str,
    parent_key: Annotated[str | None, Field(description="Parent collection key; omit for top level")] = None,
) -> dict:
    """Create a collection, optionally nested inside another."""
    body = [{"name": name, "parentCollection": _key(parent_key) if parent_key else False}]
    r = await _req("POST", "/collections", json=body)
    if r.status_code != 200:
        return _fail(r)
    _invalidate_collections()
    ok = r.json().get("successful", {})
    return {"status": "ok" if ok else "error",
            "key": next((v["key"] for v in ok.values()), None), "failed": r.json().get("failed") or None}


@mcp.tool()
@_tool
async def create_item(
    item_type: Annotated[str, Field(description="Zotero itemType, e.g. journalArticle, book, bookSection")],
    fields: Annotated[dict[str, str], Field(description="Zotero field names → values, e.g. "
                                            "{'title': ..., 'date': '2015', 'publicationTitle': ...}")],
    creators: Annotated[list[dict[str, str]] | None, Field(
        description="[{'creatorType':'author','lastName':'Van Eck','firstName':'Dingmar'}]")] = None,
    collection_keys: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Create an item from explicit metadata. Prefer create_item_from_doi when a DOI exists."""
    tpl = await _template(item_type)
    unknown = sorted(set(fields) - set(tpl))
    if unknown:
        raise ToolError(f"fields not valid for {item_type}: {unknown}; valid: "
                        f"{sorted(k for k in tpl if k not in ('itemType', 'creators', 'tags', 'collections', 'relations'))}")
    tpl.update(fields)
    tpl["creators"] = creators or []
    tpl["collections"] = [_key(k) for k in collection_keys or []]
    tpl["tags"] = [{"tag": t} for t in tags or []]
    return await _post_items([tpl])


CSL_TYPES = {"journal-article": "journalArticle", "proceedings-article": "conferencePaper",
             "book-chapter": "bookSection", "book": "book", "monograph": "book", "edited-book": "book",
             "reference-book": "book", "posted-content": "preprint", "report": "report",
             "dissertation": "thesis", "dataset": "dataset"}


@mcp.tool()
@_tool
async def create_item_from_doi(
    doi: str,
    collection_keys: list[str] | None = None,
    tags: list[str] | None = None,
    allow_duplicate: Annotated[bool, Field(description="Create even if the DOI is already in the library")] = False,
) -> dict:
    """Create an item with metadata fetched from Crossref for the given DOI."""
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", doi.strip(), flags=re.I)
    r = await _public().get(f"https://api.crossref.org/works/{doi}")
    if r.status_code != 200:
        raise ToolError(f"DOI not found on Crossref (HTTP {r.status_code})")
    m = r.json()["message"]
    title = " ".join((m.get("title") or [""])[0].split())
    if m.get("subtitle"):
        title += ": " + m["subtitle"][0]
    if not allow_duplicate and title:
        s = await _req("GET", "/items/top", params={"q": title[:100], "limit": 25})
        for it in (s.json() if s.status_code == 200 else []):
            if (it["data"].get("DOI") or "").lower() == doi.lower():
                return {"status": "duplicate", "existing": _summary(it)}
    item_type = CSL_TYPES.get(m.get("type"), "journalArticle")
    tpl = await _template(item_type)
    container = (m.get("container-title") or [None])[0]
    parts = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p)
    values = {"title": title, "publicationTitle": container, "proceedingsTitle": container,
              "bookTitle": container, "volume": m.get("volume"), "issue": m.get("issue"),
              "pages": m.get("page"), "date": date, "DOI": m.get("DOI"), "url": m.get("URL"),
              "ISSN": (m.get("ISSN") or [None])[0], "ISBN": (m.get("ISBN") or [None])[0],
              "publisher": m.get("publisher"), "language": m.get("language"),
              "abstractNote": _strip_html(m.get("abstract"))}
    for k, v in values.items():
        if k in tpl and v:
            tpl[k] = v
    tpl["creators"] = [
        {"creatorType": role, "lastName": a["family"], "firstName": a.get("given", "")} if "family" in a
        else {"creatorType": role, "name": a.get("name", "")}
        for role in ("author", "editor") for a in m.get(role, [])]
    tpl["collections"] = [_key(k) for k in collection_keys or []]
    tpl["tags"] = [{"tag": t} for t in tags or []]
    out = await _post_items([tpl])
    out["itemType"], out["title"] = item_type, title
    return out


@mcp.tool()
@_tool
async def update_item(
    key: str,
    fields: Annotated[dict[str, Any], Field(description="Only the fields to change, e.g. {'date': '2016'}; "
                                            "'creators' replaces the whole author list")],
) -> dict:
    """Change metadata fields of an item. Use set_item_collections / update_item_tags for those."""
    it = await _get_item(key)
    blocked = {"key", "version", "itemType", "collections", "tags", "deleted", "parentItem"} & set(fields)
    if blocked:
        raise ToolError(f"cannot change {sorted(blocked)} with update_item")
    unknown = sorted(set(fields) - set(it["data"]))
    if unknown:
        raise ToolError(f"fields not valid for {it['data']['itemType']}: {unknown}")
    return await _patch(it["key"], it["version"], fields)


@mcp.tool()
@_tool
async def set_item_collections(
    key: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Add an item to and/or remove it from collections. The item and its PDF are never deleted."""
    it = await _get_item(key)
    cur = list(it["data"].get("collections", []))
    rm = {_key(k) for k in remove or []}
    new = [k for k in cur if k not in rm] + [k for k in map(_key, add or []) if k not in cur]
    out = await _patch(it["key"], it["version"], {"collections": new})
    colls = await _collections()
    out["collections"] = [colls.get(k, {}).get("path", k) for k in new]
    return out


@mcp.tool()
@_tool
async def update_item_tags(
    key: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> dict:
    """Add and/or remove tags on an item."""
    it = await _get_item(key)
    rm = set(remove or [])
    tags = [t for t in it["data"].get("tags", []) if t["tag"] not in rm]
    have = {t["tag"] for t in tags}
    tags += [{"tag": t} for t in add or [] if t not in have]
    out = await _patch(it["key"], it["version"], {"tags": tags})
    out["tags"] = [t["tag"] for t in tags]
    return out


@mcp.tool()
@_tool
async def add_note(parent_key: str, text: Annotated[str, Field(description="Plain text; blank lines split paragraphs")]) -> dict:
    """Attach a note to an item."""
    paras = "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in re.split(r"\n\s*\n", text.strip()))
    return await _post_items([{"itemType": "note", "parentItem": _key(parent_key), "note": paras,
                               "tags": [], "collections": [], "relations": {}}])


@mcp.tool()
@_tool
async def trash_item(key: str) -> dict:
    """Move an item to the Zotero trash (recoverable with restore_item until the trash is emptied)."""
    it = await _get_item(key)
    out = await _patch(it["key"], it["version"], {"deleted": 1})
    out["title"] = it["data"].get("title")
    return out


@mcp.tool()
@_tool
async def restore_item(key: str) -> dict:
    """Restore an item from the Zotero trash."""
    it = await _get_item(key)
    return await _patch(it["key"], it["version"], {"deleted": 0})


@mcp.tool()
@_tool
async def format_citations(
    keys: list[str],
    style: Annotated[str, Field(description="CSL style id, e.g. apa, chicago-author-date, "
                                "modern-language-association, ieee")] = "apa",
    locale: str = "en-US",
) -> dict:
    """Formatted bibliography entries and in-text citations for up to 50 items."""
    ks = [_key(k) for k in keys][:50]
    r = await _req("GET", "/items", params={"itemKey": ",".join(ks), "include": "bib,citation",
                                            "style": style, "locale": locale, "linkwrap": 0})
    if r.status_code != 200:
        return _fail(r)
    return {"style": style, "entries": [{"key": it["key"], "citation": _strip_html(it.get("citation")),
                                         "bib": _strip_html(it.get("bib"))} for it in r.json()]}


@mcp.tool()
@_tool
async def list_tags(limit: Annotated[int, Field(ge=1, le=100)] = 100, start: int = 0) -> dict:
    """Tags used in the library with number of items."""
    r = await _req("GET", "/tags", params={"limit": limit, "start": start})
    if r.status_code != 200:
        return _fail(r)
    return {"total": int(r.headers.get("Total-Results", "0")),
            "tags": [{"tag": t["tag"], "numItems": t.get("meta", {}).get("numItems")} for t in r.json()]}


async def _paginate(path: str, params: dict | None = None, cap: int = 5000) -> list[dict]:
    out, start = [], 0
    while len(out) < cap:
        r = await _req("GET", path, params={**(params or {}), "limit": 100, "start": start})
        if r.status_code != 200:
            raise ToolError(f"{path}: HTTP {r.status_code} {r.text[:200]}")
        page = r.json()
        out += page
        start += 100
        if not page or start >= int(r.headers.get("Total-Results", "0")):
            break
    return out


async def _items_by_keys(keys: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(keys), 50):
        r = await _req("GET", "/items", params={"itemKey": ",".join(keys[i:i + 50]), "limit": 50})
        if r.status_code != 200:
            raise ToolError(f"cannot fetch items: HTTP {r.status_code}")
        out += r.json()
    return out


async def _post_updates(objs: list[dict]) -> dict:
    """Batch-update existing items; each object carries key, version and the changed fields."""
    ok, unchanged, failed = [], [], {}
    for i in range(0, len(objs), 50):
        r = await _req("POST", "/items", json=objs[i:i + 50])
        if r.status_code != 200:
            failed[f"batch {i // 50}"] = f"HTTP {r.status_code} {r.text[:200]}"
            continue
        d = r.json()
        ok += [v["key"] if isinstance(v, dict) else v for v in d.get("successful", {}).values()]
        unchanged += list(d.get("unchanged", {}).values())
        failed |= {objs[i + int(j)]["key"]: f.get("message") for j, f in d.get("failed", {}).items()}
    return {"status": "ok" if not failed else "partial", "updated": len(ok),
            "unchanged": len(unchanged), "failed": failed or None}


@mcp.tool()
@_tool
async def export_items(
    format: Literal["bibtex", "biblatex", "ris", "csljson"] = "bibtex",
    keys: list[str] | None = None,
    collection_key: Annotated[str | None, Field(description="Export a whole collection (top-level items)")] = None,
) -> dict:
    """Export items as BibTeX, BibLaTeX, RIS or CSL-JSON, by keys or by collection."""
    if keys:
        ks = [_key(k) for k in keys]
        parts = []
        for i in range(0, len(ks), 50):
            r = await _req("GET", "/items", params={"itemKey": ",".join(ks[i:i + 50]), "format": format})
            if r.status_code != 200:
                return _fail(r)
            parts.append(r.text)
    elif collection_key:
        parts, start = [], 0
        while True:
            r = await _req("GET", f"/collections/{_key(collection_key)}/items/top",
                           params={"format": format, "limit": 100, "start": start})
            if r.status_code != 200:
                return _fail(r)
            parts.append(r.text)
            start += 100
            if start >= int(r.headers.get("Total-Results", "0")):
                break
    else:
        raise ToolError("give keys or collection_key")
    return {"format": format, "content": "\n".join(p.strip() for p in parts if p.strip())}


@mcp.tool()
@_tool
async def update_collection(
    key: str,
    name: str | None = None,
    parent_key: Annotated[str | None, Field(description="New parent collection key")] = None,
    move_to_top: Annotated[bool, Field(description="Make it a top-level collection")] = False,
) -> dict:
    """Rename a collection and/or move it under another collection (or to the top level)."""
    k = _key(key)
    r = await _req("GET", f"/collections/{k}")
    if r.status_code != 200:
        return _fail(r)
    c = r.json()
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if move_to_top:
        body["parentCollection"] = False
    elif parent_key:
        p, colls = _key(parent_key), await _collections()
        node = p
        while node:
            if node == k:
                raise ToolError("cannot move a collection inside itself or its subcollections")
            node = colls.get(node, {}).get("parent")
        body["parentCollection"] = p
    if not body:
        raise ToolError("nothing to change")
    r = await _req("PATCH", f"/collections/{k}", json=body,
                   headers={"If-Unmodified-Since-Version": str(c["version"])})
    if r.status_code != 204:
        return _fail(r)
    _invalidate_collections()
    return {"status": "ok", "key": k, "path": (await _collections()).get(k, {}).get("path")}


@mcp.tool()
@_tool
async def delete_collection(
    key: str,
    include_subcollections: Annotated[bool, Field(
        description="Required true if the collection has subcollections (they are deleted too)")] = False,
) -> dict:
    """Delete a collection. Its items stay in the library (and in other collections)."""
    k, colls = _key(key), await _collections()
    subs = [c["path"] for ck, c in colls.items() if c["parent"] == k]
    if subs and not include_subcollections:
        return {"status": "refused", "detail": "collection has subcollections; set include_subcollections",
                "subcollections": subs}
    r = await _req("GET", f"/collections/{k}")
    if r.status_code != 200:
        return _fail(r)
    r2 = await _req("DELETE", f"/collections/{k}",
                    headers={"If-Unmodified-Since-Version": str(r.json()["version"])})
    if r2.status_code != 204:
        return _fail(r2)
    _invalidate_collections()
    return {"status": "ok", "deleted": colls.get(k, {}).get("path", k)}


@mcp.tool()
@_tool
async def rename_tag(old: str, new: str) -> dict:
    """Rename a tag on every item that has it (merges into `new` if that tag already exists)."""
    items = await _paginate("/items", {"tag": old})
    objs = []
    for it in items:
        tags = [t for t in it["data"].get("tags", []) if t["tag"] != old]
        if new not in {t["tag"] for t in tags}:
            tags.append({"tag": new})
        objs.append({"key": it["key"], "version": it["version"], "tags": tags})
    if not objs:
        return {"status": "ok", "updated": 0, "detail": f"no items tagged {old!r}"}
    return await _post_updates(objs)


@mcp.tool()
@_tool
async def delete_tag(tags: Annotated[list[str], Field(description="Tags to remove from the whole library")]) -> dict:
    """Remove tags from all items in the library. Items are not deleted."""
    r = await _req("GET", "/items", params={"limit": 1})
    version = r.headers.get("Last-Modified-Version")
    if r.status_code != 200 or not version:
        return _fail(r)
    r = await _req("DELETE", "/tags", params={"tag": " || ".join(tags)},
                   headers={"If-Unmodified-Since-Version": version})
    if r.status_code != 204:
        return _fail(r)
    return {"status": "ok", "deleted": tags}


@mcp.tool()
@_tool
async def list_trash(limit: Annotated[int, Field(ge=1, le=100)] = 50, start: int = 0) -> dict:
    """Items currently in the Zotero trash."""
    r = await _req("GET", "/items/trash", params={"limit": limit, "start": start})
    if r.status_code != 200:
        return _fail(r)
    return {"total": int(r.headers.get("Total-Results", "0")),
            "items": [_summary(it) for it in r.json() if it["data"]["itemType"] not in ("annotation",)]}


@mcp.tool()
@_tool
async def list_saved_searches() -> dict:
    """Saved searches with their conditions. The API cannot run them: reproduce them with search_items."""
    r = await _req("GET", "/searches")
    if r.status_code != 200:
        return _fail(r)
    return {"searches": [{"key": s["key"], "name": s["data"]["name"], "conditions": s["data"]["conditions"]}
                         for s in r.json()]}


@mcp.tool()
@_tool
async def get_annotations(key: Annotated[str, Field(description="Item key or PDF attachment key")]) -> dict:
    """Highlights, underlines and comments made in Zotero's PDF reader, in page order."""
    it = await _get_item(key)
    if it["data"]["itemType"] == "attachment":
        atts = [it]
    else:
        r = await _req("GET", f"/items/{it['key']}/children")
        atts = [c for c in (r.json() if r.status_code == 200 else []) if c["data"]["itemType"] == "attachment"]
    out = []
    for a in atts:
        for c in await _paginate(f"/items/{a['key']}/children"):
            d = c["data"]
            if d["itemType"] != "annotation":
                continue
            out.append({k: v for k, v in {
                "key": c["key"], "type": d.get("annotationType"), "page": d.get("annotationPageLabel"),
                "text": d.get("annotationText"), "comment": d.get("annotationComment"),
                "color": d.get("annotationColor"), "tags": [t["tag"] for t in d.get("tags", [])],
                "sort": d.get("annotationSortIndex"), "attachment": a["key"]}.items() if v not in (None, "", [])})
    out.sort(key=lambda x: (x["attachment"], x.get("sort", "")))
    for x in out:
        x.pop("sort", None)
    return {"count": len(out), "annotations": out}


def _norm_title(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


@mcp.tool()
@_tool
async def find_duplicates(
    collection_key: Annotated[str | None, Field(description="Limit to one collection; default whole library")] = None,
) -> dict:
    """Groups of items sharing the same DOI or the same normalized title."""
    path = f"/collections/{_key(collection_key)}/items/top" if collection_key else "/items/top"
    items = await _paginate(path)
    groups: dict[str, list[dict]] = {}
    for it in items:
        d = it["data"]
        doi = (d.get("DOI") or "").lower().strip()
        title = _norm_title(d.get("title"))
        for sig in ([f"doi:{doi}"] if doi else []) + ([f"title:{title}"] if len(title) > 10 else []):
            groups.setdefault(sig, []).append(it)
    seen, out = set(), []
    for sig, its in groups.items():
        keys = frozenset(i["key"] for i in its)
        if len(keys) > 1 and keys not in seen:
            seen.add(keys)
            out.append({"match": sig.split(":")[0], "items": [_summary(i) for i in its]})
    return {"scanned": len(items), "groups": out}


@mcp.tool()
@_tool
async def update_items_batch(
    keys: Annotated[list[str] | None, Field(description="Items to change")] = None,
    collection_key: Annotated[str | None, Field(description="Or: all top-level items of this collection")] = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    add_collections: list[str] | None = None,
    remove_collections: list[str] | None = None,
    fields: Annotated[dict[str, str] | None, Field(description="Same field values for every item, "
                                                   "e.g. {'extra': 'read'}")] = None,
) -> dict:
    """Apply the same tag, collection or field change to many items at once."""
    if keys:
        items = await _items_by_keys([_key(k) for k in keys])
    elif collection_key:
        items = await _paginate(f"/collections/{_key(collection_key)}/items/top")
    else:
        raise ToolError("give keys or collection_key")
    blocked = {"key", "version", "itemType", "collections", "tags", "deleted", "parentItem", "creators"} & set(fields or {})
    if blocked:
        raise ToolError(f"cannot set {sorted(blocked)} in batch")
    add_c, rm_c = [_key(k) for k in add_collections or []], {_key(k) for k in remove_collections or []}
    rm_t = set(remove_tags or [])
    objs = []
    for it in items:
        d = it["data"]
        obj: dict[str, Any] = {"key": it["key"], "version": it["version"]}
        if add_tags or rm_t:
            tags = [t for t in d.get("tags", []) if t["tag"] not in rm_t]
            have = {t["tag"] for t in tags}
            obj["tags"] = tags + [{"tag": t} for t in add_tags or [] if t not in have]
        if add_c or rm_c:
            cur = [c for c in d.get("collections", []) if c not in rm_c]
            obj["collections"] = cur + [c for c in add_c if c not in cur]
        for f, v in (fields or {}).items():
            if f in d:
                obj[f] = v
        objs.append(obj)
    out = await _post_updates(objs)
    out["matched"] = len(items)
    return out


def _item_uri(key: str) -> str:
    return f"http://zotero.org/users/{USER_ID}/items/{key}"


def _related(d: dict) -> list[str]:
    rel = (d.get("relations") or {}).get("dc:relation", [])
    rel = [rel] if isinstance(rel, str) else rel
    return [u.rsplit("/", 1)[-1] for u in rel]


@mcp.tool()
@_tool
async def list_related(key: str) -> dict:
    """Items linked to this one as 'related'."""
    it = await _get_item(key)
    keys = _related(it["data"])
    return {"key": it["key"], "related": [_summary(r) for r in await _items_by_keys(keys)] if keys else []}


@mcp.tool()
@_tool
async def set_related(key: str, other_key: str, remove: bool = False) -> dict:
    """Link (or unlink) two items as related, in both directions like Zotero desktop does."""
    a, b = await _get_item(key), await _get_item(other_key)
    objs = []
    for x, y in ((a, b), (b, a)):
        rel = dict(x["data"].get("relations") or {})
        cur = rel.get("dc:relation", [])
        cur = [cur] if isinstance(cur, str) else list(cur)
        uri = _item_uri(y["key"])
        cur = [u for u in cur if u != uri] if remove else cur + ([uri] if uri not in cur else [])
        if cur:
            rel["dc:relation"] = cur
        else:
            rel.pop("dc:relation", None)
        objs.append({"key": x["key"], "version": x["version"], "relations": rel})
    return await _post_updates(objs)


class _TokenAuth:
    """Bearer header or ?token= query: Claude web connectors cannot send custom headers."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope["headers"]).get(b"authorization", b"")
            bearer = header[7:] if header[:7].lower() == b"bearer " else b""
            query = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0].encode()
            if not (hmac.compare_digest(bearer, self.token) or hmac.compare_digest(query, self.token)):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    if not (USER_ID and API_KEY):
        raise SystemExit("ZOTERO_USER_ID and ZOTERO_API_KEY are required")
    if os.environ.get("MCP_TRANSPORT", "stdio") != "http":
        mcp.run()
        return
    import uvicorn
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_AUTH_TOKEN of at least 32 characters is required in http mode")
    # Access log off: the token travels in the query string
    uvicorn.run(_TokenAuth(mcp.streamable_http_app(), token), host="127.0.0.1",
                port=int(os.environ.get("PORT", "3003")), access_log=False)


if __name__ == "__main__":
    main()
