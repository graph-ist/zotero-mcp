# Changelog

## Unreleased

Fixed
- `merge_duplicates` refuses items that look like different editions or printings (years more than one apart, disjoint ISBNs, different edition statements, publishers with no name in common) and suggests `set_related`; `allow_different_editions=true` overrides. Two editions of a book with the same title and no DOI were accepted before.
- `create_item_from_isbn` reports what each catalogue answered (`catalogues`: `used`, `not found`, or the error), also on success and in `dry_run`, so a catalogue skipped in silence shows why. The DNB lookup raises SRU diagnostics instead of taking them for "not found", and asks again with the ISBN-10 when the ISBN-13 finds nothing (older books are catalogued under the ISBN printed in them).

- `create_item_from_isbn` leaves the DNB alone for six hours after an HTTP 403 or 429 (an address that asks too often is blocked, and insisting prolongs it); `catalogues` says `paused for N more minutes after HTTP 403`. The pause is kept in memory and ends with a restart.

Added
- `index_fulltext(changed=true, max_items)`: the whole library kept indexed incrementally (new items, changed attachments and annotations, files of deleted items removed), state in `KNOWLEDGE_DIR/.fulltext_state.json`; `graphist-zotero-index` (root wrapper `index_worker.py`) and `deploy/zotero-index.{service,timer}` run it daily.
- `library.json` items carry `abstract` (at most 4000 characters); `schema_version` stays 1. Copy `tests/fixtures/contract/library.json` to the DOI MCP.
- `get_page_layout`: figures and tables of a PDF page with caption labels and boxes (raster images, vector drawing clusters, ruled tables).
- `annotate_region`: an area annotation around a figure or table by caption label or region id (caption included by default), or around any rectangle; `dry_run`.
- `merge_duplicates`: plan first, then `confirm=true` with a `plan_token` tied to the versions of every item involved; fills empty fields, merges Extra lines, tags, collections and relations (`dc:replaces` on the kept item), moves attachments and notes, re-links related items, trashes the others only after every child has moved. Different item types or DOIs are refused.
- `create_item_from_isbn`: a book from Open Library, Google Books or the DNB (DNB first for `978-3`), with check digit validation, the same expected title/author/year check and `Verified:` line as `create_item_from_doi`, duplicate check on the ISBN-13 of books, `dry_run`.
- `import_items`: up to 100 items from CSL JSON or BibTeX/BibLaTeX (new `bibtex.py`, standard library only); records without a faithful Zotero type are refused, duplicates found by DOI, ISBN or title + first author + year, citation keys kept in Extra, unmapped fields reported, `dry_run`.
- `update_annotation` (comment, colour, tags) and `delete_annotation` (permanent, needs `confirm=true`).
- `if_exists` (`skip`, `file`, `duplicate`) on `create_item_from_doi`, `create_item_from_isbn` and `import_items`; `file` adds the requested collections and tags to the existing item. `allow_duplicate` still works.

Changed
- `create_item_from_doi` checks the library before asking Crossref: a DOI already present answers `duplicate` without a Crossref request, even if Crossref is unreachable.
- The DOI index also holds the ISBN-13s of books (`library_isbns`).

## 0.10.0

Changed
- Published on PyPI as `graphist-zotero-mcp`, MIT licence; `uvx graphist-zotero-mcp` runs it without a clone. Commands `graphist-zotero-mcp`, `graphist-zotero-snapshot`, `graphist-zotero-ocr`; `python -m zotero_mcp` also works.
- `snapshot.py` and `ocr_worker.py` moved into the package (`zotero_mcp/snapshot.py`, `zotero_mcp/ocr_worker.py`), so the installed package carries them; the scripts at the root are now wrappers and the systemd units keep working unchanged.
- Without `ZOTERO_OCR_DIR` and without `/var/lib/zotero-mcp`, the OCR folder is `~/.local/state/zotero-mcp/ocr` (or under `$XDG_STATE_HOME`). The OCR worker imports `fcntl` only when it runs the queue, so the server also starts on Windows.

Added
- `MCP_ALLOW_QUERY_TOKEN=true`: the token is also accepted as `?token=` in the URL, for Claude web's connector form, which has only a name and a URL. Off by default.

## 0.9.1 (2026-11-04)

Added
- `check_claims`: the figures a claim attributes to the source ('115 ms', '50%', '24 participants', '0.83'; years and lone digits excluded) are searched in the text with their unit, decimal comma or point, like quotations (`numbers` per claim, with the pages where each occurs).

## 0.9.0 (2026-11-04)

Changed (breaking)
- HTTP transport: the token is accepted only in the `Authorization: Bearer` header; `?token=` in the URL is refused. Claude web custom connectors send it from *Request headers*.

Added
- `index_fulltext`: page texts and annotations of items written to `KNOWLEDGE_DIR/fulltext/KEY.json` for the shared corpus searched by the DOI MCP.
- `library.json` items carry `citekey` (Better BibTeX `Citation Key:` line in Extra).
- `GET /status` on loopback, without credentials: version, snapshot, indexed files.
- `tests/fixtures/contract/library.json`: the snapshot format as the DOI MCP's tests read it; regenerate with `UPDATE_CONTRACT=1` and copy it over when the format changes.

## 0.8.0 (2026-11-04)

Added
- `set_verification`: records on an item the outcome of a bibliographic check (`verify_doi`, `verify_book`) as a `Verified: <date> <status> <source>` line in Extra; `create_item_from_doi` writes the same line when the record matched the expected title, author and year, and reports `verified`.
- `create_item_from_patent`: a `patent` item from the fields of the DOI MCP's `patent_get`, with duplicate check on the publication number.
- The library snapshot is refreshed in the background a few seconds after every successful write (`KNOWLEDGE_AUTO_REFRESH`, default on). `library.json` items carry `verified` and `patent_number`.

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
