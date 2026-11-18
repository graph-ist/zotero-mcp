"""Tools on items: search, read, create, edit, trash, relations, duplicates, citations, export."""
import html
import re
import time
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from . import api, config, matching
from .pdf import _norm_word
from .core import (ToolError, collections, creators, fail, fetch_item, children, items_by_keys, key_of,
                   library_dois, paginate, patch, post_items, post_updates, req, strip_html, summary,
                   template, tool)

CSL_TYPES = {"journal-article": "journalArticle", "proceedings-article": "conferencePaper",
             "book-chapter": "bookSection", "book": "book", "monograph": "book", "edited-book": "book",
             "reference-book": "book", "posted-content": "preprint", "report": "report",
             "dissertation": "thesis", "dataset": "dataset"}


@tool
async def search_items(
    query: Annotated[str, Field(description="Search text; empty lists the most recently modified items")] = "",
    mode: Annotated[Literal["titleCreatorYear", "everything"],
                    Field(description="'everything' also searches all fields and indexed PDF full text")]
        = "titleCreatorYear",
    collection_key: Annotated[str | None, Field(description="Restrict to one collection (see list_collections)")] = None,
    tag: Annotated[str | None, Field(description="Restrict to items with this tag")] = None,
    item_type: Annotated[str | None, Field(description="Zotero itemType in camelCase, e.g. journalArticle, "
                                           "book, bookSection, conferencePaper, thesis, report, webpage; "
                                           "an unknown type is rejected")] = None,
    sort: Literal["dateModified", "dateAdded", "title", "creator", "date"] = "dateModified",
    limit: Annotated[int, Field(ge=1, le=100)] = 25,
    start: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Search top-level items (no attachments or notes) in the Zotero library."""
    path = f"/collections/{key_of(collection_key)}/items/top" if collection_key else "/items/top"
    params: dict[str, Any] = {"limit": limit, "start": start, "sort": sort,
                              "direction": "asc" if sort in ("title", "creator") else "desc"}
    if query:
        params |= {"q": query, "qmode": mode}
    if tag:
        params["tag"] = tag
    if item_type:
        await template(item_type)  # rejects unknown types instead of returning nothing
        params["itemType"] = item_type
    r = await req("GET", path, params=params)
    if r.status_code != 200:
        return fail(r)
    colls = await collections()
    return {"total": int(r.headers.get("Total-Results", "0")), "start": start,
            "items": [summary(it, colls) for it in r.json()]}


@tool
async def get_item(key: Annotated[str, Field(description="8-character item key")]) -> dict:
    """Full metadata of one item, plus its attachments and notes."""
    it = await fetch_item(key)
    kids = await children(it["key"])
    colls = await collections()
    data = {k: v for k, v in it["data"].items() if v not in ("", [], {}) and k != "relations"}
    data["collections"] = [{"key": k, "path": colls.get(k, {}).get("path")} for k in data.get("collections", [])]
    atts = [{"key": c["key"], "title": c["data"].get("title"), "filename": c["data"].get("filename"),
             "contentType": c["data"].get("contentType"), "linkMode": c["data"].get("linkMode")}
            for c in kids if c["data"]["itemType"] == "attachment"]
    notes = [{"key": c["key"], "text": (strip_html(c["data"].get("note")) or "")[:1000]}
             for c in kids if c["data"]["itemType"] == "note"]
    return {"item": data, "attachments": atts, "notes": notes}


@tool
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
    tpl = await template(item_type)
    unknown = sorted(set(fields) - set(tpl))
    if unknown:
        raise ToolError(f"fields not valid for {item_type}: {unknown}; valid: "
                        f"{sorted(k for k in tpl if k not in ('itemType', 'creators', 'tags', 'collections', 'relations'))}")
    tpl.update(fields)
    tpl["creators"] = creators or []
    tpl["collections"] = [key_of(k) for k in collection_keys or []]
    tpl["tags"] = [{"tag": t} for t in tags or []]
    return await post_items([tpl])


IfExists = Annotated[Literal["skip", "file", "duplicate"], Field(
    description="When the work is already in the library: 'skip' reports it and changes nothing; "
                "'file' adds the given collections and tags to the existing item; 'duplicate' creates a new item anyway")]


async def on_existing(existing: dict, if_exists: str, collection_keys: list[str] | None,
                      tags: list[str] | None) -> dict:
    """The answer for a work already in the library: status 'duplicate' with the existing item,
    and with if_exists='file' the collections and tags it was missing added to it (never removed)."""
    out: dict[str, Any] = {"status": "duplicate", "existing": existing}
    if if_exists != "file":
        return out
    it = await fetch_item(existing["key"])
    d = it["data"]
    new_c = [c for c in dict.fromkeys(key_of(k) for k in collection_keys or []) if c not in d.get("collections", [])]
    have = {t["tag"] for t in d.get("tags", [])}
    new_t = [t for t in dict.fromkeys(tags or []) if t not in have]
    body: dict[str, Any] = {}
    if new_c:
        body["collections"] = d.get("collections", []) + new_c
    if new_t:
        body["tags"] = d.get("tags", []) + [{"tag": t} for t in new_t]
    if body:
        r = await patch(it["key"], it["version"], body)
        if r.get("status") != "ok":
            return r | {"existing": existing}
    out["filed"] = {"collections_added": new_c, "tags_added": new_t}
    return out


@tool
async def create_item_from_doi(
    doi: str,
    collection_keys: list[str] | None = None,
    tags: list[str] | None = None,
    if_exists: IfExists = "skip",
    allow_duplicate: Annotated[bool, Field(description="Same as if_exists='duplicate' (kept for older callers)")] = False,
    expected_title: Annotated[str | None, Field(description="Title of the work you mean; the DOI's record must match it")] = None,
    expected_first_author: Annotated[str | None, Field(description="Surname of the first author you expect")] = None,
    expected_year: Annotated[int | None, Field(description="Year you expect (±1 allowed)")] = None,
    force: Annotated[bool, Field(description="Create despite a mismatch or a retraction")] = False,
) -> dict:
    """Create an item with metadata fetched from Crossref for the given DOI, after checking it.

    Pass expected_title / expected_first_author / expected_year whenever you know which work you
    mean: a DOI belonging to another paper is the commonest error, and the item is then not
    created (status 'not_created', with the record Crossref has). Retracted works (Crossref
    retraction notices, OpenAlex is_retracted) are not created either unless force=True."""
    from .core import public
    doi = api.norm_doi(doi)
    if allow_duplicate:
        if_exists = "duplicate"
    if if_exists != "duplicate":
        existing = (await library_dois()).get(doi)
        if existing:
            return await on_existing(existing, if_exists, collection_keys, tags)
    r = await public().get(f"https://api.crossref.org/works/{doi}")
    if r.status_code != 200:
        raise ToolError(f"DOI not found on Crossref (HTTP {r.status_code})")
    m = r.json()["message"]
    title = " ".join((m.get("title") or [""])[0].split())
    if m.get("subtitle"):
        title += ": " + m["subtitle"][0]
    problems, warnings = await _check_doi_record(m, doi, title, expected_title, expected_first_author, expected_year)
    if problems and not force:
        return {"status": "not_created", "problems": problems, "warnings": warnings,
                "crossref": _record_summary(m, title)}
    item_type = CSL_TYPES.get(m.get("type"), "journalArticle")
    tpl = await template(item_type)
    container = (m.get("container-title") or [None])[0]
    parts = ((m.get("issued") or {}).get("date-parts") or [[None]])[0]
    date = "-".join(f"{p:02d}" if i else str(p) for i, p in enumerate(parts) if p)
    values = {"title": title, "publicationTitle": container, "proceedingsTitle": container,
              "bookTitle": container, "volume": m.get("volume"), "issue": m.get("issue"),
              "pages": m.get("page"), "date": date, "DOI": m.get("DOI"), "url": m.get("URL"),
              "ISSN": (m.get("ISSN") or [None])[0], "ISBN": (m.get("ISBN") or [None])[0],
              "publisher": m.get("publisher"), "language": m.get("language"),
              "abstractNote": strip_html(m.get("abstract"))}
    for k, v in values.items():
        if k in tpl and v:
            tpl[k] = v
    tpl["creators"] = [
        {"creatorType": role, "lastName": a["family"], "firstName": a.get("given", "")} if "family" in a
        else {"creatorType": role, "name": a.get("name", "")}
        for role in ("author", "editor") for a in m.get(role, [])]
    tpl["collections"] = [key_of(k) for k in collection_keys or []]
    tpl["tags"] = [{"tag": t} for t in tags or []]
    if expected_title and not problems and "extra" in tpl:
        # the check passed against what the caller expected: say so on the item, so the snapshot,
        # the maps and the notes can tell it from an item imported as it came
        tpl["extra"] = api.set_verified(tpl.get("extra"), api.verified_line(
            "match", "create_item_from_doi crossref title+author+year", _today()))
        out_verified = True
    else:
        out_verified = False
    out = await post_items([tpl])
    out["itemType"], out["title"], out["verified"] = item_type, title, out_verified
    if problems or warnings:
        out["problems_overridden"], out["warnings"] = problems, warnings
    return out


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


@tool
async def set_verification(
    key: str,
    status: Literal["match", "mismatch", "retracted", "unverified"],
    source: Annotated[str, Field(description="What was checked and its outcome, e.g. 'verify_doi registered_match', "
                                 "'verify_book verified (Open Library, DNB)', 'verify_doi registered_title_mismatch'")],
) -> dict:
    """Record on the item the outcome of a bibliographic check made with the DOI MCP (verify_doi,
    verify_book, find_doi) as a "Verified: <date> <status> <source>" line in Extra, replacing an
    earlier one. The library snapshot exposes it as `verified`, and the DOI MCP's maps show it.
    Call it after verifying an item that was imported by hand or by the browser connector."""
    it = await fetch_item(key)
    d = it["data"]
    extra = api.set_verified(d.get("extra"), api.verified_line(status, source, _today()))
    out = await patch(it["key"], it["version"], {"extra": extra})
    if out.get("status") == "ok":
        out["verified"] = api.parse_verified(extra)
    return out


ISSUING = {"EP": "European Patent Office", "US": "United States Patent and Trademark Office",
           "WO": "World Intellectual Property Organization", "DE": "Deutsches Patent- und Markenamt",
           "GB": "Intellectual Property Office", "FR": "INPI", "IT": "UIBM", "JP": "Japan Patent Office",
           "CN": "China National Intellectual Property Administration", "KR": "Korean Intellectual Property Office"}


@tool
async def create_item_from_patent(
    number: Annotated[str, Field(description="Publication number as patent_get returns it, e.g. EP3500000")],
    title: str,
    applicants: list[str] | None = None,
    inventors: list[str] | None = None,
    published: Annotated[str | None, Field(description="Publication date YYYY-MM-DD")] = None,
    priority: Annotated[str | None, Field(description="Earliest priority date YYYY-MM-DD")] = None,
    abstract: str | None = None,
    url: str | None = None,
    family_id: str | None = None,
    ipc: list[str] | None = None,
    collection_keys: list[str] | None = None,
    tags: list[str] | None = None,
    allow_duplicate: bool = False,
) -> dict:
    """Create a patent item from the fields of the DOI MCP's patent_get (number, title,
    applicants, inventors, published, priority, abstract, url, family_id, ipc). Patents have no
    DOI: the publication number is the identifier, and an item with the same number is a
    duplicate (status 'duplicate') unless allow_duplicate. family_id and IPC classes go to Extra."""
    num = re.sub(r"[\s.,/-]", "", number).upper()
    if not re.fullmatch(r"[A-Z]{2}\d+[A-Z]?\d?", num):
        raise ToolError(f"not a patent publication number: {number!r} (e.g. EP3500000, US2019123456A1)")
    if not allow_duplicate:
        r = await req("GET", "/items/top", params={"q": num, "qmode": "everything", "itemType": "patent", "limit": 50})
        for it in (r.json() if r.status_code == 200 else []):
            have = re.sub(r"[\s.,/-]", "", it["data"].get("patentNumber") or "").upper()
            if have and (have == num or have.startswith(num) or num.startswith(have)):
                return {"status": "duplicate", "existing": summary(it)}
    tpl = await template("patent")
    extra = "\n".join(x for x in [f"EPO family: {family_id}" if family_id else None,
                                 f"IPC: {', '.join(ipc)}" if ipc else None] if x)
    values = {"title": title, "patentNumber": num, "assignee": "; ".join(applicants or []) or None,
              "issuingAuthority": ISSUING.get(num[:2]), "country": num[:2], "issueDate": published,
              "filingDate": priority, "priorityNumbers": None, "abstractNote": abstract, "url": url,
              "extra": extra or None}
    for k, v in values.items():
        if k in tpl and v:
            tpl[k] = v
    tpl["creators"] = [{"creatorType": "inventor", "lastName": n.split(",")[0].strip().title() if "," in n else n.title(),
                        "firstName": n.split(",", 1)[1].strip().title() if "," in n else ""} for n in inventors or []]
    tpl["collections"] = [key_of(k) for k in collection_keys or []]
    tpl["tags"] = [{"tag": t} for t in tags or []]
    out = await post_items([tpl])
    out["itemType"], out["patentNumber"] = "patent", num
    return out


def _record_summary(m: dict, title: str) -> dict:
    a = (m.get("author") or m.get("editor") or [{}])[0]
    return {"title": title, "first_author": a.get("family") or a.get("name"), "year": _cr_year(m),
            "type": m.get("type"), "container": (m.get("container-title") or [None])[0]}


def _cr_year(m: dict) -> int | None:
    for k in ("issued", "published-print", "published-online", "created"):
        p = ((m.get(k) or {}).get("date-parts") or [[None]])[0]
        if p and p[0]:
            return int(p[0])
    return None


async def _check_doi_record(m, doi, title, exp_title, exp_author, exp_year) -> tuple[list[str], list[str]]:
    """Problems block creation (mismatch with what the caller expects, retraction); warnings don't.

    The comparison is the DOI MCP's (zotero_mcp/matching.py, a copy of its matching.py): same
    title similarity, surname and year rules as find_doi and verify_doi."""
    from .core import public
    problems, warnings = [], []
    if not title:
        problems.append("the Crossref record has no title")
    people = [a.get("family") or a.get("name") or "" for a in (m.get("author") or []) + (m.get("editor") or [])]
    year = _cr_year(m)
    cand = {"title": title, "subtitle": (m.get("subtitle") or [None])[0], "authors": people, "year": year,
            "type": m.get("type")}
    check = matching.assess(cand, exp_title or title or "", exp_author, exp_year)
    if exp_title and check["score"] < matching.TITLE_THRESHOLD:
        problems.append(f"title mismatch: expected {exp_title!r}, the DOI is {title!r} (similarity {check['score']})")
    if exp_author and check["author_ok"] is False:
        problems.append(f"author mismatch: expected {exp_author!r}, the DOI's authors are {people[:5]}")
    if exp_year and check["year_ok"] is False:
        problems.append(f"year mismatch: expected {exp_year}, the DOI is {year}")
    if exp_title and not problems and not matching.eligible(check):
        warnings.append("short title: pass expected_first_author so the match can be confirmed")
    if not people:
        warnings.append("no authors in the Crossref record")
    if m.get("type") in ("peer-review", "component", "dataset", "journal-issue", "journal"):
        warnings.append(f"the DOI is a {m.get('type')!r}, not a publication")
    if exp_title and matching.is_derivative(cand, exp_title):
        problems.append(f"the DOI is a derivative record (erratum, review, reply) of {exp_title!r}, not the work")
    # a retracted article lists its notices in 'updated-by' (Crossref + Retraction Watch)
    retracted = bool(matching.RETRACTED_PREFIX_RE.match(title or "")) or any(
        u.get("type") in ("retraction", "withdrawal") for u in m.get("updated-by") or [])
    try:
        r = await public().get(f"https://api.openalex.org/works/doi:{doi}",
                                params={"select": "is_retracted"}, timeout=15)
        if r.status_code == 200 and r.json().get("is_retracted"):
            retracted = True
    except httpx.HTTPError:
        warnings.append("OpenAlex not reachable: retraction checked on Crossref only")
    if retracted:
        problems.append("the work is retracted")
    return problems, warnings


@tool
async def find_dois(
    dois: Annotated[list[str], Field(description="DOIs, with or without https://doi.org/ or doi: prefix", max_length=500)],
) -> dict:
    """Which of these DOIs are already in the library: one call for a whole reference list, map or
    search result. Matches the DOI field and "DOI:" lines in Extra, case-insensitively."""
    index = await library_dois()
    found, missing = {}, []
    for doi in dois:
        hit = index.get(api.norm_doi(doi))
        if hit:
            found[doi] = {k: hit.get(k) for k in ("key", "title", "creators", "date")}
        else:
            missing.append(doi)
    return {"in_library": found, "missing": missing, "checked": len(dois), "library_dois": len(index)}


@tool
async def update_item(
    key: str,
    fields: Annotated[dict[str, Any], Field(description="Only the fields to change, e.g. {'date': '2016'}; "
                                            "'creators' replaces the whole author list")],
) -> dict:
    """Change metadata fields of an item. Use update_item_links for tags and collections."""
    it = await fetch_item(key)
    blocked = {"key", "version", "itemType", "collections", "tags", "deleted", "parentItem"} & set(fields)
    if blocked:
        raise ToolError(f"cannot change {sorted(blocked)} with update_item")
    unknown = sorted(set(fields) - set(it["data"]))
    if unknown:
        raise ToolError(f"fields not valid for {it['data']['itemType']}: {unknown}")
    return await patch(it["key"], it["version"], fields)


@tool
async def update_item_links(
    key: str,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
    add_collections: Annotated[list[str] | None, Field(description="Collection keys (see list_collections)")] = None,
    remove_collections: list[str] | None = None,
) -> dict:
    """Add and/or remove tags and collections on one item. For many items use update_items_batch;
    for other metadata use update_item. The item and its PDF are never deleted."""
    if not any((add_tags, remove_tags, add_collections, remove_collections)):
        raise ToolError("give at least one of add_tags, remove_tags, add_collections, remove_collections")
    it = await fetch_item(key)
    body: dict[str, Any] = {}
    rm = set(remove_tags or [])
    tags = [t for t in it["data"].get("tags", []) if t["tag"] not in rm]
    have = {t["tag"] for t in tags}
    tags += [{"tag": t} for t in add_tags or [] if t not in have]
    if add_tags or remove_tags:
        body["tags"] = tags
    cur = list(it["data"].get("collections", []))
    rmc = {key_of(k) for k in remove_collections or []}
    new = [k for k in cur if k not in rmc] + [k for k in map(key_of, add_collections or []) if k not in cur]
    if add_collections or remove_collections:
        body["collections"] = new
    out = await patch(it["key"], it["version"], body)
    colls = await collections()
    out["tags"] = [t["tag"] for t in tags]
    out["collections"] = [colls.get(k, {}).get("path", k) for k in new]
    return out


@tool
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
        items = await items_by_keys([key_of(k) for k in keys])
    elif collection_key:
        items = await paginate(f"/collections/{key_of(collection_key)}/items/top")
    else:
        raise ToolError("give keys or collection_key")
    blocked = {"key", "version", "itemType", "collections", "tags", "deleted", "parentItem", "creators"} & set(fields or {})
    if blocked:
        raise ToolError(f"cannot set {sorted(blocked)} in batch")
    add_c, rm_c = [key_of(k) for k in add_collections or []], {key_of(k) for k in remove_collections or []}
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
    out = await post_updates(objs)
    out["matched"] = len(items)
    return out


@tool
async def add_note(parent_key: str, text: Annotated[str, Field(description="Plain text; blank lines split paragraphs")]) -> dict:
    """Attach a note to an item."""
    paras = "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in re.split(r"\n\s*\n", text.strip()))
    return await post_items([{"itemType": "note", "parentItem": key_of(parent_key), "note": paras,
                              "tags": [], "collections": [], "relations": {}}])


@tool
async def trash_item(key: str) -> dict:
    """Move an item to the Zotero trash (recoverable with restore_item until the trash is emptied)."""
    it = await fetch_item(key)
    out = await patch(it["key"], it["version"], {"deleted": 1})
    out["title"] = it["data"].get("title")
    return out


@tool
async def restore_item(key: str) -> dict:
    """Restore an item from the Zotero trash."""
    it = await fetch_item(key)
    return await patch(it["key"], it["version"], {"deleted": 0})


@tool
async def list_trash(limit: Annotated[int, Field(ge=1, le=100)] = 50, start: int = 0) -> dict:
    """Items currently in the Zotero trash."""
    r = await req("GET", "/items/trash", params={"limit": limit, "start": start})
    if r.status_code != 200:
        return fail(r)
    return {"total": int(r.headers.get("Total-Results", "0")),
            "items": [summary(it) for it in r.json() if it["data"]["itemType"] not in ("annotation",)]}


@tool
async def list_saved_searches() -> dict:
    """Saved searches with their conditions. The API cannot run them: reproduce them with search_items."""
    r = await req("GET", "/searches")
    if r.status_code != 200:
        return fail(r)
    return {"searches": [{"key": s["key"], "name": s["data"]["name"], "conditions": s["data"]["conditions"]}
                         for s in r.json()]}


@tool
async def format_citations(
    keys: Annotated[list[str], Field(min_length=1, max_length=50)],
    style: Annotated[str, Field(description="CSL style id, e.g. apa, chicago-author-date, "
                                "modern-language-association, ieee")] = "apa",
    locale: str = "en-US",
) -> dict:
    """Formatted bibliography entries and in-text citations for up to 50 items."""
    ks = [key_of(k) for k in keys]
    r = await req("GET", "/items", params={"itemKey": ",".join(ks), "include": "bib,citation",
                                           "style": style, "locale": locale, "linkwrap": 0})
    if r.status_code != 200:
        return fail(r)
    return {"style": style, "entries": [{"key": it["key"], "citation": strip_html(it.get("citation")),
                                         "bib": strip_html(it.get("bib"))} for it in r.json()]}


@tool
async def export_items(
    format: Literal["bibtex", "biblatex", "ris", "csljson"] = "bibtex",
    keys: list[str] | None = None,
    collection_key: Annotated[str | None, Field(description="Export a whole collection (top-level items)")] = None,
    max_items: Annotated[int, Field(ge=1, le=1000, description="Stop after this many items; "
                                    "`truncated` says if the collection has more")] = 200,
) -> dict:
    """Export items as BibTeX, BibLaTeX, RIS or CSL-JSON, by keys or by collection."""
    if keys:
        ks = [key_of(k) for k in keys][:max_items]
        total, parts = len(keys), []
        for i in range(0, len(ks), 50):
            r = await req("GET", "/items", params={"itemKey": ",".join(ks[i:i + 50]), "format": format})
            if r.status_code != 200:
                return fail(r)
            parts.append(r.text)
    elif collection_key:
        parts, start, total = [], 0, 0
        while start < max_items:
            r = await req("GET", f"/collections/{key_of(collection_key)}/items/top",
                          params={"format": format, "limit": min(100, max_items - start), "start": start})
            if r.status_code != 200:
                return fail(r)
            parts.append(r.text)
            total = int(r.headers.get("Total-Results", "0"))
            start += 100
            if start >= total:
                break
    else:
        raise ToolError("give keys or collection_key")
    return {"format": format, "exported": min(total, max_items), "total": total,
            "truncated": total > max_items, "content": "\n".join(p.strip() for p in parts if p.strip())}


def _norm_title(t: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


@tool
async def find_duplicates(
    collection_key: Annotated[str | None, Field(description="Limit to one collection; default whole library")] = None,
    limit: Annotated[int, Field(ge=1, le=200, description="Groups returned; `total_groups` tells how many exist")] = 50,
) -> dict:
    """Groups of items sharing the same DOI or the same normalized title, plus preprint ↔
    published-version pairs (match 'preprint'): similar titles, shared author surnames and
    years within 3. Check those before merging: a preprint can differ from the final version."""
    path = f"/collections/{key_of(collection_key)}/items/top" if collection_key else "/items/top"
    items = await paginate(path)
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
            out.append({"match": sig.split(":")[0], "items": [summary(i) for i in its]})
    for a, b in _preprint_pairs(items):
        keys = frozenset((a["key"], b["key"]))
        if not any(keys <= frozenset(i["key"] for i in g["items"]) for g in out):
            out.append({"match": "preprint", "items": [summary(a), summary(b)]})
    return {"scanned": len(items), "total_groups": len(out), "groups": out[:limit]}


PREPRINT_DOI = ("10.48550/", "10.31234/", "10.31235/", "10.31219/", "10.1101/", "10.2139/ssrn",
                "10.20944/", "10.21203/", "10.5281/zenodo", "10.36227/", "10.22541/", "10.32942/")


def _is_preprint(d: dict) -> bool:
    doi = (d.get("DOI") or "").lower()
    return (d.get("itemType") == "preprint" or doi.startswith(PREPRINT_DOI)
            or bool(re.search(r"arxiv|biorxiv|medrxiv|psyarxiv|ssrn|preprint",
                              " ".join(str(d.get(k) or "") for k in ("repository", "publicationTitle", "url", "extra")), re.I)))


def _surnames(d: dict) -> set[str]:
    return {_norm_word(c.get("lastName") or (c.get("name") or "").split(" ")[-1]) for c in d.get("creators", [])} - {""}


def _item_year(d: dict) -> int | None:
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", d.get("date") or "")
    return int(m.group(1)) if m else None


def _preprint_pairs(items: list[dict]) -> list[tuple[dict, dict]]:
    """A preprint and another item that look like the same work under a different title or DOI."""
    pre = [i for i in items if _is_preprint(i["data"])]
    out = []
    for p in pre:
        sp, yp = _surnames(p["data"]), _item_year(p["data"])
        for o in items:
            if o["key"] == p["key"] or (_is_preprint(o["data"]) and o["key"] < p["key"]):
                continue
            so, yo = _surnames(o["data"]), _item_year(o["data"])
            if not sp or not so or len(sp & so) < min(2, len(sp), len(so)):
                continue
            if yp and yo and abs(yp - yo) > 3:
                continue
            if matching.title_sim(p["data"].get("title") or "", o["data"].get("title"))[0] >= matching.TITLE_THRESHOLD:
                out.append((p, o))
    return out


def _item_uri(key: str) -> str:
    return f"http://zotero.org/users/{config.USER_ID}/items/{key}"


def _related(d: dict) -> list[str]:
    rel = (d.get("relations") or {}).get("dc:relation", [])
    rel = [rel] if isinstance(rel, str) else rel
    return [u.rsplit("/", 1)[-1] for u in rel]


@tool
async def list_related(key: str) -> dict:
    """Items linked to this one as 'related'."""
    it = await fetch_item(key)
    keys = _related(it["data"])
    return {"key": it["key"], "related": [summary(r) for r in await items_by_keys(keys)] if keys else []}


@tool
async def set_related(key: str, other_key: str, remove: bool = False) -> dict:
    """Link (or unlink) two items as related, in both directions like Zotero desktop does."""
    a, b = await fetch_item(key), await fetch_item(other_key)
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
    return await post_updates(objs)
