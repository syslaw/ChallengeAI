
"""
Task 2: Authenticated MCP HTTP JSON-RPC Security Gateway.

Flow:
    AI Client
       |
       | HTTP POST + Authorization: Bearer <token>
       v
    FastAPI Gateway
       |
       | 1. Authenticate caller
       | 2. Validate JSON-RPC
       | 3. Authorize requested tool
       |
       +---- Unauthorized --> JSON-RPC -32001 (no forwarding)
       |
       +---- Authorized ----> Downstream MCP HTTP server
                                  |
                                  v
                            Upstream response

Scope: JSON-RPC over HTTP with JSON responses.
"""

import json
import os
import secrets
import re

import httpx

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

app = FastAPI(title="MCP Security Gateway")

# Store the upstream HTTP transport in application state.
#
# None means httpx will use its normal HTTP transport
# and make real network connections.
#
# During testing, we can replace None with MockTransport
# to simulate an upstream server without using the network.
app.state.upstream_transport = None


# The upstream address is fixed by trusted configuration.
# Never let the caller choose the upstream URL, as that
# could turn our gateway into an arbitrary HTTP proxy.
UPSTREAM_URL = os.getenv(
    "MCP_UPSTREAM_URL",
    "http://127.0.0.1:8001/mcp",
)

# ******** DEVELOPMENT ONLY CREDENTIALS ********
# Production credentials must come from a secure secret
# management solution, never hardcoded source files.
#
# Mapping: bearer token -> role
#
# Retrieve the administrator token from the environment.
# Use a development token if none is configured.

ADMIN_TOKEN = os.getenv(
    "MCP_ADMIN_TOKEN",
    "dev-admin-token",
)

# Retrieve the viewer token from the environment.
VIEWER_TOKEN = os.getenv(
    "MCP_VIEWER_TOKEN",
    "dev-viewer-token",
)

# Prevent empty tokens from being configured.
if not ADMIN_TOKEN or not VIEWER_TOKEN:
    raise ValueError(
        "Gateway tokens must not be empty"
    )

# Prevent the same token from being assigned
# to both administrator and viewer roles (dictionary type).
if ADMIN_TOKEN == VIEWER_TOKEN:
    raise ValueError(
        "Admin and viewer tokens must be different"
    )

# Map each validated token to its corresponding role.
TOKENS = {
    ADMIN_TOKEN: "admin",
    VIEWER_TOKEN: "viewer",
}



# ---------------------------------------------------------
# JSON-RPC response helper
# ---------------------------------------------------------

def rpc_error(request_id, code: int, message: str):
    """
    Construct a JSON-RPC 2.0 error response.

    The request ID is preserved so the client can match
    the error to the original request.

    A null ID is used when a valid ID cannot be determined.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
        },
    }


# ---------------------------------------------------------
# Authentication
# ---------------------------------------------------------


def authenticate(authorization: str | None) -> str | None:
    """
    Validate an HTTP Bearer credential and return its role.

    Returns:
        admin  - valid administrator credential
        viewer - valid viewer credential
        None   - missing or invalid credential
    """

    if not authorization:
        return None

    # Bearer tokens use a restricted ASCII character set.
    #
    # fullmatch() requires the entire header to match.
    # IGNORECASE allows "Bearer", "bearer", etc.
    #
    # The token itself remains case-sensitive.
    match = re.fullmatch(
        r"Bearer +([A-Za-z0-9\-._~+/]+=*)",
        authorization,
        flags=re.IGNORECASE | re.ASCII,
    )

    if match is None:
        return None

    token = match.group(1)

    # Compare bytes rather than strings, avoiding
    # compare_digest's ASCII string restriction.
    for known_token, role in TOKENS.items():

        if secrets.compare_digest(
            token.encode("utf-8"),
            known_token.encode("utf-8"),
        ):
            return role

    return None



# ---------------------------------------------------------
# Strict JSON parsing // Added Hardening Measure
# ---------------------------------------------------------

class DuplicateJSONKeyError(ValueError):
    """
    Raised when a JSON object contains the same key twice.

    Added to reject duplicate keys to prevent ambiguity between
    the gateway's interpretation of a request and the upstream
    server's interpretation.
    """


def reject_duplicate_keys(pairs):
    """
    Convert JSON key/value pairs into a Python dictionary.

    Unlike the default JSON parser, reject any duplicate
    keys rather than silently keeping the last value.

    This function is called for every JSON object,
    including nested objects such as params and arguments.
    """

    result = {}

    for key, value in pairs:

        if key in result:
            raise DuplicateJSONKeyError(
                f"Duplicate JSON key: {key}"
            )

        result[key] = value

    return result


def reject_invalid_constant(value):
    """
    Reject non-standard JSON numeric constants.

    Python's JSON parser normally accepts NaN, Infinity,
    and -Infinity, even though they are not valid JSON.
    """

    raise ValueError(
        f"Invalid JSON constant: {value}"
    )


# ---------------------------------------------------------
# JSON-RPC validation
# ---------------------------------------------------------

def validate_rpc(payload):
    """
    Validate the structure of a JSON-RPC 2.0 request.

    This function checks that the request contains
    the correct fields and data types before it
    reaches our authorization logic.

    Returns:
        (request_id, error_response)

    If validation succeeds, error_response is None.
    If validation fails, error_response contains
    a JSON-RPC error dictionary.
    """

    # -------------------------------------------------
    # 1. Validate the overall request structure
    # -------------------------------------------------

    # A single JSON-RPC request must be a JSON object,
    # which Python represents as a dictionary.
    #
    # This also rejects batch requests (JSON arrays).
    # Our gateway does not support batch processing.

    if not isinstance(payload, dict):
        return None, rpc_error(
            None, -32600, "Invalid Request"
        )

    # -------------------------------------------------
    # 2. Validate the request ID
    # -------------------------------------------------

    request_id = payload.get("id")

    # MCP request IDs must be strings or integers.
    #
    # We explicitly reject booleans because Python
    # treats bool as a subclass of int.
    #
    # We also reject missing IDs because this gateway
    # supports requests, not notifications.

    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, (str, int))
    ):
        return None, rpc_error(
            None, -32600, "Invalid Request"
        )

    # -------------------------------------------------
    # 3. Validate the JSON-RPC version
    # -------------------------------------------------

    # Only JSON-RPC version 2.0 is supported.

    if payload.get("jsonrpc") != "2.0":
        return request_id, rpc_error(
            request_id, -32600, "Invalid Request"
        )

    # -------------------------------------------------
    # 4. Validate the method
    # -------------------------------------------------

    method = payload.get("method")

    # The method must be a non-empty string.
    # Examples: tools/list, tools/call

    if not isinstance(method, str) or not method:
        return request_id, rpc_error(
            request_id, -32600, "Invalid Request"
        )

    # -------------------------------------------------
    # 5. Validate the general params structure
    # -------------------------------------------------

    # Parameters are optional.
    #
    # However, if provided, they must be a JSON object
    # because MCP uses named parameters.

    if "params" in payload:

        if not isinstance(payload["params"], dict):
            return request_id, rpc_error(
                request_id, -32602, "Invalid params"
            )

    # -------------------------------------------------
    # 6. Validate tool-specific request structure
    # -------------------------------------------------

    if method == "tools/call":

        params = payload.get("params")

        # A tool call requires a parameters object.

        if not isinstance(params, dict):
            return request_id, rpc_error(
                request_id, -32602, "Invalid params"
            )

        name = params.get("name")

        # The tool name must be a non-empty string.
        #
        # This is particularly important because our
        # authorization function checks whether the
        # name begins with admin_.

        if not isinstance(name, str) or not name:
            return request_id, rpc_error(
                request_id, -32602, "Invalid params"
            )

        # Tool arguments are optional, but if present
        # they must be represented by a dictionary.
        #
        # We validate the structure here, not the
        # individual tool's business requirements.

        if "arguments" in params:

            if not isinstance(
                params["arguments"], dict
            ):
                return request_id, rpc_error(
                    request_id, -32602, "Invalid params"
                )

    # -------------------------------------------------
    # 7. Validation successful
    # -------------------------------------------------

    # Returning None as the error response tells
    # the gateway that validation succeeded.
    #
    # The request can now proceed to authorization.

    return request_id, None

# ---------------------------------------------------------
# Authorization
# ---------------------------------------------------------

def is_authorized(role: str, payload: dict) -> bool:
    """
    Apply the assessment's tool-level security policy.

    tools/list:
        Allowed for authenticated users.

    tools/call:
        Tool names beginning with admin_ require admin.

    Other JSON-RPC methods:
        Not restricted by this specific tool policy.
    """

    if payload["method"] != "tools/call":
        return True

    tool_name = payload["params"]["name"]

    if tool_name.startswith("admin_") and role != "admin":
        return False

    return True


# ---------------------------------------------------------
# HTTP gateway endpoint
# ---------------------------------------------------------

@app.post("/mcp")
async def mcp_gateway(request: Request):
    """
    Authenticate, inspect, authorize, and forward a
    single JSON-RPC request.
    """

    # STEP 1: Authenticate before processing a request.
    role = authenticate(request.headers.get("authorization"))

    if role is None:
        return JSONResponse(
            status_code=401,
            content=rpc_error(None, -32000, "Unauthorized"),
            headers={"WWW-Authenticate": "Bearer"},
        )

    # STEP 2: Parse the incoming JSON (With added hardening measure for duplicative keys).
    
    try:
        raw_body = await request.body()

        # Enforce a one-megabyte request size limit.
        #
        # This prevents excessively large payloads from
        # being processed or forwarded. Because request.body()
        # already buffered the data, this is a processing
        # limit rather than a complete memory-level defense.
        if len(raw_body) > 1_048_576:
            return JSONResponse(
                status_code=413,
                content=rpc_error(
                    None, -32600, "Request too large"
                ),
            )

        # Explicitly decode as UTF-8 and use our custom
        # JSON parsing functions.
        payload = json.loads(
            raw_body.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys, # Allows interception of that process. Python will pass the key/value pairs into this function before converting them into a dictionary.
            parse_constant=reject_invalid_constant,
        )

        # Reject strings containing invalid Unicode surrogates.
        # Prevents malformed strings from causing downstream
        # JSON serialization failures. Added after stress testing.
        json.dumps(payload, ensure_ascii=False).encode("utf-8")

    except (ValueError, UnicodeDecodeError, RecursionError):
        # Never return the underlying parsing exception.
        # It could expose request content or implementation
        # details that are unnecessary for the client.
        return JSONResponse(
            status_code=400,
            content=rpc_error(
                None, -32700, "Parse error"
            ),
        )


    # STEP 3: Validate the JSON-RPC envelope.
    request_id, error = validate_rpc(payload)

    if error is not None:
        return JSONResponse(status_code=400, content=error)

    # STEP 4: Apply authorization BEFORE contacting
    # the upstream MCP server.
    if not is_authorized(role, payload):
        return JSONResponse(
            status_code=403,
            content=rpc_error(
                request_id,
                -32001,
                "Unauthorized Tool Call",
            ),
        )

    # STEP 5: Forward authorized requests to upstream.
    #
    # Send the original request body rather than
    # reconstructing it, preserving the JSON-RPC payload.
    #
    # Crucially, the client's Authorization header is NOT
    # forwarded. This prevents disclosure of gateway tokens.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    # REMOVE BEFORE SUBMITTING - REDUNDANT
    ## Forward selected MCP transport metadata.
    #for header_name in (
    #    "MCP-Protocol-Version",
    #    "MCP-Session-Id",
    #    "Mcp-Method",
    #    "Mcp-Name",
    #):
    #    value = request.headers.get(header_name)
    #    if value is not None:
    #        headers[header_name] = value

    try:
        # Redoing to create an asynchronous HTTP client.
        #
        # timeout:
        #   Limit network inactivity during upstream requests.
        #
        # follow_redirects:
        #   Prevent the client from automatically following
        #   redirects to unexpected upstream destinations.
        #
        # transport:
        #   Use our configured transport. In normal operation,
        #   this is None. Tests can inject a MockTransport.
        #
        # trust_env:
        #   Ignore environment-configured HTTP proxies.
        #   Our gateway connects to a trusted, fixed upstream.
        async with httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            transport=request.app.state.upstream_transport,
            trust_env=False,
        ) as client:

            # Forward the original authorized request bytes.
            upstream = await client.post(
                UPSTREAM_URL,
                content=raw_body,
                headers=headers,
            )

    except httpx.TimeoutException:
        return JSONResponse(
            status_code=504,
            content=rpc_error(
                request_id,
                -32002,
                "Upstream timeout",
            ),
        )

    except httpx.RequestError:
        return JSONResponse(
            status_code=502,
            content=rpc_error(
                request_id,
                -32002,
                "Upstream unavailable",
            ),
        )

    # STEP 6: Return the upstream response without
    # changing its body or HTTP status.
    #
    # Only explicitly supported response metadata is
    # passed through. We do not copy hop-by-hop headers.
    response_headers = {}

    for header_name in (
        "MCP-Session-Id",
        "MCP-Protocol-Version",
    ):
        value = upstream.headers.get(header_name)
        if value is not None:
            response_headers[header_name] = value

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=None,
        headers={
            **response_headers,
            "Content-Type": upstream.headers.get(
                "content-type", "application/json"
            ),
        },
    )
