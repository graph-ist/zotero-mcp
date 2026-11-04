"""Reading a PDF by structure: the outline of sections (bookmarks, or headings found from the
typography) and the reference list, so the model reads parts rather than a stream of characters."""
import asyncio
import re
from typing import Annotated

from pydantic import Field

from . import api
from .core import ToolError, library_dois, public, tool
from .pdf import pdf_or_ocr, pdf_attachment

KINDS = [("abstract", r"^(abstract|sommario|zusammenfassung|riassunto)\b"),
         ("references", r"^(references?|bibliograph(y|ie|ia)|works cited|literatur(verzeichnis)?|riferimenti( bibliografici)?|literature cited)\b"),
         ("notes", r"^(notes?|endnotes|anmerkungen|note)\b"),
         ("appendix", r"^(appendi(x|ces|ce)|anhang|supplementary)\b"),
         ("acknowledgements", r"^(acknowledg(e)?ments?|ringraziamenti|danksagung)\b"),
         ("conclusion", r"^(\d+(\.\d+)*\.?\s+)?(conclusions?|discussion( and conclusions?)?|conclusioni|fazit|schluss)\b"),
         ("introduction", r"^(\d+(\.\d+)*\.?\s+)?(introduction|introduzione|einleitung)\b"),
         ("method", r"^(\d+(\.\d+)*\.?\s+)?(methods?|methodology|materials and methods|metodo|methoden?)\b")]


def kind_of(title: str) -> str | None:
    t = title.strip().lower()
    for kind, pat in KINDS:
        if re.match(pat, t):
            return kind
    return None


def _heuristic_outline(doc) -> list[dict]:
    """Headings from the typography: lines set larger than the body text, or bold at body size,
    short, not sentence-like. Levels follow the distinct font sizes."""
    sizes: dict[float, int] = {}
    lines = []  # (page index, size, bold, text, y position relative to the page)
    for pno, page in enumerate(doc):
        h = page.rect.height or 1
        for block in page.get_text("dict", flags=0)["blocks"]:
            for line in block.get("lines", []):
                spans = [s for s in line["spans"] if s["text"].strip()]
                if not spans:
                    continue
                text = " ".join(s["text"].strip() for s in spans)
                size = round(max(s["size"] for s in spans), 1)
                bold = all(s["flags"] & 16 or "bold" in s["font"].lower() for s in spans)
                for s in spans:
                    sizes[round(s["size"], 1)] = sizes.get(round(s["size"], 1), 0) + len(s["text"])
                lines.append((pno, size, bold, text, line["bbox"][1] / h))
    if not sizes:
        return []
    body = max(sizes, key=sizes.get)
    candidates = []
    for pno, size, bold, text, y in lines:
        words = text.split()
        if not (1 <= len(words) <= 14 and len(text) <= 120 and re.search(r"[A-Za-zÀ-ÿ]{2}", text)):
            continue
        if text.rstrip().endswith((".", ",", ";", ":")) and not re.match(r"^\d+(\.\d+)*\.?$", text.split()[-1]):
            continue
        if y < 0.04 or y > 0.96:  # running heads and footers
            continue
        if size >= body * 1.15 or (bold and size >= body * 0.98):
            candidates.append((pno, size, text))
    # Running heads repeat the same text on many pages
    counts: dict[str, set] = {}
    for pno, _, text in candidates:
        counts.setdefault(text.lower(), set()).add(pno)
    candidates = [c for c in candidates if len(counts[c[2].lower()]) <= 2]
    levels = {s: i + 1 for i, s in enumerate(sorted({c[1] for c in candidates}, reverse=True)[:3])}
    out = []
    for pno, size, text in candidates:
        if size in levels:
            out.append({"title": text, "level": levels[size], "page": pno + 1})
    return out[:300]


def outline(data: bytes) -> dict:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        toc = doc.get_toc()
        if toc:
            sections = [{"title": " ".join(t.split()), "level": max(1, lvl), "page": max(1, page)}
                        for lvl, t, page in toc if page > 0]
            source = "bookmarks"
        else:
            sections, source = _heuristic_outline(doc), "typography"
        pages = doc.page_count
    for i, s in enumerate(sections):
        nxt = next((n for n in sections[i + 1:] if n["level"] <= s["level"]), None)
        s["page_end"] = max(s["page"], (nxt["page"] - 1) if nxt and nxt["page"] > s["page"] else (nxt["page"] if nxt else pages))
        kind = kind_of(s["title"])
        if kind:
            s["kind"] = kind
    return {"pages": pages, "source": source, "sections": sections}


@tool
async def get_outline(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
) -> dict:
    """Sections of a PDF with their page ranges, from its bookmarks or, failing that, from the
    typography (lines set larger or bolder than the body text). Use it before reading a long
    paper: then get_fulltext(key, pages='12-15') reads one section. Each section may carry a
    `kind` (abstract, introduction, method, conclusion, references, notes, appendix)."""
    att = await pdf_attachment(key)
    data = await asyncio.to_thread(pdf_or_ocr, att["key"])
    if data is None:
        raise ToolError("PDF not found on WebDAV: sync files from Zotero desktop first")
    out = await asyncio.to_thread(outline, data)
    if not out["sections"]:
        out["note"] = "no headings recognised (scan without OCR, or uniform typography); read with get_fulltext"
    return {"attachment_key": att["key"], **out}


# ---- references ----------------------------------------------------------------------------------
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)")
_HEAD_RE = re.compile(r"^\s*(\d+(\.\d+)*\.?\s+)?(references?|bibliograph(y|ie|ia)|works cited|literature cited|"
                      r"literatur(verzeichnis)?|riferimenti( bibliografici)?)\s*$", re.I | re.M)
_STOP_RE = re.compile(r"^\s*(\d+(\.\d+)*\.?\s+)?(appendi(x|ces)|anhang|supplementary|acknowledg|"
                      r"about the authors?|author biograph|notes on contributors)", re.I | re.M)


def _clean_doi(d: str) -> str:
    return api.norm_doi(re.sub(r"[\.,;:)\]]+$", "", d))


def reference_section(text: str) -> tuple[str, bool]:
    """(text of the reference list, found by heading?). Without a heading, the last quarter."""
    heads = list(_HEAD_RE.finditer(text))
    if heads:
        start = heads[-1].end()
        stop = _STOP_RE.search(text, start)
        return text[start:stop.start() if stop else None], True
    return text[int(len(text) * 0.75):], False


def split_references(section: str) -> list[str]:
    """Entries of a reference list: numbered ([1], 1.), else blank-line separated, else lines that
    start a new 'Surname, I.' or 'Surname I' pattern followed by a year."""
    section = re.sub(r"[ \t]+", " ", section.strip())
    numbered = re.split(r"\n(?=\s*(?:\[\d{1,3}\]|\d{1,3}\.)\s)", "\n" + section)
    numbered = [e.strip() for e in numbered if e.strip()]
    if len(numbered) >= 3:
        return numbered
    blocks = [b.strip() for b in re.split(r"\n\s*\n", section) if b.strip()]
    if len(blocks) >= 3 and sum(len(b) for b in blocks) / len(blocks) < 400:
        return blocks
    start = re.compile(r"^(?:[A-ZÀ-Þ][\w'’-]+(?:,| [A-Z]\.|\s[A-ZÀ-Þ][\w'’-]+,).{0,80}?\(?\d{4}[a-z]?\)?|[A-ZÀ-Þ][^\n]{0,60}\(\d{4}\))")
    entries, cur = [], []
    for line in section.split("\n"):
        if start.match(line.strip()) and cur:
            entries.append("\n".join(cur).strip())
            cur = []
        cur.append(line)
    if cur:
        entries.append("\n".join(cur).strip())
    return [e for e in entries if len(e) > 20]


def parse_reference(text: str) -> dict:
    text = re.sub(r"(10\.\d{4,9}/\S*)\n(?=[a-z0-9(\[/._-])", r"\1", text)  # DOI broken over two lines
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # words split by line breaks
    text = " ".join(text.split())
    doi = DOI_RE.search(text)
    year = re.search(r"\((\d{4})[a-z]?\)|\b(1[89]\d{2}|20\d{2})\b", text)
    first = re.match(r"^\s*(?:\[\d+\]|\d+\.)?\s*([A-ZÀ-Þ][\w'’-]+(?: [A-ZÀ-Þ][\w'’-]+)?)", text)
    out = {"text": text[:300]}
    if doi:
        out["doi"] = _clean_doi(doi.group(1))
    if year:
        out["year"] = year.group(1) or year.group(2)
    if first:
        out["first_author"] = first.group(1)
    return out


async def crossref_lookup(entry: dict) -> str | None:
    """DOI of a reference without one, from Crossref's bibliographic query; only a confident match
    (same first author surname and year, high score) is accepted."""
    r = await public().get("https://api.crossref.org/works", params={
        "query.bibliographic": entry["text"][:250], "rows": 2, "select": "DOI,author,issued,score,title"})
    if r.status_code != 200:
        return None
    for w in r.json().get("message", {}).get("items", []):
        family = (w.get("author") or [{}])[0].get("family", "").lower()
        year = str(((w.get("issued") or {}).get("date-parts") or [[None]])[0][0] or "")
        if w.get("score", 0) >= 60 and family and family in entry.get("first_author", "").lower() \
                and (not entry.get("year") or year == entry["year"]):
            return api.norm_doi(w["DOI"])
    return None


@tool
async def extract_references(
    key: Annotated[str, Field(description="Item key or PDF attachment key")],
    resolve_missing: Annotated[int, Field(ge=0, le=50, description="Look up on Crossref the DOI of up to this "
                                          "many entries that cite none (about one second each)")] = 0,
    max_entries: Annotated[int, Field(ge=1, le=500)] = 200,
) -> dict:
    """The reference list of a PDF, entry by entry, with DOIs found in the text and whether each
    cited work is already in the library (find_dois). Use it to see what a paper builds on and
    what is missing; add missing works with create_item_from_doi."""
    att = await pdf_attachment(key)
    data = await asyncio.to_thread(pdf_or_ocr, att["key"])
    if data is None:
        raise ToolError("PDF not found on WebDAV: sync files from Zotero desktop first")

    def _text() -> str:
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return "\n".join(p.get_text() for p in doc)

    text = await asyncio.to_thread(_text)
    if len(text.strip()) < 200:
        raise ToolError("the PDF has no text layer; run ocr_pdf first")
    section, by_heading = reference_section(text)
    entries = [parse_reference(e) for e in split_references(section)]
    truncated = len(entries) > max_entries
    entries = entries[:max_entries]
    looked_up = 0
    for e in entries:
        if "doi" not in e and looked_up < resolve_missing:
            looked_up += 1
            doi = await crossref_lookup(e)
            if doi:
                e["doi"], e["doi_source"] = doi, "crossref"
    index = await library_dois()
    for i, e in enumerate(entries, 1):
        e["n"] = i
        hit = index.get(e.get("doi", ""))
        if hit:
            e["in_library"] = {k: hit.get(k) for k in ("key", "title", "date")}
    with_doi = [e for e in entries if "doi" in e]
    return {"attachment_key": att["key"], "section_found": by_heading, "entries": entries,
            "counts": {"entries": len(entries), "with_doi": len(with_doi),
                       "in_library": sum(1 for e in entries if "in_library" in e),
                       "missing_with_doi": sum(1 for e in with_doi if "in_library" not in e)},
            "truncated": truncated, "looked_up": looked_up,
            "missing_dois": [e["doi"] for e in with_doi if "in_library" not in e]}
