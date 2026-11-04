"""Shared fixtures: the server runs against FakeZotero through httpx.MockTransport, with WebDAV
and OCR directories in a temporary folder. No network."""
import asyncio
import json
import os
import sys
import tempfile
import zipfile

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp(prefix="zotero-mcp-tests-")
os.environ.update(ZOTERO_USER_ID="1", ZOTERO_API_KEY="test-key", KNOWLEDGE_AUTO_REFRESH="false",
                  ZOTERO_WEBDAV_DIR=os.path.join(TMP, "webdav"),
                  ZOTERO_OCR_DIR=os.path.join(TMP, "ocr"),
                  KNOWLEDGE_DIR=os.path.join(TMP, "knowledge"))
os.makedirs(os.environ["ZOTERO_WEBDAV_DIR"])
os.makedirs(os.environ["KNOWLEDGE_DIR"])

import server  # noqa: E402,F401  (the entry point must import)
from zotero_mcp import api, config, core  # noqa: E402
from fake_zotero import FakeZotero  # noqa: E402


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def zot():
    """A fresh fake library behind every HTTP client of the package, caches cleared."""
    fake = FakeZotero()
    api.TRANSPORT = httpx.MockTransport(fake.dispatch)
    core.reset_clients()
    yield fake
    core.reset_clients()
    api.TRANSPORT = None


@pytest.fixture
def call():
    """call('tool', **args) -> the tool's dict result."""
    def _call(_name: str, **args):
        res = run(core.mcp.call_tool(_name, args))
        if isinstance(res, dict):
            return res
        if isinstance(res, tuple):  # (content blocks, structured)
            return res[1] if isinstance(res[1], dict) else json.loads(res[0][0].text)
        return json.loads(res[0].text)
    return _call


def make_pdf(pages: list[str]) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for text in pages:
        p = doc.new_page(width=595, height=842)
        y = 100
        for line in text.split("\n"):
            p.insert_text((72, y), line, fontsize=11)
            y += 14
    return doc.tobytes()


def put_webdav(att_key: str, member: str, data: bytes) -> str:
    path = os.path.join(config.WEBDAV_DIR, f"{att_key}.zip")
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(member, data)
    return path


def article(zot: FakeZotero, title="Design epistemology", year="2015", doi="10.1000/abc", **extra) -> str:
    return zot.add_item("journalArticle", title=title, date=year, DOI=doi, publicationTitle="Design Studies",
                        creators=[{"creatorType": "author", "lastName": "Cross", "firstName": "Nigel"}], **extra)
