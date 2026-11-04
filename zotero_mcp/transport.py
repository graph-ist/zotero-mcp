"""Entry point: stdio, or streamable HTTP on loopback behind a token."""
import hmac
import os
from urllib.parse import parse_qs

from . import config
from .core import mcp


class TokenAuth:
    """Bearer header or ?token= query: Claude web connectors cannot send custom headers."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            header = dict(scope.get("headers", [])).get(b"authorization", b"")
            bearer = header[7:] if header[:7].lower() == b"bearer " else b""
            query = parse_qs(scope.get("query_string", b"").decode()).get("token", [""])[0].encode()
            if not (hmac.compare_digest(bearer, self.token) or hmac.compare_digest(query, self.token)):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
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
    # Access log off: the token travels in the query string
    uvicorn.run(TokenAuth(mcp.streamable_http_app(), token), host="127.0.0.1",
                port=int(os.environ.get("PORT", "3003")), access_log=False)
