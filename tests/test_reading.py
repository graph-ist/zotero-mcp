"""get_outline, get_fulltext(pages=...) and extract_references on PDFs built in memory."""
import pymupdf

from conftest import article, put_webdav


def paper_pdf(with_toc: bool = False) -> bytes:
    doc = pymupdf.open()
    body = "Body text of the paper, long enough to be the dominant font size on the page. " * 3

    def page(lines):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 30), "Running head: Design epistemology", fontsize=9)  # must be ignored
        y = 100
        for text, size, bold in lines:
            for chunk in [text[i:i + 90] for i in range(0, len(text), 90)]:
                p.insert_text((72, y), chunk, fontsize=size, fontname="hebo" if bold else "helv")
                y += size + 4
            y += 6
        return p

    page([("Design Epistemology Revisited", 18, True), ("Abstract", 12, True), (body, 10, False),
          ("1. Introduction", 14, True), (body, 10, False), ("1.1 Background", 12, True), (body, 10, False)])
    page([("2. Method", 14, True), (body, 10, False), ("This sentence is bold but ends with a period.", 10, True),
          (body, 10, False)])
    page([("3. Conclusion", 14, True), (body, 10, False), ("References", 14, True),
          ("Cross, N. (1982). Designerly ways of knowing. Design Studies, 3(4), 221-227. https://doi.org/10.1016/0142-694X(82)90040-0", 10, False),
          ("Schön, D. A. (1983). The reflective practitioner. Basic Books.", 10, False),
          ("Simon, H. A. (1996). The sciences of the artificial (3rd ed.). MIT Press. doi:10.7551/mitpress/12107.001.0001", 10, False),
          ("Appendix A", 14, True), (body, 10, False)])
    if with_toc:
        doc.set_toc([[1, "Introduction", 1], [2, "Background", 1], [1, "Method", 2], [1, "Conclusion", 3], [1, "References", 3]])
    return doc.tobytes()


def test_outline_from_typography(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    put_webdav(att, "p.pdf", paper_pdf())
    out = call("get_outline", key=k)
    assert out["source"] == "typography" and out["pages"] == 3
    titles = [s["title"] for s in out["sections"]]
    assert titles == ["Design Epistemology Revisited", "Abstract", "1. Introduction", "1.1 Background",
                      "2. Method", "3. Conclusion", "References", "Appendix A"]
    by = {s["title"]: s for s in out["sections"]}
    assert by["1. Introduction"]["level"] == 2 and by["1.1 Background"]["level"] == 3
    assert by["Abstract"]["kind"] == "abstract" and by["References"]["kind"] == "references"
    assert by["2. Method"]["page"] == 2 and by["2. Method"]["page_end"] == 2 and by["1. Introduction"]["page_end"] == 1
    assert not any("Running head" in t or "ends with a period" in t for t in titles)


def test_outline_from_bookmarks_and_missing_file(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    assert "sync files" in call("get_outline", key=k)["detail"]
    put_webdav(att, "p.pdf", paper_pdf(with_toc=True))
    out = call("get_outline", key=att)
    assert out["source"] == "bookmarks" and [s["page"] for s in out["sections"]] == [1, 1, 2, 3, 3]
    assert out["sections"][0]["page_end"] == 1 and out["sections"][2]["kind"] == "method"


def test_fulltext_pages(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    zot.fulltext[att] = {"content": "index text"}  # ignored when pages is given
    put_webdav(att, "p.pdf", paper_pdf())
    out = call("get_fulltext", key=k, pages="2-3")
    assert out["source"] == "webdav_pdf_pages" and out["text"].startswith("[p. 2]\n") and "[p. 3]" in out["text"]
    assert "Introduction" not in out["text"] and "References" in out["text"]
    assert "out of range" in call("get_fulltext", key=k, pages="4")["detail"]
    assert "like '7'" in call("get_fulltext", key=k, pages="x")["detail"]
    assert call("get_fulltext", key=k)["source"] == "zotero_fulltext_index"


def test_extract_references(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    put_webdav(att, "p.pdf", paper_pdf())
    cross = article(zot, title="Designerly ways of knowing", doi="10.1016/0142-694X(82)90040-0")
    zot.crossref_query = [{"DOI": "10.5555/reflective", "score": 90, "author": [{"family": "Schön"}],
                           "title": ["The reflective practitioner: How professionals think in action"],
                           "issued": {"date-parts": [[1983]]}, "_match": ["reflective practitioner"]},
                          # same query hit, wrong work: a review carrying the title must not be accepted
                          {"DOI": "10.5555/review", "score": 95, "author": [{"family": "Other"}],
                           "title": ["Review of: The reflective practitioner"], "type": "journal-article",
                           "issued": {"date-parts": [[1984]]}, "_match": ["reflective practitioner"]}]
    out = call("extract_references", key=k)
    assert out["section_found"] and out["counts"] == {"entries": 3, "with_doi": 2, "in_library": 1, "missing_with_doi": 1}
    e = out["entries"]
    assert e[0]["doi"] == "10.1016/0142-694x(82)90040-0" and e[0]["in_library"]["key"] == cross
    assert e[0]["first_author"] == "Cross" and e[0]["year"] == "1982"
    assert "doi" not in e[1] and e[2]["doi"] == "10.7551/mitpress/12107.001.0001"
    assert out["missing_dois"] == ["10.7551/mitpress/12107.001.0001"]
    out = call("extract_references", key=k, resolve_missing=5)
    assert out["looked_up"] == 1 and out["entries"][1]["doi"] == "10.5555/reflective" and out["entries"][1]["doi_source"] == "crossref"
    assert out["entries"][1]["doi_title_score"] >= 0.88
    assert out["counts"]["with_doi"] == 3


def test_extract_references_needs_text(zot, call):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    put_webdav(att, "s.pdf", pymupdf.open().tobytes() if False else __import__("conftest").make_pdf(["", ""]))
    assert "ocr_pdf" in call("extract_references", key=k)["detail"]
