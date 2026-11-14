"""
Task 1: Custom MCP Server

Purpose:
    Create a basic Model Context Protocol (MCP) server that exposes
    two tools to an AI client:

    1. get_customer_record - Retrieve simulated customer information.
    2. trigger_refund - Simulate processing a customer refund.

Architecture:
    - Uses the official MCP Python SDK (version 2.x).
    - Communicates with MCP clients through stdio.
    - Uses Python's logging library for diagnostic messages.

IMPORTANT:
    This is our initial implementation. We will add strict Pydantic validation, error handling, and automated tests in subsequent steps.
"""

# ============================================================
# SECTION 1: IMPORT REQUIRED PYTHON LIBRARIES
# ============================================================

# asyncio allows Python to execute asynchronous operations.
# MCP servers often use asynchronous communication because they
# need to receive requests and send responses efficiently.
import asyncio

# logging provides a structured way to record diagnostic messages.
# Unlike print(), Python's standard logging configuration sends
# messages to stderr by default.
#
# This matters because MCP uses stdout for protocol messages.
# Printing debugging text to stdout could corrupt communication.
import logging

# Import MCPServer from the official MCP Python SDK (version 2.x).
#
# MCPServer manages the protocol, registers our tools,
# and communicates with MCP clients.
from mcp.server.mcpserver import MCPServer

# Import our request models so that both MCP tools can validate
# incoming arguments before performing their business logic.
from pydantic import ValidationError

# === Error Handling Logic for JSON RPC ===
# Pydantic provides validation errors when request inputs
# do not satisfy the constraints defined in models.py.
from pydantic import ValidationError

# === Error Handling Logic for JSON RPC ===
# MCPError represents a JSON-RPC protocol error.
from mcp.shared.exceptions import MCPError

# === Error Handling Logic for JSON RPC ===
# Import the SDK's standard Invalid params error code (-32602).
from mcp.types import INVALID_PARAMS

from task1_mcp_server.models import (
    CustomerLookupRequest,
    RefundRequest,
)

# ============================================================
# SECTION 2: CONFIGURE APPLICATION LOGGING
# ============================================================

# Set the default logging level to INFO.
#
# INFO messages describe normal application activity.
# ERROR messages describe failures.
#
# Explicitly use stderr to keep stdout free of debugging output.
import sys

logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
)

# Create a logger associated with this Python module.
#
# __name__ automatically identifies the current module.
logger = logging.getLogger(__name__)


# ============================================================
# SECTION 3: INITIALIZE OUR MCP SERVER
# ============================================================

# Create an MCPServer instance.
#
# Think of this as creating the server application before
# configuring which capabilities it exposes.
#
# name:
#     Internal identifier for our MCP server.
#
# description:
#     Explains the server's purpose.
#
# version:
#     Version of OUR application, not the MCP SDK version.


# ============================================================
# CUSTOM MCP SERVER: PROTOCOL-LEVEL INPUT VALIDATION
# ============================================================

class ValidatingMCPServer(MCPServer):
    """
    Extend the standard MCP server with JSON-RPC
    invalid-parameter handling.

    Why this is necessary:
        Our Pydantic models already reject invalid inputs.

        However, the MCP SDK normally converts exceptions
        raised inside tool functions into tool-level errors
        with is_error=True.

        The assessment requires JSON-RPC error code -32602
        for invalid request parameters.

    Design:
        Validate arguments before executing the tool.

        If validation fails, raise MCPError(-32602).

        Otherwise, delegate execution to the standard
        MCPServer implementation.
    """

    async def call_tool(
        self,
        name: str,
        arguments: dict,
        context=None,
    ):
        """
        Validate known tool arguments before dispatch.

        This method overrides the public MCPServer.call_tool()
        method, not the SDK's private request handler.
        """

        # Associate each registered tool with the Pydantic
        # model responsible for validating its arguments.
        validation_models = {
            "get_customer_record": CustomerLookupRequest,
            "trigger_refund": RefundRequest,
        }

        # Look up the validation model for the requested tool.
        model = validation_models.get(name)

        # Only validate tools that have an associated model.
        #
        # Unknown tools remain the SDK's responsibility.
        if model is not None:
            try:
                # Validate the entire arguments dictionary.
                #
                # This checks:
                #   - Required fields
                #   - Strict input types
                #   - Customer ID formatting
                #   - Positive refund amounts
                #   - Decimal precision
                #   - Refund reason length
                #   - Unexpected extra fields
                model.model_validate(arguments)

            except ValidationError as error:
                # Log only the rejected field names.
                #
                # Avoid logging customer-supplied values,
                # which may contain sensitive information.
                invalid_fields = sorted({
                    ".".join(str(part) for part in item["loc"])
                    for item in error.errors()
                })

                logger.warning(
                    "Rejected invalid arguments for %s: %s",
                    name,
                    invalid_fields,
                )

                # Raise a protocol-level error rather than
                # returning an MCP tool result with is_error=True.
                #
                # INVALID_PARAMS is the SDK constant for -32602.
                raise MCPError(
                    code=INVALID_PARAMS,
                    message="Invalid params",
                ) from error

        # If validation succeeds, execute the tool using
        # the SDK's original dispatch implementation.
        return await super().call_tool(
            name=name,
            arguments=arguments,
            context=context,
        )

server = ValidatingMCPServer(
    name="whitematter-customer-service",
    description="Customer records and refund processing MCP server",
    version="1.0.0",
)


# ============================================================
# SECTION 4: REGISTER THE CUSTOMER LOOKUP TOOL
# ============================================================

# @server.tool() is a Python decorator.
#
# A decorator modifies or registers a function's behavior.
#
# Here, it tells MCPServer:
# "Make the following Python function available as an MCP tool."
#
# MCP clients can discover registered tools through tools/list
# and invoke them through tools/call.

@server.tool(
    name="get_customer_record",
    description="Retrieve a customer record using its customer ID.",
)
def get_customer_record(customer_id: str) -> dict:
    """
    Retrieve a simulated customer record.

    Validation:
        The customer ID must follow the CUST-XXXXX format.
        Pydantic rejects invalid values before we retrieve data.
    """

    # Validate the incoming customer ID against our Pydantic model.
    #
    # If the ID is malformed, Pydantic raises ValidationError,
    # preventing execution of the remaining function.
    validated_request = CustomerLookupRequest(
        customer_id=customer_id
    )

    # Use the validated value rather than the original input.
    logger.info(
        "Looking up customer %s",
        validated_request.customer_id,
    )

    # Return mock data; no external database is required.
    return {
        "customer_id": validated_request.customer_id,
        "name": "Example Customer",
        "status": "active",
    }



# ============================================================
# SECTION 5: REGISTER THE REFUND PROCESSING TOOL
# ============================================================

# Register another tool using the same decorator pattern.
#
# This tool accepts three parameters:
#   1. customer_id - Customer receiving the refund.
#   2. amount      - Amount being refunded.
#   3. reason      - Explanation for the refund.
#
# Later, Pydantic will enforce strict validation rules.

@server.tool(
    name="trigger_refund",
    description="Simulate a customer refund.",
)
def trigger_refund(
    customer_id: str,
    amount: float,
    reason: str,
) -> dict:
    """
    Simulate a refund after validating all three inputs.

    Validation requirements:
        - Customer ID must match CUST-XXXXX.
        - Amount must be positive.
        - Reason must contain at least 10 characters.

    No actual money is transferred.
    """

    # Validate all incoming fields together.
    #
    # Pydantic checks their types and constraints.
    # Invalid inputs raise ValidationError.
    validated_request = RefundRequest(
        customer_id=customer_id,
        amount=amount,
        reason=reason,
    )

    # Only valid refund requests reach this point.
    logger.info(
        "Processing refund for %s",
        validated_request.customer_id,
    )

    # Return a simulated successful transaction.
    return {
        "customer_id": validated_request.customer_id,
        "amount": validated_request.amount,
        "reason": validated_request.reason,
        "status": "approved",
    }

# ============================================================
# SECTION 6: START THE MCP SERVER
# ============================================================

async def main():
    """
    Start the MCP server using standard input/output.

    stdio transport:
        stdin  = Incoming MCP client messages.
        stdout = Outgoing MCP protocol messages.
        stderr = Application logs and diagnostic messages.

    This separation is explicitly required by the assessment.
    """

    # Log startup information to stderr.
    logger.info("Starting WhiteMatter MCP server")

    # Start listening for MCP requests using stdio.
    #
    # 'await' allows this asynchronous operation to run
    # within Python's event loop.
    await server.run_stdio_async()


# ============================================================
# SECTION 7: PYTHON ENTRY POINT
# ============================================================

# This condition ensures that main() runs only when this
# Python file is executed directly.
#
# If another Python file imports server.py, main() will
# not automatically execute.
if __name__ == "__main__":

    # Start Python's asynchronous event loop and run main().
    asyncio.run(main())
