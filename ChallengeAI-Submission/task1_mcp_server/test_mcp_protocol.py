
"""
Task 1: MCP Server Tool-Dispatch Tests

Purpose:
    Verify that the MCP server exposes and executes
    our registered tools correctly.

These tests differ from test_server.py:

    test_server.py:
        Tests Python functions directly.

    test_mcp_protocol.py:
        Tests tool discovery and execution through
        the MCP server's dispatch methods.

Note:
    Actual JSON-RPC communication over stdio
    will be tested separately.

Run:
    python -m pytest task1_mcp_server/test_mcp_protocol.py -v
"""

# ============================================================
# SECTION 1: IMPORTS
# ============================================================

# pytest provides our automated testing framework.
import pytest

# Import the configured MCP server instance.
#
# This is the same server that registers our two tools.
from task1_mcp_server.server import server


# ============================================================
# SECTION 2: TOOL DISCOVERY
# ============================================================

@pytest.mark.asyncio
async def test_mcp_tool_registration():
    """
    Verify that the MCP server advertises both required tools.

    MCP clients need to discover a tool before they can
    decide how to invoke it.
    """

    # list_tools() is asynchronous in MCP SDK 2.3.0,
    # so we use await to retrieve its result.
    tools = await server.list_tools()

    # Extract the name of each registered tool.
    tool_names = [tool.name for tool in tools]

    # Confirm that both assessment-required tools exist.
    assert "get_customer_record" in tool_names
    assert "trigger_refund" in tool_names


# ============================================================
# SECTION 3: CUSTOMER LOOKUP THROUGH MCP
# ============================================================

@pytest.mark.asyncio
async def test_mcp_customer_lookup():
    """
    Execute a customer lookup through the MCP server.

    This verifies the server can locate and invoke
    the registered tool using its MCP name.
    """

    # The arguments dictionary represents the inputs
    # an MCP client would supply for this tool.
    result = await server.call_tool(
        name="get_customer_record",
        arguments={
            "customer_id": "CUST-12345",
        },
    )

    # A successful MCP tool response should not be marked as an error.
    # The MCP SDK exposes the error flag as is_error.
    # False indicates that the tool completed successfully.
    assert result.is_error is False

    # Verify that the response contains our customer ID.
    #
    # We use str(result) initially because the SDK
    # may wrap tool output in structured content objects.
    assert "CUST-12345" in str(result)


# ============================================================
# SECTION 4: REFUND THROUGH MCP
# ============================================================

@pytest.mark.asyncio
async def test_mcp_refund():
    """
    Execute a valid refund through the MCP server.

    This checks that MCP dispatch reaches our refund
    validation and business logic.
    """

    result = await server.call_tool(
        name="trigger_refund",
        arguments={
            "customer_id": "CUST-12345",
            "amount": 25.40,
            "reason": "Incorrect item delivered",
        },
    )

    # The refund should complete successfully.
    # Verify that the refund tool completed without an MCP error.
    assert result.is_error is False

    # Check that the simulated approval appears
    # in the returned MCP result.
    assert "approved" in str(result)
