"""Offline tests of the DOI checks in create_item_from_doi and of preprint pairing."""
import asyncio

from zotero_mcp import core
from zotero_mcp import items as s


class _Resp:
    def __init__(self, retracted):
        self.status_code, self._r = 200, retracted

    def json(self):
        return {"is_retracted": self._r}


class _Client:
    def __init__(self, retracted=False):
        self.r = retracted

    async def get(self, *a, **k):
        return _Resp(self.r)


REC = {"title": ["Attention is all you need"], "author": [{"family": "Vaswani", "given": "A."}],
       "issued": {"date-parts": [[2017, 6]]}, "type": "journal-article"}


def check(m, retracted=False, **exp):
    core._pub = _Client(retracted)
    title = m["title"][0]
    return asyncio.run(s._check_doi_record(m, "10.1/x", title, exp.get("t"), exp.get("a"), exp.get("y")))


def test_match_passes():
    p, w = check(REC, t="Attention Is All You Need", a="Ashish Vaswani", y=2018)
    assert p == [] and w == []


def test_mismatch_blocks():
    p, _ = check(REC, t="Deep residual learning for image recognition", a="He, Kaiming", y=2015)
    assert len(p) == 3 and p[0].startswith("title mismatch")


def test_retraction():
    assert check(REC, retracted=True)[0] == ["the work is retracted"]
    m = dict(REC, **{"updated-by": [{"type": "retraction", "DOI": "10.1/notice"}]})
    assert check(m)[0] == ["the work is retracted"]


def it(key, title, names, year, **kw):
    return {"key": key, "data": {"title": title, "date": str(year),
                                 "creators": [{"lastName": n} for n in names], **kw}}


def test_preprint_pairs():
    items = [it("A", "Large language models as simulated participants: a review", ["Rossi", "Bianchi"], 2023,
                itemType="preprint", DOI="10.48550/arXiv.2301.1"),
             it("B", "Large language models as simulated participants", ["Rossi", "Bianchi", "Verdi"], 2025,
                itemType="journalArticle", DOI="10.1000/j.1"),
             it("C", "Large language models as simulated participants", ["Neri"], 2024, itemType="journalArticle"),
             it("D", "Something else entirely about memory", ["Rossi", "Bianchi"], 2023, itemType="journalArticle")]
    assert [(a["key"], b["key"]) for a, b in s._preprint_pairs(items)] == [("A", "B")]
