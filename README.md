# zotero-mcp

MCP server to search, read and edit a Zotero library through the Zotero Web API v3. It runs over stdio for local clients or as streamable HTTP behind a reverse proxy, and can read PDFs from a WebDAV copy of the Zotero file storage to provide full text and to create highlights.

## Requirements

- Python 3.10 or later
- A Zotero user ID and an API key with write access (https://www.zotero.org/settings/keys)
- Optional: Zotero file sync over WebDAV, needed for `get_fulltext` (fallback) and `annotate_pdf`

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
| PDF | `get_annotations`, `annotate_pdf` |

`get_fulltext` uses Zotero's full-text index first and falls back to text extracted from the WebDAV copy with PyMuPDF. `annotate_pdf` locates a passage in the PDF and creates a highlight or underline annotation through the API; the PDF file is not modified. It needs selectable text, so scanned documents are not supported. Writes use `If-Unmodified-Since-Version`, so a conflicting change made elsewhere returns an error instead of being overwritten.

## Deployment

The repository includes the files used to run the server on a Raspberry Pi (Debian) and expose it through Tailscale Funnel. Paths, the user name and the LAN range are specific to that setup and need adapting.

- `zotero-mcp.service`: systemd unit for the server, sandboxed, reading its environment from `/etc/zotero-mcp/env`
- `zotero-webdav.service`: `rclone serve webdav` for the Zotero file storage, restricted to loopback, the LAN and the tailnet
- `nftables-aegis.nft`: default-drop input firewall allowing SSH, WebDAV and DNS from the LAN and everything from `tailscale0`
- `smoke-test.sh`: checks that unauthenticated requests get 401, lists the tools and calls a few of them; `./smoke-test.sh all` adds the remaining read-only calls

After changing a unit file or the script it runs, use `systemctl restart`: `enable --now` does nothing on a unit that is already active.

## Tests

```sh
python test_locate.py
```

The test builds a PDF in memory and checks the text location used by `annotate_pdf`. It makes no calls to the Zotero API.
