"""index_fulltext(changed=true) / index_worker.py: the whole library kept indexed incrementally."""
import json
import os
import shutil

import pytest

from zotero_mcp import checks
from conftest import article, make_pdf, put_webdav

BODY = " Body text long enough not to look like a scan." * 6


def _clean():
    from zotero_mcp import snapshot
    for p in (snapshot.out_path(), checks._state_path()):
        if os.path.exists(p):
            os.remove(p)
    shutil.rmtree(checks.fulltext_dir(), ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_knowledge_dir():
    """Each test starts without snapshot, state or fulltext files, and leaves none behind."""
    _clean()
    yield
    _clean()


def _with_pdf(zot, title, file=True):
    k = article(zot, title=title, doi="")
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    if file:
        put_webdav(att, "p.pdf", make_pdf([f"{title} first page." + BODY]))
    return k, att


def _files():
    d = checks.fulltext_dir()
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []


def test_changed_indexes_new_then_only_what_changed(zot, call):
    _clean()
    a, att_a = _with_pdf(zot, "Affordances")
    b, att_b = _with_pdf(zot, "Broken", file=False)
    article(zot, title="No attachment", doi="")
    first = call("index_fulltext", changed=True)
    assert first["items_indexed"] == 1 and first["indexed"][b] == "no file on WebDAV" and _files() == [a]
    state = json.load(open(checks._state_path()))
    assert state["pending"] == [] and b in state["skip"]

    quiet = call("index_fulltext", changed=True)
    assert quiet["indexed"] == {} and quiet["remaining"] == 0  # b is not retried while unchanged

    zot.add_item("annotation", parentItem=att_a, annotationType="highlight", annotationText="first page",
                 annotationComment="note", annotationPageLabel="1")
    put_webdav(att_b, "p.pdf", make_pdf(["Broken, now with a file." + BODY]))
    zot.items[att_b]["data"]["filename"] = "p.pdf"
    zot.items[att_b]["version"] = zot.items[att_b]["data"]["version"] = zot._bump()
    again = call("index_fulltext", changed=True)
    assert set(again["indexed"]) == {a, b} and _files() == sorted([a, b])
    doc = json.load(open(os.path.join(checks.fulltext_dir(), f"{a}.json")))
    assert doc["annotations"][0]["comment"] == "note"

    zot.items[a]["data"]["deleted"] = True
    zot.items[a]["version"] = zot.items[a]["data"]["version"] = zot._bump()
    gone = call("index_fulltext", changed=True)
    assert gone["removed"] == [a] and _files() == [b]


def test_changed_respects_max_items_and_the_worker_finishes(zot, call, capsys, monkeypatch):
    _clean()
    keys = [_with_pdf(zot, f"Paper {i}")[0] for i in range(3)]
    part = call("index_fulltext", changed=True, max_items=1)
    assert part["items_indexed"] == 1 and part["remaining"] == 2 and "next" in part
    from zotero_mcp import index_worker
    monkeypatch.setattr(index_worker.sys, "argv", ["graphist-zotero-index"])
    index_worker.main()
    out = json.loads(capsys.readouterr().out)
    assert out["items_indexed"] == 2 and out["remaining"] == 0 and _files() == sorted(keys)


def test_changed_validation(zot, call):
    assert "leave out item_keys" in call("index_fulltext", changed=True, item_keys=["ABCDEFGH"])["detail"]
    _clean()
    zot_empty = call("index_fulltext", changed=True)
    assert "snapshot is missing or empty" in zot_empty["detail"]
