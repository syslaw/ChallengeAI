
import json
import os
from collections.abc import AsyncIterator

import anyio
import httpx

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from task3_guardrail.mock_provider import (
    ChatRequest,
    create_sse_event,
)
from task3_guardrail.redactor import StreamingRedactor


app = FastAPI(title="LLM Streaming PII Gateway")


# The upstream destination is controlled by the server,
# not by individual client requests.
UPSTREAM_URL = os.getenv(
    "LLM_UPSTREAM_URL",
    "http://127.0.0.1:8003/v1/chat/completions",
)

# Limit the size of each SSE event independently of the
# redactor's 320-character pending text buffer.
MAX_EVENT_BYTES = 16 * 1024

# Prevent a stalled upstream connection from waiting forever.
UPSTREAM_TIMEOUT = httpx.Timeout(
    10.0,
    connect=3.0,
)


# ---------------------------------------------------------
# 1. Parse incoming SSE events
# ---------------------------------------------------------

async def iter_sse_data(
    response: httpx.Response,
) -> AsyncIterator[str]:
    """
    Read incoming bytes and yield complete SSE data payloads.

    Network chunks may split SSE lines, JSON payloads,
    or UTF-8 characters at arbitrary positions.
    """

    pending_line = bytearray()
    data_lines = []
    event_size = 0

    # Read bytes as they arrive. Do not request a fixed
    # chunk size that could delay small streaming events.
    async for chunk in response.aiter_raw():

        # Split on newline bytes while retaining an
        # incomplete final line between network chunks.
        parts = chunk.split(b"\n")

        for index, part in enumerate(parts):

            # Check the event size before retaining bytes.
            event_size += len(part)

            if event_size > MAX_EVENT_BYTES:
                raise ValueError("SSE event exceeds size limit")

            pending_line.extend(part)

            # The last part has no terminating newline.
            # Retain it until more network data arrives.
            if index == len(parts) - 1:
                continue

            event_size += 1

            if event_size > MAX_EVENT_BYTES:
                raise ValueError("SSE event exceeds size limit")

            # A complete line can now safely be decoded.
            # Remove CR for providers using CRLF endings.
            line = bytes(pending_line).rstrip(b"\r")
            pending_line.clear()

            # An empty line marks the end of an SSE event.
            if not line:
                payload = "\n".join(data_lines)

                data_lines.clear()
                event_size = 0

                if payload:
                    yield payload

                continue

            # SSE comments are not generated model content.
            if line.startswith(b":"):
                continue

            # Our supported SSE format uses data fields.
            if line.startswith(b"data:"):
                value = line[5:]

                # SSE allows one optional space after
                # the field's colon.
                if value.startswith(b" "):
                    value = value[1:]

                data_lines.append(value.decode("utf-8"))

            # Other SSE fields are not required by our
            # supported provider format and are ignored.

    # A partially received event is not safe to process.
    if pending_line or data_lines:
        raise ValueError("Incomplete SSE event")


# ---------------------------------------------------------
# 2. Redact the upstream content stream
# ---------------------------------------------------------

async def sanitize_stream(
    response: httpx.Response,
) -> AsyncIterator[str]:
    """
    Convert upstream SSE events into sanitized SSE events.

    One redactor instance is created per request so that
    pending text is never shared between client streams.
    """

    redactor = StreamingRedactor()
    finish_reason = None

    try:
        async for payload in iter_sse_data(response):

            # The provider has finished generating text.
            if payload == "[DONE]":

                # Our supported provider sends a finish
                # event before the final [DONE] marker.
                if finish_reason is None:
                    raise ValueError("Missing finish event")

                # Classify any final unresolved text BEFORE
                # sending completion events to the client.
                remaining = redactor.finish()

                if remaining:
                    yield create_sse_event(remaining)

                # Rebuild the completion event instead of
                # forwarding uninspected upstream metadata.
                completion = {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": finish_reason,
                        }
                    ]
                }

                yield f"data: {json.dumps(completion)}\n\n"
                yield "data: [DONE]\n\n"
                return

            # Decode the complete SSE data payload.
            event = json.loads(payload)

            if not isinstance(event, dict):
                raise ValueError("Invalid stream event")

            choices = event.get("choices")

            # This gateway supports one text completion.
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Unsupported stream choices")

            choice = choices[0]

            if not isinstance(choice, dict):
                raise ValueError("Invalid stream choice")

            if choice.get("index", 0) != 0:
                raise ValueError("Unsupported choice index")

            delta = choice.get("delta")

            if not isinstance(delta, dict):
                raise ValueError("Invalid stream delta")

            # Reject tool calls and other unsupported
            # generated content rather than pass them through.
            if set(delta) - {"role", "content"}:
                raise ValueError("Unsupported delta fields")

            if finish_reason is not None:
                raise ValueError("Unexpected event after completion")

            # Preserve the supported assistant role event.
            if "role" in delta:
                if delta["role"] != "assistant":
                    raise ValueError("Unsupported stream role")

                role_event = {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant"},
                            "finish_reason": None,
                        }
                    ]
                }

                yield f"data: {json.dumps(role_event)}\n\n"

            # Only generated text is sent to the redactor.
            content = delta.get("content")

            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("Invalid content delta")

                safe_text = redactor.feed(content)

                # An empty result is normal when a potential
                # PII candidate is still being buffered.
                if safe_text:
                    yield create_sse_event(safe_text)

            # Retain the finish reason until all pending
            # text has been classified and released.
            reason = choice.get("finish_reason")

            if reason is not None:
                if reason not in ("stop", "length"):
                    raise ValueError("Unsupported finish reason")

                finish_reason = reason

        # A successful stream must explicitly finish.
        # Do not flush unresolved text on an unexpected EOF.
        raise ValueError("Upstream stream ended unexpectedly")

    except (
        ValueError,
        httpx.HTTPError,
        RecursionError,
    ):
        # HTTP headers have already been sent at this point.
        # Return a sanitized SSE error instead of raw upstream
        # data, exceptions, or a misleading success marker.
        error = {
            "error": {
                "message": "Upstream stream failed",
                "type": "upstream_stream_error",
            }
        }

        yield f"data: {json.dumps(error)}\n\n"


# ---------------------------------------------------------
# 3. Ensure upstream connections are always closed
# ---------------------------------------------------------

async def close_upstream(
    client: httpx.AsyncClient,
    response: httpx.Response | None,
) -> None:
    """
    Release the upstream HTTP response and client.

    Shield cleanup from cancellation so a downstream
    disconnect does not interrupt connection cleanup.
    """

    with anyio.CancelScope(shield=True):
        try:
            if response is not None:
                await response.aclose()
        finally:
            await client.aclose()


class ManagedStreamingResponse(StreamingResponse):
    """
    StreamingResponse that owns the upstream connection.

    Cleanup runs after normal completion, errors, or
    cancellation caused by a client disconnect.
    """

    def __init__(
        self,
        content: AsyncIterator[str],
        upstream_client: httpx.AsyncClient,
        upstream_response: httpx.Response,
    ):
        self.upstream_client = upstream_client
        self.upstream_response = upstream_response

        super().__init__(
            content,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
            },
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await close_upstream(
                self.upstream_client,
                self.upstream_response,
            )


# ---------------------------------------------------------
# 4. Sanitize request-validation errors
# ---------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_error(
    request: Request,
    exc: RequestValidationError,
):
    # FastAPI's default validation errors can include
    # submitted values. Do not echo potentially sensitive
    # prompt content in gateway error responses.
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Invalid streaming chat request"
        },
    )


# ---------------------------------------------------------
# 5. Gateway endpoint
# ---------------------------------------------------------

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    """
    Forward a streaming chat request to the LLM provider
    and return its sanitized response to the client.
    """

    client = httpx.AsyncClient(
        timeout=UPSTREAM_TIMEOUT,

        # Do not automatically use system proxy settings
        # or follow redirects to unexpected destinations.
        trust_env=False,
        follow_redirects=False,
    )

    upstream = None
    handed_off = False

    try:
        # Build the upstream request from validated input.
        # No client Authorization headers are forwarded.
        upstream_request = client.build_request(
            method="POST",
            url=UPSTREAM_URL,
            json=request.model_dump(),
            headers={
                "Accept": "text/event-stream",

                # Keep the upstream wire format simple.
                "Accept-Encoding": "identity",
            },
        )

        # IMPORTANT: stream=True prevents httpx from
        # downloading the entire response into memory.
        upstream = await client.send(
            upstream_request,
            stream=True,
        )

        # Validate the upstream response BEFORE sending
        # HTTP response headers to our client.
        upstream.raise_for_status()

        content_type = upstream.headers.get(
            "content-type", ""
        ).split(";", 1)[0].strip().lower()

        if content_type != "text/event-stream":
            raise ValueError("Unexpected upstream content type")

        encoding = upstream.headers.get(
            "content-encoding", "identity"
        ).lower()

        if encoding != "identity":
            raise ValueError("Unsupported upstream encoding")

        # Transfer connection ownership to the streaming
        # response. It will close the connection after use.
        downstream = ManagedStreamingResponse(
            sanitize_stream(upstream),
            client,
            upstream,
        )

        handed_off = True
        return downstream

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Upstream provider timed out",
        ) from None

    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="Upstream provider unavailable",
        ) from None

    finally:
        # If we failed before creating a downstream
        # streaming response, this endpoint still owns
        # the upstream connection and must close it.
        if not handed_off:
            await close_upstream(client, upstream)
