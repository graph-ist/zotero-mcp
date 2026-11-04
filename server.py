#!/usr/bin/env python3
"""MCP server: search, read and edit a Zotero library through the Zotero Web API v3.

Env:
  ZOTERO_USER_ID, ZOTERO_API_KEY  required
  ZOTERO_WEBDAV_DIR               WebDAV storage with KEY.zip files, default /srv/zotero-webdav/zotero
  ZOTERO_OCR_DIR                  OCR queue, status and cache, default /var/lib/zotero-mcp/ocr
  ZOTERO_MAX_PDF_MB               attachments above this size are refused, default 200
  MCP_TRANSPORT                   'stdio' (default) or 'http' (streamable HTTP on 127.0.0.1)
  PORT                            HTTP port, default 3003
  MCP_AUTH_TOKEN                  required in http mode, Authorization: Bearer header only
  MCP_ALLOWED_HOSTS               comma-separated public host names allowed behind a proxy

The code lives in the zotero_mcp package: api (Zotero client shared with the worker and the
snapshot), core (plumbing), items, collections, pdf, ocr (tools), transport (entry point)."""
from zotero_mcp import main, mcp  # noqa: F401

if __name__ == "__main__":
    main()
