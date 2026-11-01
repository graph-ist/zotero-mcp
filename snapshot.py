"""Write a snapshot of the Zotero library to KNOWLEDGE_DIR/library.json for other services on
the Pi (DOI MCP literature maps, knowledge views) that must not call the Zotero API.

Items (no notes, annotations or attachments as items) with key, type, title, creators, year,
DOI (also "DOI:" lines in Extra), ISBN, tags, collection paths and PDF/EPUB attachment keys.
Nothing is written when the library version has not changed since the last snapshot.
Usage: snapshot.py [--force]   Env: ZOTERO_USER_ID, ZOTERO_API_KEY, KNOWLEDGE_DIR
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

OUT_DIR = os.environ.get("KNOWLEDGE_DIR", "/var/lib/knowledge")
OUT = os.path.join(OUT_DIR, "library.json")
BASE = f"https://api.zotero.org/users/{os.environ.get('ZOTERO_USER_ID', '')}"


def get(path: str, since: int | None = None) -> tuple[int, list | dict, dict]:
    headers = {"Zotero-API-Key": os.environ.get("ZOTERO_API_KEY", ""), "Zotero-API-Version": "3"}
    if since is not None:
        headers["If-Modified-Since-Version"] = str(since)
    req = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, [], dict(e.headers)
        raise


def all_pages(path: str) -> tuple[list, int]:
    out, start, version = [], 0, 0
    while True:
        sep = "&" if "?" in path else "?"
        _, page, headers = get(f"{path}{sep}limit=100&start={start}")
        version = int(headers.get("Last-Modified-Version", version))
        out += page
        start += len(page)
        if len(page) < 100 or start >= int(headers.get("Total-Results", start)):
            return out, version


def norm_doi(d: str) -> str:
    return re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", d.strip(), flags=re.I).rstrip(".").lower()


def build() -> dict:
    collections, _ = all_pages("/collections")
    names = {c["key"]: (c["data"]["name"], c["data"].get("parentCollection") or None) for c in collections}

    def path(key: str) -> str:
        parts, seen = [], set()
        while key and key in names and key not in seen:
            seen.add(key)
            parts.append(names[key][0])
            key = names[key][1]
        return " › ".join(reversed(parts))

    objects, version = all_pages("/items")
    attachments: dict[str, list] = {}
    for o in objects:
        d = o["data"]
        if d["itemType"] == "attachment" and d.get("parentItem") and d.get("contentType") in (
                "application/pdf", "application/epub+zip"):
            attachments.setdefault(d["parentItem"], []).append({"key": d["key"], "contentType": d["contentType"]})
    items = []
    for o in objects:
        d = o["data"]
        if d["itemType"] in ("attachment", "note", "annotation"):
            continue
        extra = [norm_doi(x) for x in re.findall(r"^DOI:\s*(\S+)", d.get("extra") or "", re.M | re.I)]
        year = re.search(r"\d{4}", d.get("date") or "")
        items.append({
            "key": d["key"], "type": d["itemType"], "title": d.get("title") or "",
            "creators": [c.get("lastName") or c.get("name") or "" for c in d.get("creators", [])],
            "year": int(year.group(0)) if year else None,
            "doi": norm_doi(d["DOI"]) if d.get("DOI") else None, "extra_dois": extra,
            "isbn": d.get("ISBN") or None, "language": d.get("language") or None,
            "tags": [t["tag"] for t in d.get("tags", [])],
            "collections": [path(k) for k in d.get("collections", [])],
            "attachments": attachments.get(d["key"], [])})
    return {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "library_version": version,
            "items": items, "collections": sorted(path(k) for k in names)}


def main() -> None:
    force = "--force" in sys.argv
    old_version = None
    if os.path.exists(OUT) and not force:
        try:
            old_version = json.load(open(OUT)).get("library_version")
        except ValueError:
            pass
    if old_version is not None:
        status, _, _ = get("/items?limit=1&format=keys", since=old_version)
        if status == 304:
            print("library unchanged (version", old_version, ")")
            return
    snap = build()
    old_umask = os.umask(0o027)
    try:
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, OUT)
    finally:
        os.umask(old_umask)
    print(len(snap["items"]), "items,", len(snap["collections"]), "collections, version", snap["library_version"])


if __name__ == "__main__":
    main()
