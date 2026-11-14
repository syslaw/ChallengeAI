
# Task 4 — Token-Aware Rate Limiter and Model Fallback Router

## Overview

This implementation provides a lightweight, asynchronous LLM gateway with per-tenant token rate limiting, persistent SQLite accounting, and automatic model fallback.

The gateway authenticates incoming completion requests, reserves estimated token usage before contacting a provider, and reconciles reservations against validated provider-reported usage when available.

If the primary provider returns HTTP 429 or exceeds a three-second deadline, the gateway automatically attempts a secondary provider. Provider and database failures are translated into standardized, sanitized gateway errors.

## Architecture

```text
Client
  |
  v
FastAPI Gateway (:8004)
  |
  +-- Authenticate tenant and validate request
  |
  +-- SQLite Rate Limiter
  |     - Evict expired reservations
  |     - Calculate active tenant usage
  |     - Atomically reserve tokens
  |
  +-- Primary Provider (:8005)
  |     |
  |     +-- HTTP 429 or 3-second timeout
  |           |
  |           v
  |         Secondary Provider (:8006)
  |
  +-- Reconcile token reservation
  |
  v
Completion Response / Standardized Error
```

### Components

| File | Responsibility |
|------|----------------|
| `gateway.py` | Authentication, admission, routing orchestration, reconciliation, and error handling |
| `limiter.py` | Persistent SQLite sliding-window token accounting |
| `router.py` | Asynchronous provider communication, deadlines, fallback, and response validation |
| `models.py` | Strict completion request validation and token estimation |
| `config.py` | Gateway configuration and development credentials |
| `mock_providers.py` | Deterministic primary and secondary mock LLM providers |
| `inspect_usage.py` | Read-only inspection of stored reservations and active tenant usage |
| `tests/` | Limiter, router, and gateway integration tests |

## Requirements

- Python 3.13 (developed and tested with Python 3.13.17)
- FastAPI
- Uvicorn
- HTTPX
- Pydantic
- pytest and pytest-asyncio for testing

SQLite support is provided by Python's standard library.

Install dependencies from the repository root:

```bash
python -m pip install -r requirements.txt
```

No external LLM credentials, Redis instances, or database servers are required.

## Running the Application

Run the following commands from the repository root with the virtual environment activated.

### Terminal 1 — Primary Provider

```bash
python -m uvicorn task4_router.mock_providers:primary_app \
    --host 127.0.0.1 --port 8005
```

### Terminal 2 — Secondary Provider

```bash
python -m uvicorn task4_router.mock_providers:secondary_app \
    --host 127.0.0.1 --port 8006
```

### Terminal 3 — Gateway

```bash
python -m uvicorn task4_router.gateway:app \
    --host 127.0.0.1 --port 8004
```

The gateway initializes its on-disk SQLite database during application startup. If initialization fails, the gateway does not begin serving requests.

Upon running and starting the gateway, the database will be created. I did not
ship my databse in this project explicitly.

## API Usage

### Completion Request

Endpoint:

```text
POST /v1/completions
```

Example:

```bash
curl -s http://127.0.0.1:8004/v1/completions \
    -H "Authorization: Bearer dev-tenant-a-key" \
    -H "Content-Type: application/json" \
    -d '{"prompt":"Hello","max_tokens":100}'
```

Example response:

```json
{
  "model": "mock-primary-model",
  "choices": [
    {
      "text": "mock mock mock mock mock mock mock mock",
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 5,
    "completion_tokens": 8,
    "total_tokens": 13
  },
  "provider": "primary",
  "fallback_reason": null
}
```

The gateway supports a single non-streaming text completion. Unknown request fields, invalid types, blank prompts, and out-of-range output token limits are rejected.

### Authentication

Two development API keys are configured by default:

| API Key | Internal Tenant ID |
|---------|--------------------|
| `dev-tenant-a-key` | `tenant-a` |
| `dev-tenant-b-key` | `tenant-b` |

Each tenant has an independent token budget. API keys are validated against a server-configured allowlist using timing-resistant comparisons.

Raw API keys are not stored in SQLite or forwarded to model providers.

These are fictional development credentials and must be replaced before deployment outside the local assessment environment.

## Token-Aware Rate Limiting

Each tenant receives a rolling budget of 50,000 tokens per 60 seconds.

Before contacting a provider, the gateway calculates a conservative reservation:

```text
Reserved tokens = Estimated input tokens + Maximum output tokens
```

Input tokens are estimated using the UTF-8 byte length of the prompt. This estimator is deterministic and requires no external tokenizer dependency, but it is not exact model-specific tokenization.

For example:

```text
Prompt: "Hello"
Estimated input tokens: 5
Requested maximum output tokens: 100
Initial reservation: 105 tokens
```

A reservation is accepted only if the tenant's current usage plus the requested reservation does not exceed the configured budget.

Rejected requests return HTTP 429 without contacting either provider.

### Reservation Reconciliation

When valid provider-reported usage is available, the gateway applies the following policy:

| Outcome | Accounting Policy |
|---------|-------------------|
| Primary success | Reconcile to actual reported usage |
| Primary HTTP 429, secondary success | Reconcile to secondary reported usage |
| Primary timeout, secondary success | Retain the greater of the original reservation or reported secondary usage |
| Missing or invalid usage | Retain the original reservation |
| Provider failure or cancellation | Retain the reservation until expiration |

A successful mock completion for the example request reports 13 actual tokens, reducing its charge from 105 to 13.

After a primary timeout, the same successful secondary response retains a 105-token charge because primary token consumption is uncertain.

Provider usage is validated for nonnegative integer values, consistency between input, output, and total token counts, and an upper bound of 500,000 tokens per completion.

This upper bound is a deliberate defensive implementation limit intended to reject pathological accounting values.

Reconciliation preserves the original admission timestamp and never recreates expired reservations. If reported actual usage exceeds the original reservation, the recorded charge increases accordingly. Subsequent admissions may be rejected until sufficient capacity expires.

## SQLite Persistence and Concurrency

The default database is:

```text
task4_router/token_usage.sqlite3
```

Each reservation is stored in the `token_events` table with:

- Unique request ID
- Internal tenant ID
- Admission timestamp
- Reserved or reconciled token count

Indexes support efficient tenant usage queries and expiration cleanup.

Admission uses an explicit `BEGIN IMMEDIATE` transaction to make the following operations atomic:

1. Evict expired reservations.
2. Calculate the tenant's active token usage.
3. Check the requested reservation against available capacity.
4. Insert the reservation if permitted.

Concurrent writers cannot both admit requests based on the same stale usage calculation.

Each operation opens and closes its own SQLite connection. WAL mode improves read/write concurrency, while a bounded busy timeout prevents indefinite waits for database locks.

Synchronous SQLite operations execute through `asyncio.to_thread()` so database lock waits do not block the asynchronous gateway event loop.

Database transactions are committed before provider HTTP requests begin.

Reservations expire exactly 60 seconds after admission. Expired records are excluded from active usage immediately and physically removed during subsequent admission or reconciliation operations.

A database admission failure returns HTTP 503 without contacting a provider. If reconciliation fails after provider execution, the gateway also returns HTTP 503 and retains the original reservation.

## Provider Fallback

The router performs one primary attempt and, when eligible, one secondary attempt.

| Primary Outcome | Routing Behavior |
|-----------------|------------------|
| Valid HTTP 200 response | Return primary completion |
| HTTP 429 | Attempt secondary provider |
| Three-second deadline exceeded | Cancel primary attempt and attempt secondary |
| Other unsuccessful HTTP status | Return sanitized provider error |
| Connection or response validation failure | Return sanitized provider error |

The primary and secondary attempts are sequential. The secondary starts only after the primary attempt has returned an eligible failure or its timeout context has exited.

The router uses `asyncio.timeout()` to enforce a total asynchronous HTTP deadline, including response-body consumption, rather than relying solely on a read inactivity timeout.

The secondary also has a three-second deadline. No recursive fallback or automatic retry loop is performed.

### Demonstrating Fallback

Restart the primary provider in HTTP 429 mode:

```bash
TASK4_PRIMARY_MODE=rate_limit \
python -m uvicorn task4_router.mock_providers:primary_app \
    --host 127.0.0.1 --port 8005
```

Send the same completion request through the gateway. A successful response will contain:

```json
{
  "provider": "secondary",
  "fallback_reason": "rate_limit"
}
```

To demonstrate timeout fallback:

```bash
TASK4_PRIMARY_MODE=timeout \
python -m uvicorn task4_router.mock_providers:primary_app \
    --host 127.0.0.1 --port 8005
```

The primary mock waits five seconds, while the router applies its three-second deadline.

A successful fallback response contains:

```json
{
  "provider": "secondary",
  "fallback_reason": "timeout"
}
```

The secondary provider must remain running in success mode for these demonstrations.

## Standardized Errors

Gateway errors use a consistent response structure:

```json
{
  "error": {
    "code": "provider_error",
    "message": "The model provider could not complete the request."
  }
}
```

| HTTP Status | Error Code |
|-------------|------------|
| 400 | `invalid_request` |
| 401 | `invalid_api_key` |
| 404 | `not_found` |
| 405 | `method_not_allowed` |
| 429 | `rate_limit_exceeded` |
| 500 | `internal_error` |
| 502 | `provider_error` |
| 503 | `service_unavailable` |
| 504 | `provider_timeout` |

Raw upstream error bodies, stack traces, database paths, and invalid request contents are not returned to callers.

A local quota rejection is distinct from a primary provider HTTP 429. Local rejection prevents provider execution, whereas provider HTTP 429 can trigger fallback after successful admission.

## Configuration

The following environment variables override development defaults:

| Variable | Default |
|----------|---------|
| `TASK4_DATABASE_PATH` | `task4_router/token_usage.sqlite3` |
| `TASK4_PRIMARY_URL` | `http://127.0.0.1:8005/v1/completions` |
| `TASK4_SECONDARY_URL` | `http://127.0.0.1:8006/v1/completions` |
| `TASK4_TENANT_A_API_KEY` | `dev-tenant-a-key` |
| `TASK4_TENANT_B_API_KEY` | `dev-tenant-b-key` |
| `TASK4_PRIMARY_MODE` | `success` |
| `TASK4_SECONDARY_MODE` | `success` |

Mock provider modes include `success`, `rate_limit`, `timeout`, `error`, and `invalid_response`.

Other application limits are defined in code:

| Setting | Value |
|---------|-------|
| Tenant token budget | 50,000 |
| Sliding window | 60 seconds |
| Primary deadline | 3 seconds |
| Secondary deadline | 3 seconds |
| SQLite busy timeout | 2 seconds |
| Maximum prompt length | 50,000 characters |
| Maximum requested output tokens | 4,096 |
| Default requested output tokens | 256 |
| Maximum validated provider usage | 500,000 tokens |

Provider destinations are controlled by server configuration rather than client request parameters.

## Database Inspection

Inspect persistent reservations and active tenant usage:

```bash
python -m task4_router.inspect_usage
```

The inspection utility opens SQLite in read-only mode and displays stored records, reservation ages, expiration status, and remaining tenant capacity.

An expired record may remain physically stored until the next admission or reconciliation, but it no longer counts toward the tenant's budget.

## Testing

Run the Task 4 automated suite:

```bash
python -m pytest task4_router/tests/ -v
```

Confirmed results:

| Test Suite | Passing Tests |
|------------|---------------|
| SQLite rate limiter | 10 |
| Provider router | 13 |
| Gateway integration | 12 |
| Total | 35 |

Automated coverage includes:

- Exact quota boundaries and tenant isolation
- Deterministic sliding-window expiration and physical eviction
- Persistence across limiter instances
- Atomic concurrent reservations
- Reservation reconciliation
- Provider HTTP 429 and timeout fallback
- Cancellation before fallback and total response deadlines
- Invalid provider responses and usage validation
- Authentication and sanitized gateway errors
- Actual SQLite lock failures during admission and reconciliation
- Concurrent gateway admission with in-flight reservations

Tests use temporary on-disk SQLite databases, an injectable clock, HTTPX mock transports, and direct ASGI integration. Running Uvicorn servers or accessing external providers is not required.

Manual end-to-end testing additionally verified real HTTP communication, the actual three-second primary deadline, in-flight SQLite reservations, conservative timeout accounting, tenant isolation, and physical eviction of expired records.

The complete assessment regression passed 127 automated tests across Tasks 1–4.

```bash
python -m pytest -q
```

## Design Decisions and Limitations

- SQLite was selected to satisfy persistent storage requirements without external infrastructure. Its single-writer architecture is appropriate for this assessment but may limit throughput under heavy production traffic.
- Token admission uses a conservative UTF-8 byte-length estimate rather than exact model-specific tokenization.
- Accounting tracks logical gateway requests, not exact combined provider billing. A timed-out provider may continue processing remotely after local cancellation, so total upstream consumption can exceed the retained reservation.
- Provider-reported usage is structurally validated but is assumed to originate from a trusted configured provider.
- The sliding window uses admission timestamps and wall-clock time, not continuous token-generation timestamps. Significant system-clock adjustments may affect expiration behavior.
- The asynchronous deadline is not a hard real-time execution guarantee and depends on cooperative cancellation.
- The gateway uses a request-scoped HTTP client and a single non-streaming completion response. It does not implement full OpenAI API compatibility, shared connection pooling, or production response-size controls.
- Authentication uses configurable local API keys rather than a production identity service. Request-body transport limits, distributed quotas, and production deployment hardening are outside the assessment scope.

The implementation intentionally prioritizes the assessment's required functionality, concurrency correctness, persistent accounting, and understandable failure behavior over unnecessary infrastructure.
