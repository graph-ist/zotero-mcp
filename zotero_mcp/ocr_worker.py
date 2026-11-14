"""OCR worker for scanned PDFs in the Zotero WebDAV storage.

Jobs are JSON files in QUEUE, written by the MCP tool ocr_pdf or by `scan`. For each job:
1. the PDF is taken from KEY.zip; if it already has text, nothing is done;
2. ocrmypdf adds an invisible text layer without touching the page images (--optimize 0), so the
   pages keep their geometry and existing annotations stay in place; the result is kept in CACHE;
3. the text goes to Zotero's full-text index (search, get_fulltext);
4. with replace, the attachment on WebDAV is swapped for the OCR version, with the same file name
   and a new .prop (mtime, MD5), after a copy of the original zip in ORIGINALS. Zotero clients then
   download it and the text becomes selectable in the reader.

Usage: graphist-zotero-ocr run | scan | enqueue KEY [--replace] [--mode normal|rasterize|enhance]
OCR languages come from the item's Language field (en, it), else ZOTERO_OCR_LANGS (eng+ita).
Env: ZOTERO_USER_ID, ZOTERO_API_KEY, ZOTERO_WEBDAV_DIR, ZOTERO_OCR_DIR, ZOTERO_OCR_LANGS
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

from . import api as zotero_api
from . import config

WEBDAV = config.WEBDAV_DIR
ROOT = config.OCR_DIR
LANGS = config.OCR_LANGS
QUEUE, STATUS, CACHE, WORK, INCOMING = (os.path.join(ROOT, d) for d in ("queue", "status", "cache", "work", "incoming"))
ORIGINALS = os.path.join(os.path.dirname(ROOT), "originals")
MAX_PDF_BYTES = config.MAX_PDF_BYTES
KEY_RE = re.compile(r"[A-Z0-9]{8}")
TEXT_CHARS = 50          # a page with fewer characters counts as image only
NATIVE_SHARE = 0.7       # documents with text on this share of pages are left alone


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def write_json(path: str, data: dict) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def set_status(key: str, **fields) -> dict:
    path = os.path.join(STATUS, f"{key}.json")
    data = json.load(open(path)) if os.path.exists(path) else {"key": key}
    data |= fields | {"updated": now()}
    write_json(path, data)
    return data


_client: zotero_api.Client | None = None


def api(method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    """(status, body) from the Zotero API; retries on 429/503 like the server (zotero_mcp.api)."""
    global _client
    if _client is None:
        _client = zotero_api.Client()
    r = _client.request(method, path, json=body)
    return r.status_code, r.content


def read_pdf(key: str) -> tuple[str, bytes] | None:
    """(member name, PDF bytes) from KEY.zip, or None when the attachment is not a PDF."""
    path = os.path.join(WEBDAV, f"{key}.zip")
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.file_size > MAX_PDF_BYTES:
                raise RuntimeError(f"larger than {MAX_PDF_BYTES >> 20} MB")
            if info.filename.lower().endswith(".pdf"):
                data = z.read(info)
                if data[:5] == b"%PDF-":
                    return info.filename, data
    return None


def text_pages(data: bytes, sample: int | None = None) -> tuple[int, int]:
    """(pages, pages with text); with sample, only the first pages are opened."""
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = range(min(doc.page_count, sample) if sample else doc.page_count)
        with_text = sum(1 for i in pages if len(doc[i].get_text().strip()) >= TEXT_CHARS)
        return len(pages), with_text


def clean(text: str) -> str:
    """Light clean-up for search, from text-grab: ligatures, soft hyphens, words split at line ends.
    Page breaks become blank lines; headings, numbers and short lines are kept."""
    for a, b in {"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"}.items():
        text = text.replace(a, b)
    text = re.sub(r"[­​‌‍﻿]", "", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = text.replace("\f", "\n\n")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Zotero language field -> Tesseract models; anything else falls back to LANGS.
TESSERACT = {"en": "eng", "eng": "eng", "english": "eng", "it": "ita", "ita": "ita", "italian": "ita", "italiano": "ita"}


def languages(key: str) -> str:
    """OCR languages from the parent item's Language field (en, it-IT...); LANGS when unknown."""
    status, body = api("GET", f"/items/{key}")
    if status != 200:
        return LANGS
    parent = json.loads(body)["data"].get("parentItem")
    if parent:
        status, body = api("GET", f"/items/{parent}")
        if status != 200:
            return LANGS
    code = json.loads(body)["data"].get("language", "").strip().lower()
    return TESSERACT.get(code.split("-")[0].split("_")[0], LANGS) if code else LANGS


def annotations(key: str) -> int:
    status, body = api("GET", f"/items/{key}/children?itemType=annotation&limit=1")
    if status != 200:
        raise RuntimeError(f"cannot read annotations (HTTP {status})")
    return len(json.loads(body))


def replace_in_webdav(key: str, member: str, pdf: bytes) -> tuple[str, int]:
    """Swap KEY.zip and KEY.prop for the OCR version; the original zip is copied first.
    Returns (md5, mtime): Zotero clients only download the new file once the attachment item
    carries them too (see sync_file_metadata)."""
    os.makedirs(ORIGINALS, exist_ok=True)
    zip_path, prop_path = (os.path.join(WEBDAV, f"{key}.{ext}") for ext in ("zip", "prop"))
    shutil.copy2(zip_path, os.path.join(ORIGINALS, f"{key}-{time.strftime('%Y%m%d-%H%M%S')}.zip"))
    if os.path.exists(prop_path):
        shutil.copy2(prop_path, os.path.join(ORIGINALS, f"{key}-{time.strftime('%Y%m%d-%H%M%S')}.prop"))
    return write_to_webdav(key, member, pdf)


def write_to_webdav(key: str, member: str, pdf: bytes) -> tuple[str, int]:
    """Write KEY.zip (one member) and KEY.prop atomically, as Zotero's WebDAV sync does."""
    zip_path, prop_path = (os.path.join(WEBDAV, f"{key}.{ext}") for ext in ("zip", "prop"))
    old_umask = os.umask(0o002)  # group zotero-dav (rclone) must keep write access
    try:
        tmp_zip, tmp_prop = f"{zip_path}.ocr-tmp", f"{prop_path}.ocr-tmp"
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(member, pdf)
        mtime = int(time.time() * 1000)
        with open(tmp_prop, "w") as f:
            f.write(f'<properties version="1"><mtime>{mtime}</mtime>'
                    f'<hash>{hashlib.md5(pdf).hexdigest()}</hash></properties>')
        os.replace(tmp_zip, zip_path)
        os.replace(tmp_prop, prop_path)
    finally:
        os.umask(old_umask)
    return hashlib.md5(pdf).hexdigest(), mtime


def sync_file_metadata(key: str, md5: str, mtime: int) -> None:
    """Write md5/mtime on the attachment item, so synced clients see the file changed and fetch it."""
    status, body = api("GET", f"/items/{key}")
    if status != 200:
        raise RuntimeError(f"attachment not readable (HTTP {status})")
    version = json.loads(body)["version"]
    status, body = api("PATCH", f"/items/{key}", {"md5": md5, "mtime": mtime, "version": version})
    if status not in (200, 204):
        raise RuntimeError(f"file metadata not updated (HTTP {status}): {body[:200]!r}")


def rasterize(data: bytes, enhance: bool = False, dpi: int = 300) -> bytes:
    """Redraw every page as an image of the same size (a "screenshot" of the page). Used when the PDF
    cannot be read as is (e.g. a corrupt JPEG stops Ghostscript): MuPDF draws what it can and a broken
    image becomes at worst a dirty area. With enhance, the text-grab preprocessing is applied first
    (greyscale, contrast x1.5, sharpness x2, 3px median filter): only for poor scans, since it can
    erase footnotes and small type."""
    import io
    import pymupdf
    from PIL import Image, ImageEnhance, ImageFilter
    out = pymupdf.open()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if enhance else pymupdf.csRGB)
            if enhance:
                img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                img = ImageEnhance.Contrast(img).enhance(1.5)
                img = ImageEnhance.Sharpness(img).enhance(2.0)
                img = img.filter(ImageFilter.MedianFilter(size=3))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                stream = buf.getvalue()
            else:
                stream = pix.tobytes("jpeg", jpg_quality=85)
            target = out.new_page(width=page.rect.width, height=page.rect.height)
            target.insert_image(target.rect, stream=stream)
    result = out.tobytes(garbage=3, deflate=True)
    out.close()
    return result


def attach(key: str, filename: str) -> None:
    """New attachment created by attach_pdf: move INCOMING/KEY.pdf into WebDAV, then give the item
    its md5/mtime so Zotero clients download it. The file must not exist yet."""
    src = os.path.join(INCOMING, f"{key}.pdf")
    if os.path.exists(os.path.join(WEBDAV, f"{key}.zip")):
        raise RuntimeError("a file for this attachment already exists on WebDAV")
    pdf = open(src, "rb").read()
    if pdf[:5] != b"%PDF-":
        raise RuntimeError("incoming file is not a PDF")
    sync_file_metadata(key, *write_to_webdav(key, os.path.basename(filename), pdf))
    os.remove(src)


def process(job: dict) -> dict:
    key, replace = job["key"], job.get("replace", "no")
    started = time.time()
    set_status(key, state="running", started=now(), replace=replace)
    if job.get("action") == "attach":
        attach(key, job["filename"])
        set_status(key, attached=True)
    found = read_pdf(key)
    if found is None:
        return set_status(key, state="skipped", reason="no PDF in the attachment")
    member, data = found
    pages, with_text = text_pages(data)
    mode = job.get("mode", "normal")  # normal | rasterize | enhance; the last two always redo the OCR
    if mode == "normal" and with_text >= pages * NATIVE_SHARE and not os.path.exists(os.path.join(CACHE, f"{key}.pdf")):
        if job.get("action") == "attach":
            return set_status(key, state="done", reason="attached; the PDF has text, no OCR needed", pages=pages,
                              seconds=int(time.time() - started))
        return set_status(key, state="skipped", reason="the PDF already has text", pages=pages)

    cached = os.path.join(CACHE, f"{key}.pdf")
    if mode != "normal" or not os.path.exists(cached):
        os.makedirs(WORK, exist_ok=True)
        src, out, side = (os.path.join(WORK, f"{key}.{ext}") for ext in ("in.pdf", "pdf", "txt"))
        with open(src, "wb") as f:
            f.write(data if mode == "normal" else rasterize(data, enhance=mode == "enhance"))
        langs = languages(key)
        set_status(key, languages=langs, mode=mode)
        # Deskew only on redrawn pages: on the original it would shift existing annotations.
        flags = ["--skip-text"] if mode == "normal" else ["--force-ocr", "--deskew"]
        run = subprocess.run(["ocrmypdf", "-l", langs, "--jobs", str(os.cpu_count() or 2), *flags,
                              "--output-type", "pdf", "--optimize", "0", "--sidecar", side, src, out],
                             capture_output=True, text=True)
        if run.returncode != 0:
            raise RuntimeError(f"ocrmypdf exit {run.returncode}: {run.stderr.strip()[-300:]}")
        text = clean(open(side, encoding="utf-8", errors="replace").read())
        with open(os.path.join(CACHE, f"{key}.txt"), "w") as f:
            f.write(text)
        os.replace(out, cached)
        for leftover in (src, side):
            os.remove(leftover)
    text = open(os.path.join(CACHE, f"{key}.txt")).read()

    status, body = api("PUT", f"/items/{key}/fulltext",
                       {"content": text, "indexedPages": pages, "totalPages": pages})
    if status not in (200, 204):
        raise RuntimeError(f"full-text index not updated (HTTP {status}): {body[:200]!r}")

    replaced = False
    if replace == "yes" or (replace == "if-unannotated" and annotations(key) == 0):
        sync_file_metadata(key, *replace_in_webdav(key, member, open(cached, "rb").read()))
        replaced = True
    return set_status(key, state="done", pages=pages, ocr_pages=pages if mode != "normal" else pages - with_text, chars=len(text),
                      indexed=True, replaced=replaced, seconds=int(time.time() - started), error=None)


def run_queue() -> None:
    import fcntl  # Unix only: importing the module (to enqueue jobs) also works on Windows
    os.makedirs(QUEUE, exist_ok=True)
    with open(os.path.join(ROOT, "worker.lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another worker is running")
            return
        while jobs := sorted(os.listdir(QUEUE), key=lambda n: os.path.getmtime(os.path.join(QUEUE, n))):
            path = os.path.join(QUEUE, jobs[0])
            job = json.load(open(path))
            try:
                result = process(job)
                print(job["key"], result["state"], result.get("seconds", ""))
            except Exception as e:  # one failing document must not stop the queue
                set_status(job["key"], state="failed", error=str(e)[:500])
                print(job["key"], "failed:", e)
            os.remove(path)


def enqueue_attach(key: str, filename: str) -> dict:
    """Job for a PDF saved in INCOMING by attach_pdf: store it, then OCR it if it is a scan."""
    if not KEY_RE.fullmatch(key):
        raise ValueError(f"invalid key {key!r}")
    os.makedirs(QUEUE, exist_ok=True)
    write_json(os.path.join(QUEUE, f"{key}.json"), {"key": key, "action": "attach", "filename": filename,
                                                     "replace": "if-unannotated", "source": "attach_pdf",
                                                     "requested": now()})
    return set_status(key, state="queued", replace="if-unannotated", source="attach_pdf", error=None)


def enqueue(key: str, replace: str = "no", source: str = "manual", mode: str = "normal") -> dict:
    """mode: normal (text layer over the original pages), rasterize (pages redrawn as images, for
    PDFs that cannot be read), enhance (redrawn and preprocessed, for poor scans)."""
    if not KEY_RE.fullmatch(key):
        raise ValueError(f"invalid key {key!r}")
    if mode not in ("normal", "rasterize", "enhance"):
        raise ValueError(f"invalid mode {mode!r}")
    os.makedirs(QUEUE, exist_ok=True)
    write_json(os.path.join(QUEUE, f"{key}.json"), {"key": key, "replace": replace, "source": source,
                                                     "mode": mode, "requested": now()})
    return set_status(key, state="queued", replace=replace, source=source, error=None)


def scan() -> None:
    """Queue scanned PDFs added since the last scan; they are replaced only while they have no annotations."""
    os.makedirs(STATUS, exist_ok=True)
    seen_path = os.path.join(ROOT, "seen.json")
    seen = json.load(open(seen_path)) if os.path.exists(seen_path) else {}
    first_run = not seen
    for name in sorted(os.listdir(WEBDAV)):
        if not name.endswith(".zip"):
            continue
        key, mtime = name[:-4], int(os.path.getmtime(os.path.join(WEBDAV, name)))
        if seen.get(key) == mtime or os.path.exists(os.path.join(STATUS, f"{key}.json")):
            seen[key] = mtime
            continue
        # The first run only records the library as it is: older scans are OCRed on request (ocr_pdf).
        if not first_run:
            try:
                found = read_pdf(key)
                if found:
                    pages, with_text = text_pages(found[1], sample=20)
                    if with_text < pages * NATIVE_SHARE:
                        enqueue(key, "if-unannotated", "nightly")
                        print("queued", key)
            except Exception as e:
                print(key, "not checked:", e)
        seen[key] = mtime
    write_json(seen_path, seen)


def main() -> None:
    for d in (QUEUE, STATUS, CACHE):
        os.makedirs(d, exist_ok=True)
    command = sys.argv[1:] or ["run"]
    if command[0] == "run":
        run_queue()
    elif command[0] == "scan":
        scan()
        run_queue()
    elif command[0] == "enqueue" and len(command) >= 2:
        mode = command[command.index("--mode") + 1] if "--mode" in command else "normal"
        print(json.dumps(enqueue(command[1], "yes" if "--replace" in command else "no", mode=mode)))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
