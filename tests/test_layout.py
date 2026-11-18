"""get_page_layout and annotate_region on generated PDFs."""
import json

import pymupdf
import pytest

from zotero_mcp import layout
from conftest import article, put_webdav


def figure_page() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    p.insert_text((72, 80), "Some body text about the study, long enough to be text.", fontsize=11)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), 0)
    pix.clear_with(180)
    p.insert_image(pymupdf.Rect(150, 110, 450, 310), pixmap=pix)
    p.insert_text((150, 330), "Figure 1: A raster figure of the setup.", fontsize=10)
    p.insert_text((100, 400), "Table 2. Results by condition", fontsize=10)
    x0, y0, cw, rh = 100, 410, 130, 22
    for r in range(4):
        for c in range(3):
            p.insert_text((x0 + c * cw + 5, y0 + r * rh + 15), f"r{r}c{c}", fontsize=9)
    for r in range(5):
        p.draw_line((x0, y0 + r * rh), (x0 + 3 * cw, y0 + r * rh))
    for c in range(4):
        p.draw_line((x0 + c * cw, y0), (x0 + c * cw, y0 + 4 * rh))
    p.draw_circle((200, 650), 40)
    p.draw_rect(pymupdf.Rect(260, 610, 360, 690))
    p.draw_line((210, 650), (300, 650))
    p.insert_text((180, 720), "Fig. 3. Two panels of a vector diagram.", fontsize=10)
    p.insert_text((72, 790), "As shown in Figure 1, the effect is clear.", fontsize=11)
    q = doc.new_page(width=595, height=842)
    q.insert_text((72, 80), "Second page, text only, mentions Table 2 and Figure 3 again.", fontsize=11)
    return doc.tobytes()


def scan_page() -> bytes:
    doc = pymupdf.open()
    p = doc.new_page(width=595, height=842)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), 0)
    pix.clear_with(250)
    p.insert_image(p.rect, pixmap=pix)
    return doc.tobytes()


def _item(zot, data):
    k = article(zot)
    att = zot.add_item("attachment", parentItem=k, contentType="application/pdf")
    put_webdav(att, "a.pdf", data)
    return k, att


def test_norm_label():
    assert layout.norm_label("fig. 3") == layout.norm_label("Figure 3") == "Figure 3"
    assert layout.norm_label("Abb. 4") == "Figure 4" and layout.norm_label("Tabella 2") == "Table 2"
    assert layout.norm_label("Section 2") is None


def test_get_page_layout_finds_figures_tables_and_captions(zot, call):
    k, att = _item(zot, figure_page())
    out = call("get_page_layout", key=k, page=1)
    assert out["attachment_key"] == att and out["pages"] == 2 and out["warnings"] == []
    regs = out["regions"]
    assert [(r["kind"], r.get("caption_label")) for r in regs] == [("figure", "Figure 1"), ("table", "Table 2"),
                                                                  ("figure", "Figure 3")]
    x0, y0, x1, y1 = regs[0]["rect"]
    assert abs(x0 - 150 / 595) < 0.01 and abs(y0 - 110 / 842) < 0.01 and abs(x1 - 450 / 595) < 0.01 and abs(y1 - 310 / 842) < 0.01
    assert regs[2]["rect"][0] < 165 / 595 and regs[2]["rect"][2] > 355 / 595  # both panels in one box
    assert call("get_page_layout", key=k, page=2)["regions"] == []
    assert "out of range" in call("get_page_layout", key=k, page=3)["detail"]


def test_get_page_layout_warns_on_scans(zot, call):
    k, _ = _item(zot, scan_page())
    out = call("get_page_layout", key=k, page=1)
    assert out["regions"] == [] and "scanned page" in out["warnings"][0]


def test_annotate_region_by_label_creates_an_image_annotation(zot, call):
    k, att = _item(zot, figure_page())
    dry = call("annotate_region", key=k, label="figure 1", dry_run=True)
    assert dry["status"] == "found" and dry["caption_label"] == "Figure 1" and dry["page"] == "1" and len(zot.items) == 2
    out = call("annotate_region", key=k, label="Fig. 1", comment="setup", color="blue", tags=["figure"])
    assert out["status"] == "ok" and out["caption_label"] == "Figure 1"
    d = zot.items[out["created_keys"][0]]["data"]
    assert d["annotationType"] == "image" and d["parentItem"] == att and d["annotationColor"] == "#2ea8e5"
    assert d["annotationComment"] == "setup" and d["tags"] == [{"tag": "figure"}] and d["annotationPageLabel"] == "1"
    pos = json.loads(d["annotationPosition"])
    (r,) = pos["rects"]
    # PDF user space, origin bottom-left: the figure spans y 110-310 from the top, the caption below it
    assert pos["pageIndex"] == 0 and r[3] == pytest.approx(842 - 110 + 2, abs=1) and r[1] < 842 - 330
    assert d["annotationSortIndex"].startswith("00000|")


def test_annotate_region_by_id_rect_and_without_caption(zot, call):
    k, _ = _item(zot, figure_page())
    t = call("annotate_region", key=k, page=1, region_id=2, include_caption=False)
    assert t["status"] == "ok" and t["kind"] == "table"
    r = json.loads(zot.items[t["created_keys"][0]]["data"]["annotationPosition"])["rects"][0]
    assert r[3] == pytest.approx(842 - 410 + 2, abs=1)  # table top, caption above it left out
    free = call("annotate_region", key=k, page=2, rect=[0.1, 0.1, 0.5, 0.3])
    pos = json.loads(zot.items[free["created_keys"][0]]["data"]["annotationPosition"])
    assert pos["pageIndex"] == 1 and pos["rects"][0] == pytest.approx([59.5, 842 * 0.7, 297.5, 842 * 0.9], abs=0.01)


def test_annotate_region_validation(zot, call):
    k, _ = _item(zot, figure_page())
    assert "exactly one" in call("annotate_region", key=k)["detail"]
    assert "exactly one" in call("annotate_region", key=k, label="Figure 1", page=1, region_id=1)["detail"]
    assert "page is required" in call("annotate_region", key=k, region_id=1)["detail"]
    assert "fractions" in call("annotate_region", key=k, page=1, rect=[0.5, 0.1, 0.2, 0.3])["detail"]
    assert "no graphic captioned 'Figure 9'" in call("annotate_region", key=k, label="Figure 9")["detail"]
    assert "not a figure or table label" in call("annotate_region", key=k, label="Section 2")["detail"]
    assert "no region 9" in call("annotate_region", key=k, page=1, region_id=9)["detail"]
    k2 = article(zot, doi="10.1/x")
    assert "no PDF attachment" in call("annotate_region", key=k2, label="Figure 1")["detail"]
