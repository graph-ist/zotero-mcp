"""merge_duplicates: plan, token, execution order, refusals and failure in the middle."""
from zotero_mcp.items import _item_uri
from conftest import article


def _pair(zot):
    c1, c2 = zot.add_collection("Thesis"), zot.add_collection("Reading")
    a = article(zot, title="Designerly ways of knowing", doi="10.1016/0142-694x(82)90040-0", collections=[c1],
                tags=["design"], extra="Citation Key: cross1982\nVerified: 2026-11-01 match verify_doi registered_match",
                dateAdded="2020-01-01T00:00:00Z", volume="3")
    b = article(zot, title="Designerly Ways of Knowing", doi="", collections=[c2], tags=["design", "epistemology"],
                extra="Citation Key: cross82b\ntex.note: from BibTeX", abstractNote="An abstract.", issue="4",
                volume="03", dateAdded="2021-01-01T00:00:00Z")
    pdf = zot.add_item("attachment", parentItem=b, contentType="application/pdf")
    ann = zot.add_item("annotation", parentItem=pdf, annotationType="highlight", annotationText="x")
    note = zot.add_item("note", parentItem=b, note="<p>n</p>")
    return a, b, pdf, ann, note, c1, c2


def test_merge_plan_then_confirm(zot, call):
    a, b, pdf, ann, note, c1, c2 = _pair(zot)
    other = article(zot, title="Related work", doi="10.1/r", relations={"dc:relation": [_item_uri(b)]})
    zot.items[b]["data"]["relations"] = {"dc:relation": [_item_uri(other)]}
    plan = call("merge_duplicates", keys=[a, b], keep=a)
    assert plan["status"] == "plan" and plan["keep"]["key"] == a and [m["key"] for m in plan["merge"]] == [b]
    assert plan["fill"] == {"abstractNote": {"value": "An abstract.", "from": b}, "issue": {"value": "4", "from": b}}
    assert plan["extra_added"] == ["tex.note: from BibTeX"] and plan["tags_added"] == ["epistemology"]
    assert plan["collections_added"] == ["Reading"] and plan["children_to_move"] == {"attachment": 1, "note": 1}
    assert plan["conflicts"]["volume"] == {"kept": "3", "others": [{"key": b, "value": "03"}]}
    assert plan["conflicts"]["extra:Citation Key"]["others"] == [{"key": b, "value": "cross82b"}]
    assert plan["relinked_items"] == [other]
    v_before = {k: zot.items[k]["version"] for k in (a, b)}
    assert call("merge_duplicates", keys=[a, b], keep=a, confirm=True)["status"] == "confirm_required"
    assert {k: zot.items[k]["version"] for k in (a, b)} == v_before

    out = call("merge_duplicates", keys=[a, b], keep=a, confirm=True, plan_token=plan["plan_token"])
    assert out["status"] == "merged" and out["children_moved"] == 2 and out["trashed"] == [b] and out["relinked"] == 1
    d = zot.items[a]["data"]
    assert d["abstractNote"] == "An abstract." and d["issue"] == "4" and d["volume"] == "3"
    assert d["extra"].splitlines() == ["Citation Key: cross1982",
                                       "Verified: 2026-11-01 match verify_doi registered_match", "tex.note: from BibTeX"]
    assert [t["tag"] for t in d["tags"]] == ["design", "epistemology"] and d["collections"] == [c1, c2]
    assert d["relations"] == {"dc:relation": [_item_uri(other)], "dc:replaces": [_item_uri(b)]}
    assert zot.items[other]["data"]["relations"] == {"dc:relation": [_item_uri(a)]}
    assert zot.items[pdf]["data"]["parentItem"] == a and zot.items[note]["data"]["parentItem"] == a
    assert zot.items[ann]["data"]["parentItem"] == pdf and zot.items[b]["data"]["deleted"]


def test_merge_default_keeper_and_stale_token(zot, call):
    a, b, *_ = _pair(zot)
    plan = call("merge_duplicates", keys=[a, b])
    assert plan["keep"]["key"] == b  # it has the attachment and the note
    zot.items[a]["data"]["title"] = "edited elsewhere"
    zot.items[a]["version"] = zot.items[a]["data"]["version"] = zot._bump()
    out = call("merge_duplicates", keys=[a, b], confirm=True, plan_token=plan["plan_token"])
    assert out["status"] == "plan_changed" and out["plan_token"] != plan["plan_token"]
    assert not zot.items[a]["data"].get("deleted") and not zot.items[b]["data"].get("deleted")


def test_merge_stops_before_trash_when_a_child_cannot_move(zot, call):
    a, b, pdf, ann, note, *_ = _pair(zot)
    plan = call("merge_duplicates", keys=[a, b], keep=a)
    # the note is edited elsewhere right after the kept item is updated, i.e. after the token check
    orig = zot.handler

    def handler(request):
        resp = orig(request)
        if request.method == "PATCH" and request.url.path.endswith(f"/items/{a}"):
            zot.items[note]["version"] = zot.items[note]["data"]["version"] = zot._bump()
        return resp
    zot.handler = handler
    out = call("merge_duplicates", keys=[a, b], keep=a, confirm=True, plan_token=plan["plan_token"])
    assert out["status"] == "partial" and note in out["failed"] and out.get("trashed") is None
    assert not zot.items[b]["data"].get("deleted") and zot.items[pdf]["data"]["parentItem"] == a
    zot.handler = orig
    again = call("merge_duplicates", keys=[a, b], keep=a)
    assert again["children_to_move"] == {"note": 1}
    done = call("merge_duplicates", keys=[a, b], keep=a, confirm=True, plan_token=again["plan_token"])
    assert done["status"] == "merged" and zot.items[note]["data"]["parentItem"] == a and zot.items[b]["data"]["deleted"]


def test_merge_refusals(zot, call):
    a = article(zot, doi="10.1/a")
    b = article(zot, doi="10.1/b")
    pre = zot.add_item("preprint", title="Design epistemology", DOI="10.31234/osf.io/x")
    att = zot.add_item("attachment", parentItem=a, contentType="application/pdf")
    assert "different DOIs" in call("merge_duplicates", keys=[a, b])["detail"]
    assert "different item types" in call("merge_duplicates", keys=[a, pre])["detail"]
    assert "child item" in call("merge_duplicates", keys=[a, att])["detail"]
    assert "two different" in call("merge_duplicates", keys=[a, a.lower()])["detail"]
    zot.items[b]["data"]["DOI"] = "10.1/a"
    assert "not among" in call("merge_duplicates", keys=[a, b], keep=pre)["detail"]
    zot.items[b]["data"]["deleted"] = True
    assert "trash" in call("merge_duplicates", keys=[a, b])["detail"]
