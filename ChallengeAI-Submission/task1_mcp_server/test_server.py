
"""
Task 1: Automated MCP Tool Validation Tests

Purpose:
    Verify that our customer lookup and refund tools
    accept valid requests and reject invalid inputs.

Run from the ChallengeAI project directory:

    python -m pytest task1_mcp_server/test_server.py -v

These are Python function-level tests.
MCP JSON-RPC protocol tests will be added separately.
"""

# pytest provides test execution and exception assertions.
import pytest

# Pydantic raises ValidationError when input violates
# our validation models.
from pydantic import ValidationError

# Import the functions that our MCP server exposes as tools.
from task1_mcp_server.server import (
    get_customer_record,
    trigger_refund,
)


# ============================================================
# SECTION 1: CUSTOMER LOOKUP TESTS
# ============================================================

def test_valid_customer_id():
    """A properly formatted customer ID should succeed."""

    result = get_customer_record("CUST-12345")

    assert result["customer_id"] == "CUST-12345"
    assert result["status"] == "active"


@pytest.mark.parametrize(
    "invalid_id",
    [
        "CUST-1234",      # Too few digits
        "CUST-123456",    # Too many digits
        "CUST-ABCDE",     # Letters instead of digits
        "CUSTOMER-12345", # Incorrect prefix
    ],
)
def test_invalid_customer_ids(invalid_id):
    """Malformed customer IDs must be rejected."""

    # pytest.raises expects the function to fail.
    #
    # If ValidationError occurs, the test passes.
    # If the function accepts the input, the test fails.
    with pytest.raises(ValidationError):
        get_customer_record(invalid_id)


# ============================================================
# SECTION 2: VALID REFUND TESTS
# ============================================================

@pytest.mark.parametrize(
    "amount",
    [
        25.40,  # Two decimal places
        25.5,   # One decimal place
        25.0,   # Whole-dollar amount
        0.01,   # Smallest positive cent amount
    ],
)
def test_valid_refund_amounts(amount):
    """Positive refund amounts representing whole cents pass."""

    result = trigger_refund(
        "CUST-12345",
        amount,
        "Incorrect item delivered",
    )

    assert result["status"] == "approved"
    assert result["amount"] == amount


# ============================================================
# SECTION 3: INVALID REFUND AMOUNT TESTS
# ============================================================

@pytest.mark.parametrize(
    "amount",
    [
        -25.50,     # Negative amount
        0.0,        # Zero amount
        25.402491,  # Fractional cents
        10.999,     # More than two meaningful decimal places
        0.001,      # Less than one cent
    ],
)
def test_invalid_refund_amounts(amount):
    """Invalid monetary values must fail validation."""

    with pytest.raises(ValidationError):
        trigger_refund(
            "CUST-12345",
            amount,
            "Incorrect item delivered",
        )


# ============================================================
# SECTION 4: REFUND REASON VALIDATION
# ============================================================

def test_refund_reason_too_short():
    """Refund reasons shorter than 10 characters are rejected."""

    with pytest.raises(ValidationError):
        trigger_refund(
            "CUST-12345",
            25.50,
            "Too short",
        )


def test_valid_refund_reason():
    """A sufficiently descriptive refund reason is accepted."""

    result = trigger_refund(
        "CUST-12345",
        25.50,
        "Customer received the incorrect item",
    )

    assert result["status"] == "approved"
