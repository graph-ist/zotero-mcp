"""In-memory stand-in for the Zotero Web API v3 (and the public APIs the server calls), served
through httpx.MockTransport. It implements the subset of routes and parameters server.py uses,
with the same response shapes, so the tools can be tested offline."""
import json
import random
import re
import string
from urllib.parse import parse_qs, urlparse

import httpx

TEMPLATES = {
    "patent": ["title", "abstractNote", "place", "country", "assignee", "issuingAuthority", "patentNumber",
               "filingDate", "pages", "applicationNumber", "priorityNumbers", "issueDate", "references",
               "legalStatus", "language", "shortTitle", "url", "accessDate", "rights", "extra"],
    "journalArticle": ["title", "abstractNote", "publicationTitle", "volume", "issue", "pages", "date",
                       "series", "seriesTitle", "seriesText", "journalAbbreviation", "language", "DOI",
                       "ISSN", "shortTitle", "url", "accessDate", "archive", "archiveLocation",
                       "libraryCatalog", "callNumber", "rights", "extra"],
    "book": ["title", "abstractNote", "series", "seriesNumber", "volume", "numberOfVolumes", "edition",
             "place", "publisher", "date", "numPages", "language", "ISBN", "shortTitle", "url",
             "accessDate", "archive", "archiveLocation", "libraryCatalog", "callNumber", "rights", "extra"],
    "bookSection": ["title", "abstractNote", "bookTitle", "series", "seriesNumber", "volume",
                    "numberOfVolumes", "edition", "place", "publisher", "date", "pages", "language",
                    "ISBN", "shortTitle", "url", "accessDate", "extra"],
    "conferencePaper": ["title", "abstractNote", "date", "proceedingsTitle", "conferenceName", "place",
                        "publisher", "volume", "pages", "series", "language", "DOI", "ISBN", "shortTitle",
                        "url", "accessDate", "extra"],
    "preprint": ["title", "abstractNote", "genre", "repository", "archiveID", "place", "date", "series",
                 "seriesNumber", "DOI", "citationKey", "url", "accessDate", "archive", "archiveLocation",
                 "shortTitle", "language", "libraryCatalog", "callNumber", "rights", "extra"],
    "thesis": ["title", "abstractNote", "thesisType", "university", "place", "date", "numPages",
               "language", "shortTitle", "url", "accessDate", "extra"],
    "report": ["title", "abstractNote", "reportNumber", "reportType", "seriesTitle", "place",
               "institution", "date", "pages", "language", "shortTitle", "url", "accessDate", "extra"],
    "webpage": ["title", "abstractNote", "websiteTitle", "websiteType", "date", "shortTitle", "url",
                "accessDate", "language", "rights", "extra"],
    "manuscript": ["title", "abstractNote", "manuscriptType", "place", "date", "numPages", "language",
                   "shortTitle", "url", "accessDate", "extra"],
    "dataset": ["title", "abstractNote", "identifier", "type", "versionNumber", "date", "repository",
                "repositoryLocation", "format", "DOI", "citationKey", "url", "accessDate", "language",
                "shortTitle", "libraryCatalog", "callNumber", "rights", "extra"],
}


def _rand_key() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


class FakeZotero:
    def __init__(self, user_id: str = "1"):
        self.user_id = user_id
        self.version = 10
        self.items: dict[str, dict] = {}
        self.collections: dict[str, dict] = {}
        self.fulltext: dict[str, dict] = {}
        self.searches: list[dict] = []
        self.deleted: list[tuple[int, str]] = []
        self.calls: list[tuple[str, str]] = []
        self.on_get: dict[str, callable] = {}  # item key -> hook run after it is read (to simulate races)
        # Public APIs
        self.crossref: dict[str, dict] = {}     # doi -> Crossref "message"
        self.crossref_query: list[dict] = []    # works returned by /works?query.bibliographic (with _match terms)
        self.openalex: dict[str, dict] = {}     # doi -> OpenAlex work
        self.urls: dict[str, tuple[int, bytes, str]] = {}  # url -> (status, body, content-type)
        self.openlibrary: dict[str, dict] = {}  # ISBN-13 -> record of /api/books?jscmd=data
        self.googlebooks: dict[str, list[dict]] = {}  # ISBN-13 -> volumeInfo list returned for q=isbn:
        self.dnb: dict[str, str] = {}  # ISBN as queried (13 or 10 digits) -> MARC21-xml record (the <record> element)
        self.dnb_diagnostic: str | None = None  # an SRU diagnostic message to answer with
        self.public_status: dict[str, int] = {}  # host -> HTTP status to answer with (quota, outage)
        self.fail_next: list[int] = []  # HTTP statuses to answer with before serving (tests retry)

    def dispatch(self, request: httpx.Request) -> httpx.Response:
        """One transport for both the Zotero API and the public APIs."""
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"Retry-After": "0"})
        if request.url.host == "api.zotero.org":
            return self.handler(request)
        return self.public_handler(request)

    # ---- library setup ----------------------------------------------------------------------
    def _bump(self) -> int:
        self.version += 1
        return self.version

    def add_item(self, item_type: str = "journalArticle", key: str | None = None, **data) -> str:
        key = key or _rand_key()
        d = {"key": key, "version": 0, "itemType": item_type, "creators": [], "tags": [],
             "collections": [], "relations": {}}
        if item_type == "attachment":
            d |= {"linkMode": "imported_file", "contentType": "", "filename": "", "parentItem": ""}
        elif item_type in ("note", "annotation"):
            d |= {"parentItem": ""}
        else:
            d |= {f: "" for f in TEMPLATES.get(item_type, TEMPLATES["journalArticle"])}
        d.update(data)
        d["tags"] = [{"tag": t} if isinstance(t, str) else t for t in d["tags"]]
        d["version"] = self._bump()
        self.items[key] = {"key": key, "version": d["version"], "library": {}, "meta": {}, "data": d}
        return key

    def add_collection(self, name: str, parent: str | None = None, key: str | None = None) -> str:
        key = key or _rand_key()
        v = self._bump()
        self.collections[key] = {"key": key, "version": v, "meta": {},
                                 "data": {"key": key, "version": v, "name": name,
                                          "parentCollection": parent or False, "relations": {}}}
        return key

    # ---- helpers -------------------------------------------------------------------------------
    def _out(self, it: dict) -> dict:
        d = it["data"]
        meta = dict(it["meta"])
        if d["itemType"] not in ("attachment", "note", "annotation"):
            meta["numChildren"] = sum(1 for o in self.items.values() if o["data"].get("parentItem") == d["key"])
        return {"key": it["key"], "version": it["version"], "library": {"type": "user", "id": int(self.user_id)},
                "meta": meta, "data": json.loads(json.dumps(d))}

    def _text(self, d: dict, everything: bool) -> str:
        parts = [d.get("title") or "", d.get("date") or ""]
        parts += [c.get("lastName") or c.get("name") or "" for c in d.get("creators", [])]
        if everything:
            parts += [str(v) for k, v in d.items() if isinstance(v, str)]
            parts.append((self.fulltext.get(d["key"]) or {}).get("content", ""))
            parts += [(self.fulltext.get(o["key"]) or {}).get("content", "") for o in self.items.values()
                      if o["data"].get("parentItem") == d["key"]]
        return " ".join(parts).lower()

    def _filter(self, objs: list[dict], q: dict, top: bool = False, trash: bool = False) -> list[dict]:
        out = []
        include_trashed = q.get("includeTrashed", ["0"])[0] == "1"
        for it in objs:
            d = it["data"]
            if top and d.get("parentItem"):
                continue
            if trash != bool(d.get("deleted")) and not (include_trashed and not trash):
                continue
            if "itemType" in q and d["itemType"] != q["itemType"][0]:
                continue
            if "tag" in q and q["tag"][0] not in {t["tag"] for t in d.get("tags", [])}:
                continue
            if "q" in q:  # like Zotero's quick search: every word must occur
                text = self._text(d, q.get("qmode", [""])[0] == "everything")
                if not all(w in text for w in q["q"][0].lower().split()):
                    continue
            if "since" in q and it["version"] <= int(q["since"][0]):
                continue
            if "itemKey" in q and it["key"] not in q["itemKey"][0].split(","):
                continue
            out.append(it)
        sort = q.get("sort", ["dateModified"])[0]
        keyf = {"title": lambda i: (i["data"].get("title") or "").lower(),
                "creator": lambda i: self._text(i["data"], False),
                "date": lambda i: i["data"].get("date") or "",
                "dateAdded": lambda i: i["version"]}.get(sort, lambda i: i["version"])
        out.sort(key=keyf, reverse=q.get("direction", ["desc"])[0] == "desc")
        return out

    def _page(self, objs: list, q: dict, fmt_item=None) -> httpx.Response:
        start, limit = int(q.get("start", ["0"])[0]), int(q.get("limit", ["25"])[0])
        page = objs[start:start + limit]
        headers = {"Total-Results": str(len(objs)), "Last-Modified-Version": str(self.version)}
        fmt = q.get("format", ["json"])[0]
        if fmt == "keys":
            return httpx.Response(200, text="\n".join(o["key"] for o in page), headers=headers)
        if fmt in ("bibtex", "biblatex", "ris", "csljson"):
            body = "\n".join(f"@article{{{o['key']},\n  title = {{{o['data'].get('title', '')}}}\n}}" for o in page)
            return httpx.Response(200, text=body, headers=headers)
        rows = []
        for o in page:
            row = self._out(o) if fmt_item is None else fmt_item(o)
            for inc in q.get("include", [""])[0].split(","):
                if inc == "bib":
                    row["bib"] = f"<div class=\"csl-bib-body\">{o['data'].get('title')} ({o['data'].get('date')}).</div>"
                if inc == "citation":
                    row["citation"] = f"<span>({o['data'].get('date') or 'n.d.'})</span>"
            rows.append(row)
        return httpx.Response(200, json=rows, headers=headers)

    def _coll_out(self, c: dict) -> dict:
        num = sum(1 for i in self.items.values() if c["key"] in i["data"].get("collections", []) and not i["data"].get("deleted"))
        return {"key": c["key"], "version": c["version"], "meta": {"numItems": num, "numCollections": sum(
            1 for x in self.collections.values() if x["data"]["parentCollection"] == c["key"])}, "data": dict(c["data"])}

    def _write_item(self, obj: dict, idx: str, result: dict) -> None:
        if "key" in obj and obj["key"] in self.items:
            it = self.items[obj["key"]]
            if obj.get("version") != it["version"]:
                result["failed"][idx] = {"code": 412, "message": "Item has been modified since specified version"}
                return
            changed = {k: v for k, v in obj.items() if k not in ("key", "version") and it["data"].get(k) != v}
            if not changed:
                result["unchanged"][idx] = obj["key"]
                return
            it["data"].update(changed)
            it["version"] = it["data"]["version"] = self._bump()
            result["successful"][idx] = self._out(it)
            result["success"][idx] = obj["key"]
            return
        if obj.get("itemType") not in TEMPLATES and obj.get("itemType") not in ("attachment", "note", "annotation"):
            result["failed"][idx] = {"code": 400, "message": f"'{obj.get('itemType')}' is not a valid item type"}
            return
        if obj.get("parentItem") and obj["parentItem"] not in self.items:
            result["failed"][idx] = {"code": 400, "message": "Parent item not found"}
            return
        key = self.add_item(obj["itemType"], **{k: v for k, v in obj.items() if k not in ("itemType", "key", "version")})
        result["successful"][idx] = self._out(self.items[key])
        result["success"][idx] = key

    # ---- Zotero API handler --------------------------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        u = urlparse(str(request.url))
        q = parse_qs(u.query)
        path = re.sub(rf"^/users/{self.user_id}", "", u.path)
        self.calls.append((request.method, path + ("?" + u.query if u.query else "")))
        m = request.method
        if request.headers.get("Zotero-API-Key") is None:
            return httpx.Response(403, text="no key")
        since = request.headers.get("If-Modified-Since-Version")
        if m == "GET" and since and int(since) >= self.version:
            return httpx.Response(304, headers={"Last-Modified-Version": str(self.version)})
        unmod = request.headers.get("If-Unmodified-Since-Version")

        if path == "/items/new":
            t = q.get("itemType", [""])[0]
            if t == "attachment":
                return httpx.Response(200, json={"itemType": "attachment", "linkMode": q.get("linkMode", ["imported_file"])[0],
                                                 "title": "", "accessDate": "", "url": "", "note": "", "tags": [],
                                                 "relations": {}, "contentType": "", "charset": "", "filename": "",
                                                 "md5": None, "mtime": None})
            if t == "note":
                return httpx.Response(200, json={"itemType": "note", "note": "", "tags": [], "relations": {}})
            if t not in TEMPLATES:
                return httpx.Response(400, text="Invalid item type")
            return httpx.Response(200, json={"itemType": t, **{f: "" for f in TEMPLATES[t]}, "creators": [],
                                             "tags": [], "collections": [], "relations": {}})
        if path == "/deleted":
            s = int(q.get("since", ["0"])[0])
            return httpx.Response(200, json={"items": [k for v, k in self.deleted if v > s], "collections": [],
                                             "searches": [], "tags": [], "settings": []},
                                  headers={"Last-Modified-Version": str(self.version)})
        if path == "/searches":
            return self._page(self.searches, q, fmt_item=lambda s: s)
        if path == "/tags":
            if m == "DELETE":
                if unmod is None or int(unmod) != self.version:
                    return httpx.Response(412, text="precondition failed")
                names = [t.strip() for t in q["tag"][0].split("||")]
                for it in self.items.values():
                    before = len(it["data"]["tags"])
                    it["data"]["tags"] = [t for t in it["data"]["tags"] if t["tag"] not in names]
                    if len(it["data"]["tags"]) != before:
                        it["version"] = it["data"]["version"] = self._bump()
                return httpx.Response(204)
            counts: dict[str, int] = {}
            for it in self.items.values():
                for t in it["data"].get("tags", []):
                    counts[t["tag"]] = counts.get(t["tag"], 0) + 1
            rows = [{"tag": t, "meta": {"numItems": n}} for t, n in sorted(counts.items())]
            return self._page(rows, q, fmt_item=lambda r: r)

        if path == "/collections":
            if m == "POST":
                result = {"successful": {}, "success": {}, "unchanged": {}, "failed": {}}
                for i, c in enumerate(json.loads(request.content)):
                    parent = c.get("parentCollection") or False
                    if parent and parent not in self.collections:
                        result["failed"][str(i)] = {"code": 400, "message": "Parent collection not found"}
                        continue
                    k = self.add_collection(c["name"], parent or None)
                    result["successful"][str(i)] = self._coll_out(self.collections[k])
                    result["success"][str(i)] = k
                return httpx.Response(200, json=result)
            return self._page(list(self.collections.values()), q, fmt_item=self._coll_out)
        mc = re.fullmatch(r"/collections/([A-Z0-9]{8})(/items/top|/items)?", path)
        if mc:
            k, sub = mc.group(1), mc.group(2)
            if k not in self.collections:
                return httpx.Response(404, text="Collection not found")
            c = self.collections[k]
            if sub:
                objs = [i for i in self.items.values() if k in i["data"].get("collections", [])]
                return self._page(self._filter(objs, q, top=sub.endswith("top")), q)
            if m == "GET":
                return httpx.Response(200, json=self._coll_out(c), headers={"Last-Modified-Version": str(c["version"])})
            if unmod is None or int(unmod) != c["version"]:
                return httpx.Response(412, text="precondition failed")
            if m == "PATCH":
                body = json.loads(request.content)
                if "name" in body:
                    c["data"]["name"] = body["name"]
                if "parentCollection" in body:
                    c["data"]["parentCollection"] = body["parentCollection"] or False
                c["version"] = c["data"]["version"] = self._bump()
                return httpx.Response(204)
            if m == "DELETE":
                gone = {k} | {x for x, v in self.collections.items() if v["data"]["parentCollection"] == k}
                for sk in gone:
                    del self.collections[sk]
                for it in self.items.values():
                    it["data"]["collections"] = [c for c in it["data"].get("collections", []) if c not in gone]
                self._bump()
                return httpx.Response(204)

        if path in ("/items", "/items/top", "/items/trash"):
            if m == "POST":
                result = {"successful": {}, "success": {}, "unchanged": {}, "failed": {}}
                for i, obj in enumerate(json.loads(request.content)):
                    self._write_item(obj, str(i), result)
                return httpx.Response(200, json=result)
            objs = self._filter(list(self.items.values()), q, top=path.endswith("top"), trash=path.endswith("trash"))
            return self._page(objs, q)
        mi = re.fullmatch(r"/items/([A-Z0-9]{8})(/children|/fulltext)?", path)
        if mi:
            k, sub = mi.group(1), mi.group(2)
            if k not in self.items:
                return httpx.Response(404, text="Item not found")
            it = self.items[k]
            if sub == "/children":
                objs = [o for o in self.items.values() if o["data"].get("parentItem") == k]
                return self._page(self._filter(objs, q | {"includeTrashed": ["1"]}), q)
            if sub == "/fulltext":
                if m == "PUT":
                    self.fulltext[k] = json.loads(request.content)
                    return httpx.Response(204)
                if k not in self.fulltext:
                    return httpx.Response(404, text="Full-text content not found")
                return httpx.Response(200, json=self.fulltext[k])
            if m == "GET":
                out = self._out(it)
                if k in self.on_get:
                    self.on_get.pop(k)()
                return httpx.Response(200, json=out, headers={"Last-Modified-Version": str(it["version"])})
            body = json.loads(request.content) if m == "PATCH" else {}
            if unmod is None and "version" in body:  # the API also accepts the version inside the object
                unmod = str(body.pop("version"))
            if unmod is None or int(unmod) != it["version"]:
                return httpx.Response(412, text="Item has been modified since specified version")
            if m == "PATCH":
                for f, v in body.items():
                    if f == "deleted":
                        if v:
                            it["data"]["deleted"] = True
                        else:
                            it["data"].pop("deleted", None)
                    else:
                        it["data"][f] = v
                it["version"] = it["data"]["version"] = self._bump()
                return httpx.Response(204)
            if m == "DELETE":
                del self.items[k]
                self.deleted.append((self._bump(), k))
                return httpx.Response(204)
        return httpx.Response(404, text=f"no route for {m} {path}")

    # ---- public APIs ---------------------------------------------------------------------------
    def public_handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        if "Zotero-API-Key" in request.headers:
            raise AssertionError("Zotero key sent to a third party")
        host = request.url.host
        if host in self.public_status:
            return httpx.Response(self.public_status[host], json={"error": "simulated"})
        pq = parse_qs(urlparse(url).query)
        if url.startswith("https://openlibrary.org/api/books?"):
            key = pq["bibkeys"][0]
            rec = self.openlibrary.get(key.removeprefix("ISBN:"))
            return httpx.Response(200, json={key: rec} if rec else {})
        if url.startswith("https://www.googleapis.com/books/v1/volumes?"):
            vols = self.googlebooks.get(pq["q"][0].removeprefix("isbn:"), [])
            return httpx.Response(200, json={"kind": "books#volumes", "totalItems": len(vols),
                                             **({"items": [{"volumeInfo": v} for v in vols]} if vols else {})})
        if url.startswith("https://services.dnb.de/sru/dnb?"):
            rec = self.dnb.get(pq["query"][0].removeprefix("num="))
            if self.dnb_diagnostic:
                body = ('<?xml version="1.0" encoding="UTF-8"?><searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
                        '<version>1.1</version><numberOfRecords>0</numberOfRecords><diagnostics>'
                        '<diagnostic xmlns="http://www.loc.gov/zing/srw/diagnostic/"><uri>info:srw/diagnostic/1/10</uri>'
                        f'<message>{self.dnb_diagnostic}</message></diagnostic></diagnostics></searchRetrieveResponse>')
                return httpx.Response(200, content=body.encode(), headers={"Content-Type": "application/xml"})
            body = ('<?xml version="1.0" encoding="UTF-8"?><searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
                    f'<version>1.1</version><numberOfRecords>{1 if rec else 0}</numberOfRecords>'
                    + (f'<records><record><recordSchema>MARC21-xml</recordSchema><recordPacking>xml</recordPacking>'
                       f'<recordData>{rec}</recordData><recordPosition>1</recordPosition></record></records>' if rec else "")
                    + '</searchRetrieveResponse>')
            return httpx.Response(200, content=body.encode(), headers={"Content-Type": "application/xml"})
        if url.startswith("https://api.crossref.org/works?"):
            q = parse_qs(urlparse(url).query).get("query.bibliographic", [""])[0].lower()
            hits = [w for w in self.crossref_query if any(t in q for t in w.get("_match", []))]
            return httpx.Response(200, json={"status": "ok", "message": {"items": hits}})
        mcr = re.match(r"https://api\.crossref\.org/works/(.+)$", url)
        if mcr:
            doi = mcr.group(1).lower()
            if doi in self.crossref:
                return httpx.Response(200, json={"status": "ok", "message": self.crossref[doi]})
            return httpx.Response(404, text="Resource not found.")
        moa = re.match(r"https://api\.openalex\.org/works/doi:(.+)$", url)
        if moa:
            w = self.openalex.get(moa.group(1).lower())
            return httpx.Response(200, json=w) if w else httpx.Response(404, json={"error": "not found"})
        if url in self.urls:
            status, body, ctype = self.urls[url]
            return httpx.Response(status, content=body, headers={"Content-Type": ctype})
        return httpx.Response(404, text="unknown url")
