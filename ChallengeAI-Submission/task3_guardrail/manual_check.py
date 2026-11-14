
import asyncio
import json
import time

import httpx


PROVIDER_URL = (
    "http://127.0.0.1:8003/v1/chat/completions"
)

GATEWAY_URL = (
    "http://127.0.0.1:8002/v1/chat/completions"
)


# Use the exact same request for both services.
REQUEST = {
    "model": "mock-llm",
    "messages": [
        {
            "role": "user",
            "content": "Provide the customer information."
        }
    ],
    "stream": True,
}


# Fictional sensitive values from our mock provider.
SENSITIVE_VALUES = [
    "alice@example.com",
    "123-45-6789",
    "4111 1111 1111 1111",
]


async def inspect_stream(url: str) -> dict:
    """
    Read an SSE response incrementally and collect test results.

    This is a test client, not part of the gateway.
    Collecting the complete output here is intentional
    because we need to verify the final sanitized text.
    """

    chunks = []
    events = []

    first_content_time = None
    done_time = None

    async with httpx.AsyncClient(timeout=10.0) as client:

        # Start measuring before sending the HTTP request.
        start = time.perf_counter()

        async with client.stream(
            "POST",
            url,
            json=REQUEST,
        ) as response:

            response.raise_for_status()

            assert response.headers["content-type"].startswith(
                "text/event-stream"
            )

            # Read each SSE line as it arrives.
            async for line in response.aiter_lines():

                if not line.startswith("data:"):
                    continue

                payload = line[5:].strip()

                if payload == "[DONE]":
                    events.append("done")
                    done_time = time.perf_counter() - start
                    break

                event = json.loads(payload)

                # Fail the test if the gateway reports
                # an upstream streaming error.
                assert "error" not in event, event

                choice = event["choices"][0]
                delta = choice["delta"]

                content = delta.get("content")

                if content:
                    # Record when the first actual text
                    # becomes available to the client.
                    if first_content_time is None:
                        first_content_time = (
                            time.perf_counter() - start
                        )

                    chunks.append(content)
                    events.append("content")

                if choice.get("finish_reason"):
                    events.append("finish")

    total_time = time.perf_counter() - start

    return {
        "text": "".join(chunks),
        "chunks": chunks,
        "events": events,
        "first_content_time": first_content_time,
        "done_time": done_time,
        "total_time": total_time,
    }


async def main():

    print("\n--- DIRECT MOCK PROVIDER ---")

    provider = await inspect_stream(PROVIDER_URL)

    print(provider["text"])

    print("\n--- THROUGH PII GATEWAY ---")

    gateway = await inspect_stream(GATEWAY_URL)

    print(gateway["text"])

    # -------------------------------------------------
    # Check 1: Verify the mock actually generated PII.
    # -------------------------------------------------

    for value in SENSITIVE_VALUES:
        assert value in provider["text"]

    print("\nPASS: Provider generated all three PII values.")

    # -------------------------------------------------
    # Check 2: Verify the complete gateway output.
    # -------------------------------------------------

    expected = provider["text"]

    for value in SENSITIVE_VALUES:
        expected = expected.replace(value, "[REDACTED]")

    assert gateway["text"] == expected

    assert gateway["text"].count("[REDACTED]") == 3

    print("PASS: All three PII values were redacted.")
    print("PASS: All remaining text was preserved.")

    # -------------------------------------------------
    # Check 3: Verify streaming responsiveness.
    # -------------------------------------------------

    print("\n--- STREAMING TIMING ---")

    for name, result in [
        ("Provider", provider),
        ("Gateway", gateway),
    ]:

        assert result["first_content_time"] is not None
        assert result["done_time"] is not None

        # Text must arrive before generation completes.
        assert (
            result["first_content_time"]
            < result["done_time"]
        )

        # Confirm multiple content events were delivered.
        assert len(result["chunks"]) > 1

        print(
            f"{name}: "
            f"first text = "
            f"{result['first_content_time']:.3f}s | "
            f"DONE = {result['done_time']:.3f}s | "
            f"content events = {len(result['chunks'])}"
        )

    print("PASS: Safe text arrived before stream completion.")

    # -------------------------------------------------
    # Check 4: Verify completion ordering.
    # -------------------------------------------------

    assert gateway["events"][-2:] == ["finish", "done"]

    # Our mock's final word is buffered until finish().
    assert gateway["chunks"][-1] == "securely."

    print("\nPASS: Final buffered text was preserved.")
    print("PASS: Finish event arrived before [DONE].")

    print("\nAll integration checks passed.")


if __name__ == "__main__":
    asyncio.run(main())