# Task 2 — MCP Security Gateway Proxy

## Overview

A lightweight authenticated HTTP/JSON-RPC reverse proxy that sits between an AI agent client and a downstream mock Model Context Protocol (MCP) server. The gateway authenticates callers using Bearer tokens, enforces role-based tool authorization, and forwards permitted requests to the upstream service.

The primary security requirement is that a caller without the `admin` role cannot execute a tool whose name begins with `admin_`. Unauthorized calls are rejected before any upstream HTTP request is attempted.

This implementation is scoped to the assessment's JSON-only request/response proxy. It is not a complete MCP Streamable HTTP transport implementation.

## Architecture

```text
AI Agent / HTTP Client
        |
        | POST /mcp + Authorization: Bearer <token>
        v
FastAPI Security Gateway (127.0.0.1:8000)
        |
        | 1. Authenticate token and resolve role
        | 2. Parse and validate JSON-RPC request
        | 3. Authorize requested tool
        |
        +-- Denied --> JSON-RPC error; no upstream call
        |
        +-- Allowed --> httpx HTTP POST
                              |
                              v
                  Mock MCP Server (127.0.0.1:8001)
                              |
                              v
                  Upstream response returned to client
```

The gateway uses FastAPI for asynchronous HTTP request handling and httpx for asynchronous upstream communication. Authentication, validation, and authorization are separate functions called by the gateway endpoint. No database, external identity provider, or policy engine is required.

## Project structure

```text
task2_gateway/
├── __init__.py
├── gateway.py
├── mock_upstream.py
├── README.md
└── tests/
    ├── __init__.py
    └── test_gateway.py
```

- `gateway.py`: authentication, JSON-RPC validation, authorization, upstream forwarding, and error handling.
- `mock_upstream.py`: mock MCP tool discovery and execution over HTTP.
- `tests/test_gateway.py`: automated tests for the gateway's core security and forwarding behavior.

## Setup

From the project root (`ChallengeAI`), activate the existing Python virtual environment and install the project dependencies if needed:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Task 2 requires `fastapi`, `uvicorn`, and `httpx`; its tests also require `pytest` and `pytest-asyncio`. Development was performed with Python 3.13. Ensure these packages are included in the project requirements file. The mock uses the MCP JSON-RPC wire format directly; the MCP SDK is not required by Task 2 itself.

### Configuration

| Environment variable | Default (local development only) |
| --- | --- |
| `MCP_ADMIN_TOKEN` | `dev-admin-token` |
| `MCP_VIEWER_TOKEN` | `dev-viewer-token` |
| `MCP_UPSTREAM_URL` | `http://127.0.0.1:8001/mcp` |

The gateway rejects empty or identical admin and viewer tokens at startup. Tokens can be overridden in the gateway's environment before starting it. The default tokens are intended only for local testing and must not be used in a deployed environment.

### Start the upstream server

In Terminal 1, from the project root with the virtual environment active:

```bash
python -m uvicorn task2_gateway.mock_upstream:app --host 127.0.0.1 --port 8001
```

### Start the gateway

In Terminal 2, from the project root with the virtual environment active:

```bash
python -m uvicorn task2_gateway.gateway:app --host 127.0.0.1 --port 8000
```

Send client requests to the gateway on port 8000, not directly to the mock on port 8001.

## Authentication and authorization

The gateway extracts a Bearer token from the HTTP `Authorization` header and resolves the caller's role using an in-memory token mapping. The authentication scheme is case-insensitive, while tokens are compared exactly using `secrets.compare_digest`.

| Operation | Viewer | Admin |
| --- | --- | --- |
| `tools/list` | Allowed | Allowed |
| `tools/call` — regular tool | Allowed | Allowed |
| `tools/call` — name beginning with `admin_` | Blocked | Allowed |

The prefix check is case-sensitive and applies specifically to `tools/call`. Other valid JSON-RPC methods are forwarded without additional method-specific restrictions. Tool discovery is not filtered: a viewer may discover an administrative tool but cannot execute it. This behavior matches the assessment's specified policy.

The mock exposes three tools: `get_customer_record`, `trigger_refund`, and `admin_reset_key`. All operations are simulated; no real customer data, refunds, or credentials are involved.

## Example requests

Run the following commands in a third terminal while both services are running. These examples use the default development tokens.

### 1. Viewer discovers available tools

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H 'Authorization: Bearer dev-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expected: HTTP 200 and the upstream tool catalog, including `admin_reset_key`.

### 2. Viewer calls a regular tool

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H 'Authorization: Bearer dev-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_customer_record","arguments":{"customer_id":"CUST-12345"}}}'
```

Expected: HTTP 200 and a simulated customer record from the upstream server.

### 3. Viewer attempts an administrative tool

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H 'Authorization: Bearer dev-viewer-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

Expected: HTTP 403 with the following JSON-RPC response:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "error": {
    "code": -32001,
    "message": "Unauthorized Tool Call"
  }
}
```

The request is rejected locally. No upstream request is made, and no corresponding tool execution should appear in the mock server logs.

### 4. Admin calls an administrative tool

```bash
curl -i -X POST http://127.0.0.1:8000/mcp \
  -H 'Authorization: Bearer dev-admin-token' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

Expected: HTTP 200 and a simulated key-reset result. The mock generates a random identifier, not a real API credential.

## Validation and error handling

The gateway checks the JSON-RPC version, request ID, method, parameter structure, and tool name before authorization. Tool arguments must be JSON objects when present; tool-specific business validation remains an upstream responsibility.

Additional safeguards include duplicate JSON key rejection, rejection of non-standard JSON constants (`NaN` and `Infinity`), and a 1 MiB request-body processing limit. Duplicate-key rejection prevents ambiguous requests from being interpreted differently by the gateway and upstream parser.

| Condition | HTTP status | JSON-RPC code |
| --- | --- | --- |
| Missing or invalid credentials | 401 | `-32000` |
| Malformed JSON | 400 | `-32700` |
| Invalid request envelope | 400 | `-32600` |
| Invalid parameters | 400 | `-32602` |
| Unauthorized administrative tool | 403 | `-32001` |
| Request body exceeds limit | 413 | `-32600` |
| Upstream connection/request failure | 502 | `-32002` |
| Upstream network timeout | 504 | `-32002` |

Authorized requests are forwarded using their original body bytes. The upstream URL is determined by server configuration, not by the caller. The gateway does not forward the client's Authorization header, does not follow redirects, and uses a five-second httpx network timeout. HTTP status and response content from a responding upstream are returned without replacing upstream-generated JSON-RPC errors; selected content-type and MCP metadata headers are preserved. Network exceptions generated by the gateway are returned as controlled errors without exposing exception details.

## Testing

From the project root:

```bash
python -m pytest task2_gateway/tests/test_gateway.py -v
```

Current automated suite: **19 test cases** covering:

- Missing, malformed, and invalid Bearer credentials; case-insensitive Bearer scheme.
- Tool discovery for both roles.
- Administrative tool denial for viewers, including an assertion that zero outbound HTTP requests occurred.
- Administrative tool authorization for admins and regular tool authorization for viewers.
- Credential isolation and byte-for-byte request body preservation.

The tests use `httpx.ASGITransport` to exercise the actual FastAPI gateway and `httpx.MockTransport` to record outbound requests and simulate upstream responses. Neither Uvicorn server needs to be running. The test fixture temporarily replaces credentials, upstream address, and transport, restoring them after each test.

Manual integration checks also cover real local HTTP forwarding and upstream unavailability. To reproduce the latter, stop the mock server while leaving the gateway running, then send an authenticated `tools/list` request. The expected result is HTTP 502 with JSON-RPC code `-32002` and message `Upstream unavailable`.

Extended automated coverage for malformed requests, duplicate keys, request size limits, upstream timeout, and upstream error preservation is deferred. These behaviors are implemented but are not all covered by the current 19-test suite.

## Scope and limitations

- This is a JSON-only, single-request proxy for the assessment. It does not implement SSE, MCP initialization, session ownership, notification handling, or JSON-RPC batch processing.
- The mock upstream is intentionally not an authenticated production MCP service and does not enforce the advertised tool-specific schemas. In a real deployment, network controls and upstream authorization should prevent clients from bypassing the gateway.
- Static development tokens are not a substitute for production identity management, secret storage, rotation, TLS, and per-user audit logging.
- The body-size limit is checked after buffering. Production deployments should enforce limits during ingestion. A shared HTTP connection pool would also improve efficiency.
- Client-supplied MCP metadata headers are currently passed through selectively. A production implementation should validate or derive policy-relevant headers and enforce session ownership.
- The strict parser does not currently reject escaped unpaired Unicode surrogates; these may cause downstream serialization failures. This is a known hardening item.
- Upstream response bodies are preserved, including upstream-generated errors. A production deployment should define a policy for sanitizing sensitive upstream error content.

## Assessment summary

The gateway implements authenticated HTTP/JSON-RPC forwarding, transparent tool discovery, role-based administrative tool restrictions, and the required `-32001: Unauthorized Tool Call` error. The core security policy is enforced before forwarding, and automated tests verify that denied calls never reach the upstream transport.
