"""Offline tests of claim evidence retrieval and source resolution."""
from zotero_mcp import claims as c

ITEMS = [{"key": "AAAA1111", "title": "We have never been modern", "creators": ["Latour"], "year": 1993, "doi": None},
         {"key": "BBBB2222", "title": "Non siamo mai stati moderni", "creators": ["Latour"], "year": 1995, "doi": None},
         {"key": "CCCC3333", "title": "Sorting things out", "creators": ["Bowker", "Star"], "year": 1999, "doi": "10.7551/x"}]


def test_resolve():
    assert [i["key"] for i in c.resolve_source("Latour 1993", ITEMS)[0]] == ["AAAA1111"]
    assert c.resolve_source("Latour (2001)", ITEMS)[1].startswith("author (year differs")
    assert [i["key"] for i in c.resolve_source("Latour, Non siamo mai stati moderni", ITEMS)[0]] == ["BBBB2222"]
    assert [i["key"] for i in c.resolve_source("doi:10.7551/X", ITEMS)[0]] == ["CCCC3333"]
    assert c.resolve_source("Bowker & Star 1999", ITEMS)[0][0]["key"] == "CCCC3333"
    assert c.resolve_source("CCCC3333", ITEMS)[0][0]["key"] == "CCCC3333"



def test_short_surname_and_title():
    items = ITEMS + [{"key": "DDDD4444", "title": "Stay cool! thermal attacks", "creators": ["Abdelrahman"], "year": 2017},
                     {"key": "EEEE5555", "title": "Development of a facial skin temperature methodology", "creators": ["Or", "Duffy"], "year": 2007}]
    assert c.resolve_source("Or & Duffy 2007", items)[0][0]["key"] == "EEEE5555"
    found, how = c.resolve_source("Abdelrahman 2017", items, title="Cognitive heat: thermal imaging to estimate cognitive load")
    assert found == [] and how.startswith("author-year matches only other works")
    assert c.resolve_source("Abdelrahman 2017", items, title="Stay Cool! Understanding thermal attacks")[0][0]["key"] == "DDDD4444"


def test_passages():
    pages = [("1", "filler words about nothing " * 40),
             ("2", "the moderns proliferate hybrids while purification denies them " + "filler text " * 50),
             ("3", " ".join(f"Smith {1990 + i}. Title." for i in range(30)))]
    best = c.best_passages("moderns proliferate hybrids purification", c.windows(pages))
    assert best[0]["page"] == "2" and best[0]["n"] == 2 and best[0]["terms_matched"] == 1.0
    assert all(p["page"] != "3" for p in best)



def test_quotes():
    claim = 'What Disalvo calls “productive friction”, and «Urbanität braucht Risiko»'
    assert c.quotes(claim) == ["productive friction", "Urbanität braucht Risiko"]
    pages = [("1", "nothing"), ("37", "Urbanität braucht heute meist besondere An-\nlässe"), ("40", "productive\nfriction, yes")]
    assert c.find_exact(pages, "urbanitat braucht heute meist besondere Anlässe") == [{"page": "37", "n": 2}]
    assert c.find_exact(pages, "Productive friction") == [{"page": "40", "n": 3}]
    assert c.find_exact(pages, "Urbanität braucht Risiko") == []
