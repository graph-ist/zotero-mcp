"""Offline test of PDF text location; no Zotero API calls."""
import os
import sys

os.environ.setdefault("ZOTERO_USER_ID", "0")
os.environ.setdefault("ZOTERO_API_KEY", "x")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pymupdf  # noqa: E402
from zotero_mcp.pdf import locate as _locate  # noqa: E402

doc = pymupdf.open()
p1 = doc.new_page(width=595, height=842)
p1.insert_text((72, 100), "Introduction page with nothing relevant.", fontsize=11)
p2 = doc.new_page(width=595, height=842)
lines = ["Design epistemology studies how designers come to",
         "know things. Design knowledge is distinct from experi-",
         "ence alone. Design epistemology again appears here."]
for n, line in enumerate(lines):
    p2.insert_text((72, 200 + n * 14), line, fontsize=11)
data = doc.tobytes()

a = _locate(data, "Design epistemology studies how designers come to know things.", None, 0)
assert a and a["pageIndex"] == 1 and len(a["rects"]) == 2, a
x0, y0, x1, y1 = a["rects"][0]
assert 842 - 200 - 12 < y0 < y1 < 842 - 200 + 12, a["rects"]  # PDF space: origin bottom-left
print("multi-line ok", a["pageLabel"], a["sortIndex"], a["rects"])

b = _locate(data, "distinct from experience alone", None, 0)
assert b and len(b["rects"]) == 2, b
print("hyphenation ok", b["rects"])

c = _locate(data, "design epistemology", None, 1)
assert c and c["rects"][0][1] < a["rects"][0][1], c
print("second occurrence ok", c["rects"])

assert _locate(data, "not in this pdf", None, 0) is None
assert _locate(data, "Design epistemology", 1, 0) is None
print("ALL OK")

# Size cap: a PDF inside a zip is returned, and refused once it exceeds the limit
import tempfile  # noqa: E402
import zipfile  # noqa: E402
from zotero_mcp import config  # noqa: E402
from zotero_mcp.core import ToolError  # noqa: E402
from zotero_mcp.pdf import pdf_bytes  # noqa: E402

_saved = config.WEBDAV_DIR, config.MAX_PDF_BYTES
try:
    with tempfile.TemporaryDirectory() as d:
        config.WEBDAV_DIR = d
        with zipfile.ZipFile(os.path.join(d, "ABCD2345.zip"), "w") as z:
            z.writestr("x.pdf", data)
        assert pdf_bytes("ABCD2345") == data
        config.MAX_PDF_BYTES = len(data) - 1
        try:
            pdf_bytes("ABCD2345")
            raise AssertionError("cap not enforced")
        except ToolError:
            pass
finally:
    config.WEBDAV_DIR, config.MAX_PDF_BYTES = _saved
print("size cap ok")
