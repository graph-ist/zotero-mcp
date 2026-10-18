#!/bin/bash
# Smoke test for zotero-mcp; reads the token on the Pi and never prints it.
T=$(sudo grep '^MCP_AUTH_TOKEN=' /etc/zotero-mcp/env | cut -d= -f2-)
U=http://127.0.0.1:3003/mcp
H=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
echo "no-token: $(curl -s -o /dev/null -w '%{http_code}' -X POST "$U" "${H[@]}" -d '{}')"
call() {
  curl -s -X POST "$U" -H "Authorization: Bearer $T" "${H[@]}" \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/call\",\"params\":{\"name\":\"$1\",\"arguments\":$2}}" \
    | sed -n 's/^data: //p' | python3 -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r["content"][0]["text"][:1500])'
  echo
}
curl -s -X POST "$U" -H "Authorization: Bearer $T" "${H[@]}" \
  -d '{"jsonrpc":"2.0","id":0,"method":"tools/list"}' | sed -n 's/^data: //p' \
  | python3 -c 'import json,sys; print("tools:", [t["name"] for t in json.load(sys.stdin)["result"]["tools"]])'
call list_collections '{}'
call search_items '{"query":"design","limit":3}'
if [[ "$1" == "all" ]]; then
  call export_items '{"keys":["LNNRR6GY"],"format":"bibtex"}'
  call get_annotations '{"key":"LNNRR6GY"}'
  call list_related '{"key":"LNNRR6GY"}'
  call list_trash '{"limit":3}'
  call list_saved_searches '{}'
  call find_duplicates '{"collection_key":"4EE6IKRK"}'
fi
