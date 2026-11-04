from zotero_mcp import bibliography as r

TEXT = "\n".join(["body text line"] * 200 + [
    "References",
    "Adams, Carol. 1990. The Sexual Politics of Meat. New York:",
    "Continuum.",
    "———. 2004. The Pornography of Meat. New York: Contin-",
    "uum.",
    "Bowker, Geoffrey C. 1 993. \"How to be Universal.\" Social Studies of Science 23.",
    "238",
    "Index",
    "Adams, 12"])


def test_extract_joins_lines_dashes_and_split_years():
    e = r.extract(TEXT)
    assert e == ["Adams, Carol. 1990. The Sexual Politics of Meat. New York: Continuum.",
                 "Adams, 2004. The Pornography of Meat. New York: Continuum.",
                 "Bowker, Geoffrey C. 1993. \"How to be Universal.\" Social Studies of Science 23."]


def test_no_bibliography():
    assert r.extract("\n".join(["just prose"] * 300)) is None


def test_comma_style_without_heading():
    refs = [f"Author{i}, Name, A Title Number {i}, New York: Press, 19{50 + i}." for i in range(20)]
    e = r.extract("\n".join(["prose"] * 100 + refs))
    assert len(e) == 20 and e[0].startswith("Author0, Name, A Title")
