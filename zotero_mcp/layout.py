"""Figures and tables on a PDF page, found from the page geometry (raster images, clusters of
vector drawings, ruled tables) and matched to their captions, and area annotations ("image"
annotations in Zotero) drawn around them or around any rectangle.

Detection is geometric, not semantic: a box covers the graphic, and labels set as text inside a
figure, or the header row of a table without rules, can fall outside it. Scanned pages have no
text layer and one full-page image, so nothing is detected there; a rectangle still works."""
import asyncio
import contextlib
import io
import json
import re
from typing import Annotated, Literal

from pydantic import Field

from .core import ToolError, post_items, tool
from .pdf import COLORS, ocr_cached, pdf_attachment, pdf_bytes

CAPTION_RE = re.compile(
    r"^\s*(?P<kind>figure|fig\.?|table|tab\.?|tabella|figura|fig|abbildung|abb\.?|tabelle|chart|plate|scheme)"
    r"\s*(?P<num>\d+[a-z]?|[IVXL]+)\s*(?:[.:|—–-]|\n|$)", re.I)
KIND = {"fig": "Figure", "figure": "Figure", "figura": "Figure", "abbildung": "Figure", "abb": "Figure",
        "chart": "Figure", "plate": "Figure", "scheme": "Figure",
        "table": "Table", "tab": "Table", "tabella": "Table", "tabelle": "Table"}


def caption_label(kind: str, num: str) -> str:
    return f"{KIND.get(kind.lower().rstrip('.'), kind.title())} {num}"


def norm_label(label: str) -> str | None:
    """'fig. 3', 'Figure 3', 'Abb. 3' -> 'Figure 3'; None if it is not a figure/table label."""
    m = CAPTION_RE.match(label.strip() + " ")
    return caption_label(m.group("kind"), m.group("num")) if m else None


def _area(r) -> float:
    return max(0.0, r[2] - r[0]) * max(0.0, r[3] - r[1])


def _inter(a, b) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def page_regions(page) -> dict:
    """Regions of one PyMuPDF page in page coordinates (points, origin top-left)."""
    W, H = page.rect.width, page.rect.height
    full = W * H
    warnings = []
    words = page.get_text("words")
    raw = []  # (source, rect)
    for info in page.get_image_info():
        r = tuple(info["bbox"])
        if _area(r) >= 0.6 * full and not words:  # a scan, often letterboxed to keep its aspect ratio
            warnings.append("scanned page (one full-page image, no text): regions cannot be detected; pass rect")
            continue
        raw.append(("image", r))
    drawings = page.get_drawings()
    if hasattr(page, "cluster_drawings") and drawings:
        try:
            for r in page.cluster_drawings(drawings=drawings):
                raw.append(("drawing", (r.x0, r.y0, r.x1, r.y1)))
        except Exception:  # noqa: BLE001  pathological vector content: images and tables still count
            warnings.append("vector graphics could not be clustered")
    raw = [(s, r) for s, r in raw if 0.01 * full <= _area(r) <= 0.95 * full
           and (r[2] - r[0]) > 0.04 * W and (r[3] - r[1]) > 0.02 * H]
    tables = []
    if any(d["rect"].height > 2 or d["rect"].width > 2 for d in drawings):
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                found = page.find_tables()
                tables = [tuple(t.bbox) for t in found.tables if t.row_count >= 2]
            except Exception:  # noqa: BLE001
                warnings.append("table detection failed on this page")
    tables = [t for t in tables if _area(t) >= 0.005 * full]
    # merge touching or overlapping graphic fragments (panels of one figure), keep tables apart
    gap = 0.02 * max(W, H)
    merged: list[list] = []
    for s, r in sorted(raw, key=lambda x: (x[1][1], x[1][0])):
        if any(_inter(r, t) >= 0.8 * _area(r) for t in tables):
            continue  # the rules of a table, already a table
        grown = (r[0] - gap, r[1] - gap, r[2] + gap, r[3] + gap)
        for m in merged:
            if _inter(grown, m[1]) > 0:
                m[1] = _union(m[1], r)
                m[0] = m[0] if m[0] == s else "figure"
                break
        else:
            merged.append([s, r])
    changed = True
    while changed:  # unions can make earlier regions overlap
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                a, b = merged[i][1], merged[j][1]
                if _inter((a[0] - gap, a[1] - gap, a[2] + gap, a[3] + gap), b) > 0:
                    merged[i] = ["figure", _union(a, b)]
                    del merged[j]
                    changed = True
                    break
            if changed:
                break
    regions = [{"kind": "figure", "source": s, "rect": r} for s, r in merged] + \
              [{"kind": "table", "source": "table", "rect": t} for t in tables]

    # captions: text blocks that start with 'Figure 3:', 'Table 2.', 'Abb. 4 ' ...
    caps = []
    for b in page.get_text("blocks"):
        if b[6] != 0:
            continue
        m = CAPTION_RE.match(b[4])
        if m:
            caps.append({"label": caption_label(m.group("kind"), m.group("num")), "rect": tuple(b[:4]),
                         "text": " ".join(b[4].split())[:300], "table": KIND.get(m.group("kind").lower().rstrip("."), "") == "Table"})
    pairs = []
    for ci, c in enumerate(caps):
        for ri, rg in enumerate(regions):
            r, cr = rg["rect"], c["rect"]
            overlap = max(0.0, min(r[2], cr[2]) - max(r[0], cr[0]))
            if overlap < 0.3 * min(r[2] - r[0], cr[2] - cr[0]):
                continue
            below = cr[1] >= r[3] - 2
            above = cr[3] <= r[1] + 2
            if not (below or above):
                continue
            dist = (cr[1] - r[3]) if below else (r[1] - cr[3])
            if dist > 0.15 * H:
                continue
            expected = above if c["table"] else below  # tables are captioned above, figures below
            score = dist / H - (0.05 if expected else 0) - (0.05 if (rg["kind"] == "table") == c["table"] else 0)
            pairs.append((score, ci, ri))
    used_c, used_r = set(), set()
    for _, ci, ri in sorted(pairs):
        if ci in used_c or ri in used_r:
            continue
        used_c.add(ci)
        used_r.add(ri)
        c = caps[ci]
        regions[ri] |= {"caption_label": c["label"], "caption_text": c["text"], "caption_rect": c["rect"],
                        "kind": "table" if c["table"] else "figure"}
    regions.sort(key=lambda g: (g["rect"][1], g["rect"][0]))
    for i, g in enumerate(regions, 1):
        g["id"] = i
    unmatched = [c["label"] for i, c in enumerate(caps) if i not in used_c]
    if unmatched:
        warnings.append(f"captions without a detected graphic: {unmatched} (text-only table or figure? pass rect)")
    return {"width": W, "height": H, "regions": regions, "warnings": warnings}


def _norm_rect(r, W, H) -> list[float]:
    return [round(r[0] / W, 4), round(r[1] / H, 4), round(r[2] / W, 4), round(r[3] / H, 4)]


def layout(data: bytes, page_no: int) -> dict:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if not 1 <= page_no <= doc.page_count:
            raise ToolError(f"page {page_no} out of range 1-{doc.page_count}")
        p = doc[page_no - 1]
        out = page_regions(p)
        W, H = out["width"], out["height"]
        regions = []
        for g in out["regions"]:
            v = {"id": g["id"], "kind": g["kind"], "rect": _norm_rect(g["rect"], W, H)}
            if g.get("caption_label"):
                v |= {"caption_label": g["caption_label"], "caption": g["caption_text"],
                      "caption_rect": _norm_rect(g["caption_rect"], W, H)}
            regions.append(v)
        return {"page": page_no, "page_label": p.get_label() or str(page_no), "pages": doc.page_count,
                "regions": regions, "warnings": out["warnings"]}


def find_label(data: bytes, label: str, page_no: int | None) -> tuple[int, dict] | None:
    """(1-based page, region) of the figure or table with this caption label."""
    import pymupdf
    want = norm_label(label)
    if not want:
        raise ToolError(f"{label!r} is not a figure or table label (e.g. 'Figure 3', 'Table 2', 'Abb. 4')")
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = [page_no - 1] if page_no else range(doc.page_count)
        for i in pages:
            if not 0 <= i < doc.page_count:
                raise ToolError(f"page {page_no} out of range 1-{doc.page_count}")
            p = doc[i]
            if want.split()[1].lower() not in p.get_text().lower():
                continue
            for g in page_regions(p)["regions"]:
                if g.get("caption_label", "").lower() == want.lower():
                    return i + 1, g
    return None


def annotation_geometry(data: bytes, page_no: int, rect_pts) -> dict:
    """Zotero position (PDF user space, origin bottom-left), page label and sort index."""
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        p = doc[page_no - 1]
        r = pymupdf.Rect(rect_pts) & p.rect
        if r.is_empty or r.width < 4 or r.height < 4:
            raise ToolError("the rectangle is empty or outside the page")
        q = r * ~p.transformation_matrix
        rect = [round(min(q.x0, q.x1), 3), round(min(q.y0, q.y1), 3), round(max(q.x0, q.x1), 3), round(max(q.y0, q.y1), 3)]
        words = p.get_text("words", sort=True)
        offset = sum(len(w[4]) + 1 for w in words if w[3] <= r.y0)
        return {"pageIndex": page_no - 1, "pageLabel": p.get_label() or str(page_no), "rects": [rect],
                "sortIndex": f"{page_no - 1:05d}|{offset:06d}|{int(r.y0):05d}"}


def page_size(data: bytes, page_no: int) -> tuple[float, float]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if not 1 <= page_no <= doc.page_count:
            raise ToolError(f"page {page_no} out of range 1-{doc.page_count}")
        r = doc[page_no - 1].rect
        return r.width, r.height


async def _pdf(key: str) -> tuple[dict, bytes]:
    att = await pdf_attachment(key)
    data = await asyncio.to_thread(pdf_bytes, att["key"])
    if data is None:
        raise ToolError("PDF not found on WebDAV: sync files from Zotero desktop first")
    return att, data


@tool
async def get_page_layout(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    page: Annotated[int, Field(ge=1, description="1-based page of the PDF (not the printed page label)")],
) -> dict:
    """Figures and tables on a PDF page, with their captions ('Figure 3', 'Table 2', 'Abb. 4') and
    boxes as fractions of the page, [x0, y0, x1, y1] from the top-left corner. Use a region with
    annotate_region (by caption label or id), or read its caption before citing a figure.

    Found from the page geometry: raster images, clusters of vector drawings, ruled tables.
    Tables without rules and text set inside figures can be missed or fall outside the box;
    scanned pages give no regions (see warnings)."""
    att, data = await _pdf(key)
    out = await asyncio.to_thread(layout, data, page)
    return {"attachment_key": att["key"], **out}


@tool
async def annotate_region(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    label: Annotated[str | None, Field(description="Caption label of the figure or table, e.g. 'Figure 3', 'Table 2'; "
                                       "searched on `page`, else on every page")] = None,
    page: Annotated[int | None, Field(ge=1, description="1-based PDF page; required with region_id and rect")] = None,
    region_id: Annotated[int | None, Field(ge=1, description="A region id from get_page_layout for that page")] = None,
    rect: Annotated[list[float] | None, Field(min_length=4, max_length=4, description=
                    "Any box as fractions of the page, [x0, y0, x1, y1] from the top-left corner")] = None,
    include_caption: Annotated[bool, Field(description="Extend the box over the caption (label and region_id)")] = True,
    comment: str | None = None,
    color: Literal["yellow", "red", "green", "blue", "purple", "magenta", "orange", "gray"] = "yellow",
    tags: list[str] | None = None,
    dry_run: Annotated[bool, Field(description="Only find the box, do not create the annotation")] = False,
) -> dict:
    """Draw an area annotation (a rectangle, Zotero's 'image' annotation) around a figure or
    table, found by its caption label or by a region id from get_page_layout, or around any
    rectangle. It appears in Zotero's reader and sidebar; the PDF file is not modified.

    Give exactly one of label, region_id, rect. The image of the area is rendered by Zotero
    desktop, not by this server: until a desktop client has opened the PDF the annotation may
    show without its picture on other devices."""
    if sum(x is not None for x in (label, region_id, rect)) != 1:
        raise ToolError("give exactly one of label, region_id, rect")
    if (region_id is not None or rect is not None) and page is None:
        raise ToolError("page is required with region_id and rect")
    att, data = await _pdf(key)
    found: dict = {}
    if rect is not None:
        if not (0 <= rect[0] < rect[2] <= 1 and 0 <= rect[1] < rect[3] <= 1):
            raise ToolError("rect must be [x0, y0, x1, y1] fractions of the page with x0 < x1 and y0 < y1")
        W, H = await asyncio.to_thread(page_size, data, page)
        box = (rect[0] * W, rect[1] * H, rect[2] * W, rect[3] * H)
        page_no = page
    else:
        if label is not None:
            hit = await asyncio.to_thread(find_label, data, label, page)
            if hit is None:
                raise ToolError(f"no graphic captioned {norm_label(label)!r} found"
                                + (f" on page {page}" if page else "") + "; check get_page_layout or pass rect")
            page_no, g = hit
        else:
            import pymupdf

            def regions():
                with pymupdf.open(stream=data, filetype="pdf") as doc:
                    if not 1 <= page <= doc.page_count:
                        raise ToolError(f"page {page} out of range 1-{doc.page_count}")
                    return page_regions(doc[page - 1])["regions"]
            g = next((x for x in await asyncio.to_thread(regions) if x["id"] == region_id), None)
            if g is None:
                raise ToolError(f"no region {region_id} on page {page}; call get_page_layout")
            page_no = page
        box = g["rect"]
        if include_caption and g.get("caption_rect"):
            box = _union(box, g["caption_rect"])
        box = (box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2)
        found = {k: v for k, v in {"kind": g["kind"], "caption_label": g.get("caption_label"),
                                   "caption": g.get("caption_text")}.items() if v}
    geo = await asyncio.to_thread(annotation_geometry, data, page_no, box)
    if dry_run:
        return {"status": "found", "attachment_key": att["key"], "page": geo["pageLabel"], **found,
                "rect_pdf": geo["rects"][0]}
    ann = {"itemType": "annotation", "parentItem": att["key"], "annotationType": "image",
           "annotationComment": comment or "", "annotationColor": COLORS[color],
           "annotationPageLabel": geo["pageLabel"], "annotationSortIndex": geo["sortIndex"],
           "annotationPosition": json.dumps({"pageIndex": geo["pageIndex"], "rects": geo["rects"]},
                                            separators=(",", ":")),
           "tags": [{"tag": t} for t in tags or []]}
    out = await post_items([ann])
    out |= {"attachment_key": att["key"], "page": geo["pageLabel"], **found}
    if ocr_cached(att["key"], "pdf"):
        out["note"] = "scanned PDF: the box was placed on the page geometry, which the OCR copy keeps"
    return out
