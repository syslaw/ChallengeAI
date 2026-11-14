
"""
Automated security tests for the MCP gateway.
"""

import httpx
import pytest

from task2_gateway import gateway


@pytest.mark.asyncio
async def test_viewer_cannot_call_admin_tool(monkeypatch):
    """
    Verify that a viewer cannot execute admin_reset_key.

    Most importantly, verify that the gateway never
    attempts to contact the upstream server.
    """

    # Record any requests that reach the upstream.
    upstream_requests = []

    # Configure a known test credential.
    #
    # monkeypatch temporarily replaces this variable
    # and restores its original value after the test.
    monkeypatch.setattr(
        gateway,
        "TOKENS",
        {"test-viewer-token": "viewer"},
    )

    # This function acts as our simulated upstream.
    # If the gateway attempts to forward a request,
    # this function will record it.
    def mock_upstream(request):

        upstream_requests.append(request)

        return httpx.Response(
            status_code=200,
            json={"unexpected": "forwarded"},
        )

    # Inject our simulated HTTP transport.
    monkeypatch.setattr(
        gateway.app.state,
        "upstream_transport",
        httpx.MockTransport(mock_upstream),
    )

    # Connect directly to the FastAPI gateway.
    # No Uvicorn server or network port is required.
    transport = httpx.ASGITransport(
        app=gateway.app
    )

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://gateway.test",
    ) as client:

        # Simulate a viewer attempting an admin call.
        response = await client.post(
            "/mcp",
            headers={
                "Authorization":
                    "Bearer test-viewer-token"
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "admin_reset_key",
                    "arguments": {},
                },
            },
        )

    # Verify that the gateway rejects the request.
    assert response.status_code == 403

    # Verify the exact JSON-RPC error required
    # by the assessment.
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32001,
            "message": "Unauthorized Tool Call",
        },
    }

    # CRITICAL SECURITY ASSERTION:
    #
    # No request was sent to the upstream server.
    assert upstream_requests == []
