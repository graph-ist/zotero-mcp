"""BibTeX and BibLaTeX reading, standard library only: entries with their fields, @string macros
and # concatenation, LaTeX accents and commands turned into Unicode, name lists split into
creators. The mapping to Zotero item types is in imports.py."""
import re
import unicodedata

MONTHS = {m: f"{i:02d}" for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


class BibError(ValueError):
    pass


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Entries as {"type", "key", "fields": {name: value}} with values already de-LaTeXed
    (names lists are left as strings: see names()), and a list of errors for entries that
    could not be read; a broken entry does not stop the ones after it."""
    macros = dict(MONTHS)
    entries, errors = [], []
    i = 0
    while True:
        i = text.find("@", i)
        if i < 0:
            break
        m = re.compile(r"@\s*([A-Za-z]+)\s*([{(])").match(text, i)
        if not m:
            i += 1
            continue
        kind, close = m.group(1).lower(), "}" if m.group(2) == "{" else ")"
        start = m.end()
        try:
            if kind in ("comment", "preamble"):
                i = _skip_group(text, m.end() - 1)
                continue
            if kind == "string":
                fields, i = _fields(text, start, close, macros)
                macros.update({k.lower(): v for k, v in fields.items()})
                continue
            km = re.compile(r"\s*([^\s,{}()=\"#%]+)\s*,").match(text, start)
            if not km:
                raise BibError("missing citation key")
            fields, i = _fields(text, km.end(), close, macros)
            entries.append({"type": kind, "key": km.group(1),
                            "fields": {k.lower(): v for k, v in fields.items()}})
        except BibError as e:
            line = text.count("\n", 0, m.start()) + 1
            errors.append(f"line {line}, @{kind}: {e}")
            i = m.end()
    return entries, errors


def _skip_group(text: str, i: int) -> int:
    """Index after the bracket group that opens at text[i]."""
    open_, close = text[i], "}" if text[i] == "{" else ")"
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_:
            depth += 1
        elif text[j] == close:
            depth -= 1
            if depth == 0:
                return j + 1
    raise BibError("unbalanced brackets")


_NAME = re.compile(r"\s*([A-Za-z][\w:.+/-]*)\s*=\s*")


def _fields(text: str, i: int, close: str, macros: dict) -> tuple[dict, int]:
    out: dict[str, str] = {}
    while True:
        while i < len(text) and text[i] in " \t\r\n,":
            i += 1
        if i >= len(text):
            raise BibError("entry not closed")
        if text[i] == close:
            return out, i + 1
        m = _NAME.match(text, i)
        if not m:
            raise BibError(f"cannot read a field near {text[i:i + 30]!r}")
        name, i = m.group(1), m.end()
        parts = []
        while True:
            value, i = _value(text, i, macros)
            parts.append(value)
            while i < len(text) and text[i].isspace():
                i += 1
            if i < len(text) and text[i] == "#":
                i += 1
                continue
            break
        out[name] = "".join(parts)


def _value(text: str, i: int, macros: dict) -> tuple[str, int]:
    while i < len(text) and text[i].isspace():
        i += 1
    if i >= len(text):
        raise BibError("value missing")
    c = text[i]
    if c == "{":
        j = _skip_group(text, i)
        return text[i + 1:j - 1], j
    if c == '"':
        depth = 0
        for j in range(i + 1, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            elif text[j] == '"' and depth == 0 and text[j - 1] != "\\":
                return text[i + 1:j], j + 1
        raise BibError("unterminated quoted value")
    m = re.compile(r"[\w.:+/-]+").match(text, i)
    if not m:
        raise BibError(f"cannot read a value near {text[i:i + 30]!r}")
    word = m.group(0)
    if word.isdigit():
        return word, m.end()
    if word.lower() in macros:
        return macros[word.lower()], m.end()
    raise BibError(f"undefined macro {word!r}")


# ---- LaTeX to Unicode --------------------------------------------------------------------------------
_ACCENTS = {"`": "\u0300", "'": "\u0301", "^": "\u0302", '"': "\u0308", "~": "\u0303", "=": "\u0304",
            ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b", "c": "\u0327", "k": "\u0328",
            "r": "\u030a", "d": "\u0323", "b": "\u0331"}
_SPECIAL = {"ss": "ß", "o": "ø", "O": "Ø", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ", "aa": "å", "AA": "Å",
            "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ", "dh": "ð", "DH": "Ð", "th": "þ", "TH": "Þ",
            "textendash": "–", "textemdash": "—", "textquoteleft": "‘", "textquoteright": "’",
            "textquotedblleft": "“", "textquotedblright": "”", "guillemotleft": "«", "guillemotright": "»",
            "S": "§", "P": "¶", "copyright": "©", "dag": "†", "ldots": "…", "dots": "…", "textellipsis": "…",
            "textregistered": "®", "texttrademark": "™", "LaTeX": "LaTeX", "TeX": "TeX", "pounds": "£",
            "euro": "€", "textdegree": "°", "times": "×", "textasciitilde": "~", "textbackslash": "\\"}
_ARG_CMDS = r"emph|textit|textbf|textsc|textrm|textsf|texttt|textup|textmd|textsl|mkbibquote|enquote|" \
            r"mbox|text|textnormal|uppercase|lowercase|MakeUppercase|MakeLowercase|url|path|nolinkurl|ensuremath"


def delatex(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\\\\", " ")
    s = re.sub(r"\\href\s*\{[^{}]*\}\s*\{([^{}]*)\}", r"\1", s)
    for _ in range(3):  # nested \emph{\textbf{...}}
        s = re.sub(rf"\\(?:{_ARG_CMDS})\s*\{{([^{{}}]*)\}}", r"\1", s)
    # \'{e}, \'e, {\'e}, \'{\i}; letter accents (\v, \c, ...) need braces or a space before the letter
    sym = r"\\([`'^\"~=.])\s*(?:\{\s*(\\?[A-Za-z])\s*\}|(\\?[A-Za-z]))"
    let = r"\\([uvHckrdb])(?:\s*\{\s*(\\?[A-Za-z])\s*\}|\s+(\\?[A-Za-z]))"

    def acc(m):
        base = (m.group(2) or m.group(3)).lstrip("\\")
        base = {"i": "i", "j": "j"}.get(base, base)
        return base + _ACCENTS[m.group(1)]
    s = re.sub(sym, acc, s)
    s = re.sub(let, acc, s)
    s = re.sub(r"\\([A-Za-z]+)\b\s?(?:\{\})?", lambda m: _SPECIAL.get(m.group(1), ""), s)
    s = re.sub(r"\\([&%$#_{}])", r"\1", s)
    s = s.replace("---", "—").replace("--", "–").replace("``", "“").replace("''", "”").replace("~", " ")
    s = s.replace("{", "").replace("}", "").replace("$", "")
    return unicodedata.normalize("NFC", " ".join(s.split()))


# ---- names -------------------------------------------------------------------------------------------
def _split_top(s: str, sep: str) -> list[str]:
    """Split at sep (a regex) only outside braces."""
    out, depth, last = [], 0, 0
    pat = re.compile(sep)
    i = 0
    while i < len(s):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif depth == 0:
            m = pat.match(s, i)
            if m and m.end() > i:
                out.append(s[last:i])
                last = i = m.end()
                continue
        i += 1
    out.append(s[last:])
    return [p.strip() for p in out if p.strip()]


def _braced(s: str) -> bool:
    """The whole string is one brace group."""
    if not (s.startswith("{") and s.endswith("}")):
        return False
    depth = 0
    for j, c in enumerate(s):
        depth += c == "{"
        depth -= c == "}"
        if depth == 0 and j < len(s) - 1:
            return False
    return True


def names(value: str, role: str) -> list[dict]:
    """Zotero creators from a BibTeX name list ('Last, First and First von Last and {Org Name}')."""
    out = []
    for name in _split_top(value, r"\s+and\s+"):
        if name.lower() == "others":
            continue
        if _braced(name):  # {World Health Organization}: an institution, not First Last
            out.append({"creatorType": role, "name": delatex(name)})
            continue
        parts = _split_top(name, r",")
        if len(parts) >= 3:      # von Last, Jr, First
            last, first = parts[0], f"{parts[2]}, {parts[1]}"
        elif len(parts) == 2:    # von Last, First
            last, first = parts
        else:
            toks = _split_top(name, r"\s+")
            if len(toks) == 1:
                out.append({"creatorType": role, "name": delatex(toks[0])})
                continue
            # 'First von Last': the last part starts at the first lowercase word, else at the last word
            k = next((j for j in range(1, len(toks) - 1) if delatex(toks[j])[:1].islower()), len(toks) - 1)
            first, last = " ".join(toks[:k]), " ".join(toks[k:])
        out.append({"creatorType": role, "lastName": delatex(last), "firstName": delatex(first)})
    return out
