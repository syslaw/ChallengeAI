
"""
Task 2: Automated MCP Security Gateway Tests.

These tests exercise the actual FastAPI gateway while
replacing network connections with simulated transports.

No Uvicorn servers need to be running.
"""

import json

import httpx
import pytest
import pytest_asyncio

from task2_gateway import gateway


# ---------------------------------------------------------
# Test credentials
# ---------------------------------------------------------

# We use separate credentials for testing so our tests
# do not depend on the gateway's actual environment
# variables or development credentials.

ADMIN_HEADERS = {
    "Authorization": "Bearer test-admin-token"
}

VIEWER_HEADERS = {
    "Authorization": "Bearer test-viewer-token"
}


# ---------------------------------------------------------
# JSON-RPC request helper
# ---------------------------------------------------------

def make_request(
    method="tools/list",
    request_id=1,
    params=None,
):
    """
    Construct a JSON-RPC request for testing.

    Default arguments allow us to create a simple
    tools/list request without repeating the envelope.
    """

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }

    # Parameters are optional in JSON-RPC.
    if params is not None:
        payload["params"] = params

    return payload


# ---------------------------------------------------------
# Reusable test fixture
# ---------------------------------------------------------

@pytest_asyncio.fixture
async def test_environment(monkeypatch):
    """
    Prepare a fresh gateway test environment.

    Returns:
        client:
            HTTP client connected directly to FastAPI.

        upstream_requests:
            List recording every outbound HTTP request.

    pytest creates a fresh fixture for each test,
    preventing one test from affecting another.
    """

    upstream_requests = []

    # Temporarily replace the gateway's credentials.
    # monkeypatch automatically restores the original
    # values after each test finishes.
    monkeypatch.setattr(
        gateway,
        "TOKENS",
        {
            "test-admin-token": "admin",
            "test-viewer-token": "viewer",
        },
    )

    # Use a simulated upstream address.
    # No DNS lookup or network connection will occur.
    monkeypatch.setattr(
        gateway,
        "UPSTREAM_URL",
        "http://upstream.test/mcp",
    )

    def mock_upstream_handler(request):
        """
        Simulate the upstream MCP server.

        Every outbound request is recorded, allowing
        tests to inspect exactly what was forwarded.
        """

        upstream_requests.append(request)

        # Parse the request that reached upstream.
        payload = json.loads(request.content)

        # Return a predictable JSON-RPC response.
        return httpx.Response(
            status_code=200,
            json={
                "jsonrpc": "2.0",
                "id": payload["id"],
                "result": {
                    "forwarded": True,
                    "method": payload["method"],
                },
            },
        )

    # Replace the gateway's real HTTP transport
    # with our simulated upstream.
    monkeypatch.setitem(
        gateway.app.state._state,
        "upstream_transport",
        httpx.MockTransport(mock_upstream_handler),
    )

    # ASGITransport sends requests directly into
    # the FastAPI application without a TCP socket.
    transport = httpx.ASGITransport(
        app=gateway.app
    )

    # Create an asynchronous client that talks to
    # our actual gateway application.
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://gateway.test",
    ) as client:

        # yield provides the test with its environment.
        # When the test finishes, execution resumes here
        # and the async context manager closes the client.
        yield client, upstream_requests


# ---------------------------------------------------------
# Authentication tests
# ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Bearer",
        "Basic test-admin-token",
        "Bearer wrong-token",
        "Bearer test-admin-token extra",
        "Bearer TEST-ADMIN-TOKEN",
    ],
)
async def test_invalid_authentication(
    test_environment,
    authorization,
):
    """
    Reject missing, malformed, and invalid credentials.

    Every rejected request must be blocked before
    reaching the upstream MCP server.
    """

    client, upstream_requests = test_environment

    headers = {}

    # None simulates a completely missing header.
    # Other values simulate different invalid headers.
    if authorization is not None:
        headers["Authorization"] = authorization

    response = await client.post(
        "/mcp",
        json=make_request(),
        headers=headers,
    )

    # Verify HTTP authentication failure.
    assert response.status_code == 401

    # Verify the gateway's JSON-RPC error code.
    assert response.json()["error"]["code"] == -32000

    # Verify the HTTP Bearer authentication challenge.
    assert response.headers["www-authenticate"] == "Bearer"

    # Invalid callers must never reach upstream.
    assert upstream_requests == []


@pytest.mark.asyncio
async def test_case_insensitive_bearer_scheme(
    test_environment,
):
    """
    The Bearer scheme is case-insensitive.

    The token itself must still match exactly.
    """

    client, upstream_requests = test_environment

    response = await client.post(
        "/mcp",
        json=make_request(),
        headers={
            "Authorization":
                "bearer test-viewer-token"
        },
    )

    assert response.status_code == 200
    assert len(upstream_requests) == 1


# ---------------------------------------------------------
# Tool discovery tests
# ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [ADMIN_HEADERS, VIEWER_HEADERS],
)
async def test_tools_list_forwarded(
    test_environment,
    headers,
):
    """
    Both authenticated roles may discover tools.

    The gateway must forward tools/list transparently.
    """

    client, upstream_requests = test_environment

    payload = make_request(
        method="tools/list",
        request_id=10,
    )

    response = await client.post(
        "/mcp",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 200

    # Exactly one request should reach upstream.
    assert len(upstream_requests) == 1

    forwarded = upstream_requests[0]

    # Verify the forwarded payload matches the
    # original request.
    assert json.loads(forwarded.content) == payload

    # Verify the gateway used the configured
    # upstream address.
    assert str(forwarded.url) == (
        "http://upstream.test/mcp"
    )

    # Verify the upstream response is preserved.
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 10,
        "result": {
            "forwarded": True,
            "method": "tools/list",
        },
    }


# ---------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "admin_reset_key",
        "admin_delete_customer",
        "admin_",
    ],
)
async def test_viewer_cannot_call_admin_tools(
    test_environment,
    tool_name,
):
    """
    Block every tool beginning with admin_ when
    the authenticated caller is a viewer.
    """

    client, upstream_requests = test_environment

    payload = make_request(
        method="tools/call",
        request_id=20,
        params={
            "name": tool_name,
            "arguments": {},
        },
    )

    response = await client.post(
        "/mcp",
        json=payload,
        headers=VIEWER_HEADERS,
    )

    assert response.status_code == 403

    # Verify the exact error required by the
    # WhiteMatter / Quilr assessment.
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 20,
        "error": {
            "code": -32001,
            "message": "Unauthorized Tool Call",
        },
    }

    # CRITICAL SECURITY ASSERTION:
    # No upstream HTTP request was attempted.
    assert upstream_requests == []


@pytest.mark.asyncio
async def test_admin_can_call_admin_tool(
    test_environment,
):
    """
    Administrators may execute admin_ tools.
    """

    client, upstream_requests = test_environment

    payload = make_request(
        method="tools/call",
        request_id=21,
        params={
            "name": "admin_reset_key",
            "arguments": {},
        },
    )

    response = await client.post(
        "/mcp",
        json=payload,
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200

    # The authorized operation must reach upstream.
    assert len(upstream_requests) == 1

    assert json.loads(
        upstream_requests[0].content
    ) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "get_customer_record",
        "trigger_refund",
        "get_admin_report",
    ],
)
async def test_viewer_can_call_regular_tools(
    test_environment,
    tool_name,
):
    """
    Viewers may execute non-administrative tools.

    The policy restricts names that BEGIN with
    admin_, not names that merely contain admin.
    """

    client, upstream_requests = test_environment

    payload = make_request(
        method="tools/call",
        request_id=22,
        params={
            "name": tool_name,
            "arguments": {},
        },
    )

    response = await client.post(
        "/mcp",
        json=payload,
        headers=VIEWER_HEADERS,
    )

    assert response.status_code == 200
    assert len(upstream_requests) == 1

    assert json.loads(
        upstream_requests[0].content
    ) == payload


# ---------------------------------------------------------
# Transparent forwarding tests
# ---------------------------------------------------------

@pytest.mark.asyncio
async def test_client_token_not_forwarded(
    test_environment,
):
    """
    The client's gateway credential must never be
    forwarded to the downstream MCP server.
    """

    client, upstream_requests = test_environment

    response = await client.post(
        "/mcp",
        json=make_request(),
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert len(upstream_requests) == 1

    forwarded = upstream_requests[0]

    # httpx header lookups are case-insensitive.
    # Verify the outbound request does not contain
    # the client's Authorization header.
    assert "authorization" not in forwarded.headers


@pytest.mark.asyncio
async def test_original_body_preserved(
    test_environment,
):
    """
    Authorized request bytes must remain unchanged.

    Verify that formatting, request IDs, tool
    arguments, and custom metadata are preserved.
    """

    client, upstream_requests = test_environment

    # Construct a raw JSON body rather than passing
    # a dictionary through httpx's JSON serializer.
    #
    # This allows us to check byte-for-byte forwarding.
    original_body = (
        b'{ "jsonrpc": "2.0", '
        b'"id": "request-123", '
        b'"method": "tools/call", '
        b'"params": {"name": "get_customer_record", '
        b'"arguments": {"customer_id": "CUST-12345"}, '
        b'"_meta": {"trace": "abc123"}} }'
    )

    response = await client.post(
        "/mcp",
        content=original_body,
        headers={
            **VIEWER_HEADERS,
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 200
    assert len(upstream_requests) == 1

    # Verify byte-for-byte equality, not merely
    # equivalence after parsing the JSON.
    assert upstream_requests[0].content == original_body

    # String request IDs must also remain intact.
    assert response.json()["id"] == "request-123"
