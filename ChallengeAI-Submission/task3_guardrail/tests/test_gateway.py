
import asyncio
import json

import httpx
import pytest

from task3_guardrail import gateway
from task3_guardrail.mock_provider import (
    ChatRequest,
    create_sse_event,
)


# Save the original client class before monkeypatching it.
# Our downstream test client must still use ASGITransport.
REAL_ASYNC_CLIENT = httpx.AsyncClient


REQUEST = {
    "model": "mock-llm",
    "messages": [
        {
            "role": "user",
            "content": "Provide customer information.",
        }
    ],
    "stream": True,
}


FINISH_EVENT = (
    'data: {"choices":[{"index":0,'
    '"delta":{},"finish_reason":"stop"}]}\n\n'
)

DONE_EVENT = "data: [DONE]\n\n"


class TrackingStream(httpx.AsyncByteStream):
    """
    A simulated upstream HTTP body.

    Records whether the gateway explicitly closes it.
    It can also pause indefinitely to simulate a provider
    that has not finished generating its response.
    """

    def __init__(self, chunks, stall=False):

        self.chunks = chunks
        self.stall = stall
        self.closed = False

    async def __aiter__(self):

        for chunk in self.chunks:
            yield chunk

        if self.stall:
            # Used only by the disconnect test.
            # The downstream disconnect should cancel
            # this wait before generation completes.
            await asyncio.Event().wait()

    async def aclose(self):

        self.closed = True


@pytest.fixture
def install_upstream(monkeypatch):
    """
    Replace the gateway's upstream HTTP client with one
    that uses our simulated provider.

    Return the created clients so tests can verify that
    the gateway closes them correctly.
    """

    def install(handler):

        clients = []

        def client_factory(*args, **kwargs):

            kwargs["transport"] = httpx.MockTransport(
                handler
            )

            client = REAL_ASYNC_CLIENT(*args, **kwargs)

            clients.append(client)

            return client

        # Only the gateway's client construction is
        # intercepted. Tests use REAL_ASYNC_CLIENT below.
        monkeypatch.setattr(
            gateway.httpx,
            "AsyncClient",
            client_factory,
        )

        return clients

    return install


async def call_gateway(payload=None):
    """
    Send an HTTP request directly to the FastAPI app.

    No Uvicorn server or local port is required.
    """

    if payload is None:
        payload = REQUEST

    transport = httpx.ASGITransport(app=gateway.app)

    async with REAL_ASYNC_CLIENT(
        transport=transport,
        base_url="http://test",
    ) as client:

        return await client.post(
            "/v1/chat/completions",
            json=payload,
        )


def make_stream(*text_deltas):
    """
    Build a successful upstream SSE stream from text.
    """

    events = [
        create_sse_event(text)
        for text in text_deltas
    ]

    events.extend([
        FINISH_EVENT,
        DONE_EVENT,
    ])

    return TrackingStream([
        event.encode("utf-8")
        for event in events
    ])


def parse_downstream(body):
    """
    Extract generated text and event ordering from the
    gateway's returned SSE body.
    """

    chunks = []
    events = []

    for line in body.splitlines():

        if not line.startswith("data:"):
            continue

        payload = line[5:].strip()

        if payload == "[DONE]":
            events.append("done")
            continue

        event = json.loads(payload)

        if "error" in event:
            events.append("error")
            continue

        choice = event["choices"][0]

        content = choice["delta"].get("content")

        if content:
            chunks.append(content)
            events.append("content")

        if choice.get("finish_reason"):
            events.append("finish")

    return "".join(chunks), events

@pytest.mark.asyncio
async def test_forwarding_and_redaction(install_upstream):
    """
    Verify the original request reaches the provider
    and all supported PII is removed from its response.
    """

    stream = make_stream(
        "Email: ",
        "alice@",
        "example",
        ".com. SSN: ",
        "123-45-",
        "6789. Card: ",
        "4111 ",
        "1111 ",
        "1111 ",
        "1111. Finished.",
    )

    requests = []

    async def handler(request):

        requests.append(request)

        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream"
            },
            stream=stream,
        )

    clients = install_upstream(handler)

    response = await call_gateway()

    assert response.status_code == 200

    assert response.headers["content-type"].startswith(
        "text/event-stream"
    )

    # The gateway must forward the original validated
    # request, including the user's actual prompt.
    assert len(requests) == 1

    assert json.loads(
        requests[0].content
    ) == REQUEST

    assert str(requests[0].url) == gateway.UPSTREAM_URL

    text, events = parse_downstream(response.text)

    assert text == (
        "Email: [REDACTED]. SSN: [REDACTED]. "
        "Card: [REDACTED]. Finished."
    )

    assert text.count("[REDACTED]") == 3

    # Completion must follow the final buffered text.
    assert events[-3:] == [
        "content",
        "finish",
        "done",
    ]

    # Verify actual connection cleanup on success.
    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_fragmented_sse_and_utf8(install_upstream):
    """
    Verify that HTTP network chunks can split SSE
    boundaries, JSON, and UTF-8 characters.
    """

    payload = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "content": (
                        "Bonjour café: alice@example.com"
                    )
                },
                "finish_reason": None,
            }
        ]
    }

    # ensure_ascii=False forces the accented character
    # to appear as real multibyte UTF-8 on the wire.
    event = (
        f"data: {json.dumps(payload, ensure_ascii=False)}"
        "\r\n\r\n"
    )

    wire = (
        event + FINISH_EVENT + DONE_EVENT
    ).encode("utf-8")

    # Deliver one byte at a time. This forces the parser
    # to handle every possible transport boundary.
    stream = TrackingStream([
        bytes([byte])
        for byte in wire
    ])

    async def handler(request):

        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream"
            },
            stream=stream,
        )

    install_upstream(handler)

    response = await call_gateway()

    assert response.status_code == 200

    text, events = parse_downstream(response.text)

    assert text == "Bonjour café: [REDACTED]"

    assert events[-2:] == ["finish", "done"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_event",
    [
        # Invalid JSON.
        "data: not-valid-json\n\n",

        # Unsupported generated tool-call content.
        (
            'data: {"choices":[{"index":0,'
            '"delta":{"tool_calls":[]},'
            '"finish_reason":null}]}\n\n'
        ),

        # A finish event without a final [DONE].
        FINISH_EVENT,
    ],
)
async def test_invalid_stream_fails_safely(
    install_upstream,
    bad_event,
):
    """
    A broken upstream stream must not expose pending
    unclassified PII or announce successful completion.
    """

    stream = TrackingStream([
        create_sse_event("Safe text ").encode("utf-8"),

        # This candidate is deliberately unresolved.
        create_sse_event("alice@").encode("utf-8"),

        bad_event.encode("utf-8"),
    ])

    async def handler(request):

        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream"
            },
            stream=stream,
        )

    clients = install_upstream(handler)

    response = await call_gateway()

    # HTTP headers have already been sent by the time
    # the streaming error occurs.
    assert response.status_code == 200

    text, events = parse_downstream(response.text)

    assert text == "Safe text "

    assert "alice@" not in response.text
    assert "not-valid-json" not in response.text
    assert "tool_calls" not in response.text

    assert events[-1] == "error"

    # A failed stream must not look like a successful
    # generation to the downstream client.
    assert "done" not in events
    assert "finish" not in events

    assert "Upstream stream failed" in response.text

    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_oversized_sse_event(install_upstream):
    """
    Reject an oversized event before accumulating an
    unbounded SSE payload.
    """

    oversized = (
        b"data: "
        + b"a" * gateway.MAX_EVENT_BYTES
    )

    stream = TrackingStream([
        oversized,
    ])

    async def handler(request):

        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream"
            },
            stream=stream,
        )

    clients = install_upstream(handler)

    response = await call_gateway()

    text, events = parse_downstream(response.text)

    assert text == ""
    assert events == ["error"]

    assert "Upstream stream failed" in response.text

    assert stream.closed
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_upstream_connection_failure(
    install_upstream,
):
    """
    Verify sanitized HTTP 502 handling when the provider
    cannot be reached.
    """

    async def handler(request):

        raise httpx.ConnectError(
            "Internal upstream connection information",
            request=request,
        )

    clients = install_upstream(handler)

    response = await call_gateway()

    assert response.status_code == 502

    assert response.json() == {
        "detail": "Upstream provider unavailable"
    }

    assert "Internal upstream" not in response.text

    # The client must be closed even if no upstream
    # response was successfully created.
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_upstream_non_success_status(
    install_upstream,
):
    """
    Verify that upstream HTTP error bodies are not
    forwarded to the client.
    """

    stream = TrackingStream([
        b"Private upstream error details"
    ])

    async def handler(request):

        return httpx.Response(
            500,
            stream=stream,
        )

    clients = install_upstream(handler)

    response = await call_gateway()

    assert response.status_code == 502

    assert "Private upstream" not in response.text

    assert stream.closed
    assert clients[0].is_closed

@pytest.mark.asyncio
async def test_invalid_request_not_forwarded(
    install_upstream,
):
    """
    A non-streaming request must be rejected before any
    upstream HTTP client or request is created.
    """

    async def handler(request):

        pytest.fail(
            "Invalid request reached upstream"
        )

    clients = install_upstream(handler)

    payload = {
        **REQUEST,
        "stream": False,
    }

    response = await call_gateway(payload)

    assert response.status_code == 422

    assert response.json() == {
        "detail": "Invalid streaming chat request"
    }

    assert clients == []

@pytest.mark.asyncio
async def test_client_disconnect_closes_upstream(
    install_upstream,
):
    """
    Verify the upstream response and HTTP client close
    when the downstream client abandons an active stream.

    Also verify content is delivered before the upstream
    provider finishes generation.
    """

    stream = TrackingStream(
        chunks=[
            create_sse_event(
                "Safe text "
            ).encode("utf-8")
        ],

        # The provider deliberately never completes.
        stall=True,
    )

    async def handler(request):

        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream"
            },
            stream=stream,
        )

    clients = install_upstream(handler)

    # Obtain the actual streaming response from our
    # gateway endpoint, including its connection owner.
    response = await gateway.chat_completions(
        ChatRequest(**REQUEST)
    )

    first_content_received = asyncio.Event()

    sent_bodies = []

    async def send(message):
        """
        Simulate the ASGI server sending response data
        to a connected downstream client.
        """

        if message["type"] == "http.response.body":

            body = message.get("body", b"")

            if body:

                sent_bodies.append(body)

                # Disconnect only after actual content
                # has been delivered to the client.
                first_content_received.set()

    async def receive():
        """
        Wait for the first content event, then simulate
        the downstream client closing its connection.
        """

        await first_content_received.wait()

        return {
            "type": "http.disconnect"
        }

    # Use ASGI's disconnect-event mechanism directly.
    # This exercises StreamingResponse's cancellation
    # handling without requiring a network server.
    scope = {
        "type": "http",
        "asgi": {
            "version": "3.0",
            "spec_version": "2.0",
        },
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "root_path": "",
        "query_string": b"",
        "headers": [],
    }

    # Ensure a broken cancellation implementation cannot
    # cause this automated test to hang indefinitely.
    await asyncio.wait_for(
        response(scope, receive, send),
        timeout=3.0,
    )

    # The client received real content even though the
    # upstream provider never finished generating.
    assert first_content_received.is_set()

    body = b"".join(sent_bodies).decode("utf-8")

    assert "Safe text " in body

    assert "[DONE]" not in body

    # Critical assertions: both resources actually closed.
    assert stream.closed
    assert clients[0].is_closed
