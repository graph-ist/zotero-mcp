#!/usr/bin/env python3
"""Write a snapshot of the Zotero library to KNOWLEDGE_DIR/library.json for other services on
the Pi (DOI MCP literature maps, knowledge views) that must not call the Zotero API.

Items (no notes, annotations or attachments as items) with key, type, title, creators, year,
DOI (also "DOI:" lines in Extra), ISBN, tags, collection paths and keys, PDF/EPUB attachment keys.
Nothing is written when the library version has not changed since the last snapshot; otherwise
only the items changed or deleted since then are fetched and merged into the previous snapshot.
Run by the MCP tool refresh_library_snapshot or by hand: snapshot.py [--force].
Env: ZOTERO_USER_ID, ZOTERO_API_KEY, KNOWLEDGE_DIR
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from zotero_mcp import api, config  # noqa: E402

# Bumped when a field changes meaning or is removed; readers (the DOI MCP's knowledge.py) refuse a
# newer major version instead of misreading it. Adding a field does not bump it.
SCHEMA_VERSION = 1
READABLE = ("application/pdf", "application/epub+zip")
SKIP = ("attachment", "note", "annotation")


def out_path() -> str:
    return os.path.join(config.KNOWLEDGE_DIR, "library.json")


def entry(d: dict, attachments: list | None = None) -> dict:
    dois = api.item_dois(d)
    year = re.search(r"\d{4}", d.get("date") or "")
    return {"key": d["key"], "type": d["itemType"], "title": d.get("title") or "",
            "creators": [c.get("lastName") or c.get("name") or "" for c in d.get("creators", [])],
            "year": int(year.group(0)) if year else None,
            "doi": api.norm_doi(d["DOI"]) if d.get("DOI") else None,
            "extra_dois": [x for x in dois if x != api.norm_doi(d.get("DOI") or "")],
            "isbn": d.get("ISBN") or None, "language": d.get("language") or None,
            "patent_number": re.sub(r"[\s.,/-]", "", d["patentNumber"]).upper() if d.get("patentNumber") else None,
            "verified": api.parse_verified(d.get("extra")),
            "tags": [t["tag"] for t in d.get("tags", [])],
            "collection_keys": list(d.get("collections", [])),
            "attachments": attachments or []}


def _apply(items: dict[str, dict], objects: list[dict], deleted: list[str]) -> None:
    """Merge changed and deleted objects into items (key -> entry)."""
    gone = set(deleted)
    for o in objects:
        d = o["data"]
        if d.get("deleted"):
            gone.add(d["key"])
    for k in gone:
        items.pop(k, None)
    for it in items.values():
        it["attachments"] = [a for a in it["attachments"] if a["key"] not in gone]
    changed = [o["data"] for o in objects if not o["data"].get("deleted")]
    for d in changed:  # regular items first, so attachments of new items find their parent
        if d["itemType"] not in SKIP:
            items[d["key"]] = entry(d, items.get(d["key"], {}).get("attachments"))
    for d in changed:
        if d["itemType"] == "attachment":
            for it in items.values():
                it["attachments"] = [a for a in it["attachments"] if a["key"] != d["key"]]
            parent = items.get(d.get("parentItem") or "")
            if parent is not None and d.get("contentType") in READABLE:
                parent["attachments"].append({"key": d["key"], "contentType": d["contentType"]})


def _assemble(items: dict[str, dict], collections: list[dict], version: int) -> dict:
    names = {c["key"]: (c["data"]["name"], c["data"].get("parentCollection") or None) for c in collections}

    def path(key: str) -> str:
        parts, seen = [], set()
        while key and key in names and key not in seen:
            seen.add(key)
            parts.append(names[key][0])
            key = names[key][1]
        return " › ".join(reversed(parts))

    rows = []
    for it in sorted(items.values(), key=lambda i: i["key"]):
        rows.append({**it, "collections": [path(k) for k in it["collection_keys"] if k in names]})
    return {"schema_version": SCHEMA_VERSION, "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "library_version": version,
            "items": rows, "collections": sorted(path(k) for k in names)}


def build(client: api.Client) -> dict:
    objects, version = client.paginate("/items")
    items: dict[str, dict] = {}
    _apply(items, objects, [])
    collections, _ = client.paginate("/collections")
    return _assemble(items, collections, version)


def update(client: api.Client, old: dict) -> dict:
    since = int(old["library_version"])
    objects, version = client.paginate("/items", {"since": since, "includeTrashed": 1})
    deleted = client.deleted_since(since).get("items", [])
    items = {i["key"]: i for i in old["items"]}
    _apply(items, objects, deleted)
    collections, _ = client.paginate("/collections")
    return _assemble(items, collections, max(version, since))


def refresh(force: bool = False) -> dict:
    """Bring library.json up to date; returns a summary."""
    client = api.Client()
    out = out_path()
    old = None
    if os.path.exists(out) and not force:
        try:
            old = json.load(open(out))
        except ValueError:
            old = None
    incremental = bool(old and old.get("library_version") is not None and old.get("schema_version") == SCHEMA_VERSION
                       and all("collection_keys" in i for i in old["items"]))
    try:
        if incremental and not client.changed_since(int(old["library_version"])):
            return {"status": "unchanged", "library_version": old["library_version"]}
        snap = update(client, old) if incremental else build(client)
    finally:
        client.close()
    old_umask = os.umask(0o027)
    try:
        os.makedirs(config.KNOWLEDGE_DIR, exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, out)
    finally:
        os.umask(old_umask)
    return {"status": "updated" if incremental else "written", "items": len(snap["items"]),
            "collections": len(snap["collections"]), "library_version": snap["library_version"],
            "generated": snap["generated"]}


def main() -> None:
    print(refresh("--force" in sys.argv))


if __name__ == "__main__":
    main()
