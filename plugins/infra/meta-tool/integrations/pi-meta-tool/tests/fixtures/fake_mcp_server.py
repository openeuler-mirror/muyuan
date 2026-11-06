import json
import sys


TOOLS = [
    {
        "name": "lookup",
        "title": "Lookup",
        "description": "Look up a value without changing state.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    },
    {
        "name": "update",
        "description": "Update a value.",
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "destructiveHint": True},
    },
]


def send(message: dict[str, object]) -> None:
    print(json.dumps(message), flush=True)


for line in sys.stdin:
    if not line.strip():
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "server/discover":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "legacy MCP server"},
            }
        )
    elif method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "fake-meta-tool-provider",
                        "version": "1.0.0",
                    },
                },
            }
        )
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = message["params"]["name"]
        inputs = message["params"].get("arguments", {})
        if not any(tool["name"] == name for tool in TOOLS):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32602, "message": f"unknown tool {name}"},
                }
            )
            continue
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps({"name": name, "inputs": inputs}),
                        }
                    ],
                    "structuredContent": {"name": name, "inputs": inputs},
                    "isError": False,
                },
            }
        )
    elif "id" in message:
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "method not found"},
            }
        )
