"""Self-contained read-only MCP fixture. No network, filesystem or account access."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

server = FastMCP("bate-demo")
read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@server.tool(annotations=read_only)
def lookup_customer(email: str) -> dict:
    """Find a synthetic customer for an email; demo data only."""
    return {"customer_id": "customer:" + email}


@server.tool(annotations=read_only)
def lookup_order(customer_id: str) -> dict:
    """Find a synthetic customer's order; demo data only."""
    return {"record_id": "order:" + customer_id}


@server.tool(annotations=read_only)
def lookup_invoice(customer_id: str) -> dict:
    """Find a synthetic customer's invoice; demo data only."""
    return {"record_id": "invoice:" + customer_id}


@server.tool(annotations=read_only)
def read_record(record_id: str) -> dict:
    """Read a synthetic record; demo data only."""
    return {"record_id": record_id, "status": "demo-result"}


if __name__ == "__main__":
    server.run(transport="stdio")
