"""MCP server for a Zotero library. Importing the package registers every tool on `mcp`."""
from . import collections, items, ocr, pdf  # noqa: F401  (tool registration)
from .core import mcp
from .transport import main

__all__ = ["mcp", "main"]
