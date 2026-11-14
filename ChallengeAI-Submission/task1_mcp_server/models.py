"""
Task 1: Input Validation Models

Purpose:
    Define strict validation rules for the two MCP tools.

    get_customer_record:
        Requires a customer ID formatted as CUST-XXXXX.

    trigger_refund:
        Requires:
        - A valid customer ID.
        - A positive refund amount.
        - An ammount representing whole cents (2 decimal places for currency input).
        - A reason containing at least 10 characters.

Design:
    Pydantic validates incoming data before business logic
    processes it. Invalid inputs raise ValidationError.

    We use strict types to avoid unexpected automatic
    conversions, such as accepting "25.50" as a number.
"""

# ============================================================
# SECTION 1: IMPORT PYDANTIC VALIDATION COMPONENTS
# ============================================================

# BaseModel defines a structured model for request data.
#
# ConfigDict lets us configure strict model behavior.
#
# Field lets us define constraints such as regex patterns,
# minimum values, and minimum string lengths.
# 
# field_validator is required for our custom refund
# decimal-precision validation. I (Max) am adding this in 
# to ensure that an incorrect decimal being added as currency
# does not end up passing. 

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Also importing decimal library to 
# help validate currency input, but keeping input as float type.
from decimal import Decimal

# ============================================================
# SECTION 2: CUSTOMER LOOKUP VALIDATION
# ============================================================

class CustomerLookupRequest(BaseModel):
    """
    Validate inputs for get_customer_record.

    Valid example:
        CUST-12345

    Invalid examples:
        CUST-1234
        CUST-ABCDE
        CUSTOMER-12345
    """

    # Strict mode prevents implicit type conversions.
    #
    # For example, an integer should not be accepted
    # as a customer ID string.
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    # The regular expression enforces:
    #
    # ^          Beginning of the string.
    # CUST-      Required literal prefix.
    # [0-9]{5}   Exactly five numeric digits.
    # $          End of the string.
    customer_id: str = Field(
        pattern=r"^CUST-[0-9]{5}$",
        description="Customer ID formatted as CUST-XXXXX",
    )


# ============================================================
# SECTION 3: REFUND REQUEST VALIDATION
# ============================================================

class RefundRequest(BaseModel):
    """
    Validate inputs for trigger_refund.

    Required fields:
        customer_id
        amount
        reason
    """

    # Enforce strict types and reject unexpected fields.
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    # Customer ID must match the same format used
    # by the customer lookup tool.
    customer_id: str = Field(
        pattern=r"^CUST-[0-9]{5}$",
        description="Customer ID formatted as CUST-XXXXX",
    )

    # Refund amount must be greater than zero.
    #
    # gt=0 means "greater than zero".
    #
    # We allow integers and floating-point numbers because
    # both are valid JSON numeric representations.
    amount: float = Field(
        gt=0,
        allow_inf_nan=False,
        description="Positive refund amount",
    )

    # The reason must contain at least 10 characters.
    #
    # min_length=10 rejects shorter explanations.
    reason: str = Field(
        min_length=10,
        description="Refund reason containing at least 10 characters",
        
    )
    # ============================================================
    # CUSTOM VALIDATION: REFUND AMOUNT DECIMAL PRECISION
    # ============================================================

    @field_validator("amount")
    @classmethod
    def validate_amount_precision(cls, value: float) -> float:
        """
        Ensure that refund amounts have no more than two
        significant decimal places.

        Valid examples:
            25
            25.5
            25.50

        Invalid examples:
            25.402491
            10.999
            0.001

        We reject invalid precision rather than automatically
        rounding, because rounding would change the requested
        refund amount.
        """

        # Convert the float's decimal representation into Decimal.
        #
        # Using str(value) avoids exposing the underlying binary
        # floating-point approximation.
        decimal_value = Decimal(str(value))

        # Multiplying by 100 converts dollars into cents.
        #
        # A valid currency amount must represent a whole
        # number of cents.
        cents = decimal_value * 100

        # If the result is not an integer, the amount has
        # more precision than our currency rules allow.
        if cents != cents.to_integral_value():
            raise ValueError(
                # This error will return as a guardrail created by Max (candidate) to prevent incorrect currency input on the type float.
                "Refund amount cannot exceed two decimal places. Rejecting fractional cents."
            )

        # Return the original value when validation succeeds.
        return value