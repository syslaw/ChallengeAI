
"""Central configuration for the Task 4 LLM gateway."""

import os
from pathlib import Path


# Locate this package independently of the terminal's
# current working directory.
PACKAGE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------
# Rate-limiter configuration
# ---------------------------------------------------------

# Maximum token budget for one tenant in a rolling window.
TOKEN_LIMIT = 50_000

# Each reservation expires 60 seconds after admission.
WINDOW_SECONDS = 60

# SQLite waits at most this long for a conflicting lock.
# A database failure will never bypass rate limiting.
SQLITE_BUSY_TIMEOUT_SECONDS = 2.0

# Use a persistent, on-disk database.
# An environment variable can override the default location.
DATABASE_PATH = Path(
    os.getenv(
        "TASK4_DATABASE_PATH",
        str(PACKAGE_DIR / "token_usage.sqlite3"),
    )
)


# ---------------------------------------------------------
# Provider configuration
# ---------------------------------------------------------

# Upstream destinations are controlled by the gateway,
# not supplied by the incoming client.
PRIMARY_PROVIDER_URL = os.getenv(
    "TASK4_PRIMARY_URL",
    "http://127.0.0.1:8005/v1/completions",
)

SECONDARY_PROVIDER_URL = os.getenv(
    "TASK4_SECONDARY_URL",
    "http://127.0.0.1:8006/v1/completions",
)

# The assessment requires a 3,000 ms primary deadline.
PRIMARY_TIMEOUT_SECONDS = 3.0

# The secondary also needs a bounded deadline so fallback
# cannot leave the request waiting indefinitely.
SECONDARY_TIMEOUT_SECONDS = 3.0


# ---------------------------------------------------------
# Request validation limits
# ---------------------------------------------------------

# Bound the supported input size to avoid accepting
# unnecessarily large prompts.
MAX_PROMPT_CHARACTERS = 50_000

# Restrict requested generation length for this assessment.
MAX_OUTPUT_TOKENS = 4_096

DEFAULT_OUTPUT_TOKENS = 256


# ---------------------------------------------------------
# Tenant authentication configuration
# ---------------------------------------------------------

# These are fictional credentials for the local assessment.
# Environment variables can override them without changing
# application code.
TENANT_A_API_KEY = os.getenv(
    "TASK4_TENANT_A_API_KEY",
    "dev-tenant-a-key",
)

TENANT_B_API_KEY = os.getenv(
    "TASK4_TENANT_B_API_KEY",
    "dev-tenant-b-key",
)
