# zotero-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

MCP server to search, read and edit a Zotero library through the Zotero Web API v3. It runs over stdio for local clients or as streamable HTTP behind a reverse proxy, and can read PDFs and EPUBs from a WebDAV copy of the Zotero file storage to provide full text and to create highlights. A background worker adds OCR to scanned PDFs.

## Requirements

- Python 3.10 or later
- A Zotero user ID and an API key with write access (https://www.zotero.org/settings/keys: the user ID is shown on that page as "Your user ID for use in API calls")
- Optional: Zotero file sync over WebDAV, with the WebDAV folder readable by the server, needed for `get_fulltext` on items Zotero has not indexed, `annotate_pdf`, `get_outline`, `extract_references`, `attach_pdf` and OCR. The files that Zotero's own storage or the desktop app keep (`~/Zotero/storage`) are not read
- Optional, for OCR: `ocrmypdf`, `tesseract-ocr-eng`, `tesseract-ocr-ita` (system packages)

Without WebDAV, searching, reading metadata and Zotero's full-text index, creating and editing items, collections, tags and notes, citations and exports all work.

## Installation

The server installs straight from this GitHub repository. With [uv](https://docs.astral.sh/uv/), `uvx --from git+https://github.com/graph-ist/zotero-mcp graphist-zotero-mcp` downloads it into an isolated environment and starts the stdio server, with no clone and no account. Without a suffix uv takes the latest commit on `main`; for a fixed version add a tag, `git+https://github.com/graph-ist/zotero-mcp@v0.10.0`. (The distribution is named `graphist-zotero-mcp` because `zotero-mcp` belongs to another project on PyPI.)

### Claude Desktop

In `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "zotero": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/graph-ist/zotero-mcp", "graphist-zotero-mcp"],
      "env": {
        "ZOTERO_USER_ID": "123456",
        "ZOTERO_API_KEY": "your_api_key",
        "KNOWLEDGE_AUTO_REFRESH": "false"
      }
    }
  }
}
```

If Claude Desktop does not find `uvx`, use its absolute path (`which uvx`). `KNOWLEDGE_AUTO_REFRESH=false` turns off the library snapshot for the [DOI MCP](https://github.com/graph-ist/doi-mcp); leave it on and set `KNOWLEDGE_DIR` only if you use that server too.

### Claude Code

```sh
claude mcp add zotero --env ZOTERO_USER_ID=123456 --env ZOTERO_API_KEY=your_api_key \
  --env KNOWLEDGE_AUTO_REFRESH=false -- uvx --from git+https://github.com/graph-ist/zotero-mcp graphist-zotero-mcp
```

### pip

```sh
pip install "graphist-zotero-mcp @ git+https://github.com/graph-ist/zotero-mcp"
```

The package installs `graphist-zotero-mcp` (the server; `python -m zotero_mcp` also works), `graphist-zotero-snapshot` (library snapshot) and `graphist-zotero-ocr` (OCR worker).

### Server install from the locked versions

```sh
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock.txt
.venv/bin/pip install --no-deps "git+https://github.com/graph-ist/zotero-mcp@v0.10.0"     # or --no-deps . from a checkout
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
| `ZOTERO_OCR_DIR` | no | `/var/lib/zotero-mcp/ocr` if `/var/lib/zotero-mcp` exists, else `~/.local/state/zotero-mcp/ocr` | OCR queue, status and cache |
| `ZOTERO_OCR_LANGS` | no | `eng+ita` | Tesseract languages when the item has no Language field |
| `MCP_TRANSPORT` | no | `stdio` | `stdio` or `http` |
| `PORT` | no | `3003` | HTTP port (bound to 127.0.0.1) |
| `MCP_AUTH_TOKEN` | in http mode | | At least 32 characters; sent as `Authorization: Bearer` header on every request (a token in the URL is refused unless `MCP_ALLOW_QUERY_TOKEN` is set) |
| `MCP_ALLOW_QUERY_TOKEN` | | off | `true` also accepts the token as `?token=` in the URL, for Claude web, whose connector form has only a name and a URL. Keep the proxy access log off |
| `MCP_ALLOWED_HOSTS` | no | | Comma-separated public host names accepted behind a proxy |
| `KNOWLEDGE_DIR` | no | `/var/lib/knowledge` | Where `library.json`, `fulltext/` and `references/` are written for the DOI MCP |
| `KNOWLEDGE_AUTO_REFRESH` | no | `true` | Refresh `library.json` a few seconds after every successful write to the library; `false` leaves it to `refresh_library_snapshot` |

## Usage

Stdio, for example in a VS Code `mcp.json`:

```json
{
  "servers": {
    "zotero": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/graph-ist/zotero-mcp", "graphist-zotero-mcp"],
      "env": { "ZOTERO_USER_ID": "123456", "ZOTERO_API_KEY": "..." }
    }
  }
}
```

HTTP:

```sh
MCP_TRANSPORT=http MCP_AUTH_TOKEN=$(openssl rand -hex 32) graphist-zotero-mcp
```

The endpoint is `http://127.0.0.1:3003/mcp`; every request carries `Authorization: Bearer <MCP_AUTH_TOKEN>` (in Claude web, under *Request headers* of the custom connector). A token in the query string is refused, so it never reaches proxy logs or browser histories, unless `MCP_ALLOW_QUERY_TOKEN=true`: Claude web's connector form now has only a name and a URL, so there the URL has the form `https://host/zotero-mcp?token=<MCP_AUTH_TOKEN>`. `GET /status` answers without credentials to loopback clients only (version, library snapshot, full texts and bibliographies indexed), for the operator on the Pi.

## Tools

| Area | Tools |
|---|---|
| Search and read | `search_items`, `find_dois`, `get_item`, `get_fulltext`, `list_collections`, `list_tags`, `list_saved_searches`, `list_trash`, `list_related`, `find_duplicates` |
| Create | `create_item`, `create_item_from_doi`, `create_item_from_isbn`, `import_items`, `create_item_from_patent`, `create_collection`, `add_note` |
| Edit | `update_item`, `update_items_batch`, `update_item_links`, `set_verification`, `set_related`, `merge_duplicates`, `update_collection`, `rename_tag` |
| Delete and restore | `trash_item`, `restore_item`, `delete_collection`, `delete_tag` |
| Output | `format_citations`, `export_items`, `export_annotations` |
| PDF | `get_outline`, `get_page_layout`, `get_annotations`, `annotate_pdf`, `annotate_region`, `update_annotation`, `delete_annotation`, `attach_pdf`, `ocr_pdf`, `ocr_status`, `extract_references` |
| Snapshot | `refresh_library_snapshot`, `index_fulltext` |

`find_dois` checks a list of DOIs against the library in one call (DOI field and `DOI:` lines in Extra); the index is rebuilt only when the library version changes, and `create_item_from_doi` uses it to refuse duplicates. `attach_pdf` finds open access copies of an item's DOI on OpenAlex (or takes a direct link), tries them in order until one returns a PDF, creates the attachment and hands the file to the OCR worker, which stores it on WebDAV and OCRs it if it is a scan. Publishers often refuse automated downloads; repositories usually do not.

`create_item_from_doi`, `create_item_from_isbn` and `import_items` share one policy for works already in the library, `if_exists`: `skip` (default) reports the existing item and changes nothing, `file` adds the requested collections and tags to it (never removes any), `duplicate` creates a new item anyway. `allow_duplicate=true` is kept as a synonym of `duplicate`.

`create_item_from_isbn` validates the check digit, looks the ISBN up in Open Library, then Google Books, then the DNB (the DNB first for the German-language group, `978-3`), and creates a `book` with the first record found; the catalogue goes to `source` and to the Library Catalog field, and `catalogues` says what each catalogue answered. The DNB is asked with the ISBN-13, then with the ISBN-10; an SRU diagnostic is reported as an error. With `expected_title`, `expected_first_author` and `expected_year` it applies the same rules as `create_item_from_doi` (via `matching.py`): a mismatch is not created, a match gets a `Verified:` line. Duplicates are books with the same ISBN-13 (ISBN-10 converted); chapters carry their book's ISBN and are not counted. `dry_run` looks up and checks without writing.

`import_items` creates up to 100 items from CSL JSON (one record, a list, or `{"items": [...]}`) or BibTeX/BibLaTeX, read by `bibtex.py` (standard library: `@string` macros, `#` concatenation, LaTeX accents and commands, name lists with particles, suffixes and `{Institutions}`). A record without a faithful Zotero type is refused and listed in `rejected` (a `@misc` without arXiv or URL, `@patent`, unknown CSL types) instead of becoming a generic document. Duplicates are found by DOI, by ISBN for books, else by title, first author and year with `matching.py`, and repeated records within one import are caught too. BibTeX keys are kept as `Citation Key:` lines in Extra; fields without a Zotero equivalent are listed per record in `unmapped`. Nothing is checked against a registry, so imported items are not marked verified.

`merge_duplicates(keys, keep)` merges duplicates as Zotero desktop does, in two calls. The first returns a plan: the item kept (by default the one with most attachments and notes, then with an abstract, then verified, then oldest), empty fields filled from the others, Extra lines added (one line per name: the kept item's `Verified:` and `Citation Key:` win, a different value is a conflict), tags and collections added, attachments and notes to move (annotations follow their PDFs), related items to re-link, and conflicting fields, where the kept value stays. The second call, `confirm=true` with the plan's `plan_token`, executes it; the token covers the version of every item involved, so a change made in between returns a new plan and writes nothing. Order: the kept item is updated and records the others as `dc:replaces`, children move, links are redirected, and only then the others go to the trash; if a child fails to move, nothing is trashed and the same call can be repeated to finish. Items of different types or with different DOIs are refused: a preprint and its published version are different works and get `set_related`. So are items that look like different editions (years more than one apart, disjoint ISBNs, different edition statements, publishers that share no name), unless `allow_different_editions=true`: each edition is a record that can be cited.

`get_page_layout(key, page)` lists the figures and tables of a PDF page with their caption labels (`Figure 3`, `Table 2`; also `Fig.`, `Abb.`, `Tabella`, `Tabelle`) and boxes as fractions of the page, `[x0, y0, x1, y1]` from the top-left corner. Detection is geometric (`layout.py`): raster images, clusters of vector drawings (fragments within 2% of the page merge into one figure) and ruled tables found by PyMuPDF; a caption is a text block starting with a label followed by `.`, `:` or a dash, matched to the nearest region above or below it (tables are expected captioned above, figures below). Tables without rules and labels set as text inside a figure can be missed; scanned pages give no regions and a warning. `annotate_region` draws a Zotero area annotation (type `image`) around a figure or table found by caption label (on one page or the whole PDF) or by region id, with the caption included by default, or around any rectangle given as page fractions; `dry_run` only finds the box. Zotero desktop renders the picture of the area, so other devices may show the box without it until a desktop client has opened the PDF.

`update_annotation` changes the comment, colour or tags of an annotation; the marked text and position stay. `delete_annotation` deletes permanently, as the Zotero reader does, and only with `confirm=true`; without it, it shows the annotation.

`check_claims` takes the claims of a draft, finds the cited source in the library and the passages of its full text that best match each claim, with page and `zotero://open-pdf` link; phrases in quotation marks and figures with their unit (`115 ms`, `50%`, `24 participants`) must appear verbatim in the text and are reported per claim. `save_references` stores the bibliographies of many items for the DOI MCP's maps.

`get_outline` lists the sections of a PDF with page ranges, from its bookmarks or from the typography (lines larger or bolder than the body text), and marks abstract, introduction, method, conclusion, references, notes and appendix; `get_fulltext(key, pages="12-15")` then reads one section. `extract_references` splits the reference list of a PDF into entries, picks up the DOIs it contains (optionally resolving the others on Crossref) and says which cited works are already in the library, so the gaps can be filled with `create_item_from_doi`. `export_annotations` renders the highlights, underlines and comments of an item or a collection as Markdown (citation, key, page, quote, comment, tags) and can save it as a child note of the item, replacing the note of an earlier export.

`get_fulltext` uses Zotero's full-text index first and falls back to text extracted from the WebDAV copy (PDF, else EPUB) with PyMuPDF. For a scanned PDF it returns the OCR text; if there is none yet, OCR is queued automatically and the response says so. `annotate_pdf` locates a passage in the PDF and creates a highlight or underline annotation through the API; the PDF file is not modified. On a scan it uses the OCR copy. Writes use `If-Unmodified-Since-Version`, so a conflicting change made elsewhere returns an error instead of being overwritten.

## OCR

`graphist-zotero-ocr` (`zotero_mcp/ocr_worker.py`) processes jobs queued by `ocr_pdf`, by `get_fulltext` on a scan, or from the command line. A PDF counts as scanned when under 70% of its pages have text. For each job:

1. ocrmypdf adds an invisible text layer and leaves the page images untouched (`--skip-text --optimize 0`), so geometry and existing annotations stay valid; the PDF and the cleaned text are kept in the cache;
2. the text goes to Zotero's full-text index (`PUT /items/{key}/fulltext`);
3. with replace (`yes`, or `if-unannotated` for automatic jobs), `KEY.zip` and `KEY.prop` on WebDAV are swapped for the OCR version after a copy of the original in `/var/lib/zotero-mcp/originals`, and the attachment's `md5` and `mtime` are updated through the API. Zotero clients only download the new file once those change.

Modes: `normal`; `rasterize`, pages redrawn as images and deskewed, for PDFs that ocrmypdf cannot read (corrupt images); `enhance`, as `rasterize` plus greyscale, contrast, sharpening and a median filter, for poor scans only, since it can erase small type. OCR languages come from the item's Language field.

```sh
graphist-zotero-ocr run | scan | enqueue KEY [--replace] [--mode normal|rasterize|enhance]
```

## Library snapshot

`graphist-zotero-snapshot` (`zotero_mcp/snapshot.py`) writes `KNOWLEDGE_DIR/library.json` (items with key, DOI, title, creators, year, tags, collection paths and keys, PDF/EPUB attachments) for services that must not call the Zotero API, such as the DOI MCP's literature maps. The tool `refresh_library_snapshot` runs it on demand (the "Refresh library and notes" button of the Literature maps page). One conditional request tells whether the library changed; if it did, only the items changed or deleted since the last snapshot are fetched and merged. The server needs group `knowledge` and write access to `/var/lib/knowledge`. The file carries `schema_version` (1); the DOI MCP refuses a newer version instead of misreading it, so bump it only when a field changes meaning or is removed. A snapshot without the field is rebuilt in full once. Each item also carries `abstract` (Abstract field, whitespace collapsed, at most 4000 characters, for the DOI MCP's item-level search), `patent_number`, `verified` (the parsed `Verified:` line of Extra) and `citekey` (the `Citation Key:` line that Better BibTeX writes for pinned keys), which the DOI MCP's `check_draft` resolves.

`index_fulltext(item_keys | collection)` writes the text of each item's PDF or EPUB page by page, with its highlights and comments, to `KNOWLEDGE_DIR/fulltext/KEY.json`: the DOI MCP's `search_everything` indexes those files together with the Obsidian notes. Scans use their OCR text. Run it per collection, then again after adding PDFs or annotating (`force` rewrites).

`index_fulltext(changed=true)` keeps the whole library indexed instead: items with a PDF or EPUB and no file yet, items whose attachment or annotations changed since the last run (one `since=` request), and removal of the files of items no longer in the library (trashed or deleted). At most `max_items` per call; the queue and the library version are kept in `KNOWLEDGE_DIR/.fulltext_state.json` (next to `fulltext/`, which the DOI MCP reads in full). An item that fails (no file on WebDAV, unreadable PDF) is retried only after its attachment or annotations change; a scan waiting for OCR stays in the queue. `graphist-zotero-index` (`zotero_mcp/index_worker.py`; `index_worker.py` at the root is a wrapper) runs the same without the limit, daily through `deploy/zotero-index.timer`; the first run on a large library takes long, and the DOI MCP then needs its own time to embed the new passages.

`create_item_from_doi` with `expected_title`, `expected_first_author` and `expected_year` writes `Verified: <date> match create_item_from_doi crossref title+author+year` in Extra when the record matched; `set_verification(key, status, source)` records on an existing item the outcome of a check made with the DOI MCP (`verify_doi`, `verify_book`), replacing an earlier line. The DOI MCP's maps show it as `verified`. `create_item_from_patent` makes a `patent` item from the fields of the DOI MCP's `patent_get`; the publication number is the identifier and a duplicate is refused. After every successful write the library snapshot is refreshed in the background (one conditional request when nothing changed), unless `KNOWLEDGE_AUTO_REFRESH=false`.

## Code layout

The code is the `zotero_mcp` package; `transport.main` is the entry point of the `graphist-zotero-mcp` command:

- `api.py`: everything about api.zotero.org shared by the server, the worker and the snapshot: base URL and headers, retry policy on 429/503, error hints, key and DOI helpers, a synchronous client
- `config.py`: settings from the environment
- `core.py`: the FastMCP instance, the `tool` decorator (structured errors), async HTTP, caches (collections, item templates, the DOI index updated with `since=`), item helpers
- `items.py`, `collections.py`, `pdf.py`, `ocr.py`: the tools by area
- `layout.py`: figure and table detection, `get_page_layout` and `annotate_region`
- `imports.py`: `create_item_from_isbn` (Open Library, Google Books, DNB SRU/MARC21) and `import_items` (CSL JSON and BibTeX mapping, duplicate lookup); `bibtex.py`: the BibTeX reader, standard library only
- `reading.py`: `get_outline` and `extract_references`; `checks.py`: `save_references`, `check_claims`, `index_fulltext`, with `bibliography.py` (reference lists from PDF text) and `claims.py` (BM25 passages, quotations and figures)
- `transport.py`: stdio or HTTP with the token check
- `snapshot.py`: the library snapshot; `ocr_worker.py`: the OCR worker; `index_worker.py`: daily incremental indexing for the shared corpus
- `matching.py`: a verbatim copy of the DOI MCP's `matching.py` (title similarity, surname and year rules, errata and replies), so `create_item_from_doi` and `extract_references` accept a record under the same rules as `find_doi` and `verify_doi`; update the two files together

`server.py`, `snapshot.py`, `ocr_worker.py` and `index_worker.py` at the root are thin wrappers around the package, kept so that installs that run the scripts from a checkout (the systemd units in `deploy/`) keep working.

## Deployment

Versions are listed in `CHANGELOG.md`.

The repository includes the files used to run the server on a Raspberry Pi (Debian) and expose it through Tailscale Funnel. Paths, the user name and the LAN range are specific to that setup and need adapting.

Unit files and the firewall are in `deploy/`.

- `zotero-mcp.service`: systemd unit for the server, sandboxed, reading its environment from `/etc/zotero-mcp/env`; it runs as `zotero-mcp` with read-only access to the WebDAV storage
- `zotero-index.service` and `zotero-index.timer`: `index_worker.py` daily at 03:17, same sandbox as the server (reads WebDAV, writes `/var/lib/knowledge`)
- `zotero-ocr.service` and `zotero-ocr.path`: the OCR worker, started when a job appears in the queue; it can write to the WebDAV storage (group `zotero-dav`, setgid directory) and to `/var/lib/zotero-mcp`
- `zotero-webdav.service`: `rclone serve webdav` for the Zotero file storage, restricted to loopback, the LAN and the tailnet
- `nftables-aegis.nft`: default-drop input firewall allowing SSH, WebDAV and DNS from the LAN and everything from `tailscale0`
- `smoke-test.sh`: checks that unauthenticated requests get 401, lists the tools and calls a few of them; `./smoke-test.sh all` adds the remaining read-only calls

After changing a unit file or the script it runs, use `systemctl restart`: `enable --now` does nothing on a unit that is already active.

## Tests

```sh
uv run --group dev pytest tests      # or: pip install -e . pytest && python -m pytest tests
```

`tests/test_imports.py` covers ISBN lookups (Open Library, Google Books, DNB served by the fake), the BibTeX reader and `import_items`; `tests/test_merge.py` `merge_duplicates`; `tests/test_layout.py` region detection and area annotations on generated PDFs; `tests/test_index_changed.py` the incremental indexing. `tests/test_tools.py` calls every tool against `tests/fake_zotero.py`, an in-memory stand-in for the Zotero Web API (and for Crossref, OpenAlex and PDF downloads) served through `httpx.MockTransport`; WebDAV and OCR directories are temporary. `tests/test_locate.py` checks the text location used by `annotate_pdf` and the size cap; `tests/test_ocr_worker.py` checks text clean-up, the WebDAV replacement with backup and `.prop`, and the queue. Nothing touches the network.

A release is a git tag `vX.Y.Z` matching `VERSION` in `zotero_mcp/config.py`. Publishing to PyPI is prepared in `.github/workflows/release.yml` (PyPI trusted publishing, no token in the repository) and stays off until the repository variable `PYPI_PUBLISH` is `true`.

## Licence

[MIT](LICENSE). The server depends on [PyMuPDF](https://pymupdf.readthedocs.io/), which is licensed under the AGPL-3.0 (or a commercial licence from Artifex). Installing the server downloads PyMuPDF separately from PyPI; whoever distributes the server together with PyMuPDF, for example in a container image, or offers it to others as a network service, should check the AGPL obligations that apply to that combination.
