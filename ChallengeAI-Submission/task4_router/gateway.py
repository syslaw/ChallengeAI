
"""Task 4 gateway: authentication, token admission, and routing."""

import asyncio
import secrets
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from task4_router.config import (
    TENANT_A_API_KEY,
    TENANT_B_API_KEY,
)

from task4_router.limiter import TokenRateLimiter
from task4_router.models import CompletionRequest
from task4_router.router import ModelRouter, RoutingError


# ---------------------------------------------------------
# Standardized gateway errors
# ---------------------------------------------------------


class GatewayError(Exception):
    """An expected gateway failure with a safe public message."""

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


def error_response(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Construct the gateway's standardized error payload."""

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
            }
        },
        headers=headers,
    )


# ---------------------------------------------------------
# Application factory
# ---------------------------------------------------------


def create_app(
    limiter: TokenRateLimiter | None = None,
    router: ModelRouter | None = None,
    tenant_keys: dict[str, str] | None = None,
) -> FastAPI:
    """Create a gateway with optional test dependencies."""

    # Normal execution uses our existing configuration.
    # Tests can inject a temporary SQLite database and
    # a router backed by HTTPX MockTransport.
    if limiter is None:
        limiter = TokenRateLimiter()

    if router is None:
        router = ModelRouter()

    # Map API keys to stable internal tenant identifiers.
    # Never pass these raw keys to the SQLite limiter.
    if tenant_keys is None:
        tenant_keys = {
            TENANT_A_API_KEY: "tenant-a",
            TENANT_B_API_KEY: "tenant-b",
        }

        # Reject a misconfiguration that would accidentally
        # map both tenants to the same API key.
        if TENANT_A_API_KEY == TENANT_B_API_KEY:
            raise ValueError("Tenant API keys must be distinct.")

    # Make an independent copy of the configured mapping.
    tenant_keys = dict(tenant_keys)

    if not tenant_keys:
        raise ValueError("At least one tenant key is required.")

    # Validate server-controlled credentials at startup.
    # Bearer keys must be nonempty printable ASCII without
    # spaces. This also makes byte comparisons predictable.
    for key, tenant_id in tenant_keys.items():
        if not key or any(
            not 33 <= ord(character) <= 126
            for character in key
        ):
            raise ValueError("Invalid tenant key configuration.")

        if not tenant_id.strip():
            raise ValueError("Invalid tenant ID configuration.")

    # Convert configured keys to bytes once, rather than
    # repeatedly encoding them during authentication.
    configured_keys = [
        (key.encode("ascii"), tenant_id)
        for key, tenant_id in tenant_keys.items()
    ]

    # -----------------------------------------------------
    # Application startup and shutdown
    # -----------------------------------------------------

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Initialize the database before accepting requests.
        # SQLite operations are synchronous, so execute
        # them in a worker thread.
        await asyncio.to_thread(limiter.initialize)

        yield

        # No persistent SQLite connections or HTTP clients
        # need to be closed here. Our existing components
        # scope those resources to individual operations.

    app = FastAPI(
        title="Task 4 LLM Gateway",
        lifespan=lifespan,
        debug=False,
    )

    # -----------------------------------------------------
    # Gateway exception handlers
    # -----------------------------------------------------

    @app.exception_handler(GatewayError)
    async def handle_gateway_error(
        request: Request,
        error: GatewayError,
    ):
        headers = None

        if error.status_code == 401:
            headers = {
                "WWW-Authenticate": "Bearer"
            }

        return error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
            headers=headers,
        )

    @app.exception_handler(RoutingError)
    async def handle_routing_error(
        request: Request,
        error: RoutingError,
    ):
        # RoutingError contains only our predefined public
        # messages, never raw provider exception details.
        return error_response(
            status_code=error.status_code,
            code=error.code,
            message=error.message,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        error: RequestValidationError,
    ):
        # Do not return error.errors(), which could include
        # sensitive values supplied in the original request.
        return error_response(
            status_code=400,
            code="invalid_request",
            message="Invalid completion request.",
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(
        request: Request,
        error: StarletteHTTPException,
    ):
        # Do not expose arbitrary framework error details.
        if error.status_code == 404:
            return error_response(
                status_code=404,
                code="not_found",
                message="Gateway endpoint not found.",
            )

        if error.status_code == 405:
            return error_response(
                status_code=405,
                code="method_not_allowed",
                message="HTTP method not allowed.",
                headers=error.headers,
            )

        return error_response(
            status_code=error.status_code,
            code="request_error",
            message="The gateway could not process the request.",
        )

    @app.exception_handler(sqlite3.Error)
    async def handle_database_error(
        request: Request,
        error: sqlite3.Error,
    ):
        # Never expose SQL statements, database paths,
        # lock information, or raw SQLite exceptions.
        return error_response(
            status_code=503,
            code="service_unavailable",
            message="The gateway is temporarily unavailable.",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        error: Exception,
    ):
        # Final safety net for unexpected application errors.
        # Do not expose str(error) or a traceback to callers.
        return error_response(
            status_code=500,
            code="internal_error",
            message="An internal gateway error occurred.",
        )

    # -----------------------------------------------------
    # Tenant authentication
    # -----------------------------------------------------

    async def authenticate_tenant(
        request: Request,
    ) -> str:
        """Resolve a Bearer API key to an internal tenant ID."""

        def invalid_key() -> GatewayError:
            return GatewayError(
                status_code=401,
                code="invalid_api_key",
                message="Missing or invalid API key.",
            )

        # Reject ambiguous requests with multiple
        # Authorization headers.
        authorization_headers = request.headers.getlist(
            "authorization"
        )

        if len(authorization_headers) != 1:
            raise invalid_key()

        parts = authorization_headers[0].split()

        if len(parts) != 2:
            raise invalid_key()

        scheme, supplied_key = parts

        # HTTP authentication schemes are case-insensitive.
        if scheme.lower() != "bearer":
            raise invalid_key()

        try:
            supplied_bytes = supplied_key.encode("ascii")

        except UnicodeEncodeError:
            raise invalid_key() from None

        # Compare against every configured credential.
        # compare_digest provides timing-resistant equality
        # checks without introducing another dependency.
        authenticated_tenant = None

        for expected_key, tenant_id in configured_keys:
            if secrets.compare_digest(
                supplied_bytes,
                expected_key,
            ):
                authenticated_tenant = tenant_id

        if authenticated_tenant is None:
            raise invalid_key()

        # Return only the internal tenant identifier.
        return authenticated_tenant

    # -----------------------------------------------------
    # Public completion endpoint
    # -----------------------------------------------------

    @app.post("/v1/completions")
    async def create_completion(
        request: CompletionRequest,
        tenant_id: str = Depends(authenticate_tenant),
    ):
        """Authenticate, rate-limit, route, and account."""

        # -------------------------------------------------
        # 1. Reserve the tenant's token budget
        # -------------------------------------------------

        reserved_tokens = request.reserved_tokens()

        # SQLite is synchronous. Running it in a worker
        # thread prevents database lock waits from blocking
        # the FastAPI event loop.
        reservation = await asyncio.to_thread(
            limiter.reserve,
            tenant_id,
            reserved_tokens,
        )

        if not reservation.allowed:
            # Local quota rejection is FINAL.
            #
            # Neither primary nor secondary providers should
            # receive a request when admission is denied.
            raise GatewayError(
                status_code=429,
                code="rate_limit_exceeded",
                message="Tenant token limit exceeded.",
            )

        # A successful reservation must have an internal ID.
        request_id = reservation.request_id

        if request_id is None:
            raise RuntimeError("Missing reservation ID.")

        # -------------------------------------------------
        # 2. Route the completion
        # -------------------------------------------------

        # At this point the SQLite transaction has already
        # committed. We hold no database locks during HTTP I/O.
        #
        # RoutingError propagates to our sanitized handler.
        # Failed requests retain their original reservation.
        #
        # Cancellation also propagates naturally. We do not
        # refund uncertain provider usage on cancellation.
        result = await router.complete(request)

        # -------------------------------------------------
        # 3. Reconcile the reservation
        # -------------------------------------------------

        if result.usage is not None:
            actual_tokens = result.usage.total_tokens

            if result.fallback_reason == "timeout":
                # A timed-out primary may still have
                # consumed tokens remotely.
                #
                # Retain at least the original reservation.
                # This is conservative logical-request
                # accounting, not a provider billing ledger.
                charged_tokens = max(
                    reserved_tokens,
                    actual_tokens,
                )

            else:
                # Primary success or explicit HTTP 429
                # fallback: reconcile to valid actual usage.
                charged_tokens = actual_tokens

            await asyncio.to_thread(
                limiter.reconcile,
                request_id,
                charged_tokens,
            )

        # If usage is missing or invalid, leave the
        # existing reservation unchanged.
        #
        # If the reservation has already expired,
        # reconcile() does not resurrect it.

        # -------------------------------------------------
        # 4. Construct the supported public response
        # -------------------------------------------------

        # Only explicitly supported provider fields are
        # returned. Raw upstream response bodies and
        # arbitrary provider metadata are never forwarded.
        response = {
            "model": result.model,
            "choices": [
                {
                    "text": result.text,
                    "finish_reason": result.finish_reason,
                }
            ],
            "usage": (
                result.usage.model_dump()
                if result.usage is not None
                else None
            ),

            # Deliberate gateway metadata for demonstrating
            # fallback and identifying the selected provider.
            "provider": result.provider,
            "fallback_reason": result.fallback_reason,
        }

        return response

    return app


# Uvicorn imports this application during normal execution.
app = create_app()
