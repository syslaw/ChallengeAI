
"""
Task 2: Mock MCP Upstream Server.

This application simulates a downstream MCP server that
receives JSON-RPC requests over HTTP.

Responsibilities:
    - Return available tools for tools/list.
    - Execute simulated tools for tools/call.
    - Return JSON-RPC errors for unsupported requests.
    - Log received requests so we can verify forwarding.

IMPORTANT:
    This server deliberately does not enforce user roles.

    Authentication and authorization are the responsibility
    of our security gateway for this assessment.

    No real customer records, refunds, or API keys are used.
"""

import logging
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


# ---------------------------------------------------------
# Application setup
# ---------------------------------------------------------

app = FastAPI(title="Mock MCP Upstream Server")

# Configure logging.
#
# Logging lets us observe which requests actually reach
# the downstream server. This will be especially useful
# when proving that the gateway blocks unauthorized calls.
#
# Unlike Task 1, this application communicates through
# HTTP rather than stdio. Logs cannot interfere with the
# JSON-RPC transport in the same way.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mock_upstream")


# ---------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------

# MCP tools expose their names, descriptions, and input
# schemas so a client can discover how to invoke them.
#
# These definitions follow the MCP tools/list structure.

TOOLS = [
    {
        "name": "get_customer_record",
        "description": "Retrieve a simulated customer record.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "pattern": "^CUST-[0-9]{5}$",
                }
            },
            "required": ["customer_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "trigger_refund",
        "description": "Simulate a customer refund.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "pattern": "^CUST-[0-9]{5}$",
                },
                "amount": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                },
                "reason": {
                    "type": "string",
                    "minLength": 10,
                },
            },
            "required": [
                "customer_id",
                "amount",
                "reason",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "admin_reset_key",
        "description": "Simulate resetting an administrative API key.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------
# JSON-RPC response helpers
# ---------------------------------------------------------

def rpc_success(request_id, result):
    """
    Construct a successful JSON-RPC response.

    The response ID must match the original request ID
    so the client can correlate requests and responses.
    """

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def rpc_error(request_id, code: int, message: str):
    """
    Construct a standard JSON-RPC error response.

    A JSON-RPC response contains either a result or an
    error, never both.
    """

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def tool_result(message: str):
    """
    Construct an MCP-style tool execution result.

    MCP tool responses use a content array rather than
    returning only an arbitrary string.

    isError indicates whether tool execution failed.
    """

    return {
        "content": [
            {
                "type": "text",
                "text": message,
            }
        ],
        "isError": False,
    }


# ---------------------------------------------------------
# Simulated tool execution
# ---------------------------------------------------------

def execute_tool(name: str, arguments: dict):
    """
    Execute one of our simulated MCP tools.

    These functions do not perform real operations.

    The purpose of this mock is to test gateway behavior,
    not to recreate the full Task 1 MCP implementation.
    """

    if name == "get_customer_record":

        customer_id = arguments.get(
            "customer_id", "UNKNOWN"
        )

        return tool_result(
            f"Customer record retrieved: {customer_id}. "
            "Customer name: Example Corporation. "
            "Account status: Active."
        )

    if name == "trigger_refund":

        customer_id = arguments.get(
            "customer_id", "UNKNOWN"
        )
        amount = arguments.get("amount", 0)
        reason = arguments.get("reason", "")

        return tool_result(
            f"Simulated refund of ${amount} "
            f"for customer {customer_id}. "
            f"Reason: {reason}."
        )

    if name == "admin_reset_key":

        # Generate a simulated key identifier.
        #
        # This is NOT a real API credential and does not
        # change any external system.
        #
        # The operation is intentionally distinguishable
        # from the regular tools so we can test the
        # gateway's admin_ authorization policy.
        key_id = secrets.token_hex(8)

        return tool_result(
            f"Administrative key reset simulated. "
            f"New key identifier: {key_id}."
        )

    # A None result signals that the tool does not exist.
    return None


# ---------------------------------------------------------
# MCP HTTP endpoint
# ---------------------------------------------------------

@app.post("/mcp")
async def mcp_endpoint(request: Request):
    """
    Receive and respond to an HTTP JSON-RPC request.

    The gateway sends authorized requests to this endpoint.
    """

    # Parse the incoming JSON request.
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(
            status_code=400,
            content=rpc_error(
                None, -32700, "Parse error"
            ),
        )

    # Reject unsupported request structures.
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content=rpc_error(
                None, -32600, "Invalid Request"
            ),
        )

    request_id = payload.get("id")
    method = payload.get("method")

    # Log every received request.
    #
    # Importantly, we avoid logging the Authorization
    # header or entire arguments object, as those could
    # contain credentials or sensitive customer data.
    logger.info(
        "Upstream received method=%r id=%r",
        method,
        request_id,
    )

    if (
        payload.get("jsonrpc") != "2.0"
        or not isinstance(method, str)
    ):
        return JSONResponse(
            status_code=400,
            content=rpc_error(
                request_id, -32600, "Invalid Request"
            ),
        )

    # -----------------------------------------------------
    # Method: tools/list
    # -----------------------------------------------------

    if method == "tools/list":

        # Return the complete catalog.
        #
        # The gateway is required to forward tools/list
        # transparently. Therefore, viewers can discover
        # admin tools, even though they cannot execute them.
        return JSONResponse(
            content=rpc_success(
                request_id,
                {"tools": TOOLS},
            )
        )

    # -----------------------------------------------------
    # Method: tools/call
    # -----------------------------------------------------

    if method == "tools/call":

        params = payload.get("params")

        if not isinstance(params, dict):
            return JSONResponse(
                content=rpc_error(
                    request_id, -32602, "Invalid params"
                )
            )

        name = params.get("name")
        arguments = params.get("arguments", {})

        if (
            not isinstance(name, str)
            or not name
            or not isinstance(arguments, dict)
        ):
            return JSONResponse(
                content=rpc_error(
                    request_id, -32602, "Invalid params"
                )
            )

        # This log lets us see which specific tool
        # was received by the upstream server.
        logger.info(
            "Upstream executing tool=%r id=%r",
            name,
            request_id,
        )

        result = execute_tool(name, arguments)

        if result is None:
            return JSONResponse(
                content=rpc_error(
                    request_id,
                    -32602,
                    f"Unknown tool: {name}",
                )
            )

        return JSONResponse(
            content=rpc_success(
                request_id,
                result,
            )
        )

    # -----------------------------------------------------
    # Unsupported JSON-RPC method
    # -----------------------------------------------------

    return JSONResponse(
        content=rpc_error(
            request_id,
            -32601,
            "Method not found",
        )
    )