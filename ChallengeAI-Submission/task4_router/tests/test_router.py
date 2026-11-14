
"""Core tests for provider routing and fallback behavior."""

import asyncio
import json

import httpx
import pytest

from task4_router.models import CompletionRequest
from task4_router.router import ModelRouter, RoutingError


PRIMARY_URL = "http://primary.test/v1/completions"
SECONDARY_URL = "http://secondary.test/v1/completions"


@pytest.fixture
def completion_request():
    """A deterministic request shared by the routing tests."""

    return CompletionRequest(
        prompt="Hello",
        max_tokens=100,
    )


def success_response(model="test-model"):
    """Return a valid provider response with token usage."""

    return httpx.Response(
        status_code=200,
        json={
            "model": model,
            "choices": [
                {
                    "text": "mock response",
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        },
    )


def create_router(handler):
    """Create a router using an injected mock transport."""

    return ModelRouter(
        primary_url=PRIMARY_URL,
        secondary_url=SECONDARY_URL,

        # Short deadlines keep automated tests fast.
        # Production defaults remain three seconds.
        primary_timeout=0.05,
        secondary_timeout=0.05,

        transport=httpx.MockTransport(handler),
    )


# ---------------------------------------------------------
# Primary success and fallback
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_success_does_not_call_secondary(
    completion_request,
):
    """Primary success must result in exactly one attempt."""

    calls = []

    async def handler(request):
        calls.append(str(request.url))

        # Verify that the original completion request
        # reaches the provider.
        assert json.loads(request.content) == {
            "prompt": "Hello",
            "max_tokens": 100,
        }

        # The tenant's authorization credentials must
        # not be included in provider requests.
        assert "authorization" not in request.headers

        return success_response("primary-model")

    router = create_router(handler)

    result = await router.complete(completion_request)

    assert calls == [PRIMARY_URL]

    assert result.provider == "primary"
    assert result.fallback_reason is None
    assert result.model == "primary-model"
    assert result.text == "mock response"

    assert result.usage is not None
    assert result.usage.total_tokens == 7


@pytest.mark.asyncio
async def test_primary_429_calls_secondary_once(
    completion_request,
):
    """A primary 429 triggers exactly one secondary attempt."""

    calls = []

    async def handler(request):
        calls.append(str(request.url))

        if str(request.url) == PRIMARY_URL:
            return httpx.Response(
                429,
                json={"error": "PRIVATE_RATE_LIMIT_DETAILS"},
            )

        return success_response("secondary-model")

    router = create_router(handler)

    result = await router.complete(completion_request)

    assert calls == [PRIMARY_URL, SECONDARY_URL]

    assert result.provider == "secondary"
    assert result.fallback_reason == "rate_limit"
    assert result.model == "secondary-model"

    assert result.usage is not None
    assert result.usage.total_tokens == 7


@pytest.mark.asyncio
async def test_primary_timeout_cancels_before_fallback(
    completion_request,
):
    """Primary cancellation must finish before fallback."""

    calls = []
    primary_cancelled = asyncio.Event()

    async def handler(request):
        url = str(request.url)
        calls.append(url)

        if url == PRIMARY_URL:
            try:
                # Exceed the configured 50ms test deadline.
                await asyncio.sleep(10)

            except asyncio.CancelledError:
                # Prove that the awaited primary operation
                # was actually cancelled.
                primary_cancelled.set()
                raise

        # The secondary must not start while the primary
        # coroutine is still executing.
        assert primary_cancelled.is_set()

        return success_response("secondary-model")

    router = create_router(handler)

    result = await router.complete(completion_request)

    assert primary_cancelled.is_set()
    assert calls == [PRIMARY_URL, SECONDARY_URL]

    assert result.provider == "secondary"
    assert result.fallback_reason == "timeout"


# ---------------------------------------------------------
# Total response deadline
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_covers_entire_response_body(
    completion_request,
):
    """A fast response header must not bypass the deadline."""

    calls = []
    body_cancelled = asyncio.Event()
    body_closed = asyncio.Event()

    class SlowResponseBody(httpx.AsyncByteStream):
        """Send initial data, then stall the response body."""

        async def __aiter__(self):
            try:
                # Headers and initial bytes are available.
                yield b'{"model": "slow-primary", '

                # But the full response is delayed.
                await asyncio.sleep(10)

                yield b'"choices": []}'

            except asyncio.CancelledError:
                body_cancelled.set()
                raise

        async def aclose(self):
            body_closed.set()

    async def handler(request):
        url = str(request.url)
        calls.append(url)

        if url == PRIMARY_URL:
            return httpx.Response(
                status_code=200,
                headers={
                    "content-type": "application/json"
                },
                stream=SlowResponseBody(),
            )

        # The primary response stream must have been
        # cancelled and closed before we attempt fallback.
        assert body_cancelled.is_set()
        assert body_closed.is_set()

        return success_response("secondary-model")

    router = create_router(handler)

    result = await router.complete(completion_request)

    assert calls == [PRIMARY_URL, SECONDARY_URL]

    assert body_cancelled.is_set()
    assert body_closed.is_set()

    assert result.provider == "secondary"
    assert result.fallback_reason == "timeout"


# ---------------------------------------------------------
# Sanitized provider errors
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_500_does_not_trigger_fallback(
    completion_request,
):
    """Noneligible primary errors must not cause fallback."""

    calls = []

    async def handler(request):
        calls.append(str(request.url))

        return httpx.Response(
            500,
            json={
                "error": "PRIVATE_UPSTREAM_STACK_TRACE"
            },
        )

    router = create_router(handler)

    with pytest.raises(RoutingError) as caught:
        await router.complete(completion_request)

    error = caught.value

    assert calls == [PRIMARY_URL]

    assert error.status_code == 502
    assert error.code == "provider_error"

    assert "PRIVATE" not in error.message
    assert "STACK_TRACE" not in str(error)


@pytest.mark.asyncio
async def test_secondary_failure_is_sanitized(
    completion_request,
):
    """Failed fallback must not leak upstream error details."""

    calls = []

    async def handler(request):
        url = str(request.url)
        calls.append(url)

        if url == PRIMARY_URL:
            return httpx.Response(429)

        return httpx.Response(
            500,
            text="PRIVATE_SECONDARY_STACK_TRACE",
        )

    router = create_router(handler)

    with pytest.raises(RoutingError) as caught:
        await router.complete(completion_request)

    error = caught.value

    assert calls == [PRIMARY_URL, SECONDARY_URL]

    assert error.status_code == 502
    assert error.code == "provider_error"
    assert "PRIVATE" not in str(error)


@pytest.mark.asyncio
async def test_secondary_timeout_returns_504(
    completion_request,
):
    """The backup provider must also have a bounded deadline."""

    calls = []

    async def handler(request):
        url = str(request.url)
        calls.append(url)

        if url == PRIMARY_URL:
            return httpx.Response(429)

        await asyncio.sleep(10)

        return success_response("secondary-model")

    router = create_router(handler)

    with pytest.raises(RoutingError) as caught:
        await router.complete(completion_request)

    error = caught.value

    assert calls == [PRIMARY_URL, SECONDARY_URL]

    assert error.status_code == 504
    assert error.code == "provider_timeout"


# ---------------------------------------------------------
# Response validation and token accounting
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_primary_response_does_not_fallback(
    completion_request,
):
    """HTTP 200 is not enough for a valid completion."""

    calls = []

    async def handler(request):
        calls.append(str(request.url))

        return httpx.Response(
            200,
            json={
                "model": "primary-model",
                "choices": [],
                "private_details": "do not expose",
            },
        )

    router = create_router(handler)

    with pytest.raises(RoutingError) as caught:
        await router.complete(completion_request)

    assert calls == [PRIMARY_URL]

    assert caught.value.status_code == 502
    assert caught.value.code == "provider_error"
    assert "private_details" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        None,
        {
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": -7,
        },
        {
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 100,
        },
        {
            "promt_tokens": 500_000,
            "completion_tokens": 1,
            "total_tokens": 500_001,
        },
    ],
    ids=[
        "missing_usage",
        "negative_usage",
        "inconsistent_usage",
        "oversized_usage",
    ],
)
async def test_invalid_usage_does_not_corrupt_accounting(
    completion_request,
    usage,
):
    """Keep a valid completion but reject unusable usage."""

    async def handler(request):
        payload = success_response().json()

        if usage is None:
            payload.pop("usage")
        else:
            payload["usage"] = usage

        return httpx.Response(
            200,
            json=payload,
        )

    router = create_router(handler)

    result = await router.complete(completion_request)

    assert result.provider == "primary"
    assert result.text == "mock response"

    # The future gateway must retain the original
    # token reservation when usage is unavailable.
    assert result.usage is None


# ---------------------------------------------------------
# External cancellation
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_external_cancellation_does_not_fallback(
    completion_request,
):
    """Caller cancellation must propagate, not start fallback."""

    calls = []
    primary_started = asyncio.Event()
    primary_cancelled = asyncio.Event()

    async def handler(request):
        calls.append(str(request.url))
        primary_started.set()

        try:
            await asyncio.sleep(10)

        except asyncio.CancelledError:
            primary_cancelled.set()
            raise

    # Use a longer deadline so this test exercises caller
    # cancellation rather than the router's own timeout.
    router = ModelRouter(
        primary_url=PRIMARY_URL,
        secondary_url=SECONDARY_URL,
        primary_timeout=5.0,
        secondary_timeout=5.0,
        transport=httpx.MockTransport(handler),
    )

    task = asyncio.create_task(
        router.complete(completion_request)
    )

    try:
        # Wait until we know the primary has started.
        await asyncio.wait_for(
            primary_started.wait(),
            timeout=1.0,
        )

        # Simulate the caller cancelling its operation.
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert primary_cancelled.is_set()

        # Cancellation must not trigger a second request.
        assert calls == [PRIMARY_URL]

    finally:
        # Ensure the test never leaves an unfinished task
        # behind if an earlier assertion fails.
        if not task.done():
            task.cancel()

            await asyncio.gather(
                task,
                return_exceptions=True,
            )