"""Bibliographic matching: the one place where "is this record the work that was cited?" is
decided. Standard library only, so other services can vendor the file unchanged.

Used by find_doi, verify_doi, patent_get (literature cited by patents) and the literature
maps here, and by the Zotero MCP (create_item_from_doi, extract_references), which keeps a
copy as zotero_mcp/matching.py. Keep the two files identical: the acceptance rules are
measured against tests/fixtures/find_doi_cases.json in this repository, and a second
implementation with its own thresholds would give different answers on the same cases.

Vocabulary: a *candidate* is a dict with title, optional subtitle, authors (list of names in
any format), year and optional type (Crossref/OpenAlex work type). `assess` scores it against
the cited title, first author surname and year; `eligible` applies the acceptance rules.
"""
import html
import re
import unicodedata
from difflib import SequenceMatcher

MATCHING_VERSION = 1

TITLE_THRESHOLD = 0.88
# Titles shorter than this identify little on their own, so a match also needs the author
SHORT_TITLE_TOKENS = 4
# Crossref/OpenAlex types that can share a title with a work without being that work
NON_WORK_TYPES = {"peer-review", "component", "grant", "erratum", "retraction", "paratext"}
DERIVATIVE_RE = re.compile(
    r"^\s*(?:correction|corrigendum|erratum|addendum|retraction(?:\s+(?:note|notice))?|"
    r"notice of retraction|expression of concern|decision letter|author response|"
    r"reply|response|comment|review)(?:\s+(?:to|for|on|of))?\s*[:\"'“‘«]", re.I)
RETRACTED_PREFIX_RE = re.compile(r"^\s*(?:retracted|withdrawn)(?:\s+article)?\s*:\s*", re.I)
NAME_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "le",
                  "la", "dos", "das", "ten", "ter", "zu", "al", "el", "bin", "ibn"}


def strip_html(s: str | None) -> str | None:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s or "")).split()) or None


def norm(s: str | None) -> str:
    # Drop accents but keep non-Latin letters, so Cyrillic or CJK titles don't normalize to ""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[\W_]+", " ", s.casefold()).split())


def clean_title(s: str | None) -> str:
    return RETRACTED_PREFIX_RE.sub("", strip_html(s) or "")


def title_sim(cited: str, title: str | None, subtitle: str | None = None) -> tuple[float, int]:
    """Best similarity over subtitle variants, and the token length of the shorter compared form."""
    c, t = clean_title(cited), clean_title(title)
    pairs = [(c, t), (c, t.split(":")[0]), (c.split(":")[0], t)]
    if subtitle:
        pairs.append((c, f"{t} {clean_title(subtitle)}"))
    best = (0.0, 0)
    for a, b in pairs:
        a, b = norm(a), norm(b)
        s = SequenceMatcher(None, a, b).ratio() if a and b else 0.0
        if s > best[0]:
            best = (s, min(len(a.split()), len(b.split())))
    return round(best[0], 3), best[1]


def name_tokens(name: str) -> set[str]:
    return {t for t in norm(name).split() if len(t) > 1}


def surname(author: str) -> set[str]:
    """Surname tokens of a cited author: 'Fox, M.' and 'Mark Fox' both give {'fox'}."""
    part = author.split(",")[0] if "," in author else (author.split() or [""])[-1]
    return name_tokens(part) - NAME_PARTICLES


def surname_query(author: str) -> str:
    return max(surname(author), key=len, default="")


def author_ok(author: str | None, authors: list[str]) -> bool | None:
    """Whether the cited surname is among the record's name tokens; None if undecidable."""
    want = surname(author) if author else set()
    if not want or not authors:
        return None
    return bool(want & set().union(*map(name_tokens, authors)))


def assess(cand: dict, title: str, author: str | None, year: int | None) -> dict:
    score, tokens = title_sim(title, cand["title"], cand.get("subtitle"))
    year_ok = abs(year - int(cand["year"])) <= 1 if year and cand.get("year") else None
    return {"score": score, "title_tokens": tokens, "year_ok": year_ok,
            "author_ok": author_ok(author, cand["authors"])}


def eligible(m: dict) -> bool:
    if m["score"] < TITLE_THRESHOLD or m["author_ok"] is False or m["year_ok"] is False:
        return False
    return m["title_tokens"] >= SHORT_TITLE_TOKENS or m["author_ok"] is True


def is_derivative(cand: dict, cited: str) -> bool:
    """Errata, retraction notices, peer reviews and replies that carry the work's title."""
    if cand.get("type") in NON_WORK_TYPES:
        return True
    return bool(DERIVATIVE_RE.match(cand["title"] or "")) and not DERIVATIVE_RE.match(cited)


# --- bibliography entries (one string per reference, as read from a PDF or a search report) ---

REF_STOP = {"the", "and", "of", "a", "an", "in", "on", "for", "to", "its", "la", "le", "les", "di", "il",
            "der", "die", "das", "und", "des", "du", "de", "et", "e"}
REF_YEAR = re.compile(r"\b(1[5-9]\d\d|20[0-3]\d)[a-z]?\b")
_YEAR = r"(1[5-9]\d\d|20[0-3]\d)"
# APA: 'Surname, D. A. (1983). Title' (initials before the year), also '(1983a)' and '(1983, May)'
_APA_RE = re.compile(rf"^[^()]*?\(\s*{_YEAR}[a-z]?(?:,[^)]*)?\)[.,:]?\s*(.+)$")
# Chicago author-date: 'Surname, First. 1983. Title'
_CHICAGO_RE = re.compile(rf"^[^.]*?\.?\s*{_YEAR}[a-z]?[.,]\s*(.+)$")
_TITLE_END_RE = re.compile(r"(?<!\b[A-Z])\.\s|[“”\"]|, (?:in|In)\b| In: |, [A-Z][^\W\d_]*(?: [A-Z][^\W\d_]*)?(?:, [A-Z]{2})?: ")


def ascii_fold(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


def title_words(s: str | None) -> list[str]:
    return [w for w in ascii_fold(s).split() if w not in REF_STOP and len(w) > 1]


def parse_ref(entry: str) -> tuple[str, int | None, str]:
    """(first author's surname, year, title) of a bibliography entry, APA
    ('Surname, D. A. (1983). Title. Publisher.'), author-date
    ('Surname, First. 1990. Title. Place: Publisher.') or comma style
    ('Surname, First, Title, Place: Publisher, 1990.')."""
    surname_ = re.split(r"[,.(]", entry, maxsplit=1)[0].strip()
    m = _APA_RE.match(entry) or _CHICAGO_RE.match(entry)
    if m:
        year, rest = int(m.group(1)), m.group(2)
    else:
        years = REF_YEAR.findall(entry)
        year = int(years[-1][:4]) if years else None
        rest = re.sub(r"^[^,]+,\s*[^,]+?,\s*", "", entry, count=1)
    rest = rest.strip(' "“”‘’\'')
    title = _TITLE_END_RE.split(rest)[0]
    return surname_, year, title.strip(' ,."“”')


def ref_key(surname_: str, title: str) -> str | None:
    words, last = title_words(title), ascii_fold(surname_).split()
    return f"{last[-1]}|{' '.join(words[:10])}" if words and last else None


def title_match(want: list[str], title: str | None) -> bool:
    got, w = set(title_words(title)), set(want)
    return bool(got) and len(w & got) / len(got) >= 0.8 and len(w & got) / len(w) >= 0.6
