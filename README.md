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
| `MCP_AUTH_TOKEN` | in http mode | | At least 32 characters; sent as `Authorization: Bearer` header on every request (a token in the URL is refused unless `MCP_ALLOW_QUERY_TOKEN` is set) |
| `MCP_ALLOW_QUERY_TOKEN` | | off | `true` also accepts the token as `?token=` in the URL, for Claude web, whose connector form has only a name and a URL. Keep the proxy access log off |
| `MCP_ALLOWED_HOSTS` | no | | Comma-separated public host names accepted behind a proxy |
| `KNOWLEDGE_AUTO_REFRESH` | no | `true` | Refresh `library.json` a few seconds after every successful write to the library; `false` leaves it to `refresh_library_snapshot` |

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

The endpoint is `http://127.0.0.1:3003/mcp`; every request carries `Authorization: Bearer <MCP_AUTH_TOKEN>` (in Claude web, under *Request headers* of the custom connector). A token in the query string is refused, so it never reaches proxy logs or browser histories, unless `MCP_ALLOW_QUERY_TOKEN=true`: Claude web's connector form now has only a name and a URL, so there the URL has the form `https://host/zotero-mcp?token=<MCP_AUTH_TOKEN>`. `GET /status` answers without credentials to loopback clients only (version, library snapshot, full texts and bibliographies indexed), for the operator on the Pi.

## Tools

| Area | Tools |
|---|---|
| Search and read | `search_items`, `find_dois`, `get_item`, `get_fulltext`, `list_collections`, `list_tags`, `list_saved_searches`, `list_trash`, `list_related`, `find_duplicates` |
| Create | `create_item`, `create_item_from_doi`, `create_item_from_patent`, `create_collection`, `add_note` |
| Edit | `update_item`, `update_items_batch`, `update_item_links`, `set_verification`, `set_related`, `update_collection`, `rename_tag` |
| Delete and restore | `trash_item`, `restore_item`, `delete_collection`, `delete_tag` |
| Output | `format_citations`, `export_items`, `export_annotations` |
| PDF | `get_outline`, `get_annotations`, `annotate_pdf`, `attach_pdf`, `ocr_pdf`, `ocr_status`, `extract_references` |
| Snapshot | `refresh_library_snapshot`, `index_fulltext` |

`find_dois` checks a list of DOIs against the library in one call (DOI field and `DOI:` lines in Extra); the index is rebuilt only when the library version changes, and `create_item_from_doi` uses it to refuse duplicates. `attach_pdf` finds open access copies of an item's DOI on OpenAlex (or takes a direct link), tries them in order until one returns a PDF, creates the attachment and hands the file to the OCR worker, which stores it on WebDAV and OCRs it if it is a scan. Publishers often refuse automated downloads; repositories usually do not.

`check_claims` takes the claims of a draft, finds the cited source in the library and the passages of its full text that best match each claim, with page and `zotero://open-pdf` link; phrases in quotation marks and figures with their unit (`115 ms`, `50%`, `24 participants`) must appear verbatim in the text and are reported per claim. `save_references` stores the bibliographies of many items for the DOI MCP's maps.

`get_outline` lists the sections of a PDF with page ranges, from its bookmarks or from the typography (lines larger or bolder than the body text), and marks abstract, introduction, method, conclusion, references, notes and appendix; `get_fulltext(key, pages="12-15")` then reads one section. `extract_references` splits the reference list of a PDF into entries, picks up the DOIs it contains (optionally resolving the others on Crossref) and says which cited works are already in the library, so the gaps can be filled with `create_item_from_doi`. `export_annotations` renders the highlights, underlines and comments of an item or a collection as Markdown (citation, key, page, quote, comment, tags) and can save it as a child note of the item, replacing the note of an earlier export.

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

## Library snapshot

`snapshot.py` writes `/var/lib/knowledge/library.json` (items with key, DOI, title, creators, year, tags, collection paths and keys, PDF/EPUB attachments) for services that must not call the Zotero API, such as the DOI MCP's literature maps. The tool `refresh_library_snapshot` runs it on demand (the "Refresh library and notes" button of the Literature maps page). One conditional request tells whether the library changed; if it did, only the items changed or deleted since the last snapshot are fetched and merged. The server needs group `knowledge` and write access to `/var/lib/knowledge`. The file carries `schema_version` (1); the DOI MCP refuses a newer version instead of misreading it, so bump it only when a field changes meaning or is removed. A snapshot without the field is rebuilt in full once. Each item also carries `patent_number`, `verified` (the parsed `Verified:` line of Extra) and `citekey` (the `Citation Key:` line that Better BibTeX writes for pinned keys), which the DOI MCP's `check_draft` resolves.

`index_fulltext(item_keys | collection)` writes the text of each item's PDF or EPUB page by page, with its highlights and comments, to `KNOWLEDGE_DIR/fulltext/KEY.json`: the DOI MCP's `search_everything` indexes those files together with the Obsidian notes. Scans use their OCR text. Run it per collection, then again after adding PDFs or annotating (`force` rewrites).

`create_item_from_doi` with `expected_title`, `expected_first_author` and `expected_year` writes `Verified: <date> match create_item_from_doi crossref title+author+year` in Extra when the record matched; `set_verification(key, status, source)` records on an existing item the outcome of a check made with the DOI MCP (`verify_doi`, `verify_book`), replacing an earlier line. The DOI MCP's maps show it as `verified`. `create_item_from_patent` makes a `patent` item from the fields of the DOI MCP's `patent_get`; the publication number is the identifier and a duplicate is refused. After every successful write the library snapshot is refreshed in the background (one conditional request when nothing changed), unless `KNOWLEDGE_AUTO_REFRESH=false`.

## Code layout

`server.py` is the entry point; the code is the `zotero_mcp` package:

- `api.py`: everything about api.zotero.org shared by the server, the worker and the snapshot: base URL and headers, retry policy on 429/503, error hints, key and DOI helpers, a synchronous client
- `config.py`: settings from the environment
- `core.py`: the FastMCP instance, the `tool` decorator (structured errors), async HTTP, caches (collections, item templates, the DOI index updated with `since=`), item helpers
- `items.py`, `collections.py`, `pdf.py`, `ocr.py`: the tools by area
- `reading.py`: `get_outline` and `extract_references`; `checks.py`: `save_references`, `check_claims`, `index_fulltext`, with `bibliography.py` (reference lists from PDF text) and `claims.py` (BM25 passages, quotations and figures)
- `transport.py`: stdio or HTTP with the token check
- `matching.py`: a verbatim copy of the DOI MCP's `matching.py` (title similarity, surname and year rules, errata and replies), so `create_item_from_doi` and `extract_references` accept a record under the same rules as `find_doi` and `verify_doi`; update the two files together

`ocr_worker.py` and `snapshot.py` stay at the root as the scripts the systemd units run.

## Deployment

Versions are listed in `CHANGELOG.md`.

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
pip install pytest
python -m pytest tests
```

`tests/test_tools.py` calls every tool against `tests/fake_zotero.py`, an in-memory stand-in for the Zotero Web API (and for Crossref, OpenAlex and PDF downloads) served through `httpx.MockTransport`; WebDAV and OCR directories are temporary. `tests/test_locate.py` checks the text location used by `annotate_pdf` and the size cap; `tests/test_ocr_worker.py` checks text clean-up, the WebDAV replacement with backup and `.prop`, and the queue. Nothing touches the network.
