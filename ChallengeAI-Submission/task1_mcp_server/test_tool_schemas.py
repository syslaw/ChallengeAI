
"""
Verify the input schemas advertised through MCP tool discovery.

This helps catch differences between the tool signatures
and the stricter Pydantic models used by the server.
"""

import pytest

from task1_mcp_server.server import server


@pytest.mark.asyncio
async def test_tool_input_schemas():
    """
    Inspect the JSON schemas published for both MCP tools.
    """

    # Retrieve the tools registered with our MCP server.
    tools = await server.list_tools()

    # Create a lookup table keyed by tool name.
    tools_by_name = {
        tool.name: tool
        for tool in tools
    }

    # Verify that both required tools are registered.
    assert "get_customer_record" in tools_by_name
    assert "trigger_refund" in tools_by_name

    # Inspect the customer lookup tool's input schema.
    customer_schema = tools_by_name[
        "get_customer_record"
    ].input_schema

    # The customer ID must be a required string.
    assert "customer_id" in customer_schema["required"]
    assert (
        customer_schema["properties"]["customer_id"]["type"]
        == "string"
    )

    # Inspect the refund tool's input schema.
    refund_schema = tools_by_name[
        "trigger_refund"
    ].input_schema

    # All three refund arguments must be required.
    assert set(refund_schema["required"]) == {
        "customer_id",
        "amount",
        "reason",
    }

    # The refund amount must be numeric.
    assert refund_schema["properties"]["amount"]["type"] == "number"

    # The refund reason must be a string.
    assert refund_schema["properties"]["reason"]["type"] == "string"

    # Display the actual schemas for review.
    print("\nCustomer lookup schema:")
    print(customer_schema)

    print("\nRefund schema:")
    print(refund_schema)

