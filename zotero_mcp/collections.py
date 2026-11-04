"""Tools on collections and tags."""
from typing import Annotated, Any

from pydantic import Field

from .core import ToolError, collections, fail, invalidate_collections, key_of, paginate, post_updates, req, tool


@tool
async def list_collections() -> dict:
    """All collections with key, full path ("Parent › Child") and number of items."""
    colls = await collections()
    rows = [{"key": k, "path": c["path"], "numItems": c["numItems"]} for k, c in colls.items()]
    return {"collections": sorted(rows, key=lambda c: c["path"].lower())}


@tool
async def create_collection(
    name: str,
    parent_key: Annotated[str | None, Field(description="Parent collection key; omit for top level")] = None,
) -> dict:
    """Create a collection, optionally nested inside another."""
    body = {"name": name, "parentCollection": key_of(parent_key) if parent_key else False}
    r = await req("POST", "/collections", json=[body])
    if r.status_code != 200:
        return fail(r)
    invalidate_collections()
    ok = r.json().get("successful", {})
    return {"status": "ok" if ok else "error",
            "key": next((v["key"] for v in ok.values()), None), "failed": r.json().get("failed") or None}


@tool
async def update_collection(
    key: str,
    name: str | None = None,
    parent_key: Annotated[str | None, Field(description="New parent collection key")] = None,
    move_to_top: Annotated[bool, Field(description="Make it a top-level collection")] = False,
) -> dict:
    """Rename a collection and/or move it under another collection (or to the top level)."""
    k = key_of(key)
    r = await req("GET", f"/collections/{k}")
    if r.status_code != 200:
        return fail(r)
    c = r.json()
    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if move_to_top:
        body["parentCollection"] = False
    elif parent_key:
        p, colls = key_of(parent_key), await collections()
        node = p
        while node:
            if node == k:
                raise ToolError("cannot move a collection inside itself or its subcollections")
            node = colls.get(node, {}).get("parent")
        body["parentCollection"] = p
    if not body:
        raise ToolError("nothing to change")
    r = await req("PATCH", f"/collections/{k}", json=body,
                  headers={"If-Unmodified-Since-Version": str(c["version"])})
    if r.status_code != 204:
        return fail(r)
    invalidate_collections()
    return {"status": "ok", "key": k, "path": (await collections()).get(k, {}).get("path")}


@tool
async def delete_collection(
    key: str,
    include_subcollections: Annotated[bool, Field(
        description="Required true if the collection has subcollections (they are deleted too)")] = False,
) -> dict:
    """Delete a collection. Its items stay in the library (and in other collections)."""
    k, colls = key_of(key), await collections()
    subs = [c["path"] for ck, c in colls.items() if c["parent"] == k]
    if subs and not include_subcollections:
        return {"status": "refused", "detail": "collection has subcollections; set include_subcollections",
                "subcollections": subs}
    r = await req("GET", f"/collections/{k}")
    if r.status_code != 200:
        return fail(r)
    r2 = await req("DELETE", f"/collections/{k}", headers={"If-Unmodified-Since-Version": str(r.json()["version"])})
    if r2.status_code != 204:
        return fail(r2)
    invalidate_collections()
    return {"status": "ok", "deleted": colls.get(k, {}).get("path", k)}


@tool
async def list_tags(limit: Annotated[int, Field(ge=1, le=100)] = 100, start: int = 0) -> dict:
    """Tags used in the library with number of items."""
    r = await req("GET", "/tags", params={"limit": limit, "start": start})
    if r.status_code != 200:
        return fail(r)
    return {"total": int(r.headers.get("Total-Results", "0")),
            "tags": [{"tag": t["tag"], "numItems": t.get("meta", {}).get("numItems")} for t in r.json()]}


@tool
async def rename_tag(old: str, new: str) -> dict:
    """Rename a tag on every item that has it (merges into `new` if that tag already exists)."""
    items = await paginate("/items", {"tag": old})
    objs = []
    for it in items:
        tags = [t for t in it["data"].get("tags", []) if t["tag"] != old]
        if new not in {t["tag"] for t in tags}:
            tags.append({"tag": new})
        objs.append({"key": it["key"], "version": it["version"], "tags": tags})
    if not objs:
        return {"status": "ok", "updated": 0, "detail": f"no items tagged {old!r}"}
    return await post_updates(objs)


@tool
async def delete_tag(tags: Annotated[list[str], Field(description="Tags to remove from the whole library")]) -> dict:
    """Remove tags from all items in the library. Items are not deleted."""
    r = await req("GET", "/items", params={"limit": 1})
    version = r.headers.get("Last-Modified-Version")
    if r.status_code != 200 or not version:
        return fail(r)
    r = await req("DELETE", "/tags", params={"tag": " || ".join(tags)},
                  headers={"If-Unmodified-Since-Version": version})
    if r.status_code != 204:
        return fail(r)
    return {"status": "ok", "deleted": tags}
