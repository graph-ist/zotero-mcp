"""Books by ISBN, CSL JSON and BibTeX import, the BibTeX reader and the ISBN helpers."""
import json

from zotero_mcp import api, bibtex
from conftest import article

SIMON = "9780262691918"       # Simon, The Sciences of the Artificial, 3rd ed. (ISBN-10 0-262-69191-4)
HABERMAS = "9783518284919"    # Habermas, Strukturwandel der Öffentlichkeit, stw 891 (3-518-28491-6)

OL_SIMON = {"title": "The sciences of the artificial", "authors": [{"name": "Herbert A. Simon"}],
            "publishers": [{"name": "MIT Press"}], "publish_places": [{"name": "Cambridge, Mass"}],
            "publish_date": "1996", "number_of_pages": 231}

MARC_HABERMAS = (
    '<record xmlns="http://www.loc.gov/MARC21/slim" type="Bibliographic">'
    '<datafield tag="020" ind1=" " ind2=" "><subfield code="a">3518284916</subfield></datafield>'
    '<datafield tag="041" ind1=" " ind2=" "><subfield code="a">ger</subfield></datafield>'
    '<datafield tag="100" ind1="1" ind2=" "><subfield code="a">Habermas, Jürgen</subfield>'
    '<subfield code="4">aut</subfield></datafield>'
    '<datafield tag="245" ind1="1" ind2="0"><subfield code="a">Strukturwandel der Öffentlichkeit :</subfield>'
    '<subfield code="b">Untersuchungen zu einer Kategorie der bürgerlichen Gesellschaft /</subfield>'
    '<subfield code="c">Jürgen Habermas</subfield></datafield>'
    '<datafield tag="250" ind1=" " ind2=" "><subfield code="a">1. Aufl.</subfield></datafield>'
    '<datafield tag="264" ind1=" " ind2="1"><subfield code="a">Frankfurt am Main :</subfield>'
    '<subfield code="b">Suhrkamp,</subfield><subfield code="c">[1990]</subfield></datafield>'
    '<datafield tag="300" ind1=" " ind2=" "><subfield code="a">391 S.</subfield></datafield>'
    '<datafield tag="700" ind1="1" ind2=" "><subfield code="a">Muster, Max</subfield>'
    '<subfield code="4">edt</subfield></datafield>'
    '</record>')


# ---- ISBN helpers ------------------------------------------------------------------------------

def test_norm_isbn_converts_and_validates():
    assert api.norm_isbn("0-262-69191-4") == api.norm_isbn("978-0-262-69191-8") == SIMON
    assert api.norm_isbn("026269191X") is None and api.norm_isbn("9780262691919") is None
    assert api.norm_isbn("12345") is None
    assert api.item_isbns({"ISBN": "978-0-262-69191-8 0262691914, 3-518-28491-6"}) == [SIMON, HABERMAS]
    assert api.item_isbns({"ISBN": "", "extra": "ISBN: 3518284916"}) == [HABERMAS]
    assert api.isbn10(HABERMAS) == "3518284916" and api.isbn10(SIMON) == "0262691914"
    assert api.isbn10("9791234567896") is None


# ---- create_item_from_isbn ---------------------------------------------------------------------

def test_isbn_from_open_library_with_verification(zot, call):
    zot.openlibrary[SIMON] = OL_SIMON
    out = call("create_item_from_isbn", isbn="0-262-69191-4", tags=["design"], expected_title="The Sciences of the Artificial",
               expected_first_author="Simon", expected_year=1996)
    assert out["status"] == "ok" and out["source"] == "Open Library" and out["verified"] is True
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["itemType"] == "book" and d["ISBN"] == SIMON and d["publisher"] == "MIT Press"
    assert d["place"] == "Cambridge, Mass" and d["numPages"] == "231" and d["libraryCatalog"] == "Open Library"
    assert d["creators"] == [{"creatorType": "author", "lastName": "Simon", "firstName": "Herbert A."}]
    assert d["extra"].startswith("Verified: ") and "create_item_from_isbn open_library" in d["extra"]
    dup = call("create_item_from_isbn", isbn=SIMON)
    assert dup["status"] == "duplicate" and dup["existing"]["key"] == out["created_keys"][0]


def test_isbn_mismatch_is_not_created_and_dry_run_creates_nothing(zot, call):
    zot.openlibrary[SIMON] = OL_SIMON
    out = call("create_item_from_isbn", isbn=SIMON, expected_title="Administrative Behavior", expected_year=1947)
    assert out["status"] == "not_created" and len(out["problems"]) == 2 and not zot.items
    assert "another edition" in out["problems"][1]
    dry = call("create_item_from_isbn", isbn=SIMON, dry_run=True)
    assert dry["status"] == "dry_run" and dry["record"]["publisher"] == "MIT Press" and not zot.items
    forced = call("create_item_from_isbn", isbn=SIMON, expected_title="Administrative Behavior", force=True)
    assert forced["status"] == "ok" and forced["verified"] is False and forced["problems_overridden"]


def test_isbn_falls_back_to_google_books_and_checks_the_identifier(zot, call):
    zot.public_status["openlibrary.org"] = 503
    zot.googlebooks[SIMON] = [
        {"title": "Some other book", "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9783518284919"}]},
        {"title": "The Sciences of the Artificial", "subtitle": "Third edition", "authors": ["Herbert A. Simon"],
         "publisher": "MIT Press", "publishedDate": "1996-09-26", "pageCount": 248, "language": "en",
         "description": "Classic.", "industryIdentifiers": [{"type": "ISBN_10", "identifier": "0262691914"}]}]
    out = call("create_item_from_isbn", isbn=SIMON)
    assert out["status"] == "ok" and out["source"] == "Google Books"
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["title"] == "The Sciences of the Artificial: Third edition" and d["date"] == "1996-09-26"
    assert d["abstractNote"] == "Classic." and d["language"] == "en"


def test_isbn_german_group_asks_dnb_first_and_reads_marc(zot, call):
    zot.openlibrary[HABERMAS] = {"title": "Strukturwandel der Offentlichkeit", "authors": [{"name": "J. Habermas"}]}
    zot.dnb[HABERMAS] = MARC_HABERMAS
    out = call("create_item_from_isbn", isbn="3-518-28491-6", expected_first_author="Habermas", expected_year=1990,
               expected_title="Strukturwandel der Öffentlichkeit")
    assert out["status"] == "ok" and out["source"] == "DNB" and out["verified"] is True
    assert out["catalogues"] == {"DNB": "used"}
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["title"] == "Strukturwandel der Öffentlichkeit: Untersuchungen zu einer Kategorie der bürgerlichen Gesellschaft"
    assert (d["publisher"], d["place"], d["date"], d["edition"], d["numPages"], d["language"]) == \
        ("Suhrkamp", "Frankfurt am Main", "1990", "1. Aufl.", "391", "ger")
    assert d["creators"] == [{"creatorType": "author", "lastName": "Habermas", "firstName": "Jürgen"},
                             {"creatorType": "editor", "lastName": "Muster", "firstName": "Max"}]


def test_isbn_dnb_isbn10_fallback_and_reported_outcomes(zot, call):
    zot.dnb["3518284916"] = MARC_HABERMAS  # the catalogue knows the book by its printed ISBN-10 only
    out = call("create_item_from_isbn", isbn=HABERMAS, dry_run=True)
    assert out["record"]["source"] == "DNB" and out["catalogues"] == {"DNB": "used"}
    del zot.dnb["3518284916"]
    zot.dnb_diagnostic = "Unsupported index: num"
    zot.openlibrary[HABERMAS] = {"title": "Strukturwandel der Offentlichkeit", "authors": [{"name": "Jürgen Habermas"}]}
    fell_back = call("create_item_from_isbn", isbn=HABERMAS, dry_run=True)
    assert fell_back["record"]["source"] == "Open Library"
    assert fell_back["catalogues"] == {"DNB": "SRU diagnostic: info:srw/diagnostic/1/10 Unsupported index: num",
                                       "Open Library": "used"}
    zot.openlibrary[SIMON] = OL_SIMON
    zot.public_status["openlibrary.org"] = 503
    zot.googlebooks[SIMON] = [{"title": "The Sciences of the Artificial", "authors": ["Herbert A. Simon"],
                               "industryIdentifiers": [{"type": "ISBN_13", "identifier": SIMON}]}]
    ok = call("create_item_from_isbn", isbn=SIMON)
    assert ok["catalogues"] == {"Open Library": "HTTP 503", "Google Books": "used"}


def test_isbn_errors(zot, call):
    assert "not a valid ISBN" in call("create_item_from_isbn", isbn="978-0-262-69191-9")["detail"]
    zot.public_status["www.googleapis.com"] = 429
    out = call("create_item_from_isbn", isbn=SIMON)
    assert "Open Library: not found" in out["detail"] and "Google Books: HTTP 429" in out["detail"] and "DNB: not found" in out["detail"]


def test_isbn_index_ignores_chapters(zot, call):
    zot.add_item("bookSection", title="A chapter", ISBN="0-262-69191-4")
    zot.openlibrary[SIMON] = OL_SIMON
    assert call("create_item_from_isbn", isbn=SIMON)["status"] == "ok"


# ---- BibTeX reader -----------------------------------------------------------------------------

def test_bibtex_parse_macros_concat_quotes_and_errors():
    text = r"""
    @string{ds = "Design Studies"}
    @comment{ignored {nested} text}
    @Article{cross82,
      author = {Cross, Nigel and van der Berg, Jan and {Design Research Society}},
      title = "Designerly {W}ays of {K}nowing",
      journal = ds # ", Special",
      year = 1982, month = oct,
      pages = {221--227},
    }
    @book{broken, title = {unclosed
    @book(schoen83, author = "Donald A. Sch{\"o}n", title = {The Reflective Practitioner}, year = {1983})
    """
    entries, errors = bibtex.parse(text)
    assert [e["key"] for e in entries] == ["cross82", "schoen83"]
    f = entries[0]["fields"]
    assert f["journal"] == "Design Studies, Special" and f["month"] == "10" and f["year"] == "1982"
    assert bibtex.delatex(f["title"]) == "Designerly Ways of Knowing"
    assert len(errors) == 1 and "@book" in errors[0]
    assert bibtex.names(f["author"], "author") == [
        {"creatorType": "author", "lastName": "Cross", "firstName": "Nigel"},
        {"creatorType": "author", "lastName": "van der Berg", "firstName": "Jan"},
        {"creatorType": "author", "name": "Design Research Society"}]
    assert bibtex.names(entries[1]["fields"]["author"], "author") == [
        {"creatorType": "author", "lastName": "Schön", "firstName": "Donald A."}]


def test_delatex():
    d = bibtex.delatex
    assert d(r"G{\"o}del, {\'E}cole, \c{c}a, \v{S}koda, Ma\l{}gorzata, Stra{\ss}e, \'{\i}") == \
        "Gödel, École, ça, Škoda, Małgorzata, Straße, í"
    assert d(r"\emph{Being and \textbf{Time}} --- 1927--1928 \& more~text") == "Being and Time — 1927–1928 & more text"
    assert d(r"\url{https://x.org/a_b} ``quoted''") == "https://x.org/a_b “quoted”"
    assert d(r"von {M}ises \unknowncommand{} kept") == "von Mises kept"


def test_bibtex_names_forms():
    n = bibtex.names(r"Last, Jr, First and Ludwig van Beethoven and Plato and others", "editor")
    assert n == [{"creatorType": "editor", "lastName": "Last", "firstName": "First, Jr"},
                 {"creatorType": "editor", "lastName": "van Beethoven", "firstName": "Ludwig"},
                 {"creatorType": "editor", "name": "Plato"}]


# ---- import_items ------------------------------------------------------------------------------

BIB = r"""
@article{cross1982,
  author = {Cross, Nigel}, title = {Designerly Ways of Knowing}, journaltitle = {Design Studies},
  volume = {3}, number = {4}, pages = {221--227}, date = {1982-10}, doi = {https://doi.org/10.1016/0142-694X(82)90040-0},
  keywords = {design}, mendeley-tags = {x}, foo = {bar}
}
@incollection{dorst2011,
  author = {Dorst, Kees}, editor = {Cross, Nigel}, title = {The Core of Design Thinking},
  booktitle = {Design Thinking Reader}, publisher = {Springer}, address = {Berlin}, year = {2011}, pages = {1-10}
}
@phdthesis{doe2020, author = {Doe, Jane}, title = {A Thesis on Things}, school = {TU Wien}, year = 2020}
@misc{arx, author = {Roe, R.}, title = {A Preprint Here}, eprint = {2401.00001}, archiveprefix = {arXiv}, year = 2024}
@misc{vague, author = {Roe, R.}, title = {Something Unclear}, year = 2024}
@patent{pat, title = {A Patent}, year = 2020}
"""


def test_import_bibtex_maps_types_and_fields(zot, call):
    c = zot.add_collection("Import")
    out = call("import_items", text=BIB, collection_keys=[c], tags=["imported"])
    assert out["status"] == "partial" and out["format"] == "bibtex"
    by = {e["record"]: e for e in out["created"]}
    assert set(by) == {"cross1982", "dorst2011", "doe2020", "arx"}
    assert {r["record"] for r in out["rejected"]} == {"vague", "pat"}
    assert "create_item_from_patent" in next(r["reason"] for r in out["rejected"] if r["record"] == "pat")
    a = zot.items[by["cross1982"]["key"]]["data"]
    assert (a["itemType"], a["publicationTitle"], a["issue"], a["pages"], a["date"], a["DOI"]) == \
        ("journalArticle", "Design Studies", "4", "221-227", "1982-10", "10.1016/0142-694x(82)90040-0")
    assert a["extra"] == "Citation Key: cross1982" and a["collections"] == [c] and a["tags"] == [{"tag": "imported"}]
    assert by["cross1982"]["unmapped"] == ["foo"]
    b = zot.items[by["dorst2011"]["key"]]["data"]
    assert (b["itemType"], b["bookTitle"], b["place"], b["date"]) == ("bookSection", "Design Thinking Reader", "Berlin", "2011")
    assert [x["creatorType"] for x in b["creators"]] == ["author", "editor"]
    t = zot.items[by["doe2020"]["key"]]["data"]
    assert (t["itemType"], t["university"], t["thesisType"]) == ("thesis", "TU Wien", "PhD thesis")
    p = zot.items[by["arx"]["key"]]["data"]
    assert (p["itemType"], p["repository"], p["archiveID"]) == ("preprint", "arXiv", "arXiv:2401.00001")


def test_import_detects_duplicates_by_doi_isbn_title_and_within_the_batch(zot, call):
    k_doi = article(zot, title="Something else entirely", doi="10.1016/0142-694x(82)90040-0")
    k_book = zot.add_item("book", title="The Sciences of the Artificial", ISBN="0262691914",
                          creators=[{"creatorType": "author", "lastName": "Simon", "firstName": "Herbert"}], date="1996")
    k_title = zot.add_item("book", title="The Reflective Practitioner: How Professionals Think in Action", date="1983",
                           creators=[{"creatorType": "author", "lastName": "Schön", "firstName": "Donald"}])
    csl = [{"type": "article-journal", "title": "Designerly ways of knowing", "DOI": "10.1016/0142-694X(82)90040-0",
            "author": [{"family": "Cross", "given": "Nigel"}], "issued": {"date-parts": [[1982]]}},
           {"type": "book", "title": "Sciences of the artificial, the", "ISBN": ["978-0-262-69191-8"],
            "author": [{"family": "Simon", "given": "H."}], "issued": {"date-parts": [[1996]]}},
           {"type": "book", "title": "The reflective practitioner", "subtitle": ["How professionals think in action"],
            "author": [{"family": "Schön", "given": "D. A."}], "issued": {"date-parts": [[1983]]}},
           {"type": "book", "title": "New Book Here", "author": [{"family": "Neu"}], "issued": {"date-parts": [[2020]]}},
           {"type": "book", "title": "New Book Here", "author": [{"family": "Neu"}], "issued": {"date-parts": [[2020]]}}]
    before = len(zot.items)
    dry = call("import_items", text=json.dumps(csl), dry_run=True)
    assert dry["status"] == "dry_run" and len(zot.items) == before
    assert {e["existing"]["key"] for e in dry["existing"]} == {k_doi, k_book, k_title}
    assert [e["title"] for e in dry["would_create"]] == ["New Book Here"]
    assert "same work as" in dry["rejected"][0]["reason"]
    c = zot.add_collection("Reading")
    out = call("import_items", text=json.dumps(csl), if_exists="file", collection_keys=[c])
    assert len(out["created"]) == 1 and len(zot.items) == before + 1
    assert all(e["filed"]["collections_added"] == [c] for e in out["existing"])
    assert c in zot.items[k_title]["data"]["collections"]


def test_import_csl_json_fields_names_and_unsupported(zot, call):
    rec = {"items": [
        {"type": "paper-conference", "title": "Research through Design", "container-title": "Proc. CHI",
         "event-title": "CHI 2007", "publisher": "ACM", "page": "493-502", "citation-key": "zimmerman07",
         "author": [{"family": "Zimmerman", "given": "John"}, {"family": "Forlizzi", "given": "Jodi", "suffix": "Jr."},
                    {"family": "Berg", "non-dropping-particle": "van den", "given": "A."}, {"literal": "ACM SIGCHI"}],
         "issued": {"date-parts": [[2007, 4, 28]]}, "abstract": "<p>Abs</p>", "custom": 1},
        {"type": "song", "title": "Not a paper"}]}
    out = call("import_items", text=json.dumps(rec))
    assert out["status"] == "partial" and out["format"] == "csljson"
    assert out["rejected"] == [{"record": "Not a paper", "reason": "unsupported CSL type 'song'"}]
    assert out["created"][0]["unmapped"] == ["custom"]
    d = zot.items[out["created"][0]["key"]]["data"]
    assert (d["itemType"], d["proceedingsTitle"], d["conferenceName"], d["date"], d["abstractNote"]) == \
        ("conferencePaper", "Proc. CHI", "CHI 2007", "2007-04-28", "Abs")
    assert d["extra"] == "Citation Key: zimmerman07"
    assert d["creators"][1] == {"creatorType": "author", "lastName": "Forlizzi", "firstName": "Jodi, Jr."}
    assert d["creators"][2]["lastName"] == "van den Berg" and d["creators"][3] == {"creatorType": "author", "name": "ACM SIGCHI"}


def test_import_errors(zot, call):
    assert "cannot tell the format" in call("import_items", text="just words")["detail"]
    assert "not valid JSON" in call("import_items", text="[{bad", format="csljson")["detail"]
    assert "no records" in call("import_items", text="@comment{x}", format="bibtex")["detail"]
    many = "\n".join(f"@book{{k{i}, title = {{Book number {i}}}, year = 2000}}" for i in range(101))
    assert "at most 100" in call("import_items", text=many)["detail"]
