"""Creating items from identifiers and records other than DOIs: books by ISBN (Open Library,
Google Books, DNB) and CSL JSON or BibTeX records. Duplicates are checked against the library
(DOI, ISBN of books, else title + first author + year with the DOI MCP's matching rules) and
handled with the same if_exists policy as create_item_from_doi."""
import json
import re
import xml.etree.ElementTree as ET
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from . import api, bibtex, matching
from .core import (ToolError, key_of, library_dois, library_isbns, public, req, strip_html, summary,
                   template, tool, year_of)
from .items import IfExists, _today, on_existing


def person(name: str, role: str = "author") -> dict:
    """A creator from 'Surname, First' or 'First [von] Surname'; one word is kept as a name."""
    name = " ".join((name or "").split()).strip(" ,;")
    if "," in name:
        last, first = (p.strip() for p in name.split(",", 1))
        return {"creatorType": role, "lastName": last, "firstName": first}
    parts = name.split(" ")
    if len(parts) == 1:
        return {"creatorType": role, "name": name}
    k = len(parts) - 1
    while k > 1 and parts[k - 1].lower() in matching.NAME_PARTICLES:
        k -= 1
    return {"creatorType": role, "lastName": " ".join(parts[k:]), "firstName": " ".join(parts[:k])}


def _year(s: Any) -> int | None:
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", str(s or ""))
    return int(m.group(1)) if m else None


# ---- ISBN catalogues -------------------------------------------------------------------------------
async def _openlibrary(isbn: str) -> dict | None:
    r = await public().get("https://openlibrary.org/api/books", timeout=20,
                           params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"})
    if r.status_code != 200:
        raise ToolError(f"HTTP {r.status_code}")
    b = r.json().get(f"ISBN:{isbn}")
    if not b or not b.get("title"):
        return None
    pages = b.get("number_of_pages")
    return {"title": b["title"], "subtitle": b.get("subtitle"),
            "creators": [person(a["name"]) for a in b.get("authors", []) if a.get("name")],
            "publisher": (b.get("publishers") or [{}])[0].get("name"),
            "place": (b.get("publish_places") or [{}])[0].get("name"),
            "date": b.get("publish_date"), "numPages": str(pages) if pages else None}


async def _googlebooks(isbn: str) -> dict | None:
    r = await public().get("https://www.googleapis.com/books/v1/volumes", params={"q": f"isbn:{isbn}"}, timeout=20)
    if r.status_code != 200:
        raise ToolError(f"HTTP {r.status_code}")
    for it in r.json().get("items") or []:
        v = it.get("volumeInfo") or {}
        # the search is fuzzy: keep only a volume that carries the ISBN asked for
        if isbn not in {api.norm_isbn(x.get("identifier")) for x in v.get("industryIdentifiers") or []}:
            continue
        if not v.get("title"):
            continue
        return {"title": v["title"], "subtitle": v.get("subtitle"),
                "creators": [person(a) for a in v.get("authors") or []],
                "publisher": v.get("publisher"), "date": v.get("publishedDate"),
                "numPages": str(v["pageCount"]) if v.get("pageCount") else None,
                "language": v.get("language"), "abstractNote": v.get("description")}
    return None


_MARC_ROLES = {"aut": "author", "edt": "editor", "trl": "translator", "ctb": "contributor"}


async def _dnb(isbn: str) -> dict | None:
    r = await public().get("https://services.dnb.de/sru/dnb", timeout=20, params={
        "version": "1.1", "operation": "searchRetrieve", "query": f"num={isbn}",
        "recordSchema": "MARC21-xml", "maximumRecords": "1"})
    if r.status_code != 200:
        raise ToolError(f"HTTP {r.status_code}")
    try:
        root = ET.fromstring(r.content)
    except ET.ParseError:
        raise ToolError("unreadable answer") from None
    fields: dict[str, list[dict]] = {}
    for df in root.iter():
        if not df.tag.endswith("datafield"):
            continue
        subs: dict[str, list[str]] = {}
        for sf in df:
            if sf.tag.endswith("subfield"):
                subs.setdefault(sf.get("code"), []).append((sf.text or "").strip())
        fields.setdefault(df.get("tag"), []).append(subs | {"_ind2": [df.get("ind2") or " "]})

    def first(tag, code):
        for f in fields.get(tag, []):
            if f.get(code):
                return _marc_clean(f[code][0])
        return None
    title = first("245", "a")
    if not title:
        return None
    creators = []
    for tag in ("100", "700"):
        for f in fields.get(tag, []):
            if not f.get("a"):
                continue
            role = _MARC_ROLES.get((f.get("4") or ["aut" if tag == "100" else ""])[0])
            if role:
                creators.append(person(_marc_clean(f["a"][0]), role))
    pub = next((f for f in fields.get("264", []) if f["_ind2"][0] == "1"), None) or (fields.get("260") or [None])[0] or {}
    pages = re.search(r"(\d+)\s*(?:S\.|p\.|pages|Seiten)", first("300", "a") or "")
    return {"title": title, "subtitle": first("245", "b"), "creators": creators,
            "publisher": _marc_clean((pub.get("b") or [None])[0]), "place": _marc_clean((pub.get("a") or [None])[0]),
            "date": _marc_clean((pub.get("c") or [None])[0]), "edition": first("250", "a"),
            "numPages": pages.group(1) if pages else None, "language": first("041", "a")}


def _marc_clean(s: str | None) -> str | None:
    """MARC subfields end with ISBD punctuation (' /', ' :', ',') and carry [brackets] for
    supplied data; non-filing markers (\u0098 ... \u009c) wrap leading articles."""
    if not s:
        return None
    s = s.replace("\u0098", "").replace("\u009c", "")
    s = re.sub(r"[\[\]]", "", s).strip()
    return re.sub(r"\s*[/:;,=]\s*$", "", s).strip() or None


CATALOGUES = {"Open Library": _openlibrary, "Google Books": _googlebooks, "DNB": _dnb}


@tool
async def create_item_from_isbn(
    isbn: Annotated[str, Field(description="ISBN-10 or ISBN-13, with or without hyphens")],
    collection_keys: list[str] | None = None,
    tags: list[str] | None = None,
    if_exists: IfExists = "skip",
    expected_title: Annotated[str | None, Field(description="Title of the book you mean; the record must match it")] = None,
    expected_first_author: Annotated[str | None, Field(description="Surname of the first author or editor you expect")] = None,
    expected_year: Annotated[int | None, Field(description="Year of the edition you expect (±1 allowed)")] = None,
    force: Annotated[bool, Field(description="Create despite a mismatch with what you expected")] = False,
    dry_run: Annotated[bool, Field(description="Look the ISBN up and check it, create nothing")] = False,
) -> dict:
    """Create a book item from its ISBN, with metadata from Open Library, then Google Books, then
    the DNB (the DNB first for ISBNs of the German-language group, 978-3). The first catalogue
    with a record is used and named in `source` and in the item's Library Catalog field.

    Pass expected_title / expected_first_author / expected_year when you know the book: a
    record that does not match is not created (status 'not_created'), and a matching one gets
    a "Verified:" line in Extra, as with create_item_from_doi. An ISBN identifies one edition:
    check the year and publisher against the edition you cite. A book whose ISBN is already in
    the library is a duplicate; chapters (bookSection) are not counted."""
    norm = api.norm_isbn(isbn)
    if not norm:
        raise ToolError(f"not a valid ISBN: {isbn!r} (check digit wrong or wrong length)")
    if if_exists != "duplicate" and not dry_run:
        existing = (await library_isbns()).get(norm)
        if existing:
            return await on_existing(existing, if_exists, collection_keys, tags)
    order = list(CATALOGUES)
    if norm.startswith("9783"):
        order = ["DNB"] + [c for c in order if c != "DNB"]
    rec, source, tried = None, None, {}
    for name in order:
        try:
            rec = await CATALOGUES[name](norm)
        except (ToolError, httpx.HTTPError, ValueError, KeyError) as e:
            tried[name] = str(e) or type(e).__name__
            continue
        if rec:
            source = name
            break
        tried[name] = "not found"
    if not rec:
        raise ToolError(f"ISBN {norm} not found: " + "; ".join(f"{k}: {v}" for k, v in tried.items()))
    title = " ".join(rec["title"].split())
    full_title = f"{title}: {rec['subtitle']}" if rec.get("subtitle") else title
    people = [c.get("lastName") or c.get("name") or "" for c in rec["creators"]]
    cand = {"title": title, "subtitle": rec.get("subtitle"), "authors": people, "year": _year(rec.get("date"))}
    check = matching.assess(cand, expected_title or title, expected_first_author, expected_year)
    problems, warnings = [], []
    if expected_title and check["score"] < matching.TITLE_THRESHOLD:
        problems.append(f"title mismatch: expected {expected_title!r}, the ISBN is {full_title!r} (similarity {check['score']})")
    if expected_first_author and check["author_ok"] is False:
        problems.append(f"author mismatch: expected {expected_first_author!r}, the record has {people[:5]}")
    if expected_year and check["year_ok"] is False:
        problems.append(f"year mismatch: expected {expected_year}, the record says {rec.get('date')!r} "
                        "(another edition?)")
    if expected_title and not problems and not matching.eligible(check):
        warnings.append("short title: pass expected_first_author so the match can be confirmed")
    if not people:
        warnings.append(f"no authors in the {source} record")
    record = {"source": source, "title": full_title, "creators": [c.get("lastName") or c.get("name") for c in rec["creators"]],
              "publisher": rec.get("publisher"), "place": rec.get("place"), "date": rec.get("date"),
              "edition": rec.get("edition")}
    record = {k: v for k, v in record.items() if v}
    if problems and not force:
        return {"status": "not_created", "problems": problems, "warnings": warnings, "record": record}
    if dry_run:
        existing = (await library_isbns()).get(norm)
        return {"status": "dry_run", "record": record, "problems": problems, "warnings": warnings,
                **({"existing": existing} if existing else {})}
    tpl = await template("book")
    values = {"title": full_title, "publisher": rec.get("publisher"), "place": rec.get("place"),
              "date": rec.get("date"), "numPages": rec.get("numPages"), "edition": rec.get("edition"),
              "language": rec.get("language"), "abstractNote": rec.get("abstractNote"),
              "ISBN": norm, "libraryCatalog": source}
    for k, v in values.items():
        if k in tpl and v:
            tpl[k] = v
    tpl["creators"] = rec["creators"]
    tpl["collections"] = [key_of(k) for k in collection_keys or []]
    tpl["tags"] = [{"tag": t} for t in tags or []]
    verified = bool(expected_title and not problems and "extra" in tpl)
    if verified:
        tpl["extra"] = api.set_verified(tpl.get("extra"), api.verified_line(
            "match", f"create_item_from_isbn {source.lower().replace(' ', '_')} title+author+year", _today()))
    out = await _post([tpl])
    res = {"status": "ok" if out[0].get("key") else "error", "created_keys": [out[0]["key"]] if out[0].get("key") else [],
           "source": source, "title": full_title, "verified": verified}
    if out[0].get("error"):
        res["detail"] = out[0]["error"]
    if problems or warnings:
        res["problems_overridden"], res["warnings"] = problems, warnings
    return res


async def _post(items: list[dict]) -> list[dict]:
    """Create items in batches of 50; per input item {"key"} or {"error"}."""
    out: list[dict] = [{} for _ in items]
    for i in range(0, len(items), 50):
        r = await req("POST", "/items", json=items[i:i + 50])
        if r.status_code != 200:
            for j in range(i, min(i + 50, len(items))):
                out[j] = {"error": f"HTTP {r.status_code} {r.text[:200]}"}
            continue
        d = r.json()
        for j, v in (d.get("successful") or {}).items():
            out[i + int(j)] = {"key": v["key"] if isinstance(v, dict) else v}
        for j, f in (d.get("failed") or {}).items():
            out[i + int(j)] = {"error": f.get("message")}
    return out


# ---- CSL JSON and BibTeX --------------------------------------------------------------------------
CSL_IMPORT = {"article-journal": "journalArticle", "journal-article": "journalArticle", "article": "preprint",
              "posted-content": "preprint", "paper-conference": "conferencePaper",
              "proceedings-article": "conferencePaper", "chapter": "bookSection", "book-chapter": "bookSection",
              "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book",
              "thesis": "thesis", "dissertation": "thesis", "report": "report", "webpage": "webpage",
              "post-weblog": "blogPost", "dataset": "dataset", "manuscript": "manuscript",
              "article-magazine": "magazineArticle", "article-newspaper": "newspaperArticle",
              "entry-encyclopedia": "encyclopediaArticle", "entry-dictionary": "dictionaryEntry"}
# the field that holds the container's title, per Zotero item type
CONTAINER = {"journalArticle": "publicationTitle", "magazineArticle": "publicationTitle",
             "newspaperArticle": "publicationTitle", "bookSection": "bookTitle", "conferencePaper": "proceedingsTitle",
             "webpage": "websiteTitle", "blogPost": "blogTitle", "encyclopediaArticle": "encyclopediaTitle",
             "dictionaryEntry": "dictionaryTitle", "preprint": "repository"}
NUMBER = {"report": "reportNumber", "book": "seriesNumber", "journalArticle": "issue"}
GENRE = {"thesis": "thesisType", "report": "reportType", "preprint": "genre", "webpage": "websiteType"}
CSL_IGNORED = {"id", "type", "source", "note", "keyword", "citation-key", "subtitle", "container-title-short",
               "journalAbbreviation", "shortTitle", "title-short", "status", "license", "references"}


def _csl_text(v: Any) -> str | None:
    if isinstance(v, list):
        v = next((x for x in v if x not in (None, "")), None)
    if v in (None, ""):
        return None
    return str(v).strip() or None


def _csl_date(v: Any) -> str | None:
    if isinstance(v, str):
        return v
    if not isinstance(v, dict):
        return None
    parts = (v.get("date-parts") or [[]])[0]
    if parts and parts[0]:
        return "-".join(f"{int(p):02d}" if i else str(p) for i, p in enumerate(parts[:3]) if p not in (None, ""))
    return v.get("raw") or v.get("literal")


def _csl_names(names: Any, role: str) -> list[dict]:
    out = []
    for n in names if isinstance(names, list) else []:
        if not isinstance(n, dict):
            continue
        if n.get("literal") or (n.get("name") and not n.get("family")):
            out.append({"creatorType": role, "name": n.get("literal") or n["name"]})
            continue
        family = " ".join(x for x in (n.get("non-dropping-particle"), n.get("family")) if x)
        given = " ".join(x for x in (n.get("given"), n.get("dropping-particle")) if x)
        if n.get("suffix"):
            given = f"{given}, {n['suffix']}"
        if family:
            out.append({"creatorType": role, "lastName": family, "firstName": given})
    return out


def csl_to_fields(rec: dict) -> tuple[str | None, dict, list[dict], str | None, list[str]]:
    """(Zotero itemType or None, field values, creators, citation key, unmapped CSL keys)."""
    item_type = CSL_IMPORT.get(rec.get("type"))
    t = _csl_text
    title = t(rec.get("title"))
    sub = t(rec.get("subtitle"))
    if title and sub and sub.lower() not in title.lower():
        title = f"{title}: {sub}"
    v = {"title": title, CONTAINER.get(item_type, "publicationTitle"): t(rec.get("container-title")),
         "volume": t(rec.get("volume")), "issue": t(rec.get("issue")), "pages": t(rec.get("page")),
         "publisher": t(rec.get("publisher")), "place": t(rec.get("publisher-place")) or t(rec.get("event-place")),
         "DOI": t(rec.get("DOI")), "ISBN": t(rec.get("ISBN")), "ISSN": t(rec.get("ISSN")), "url": t(rec.get("URL")),
         "abstractNote": strip_html(t(rec.get("abstract"))), "language": t(rec.get("language")),
         "edition": t(rec.get("edition")), "series": t(rec.get("collection-title")),
         "seriesNumber": t(rec.get("collection-number")), "numPages": t(rec.get("number-of-pages")),
         "conferenceName": t(rec.get("event-title")) or t(rec.get("event")),
         "date": _csl_date(rec.get("issued")), "accessDate": _csl_date(rec.get("accessed")),
         "journalAbbreviation": t(rec.get("container-title-short")) or t(rec.get("journalAbbreviation")),
         "shortTitle": t(rec.get("title-short")) or t(rec.get("shortTitle")),
         "university": t(rec.get("publisher")) if item_type == "thesis" else None,
         "institution": t(rec.get("publisher")) if item_type == "report" else None,
         GENRE.get(item_type, "_genre"): t(rec.get("genre")),
         NUMBER.get(item_type, "_number"): t(rec.get("number"))}
    if item_type == "journalArticle" and not v.get("issue"):
        v["issue"] = t(rec.get("number"))
    creators = [c for role in ("author", "editor", "translator") for c in _csl_names(rec.get(role), role)]
    used = {"title", "subtitle", "container-title", "volume", "issue", "page", "publisher", "publisher-place",
            "event-place", "DOI", "ISBN", "ISSN", "URL", "abstract", "language", "edition", "collection-title",
            "collection-number", "number-of-pages", "event-title", "event", "issued", "accessed", "genre",
            "number", "author", "editor", "translator", "container-title-short", "journalAbbreviation",
            "title-short", "shortTitle"}
    unmapped = sorted(k for k in rec if k not in used and k not in CSL_IGNORED and not k.startswith("_"))
    return item_type, v, creators, t(rec.get("citation-key")), unmapped


BIB_TYPES = {"article": "journalArticle", "book": "book", "booklet": "book", "mvbook": "book",
             "proceedings": "book", "collection": "book", "mvcollection": "book", "inbook": "bookSection",
             "incollection": "bookSection", "inproceedings": "conferencePaper", "conference": "conferencePaper",
             "phdthesis": "thesis", "mastersthesis": "thesis", "thesis": "thesis", "techreport": "report",
             "report": "report", "unpublished": "manuscript", "online": "webpage", "electronic": "webpage",
             "www": "webpage", "dataset": "dataset", "periodical": None, "patent": None, "manual": "book"}
BIB_IGNORED = {"file", "owner", "timestamp", "keywords", "mendeley-groups", "mendeley-tags", "annote", "comment",
               "note", "abstract", "subtitle", "date", "year", "month", "day", "eprinttype", "archiveprefix",
               "primaryclass", "langid", "groups", "creationdate", "modificationdate", "readstatus", "ranking"}


def bib_type(entry: dict) -> tuple[str | None, str | None]:
    """(Zotero itemType, reason when refused)."""
    kind, f = entry["type"], entry["fields"]
    if kind == "misc":
        if (f.get("archiveprefix") or f.get("eprinttype") or "").lower() == "arxiv" or "arxiv" in f.get("howpublished", "").lower():
            return "preprint", None
        if f.get("url") and not any(f.get(k) for k in ("journal", "journaltitle", "publisher", "booktitle", "institution")):
            return "webpage", None
        return None, "@misc without arXiv or URL: change it to the real type (@report, @book, @online, ...)"
    if kind == "inbook" and not f.get("booktitle"):
        return "book", None  # BibTeX inbook = pages or a chapter number of a whole book
    if kind == "patent":
        return None, "patents: use create_item_from_patent"
    if kind not in BIB_TYPES or BIB_TYPES[kind] is None:
        return None, f"unsupported entry type @{kind}"
    return BIB_TYPES[kind], None


def bib_to_fields(entry: dict, item_type: str) -> tuple[dict, list[dict], list[str]]:
    """(field values, creators, unmapped BibTeX fields) for an entry already typed by bib_type."""
    f = {k: v for k, v in entry["fields"].items()}
    d = bibtex.delatex
    get = lambda *ks: next((d(f[k]) for k in ks if f.get(k, "").strip()), None)  # noqa: E731
    title = get("title")
    sub = get("subtitle")
    if title and sub:
        title = f"{title}: {sub}"
    date = get("date")
    if not date and f.get("year"):
        y = d(f["year"])
        mon = d(f.get("month", "")).lower()[:3]
        mon = bibtex.MONTHS.get(mon, mon if mon.isdigit() else "")
        day = d(f.get("day", ""))
        date = "-".join(p.zfill(2) if i else p for i, p in enumerate((y, mon, day)) if p)
    pages = get("pages")
    v = {"title": title, "date": date, "pages": pages.replace("–", "-").replace("—", "-") if pages else None,
         "volume": get("volume"), "publisher": get("publisher"), "place": get("address", "location"),
         "edition": get("edition"), "series": get("series"), "DOI": get("doi"), "ISBN": get("isbn"),
         "ISSN": get("issn"), "url": get("url"), "abstractNote": get("abstract"), "language": get("language"),
         "accessDate": get("urldate"), "numPages": get("pagetotal"), "shortTitle": get("shorttitle")}
    if item_type == "journalArticle":
        v |= {"publicationTitle": get("journaltitle", "journal"), "issue": get("number", "issue"),
              "journalAbbreviation": get("shortjournal")}
    elif item_type == "bookSection":
        v |= {"bookTitle": get("booktitle")}
    elif item_type == "conferencePaper":
        v |= {"proceedingsTitle": get("booktitle"), "conferenceName": get("eventtitle"),
              "publisher": v["publisher"] or get("organization")}
    elif item_type == "thesis":
        default = {"phdthesis": "PhD thesis", "mastersthesis": "Master's thesis"}.get(entry["type"])
        v |= {"university": get("school", "institution"), "thesisType": get("type") or default}
    elif item_type == "report":
        v |= {"institution": get("institution", "organization"), "reportNumber": get("number"),
              "reportType": get("type")}
    elif item_type == "book":
        v |= {"seriesNumber": get("number"), "publisher": v["publisher"] or get("organization")}
    elif item_type == "preprint":
        eprint = get("eprint")
        v |= {"repository": "arXiv" if eprint else get("howpublished"),
              "archiveID": f"arXiv:{eprint}" if eprint else None}
    elif item_type == "webpage":
        v |= {"websiteTitle": get("organization", "howpublished")}
    creators = []
    for field, role in (("author", "author"), ("editor", "editor"), ("translator", "translator")):
        if f.get(field):
            creators += bibtex.names(f[field], role)
    used = {"title", "pages", "volume", "publisher", "address", "location", "edition", "series", "doi", "isbn",
            "issn", "url", "language", "urldate", "pagetotal", "shorttitle", "journaltitle", "journal", "number",
            "issue", "shortjournal", "booktitle", "eventtitle", "organization", "school", "institution", "type",
            "eprint", "howpublished", "author", "editor", "translator"}
    unmapped = sorted(k for k in f if k not in used and k not in BIB_IGNORED)
    return v, creators, unmapped


async def _find_by_title(title: str, creators: list[dict], year: str) -> dict | None:
    """An item in the library that is the same work by title, first author and year."""
    words = sorted({w for w in re.findall(r"\w+", title.casefold()) if len(w) > 3 and w not in matching.REF_STOP},
                   key=len, reverse=True)[:3]
    if not words:
        return None
    surname = next((c.get("lastName") or c.get("name") for c in creators), None)
    r = await req("GET", "/items/top", params={"q": " ".join(words), "qmode": "titleCreatorYear", "limit": 50})
    if r.status_code != 200:
        return None
    y = _year(year)
    for it in r.json():
        d = it["data"]
        cand = {"title": d.get("title") or "", "authors": [c.get("lastName") or c.get("name") or "" for c in d.get("creators", [])],
                "year": _year(d.get("date"))}
        if matching.eligible(matching.assess(cand, title, surname, y)):
            return summary(it)
    return None


@tool
async def import_items(
    text: Annotated[str, Field(description="CSL JSON (one record or a list, e.g. from Crossref or Zotero) "
                               "or BibTeX/BibLaTeX entries")],
    format: Literal["auto", "csljson", "bibtex"] = "auto",
    collection_keys: list[str] | None = None,
    tags: list[str] | None = None,
    if_exists: IfExists = "skip",
    dry_run: Annotated[bool, Field(description="Map and check the records, create nothing")] = False,
) -> dict:
    """Create items from CSL JSON or BibTeX records (at most 100 per call).

    Each record is mapped to a Zotero item type; a record whose type has no faithful mapping
    (e.g. @misc without arXiv or URL, unknown CSL types) is refused and listed in `rejected`
    rather than imported as a generic document that would lose its journal or book fields.
    Duplicates are looked up by DOI, by ISBN for books, else by title, first author and year
    (the DOI MCP's matching rules), and handled per if_exists. BibTeX citation keys are kept as
    a "Citation Key:" line in Extra. Fields with no Zotero equivalent are listed per record in
    `unmapped`. Nothing is verified against a registry: check DOIs with create_item_from_doi or
    the DOI MCP, then set_verification."""
    fmt = format
    s = text.strip()
    if fmt == "auto":
        fmt = "csljson" if s[:1] in "[{" else "bibtex" if "@" in s[:200] else ""
        if not fmt:
            raise ToolError("cannot tell the format: CSL JSON starts with [ or {, BibTeX entries with @type{")
    records: list[dict] = []  # {"label", "itemType", "values", "creators", "citekey", "unmapped"} or {"label", "reason"}
    errors: list[str] = []
    if fmt == "csljson":
        try:
            data = json.loads(s)
        except json.JSONDecodeError as e:
            raise ToolError(f"not valid JSON: {e}") from None
        data = data.get("items", [data]) if isinstance(data, dict) and "items" in data else data
        data = [data] if isinstance(data, dict) else data
        if not isinstance(data, list):
            raise ToolError("CSL JSON must be an object or a list of objects")
        for i, rec in enumerate(data):
            if not isinstance(rec, dict):
                errors.append(f"record {i}: not an object")
                continue
            item_type, values, creators, citekey, unmapped = csl_to_fields(rec)
            label = rec.get("citation-key") or rec.get("id") or _csl_text(rec.get("title")) or f"record {i}"
            if not item_type:
                records.append({"label": str(label), "reason": f"unsupported CSL type {rec.get('type')!r}"})
                continue
            records.append({"label": str(label), "itemType": item_type, "values": values, "creators": creators,
                            "citekey": citekey, "unmapped": unmapped})
    else:
        entries, errors = bibtex.parse(s)
        for e in entries:
            item_type, reason = bib_type(e)
            if not item_type:
                records.append({"label": e["key"], "reason": reason})
                continue
            values, creators, unmapped = bib_to_fields(e, item_type)
            records.append({"label": e["key"], "itemType": item_type, "values": values, "creators": creators,
                            "citekey": e["key"], "unmapped": unmapped})
    if not records and not errors:
        raise ToolError("no records found")
    if len(records) > 100:
        raise ToolError(f"{len(records)} records: import at most 100 per call")
    colls = [key_of(k) for k in collection_keys or []]

    dois, isbns = await library_dois(), await library_isbns()
    rejected = [{"record": r["label"], "reason": r["reason"]} for r in records if "reason" in r] + \
               [{"record": None, "reason": e} for e in errors]
    existing_out, to_create, plan = [], [], []
    seen: dict[str, str] = {}
    for r in records:
        if "reason" in r:
            continue
        try:
            tpl = await template(r["itemType"])
        except ToolError:
            rejected.append({"record": r["label"], "reason": f"item type {r['itemType']} not available in Zotero"})
            continue
        values = r["values"]
        if not values.get("title"):
            rejected.append({"record": r["label"], "reason": "no title"})
            continue
        dropped = sorted(k for k, v in values.items() if v and k not in tpl and not k.startswith("_"))
        for k, v in values.items():
            if k in tpl and v:
                tpl[k] = v
        if tpl.get("DOI"):
            tpl["DOI"] = api.norm_doi(tpl["DOI"])
        if r["citekey"] and "extra" in tpl:
            tpl["extra"] = f"Citation Key: {r['citekey']}"
        if not tpl.get("DOI") and "DOI" not in tpl and values.get("DOI"):
            tpl["extra"] = "\n".join(x for x in (tpl.get("extra"), f"DOI: {api.norm_doi(values['DOI'])}") if x)
        tpl["creators"] = r["creators"]
        tpl["collections"], tpl["tags"] = list(colls), [{"tag": t} for t in tags or []]
        # same work twice in this import, then already in the library
        ids = [f"doi:{d}" for d in api.item_dois(tpl)]
        if r["itemType"] == "book":
            ids += [f"isbn:{i}" for i in api.item_isbns(tpl)]
        ids.append("title:" + "|".join([matching.norm(tpl["title"]), year_of(tpl),
                                         matching.norm(next((c.get("lastName") or c.get("name", "") for c in r["creators"]), ""))]))
        twin = next((seen[i] for i in ids if i in seen), None)
        if twin:
            rejected.append({"record": r["label"], "reason": f"same work as {twin} in this import"})
            continue
        for i in ids:
            seen[i] = r["label"]
        hit = next((dois[d] for d in api.item_dois(tpl) if d in dois), None)
        if not hit and r["itemType"] == "book":
            hit = next((isbns[i] for i in api.item_isbns(tpl) if i in isbns), None)
        if not hit:
            hit = await _find_by_title(tpl["title"], r["creators"], tpl.get("date") or "")
        entry = {"record": r["label"], "itemType": r["itemType"], "title": tpl["title"]}
        unmapped = r["unmapped"] + dropped
        if unmapped:
            entry["unmapped"] = unmapped
        if hit and if_exists != "duplicate":
            existing_out.append((entry, hit))
            continue
        to_create.append(tpl)
        plan.append(entry)

    if dry_run:
        return {"status": "dry_run", "format": fmt, "would_create": plan,
                "existing": [e | {"existing": h} for e, h in existing_out], "rejected": rejected}
    existing_res = []
    for e, h in existing_out:
        res = await on_existing(h, if_exists, collection_keys, tags)
        existing_res.append(e | {k: v for k, v in res.items() if k != "status"} |
                            ({"error": res.get("detail")} if res.get("status") == "error" else {}))
    created, failed = [], []
    for e, res in zip(plan, await _post(to_create) if to_create else []):
        if res.get("key"):
            created.append(e | {"key": res["key"]})
        else:
            failed.append(e | {"error": res.get("error")})
    status = "ok" if not failed and not rejected else "partial" if created or existing_res else "error"
    return {"status": status, "format": fmt, "created": created, "existing": existing_res,
            "rejected": rejected, **({"failed": failed} if failed else {})}
