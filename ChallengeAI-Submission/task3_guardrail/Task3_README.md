# Task 3 — LLM Gateway Streaming Guardrail (PII Redaction)

## Overview

A lightweight FastAPI gateway that forwards streaming chat completion requests to an upstream LLM-compatible provider, intercepts the returning Server-Sent Events (SSE), and redacts supported personally identifiable information (PII) before sending text to the client.

The implementation uses a deterministic local mock provider to demonstrate streaming behavior without external API credentials or model costs.

## Architecture

```text
Client --> Gateway (8002) --> Mock LLM Provider (8003)
             ^                       |
             |                       v
             +--- SSE response ------+
             |
             v
        SSE parsing --> Stateful PII redaction --> Sanitized SSE --> Client
```

- `gateway.py`: validates requests, forwards them asynchronously with httpx, parses SSE, applies redaction, and manages upstream resource cleanup.
- `redactor.py`: maintains per-stream candidate state and replaces supported PII patterns with `[REDACTED]`.
- `mock_provider.py`: generates deterministic SSE text deltas with fictional PII deliberately split across events.
- `manual_check.py`: verifies real HTTP streaming, redaction, text preservation, and completion ordering.
- `tests/`: automated redactor and gateway tests.

## Running locally

Requires Python 3.13 and the project dependencies. From the repository root, activate the existing virtual environment and install dependencies if needed:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Start the mock provider in one terminal:

```bash
python -m uvicorn task3_guardrail.mock_provider:app --host 127.0.0.1 --port 8003
```

Start the gateway in another terminal:

```bash
python -m uvicorn task3_guardrail.gateway:app --host 127.0.0.1 --port 8002
```

Send a streaming request to the gateway:

```bash
curl -N http://127.0.0.1:8002/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"mock-llm","messages":[{"role":"user","content":"Provide customer information."}],"stream":true}'
```

The mock returns the same fictional customer information regardless of the prompt. The gateway forwards the validated request but replaces the three sensitive values in the returning stream. Reconstructed text:

```text
Hello! Here is the customer information you requested. The customer's email is [REDACTED]. Their SSN is [REDACTED]. Their credit card number is [REDACTED]. Please handle this information securely.
```

The downstream stream ends with a finish event followed by `data: [DONE]`.

Set `LLM_UPSTREAM_URL` to configure the upstream endpoint; it defaults to `http://127.0.0.1:8003/v1/chat/completions`.

## Streaming and security design

### Stateful redaction

Compiled regular expressions detect common ASCII email addresses, formatted US SSNs (`XXX-XX-XXXX`), and card-like sequences of 13–19 digits with optional single spaces or hyphens. Card detection intentionally does not require a valid Luhn checksum.

A per-request redactor holds unresolved candidates across text deltas and releases classified text at candidate boundaries. Spaces following digits receive special handling to support space-separated card numbers. The pending candidate is limited to 320 characters; oversized candidates are replaced and their remaining characters discarded until a boundary. On successful completion, the final candidate is classified before the finish event and `[DONE]` are sent.

### SSE processing and performance

The gateway consumes upstream bytes asynchronously using httpx with `stream=True`. A separate SSE parser reconstructs complete events, handles fragmented UTF-8 and LF/CRLF line endings, and enforces a 16 KiB event-size limit. Only generated text is passed to the redactor; downstream SSE events are rebuilt rather than forwarding arbitrary provider metadata.

The gateway does not accumulate the entire response. Retained candidate state is bounded, and ordinary text can be delivered before upstream generation completes. Candidate classification can delay individual text fragments; temporary processing memory also depends on the incoming network chunk and event size. The manual check measures client-observed time to first content and completion, rather than internal model-token timing.

### Failure handling and cleanup

- Invalid client requests return a generic HTTP 422 response.
- Upstream connection failures and unsuccessful or unsupported upstream responses return HTTP 502; upstream timeouts before streaming return HTTP 504.
- Malformed, oversized, or interrupted upstream streams produce a generic SSE error without flushing unresolved text or sending a successful completion marker.
- Upstream responses and HTTP clients are closed on successful completion, failure, and downstream cancellation. Cancellation-shielded cleanup protects resource release.
- The upstream destination is server-configured. Client authorization headers are not forwarded, redirects are disabled, and raw upstream error details are not returned to clients.

## Testing

With both services running, execute the real HTTP integration check:

```bash
python -m task3_guardrail.manual_check
```

This compares direct provider output with gateway output, verifies all three redactions and preservation of ordinary text, measures first-content and completion times, and checks final event ordering. Client-disconnect recovery was also manually checked using a curl timeout.

Run the automated tests from the repository root (no running servers required):

```bash
python -m pytest task3_guardrail/tests/ -q
```

Confirmed result: **41 passing tests** (31 redactor tests and 10 gateway tests).

Coverage includes complete and split PII patterns, every two-delta split position for the test fixtures, single-character deltas, bounded candidate memory, per-request isolation, fragmented SSE/UTF-8, request forwarding, malformed streams, upstream failures, completion ordering, and upstream resource closure after cancellation.

## Scope and limitations

- Supports a single-choice, text-only OpenAI-style SSE subset. Tool-call deltas, multiple choices, compressed responses, and other unsupported formats are outside scope.
- Pattern-based redaction is not comprehensive DLP. Internationalized or unusual email formats, unformatted SSNs, obfuscated values, truncated PII, and numeric sequences outside the supported card patterns may not be fully detected. Card-like non-sensitive identifiers and oversized ordinary candidates may be redacted.
- The gateway inspects response text only, not client prompts. The mock provider uses fictional fixtures and is not a real inference service; a real provider would require compatible protocol handling and authentication.
- The upstream read timeout limits inactivity, not total generation duration. The implementation uses one HTTP client per request and does not include production authentication, request-size limits, concurrency quotas, or shared connection pooling.
- Memory and latency checks demonstrate bounded parser/redactor state and incremental delivery for the tested workload; they are not production throughput or load benchmarks.

The solution intentionally focuses on streaming interception, stateful PII detection, bounded buffering, and safe resource management without adding unrelated infrastructure.
