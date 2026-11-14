
"""Initial integration tests for the Task 4 gateway."""

import asyncio
import json
import sqlite3
from contextlib import closing

import httpx
import pytest
import pytest_asyncio

from task4_router.gateway import create_app
from task4_router.limiter import TokenRateLimiter
from task4_router.router import ModelRouter


PRIMARY_URL = "http://primary.test/v1/completions"
SECONDARY_URL = "http://secondary.test/v1/completions"

TENANT_A_HEADERS = {
    "Authorization": "Bearer dev-tenant-a-key"
}

TENANT_B_HEADERS = {
    "Authorization": "Bearer dev-tenant-b-key"
}

COMPLETION_PAYLOAD = {
    "prompt": "Hello",
    "max_tokens": 100,
}


@pytest_asyncio.fixture
async def gateway_client(tmp_path):
    """Create an isolated gateway with real token accounting."""

    calls = []

    # Shared test configuration allows individual tests
    # to simulate provider behavior without changing
    # the gateway's implementation.

    provider_state = {
        "primary_status": 200,
        "secondary_status": 200,
        "primary_delay": 0.0,
        "include_usage": True,

        # Optional synchronization events used only by
        # the concurrent gateway admission test.
        "primary_started": None,
        "release_primary": None,
    }

    async def provider_handler(request):
        """Simulate provider responses and controlled delays."""

        url = str(request.url)
        calls.append(url)

        # Tenant credentials must never reach providers.
        assert "authorization" not in request.headers

        is_primary = url == PRIMARY_URL

        # -------------------------------------------------
        # Simulate primary provider delay or synchronization
        # -------------------------------------------------

        if is_primary:
            started = provider_state["primary_started"]
            release = provider_state["release_primary"]

            # Allow the concurrency test to observe when
            # a request has passed admission and reached
            # the primary provider.
            if started is not None:
                started.set()

            # Optionally hold the first provider request
            # open while a competing gateway request arrives.
            if release is not None:
                await release.wait()

            delay = provider_state["primary_delay"]

            if delay > 0:
                await asyncio.sleep(delay)

        # -------------------------------------------------
        # Simulate provider HTTP errors
        # -------------------------------------------------

        status = (
            provider_state["primary_status"]
            if is_primary
            else provider_state["secondary_status"]
        )

        if status != 200:
            return httpx.Response(
                status,
                json={
                    "error": (
                        "PRIVATE_UPSTREAM_ERROR: "
                        "internal provider details"
                    )
                },
            )

        # -------------------------------------------------
        # Produce deterministic successful completion
        # -------------------------------------------------

        payload = json.loads(request.content)

        prompt_tokens = len(
            payload["prompt"].encode("utf-8")
        )

        completion_tokens = min(
            payload["max_tokens"],
            8,
        )

        response_payload = {
            "model": (
                "primary-model"
                if is_primary
                else "secondary-model"
            ),
            "choices": [
                {
                    "text": "mock completion",
                    "finish_reason": "stop",
                }
            ],
        }

        # Missing usage is a supported provider outcome.
        # The gateway should keep its initial reservation.
        if provider_state["include_usage"]:
            response_payload["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": (
                    prompt_tokens + completion_tokens
                ),
            }

        return httpx.Response(
            200,
            json=response_payload,
        )


    # Real SQLite database, isolated for each test.
    limiter = TokenRateLimiter(
        database_path=tmp_path / "gateway.sqlite3",
        # Shorten the timeout to make deliberate database lock tests faster...
        busy_timeout=0.05,
    )

    # Real routing implementation with mocked HTTP I/O.
    router = ModelRouter(
        primary_url=PRIMARY_URL,
        secondary_url=SECONDARY_URL,
        #primary_timeout=0.1,
        #secondary_timeout=0.1,
        primary_timeout=1.0,
        secondary_timeout=1.0,
        transport=httpx.MockTransport(provider_handler),
    )

    # Inject test dependencies rather than monkeypatching
    # the application's global configuration.
    app = create_app(
        limiter=limiter,
        router=router,
        tenant_keys={
            "dev-tenant-a-key": "tenant-a",
            "dev-tenant-b-key": "tenant-b",
        },
    )

    # HTTPX ASGITransport does not automatically execute
    # application lifespan startup/shutdown.
    #
    # Explicitly enter the lifespan so the SQLite database
    # is initialized exactly as it is during normal startup.
    async with app.router.lifespan_context(app):

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            ),
            base_url="http://gateway.test",
        ) as client:

            yield (
                client,
                limiter,
                calls,
                provider_state,
            )


# ---------------------------------------------------------
# Initial gateway integration checkpoints
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_primary_success_and_reconciliation(
    gateway_client,
):
    """Successful completion reconciles reserved tokens."""

    client, limiter, calls, _ = gateway_client

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["model"] == "primary-model"
    assert payload["provider"] == "primary"
    assert payload["fallback_reason"] is None
    assert payload["usage"]["total_tokens"] == 13

    # The request initially reserved 105 tokens.
    # Actual provider usage reduces the charge to 13.
    assert limiter.get_usage("tenant-a") == 13

    assert calls == [PRIMARY_URL]


@pytest.mark.asyncio
async def test_gateway_invalid_api_key(
    gateway_client,
):
    """Unauthorized requests cannot reserve or route."""

    client, limiter, calls, _ = gateway_client

    response = await client.post(
        "/v1/completions",
        headers={
            "Authorization": "Bearer unrecognized-key"
        },
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 401

    assert response.json() == {
        "error": {
            "code": "invalid_api_key",
            "message": "Missing or invalid API key.",
        }
    }

    assert response.headers["www-authenticate"] == "Bearer"

    assert limiter.get_usage("tenant-a") == 0
    assert limiter.get_usage("tenant-b") == 0

    # Authentication failure must never contact providers.
    assert calls == []


@pytest.mark.asyncio
async def test_gateway_invalid_request_is_sanitized(
    gateway_client,
):
    """Validation failure must not expose client input."""

    client, limiter, calls, _ = gateway_client

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json={
            "prompt": "PRIVATE_PROMPT_CONTENT",
            "max_tokens": -100,
        },
    )

    assert response.status_code == 400

    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "Invalid completion request.",
        }
    }

    assert "PRIVATE_PROMPT_CONTENT" not in response.text

    assert limiter.get_usage("tenant-a") == 0
    assert calls == []


@pytest.mark.asyncio
async def test_gateway_local_quota_rejection(
    gateway_client,
):
    """Local 429 must not trigger provider fallback."""

    client, limiter, calls, _ = gateway_client

    # Fill tenant A's budget before sending a completion.
    assert limiter.reserve(
        "tenant-a",
        50_000,
    ).allowed

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 429

    assert response.json() == {
        "error": {
            "code": "rate_limit_exceeded",
            "message": "Tenant token limit exceeded.",
        }
    }

    # No additional tokens were reserved.
    assert limiter.get_usage("tenant-a") == 50_000

    # Neither provider may receive a rejected request.
    assert calls == []

    # Tenant B must remain independently usable.
    other_response = await client.post(
        "/v1/completions",
        headers=TENANT_B_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert other_response.status_code == 200
    assert limiter.get_usage("tenant-b") == 13

    assert calls == [PRIMARY_URL]


@pytest.mark.asyncio
async def test_gateway_provider_429_fallback(
    gateway_client,
):
    """Provider 429 triggers fallback using one reservation."""

    client, limiter, calls, provider_state = gateway_client

    # Configure the primary provider to reject the request.
    provider_state["primary_status"] = 429

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["provider"] == "secondary"
    assert payload["fallback_reason"] == "rate_limit"
    assert payload["usage"]["total_tokens"] == 13

    assert calls == [
        PRIMARY_URL,
        SECONDARY_URL,
    ]

    # A single logical request was charged.
    # The explicit primary rejection did not introduce
    # a second reservation or additional token charge.
    assert limiter.get_usage("tenant-a") == 13


# ---------------------------------------------------------
# Gateway accounting under fallback and failure
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_timeout_fallback_retains_reservation(
    gateway_client,
):
    """Timeout fallback must not refund uncertain usage."""

    client, limiter, calls, provider_state = gateway_client

    # The fixture's primary deadline is one second.
    # Delay the primary long enough to trigger fallback.
    provider_state["primary_delay"] = 5.0

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["provider"] == "secondary"
    assert payload["fallback_reason"] == "timeout"

    # The successful backup provider reports 13 tokens.
    assert payload["usage"]["total_tokens"] == 13

    # The primary's actual usage is uncertain, so the
    # gateway must retain the original 105-token reservation.
    assert limiter.get_usage("tenant-a") == 105

    # Exactly one primary and one secondary attempt.
    assert calls == [
        PRIMARY_URL,
        SECONDARY_URL,
    ]

    # Confirm fallback created only one database record.
    with closing(
        sqlite3.connect(limiter.database_path)
    ) as connection:

        rows = connection.execute(
            """
            SELECT tokens
            FROM token_events
            WHERE tenant_id = ?
            """,
            ("tenant-a",),
        ).fetchall()

    assert rows == [(105,)]


@pytest.mark.asyncio
async def test_gateway_missing_usage_retains_reservation(
    gateway_client,
):
    """A valid completion without usage must retain quota."""

    client, limiter, calls, provider_state = gateway_client

    # Return a valid completion without usage metadata.
    provider_state["include_usage"] = False

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["provider"] == "primary"
    assert payload["fallback_reason"] is None
    assert payload["usage"] is None

    # Without trustworthy actual usage, the gateway
    # cannot safely refund part of the reservation.
    assert limiter.get_usage("tenant-a") == 105

    assert calls == [PRIMARY_URL]


@pytest.mark.asyncio
async def test_gateway_primary_failure_retains_reservation(
    gateway_client,
):
    """Primary failure must be sanitized without a refund."""

    client, limiter, calls, provider_state = gateway_client

    # HTTP 500 is not an eligible fallback condition.
    provider_state["primary_status"] = 500

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 502

    assert response.json() == {
        "error": {
            "code": "provider_error",
            "message": (
                "The model provider could not "
                "complete the request."
            ),
        }
    }

    # Raw provider errors must never reach the caller.
    assert "PRIVATE_UPSTREAM_ERROR" not in response.text
    assert "internal provider details" not in response.text

    # The secondary must not be called for primary 500.
    assert calls == [PRIMARY_URL]

    # Failed requests retain their original reservation.
    assert limiter.get_usage("tenant-a") == 105


# ---------------------------------------------------------
# Provider and database error sanitization
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_secondary_failure_is_sanitized(
    gateway_client,
):
    """Failed fallback returns a generic gateway error."""

    client, limiter, calls, provider_state = gateway_client

    # Primary refuses the request, making it eligible
    # for one secondary attempt.
    provider_state["primary_status"] = 429

    # The secondary also fails.
    provider_state["secondary_status"] = 500

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 502

    assert response.json() == {
        "error": {
            "code": "provider_error",
            "message": (
                "The model provider could not "
                "complete the request."
            ),
        }
    }

    # Neither provider's private error body is exposed.
    assert "PRIVATE_UPSTREAM_ERROR" not in response.text
    assert "internal provider details" not in response.text

    # Exactly two attempts. No retries or fallback loops.
    assert calls == [
        PRIMARY_URL,
        SECONDARY_URL,
    ]

    # One logical reservation remains charged.
    assert limiter.get_usage("tenant-a") == 105


@pytest.mark.asyncio
async def test_gateway_database_admission_failure(
    gateway_client,
):
    """Unavailable SQLite admission must fail closed."""

    client, limiter, calls, _ = gateway_client

    # Create an independent connection that can hold
    # SQLite's write lock.
    with closing(
        sqlite3.connect(
            limiter.database_path,
            isolation_level=None,
        )
    ) as blocker:

        blocker.execute("BEGIN IMMEDIATE")

        try:
            # The gateway attempts its normal admission
            # transaction, but cannot acquire the lock.
            response = await client.post(
                "/v1/completions",
                headers=TENANT_A_HEADERS,
                json=COMPLETION_PAYLOAD,
            )

            assert response.status_code == 503

            assert response.json() == {
                "error": {
                    "code": "service_unavailable",
                    "message": (
                        "The gateway is temporarily unavailable."
                    ),
                }
            }

            # Internal database details must not be exposed.
            assert "database is locked" not in response.text
            assert str(limiter.database_path) not in response.text

            # Critical security assertion:
            # failed admission must never reach a provider.
            assert calls == []

        finally:
            blocker.rollback()

    # The failed admission must not have created a record.
    assert limiter.get_usage("tenant-a") == 0

    # Confirm that the gateway recovers after the
    # database lock has been released.
    recovered = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert recovered.status_code == 200
    assert limiter.get_usage("tenant-a") == 13
    assert calls == [PRIMARY_URL]


@pytest.mark.asyncio
async def test_gateway_database_reconciliation_failure(
    gateway_client,
    monkeypatch,
):
    """Failed reconciliation retains the original charge."""

    client, limiter, calls, _ = gateway_client

    # Preserve the real reconciliation method.
    original_reconcile = limiter.reconcile

    def reconcile_with_database_lock(
        request_id,
        actual_tokens,
    ):
        """Force actual SQLite lock contention at reconciliation."""

        # The gateway invokes this function in a worker
        # thread through asyncio.to_thread().
        #
        # Create and use the blocker connection inside
        # that same thread to preserve SQLite thread safety.
        with closing(
            sqlite3.connect(
                limiter.database_path,
                isolation_level=None,
            )
        ) as blocker:

            blocker.execute("BEGIN IMMEDIATE")

            try:
                # The original reconciliation method opens
                # a separate connection. Its write
                # transaction cannot acquire the lock.
                return original_reconcile(
                    request_id,
                    actual_tokens,
                )

            finally:
                blocker.rollback()

    # Replace reconciliation only for this test.
    # Admission and provider routing remain unchanged.
    monkeypatch.setattr(
        limiter,
        "reconcile",
        reconcile_with_database_lock,
    )

    response = await client.post(
        "/v1/completions",
        headers=TENANT_A_HEADERS,
        json=COMPLETION_PAYLOAD,
    )

    assert response.status_code == 503

    assert response.json() == {
        "error": {
            "code": "service_unavailable",
            "message": (
                "The gateway is temporarily unavailable."
            ),
        }
    }

    assert "database is locked" not in response.text
    assert str(limiter.database_path) not in response.text

    # Unlike admission failure, the primary provider
    # has already completed the request.
    assert calls == [PRIMARY_URL]

    # Reconciliation failed, so the original reservation
    # must still exist instead of being refunded.
    assert limiter.get_usage("tenant-a") == 105

    # Verify the physical accounting record as well.
    with closing(
        sqlite3.connect(limiter.database_path)
    ) as connection:

        rows = connection.execute(
            """
            SELECT tokens
            FROM token_events
            WHERE tenant_id = ?
            """,
            ("tenant-a",),
        ).fetchall()

    assert rows == [(105,)]


# ---------------------------------------------------------
# Concurrent asynchronous gateway admission
# ---------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_concurrent_admission_respects_budget(
    gateway_client,
):
    """An in-flight reservation must protect tenant capacity."""

    client, limiter, calls, provider_state = gateway_client

    # Leave exactly 200 tokens available.
    initial = limiter.reserve(
        "tenant-a",
        49_800,
    )

    assert initial.allowed
    assert limiter.get_usage("tenant-a") == 49_800

    # Synchronization events allow the test to coordinate
    # both gateway requests without arbitrary sleep delays.
    primary_started = asyncio.Event()
    release_primary = asyncio.Event()

    provider_state["primary_started"] = primary_started
    provider_state["release_primary"] = release_primary

    # Start Request A asynchronously.
    #
    # It should reserve 105 tokens, reach the primary
    # provider, and wait for release_primary.
    first_task = asyncio.create_task(
        client.post(
            "/v1/completions",
            headers=TENANT_A_HEADERS,
            json=COMPLETION_PAYLOAD,
        )
    )

    try:
        # Wait until Request A has reached the provider.
        # At this point, admission must already be committed.
        await asyncio.wait_for(
            primary_started.wait(),
            timeout=2.0,
        )

        # The first request is still processing.
        assert not first_task.done()

        # Verify that the full reservation is counted
        # before provider completion or reconciliation.
        assert limiter.get_usage("tenant-a") == 49_905

        # Submit Request B while Request A is still
        # waiting for its primary provider.
        second_response = await asyncio.wait_for(
            client.post(
                "/v1/completions",
                headers=TENANT_A_HEADERS,
                json=COMPLETION_PAYLOAD,
            ),
            timeout=2.0,
        )

        # Only 95 tokens are available, but Request B
        # requires 105. It must therefore be rejected.
        assert second_response.status_code == 429

        assert second_response.json() == {
            "error": {
                "code": "rate_limit_exceeded",
                "message": "Tenant token limit exceeded.",
            }
        }

        # Request A must still be in flight when Request B
        # receives its quota decision.
        assert not first_task.done()

        # Rejected admission must not increase usage.
        assert limiter.get_usage("tenant-a") == 49_905

        # Only Request A should have reached a provider.
        assert calls == [PRIMARY_URL]

        # Allow Request A's primary provider to complete.
        release_primary.set()

        first_response = await asyncio.wait_for(
            first_task,
            timeout=2.0,
        )

        assert first_response.status_code == 200

        payload = first_response.json()

        assert payload["provider"] == "primary"
        assert payload["fallback_reason"] is None
        assert payload["usage"]["total_tokens"] == 13

        # The original 105-token reservation has now
        # been reconciled to 13 actual tokens.
        assert limiter.get_usage("tenant-a") == 49_813

        # Request B must never have reached either provider.
        assert calls == [PRIMARY_URL]

        # Verify the actual persistent records:
        # one seeded event and one successful completion.
        with closing(
            sqlite3.connect(limiter.database_path)
        ) as connection:

            rows = connection.execute(
                """
                SELECT tokens
                FROM token_events
                WHERE tenant_id = ?
                ORDER BY tokens
                """,
                ("tenant-a",),
            ).fetchall()

        assert rows == [
            (13,),
            (49_800,),
        ]

    finally:
        # Always release the provider handler, even if an
        # earlier assertion failed.
        release_primary.set()

        # Prevent a failed test from leaving an unfinished
        # gateway request running in the event loop.
        if not first_task.done():
            first_task.cancel()

        await asyncio.gather(
            first_task,
            return_exceptions=True,
        )
