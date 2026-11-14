
import asyncio
import json
from collections.abc import AsyncIterator
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


app = FastAPI(title="Mock LLM Provider")


# Simulates the short delay between generated LLM text deltas.
# This is not a production latency requirement.
CHUNK_DELAY = 0.1


class ChatMessage(BaseModel):
    """A minimal chat message accepted by the mock provider."""

    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """A minimal OpenAI-style chat completion request."""

    model: str
    messages: list[ChatMessage]

    # This assessment focuses exclusively on streaming.
    stream: Literal[True] = True


def create_sse_event(content: str) -> str:
    """
    Convert a text delta into an OpenAI-style SSE event.

    The text is placed inside a JSON payload, and the
    payload is wrapped using SSE event boundaries.
    """

    payload = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "content": content
                },
                "finish_reason": None
            }
        ]
    }

    return f"data: {json.dumps(payload)}\n\n"


async def generate_response() -> AsyncIterator[str]:
    """
    Simulate an LLM generating a streaming response.

    Sensitive values are deliberately split across multiple
    events to test the gateway's stateful PII detection.
    """

    # These are fictional demonstration values.
    # Never use real customer PII in test fixtures.
    chunks = [
        "Hello! Here is the customer information you requested. ",
        "The customer's email is ",
        "alice@",
        "example",
        ".com",
        ". Their SSN is ",
        "123-45-",
        "6789",
        ". Their credit card number is ",
        "4111 ",
        "1111 ",
        "1111 ",
        "1111",
        ". Please handle this information securely."
    ]

    # Yield each delta independently rather than returning
    # the complete response in a single HTTP message.
    for chunk in chunks:
        yield create_sse_event(chunk)

        # Simulate model generation without blocking
        # the asynchronous event loop.
        await asyncio.sleep(CHUNK_DELAY)

    # Send a completion event similar to what an LLM
    # provider sends when it finishes generating text.
    completion = {
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": "stop"
            }
        ]
    }

    yield f"data: {json.dumps(completion)}\n\n"

    # Signal the end of the SSE stream.
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest):
    """
    Accept a chat completion request and return an SSE stream.

    The mock returns a deterministic response instead of
    calling a real language model.
    """

    return StreamingResponse(
        generate_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache"
        }
    )
