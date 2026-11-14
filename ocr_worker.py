#!/usr/bin/env python3
"""Compatibility entry point for installs that run the scripts from a checkout; the code is
zotero_mcp/ocr_worker.py (installed command: graphist-zotero-ocr)."""
from zotero_mcp.ocr_worker import main

if __name__ == "__main__":
    main()
