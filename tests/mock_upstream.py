"""Mock upstream vault-mcp pour smoke local vault-mcp-rs.

- Exige `Authorization: Bearer <TOKEN>` sur /mcp (comme le Python).
- Sert initialize / tools/list (38 noms) / tools/call vault_status /
  resources/list / prompts/list.
- Usage: python mock_upstream.py <port> <token>
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18787
EXPECTED = sys.argv[2] if len(sys.argv) > 2 else "x" * 32

TOOLS = [
    {"name": n, "description": "d", "inputSchema": {"type": "object", "properties": {}}}
    for n in [
        "list_notes", "read_note", "read_note_versioned", "search_notes",
        "search_vault", "get_graph_context", "create_note", "create_folder",
        "update_note", "append_note", "patch_note", "set_frontmatter",
        "delete_note", "move_note", "rename_note", "fix_links", "write_status",
        "reindex_vault", "reindex_note", "sync_now", "vault_status",
        "convia_status", "convia_list_pending_analysis",
        "convia_read_for_analysis", "convia_write_analysis",
        "convia_mark_blocked", "convia_requeue_blocked", "convia_list_blocked",
        "convia_scan", "wiki_ingest_status", "wiki_ingest_start",
        "wiki_ingest_claim", "wiki_ingest_read", "wiki_ingest_contract",
        "wiki_ingest_submit", "wiki_ingest_release", "wiki_ingest_merge_pending",
        "wiki_ingest_sync",
    ]
]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.send_header("mcp-session-id", "mock-vault-session")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("Authorization") != f"Bearer {EXPECTED}":
            self.send_response(401)
            self.end_headers()
            return
        ln = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(ln) or b"{}")
        except ValueError:
            self._send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse"}})
            return
        rid = data.get("id")
        method = data.get("method")
        if method == "initialize":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2025-11-25",
                "serverInfo": {"name": "vault-mcp", "version": "mock"},
                "capabilities": {"tools": {}}}})
        elif method == "tools/list":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call" and (data.get("params") or {}).get("name") == "vault_status":
            self._send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "ok"}]}})
        elif method in ("resources/list",):
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"resources": []}})
        elif method in ("prompts/list",):
            self._send({"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}})
        else:
            self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "mock: non gere"}})


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
