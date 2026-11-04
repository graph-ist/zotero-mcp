"""Settings from the environment, read once. Modules refer to them as config.NAME so tests can
override a value at run time."""
import os

VERSION = "0.7.1"
USER_ID = os.environ.get("ZOTERO_USER_ID", "")
API_KEY = os.environ.get("ZOTERO_API_KEY", "")
WEBDAV_DIR = os.environ.get("ZOTERO_WEBDAV_DIR", "/srv/zotero-webdav/zotero")
MAX_PDF_BYTES = int(os.environ.get("ZOTERO_MAX_PDF_MB", "200")) * 1024 * 1024
OCR_DIR = os.environ.get("ZOTERO_OCR_DIR", "/var/lib/zotero-mcp/ocr")
OCR_LANGS = os.environ.get("ZOTERO_OCR_LANGS", "eng+ita")
KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/var/lib/knowledge")
