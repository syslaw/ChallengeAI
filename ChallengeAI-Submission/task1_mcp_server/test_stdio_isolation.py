

"""
Verify that the MCP server keeps its protocol output
separate from application logging.

stdout: MCP JSON-RPC messages only
stderr: Application logs and diagnostics
"""

import json
import subprocess
import sys

import pytest


def test_stdio_output_isolation():
    """
    Start the MCP server as a subprocess, send a real
    JSON-RPC initialization request, and inspect both
    stdout and stderr independently.
    """

    # Construct an MCP initialization request.
    #
    # The protocolVersion identifies the MCP protocol
    # version requested by the client.
    initialization_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {
                "name": "stdio-isolation-test",
                "version": "1.0.0",
            },
        },
    }

    # Launch the server with separate pipes for its
    # standard input, output, and error streams.
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "task1_mcp_server.server",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Send the initialization request as one JSON line.
        #
        # The MCP stdio transport uses newline-delimited
        # JSON-RPC messages.
        stdout, stderr = process.communicate(
            input=json.dumps(initialization_request) + "\n",
            timeout=10,
        )

    except subprocess.TimeoutExpired:
        # Avoid leaving a background server running if
        # something unexpectedly hangs.
        process.kill()
        process.communicate()
        pytest.fail("MCP server did not respond within 10 seconds.")

    # Collect nonempty lines from stdout.
    output_lines = [
        line for line in stdout.splitlines()
        if line.strip()
    ]

    # The initialization request should produce a response.
    assert output_lines, "No JSON-RPC response received."

    # Every stdout line must contain valid JSON.
    #
    # A stray logging statement such as:
    # print("Starting server")
    # would cause this check to fail.
    for line in output_lines:
        message = json.loads(line)

        # Verify that the output is a JSON-RPC message.
        assert message["jsonrpc"] == "2.0"

    # Find the response corresponding to our request ID.
    responses = [
        json.loads(line)
        for line in output_lines
        if json.loads(line).get("id") == 1
    ]

    assert len(responses) == 1

    # Initialization should return a result, not an error.
    assert "result" in responses[0]

    # The server's startup log should appear on stderr.
    assert "Starting WhiteMatter MCP server" in stderr

    # The startup log must not appear on stdout.
    assert "Starting WhiteMatter MCP server" not in stdout

    print("\nStdout contains valid JSON-RPC messages.")
    print("Application logs are isolated to stderr.")

