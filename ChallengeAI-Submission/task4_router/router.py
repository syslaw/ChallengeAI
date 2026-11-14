
"""Asynchronous model routing with bounded provider fallback."""

import asyncio
from dataclasses import dataclass

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from task4_router.config import (
    PRIMARY_PROVIDER_URL,
    PRIMARY_TIMEOUT_SECONDS,
    SECONDARY_PROVIDER_URL,
    SECONDARY_TIMEOUT_SECONDS,
)

from task4_router.models import CompletionRequest


# ---------------------------------------------------------
# Validated provider response models
# ---------------------------------------------------------


class ProviderUsage(BaseModel):
    """Validated token usage reported by a provider."""

    model_config = ConfigDict(
        strict=True,
        extra="ignore",
    )

    # SQLite integers are signed 64-bit values. Bound
    # provider-controlled counts before database accounting.
    # EDIT - largest 64 bit int doesn't make sense for upper limit. 
    # Capping at 500k for now after code review.
    prompt_tokens: int = Field(
        ge=0,
        le=500_000
        #le=2**63 - 1,
    )

    completion_tokens: int = Field(
        ge=0,
        le=500_000
        #le=2**63 - 1,
    )

    total_tokens: int = Field(
        ge=0,
        le=500_000
        #le=2**63 - 1,
    )

    @model_validator(mode="after")
    def validate_total(self):
        """Ensure usage values are internally consistent."""

        expected_total = (
            self.prompt_tokens + self.completion_tokens
        )

        if self.total_tokens != expected_total:
            raise ValueError("Inconsistent token usage.")

        return self


class ProviderChoice(BaseModel):
    """A single non-streaming text completion."""

    model_config = ConfigDict(
        strict=True,
        extra="ignore",
    )

    text: str

    finish_reason: str = Field(
        min_length=1,
    )


class ProviderCompletion(BaseModel):
    """Required fields in a successful provider response."""

    model_config = ConfigDict(
        strict=True,
        extra="ignore",
    )

    model: str = Field(
        min_length=1,
    )

    # The assessment supports one text completion,
    # not multiple choices or tool calls.
    choices: list[ProviderChoice] = Field(
        min_length=1,
        max_length=1,
    )


@dataclass(frozen=True)
class RoutingResult:
    """Successful result returned by the model router."""

    model: str
    text: str
    finish_reason: str
    usage: ProviderUsage | None
    provider: str

    # None for primary success.
    # "rate_limit" or "timeout" for fallback success.
    fallback_reason: str | None = None


# ---------------------------------------------------------
# Sanitized routing errors
# ---------------------------------------------------------


class RoutingError(Exception):
    """A safe provider failure exposed to the gateway."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        super().__init__(message)

        self.status_code = status_code
        self.code = code
        self.message = message


class _PrimaryRateLimited(Exception):
    """Internal signal indicating an eligible HTTP 429."""


def _provider_error() -> RoutingError:
    """Construct a generic, safe upstream error."""

    return RoutingError(
        status_code=502,
        code="provider_error",
        message=(
            "The model provider could not "
            "complete the request."
        ),
    )


def _provider_timeout() -> RoutingError:
    """Construct a generic, safe timeout error."""

    return RoutingError(
        status_code=504,
        code="provider_timeout",
        message="The model provider timed out.",
    )


# ---------------------------------------------------------
# Asynchronous provider router
# ---------------------------------------------------------


class ModelRouter:
    """Route a completion to the primary or backup model."""

    def __init__(
        self,
        primary_url: str = PRIMARY_PROVIDER_URL,
        secondary_url: str = SECONDARY_PROVIDER_URL,
        primary_timeout: float = PRIMARY_TIMEOUT_SECONDS,
        secondary_timeout: float = SECONDARY_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.primary_url = primary_url
        self.secondary_url = secondary_url

        self.primary_timeout = primary_timeout
        self.secondary_timeout = secondary_timeout

        # Optional transport injection allows deterministic
        # tests without real network calls or running servers.
        self.transport = transport

        if primary_timeout <= 0:
            raise ValueError(
                "Primary timeout must be positive."
            )

        if secondary_timeout <= 0:
            raise ValueError(
                "Secondary timeout must be positive."
            )

    async def _attempt(
        self,
        client: httpx.AsyncClient,
        url: str,
        request: CompletionRequest,
        provider: str,
        timeout_seconds: float,
        fallback_reason: str | None = None,
    ) -> RoutingResult:
        """Perform one bounded provider HTTP attempt."""

        try:
            # Enforce a total deadline for the HTTP request,
            # including receiving the entire response body.
            #
            # This is not just an HTTP read inactivity
            # timeout. It covers the whole awaited attempt.
            async with asyncio.timeout(timeout_seconds):
                response = await client.post(
                    url,
                    json=request.model_dump(),
                )

        except TimeoutError:
            # asyncio.timeout cancels the awaited operation
            # and raises TimeoutError when its deadline expires.
            raise _provider_timeout() from None

        except httpx.TimeoutException:
            # A transport-level timeout is also represented
            # as a sanitized provider timeout.
            raise _provider_timeout() from None

        except httpx.HTTPError:
            # Connection errors and other HTTP transport
            # failures must not expose internal details.
            raise _provider_error() from None

        # Only a PRIMARY provider's HTTP 429 triggers
        # fallback. A secondary 429 is a final failure.
        if response.status_code == 429:
            if provider == "primary":
                raise _PrimaryRateLimited()

            raise _provider_error()

        # Other unsuccessful statuses do not trigger
        # fallback under our assessment-specific policy.
        if response.status_code != 200:
            raise _provider_error()

        # -------------------------------------------------
        # Validate successful response content
        # -------------------------------------------------

        try:
            payload = response.json()

            completion = ProviderCompletion.model_validate(
                payload
            )

            choice = completion.choices[0]

            # Prevent malformed Unicode from reaching the
            # gateway's eventual JSON response encoder.
            completion.model.encode("utf-8")
            choice.text.encode("utf-8")
            choice.finish_reason.encode("utf-8")

        except (
            ValueError,
            ValidationError,
            UnicodeError,
            RecursionError,
        ):
            raise _provider_error() from None

        # A valid completion can still have missing or
        # unusable accounting metadata.
        #
        # Do not reject the completion merely because
        # usage is unavailable. The gateway will retain
        # its original reservation in this case.
        usage = None
        raw_usage = payload.get("usage")

        if raw_usage is not None:
            try:
                usage = ProviderUsage.model_validate(
                    raw_usage
                )

            except ValidationError:
                usage = None

        return RoutingResult(
            model=completion.model,
            text=choice.text,
            finish_reason=choice.finish_reason,
            usage=usage,
            provider=provider,
            fallback_reason=fallback_reason,
        )

    async def complete(
        self,
        request: CompletionRequest,
    ) -> RoutingResult:
        """Try the primary, then fallback when eligible."""

        # A single client is shared across this logical
        # request's primary and secondary attempts.
        #
        # The client is closed on success, failure,
        # or cancellation when the context exits.
        async with httpx.AsyncClient(
            transport=self.transport,

            # Use asyncio.timeout for total deadlines,
            # rather than conflicting HTTPX timeouts.
            timeout=None,

            # Do not follow arbitrary provider redirects.
            follow_redirects=False,

            # Ignore ambient proxy configuration.
            trust_env=False,

        ) as client:

            try:
                # First and only primary attempt.
                return await self._attempt(
                    client=client,
                    url=self.primary_url,
                    request=request,
                    provider="primary",
                    timeout_seconds=self.primary_timeout,
                )

            except _PrimaryRateLimited:
                # Primary explicitly refused the request.
                fallback_reason = "rate_limit"

            except RoutingError as error:
                # Only primary timeout errors qualify
                # for fallback. Other failures propagate.
                if error.code != "provider_timeout":
                    raise

                fallback_reason = "timeout"

            # We reach this point only after the primary
            # attempt has failed with an eligible condition.
            #
            # No parallel provider task is left running
            # inside this router.
            return await self._attempt(
                client=client,
                url=self.secondary_url,
                request=request,
                provider="secondary",
                timeout_seconds=self.secondary_timeout,
                fallback_reason=fallback_reason,
            )
