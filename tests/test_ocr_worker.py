"""Tests for ocr_worker.py on temporary folders; no network, no real WebDAV files."""
import hashlib, json, os, re, sys, tempfile, zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if "ZOTERO_OCR_DIR" not in os.environ:  # run as a script: own temporary folders (pytest: see conftest.py)
    tmp = tempfile.mkdtemp()
    os.environ.update(ZOTERO_WEBDAV_DIR=os.path.join(tmp, "webdav"), ZOTERO_OCR_DIR=os.path.join(tmp, "lib", "ocr"),
                      ZOTERO_USER_ID="0", ZOTERO_API_KEY="x")
import ocr_worker as w

for d in (w.WEBDAV, w.QUEUE, w.STATUS, w.CACHE):
    os.makedirs(d, exist_ok=True)


def test_clean():
    raw = "ﬁrst experi-\nence\fNext page\n\n\n\nTitle\n12"
    assert w.clean(raw) == "first experience\n\nNext page\n\nTitle\n12"


def test_replace_keeps_original_and_writes_prop():
    key, member = "ABCD1234", "Some Book.pdf"
    with zipfile.ZipFile(os.path.join(w.WEBDAV, f"{key}.zip"), "w") as z:
        z.writestr(member, b"%PDF-original")
    open(os.path.join(w.WEBDAV, f"{key}.prop"), "w").write("<properties/>")
    md5, mtime = w.replace_in_webdav(key, member, b"%PDF-with-text")
    assert md5 == hashlib.md5(b"%PDF-with-text").hexdigest() and f"<mtime>{mtime}</mtime>" in open(os.path.join(w.WEBDAV, f"{key}.prop")).read()
    with zipfile.ZipFile(os.path.join(w.WEBDAV, f"{key}.zip")) as z:
        assert z.namelist() == [member] and z.read(member) == b"%PDF-with-text"
    prop = open(os.path.join(w.WEBDAV, f"{key}.prop")).read()
    assert re.search(rf"<hash>{hashlib.md5(b'%PDF-with-text').hexdigest()}</hash>", prop)
    assert re.search(r"<mtime>\d{13}</mtime>", prop)
    saved = sorted(os.listdir(w.ORIGINALS))
    assert [n.split("-")[0] for n in saved] == [key, key] and saved[0].endswith(".prop")
    with zipfile.ZipFile(os.path.join(w.ORIGINALS, saved[1])) as z:
        assert z.read(member) == b"%PDF-original"
    assert not [n for n in os.listdir(w.WEBDAV) if n.endswith(".ocr-tmp")]


def test_queue_skips_non_pdf_and_keeps_going():
    with zipfile.ZipFile(os.path.join(w.WEBDAV, "EPUB0001.zip"), "w") as z:
        z.writestr("book.epub", b"PK")
    w.enqueue("EPUB0001")
    w.enqueue("MISSING1")
    w.run_queue()
    assert json.load(open(os.path.join(w.STATUS, "EPUB0001.json")))["state"] == "skipped"
    assert json.load(open(os.path.join(w.STATUS, "MISSING1.json")))["state"] == "failed"
    assert os.listdir(w.QUEUE) == []


def test_enqueue_rejects_bad_keys():
    try:
        w.enqueue("../etc")
    except ValueError:
        return
    raise AssertionError("bad key accepted")


def test_attach_writes_new_file_and_metadata():
    key, pdf, calls = "NEWA1234", b"%PDF-open-access", []
    os.makedirs(w.INCOMING, exist_ok=True)
    open(os.path.join(w.INCOMING, f"{key}.pdf"), "wb").write(pdf)
    real, w.sync_file_metadata = w.sync_file_metadata, lambda *a: calls.append(a)
    try:
        w.attach(key, "Casner 2005.pdf")
        with zipfile.ZipFile(os.path.join(w.WEBDAV, f"{key}.zip")) as z:
            assert z.namelist() == ["Casner 2005.pdf"] and z.read("Casner 2005.pdf") == pdf
        assert calls[0][:2] == (key, hashlib.md5(pdf).hexdigest())
        assert not os.path.exists(os.path.join(w.INCOMING, f"{key}.pdf"))
        open(os.path.join(w.INCOMING, f"{key}.pdf"), "wb").write(pdf)
        try:
            w.attach(key, "again.pdf")  # never overwrite an existing file
            raise AssertionError("existing file overwritten")
        except RuntimeError:
            pass
    finally:
        w.sync_file_metadata = real


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
