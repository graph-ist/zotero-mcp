"""MCP server for a Zotero library. Importing the package registers every tool on `mcp`."""
from . import checks, collections, imports, items, layout, ocr, pdf, reading  # noqa: F401  (tool registration)
from .core import mcp
from .transport import main

__all__ = ["mcp", "main"]
