"""Tools that read the library snapshot and the full texts together: bibliographies saved for
the DOI MCP's literature maps (save_references) and the claim register for drafts (check_claims)."""
import asyncio
import json
from typing import Annotated, Any

from pydantic import Field

from . import bibliography
from . import claims as cl
from .core import ToolError, key_of, tool
from .pdf import pages_from_webdav, pdf_attachment, text_from_webdav


def snapshot_items() -> list[dict]:
    import snapshot  # top-level module next to the package
    try:
        return json.load(open(snapshot.out_path(), encoding="utf-8")).get("items", [])
    except (OSError, ValueError):
        return []


@tool
async def save_references(
    item_keys: Annotated[list[str] | None, Field(description="Item keys (max 40 per call)")] = None,
    collection: Annotated[str | None, Field(description="Collection name or path: every item in it with a PDF/EPUB")] = None,
    only_without_doi: Annotated[bool, Field(description="With collection: only items without a DOI (books, humanities)")] = True,
    force: Annotated[bool, Field(description="Extract again even if already done")] = False,
) -> dict:
    """Store the bibliography (reference list) of the PDF or EPUB of many items in
    KNOWLEDGE_DIR/references/KEY.json, for other services: to read one paper's references use
    extract_references. The DOI MCP's literature maps use it as the
    work's references when OpenAlex and Semantic Scholar have none: books and philosophy.
    Reads the whole file from WebDAV (Zotero's text index stops after ~200k characters, before
    most book bibliographies); scans use their OCR text. Max 40 items per call; items already
    done are skipped unless force. 'not_found' = no bibliography recognised in the text."""
    if collection:
        want = collection.strip().lower()
        items = [it for it in snapshot_items()
                 if any(p.lower() == want or p.lower().endswith(" › " + want) or p.lower().startswith(want + " › ")
                        for p in it.get("collections", []))
                 and it.get("attachments") and not (only_without_doi and it.get("doi"))]
        keys = [it["key"] for it in items]
        if not keys:
            raise ToolError("no item with a PDF/EPUB in that collection (or the library snapshot is missing: refresh_library_snapshot)")
    else:
        keys = [key_of(k) for k in item_keys or []]
    if not keys:
        raise ToolError("give item_keys or collection")
    todo = [k for k in dict.fromkeys(keys) if force or not bibliography.load(k)]
    done_before = len(set(keys)) - len(todo)
    results, remaining = {}, todo[40:]
    snap = {it["key"]: it for it in snapshot_items()}
    for key in todo[:40]:
        try:
            # attachment from the snapshot (PDF first, else EPUB); else ask the API for the PDF
            atts = sorted((snap.get(key) or {}).get("attachments") or [], key=lambda a: a["contentType"] != "application/pdf")
            if atts:
                att_key, ctype = atts[0]["key"], atts[0]["contentType"]
            else:
                att = await pdf_attachment(key)
                att_key, ctype = att["key"], "application/pdf"
            text, ocr = await asyncio.to_thread(text_from_webdav, att_key, ctype)
            if ocr:
                results[key] = f"scan waiting for OCR ({ocr}): try again later"
                continue
            if not text:
                results[key] = "no file on WebDAV"
                continue
            entries = await asyncio.to_thread(bibliography.extract, text)
            bibliography.save(key, att_key, entries, len(text))
            results[key] = "not_found" if entries is None else len(entries)
        except ToolError as e:
            results[key] = f"error: {e}"
    found = [v for v in results.values() if isinstance(v, int)]
    return {"status": "ok", "extracted": results, "items_with_bibliography": len(found),
            "references": sum(found), "already_done": done_before,
            "remaining": len(remaining), **({"next": "call again for the remaining items"} if remaining else {})}


@tool
async def check_claims(
    claims: Annotated[list[dict], Field(description="Max 30. Each: {'claim': the sentence as written in the draft, "
                                        "'source': Zotero key, DOI or 'Surname year' as cited, 'title': title of the cited work from the reference list (give it: "
                                        "same author and year is often another paper), optional 'page': cited page, "
                                        "optional 'keywords': words in the source's language when the draft is in another, "
                                        "optional 'quotes': phrases the draft attributes verbatim (text in quotation marks is taken automatically)}")],
) -> dict:
    """Claim register for a draft: for each claim, the cited source in the library and the
    passages of its full text (PDF/EPUB on WebDAV, OCR for scans) that best match it, with page
    and a zotero://open-pdf link. Use it to check a draft before it is final.

    Workflow: you extract the claims from the draft (each sentence that asserts something and
    cites, or should cite, a source) and classify them (empirical, conceptual, thesis); this tool
    finds the evidence; then you read the passages and give each claim a verdict: 'supported'
    (a passage says it), 'partly' (says less, or something nearby), 'not found' (passages
    unrelated: the source may not say it, or say it elsewhere), 'source missing' (not in the
    library), 'uncited' (needs a source). Never mark 'supported' without quoting the passage and
    its page; say the reading level: 'full text' or 'metadata only' (no file). Matching is by
    words, not meaning: a low terms_matched is a reason to look again, not proof of absence.
    Quoted phrases are searched verbatim in the whole text: 'found_on_pages': [] means the
    source does not contain them (check OCR quality before calling it a misquotation)."""
    if not claims or len(claims) > 30:
        raise ToolError("give 1-30 claims")
    items = snapshot_items()
    if not items:
        raise ToolError("library snapshot missing: refresh it first")
    cache: dict[str, Any] = {}
    out = []
    for c in claims:
        claim, source = (c.get("claim") or "").strip(), (c.get("source") or "").strip()
        row: dict[str, Any] = {"claim": claim, "source": source}
        out.append(row)
        if not claim:
            row["status"] = "empty claim"
            continue
        if not source:
            row["status"] = "uncited"
            continue
        found, how = cl.resolve_source(source, items, c.get("title"))
        row["matched_by"] = how
        if not found:
            row["status"] = "source not in library"
            continue
        if how in ("author-year", "author-title", "author (year differs: other edition?)") and not c.get("title"):
            row["check"] = "matched without the cited title: confirm item.title is the cited work"
        if len(found) > 1:
            row["status"] = "ambiguous source"
            row["candidates"] = [{"key": i["key"], "title": i["title"], "year": i.get("year"), "creators": i.get("creators")[:3]}
                                 for i in found[:8]]
            continue
        it = found[0]
        row["item"] = {"key": it["key"], "title": it["title"], "year": it.get("year"), "creators": (it.get("creators") or [])[:3]}
        atts = sorted(it.get("attachments") or [], key=lambda a: a["contentType"] != "application/pdf")
        atts = [a for a in atts if a["contentType"] in ("application/pdf", "application/epub+zip")]
        if not atts:
            row["status"], row["level"] = "no file", "metadata only"
            continue
        att = atts[0]
        if att["key"] not in cache:
            pages, ocr = await asyncio.to_thread(pages_from_webdav, att["key"], att["contentType"])
            cache[att["key"]] = (cl.windows(pages) if pages else None, ocr, pages)
        wins, ocr, pages = cache[att["key"]]
        if not wins:
            row["status"], row["level"] = ("scan waiting for OCR" if ocr else "no text"), "metadata only"
            continue
        query = claim + " " + " ".join(c.get("keywords") or [])
        passages = cl.best_passages(query, wins)
        cited = str(c.get("page") or "").strip()
        if cited and cited not in {p["page"] for p in passages}:
            on_page = cl.best_passages(query, [w for w in wins if w["page"] == cited], k=1)
            if on_page:
                passages.append({**on_page[0], "cited_page": True})
        pdf = att["contentType"] == "application/pdf"
        for p in passages:
            if pdf and p["page"] != "?":
                p["link"] = f"zotero://open-pdf/library/items/{att['key']}?page={p.pop('n')}"
            p.pop("n", None)
        row.update(status="passages" if passages else "no matching passage", level="full text", passages=passages)
        qs = cl.quotes(claim) + list(c.get("quotes") or [])
        if qs:
            # a quotation that is not in the source is the clearest sign of a wrong attribution
            row["quotes"] = [{"phrase": q, "found_on_pages": [h["page"] for h in cl.find_exact(pages, q)]} for q in qs]
    counts: dict[str, int] = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"claims": out, "summary": counts}
