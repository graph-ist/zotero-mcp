"""Reading PDFs and EPUBs from the WebDAV copy of the Zotero storage: full text, text location for
highlights, annotations."""
import asyncio
import json
import os
import re
import zipfile
from typing import Annotated, Literal

from pydantic import Field

from . import config
from .core import ToolError, children, fetch_item, key_of, paginate, patch, post_items, req, tool


# ---- files -----------------------------------------------------------------------------------------
def pdf_bytes(att_key: str) -> bytes | None:
    path = os.path.join(config.WEBDAV_DIR, f"{att_key}.zip")
    if not os.path.isfile(path):
        return None
    with zipfile.ZipFile(path) as z:
        for info in z.infolist()[:20]:  # a Zotero attachment zip has one member; never scan a huge archive
            # The whole member is read into RAM, so refuse oversized (or zip-bomb) entries up front
            if info.file_size > config.MAX_PDF_BYTES:
                raise ToolError(f"attachment {att_key} is larger than {config.MAX_PDF_BYTES >> 20} MB, not read")
            if not info.filename.lower().endswith(".pdf") and info.file_size > 1 << 20:
                continue
            data = z.read(info)
            if data[:5] == b"%PDF-":
                return data
    return None


def epub_bytes(att_key: str) -> bytes | None:
    path = os.path.join(config.WEBDAV_DIR, f"{att_key}.zip")
    if not os.path.isfile(path):
        return None
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.filename.lower().endswith(".epub"):
                if info.file_size > config.MAX_PDF_BYTES:
                    raise ToolError(f"attachment {att_key} is larger than {config.MAX_PDF_BYTES >> 20} MB, not read")
                return z.read(info)
    return None


def ocr_cached(att_key: str, ext: str) -> str | None:
    """Path of the OCR result (pdf or txt) made by ocr_worker.py for a scanned PDF, if any."""
    path = os.path.join(config.OCR_DIR, "cache", f"{att_key}.{ext}")
    return path if os.path.isfile(path) else None


def queue_ocr(att_key: str) -> str:
    """Scanned PDF without OCR yet: queue it once (replaced on WebDAV only while unannotated)."""
    import ocr_worker
    path = os.path.join(config.OCR_DIR, "status", f"{att_key}.json")
    if os.path.exists(path):
        state = json.load(open(path)).get("state")
        if state in ("queued", "running", "failed"):
            return state
    ocr_worker.enqueue(att_key, "if-unannotated", "auto")
    return "queued"


def is_scan(pages: list[str]) -> bool:
    """Text on under 70% of pages (as in ocr_worker.py)."""
    return sum(len(t.strip()) >= 50 for t in pages) < 0.7 * len(pages)


def text_from_webdav(att_key: str, content_type: str) -> tuple[str | None, str | None]:
    """(text, OCR state when the PDF is a scan whose OCR is not ready)."""
    import pymupdf
    if content_type == "application/epub+zip":
        data, kind = epub_bytes(att_key), "epub"
    else:
        data, kind = pdf_bytes(att_key), "pdf"
    if data is None:
        return None, None
    with pymupdf.open(stream=data, filetype=kind) as doc:
        pages = [p.get_text() for p in doc]
    if kind == "pdf" and is_scan(pages):
        cached = ocr_cached(att_key, "txt")
        if cached:
            return open(cached, encoding="utf-8").read(), None
        return "\n\n".join(pages).strip(), queue_ocr(att_key)
    return "\n\n".join(pages).strip(), None


def page_range(spec: str, count: int) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+))?\s*", spec or "")
    if not m:
        raise ToolError("pages must be like '7' or '12-15'")
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    if not 1 <= a <= b <= count:
        raise ToolError(f"pages out of range: the PDF has {count} pages")
    if b - a >= 50:
        raise ToolError("at most 50 pages per call")
    return a, b


def page_text(att_key: str, spec: str) -> str | None:
    import pymupdf
    data = pdf_or_ocr(att_key)
    if data is None:
        return None
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        a, b = page_range(spec, doc.page_count)
        return "\n\n".join(f"[p. {i}]\n{doc[i - 1].get_text().strip()}" for i in range(a, b + 1))


def pdf_or_ocr(att_key: str) -> bytes | None:
    """The PDF bytes, or the OCR copy when the original is a scan that has been processed."""
    data = pdf_bytes(att_key)
    if data is None:
        return None
    cached = ocr_cached(att_key, "pdf")
    if cached:
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if is_scan([p.get_text() for p in doc]):
                return open(cached, "rb").read()
    return data


# ---- attachments ----------------------------------------------------------------------------------
READABLE = ("application/pdf", "application/epub+zip")


async def readable_attachment(key: str) -> dict:
    """The attachment itself, or the first PDF (else EPUB) attachment of a parent item."""
    it = await fetch_item(key)
    if it["data"]["itemType"] == "attachment":
        if it["data"].get("contentType") not in READABLE:
            raise ToolError("this attachment is not a PDF or EPUB")
        return it
    atts = [c for c in await children(it["key"])
            if c["data"]["itemType"] == "attachment" and c["data"].get("contentType") in READABLE]
    atts.sort(key=lambda c: c["data"].get("contentType") != "application/pdf")  # PDF first
    if not atts:
        raise ToolError("no PDF or EPUB attachment for this item")
    return atts[0]


async def pdf_attachment(key: str) -> dict:
    """The attachment itself, or the first PDF attachment of a parent item."""
    it = await fetch_item(key)
    if it["data"]["itemType"] == "attachment":
        if it["data"].get("contentType") != "application/pdf":
            raise ToolError("this attachment is not a PDF")
        return it
    for c in await children(it["key"]):
        if c["data"]["itemType"] == "attachment" and c["data"].get("contentType") == "application/pdf":
            return c
    raise ToolError("no PDF attachment for this item")


# ---- text location ---------------------------------------------------------------------------------
def _norm_word(w: str) -> str:
    return re.sub(r"\W+", "", w.lower())


def locate(data: bytes, text: str, page: int | None, occurrence: int) -> dict | None:
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


# ---- tools ---------------------------------------------------------------------------------------
@tool
async def get_fulltext(
    key: Annotated[str, Field(description="Item key or PDF/EPUB attachment key")],
    offset: Annotated[int, Field(ge=0)] = 0,
    max_chars: Annotated[int, Field(ge=1000, le=100000)] = 30000,
    pages: Annotated[str | None, Field(description="Page range of the PDF to read, e.g. '12-15' or '7' "
                                       "(1-based, as in get_outline); at most 50 pages. Omit for the whole text")] = None,
) -> dict:
    """Text of an item's PDF or EPUB: Zotero's full-text index first, else read from the WebDAV copy
    (scanned PDFs use their OCR text once ocr_pdf has processed them). With `pages`, only those
    pages of the PDF, each starting with a [p. N] marker: use get_outline to pick a section.

    Long texts are paginated: call again with offset = previous offset + returned length."""
    att = await readable_attachment(key)
    att_key, content_type = att["key"], att["data"].get("contentType", "")
    text, source, ocr = None, None, None
    if pages:
        if content_type != "application/pdf":
            raise ToolError("pages applies to PDF attachments only")
        text, source = await asyncio.to_thread(page_text, att_key, pages), "webdav_pdf_pages"
        if text is None:
            raise ToolError("PDF not found on WebDAV: sync files from Zotero desktop first")
    if text is None:
        r = await req("GET", f"/items/{att_key}/fulltext")
        if r.status_code == 200 and r.json().get("content"):
            text, source = r.json()["content"], "zotero_fulltext_index"
        else:
            text, ocr = await asyncio.to_thread(text_from_webdav, att_key, content_type)
            source = "webdav_epub" if content_type == "application/epub+zip" else "webdav_pdf"
    note = ({"ocr": ocr, "ocr_note": "Scanned PDF: OCR runs in the background (minutes to hours, see "
             "ocr_status); the text below is what the scan has now."} if ocr else {})
    if not text and not ocr:
        raise ToolError("not indexed in Zotero and no text on WebDAV: sync files first")
    text = text or ""
    chunk = text[offset:offset + max_chars]
    return {"attachment_key": att_key, "source": source, "total_chars": len(text), "offset": offset,
            "returned_chars": len(chunk), "more": offset + len(chunk) < len(text), "text": chunk, **note}


@tool
async def get_annotations(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
    start: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Highlights, underlines and comments made in Zotero's PDF reader, in page order.
    Paginated: `total` is the full count; call again with start = start + limit while more."""
    out = await annotations_of(key)
    page = out[start:start + limit]
    return {"total": len(out), "start": start, "returned": len(page),
            "more": start + len(page) < len(out), "annotations": page}


async def annotations_of(key: str) -> list[dict]:
    it = await fetch_item(key)
    if it["data"]["itemType"] == "attachment":
        atts = [it]
    else:
        atts = [c for c in await children(it["key"]) if c["data"]["itemType"] == "attachment"]
    out = []
    for a in atts:
        for c in await paginate(f"/items/{a['key']}/children"):
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
    return out


COLORS = {"yellow": "#ffd400", "red": "#ff6666", "green": "#5fb236", "blue": "#2ea8e5",
          "purple": "#a28ae5", "magenta": "#e56eee", "orange": "#f19837", "gray": "#aaaaaa"}


@tool
async def annotate_pdf(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    text: Annotated[str, Field(description="Exact passage to mark, as it appears in the PDF")],
    type: Literal["highlight", "underline"] = "highlight",
    comment: Annotated[str | None, Field(description="Comment shown with the annotation")] = None,
    color: Literal["yellow", "red", "green", "blue", "purple", "magenta", "orange", "gray"] = "yellow",
    page: Annotated[int | None, Field(ge=1, description="1-based page to search; default all pages")] = None,
    occurrence: Annotated[int, Field(ge=0, description="Which match to use if the text appears more than once")] = 0,
    tags: list[str] | None = None,
    dry_run: Annotated[bool, Field(description="Only locate the text, do not create the annotation")] = False,
) -> dict:
    """Highlight or underline a passage in a PDF; it appears in Zotero's reader on every device.

    The PDF file itself is not modified. Needs the PDF on WebDAV with text; scans work once ocr_pdf
    has processed them (the OCR copy has the same page geometry)."""
    att = await pdf_attachment(key)
    data = await asyncio.to_thread(pdf_bytes, att["key"])
    if data is None:
        raise ToolError("PDF not found on WebDAV: sync files from Zotero desktop first")
    loc = await asyncio.to_thread(locate, data, text, page, occurrence)
    ocr = ocr_cached(att["key"], "pdf")
    if loc is None and ocr:  # scanned page: same positions in the OCR copy
        loc = await asyncio.to_thread(locate, open(ocr, "rb").read(), text, page, occurrence)
    if loc is None:
        raise ToolError("text not found in the PDF; copy it exactly (try a shorter passage, "
                        "avoid words hyphenated across lines) or check the page number"
                        + ("" if ocr else "; if the PDF is a scan, run ocr_pdf first"))
    if dry_run:
        return {"status": "found", "attachment_key": att["key"], "page": loc["pageLabel"],
                "lines": len(loc["rects"])}
    ann = {"itemType": "annotation", "parentItem": att["key"], "annotationType": type,
           "annotationText": " ".join(text.split()), "annotationComment": comment or "",
           "annotationColor": COLORS[color], "annotationPageLabel": loc["pageLabel"],
           "annotationSortIndex": loc["sortIndex"],
           "annotationPosition": json.dumps({"pageIndex": loc["pageIndex"], "rects": loc["rects"]},
                                            separators=(",", ":")),
           "tags": [{"tag": t} for t in tags or []]}
    out = await post_items([ann])
    out |= {"attachment_key": att["key"], "page": loc["pageLabel"]}
    return out


# ---- export of annotations ------------------------------------------------------------------------
NOTE_MARK = 'data-export="annotations"'


def _cite(d: dict) -> str:
    from .core import year_of
    names = [c.get("lastName") or c.get("name") or "" for c in d.get("creators", []) if c.get("creatorType") == "author"]
    who = names[0] if len(names) == 1 else f"{names[0]} & {names[1]}" if len(names) == 2 else f"{names[0]} et al." if names else ""
    return ", ".join(x for x in (who, year_of(d)) if x)


def annotations_markdown(item: dict, anns: list[dict]) -> str:
    d = item["data"]
    head = f"## {d.get('title') or '(untitled)'}"
    cite = _cite(d)
    lines = [f"{head} ({cite}) [{item['key']}]" if cite else f"{head} [{item['key']}]"]
    for a in anns:
        page = f"p. {a['page']}: " if a.get("page") else ""
        text = f"\"{a['text']}\"" if a.get("text") else ""
        if a.get("type") == "underline" and text:
            text = f"_{text}_"
        parts = [text] if text else []
        if a.get("comment"):
            parts.append(f"— {a['comment']}")
        if a.get("tags"):
            parts.append(" ".join(f"#{t.replace(' ', '_')}" for t in a["tags"]))
        if a.get("color") and a["color"].lower() != "#ffd400":
            name = next((n for n, h in COLORS.items() if h == a["color"].lower()), a["color"])
            parts.append(f"({name})")
        lines.append(f"- {page}{' '.join(parts)}".rstrip())
    return "\n".join(lines)


def _md_to_html(md: str) -> str:
    import html as h
    out, in_list = [], False
    for line in md.split("\n"):
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{h.escape(line[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if line.startswith("## "):
            out.append(f"<h2>{h.escape(line[3:])}</h2>")
        elif line.strip():
            out.append(f"<p>{h.escape(line)}</p>")
    if in_list:
        out.append("</ul>")
    return f'<div {NOTE_MARK}>' + "".join(out) + "</div>"


@tool
async def export_annotations(
    key: Annotated[str | None, Field(description="Item key: its highlights, underlines and comments")] = None,
    collection_key: Annotated[str | None, Field(description="Or: every annotated item of a collection (up to 50 items)")] = None,
    save_as_note: Annotated[bool, Field(description="With key: also save the Markdown as a child note of the item "
                                        "(replaces the note made by an earlier export)")] = False,
) -> dict:
    """Reading notes from Zotero's PDF annotations as Markdown: one section per item with
    citation and key, one line per annotation with page, quoted text, comment and tags. Paste it
    into Obsidian or save it as a note on the item."""
    if bool(key) == bool(collection_key):
        raise ToolError("give key or collection_key")
    if save_as_note and not key:
        raise ToolError("save_as_note needs key")
    items = [await fetch_item(key)] if key else await paginate(f"/collections/{key_of(collection_key)}/items/top", cap=50)
    sections, count = [], 0
    for it in items:
        if it["data"]["itemType"] in ("attachment", "note", "annotation"):
            raise ToolError("give the key of a regular item, not of an attachment or note")
        anns = await annotations_of(it["key"])
        if anns:
            sections.append(annotations_markdown(it, anns))
            count += len(anns)
    md = "\n\n".join(sections)
    out = {"items": len(sections), "annotations": count, "markdown": md}
    if save_as_note and md:
        existing = next((c for c in await children(items[0]["key"])
                         if c["data"]["itemType"] == "note" and NOTE_MARK in (c["data"].get("note") or "")), None)
        if existing:
            res = await patch(existing["key"], existing["version"], {"note": _md_to_html(md)})
            out["note_key"], out["note"] = existing["key"], "replaced"
        else:
            res = await post_items([{"itemType": "note", "parentItem": items[0]["key"], "note": _md_to_html(md),
                                     "tags": [{"tag": "annotations"}], "collections": [], "relations": {}}])
            out["note_key"], out["note"] = (res.get("created_keys") or [None])[0], "created"
        if res.get("status") not in ("ok",):
            out["note_error"] = res
    return out
