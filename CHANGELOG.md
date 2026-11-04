# Changelog

## 0.7.1 (2026-11-04)

Fixed
- Collections cache: the "never loaded" sentinel was `0.0` compared with `time.monotonic()` (seconds since boot), so for the first five minutes after a reboot the empty cache counted as fresh: `list_collections`, collection paths in `search_items` and `update_item_links` by collection saw no collections, and `create_collection` could not invalidate it. The sentinel is now `None`. The offline suite failed on a freshly booted machine for the same reason (6 tests).

Changed
- `create_item_from_doi` and `extract_references` (`resolve_missing`) compare records with `zotero_mcp/matching.py`, a copy of the DOI MCP's module: same title similarity (0.88 over subtitle variants), surname and year rules as `find_doi` and `verify_doi`, and derivative records (errata, reviews, replies) are refused. `extract_references` adds `doi_title_score` to a resolved entry. `find_duplicates` uses the same similarity for preprint pairs.
- `library.json` carries `schema_version: 1`; a snapshot without it is rebuilt in full once.

## 0.7.0 (2026-11-04)

Added
- `get_outline`: sections of a PDF with page ranges, from bookmarks or from the typography, with kinds (abstract, introduction, method, conclusion, references, notes, appendix).
- `get_fulltext(pages="12-15")`: one section of the PDF, with `[p. N]` markers.
- `extract_references`: entries of the reference list with DOIs (also when broken over two lines), optional Crossref lookup for the rest, and which cited works are already in the library.
- `export_annotations`: Markdown of an item's or a collection's annotations, optionally saved as a child note that later exports replace.
- `tests/`: every tool tested offline against an in-memory Zotero API (`tests/fake_zotero.py`); 52 tests.
- `CHANGELOG.md`.
- `check_claims`: for each claim of a draft, the cited source in the library and the passages of its full text that match it, with page and `zotero://open-pdf` link; quoted phrases are searched verbatim (`zotero_mcp/claims.py`).
- `save_references`: bibliographies of many items stored in `KNOWLEDGE_DIR/references/KEY.json` for the DOI MCP's literature maps (books, humanities) (`zotero_mcp/bibliography.py`).
- `create_item_from_doi`: `expected_title`, `expected_first_author`, `expected_year` and `force`; a DOI whose record does not match, or a retracted work, is not created.
- `find_duplicates`: preprint ↔ published-version pairs (`match: "preprint"`).

Changed
- The code is the `zotero_mcp` package (api, config, core, items, collections, pdf, reading, ocr, transport); `server.py` is the entry point. Unit files and script paths are unchanged.
- One Zotero client (`zotero_mcp/api.py`) for the server, `ocr_worker.py` and `snapshot.py`: retries on 429/503 and error hints everywhere.
- The DOI index and the library snapshot are incremental (`since=` and `/deleted?since=`); `library.json` gains `collection_keys` per item and the status `updated`. The first refresh after the upgrade rebuilds the file once.
- Unexpected exceptions in a tool return a structured error with a hint; the traceback goes to the server log.

## 0.6.0 (2026-11-03)

Changed (breaking)
- `set_item_collections` and `update_item_tags` are replaced by `update_item_links(key, add_tags, remove_tags, add_collections, remove_collections)`.
- `annotate_pdf`: `color` takes a name (`yellow`, `red`, `green`, `blue`, `purple`, `magenta`, `orange`, `gray`) instead of a hex value.

Changed
- `get_annotations`: `limit`/`start` pagination with `total` and `more`.
- `find_duplicates`: `limit` and `total_groups`.
- `export_items`: `max_items` cap with `exported`, `total`, `truncated`.
- `format_citations`: 1 to 50 keys enforced in the schema.
- `search_items`: an unknown `item_type` is rejected instead of returning nothing.
- `refresh_library_snapshot`: description written for the model.
- `smoke-test.sh`: `MCP_AUTH_TOKEN` and `PORT` can be given as environment variables.

## 0.5.0

- `find_dois` and `attach_pdf`; library snapshot; OCR worker with WebDAV replacement; full text for EPUB and scans.
