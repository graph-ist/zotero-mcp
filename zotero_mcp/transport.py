"""Entry point: stdio, or streamable HTTP on loopback behind a Bearer token.

The token travels only in the Authorization header (Claude web custom connectors send it from
"Request headers"); a token in the URL is not accepted, because URLs end up in proxy logs and
browser histories. GET /status answers on loopback only, without credentials, for the operator.
"""
import hmac
import json
import os

from . import config
from .core import mcp


def status() -> dict:
    """Operator view: version, library snapshot, full texts indexed for the shared corpus."""
    import snapshot  # top-level module next to the package
    out: dict = {"service": "zotero-mcp", "version": config.VERSION, "auto_refresh": config.AUTO_REFRESH}
    try:
        snap = json.load(open(snapshot.out_path(), encoding="utf-8"))
        out["library_snapshot"] = {"generated": snap.get("generated"), "library_version": snap.get("library_version"),
                                   "items": len(snap.get("items", [])), "schema_version": snap.get("schema_version")}
    except (OSError, ValueError):
        out["library_snapshot"] = None
    for name in ("fulltext", "references"):
        folder = os.path.join(config.KNOWLEDGE_DIR, name)
        try:
            out[name] = sum(1 for f in os.listdir(folder) if f.endswith(".json"))
        except OSError:
            out[name] = 0
    return out


class TokenAuth:
    """Authorization: Bearer <token> on every request; /status for loopback clients."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("path") == "/status" and scope.get("method") == "GET":
                from starlette.responses import JSONResponse
                client = (scope.get("client") or ("", 0))[0]
                if client in ("127.0.0.1", "::1"):
                    await JSONResponse(status())(scope, receive, send)
                else:
                    await JSONResponse({"error": "not found"}, status_code=404)(scope, receive, send)
                return
            header = dict(scope.get("headers", [])).get(b"authorization", b"")
            bearer = header[7:] if header[:7].lower() == b"bearer " else b""
            if not hmac.compare_digest(bearer, self.token):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401,
                                   headers={"WWW-Authenticate": 'Bearer realm="zotero-mcp"'})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main() -> None:
    if not (config.USER_ID and config.API_KEY):
        raise SystemExit("ZOTERO_USER_ID and ZOTERO_API_KEY are required")
    if os.environ.get("MCP_TRANSPORT", "stdio") != "http":
        mcp.run()
        return
    import uvicorn
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_AUTH_TOKEN of at least 32 characters is required in http mode")
    uvicorn.run(TokenAuth(mcp.streamable_http_app(), token), host="127.0.0.1",
                port=int(os.environ.get("PORT", "3003")), access_log=False)
