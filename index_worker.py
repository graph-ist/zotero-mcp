#!/usr/bin/env python3
"""Keep KNOWLEDGE_DIR/fulltext in step with the library for the DOI MCP's search_everything:
index_fulltext(changed=true) without the per-call limit. Run daily by zotero-index.timer.
    index_worker.py [MAX_ITEMS]      default: all pending items
Env: ZOTERO_USER_ID, ZOTERO_API_KEY, ZOTERO_WEBDAV_DIR, ZOTERO_OCR_DIR, KNOWLEDGE_DIR
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KNOWLEDGE_AUTO_REFRESH", "false")  # it writes nothing to Zotero
from zotero_mcp import checks  # noqa: E402


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    out = asyncio.run(checks.index_changed(limit))
    out["indexed"] = {k: v for k, v in out["indexed"].items() if not isinstance(v, dict)}  # failures only
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
