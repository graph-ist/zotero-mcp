"""Tools that hand work to the background worker on the Pi: OCR, attaching downloaded PDFs,
and the library snapshot for other services."""
import asyncio
import json
import os
import re
import time
from typing import Annotated, Literal

import httpx
from pydantic import Field

from . import api, config
from .core import ToolError, children, fetch_item, post_items, public, req, tool, year_of
from .pdf import pdf_attachment


@tool
async def ocr_pdf(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    replace: Annotated[bool, Field(description="Also replace the PDF on WebDAV with the OCR version, "
                                   "so the text can be selected in Zotero on every device (original kept on the Pi)")] = False,
    mode: Annotated[Literal["normal", "rasterize", "enhance"], Field(
        description="normal: text layer over the original pages. rasterize: pages redrawn as images and "
                    "deskewed, for PDFs whose OCR failed (corrupt images). enhance: as rasterize plus contrast/"
                    "sharpening, only for poor scans with bad text. rasterize/enhance redo the OCR even if done")] = "normal",
) -> dict:
    """Queue OCR for a scanned PDF. Runs in the background on the Pi (minutes to hours for a book):
    the text goes to Zotero's full-text index, and get_fulltext / annotate_pdf work on the scan.
    PDFs that already have text are left alone. Follow progress with ocr_status."""
    import ocr_worker
    att = await pdf_attachment(key)
    status = await asyncio.to_thread(ocr_worker.enqueue, att["key"], "yes" if replace else "no", "tool", mode)
    return {"status": "queued", "attachment_key": att["key"], "replace": replace, "mode": mode,
            "queued_at": status["updated"]}


@tool
async def ocr_status(
    key: Annotated[str | None, Field(description="Item or attachment key; omit to list every OCR job")] = None,
) -> dict:
    """State of OCR jobs: queued, running, done (pages, seconds, indexed, replaced), skipped or failed."""
    folder = os.path.join(config.OCR_DIR, "status")
    if key:
        att = await pdf_attachment(key)
        path = os.path.join(folder, f"{att['key']}.json")
        if not os.path.exists(path):
            return {"attachment_key": att["key"], "state": "never queued"}
        return json.load(open(path))
    jobs = [json.load(open(os.path.join(folder, n))) for n in sorted(os.listdir(folder))] if os.path.isdir(folder) else []
    return {"total": len(jobs), "jobs": sorted(jobs, key=lambda j: j.get("updated", ""), reverse=True)}


@tool
async def refresh_library_snapshot() -> dict:
    """Update the library index that the DOI server uses to flag which papers are already in Zotero
    (keys and tags). Call it after adding or changing many items; a no-op if nothing changed."""
    import snapshot
    return await asyncio.to_thread(snapshot.refresh)


# ---- attach_pdf -----------------------------------------------------------------------------------
def pdf_filename(d: dict) -> str:
    """'Casner 2005.pdf', 'Miller Ward 2002.pdf': first two surnames and year, as in the library."""
    names = [c.get("lastName") or c.get("name") or "" for c in d.get("creators", [])
             if c.get("creatorType") in ("author", "editor")][:2]
    base = " ".join(filter(None, names + [year_of(d)])) or (d.get("title") or "Full text")[:60]
    return re.sub(r'[\\/:*?"<>|]+', " ", base).strip() + ".pdf"


async def open_access_pdfs(doi: str) -> list[dict]:
    """Open access PDF links for a DOI from OpenAlex, best first: url, version, license, host.
    Several are kept because publishers often refuse automated downloads; repositories do not."""
    r = await public().get(f"https://api.openalex.org/works/doi:{doi}")
    if r.status_code != 200:
        return []
    w, out = r.json(), []
    for loc in [w.get("best_oa_location")] + (w.get("locations") or []):
        if loc and loc.get("pdf_url") and loc["pdf_url"] not in [o["url"] for o in out]:
            out.append({"url": loc["pdf_url"], "version": loc.get("version"), "license": loc.get("license"),
                        "host": (loc.get("source") or {}).get("display_name")})
    return out


async def download_pdf(url: str) -> bytes:
    data = bytearray()
    try:
        async with public().stream("GET", url, headers={"Accept": "application/pdf"}) as r:
            if r.status_code != 200:
                raise ToolError(f"download failed (HTTP {r.status_code}) from {url}")
            async for chunk in r.aiter_bytes():
                data += chunk
                if len(data) > config.MAX_PDF_BYTES:
                    raise ToolError(f"file larger than {config.MAX_PDF_BYTES >> 20} MB")
    except httpx.HTTPError as e:
        raise ToolError(f"download failed ({type(e).__name__}) from {url}") from None
    if bytes(data[:5]) != b"%PDF-":
        raise ToolError(f"{url} did not return a PDF (probably a landing page or a paywall); "
                        "open it in a browser, or pass a direct PDF link as url")
    return bytes(data)


@tool
async def attach_pdf(
    key: Annotated[str, Field(description="Key of the parent item")],
    url: Annotated[str | None, Field(description="Direct PDF link; omit to look up an open access PDF "
                                     "for the item's DOI on OpenAlex")] = None,
    allow_second_pdf: Annotated[bool, Field(description="Attach even if the item already has a PDF")] = False,
) -> dict:
    """Attach a PDF to an item: an open access copy found via the DOI, or a given link. The file goes
    to the WebDAV storage through the background worker (seconds), then Zotero clients download it;
    a scan is also OCRed. Only for legally available copies (open access, author versions)."""
    import ocr_worker
    item = (await fetch_item(key))["data"]
    if item["itemType"] in ("attachment", "note", "annotation"):
        raise ToolError("give the key of a regular item, not of an attachment or note")
    pdfs = [c["data"]["key"] for c in await children(item["key"]) if c["data"].get("contentType") == "application/pdf"]
    if pdfs and not allow_second_pdf:
        return {"status": "already_has_pdf", "attachment_keys": pdfs}
    if url:
        sources = [{"url": url}]
    else:
        doi = next(iter(api.item_dois(item)), None)
        if not doi:
            raise ToolError("the item has no DOI: pass url")
        sources = await open_access_pdfs(doi)
        if not sources:
            return {"status": "no_open_access_pdf", "doi": doi}
    data, tried = None, []
    for source in sources:
        try:
            data = await download_pdf(source["url"])
            break
        except ToolError as e:
            tried.append(str(e))
    if data is None:
        return {"status": "download_failed", "tried": tried,
                "hint": "publishers often block automated downloads: open a link in a browser, "
                        "or pass a direct PDF link (e.g. an author or repository copy) as url"}

    filename = pdf_filename(item)
    r = await req("GET", f"{api.API}/items/new", params={"itemType": "attachment", "linkMode": "imported_file"})
    tpl = r.json()
    tpl.update(parentItem=item["key"], title=filename[:-4], filename=filename, contentType="application/pdf",
               url=source["url"], accessDate=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    tpl = {k: v for k, v in tpl.items() if k not in ("md5", "mtime")}
    out = await post_items([tpl])
    if out.get("status") != "ok":
        raise ToolError(f"attachment not created: {out}")
    att = out["created_keys"][0]
    try:
        os.makedirs(os.path.join(config.OCR_DIR, "incoming"), exist_ok=True)
        old = os.umask(0o007)
        try:
            with open(os.path.join(config.OCR_DIR, "incoming", f"{att}.pdf"), "wb") as f:
                f.write(data)
        finally:
            os.umask(old)
        await asyncio.to_thread(ocr_worker.enqueue_attach, att, filename)
    except Exception:
        await req("DELETE", f"/items/{att}", headers={"If-Unmodified-Since-Version": str(
            (await fetch_item(att))["version"])})
        raise
    return {"status": "queued", "attachment_key": att, "filename": filename, "bytes": len(data),
            **{k: v for k, v in source.items() if v}, "next": "check ocr_status; then sync Zotero"}
