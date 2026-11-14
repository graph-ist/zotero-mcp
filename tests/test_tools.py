"""Every MCP tool against the fake Zotero API. Each test builds the library it needs."""
import json
import os

import pytest

from zotero_mcp import config, core
from conftest import article, make_pdf, put_webdav, run

OCR = config.OCR_DIR


# ---- search and read -------------------------------------------------------------------------

def test_search_items_lists_top_level_with_collection_paths(zot, call):
    parent = zot.add_collection("Thesis")
    child = zot.add_collection("Chapter 1", parent)
    k = article(zot, collections=[child], tags=["read"])
    zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    out = call("search_items")
    assert out["total"] == 1 and out["items"][0]["key"] == k
    assert out["items"][0]["collections"] == ["Thesis › Chapter 1"]
    assert out["items"][0]["creators"] == ["Cross, Nigel"] and out["items"][0]["tags"] == ["read"]


def test_search_items_filters(zot, call):
    a = article(zot, title="Affordances in HCI", tags=["hci"])
    b = zot.add_item("book", title="Phenomenology of Perception", date="1945")
    assert [i["key"] for i in call("search_items", query="affordance")["items"]] == [a]
    assert [i["key"] for i in call("search_items", tag="hci")["items"]] == [a]
    assert [i["key"] for i in call("search_items", item_type="book")["items"]] == [b]
    c = zot.add_collection("Phil")
    zot.items[b]["data"]["collections"] = [c]
    assert [i["key"] for i in call("search_items", collection_key=c)["items"]] == [b]
    page = call("search_items", limit=1, start=1, sort="title")
    assert page["total"] == 2 and len(page["items"]) == 1 and page["start"] == 1


def test_search_items_rejects_unknown_item_type(zot, call):
    out = call("search_items", item_type="journal_article")
    assert out["status"] == "error" and "journal_article" in out["detail"]


def test_search_everything_reaches_fulltext_index(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    zot.fulltext[att] = {"content": "the tacit dimension of knowing"}
    assert call("search_items", query="tacit")["total"] == 0
    assert call("search_items", query="tacit", mode="everything")["total"] == 1


def test_get_item_returns_attachments_and_truncated_notes(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf", filename="Cross 2015.pdf")
    zot.add_item("note", parentItem=k, note="<p>first</p><p>" + "x" * 2000 + "</p>")
    out = call("get_item", key=k.lower())
    assert out["item"]["title"] == "Design epistemology" and "relations" not in out["item"]
    assert out["attachments"][0]["key"] == att and out["attachments"][0]["filename"] == "Cross 2015.pdf"
    assert out["notes"][0]["text"].startswith("first\n") and len(out["notes"][0]["text"]) == 1000


def test_get_item_bad_key(zot, call):
    out = call("get_item", key="nope")
    assert out["status"] == "error" and "8 characters" in out["detail"]
    assert call("get_item", key="ZZZZZZZZ")["status"] == "error"


def test_get_fulltext_prefers_index_then_webdav_and_paginates(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    zot.fulltext[att] = {"content": "a" * 1500}
    out = call("get_fulltext", key=k, max_chars=1000)
    assert out["source"] == "zotero_fulltext_index" and out["returned_chars"] == 1000 and out["more"]
    assert call("get_fulltext", key=k, offset=1000, max_chars=1000)["more"] is False
    del zot.fulltext[att]
    put_webdav(att, "Cross 2015.pdf", make_pdf(["Page one text about design, long enough to count as text.", "Page two, also long enough to count as a text page here."]))
    out = call("get_fulltext", key=att)
    assert out["source"] == "webdav_pdf" and "Page two" in out["text"] and "ocr" not in out


def test_get_fulltext_errors(zot, call):
    k = article(zot)
    assert "no PDF" in call("get_fulltext", key=k)["detail"]
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    assert "sync files" in call("get_fulltext", key=k)["detail"]


def test_get_fulltext_scan_queues_ocr(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    put_webdav(att, "scan.pdf", make_pdf(["", "", ""]))
    out = call("get_fulltext", key=k)
    assert out["ocr"] == "queued" and os.path.exists(os.path.join(OCR, "queue", f"{att}.json"))
    assert call("ocr_status", key=k)["state"] == "queued"


def test_list_collections_sorted_paths(zot, call):
    p = zot.add_collection("B parent")
    zot.add_collection("A child", p)
    zot.add_collection("A top")
    out = call("list_collections")["collections"]
    assert [c["path"] for c in out] == ["A top", "B parent", "B parent › A child"]
    assert all({"key", "path", "numItems"} <= set(c) for c in out)


def test_list_tags_trash_saved_searches(zot, call):
    article(zot, tags=["a", "b"])
    k = zot.add_item("book", title="Gone", deleted=True)
    zot.searches.append({"key": "SRCH0001", "version": 1, "data": {"name": "Recent", "conditions": [{"condition": "dateAdded", "operator": "isInTheLast", "value": "7 days"}]}})
    tags = call("list_tags")
    assert tags["total"] == 2 and tags["tags"][0] == {"tag": "a", "numItems": 1}
    assert [i["key"] for i in call("list_trash")["items"]] == [k]
    assert call("list_saved_searches")["searches"][0]["name"] == "Recent"


# ---- create --------------------------------------------------------------------------------

def test_create_item_validates_fields(zot, call):
    out = call("create_item", item_type="book", fields={"title": "T", "bogus": "x"})
    assert out["status"] == "error" and "bogus" in out["detail"] and "publisher" in out["detail"]
    assert "unknown itemType" in call("create_item", item_type="paper", fields={})["detail"]


def test_create_item_ok(zot, call):
    c = zot.add_collection("New")
    out = call("create_item", item_type="book", fields={"title": "T", "date": "1999"},
               creators=[{"creatorType": "author", "lastName": "Schön", "firstName": "Donald"}],
               collection_keys=[c], tags=["t1"])
    assert out["status"] == "ok" and len(out["created_keys"]) == 1
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["collections"] == [c] and d["tags"] == [{"tag": "t1"}] and d["creators"][0]["lastName"] == "Schön"


def test_create_item_from_doi_maps_crossref_and_refuses_duplicates(zot, call):
    zot.crossref["10.1016/j.destud.2015.01.001"] = {
        "type": "journal-article", "title": ["Designerly ways"], "subtitle": ["Of knowing"],
        "container-title": ["Design Studies"], "volume": "3", "issue": "4", "page": "221-227",
        "issued": {"date-parts": [[1982, 10]]}, "DOI": "10.1016/j.destud.2015.01.001",
        "author": [{"family": "Cross", "given": "Nigel"}, {"name": "Design Research Society"}],
        "abstract": "<jats:p>Abstract <i>text</i></jats:p>"}
    out = call("create_item_from_doi", doi="https://doi.org/10.1016/j.destud.2015.01.001", tags=["new"])
    assert out["status"] == "ok" and out["itemType"] == "journalArticle" and out["title"] == "Designerly ways: Of knowing"
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["date"] == "1982-10" and d["pages"] == "221-227" and d["abstractNote"] == "Abstract text"
    assert d["creators"][1] == {"creatorType": "author", "name": "Design Research Society"}
    dup = call("create_item_from_doi", doi="10.1016/J.DESTUD.2015.01.001")
    assert dup["status"] == "duplicate" and dup["existing"]["key"] == out["created_keys"][0]
    assert call("create_item_from_doi", doi="10.1016/j.destud.2015.01.001", allow_duplicate=True)["status"] == "ok"
    assert "Crossref" in call("create_item_from_doi", doi="10.9999/none")["detail"]


def test_create_collection(zot, call):
    p = call("create_collection", name="Top")
    c = call("create_collection", name="Sub", parent_key=p["key"])
    assert p["status"] == c["status"] == "ok"
    assert [x["path"] for x in call("list_collections")["collections"]] == ["Top", "Top › Sub"]


def test_add_note_html(zot, call):
    k = article(zot)
    out = call("add_note", parent_key=k, text="First <b>para</b>\nline two\n\nSecond")
    assert out["status"] == "ok"
    assert zot.items[out["created_keys"][0]]["data"]["note"] == "<p>First &lt;b&gt;para&lt;/b&gt;<br/>line two</p><p>Second</p>"


# ---- DOIs and PDFs ---------------------------------------------------------------------------

def test_find_dois_matches_field_and_extra(zot, call):
    a = article(zot, doi="10.1000/ABC")
    b = zot.add_item("book", title="Book", extra="DOI: 10.5555/book\nOther: x")
    out = call("find_dois", dois=["https://doi.org/10.1000/abc", "doi:10.5555/BOOK", "10.1/missing"])
    assert set(out["in_library"]) == {"https://doi.org/10.1000/abc", "doi:10.5555/BOOK"}
    assert out["in_library"]["doi:10.5555/BOOK"]["key"] == b and out["missing"] == ["10.1/missing"]
    assert out["library_dois"] == 2
    calls_before = len(zot.calls)
    call("find_dois", dois=["10.1000/abc"])  # unchanged library: one conditional request, no re-scan
    assert len(zot.calls) - calls_before == 1
    article(zot, doi="10.1000/new")
    assert "10.1000/new" in call("find_dois", dois=["10.1000/new"])["in_library"]


def test_attach_pdf_from_open_access(zot, call):
    k = article(zot, doi="10.1000/oa")
    pdf = make_pdf(["Open access text"])
    zot.openalex["10.1000/oa"] = {"best_oa_location": {"pdf_url": "https://pub.example/blocked.pdf", "version": "publishedVersion"},
                                  "locations": [{"pdf_url": "https://repo.example/oa.pdf", "version": "acceptedVersion",
                                                 "license": "cc-by", "source": {"display_name": "Repo"}}]}
    zot.urls["https://pub.example/blocked.pdf"] = (200, b"<html>paywall</html>", "text/html")
    zot.urls["https://repo.example/oa.pdf"] = (200, pdf, "application/pdf")
    out = call("attach_pdf", key=k)
    assert out["status"] == "queued" and out["host"] == "Repo" and out["bytes"] == len(pdf)
    att = out["attachment_key"]
    d = zot.items[att]["data"]
    assert d["parentItem"] == k and d["filename"] == "Cross 2015.pdf" and d["contentType"] == "application/pdf"
    assert open(os.path.join(OCR, "incoming", f"{att}.pdf"), "rb").read() == pdf
    assert json.load(open(os.path.join(OCR, "queue", f"{att}.json")))["action"] == "attach"
    assert call("attach_pdf", key=k)["status"] == "already_has_pdf"


def test_attach_pdf_failures(zot, call):
    k = article(zot, doi="")
    assert "no DOI" in call("attach_pdf", key=k)["detail"]
    zot.urls["https://x.example/landing"] = (200, b"<html>", "text/html")
    out = call("attach_pdf", key=k, url="https://x.example/landing")
    assert out["status"] == "download_failed" and "landing page" in out["tried"][0]
    k2 = article(zot, doi="10.1000/none")
    assert call("attach_pdf", key=k2)["status"] == "no_open_access_pdf"
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    assert "regular item" in call("attach_pdf", key=att)["detail"]


def test_get_annotations_in_page_order_paginated(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    for i, page in enumerate(("12", "3", "7")):
        zot.add_item("annotation", parentItem=att, annotationType="highlight", annotationText=f"t{i}",
                     annotationPageLabel=page, annotationSortIndex=f"{int(page):05d}|000000|00000",
                     annotationColor="#ffd400", tags=["important"] if i == 0 else [])
    out = call("get_annotations", key=k)
    assert out["total"] == 3 and [a["page"] for a in out["annotations"]] == ["3", "7", "12"]
    assert out["annotations"][2]["tags"] == ["important"] and "sort" not in out["annotations"][0]
    page = call("get_annotations", key=att, limit=2, start=2)
    assert page["returned"] == 1 and page["more"] is False and page["annotations"][0]["text"] == "t0"


def test_annotate_pdf_locates_and_creates(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    put_webdav(att, "a.pdf", make_pdf(["Intro.", "Design knowledge is distinct from experi-\nence alone."]))
    dry = call("annotate_pdf", key=k, text="distinct from experience alone", dry_run=True)
    assert dry == {"status": "found", "attachment_key": att, "page": "2", "lines": 2}
    out = call("annotate_pdf", key=k, text="distinct from experience alone", type="underline", color="blue",
               comment="hyphenation", tags=["method"])
    assert out["status"] == "ok" and out["page"] == "2"
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["annotationType"] == "underline" and d["annotationColor"] == "#2ea8e5" and d["parentItem"] == att
    pos = json.loads(d["annotationPosition"])
    assert pos["pageIndex"] == 1 and len(pos["rects"]) == 2 and d["tags"] == [{"tag": "method"}]
    missing = call("annotate_pdf", key=k, text="not in the document")
    assert "text not found" in missing["detail"] and "ocr_pdf" in missing["detail"]


def test_annotate_pdf_validation(zot, call):
    k = article(zot)
    with pytest.raises(Exception, match="yellow"):
        call("annotate_pdf", key=k, text="x", color="pink")
    assert "no PDF attachment" in call("annotate_pdf", key=k, text="x")["detail"]


def test_ocr_pdf_and_status(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    assert call("ocr_status", key=att)["state"] == "never queued"
    out = call("ocr_pdf", key=k, replace=True, mode="rasterize")
    assert out["status"] == "queued" and out["attachment_key"] == att
    job = json.load(open(os.path.join(OCR, "queue", f"{att}.json")))
    assert job["mode"] == "rasterize" and job["replace"] == "yes"
    st = call("ocr_status")
    assert st["total"] >= 1 and any(j["key"] == att for j in st["jobs"])


# ---- edit ------------------------------------------------------------------------------------

def test_update_item(zot, call):
    k = article(zot)
    assert "cannot change" in call("update_item", key=k, fields={"tags": []})["detail"]
    assert "not valid" in call("update_item", key=k, fields={"nope": 1})["detail"]
    out = call("update_item", key=k, fields={"date": "2016", "creators": [{"creatorType": "author", "name": "X"}]})
    assert out["status"] == "ok" and zot.items[k]["data"]["date"] == "2016" and zot.items[k]["data"]["creators"] == [{"creatorType": "author", "name": "X"}]


def test_update_item_conflict_gives_hint(zot, call):
    k = article(zot)
    zot.on_get[k] = lambda: zot.items[k].__setitem__("version", zot.items[k]["version"] + 1)
    out = call("update_item", key=k, fields={"date": "2016"})
    assert out["status"] == "error" and out["http_status"] == 412 and "fetch it again" in out["hint"]


def test_update_item_links(zot, call):
    c1, c2 = zot.add_collection("One"), zot.add_collection("Two")
    k = article(zot, tags=["old", "keep"], collections=[c1])
    assert "at least one" in call("update_item_links", key=k)["detail"]
    out = call("update_item_links", key=k, add_tags=["new", "keep"], remove_tags=["old"],
               add_collections=[c2], remove_collections=[c1])
    assert out["status"] == "ok" and out["tags"] == ["keep", "new"] and out["collections"] == ["Two"]
    assert zot.items[k]["data"]["collections"] == [c2]


def test_update_items_batch(zot, call):
    c = zot.add_collection("Batch")
    a, b = article(zot, collections=[c]), article(zot, title="Other", collections=[c])
    zot.add_item("attachment", parentItem=a, contentType="application/pdf", collections=[c])
    assert "give keys" in call("update_items_batch")["detail"]
    assert "cannot set" in call("update_items_batch", keys=[a], fields={"itemType": "book"})["detail"]
    out = call("update_items_batch", collection_key=c, add_tags=["batch"], fields={"extra": "read"})
    assert out["matched"] == 2 and out["updated"] == 2 and out["failed"] is None
    assert all(zot.items[k]["data"]["extra"] == "read" and zot.items[k]["data"]["tags"] == [{"tag": "batch"}] for k in (a, b))
    assert call("update_items_batch", keys=[a], add_tags=["batch"])["unchanged"] == 1


def test_trash_restore(zot, call):
    k = article(zot)
    out = call("trash_item", key=k)
    assert out["status"] == "ok" and out["title"] == "Design epistemology" and zot.items[k]["data"]["deleted"]
    assert call("search_items")["total"] == 0 and call("list_trash")["total"] == 1
    assert call("restore_item", key=k)["status"] == "ok" and "deleted" not in zot.items[k]["data"]


def test_update_collection(zot, call):
    a = zot.add_collection("A")
    b = zot.add_collection("B", a)
    assert "nothing to change" in call("update_collection", key=a)["detail"]
    assert "inside itself" in call("update_collection", key=a, parent_key=b)["detail"]
    out = call("update_collection", key=b, name="B2", move_to_top=True)
    assert out["status"] == "ok" and out["path"] == "B2"
    assert call("update_collection", key=b, parent_key=a)["path"] == "A › B2"


def test_delete_collection(zot, call):
    a = zot.add_collection("A")
    b = zot.add_collection("B", a)
    k = article(zot, collections=[a, b])
    refused = call("delete_collection", key=a)
    assert refused["status"] == "refused" and refused["subcollections"] == ["A › B"]
    out = call("delete_collection", key=a, include_subcollections=True)
    assert out["status"] == "ok" and out["deleted"] == "A" and not zot.collections
    assert k in zot.items and zot.items[k]["data"]["collections"] == []


def test_rename_and_delete_tag(zot, call):
    a = article(zot, tags=["old", "new"])
    b = article(zot, title="B", tags=["old"])
    c = article(zot, title="C", tags=["other"])
    assert call("rename_tag", old="none", new="x")["updated"] == 0
    out = call("rename_tag", old="old", new="new")
    assert out["status"] == "ok" and out["updated"] == 2
    assert zot.items[a]["data"]["tags"] == [{"tag": "new"}] and zot.items[b]["data"]["tags"] == [{"tag": "new"}]
    assert call("delete_tag", tags=["new", "other"])["status"] == "ok"
    assert all(zot.items[k]["data"]["tags"] == [] for k in (a, b, c))


def test_related(zot, call):
    a, b = article(zot), article(zot, title="B")
    out = call("set_related", key=a, other_key=b)
    assert out["status"] == "ok" and out["updated"] == 2
    assert [r["key"] for r in call("list_related", key=b)["related"]] == [a]
    assert call("set_related", key=a, other_key=b, remove=True)["updated"] == 2
    assert call("list_related", key=a)["related"] == []


# ---- output ----------------------------------------------------------------------------------

def test_format_citations(zot, call):
    a = article(zot)
    out = call("format_citations", keys=[a], style="apa")
    assert out["entries"][0] == {"key": a, "citation": "(2015)", "bib": "Design epistemology (2015)."}
    with pytest.raises(Exception):
        call("format_citations", keys=[])


def test_export_items_keys_and_collection_cap(zot, call):
    c = zot.add_collection("Exp")
    keys = [article(zot, title=f"T{i}", collections=[c]) for i in range(3)]
    out = call("export_items", keys=keys[:2], format="bibtex")
    assert out["content"].count("@article") == 2 and out["truncated"] is False
    out = call("export_items", collection_key=c, max_items=2)
    assert out["exported"] == 2 and out["total"] == 3 and out["truncated"] and out["content"].count("@article") == 2
    assert "give keys" in call("export_items")["detail"]


def test_find_duplicates(zot, call):
    article(zot, doi="10.1/dup"); article(zot, title="Same Title Here!", doi="10.1/dup")
    article(zot, title="same title here", doi=""); article(zot, title="Unique one", doi="")
    out = call("find_duplicates")
    assert out["scanned"] == 4 and out["total_groups"] == 2 and {g["match"] for g in out["groups"]} == {"doi", "title"}
    assert len(call("find_duplicates", limit=1)["groups"]) == 1


# ---- cross-cutting ---------------------------------------------------------------------------

def test_unexpected_exception_becomes_structured_error(zot, call, monkeypatch):
    k = article(zot)
    async def boom(*a, **kw):
        raise RuntimeError("socket closed")
    monkeypatch.setattr(core, "req", boom)
    out = call("get_item", key=k)
    assert out["status"] == "error" and "RuntimeError" in out["detail"] and "retry" in out["hint"]


def test_retry_on_429(zot, call):
    k = article(zot)
    zot.fail_next = [429, 503]
    assert call("get_item", key=k)["item"]["key"] == k


def test_doi_index_is_incremental(zot, call):
    a = article(zot, doi="10.1/a")
    assert call("find_dois", dois=["10.1/a"])["missing"] == []
    n = len(zot.calls)
    b = article(zot, title="B", doi="10.1/b")
    zot.items[a]["data"]["DOI"] = "10.1/a2"
    zot.items[a]["version"] = zot.items[a]["data"]["version"] = zot._bump()
    out = call("find_dois", dois=["10.1/a", "10.1/a2", "10.1/b"])
    assert out["missing"] == ["10.1/a"] and out["in_library"]["10.1/b"]["key"] == b
    assert [c for c in zot.calls[n:] if "since=" in c[1]] and not any("start=100" in c[1] for c in zot.calls[n:])
    del zot.items[b]
    zot.deleted.append((zot._bump(), b))
    assert call("find_dois", dois=["10.1/b"])["missing"] == ["10.1/b"]
    call("trash_item", key=a)
    assert call("find_dois", dois=["10.1/a2"])["missing"] == ["10.1/a2"]


def test_refresh_library_snapshot_full_then_incremental(zot, call):
    from zotero_mcp import snapshot
    c = zot.add_collection("Phil")
    a = article(zot, doi="10.1/a", collections=[c], tags=["read"])
    zot.add_item("attachment", parentItem=a, contentType="application/pdf")
    out = call("refresh_library_snapshot")
    assert out["status"] == "written" and out["items"] == 1
    snap = json.load(open(snapshot.out_path()))
    assert snap["items"][0]["collections"] == ["Phil"] and snap["items"][0]["attachments"][0]["contentType"] == "application/pdf"
    assert call("refresh_library_snapshot")["status"] == "unchanged"
    n = len(zot.calls)
    b = article(zot, title="B", doi="", extra="DOI: 10.1/b")
    zot.collections[c]["data"]["name"] = "Philosophy"
    call("trash_item", key=a)
    out = call("refresh_library_snapshot")
    assert out["status"] == "updated" and out["items"] == 1
    snap = json.load(open(snapshot.out_path()))
    assert snap["items"][0]["key"] == b and snap["items"][0]["extra_dois"] == ["10.1/b"]
    assert snap["collections"] == ["Philosophy"] and any("since=" in x[1] for x in zot.calls[n:])


def test_tool_count_and_names():
    names = sorted(t.name for t in run(core.mcp.list_tools()))
    assert len(names) == 40 and {"update_item_links", "set_verification", "create_item_from_patent", "index_fulltext"} <= set(names) and "set_item_collections" not in names


def test_worker_uses_shared_client(zot):
    from zotero_mcp import ocr_worker as w
    w._client = None
    k = article(zot, language="it-IT")
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    assert w.languages(att) == "ita" and w.languages("ZZZZZZZZ") == w.LANGS
    w.sync_file_metadata(att, "d41d8cd98f00b204e9800998ecf8427e", 1700000000000)
    assert zot.items[att]["data"]["md5"] == "d41d8cd98f00b204e9800998ecf8427e"
    w._client = None


def test_export_annotations_markdown_and_note(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    zot.add_item("annotation", parentItem=att, annotationType="highlight", annotationText="tacit knowing",
                 annotationPageLabel="12", annotationSortIndex="00011|000000|00000", annotationColor="#ffd400",
                 annotationComment="link to Polanyi", tags=["epistemology"])
    zot.add_item("annotation", parentItem=att, annotationType="underline", annotationText="reflection-in-action",
                 annotationPageLabel="3", annotationSortIndex="00002|000000|00000", annotationColor="#2ea8e5")
    assert "give key or collection_key" in call("export_annotations")["detail"]
    out = call("export_annotations", key=k)
    assert out["items"] == 1 and out["annotations"] == 2
    assert out["markdown"].splitlines() == [
        f"## Design epistemology (Cross, 2015) [{k}]",
        '- p. 3: _"reflection-in-action"_ (blue)',
        '- p. 12: "tacit knowing" — link to Polanyi #epistemology']
    out = call("export_annotations", key=k, save_as_note=True)
    note = zot.items[out["note_key"]]["data"]
    assert out["note"] == "created" and note["parentItem"] == k and "<li>p. 12:" in note["note"] and "&quot;tacit" in note["note"]
    again = call("export_annotations", key=k, save_as_note=True)
    assert again["note"] == "replaced" and again["note_key"] == out["note_key"]
    assert sum(1 for i in zot.items.values() if i["data"]["itemType"] == "note") == 1
    c = zot.add_collection("Coll")
    zot.items[k]["data"]["collections"] = [c]
    article(zot, title="No annotations", collections=[c])
    out = call("export_annotations", collection_key=c)
    assert out["items"] == 1 and out["markdown"].startswith("## Design epistemology")


def test_collections_load_in_the_first_minutes_after_boot(zot, call, monkeypatch):
    """The cache sentinel must not read as 'fresh' when time.monotonic() is small (just after a reboot)."""
    import time as _time
    zot.add_collection("Early")
    monkeypatch.setattr(_time, "monotonic", lambda: 42.0)
    core.reset_clients()
    assert [c["path"] for c in call("list_collections")["collections"]] == ["Early"]


def test_matching_module_is_the_doi_mcp_copy():
    """zotero_mcp/matching.py is vendored from the DOI MCP: the same rules must apply on both sides."""
    from zotero_mcp import matching
    assert matching.MATCHING_VERSION == 1 and matching.TITLE_THRESHOLD == 0.88
    assert matching.eligible(matching.assess({"title": "Designerly ways of knowing", "authors": ["Nigel Cross"], "year": 1982},
                                             "Designerly ways of knowing", "Cross", 1982))
    assert not matching.eligible(matching.assess({"title": "Designerly ways of knowing", "authors": ["Nigel Cross"], "year": 1982},
                                                 "Designerly ways of knowing", "Schön", 1982))


def test_create_item_from_doi_records_the_verification(zot, call):
    zot.crossref["10.1000/v"] = {"type": "journal-article", "title": ["Designerly ways of knowing"],
                                 "issued": {"date-parts": [[1982]]}, "DOI": "10.1000/v",
                                 "author": [{"family": "Cross", "given": "Nigel"}]}
    plain = call("create_item_from_doi", doi="10.1000/v", allow_duplicate=True)
    assert plain["verified"] is False and "Verified" not in (zot.items[plain["created_keys"][0]]["data"].get("extra") or "")
    checked = call("create_item_from_doi", doi="10.1000/v", allow_duplicate=True,
                   expected_title="Designerly ways of knowing", expected_first_author="Cross", expected_year=1982)
    extra = zot.items[checked["created_keys"][0]]["data"]["extra"]
    assert checked["verified"] is True and extra.startswith("Verified: ") and " match create_item_from_doi" in extra
    # set_verification replaces the line and the snapshot exposes it
    out = call("set_verification", key=checked["created_keys"][0], status="match", source="verify_doi registered_match")
    assert out["status"] == "ok" and out["verified"]["status"] == "match" and out["verified"]["detail"] == "verify_doi registered_match"
    extra = zot.items[checked["created_keys"][0]]["data"]["extra"]
    assert extra.count("Verified:") == 1 and "verify_doi registered_match" in extra
    from zotero_mcp import snapshot
    assert snapshot.entry(zot.items[checked["created_keys"][0]]["data"])["verified"]["status"] == "match"


def test_create_item_from_patent(zot, call):
    out = call("create_item_from_patent", number="EP 3 500 000 B1", title="Foldable display device",
               applicants=["ACME GMBH"], inventors=["LEE, JUNG", "Khalilbeigi Mohammadreza"], published="2020-01-15",
               priority="2017-06-01", abstract="A hinge.", url="https://worldwide.espacenet.com/patent/search?q=pn%3DEP3500000",
               family_id="12345", ipc=["G06F 1/16"], tags=["prior-art/foldable"])
    assert out["status"] == "ok" and out["patentNumber"] == "EP3500000B1"
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["itemType"] == "patent" and d["assignee"] == "ACME GMBH" and d["issuingAuthority"] == "European Patent Office"
    assert d["creators"][0] == {"creatorType": "inventor", "lastName": "Lee", "firstName": "Jung"}
    assert "EPO family: 12345" in d["extra"] and "IPC: G06F 1/16" in d["extra"] and d["issueDate"] == "2020-01-15"
    dup = call("create_item_from_patent", number="EP3500000", title="Foldable display device")
    assert dup["status"] == "duplicate" and dup["existing"]["key"] == out["created_keys"][0]
    assert "not a patent" in call("create_item_from_patent", number="10.1000/x", title="t")["detail"]
    from zotero_mcp import snapshot
    assert snapshot.entry(d)["patent_number"] == "EP3500000B1"


def test_writes_schedule_a_snapshot_refresh(zot, monkeypatch):
    """A successful write schedules library.json to be refreshed; GETs and failures do not."""
    from zotero_mcp import snapshot
    monkeypatch.setattr(config, "AUTO_REFRESH", True)
    if os.path.exists(snapshot.out_path()):
        os.remove(snapshot.out_path())

    async def scenario():
        await core.req("GET", "/items/top", params={"limit": 1})
        assert core._refresh["task"] is None
        await core.req("POST", "/items", json=[{"itemType": "book", "title": "T", "creators": [], "tags": [], "collections": [], "relations": {}}])
        task = core._refresh["task"]
        assert task is not None and not task.done()
        task.cancel()
        t = core.schedule_snapshot_refresh(delay=0)
        await t
    run(scenario())
    assert json.load(open(snapshot.out_path()))["items"][0]["title"] == "T"
    core._refresh["task"] = None


def test_index_fulltext_writes_pages_and_annotations(zot, call):
    from zotero_mcp import snapshot
    k = article(zot, title="Design epistemology", doi="10.1000/ft")
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    body = " Body text long enough not to look like a scan." * 6
    put_webdav(att, "p.pdf", make_pdf(["First page about affordances." + body, "Second page about perception." + body]))
    zot.add_item("annotation", parentItem=att, annotationType="highlight", annotationText="about affordances",
                 annotationComment="key passage", annotationPageLabel="1", annotationSortIndex="00000|000000|00000")
    from zotero_mcp import snapshot
    if os.path.exists(snapshot.out_path()):
        os.remove(snapshot.out_path())  # other tests leave a snapshot of another fake library behind
    call("refresh_library_snapshot")
    out = call("index_fulltext", item_keys=[k])
    assert out["items_indexed"] == 1 and out["pages"] == 2 and out["annotations"] == 1
    doc = json.load(open(os.path.join(config.KNOWLEDGE_DIR, "fulltext", f"{k}.json")))
    assert doc["title"] == "Design epistemology" and doc["pages"][1]["text"].startswith("Second page")
    assert doc["annotations"][0]["text"] == "about affordances" and doc["annotations"][0]["page"] == "1"
    again = call("index_fulltext", item_keys=[k])
    assert again["items_indexed"] == 0 and again["already_done"] == 1
    assert snapshot.entry(dict(zot.items[k]["data"], extra="Citation Key: cross1982design"))["citekey"] == "cross1982design"


def test_http_auth_requires_bearer_header_and_status_is_loopback_only(monkeypatch):
    import httpx
    from zotero_mcp import transport
    from zotero_mcp.transport import TokenAuth

    async def inner(scope, receive, send):
        from starlette.responses import JSONResponse
        await JSONResponse({"ok": True})(scope, receive, send)

    token = "t" * 32
    app = TokenAuth(inner, token)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as c:
            assert (await c.get("/mcp?token=" + token)).status_code == 401
            assert (await c.get("/mcp")).status_code == 401
            ok = await c.get("/mcp", headers={"Authorization": f"Bearer {token}"})
            assert ok.status_code == 200 and ok.json() == {"ok": True}
            monkeypatch.setattr(transport, "ALLOW_QUERY_TOKEN", True)  # Claude web: the token in the URL
            assert (await c.get("/mcp?token=" + token)).status_code == 200
            assert (await c.get("/mcp?token=wrong")).status_code == 401
            monkeypatch.setattr(transport, "ALLOW_QUERY_TOKEN", False)
            st = await c.get("/status")
            assert st.status_code == 200 and st.json()["service"] == "zotero-mcp" and "fulltext" in st.json()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.5", 4000)), base_url="http://host") as c:
            assert (await c.get("/status")).status_code == 404
    run(scenario())


def test_contract_fixture_matches_the_snapshot_writer(zot):
    """tests/fixtures/contract/library.json is what the DOI MCP's tests read: regenerate it with
    UPDATE_CONTRACT=1 when the snapshot format changes, and copy it to the DOI MCP."""
    from zotero_mcp import snapshot
    c = zot.add_collection("Design", key="C0000001")
    sub = zot.add_collection("Theory", parent=c, key="C0000002")
    a = zot.add_item("journalArticle", key="ZKEY0001", title="Designerly ways of knowing", date="1982", DOI="10.1016/0142-694X(82)90040-0",
                     creators=[{"creatorType": "author", "lastName": "Cross", "firstName": "Nigel"}], collections=[sub],
                     tags=["cited/thesis", "core/seminal", "sub/330304-design-theory"],
                     extra="Citation Key: cross1982designerly\nVerified: 2026-11-04 match verify_doi registered_match")
    zot.add_item("attachment", key="ATT00001", parentItem=a, contentType="application/pdf")
    zot.add_item("book", key="ZKEY0002", title="The reflective practitioner", date="1983", ISBN="9780465068784",
                 creators=[{"creatorType": "author", "lastName": "Schön", "firstName": "Donald A."}], tags=["cited/thesis"])
    zot.add_item("patent", key="ZKEY0003", title="Foldable display device", patentNumber="EP 3500000 B1", date="2020",
                 creators=[{"creatorType": "inventor", "lastName": "Lee", "firstName": "Jung"}], tags=["prior-art/foldable"])
    items: dict = {}
    snapshot._apply(items, [o for o in zot.items.values()], [])
    snap = snapshot._assemble(items, list(zot.collections.values()), 7)
    snap["generated"] = "2026-11-04T00:00:00Z"
    path = os.path.join(os.path.dirname(__file__), "fixtures", "contract", "library.json")
    if os.environ.get("UPDATE_CONTRACT"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        json.dump(snap, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    assert json.load(open(path, encoding="utf-8")) == json.loads(json.dumps(snap, sort_keys=True)), \
        "snapshot format changed: run with UPDATE_CONTRACT=1 and copy tests/fixtures/contract to the DOI MCP"
