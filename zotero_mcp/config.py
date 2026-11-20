"""Settings from the environment, read once. Modules refer to them as config.NAME so tests can
override a value at run time."""
import os

VERSION = "0.11.0"


def _default_ocr_dir() -> str:
    """/var/lib/zotero-mcp/ocr on a server install (the folder exists); otherwise a per-user
    state folder, so a desktop install works without root."""
    if os.path.isdir("/var/lib/zotero-mcp"):
        return "/var/lib/zotero-mcp/ocr"
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "zotero-mcp", "ocr")


USER_ID = os.environ.get("ZOTERO_USER_ID", "")
API_KEY = os.environ.get("ZOTERO_API_KEY", "")
WEBDAV_DIR = os.environ.get("ZOTERO_WEBDAV_DIR", "/srv/zotero-webdav/zotero")
MAX_PDF_BYTES = int(os.environ.get("ZOTERO_MAX_PDF_MB", "200")) * 1024 * 1024
OCR_DIR = os.environ.get("ZOTERO_OCR_DIR") or _default_ocr_dir()
OCR_LANGS = os.environ.get("ZOTERO_OCR_LANGS", "eng+ita")
KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", "/var/lib/knowledge")
# After a successful write to the library the snapshot is refreshed a few seconds later, so the
# DOI MCP's maps see the change without a manual refresh_library_snapshot. "false" disables it.
AUTO_REFRESH = os.environ.get("KNOWLEDGE_AUTO_REFRESH", "true").strip().lower() != "false"
