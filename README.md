# zotero-mcp

MCP server to search, read and edit a Zotero library through the Zotero Web API v3. It runs over stdio for local clients or as streamable HTTP behind a reverse proxy, and can read PDFs and EPUBs from a WebDAV copy of the Zotero file storage to provide full text and to create highlights. A background worker adds OCR to scanned PDFs.

## Requirements

- Python 3.10 or later
- A Zotero user ID and an API key with write access (https://www.zotero.org/settings/keys)
- Optional: Zotero file sync over WebDAV, needed for `get_fulltext` (fallback) and `annotate_pdf`
- Optional, for OCR: `ocrmypdf`, `tesseract-ocr-eng`, `tesseract-ocr-ita` (system packages)

```sh
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock.txt
```

`requirements.lock.txt` pins every dependency with hashes (the versions running on the Pi). To update, loosen the pins, run `uv pip compile requirements.txt --universal --python-version 3.13 --generate-hashes -o requirements.lock.txt`, and run `uvx pip-audit` on the result.

## Configuration

All settings are environment variables.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `ZOTERO_USER_ID` | yes | | Numeric Zotero user ID |
| `ZOTERO_API_KEY` | yes | | Zotero API key |
| `ZOTERO_WEBDAV_DIR` | no | `/srv/zotero-webdav/zotero` | Directory with the `KEY.zip` attachment files |
| `ZOTERO_MAX_PDF_MB` | no | `200` | Attachments larger than this are refused instead of being read into memory |
| `ZOTERO_OCR_DIR` | no | `/var/lib/zotero-mcp/ocr` | OCR queue, status and cache |
| `ZOTERO_OCR_LANGS` | no | `eng+ita` | Tesseract languages when the item has no Language field |
| `MCP_TRANSPORT` | no | `stdio` | `stdio` or `http` |
| `PORT` | no | `3003` | HTTP port (bound to 127.0.0.1) |
| `MCP_AUTH_TOKEN` | in http mode | | At least 32 characters; sent as `Authorization: Bearer` header or `?token=` query parameter |
| `MCP_ALLOWED_HOSTS` | no | | Comma-separated public host names accepted behind a proxy |

## Usage

Stdio, for example in a VS Code `mcp.json`:

```json
{
  "servers": {
    "zotero": {
      "command": "/path/to/zotero-mcp/.venv/bin/python",
      "args": ["/path/to/zotero-mcp/server.py"],
      "env": { "ZOTERO_USER_ID": "123456", "ZOTERO_API_KEY": "..." }
    }
  }
}
```

HTTP:

```sh
MCP_TRANSPORT=http MCP_AUTH_TOKEN=$(openssl rand -hex 32) python server.py
```

The endpoint is `http://127.0.0.1:3003/mcp`. The query-string token exists because some web clients cannot set custom headers; for that reason the access log is disabled. Use it only over TLS.

## Tools

| Area | Tools |
|---|---|
| Search and read | `search_items`, `get_item`, `get_fulltext`, `list_collections`, `list_tags`, `list_saved_searches`, `list_trash`, `list_related`, `find_duplicates` |
| Create | `create_item`, `create_item_from_doi`, `create_collection`, `add_note` |
| Edit | `update_item`, `update_items_batch`, `update_item_tags`, `set_item_collections`, `set_related`, `update_collection`, `rename_tag` |
| Delete and restore | `trash_item`, `restore_item`, `delete_collection`, `delete_tag` |
| Output | `format_citations`, `export_items` |
| PDF | `get_annotations`, `annotate_pdf`, `ocr_pdf`, `ocr_status` |

`get_fulltext` uses Zotero's full-text index first and falls back to text extracted from the WebDAV copy (PDF, else EPUB) with PyMuPDF. For a scanned PDF it returns the OCR text; if there is none yet, OCR is queued automatically and the response says so. `annotate_pdf` locates a passage in the PDF and creates a highlight or underline annotation through the API; the PDF file is not modified. On a scan it uses the OCR copy. Writes use `If-Unmodified-Since-Version`, so a conflicting change made elsewhere returns an error instead of being overwritten.

## OCR

`ocr_worker.py` processes jobs queued by `ocr_pdf`, by `get_fulltext` on a scan, or from the command line. A PDF counts as scanned when under 70% of its pages have text. For each job:

1. ocrmypdf adds an invisible text layer and leaves the page images untouched (`--skip-text --optimize 0`), so geometry and existing annotations stay valid; the PDF and the cleaned text are kept in the cache;
2. the text goes to Zotero's full-text index (`PUT /items/{key}/fulltext`);
3. with replace (`yes`, or `if-unannotated` for automatic jobs), `KEY.zip` and `KEY.prop` on WebDAV are swapped for the OCR version after a copy of the original in `/var/lib/zotero-mcp/originals`, and the attachment's `md5` and `mtime` are updated through the API. Zotero clients only download the new file once those change.

Modes: `normal`; `rasterize`, pages redrawn as images and deskewed, for PDFs that ocrmypdf cannot read (corrupt images); `enhance`, as `rasterize` plus greyscale, contrast, sharpening and a median filter, for poor scans only, since it can erase small type. OCR languages come from the item's Language field.

```sh
ocr_worker.py run | scan | enqueue KEY [--replace] [--mode normal|rasterize|enhance]
```

## Deployment

The repository includes the files used to run the server on a Raspberry Pi (Debian) and expose it through Tailscale Funnel. Paths, the user name and the LAN range are specific to that setup and need adapting.

Unit files and the firewall are in `deploy/`.

- `zotero-mcp.service`: systemd unit for the server, sandboxed, reading its environment from `/etc/zotero-mcp/env`; it runs as `zotero-mcp` with read-only access to the WebDAV storage
- `zotero-ocr.service` and `zotero-ocr.path`: the OCR worker, started when a job appears in the queue; it can write to the WebDAV storage (group `zotero-dav`, setgid directory) and to `/var/lib/zotero-mcp`
- `zotero-webdav.service`: `rclone serve webdav` for the Zotero file storage, restricted to loopback, the LAN and the tailnet
- `nftables-aegis.nft`: default-drop input firewall allowing SSH, WebDAV and DNS from the LAN and everything from `tailscale0`
- `smoke-test.sh`: checks that unauthenticated requests get 401, lists the tools and calls a few of them; `./smoke-test.sh all` adds the remaining read-only calls

After changing a unit file or the script it runs, use `systemctl restart`: `enable --now` does nothing on a unit that is already active.

## Tests

```sh
python test_locate.py
python test_ocr_worker.py
```

`test_locate.py` builds a PDF in memory and checks the text location used by `annotate_pdf` and the size cap. `test_ocr_worker.py` checks text clean-up, the WebDAV replacement with backup and `.prop`, and the queue, in a temporary directory. Neither calls the Zotero API.
