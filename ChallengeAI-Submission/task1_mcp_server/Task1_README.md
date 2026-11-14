# Task 1 — Custom MCP Server with Strict Validation & Transport Handling

## Overview

This project implements a lightweight Model Context Protocol (MCP) server using Python and the official MCP SDK. The server exposes two tools for retrieving customer records and initiating simulated refunds, with strict input validation and protocol-compliant error handling.

The implementation focuses on three core requirements:

- Strict input schema validation using Pydantic.
- Standard MCP JSON-RPC error handling.
- Clean stdio transport with complete separation between protocol messages and application logging.

## Technology Stack

- Python 3.13
- Official Python MCP SDK
- Pydantic
- pytest
- pytest-asyncio

## Installation

From the project root, create and activate a Python virtual environment if one does not already exist:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the project dependencies:

```bash
pip install -r requirements.txt
```

## Running the Server

From the project root:

```bash
python -m task1_mcp_server.server
```

The server communicates using stdio transport and is intended to be launched by an MCP-compatible client.

The server does not expose an HTTP endpoint. MCP clients communicate with the server through its standard input and output streams.

## Available Tools

### 1. get_customer_record

Retrieves a simulated customer record using a validated customer identifier.

**Input schema:**

| Parameter | Type | Validation |
| --- | --- | --- |
| customer_id | string | Must match `CUST-XXXXX`, where each X is a digit |

Example arguments:

```json
{
  "customer_id": "CUST-12345"
}
```

Customer records are simulated for the purposes of this assessment.

### 2. trigger_refund

Initiates a simulated refund for a customer.

**Input schema:**

| Parameter | Type | Validation |
| --- | --- | --- |
| customer_id | string | Must match `CUST-XXXXX` |
| amount | float | Must be greater than zero |
| reason | string | Must contain at least 10 characters |

Example arguments:

```json
{
  "customer_id": "CUST-12345",
  "amount": 49.99,
  "reason": "Duplicate customer charge"
}
```

Refund processing is simulated. No external payment processor is connected, and no actual financial transactions are performed.

## Architecture

```text
MCP Client
    |
    | JSON-RPC over stdio
    v
MCP Server
    |
    v
Pydantic Input Validation
    |
    +-- Invalid input --> JSON-RPC -32602
    |
    +-- Valid input ----> Tool Execution
                              |
                              v
                        Mock Customer Data
                        / Refund Processing
```

The MCP SDK manages the protocol lifecycle and stdio transport. Pydantic models enforce the input requirements before the corresponding tool performs its business logic.

This separation keeps validation, protocol handling, and business logic independently understandable and testable.

## Input Validation

Strict Pydantic validation is used to reject malformed or invalid tool arguments before execution.

Validation includes:

- Customer identifiers that do not follow the required format.
- Missing required parameters.
- Incorrect input types.
- Zero or negative refund amounts.
- Refund reasons shorter than 10 characters.

Invalid arguments are rejected rather than silently corrected or passed to the tool implementation.

## JSON-RPC Error Handling

Invalid tool parameters are mapped to the standard JSON-RPC error code:

```text
-32602 — Invalid params
```

For example, the following arguments are invalid:

```json
{
  "customer_id": "INVALID",
  "amount": -25.00,
  "reason": "Refund"
}
```

The server returns a protocol-level error instead of executing the refund operation.

This ensures that MCP clients can distinguish invalid tool inputs from successful tool execution and handle validation failures programmatically.

## Transport and Logging

The server uses stdio transport, with explicit separation between protocol traffic and application diagnostics.

| Stream | Purpose |
| --- | --- |
| stdin | Incoming MCP JSON-RPC messages |
| stdout | Outgoing MCP JSON-RPC messages only |
| stderr | Application, system, and debug logging |

Application logging is directed exclusively to stderr. Diagnostic print statements are avoided on stdout to prevent corruption of the MCP communication stream.

This is particularly important because an MCP client expects stdout to contain valid protocol messages rather than a mixture of protocol messages and human-readable application output.

## Testing

Run the Task 1 automated test suite from the project root:

```bash
python -m pytest task1_mcp_server -v
```

The test suite verifies successful tool execution, input validation, malformed argument handling, and protocol behavior.

Key test scenarios include:

- Valid customer record requests.
- Valid refund requests.
- Invalid customer identifier formats.
- Missing or incorrectly typed arguments.
- Zero and negative refund amounts.
- Refund reasons below the minimum length.
- JSON-RPC invalid-parameter error handling.

### Test Results

```text
32 passed
```

All 32 Task 1 automated tests passed during assessment validation.

## Design Decisions

### Official MCP SDK

The official SDK is used instead of implementing the MCP protocol manually. This reduces unnecessary protocol implementation complexity and provides a standard foundation for MCP client interoperability.

### Strict Pydantic Validation

Input validation is performed before business logic execution. This prevents malformed requests from reaching customer lookup or refund processing and provides predictable error behavior.

### Protocol-Level Errors

Invalid parameters are reported through standard JSON-RPC errors rather than being returned as ordinary successful tool responses containing error messages.

### Stdio Isolation

Protocol traffic and application logging are intentionally separated. This protects the integrity of the MCP communication channel and supports reliable client-server interaction.

### Lightweight Mock Business Logic

Customer retrieval and refund processing use simulated data and behavior. This keeps the implementation focused on MCP protocol compliance, transport handling, and validation rather than unrelated external system integrations.

## Assessment Requirements

| Requirement | Implementation |
| --- | --- |
| Runnable MCP server using the official SDK | Implemented |
| `get_customer_record` tool | Implemented |
| `trigger_refund` tool | Implemented |
| Strict Pydantic schema validation | Implemented |
| Standard JSON-RPC invalid-parameter errors | Implemented |
| Stdio transport | Implemented |
| JSON-RPC-only stdout | Implemented |
| Application logging directed to stderr | Implemented |
| Automated functional and validation testing | 32 tests passing |

## Limitations and Production Considerations

This implementation is intentionally scoped to the assessment requirements.

A production deployment would additionally require integration with an authoritative customer data source, authentication and authorization controls, secure payment processor integration, refund idempotency, and durable audit logging.

These capabilities are outside the scope of Task 1.

## Conclusion

This implementation demonstrates a functional MCP server with strict schema validation, standard JSON-RPC error handling, and clean stdio transport. The solution satisfies the core assessment requirements while maintaining a lightweight architecture and independently testable components.