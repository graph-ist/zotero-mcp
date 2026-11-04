"""Bibliography of a book or article, extracted from the text of its PDF.

Used by the save_references tool: the entries are written to KNOWLEDGE_DIR/references/KEY.json,
where the DOI MCP's literature maps read them as the work's reference list. This matters for
books and the humanities, which OpenAlex and Semantic Scholar mostly list without references.

Only the raw entries are extracted here (one string per reference, repeated-author dashes
replaced by the surname); resolving them to works is the DOI MCP's job.
"""
import json
import os
import re
import time

from . import config


def out_dir() -> str:
    return os.path.join(config.KNOWLEDGE_DIR, "references")

HEADING = re.compile(
    r"^\s*(references|bibliography|select(ed)? bibliography|works cited|literature( cited)?|sources"
    r"|bibliografia|riferimenti bibliografici|riferimenti|literatur(verzeichnis)?|bibliographie)\s*$", re.I)
END = re.compile(r"^\s*(index|name index|subject index|general index|indice( dei nomi| analitico)?|register"
                 r"|about the authors?|acknowledg(e)?ments)\s*$", re.I)
NAME = r"[A-ZÀ-Þ][\w'’\-À-ÿ]+"
START = re.compile(rf"^(?:{NAME}(?: {NAME}){{0,2}}, (?:[A-ZÀ-Þ][\w.\-À-ÿ]*|[A-Z]\.)|[A-ZÀ-Þ]{{2,}}, [A-Z]|[—–-]{{2,}}|_{{3,}})")
YEAR = re.compile(r"\b(1[5-9]\d\d|20[0-3]\d)[a-z]?\b")
DASHES = re.compile(r"^(?:[—–-]{2,}|_{3,})\.?,?\s*")


def find_bibliography(text: str) -> list[str] | None:
    """Lines of the bibliography: from the first matching heading in the second half of the text
    (running heads repeat it) to the index; without a heading, from the first dense run of
    entry-like lines in the last 45%. None when nothing looks like a bibliography."""
    lines = text.split("\n")
    n = len(lines)
    start = next((i + 1 for i, line in enumerate(lines) if i > 0.5 * n and HEADING.match(line)), None)
    if start is None:
        for i in range(int(0.55 * n), n):
            window = lines[i:i + 40]
            if sum(bool(START.match(x.strip())) for x in window) >= 12 and START.match(lines[i].strip()):
                start = i
                break
    if start is None:
        return None
    end = next((i for i in range(start + 5, n) if END.match(lines[i])), n)
    return lines[start:end]


def split_entries(lines: list[str]) -> list[str]:
    """One string per reference. A new entry starts with 'Surname, First' (or a dash for the same
    author) once the current one has a year; wrapped lines and hyphenated words are joined."""
    out, cur, last_author = [], "", ""
    for raw in lines:
        # OCR splits years ("1 967") and page numbers; join a split four-digit year
        line = re.sub(r"\b([12]) ?(\d) ?(\d) ?(\d)\b", r"\1\2\3\4", raw.strip())
        if not line or HEADING.match(line) or re.fullmatch(r"[\divxlc]+", line, re.I):
            continue
        if START.match(line) and (not cur or YEAR.search(cur)):
            if cur:
                out.append(cur)
            if DASHES.match(line):
                line = DASHES.sub(f"{last_author}, ", line, count=1) if last_author else line
            else:
                last_author = re.split(r",", line, maxsplit=1)[0].strip()
            cur = line
        elif cur:
            cur = cur[:-1] + line if cur.endswith("-") and not cur.endswith(" -") else f"{cur} {line}"
    if cur:
        out.append(cur)
    return [e for e in (re.sub(r"\s+", " ", x).strip() for x in out) if YEAR.search(e) and 25 < len(e) < 600]


def extract(text: str) -> list[str] | None:
    lines = find_bibliography(text)
    return None if lines is None else split_entries(lines)


def path(item_key: str) -> str:
    return os.path.join(out_dir(), f"{item_key}.json")


def load(item_key: str) -> dict | None:
    try:
        return json.load(open(path(item_key), encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save(item_key: str, attachment_key: str, entries: list[str] | None, chars: int) -> dict:
    """Write KEY.json readable by the group `knowledge`; entries None = no bibliography found."""
    data = {"item_key": item_key, "attachment_key": attachment_key,
            "extracted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "text_chars": chars, "found": entries is not None, "entries": entries or []}
    old = os.umask(0o027)
    try:
        os.makedirs(out_dir(), exist_ok=True)
        tmp = path(item_key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path(item_key))
    finally:
        os.umask(old)
    return data
