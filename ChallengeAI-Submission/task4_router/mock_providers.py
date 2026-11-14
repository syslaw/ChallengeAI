
"""Deterministic local LLM providers for Task 4."""

import asyncio
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from task4_router.models import CompletionRequest


# These modes are controlled by the server environment.
# Clients cannot select them in their completion request.
SUPPORTED_MODES = {
    "success",
    "rate_limit",
    "timeout",
    "error",
    "invalid_response",
}


def create_mock_provider(
    provider_name: str,
    model_name: str,
    mode: str = "success",
) -> FastAPI:
    """Create a small mock LLM provider application."""

    if mode not in SUPPORTED_MODES:
        raise ValueError("Unsupported mock provider mode.")

    app = FastAPI(
        title=f"Task 4 {provider_name} provider",
    )

    @app.post("/v1/completions")
    async def create_completion(
        request: CompletionRequest,
    ):
        """Return a deterministic result or simulated failure."""

        # Simulate provider-side rate limiting.
        if mode == "rate_limit":
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "message": "Provider capacity exceeded."
                    }
                },
            )

        # Simulate a provider that takes longer than
        # the gateway's three-second deadline.
        #
        # asyncio.sleep is nonblocking, so other requests
        # can still be handled by this application.
        if mode == "timeout":
            await asyncio.sleep(5.0)

        # Simulate an upstream failure containing private
        # information. The gateway must never expose this
        # error body to its own callers.
        if mode == "error":
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": (
                            "PRIVATE_UPSTREAM_ERROR: "
                            "internal provider stack trace"
                        )
                    }
                },
            )

        # Simulate a successful HTTP status with an
        # invalid application-level response.
        if mode == "invalid_response":
            return {
                "unexpected": "not a valid completion"
            }

        # -------------------------------------------------
        # Deterministic successful completion
        # -------------------------------------------------

        # This is a mock, not a real model tokenizer.
        # We reuse our documented byte-length input estimate.
        prompt_tokens = request.estimated_input_tokens()

        # Simulate a short generated response that does
        # not exceed the requested output token maximum.
        completion_tokens = min(request.max_tokens, 8)

        # Produce predictable text with one mock token
        # per word. This is test data, not real inference.
        text = " ".join(
            ["mock"] * completion_tokens
        )

        return {
            "model": model_name,
            "choices": [
                {
                    "text": text,
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": (
                    prompt_tokens + completion_tokens
                ),
            },
        }

    return app


# Both applications are constructed from the same
# implementation but have independent configurations.

primary_app = create_mock_provider(
    provider_name="primary",
    model_name="mock-primary-model",
    mode=os.getenv(
        "TASK4_PRIMARY_MODE",
        "success",
    ),
)

secondary_app = create_mock_provider(
    provider_name="secondary",
    model_name="mock-secondary-model",
    mode=os.getenv(
        "TASK4_SECONDARY_MODE",
        "success",
    ),
)