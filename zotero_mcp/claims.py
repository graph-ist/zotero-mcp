"""Evidence for claims in a draft: find the cited source in the library and the passages of its
full text that best match each claim, with the page. Used by the check_claims tool.

No judgement is made here: the passages go back to the model, which reads them and decides
whether they support the claim. Retrieval is lexical (BM25 over overlapping windows of each
page), so a claim written in another language than the source needs `keywords` in the source's
language.
"""
import math
import re
import unicodedata

STOP = set("""a an the of and or in on at to for from by with as is are was were be been being this that
these those it its which who whom whose what not no but if then than so such into about over under
between through also can could may might must should would will shall do does did has have had there
their they them he she his her we our you your i me my one two more most less other some any all
il lo la i gli le un uno una di da del della dei degli delle dello al alla ai agli alle nel nella nei
negli nelle sul sulla sui con per tra fra che chi cui non e ed o ma se come anche piu più è sono era
erano essere ha hanno questo questa questi queste quello quella suo sua suoi sue loro si ci ne
der die das und oder ein eine ist sind nicht mit von zu den dem des le les des et ou est sont une""".split())


NOT_NAMES = {"et", "al", "and", "und", "e", "y", "in", "see", "cf", "vgl", "ibid", "ed", "eds", "hrsg", "trans", "tr"}


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s.lower()) if not unicodedata.combining(c))


def tokens(text: str) -> list[str]:
    """Lowercase, accents folded, stopwords out, cut to 6 letters (a crude stem for en/it/de/fr)."""
    return [w[:6] for w in re.findall(r"[a-z0-9]+", _fold(text)) if len(w) > 2 and w not in STOP]


def windows(pages: list[tuple[str, str]], size: int = 120, step: int = 60) -> list[dict]:
    """Overlapping word windows of each page: {page label, n (1-based page index), text}."""
    out = []
    for n, (label, text) in enumerate(pages, 1):
        words = text.split()
        for i in range(0, max(1, len(words) - size // 2), step):
            chunk = " ".join(words[i:i + size])
            if len(chunk) > 80:
                out.append({"page": label, "n": n, "text": chunk})
    return out


def best_passages(claim: str, wins: list[dict], k: int = 3) -> list[dict]:
    """Top-k windows by BM25 against the claim; overlapping windows of one page count once."""
    q = set(tokens(claim))
    if not q or not wins:
        return []
    # windows dense with years are reference lists or indexes, not arguments
    wins = [w for w in wins if len(re.findall(r"\b(1[5-9]\d\d|20[0-3]\d)\b", w["text"])) < 5]
    if not wins:
        return []
    docs = [tokens(w["text"]) for w in wins]
    n, avg = len(docs), sum(map(len, docs)) / len(docs) or 1
    df = {t: sum(t in set(d) for d in docs) for t in q}
    scored = []
    for w, d in zip(wins, docs):
        tf = {t: d.count(t) for t in q}
        s = sum(math.log(1 + (n - df[t] + .5) / (df[t] + .5)) * tf[t] * 2.2 / (tf[t] + 1.2 * (.25 + .75 * len(d) / avg))
                for t in q if tf[t])
        if s > 0:
            scored.append((s, sum(1 for t in q if tf[t]) / len(q), w))
    scored.sort(key=lambda x: -x[0])
    out, pages = [], set()
    for s, cover, w in scored:
        if w["page"] in pages:
            continue
        pages.add(w["page"])
        out.append({"page": w["page"], "n": w["n"], "score": round(s, 2), "terms_matched": round(cover, 2), "text": w["text"]})
        if len(out) == k:
            break
    return out


def resolve_source(source: str, items: list[dict], title: str | None = None) -> tuple[list[dict], str]:
    """Library items a citation points to: Zotero key, DOI, or 'Surname year' / 'Surname (year)' /
    'Surname, title words'. Returns (items, how)."""
    src = source.strip()
    if re.fullmatch(r"[A-Z0-9]{8}", src):
        return [i for i in items if i["key"] == src], "key"
    doi = re.search(r"10\.\d{4,9}/\S+", src)
    if doi:
        d = doi.group(0).lower().rstrip(".,;)")
        return [i for i in items if (i.get("doi") or "").lower() == d or d in [x.lower() for x in i.get("extra_dois") or []]], "doi"
    year = re.search(r"\b(1[5-9]\d\d|20\d\d)[a-z]?\b", src)
    # capitalised words are surnames ("Or", "Le", "Da" are real ones): only connectors are dropped
    names = [w for w in re.findall(r"[^\W\d_][\w'’\-]+", src) if w[0].isupper() and _fold(w) not in NOT_NAMES]
    words = set(tokens(re.sub(r"\b(1[5-9]\d\d|20\d\d)[a-z]?\b", "", src)))
    if not names:
        return [], "unparsed"
    by_name = [it for it in items
               if _fold(names[0]) in {_fold(c.split(",")[0].split()[-1]) for c in it.get("creators") or [] if c.strip()}]
    how = "author-title"
    out = by_name
    if year:
        out = [it for it in by_name if it.get("year") and int(it["year"]) == int(year.group(1))]
        how = "author-year"
        if not out:  # cited in another edition or translation than the one in the library
            out, how = by_name, "author (year differs: other edition?)"
    if title:
        # the cited title decides: same author and year is often another paper
        want = set(tokens(title))
        fit = [i for i in out if want and len(want & set(tokens(i.get("title") or ""))) / len(want) >= 0.5]
        if not fit and out:
            return [], "author-year matches only other works: " + "; ".join((i.get("title") or "")[:60] for i in out[:3])
        return fit, how + "+title"
    if len(out) > 1 and words:
        # surname plus words of the title narrows editions and namesakes
        ranked = sorted(out, key=lambda i: -len(words & set(tokens(i.get("title") or ""))))
        top = len(words & set(tokens(ranked[0].get("title") or "")))
        if top:
            out = [i for i in ranked if len(words & set(tokens(i.get("title") or ""))) == top]
    return out, how


QUOTED = re.compile(r"[\"“«„]([^\"”»“]{4,200})[\"”»“]")


def quotes(claim: str) -> list[str]:
    """Phrases the draft puts in quotation marks: they must appear verbatim in the source."""
    return [q.strip() for q in QUOTED.findall(claim)]


def find_exact(pages: list[tuple[str, str]], phrase: str) -> list[dict]:
    """Pages where the phrase occurs, ignoring case, accents, punctuation and line breaks
    (hyphenation at line ends is joined)."""
    want = " ".join(re.findall(r"[a-z0-9]+", _fold(phrase)))
    out = []
    for n, (label, text) in enumerate(pages, 1):
        flat = " ".join(re.findall(r"[a-z0-9]+", _fold(re.sub(r"-\s*\n\s*", "", text))))
        if want and want in flat:
            out.append({"page": label, "n": n})
    return out
