

"""
Task 1: MCP Stdio Integration Test

Purpose:
    Launch our MCP server through its actual stdio transport.

    Verify that:
        1. An MCP client can connect to the server.
        2. Both tools are discoverable.
        3. A valid tool call succeeds.
        4. An invalid tool call is rejected.

    This is an integration test, not just a direct
    Python function test.
"""

# ============================================================
# SECTION 1: IMPORTS
# ============================================================

# sys.executable identifies the Python interpreter currently
# running this test, including our virtual environment.
import sys

# pytest runs our automated test.
import pytest

# ClientSession provides the MCP client interface.
from mcp import ClientSession

# StdioServerParameters describes how to launch the server.
# stdio_client manages communication with the subprocess.
from mcp.client.stdio import StdioServerParameters, stdio_client

# === Error Handling Logic for JSON RPC ===
# MCPError represents a JSON-RPC protocol error.
from mcp.shared.exceptions import MCPError

# === Error Handling Logic for JSON RPC ===
# Import the SDK's standard Invalid params error code (-32602).
from mcp.types import INVALID_PARAMS

# ============================================================
# SECTION 2: REAL MCP CLIENT/SERVER TEST
# ============================================================

@pytest.mark.asyncio
async def test_mcp_stdio_connection():
    """
    Launch the MCP server and communicate over stdio.

    The server runs in a separate Python process.
    """

    # Tell the MCP client how to launch our server.
    #
    # -m runs the server as a Python module.
    # This preserves our project's package imports.
    server_parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "task1_mcp_server.server"],
    )

    # Start the server subprocess and establish stdio streams.
    async with stdio_client(server_parameters) as (
        read_stream,
        write_stream,
    ):

        # Create an MCP client session using those streams.
        async with ClientSession(
            read_stream,
            write_stream,
        ) as session:

            # MCP requires initialization before tool calls.
            await session.initialize()

            # Ask the running server which tools it exposes.
            tools_response = await session.list_tools()

            tool_names = [
                tool.name
                for tool in tools_response.tools
            ]

            # Both required tools must be advertised.
            assert "get_customer_record" in tool_names
            assert "trigger_refund" in tool_names

            # Call the customer lookup tool through stdio.
            valid_result = await session.call_tool(
                "get_customer_record",
                arguments={
                    "customer_id": "CUST-12345",
                },
            )

            # Confirm the valid request succeeds.
            assert valid_result.is_error is False

            # Confirm the expected customer was returned.
            assert "CUST-12345" in str(valid_result)

            # Send a deliberately invalid customer ID.
            #
            # We don't yet assume how the SDK will represent
            # this error at the client boundary.
            try:
                invalid_result = await session.call_tool(
                    "get_customer_record",
                    arguments={
                        "customer_id": "CUST-1234",
                    },
                )

            except Exception as error:
                # Print the exception observed by the client.
                print(
                    "\nClient received exception:", type(error).__name__,)
                print("Client received error code:", error.code)
                print("Client received message:", str(error))

                # This confirms the client received a failure.
                return

            # Alternatively, the SDK may return a tool result
            # marked as an error rather than raise an exception.
            print(
                "\nClient received result:",
                invalid_result,
            )

            assert invalid_result.is_error is True


# ============================================================
# SECTION 3: REFUND VALIDATION OVER REAL MCP STDIO
# ============================================================

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        # A negative refund must be rejected.
        {
            "customer_id": "CUST-12345",
            "amount": -25.50,
            "reason": "Incorrect item delivered",
        },

        # Refund amounts containing fractional cents
        # must be rejected by our custom validator.
        {
            "customer_id": "CUST-12345",
            "amount": 25.402491,
            "reason": "Incorrect item delivered",
        },

        # Refund reasons must contain at least 10 characters.
        {
            "customer_id": "CUST-12345",
            "amount": 25.50,
            "reason": "Too short",
        },

        # Missing required fields must be rejected.
        {
            "customer_id": "CUST-12345",
            "amount": 25.50,
        },

        # Unexpected fields are forbidden by our
        # Pydantic model configuration.
        {
            "customer_id": "CUST-12345",
            "amount": 25.50,
            "reason": "Incorrect item delivered",
            "approved_by": "unauthorized-user",
        },
        # Refund amounts cannot contain fractional cents.
        # This amount has six decimal places and must be rejected.
        {
            "customer_id": "CUST-12345",
            "amount": 24.501284,
            "reason": "Customer was charged incorrectly",
        },
    ],
)
async def test_invalid_refunds_over_stdio(arguments):
    """
    Verify that invalid refund requests produce JSON-RPC
    error code -32602 through the real MCP transport.

    pytest runs this function once for every argument
    dictionary listed above.
    """

    # Launch the same server used by our existing
    # successful stdio integration test.
    server_parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "task1_mcp_server.server"],
    )

    # Establish the stdio connection to the server.
    async with stdio_client(server_parameters) as (
        read_stream,
        write_stream,
    ):

        # Create an MCP client session.
        async with ClientSession(
            read_stream,
            write_stream,
        ) as session:

            # Complete the MCP initialization handshake.
            await session.initialize()

            # Each invalid request must raise MCPError.
            #
            # If the request unexpectedly succeeds,
            # pytest automatically fails the test.
            with pytest.raises(MCPError) as captured_error:
                await session.call_tool(
                    "trigger_refund",
                    arguments=arguments,
                )

            # Verify the exact protocol-level error code.
            assert captured_error.value.code == INVALID_PARAMS

            # Verify that internal validation details
            # are not exposed in the public error message.
            assert str(captured_error.value) == "Invalid params"

            # Display the JSON-RPC error details for each
            # invalid refund request during test execution.
            print("\nRejected refund amount:", arguments.get("amount"))
            print("Client received error code:", captured_error.value.code)
            print("Client received message:", str(captured_error.value))
