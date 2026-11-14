
import json


def create_sse_event(content: str) -> str:
    """
    Convert a text delta into an OpenAI-style SSE event.

    Each event contains a JSON payload followed by two
    newline characters, which mark the event boundary.
    """

    # Construct the portion of an OpenAI streaming response
    # that contains newly generated text.
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

    # Serialize the Python dictionary into valid JSON.
    # json.dumps safely handles quotes, newlines, and other
    # characters that require escaping inside JSON strings.
    json_payload = json.dumps(payload)

    # SSE messages begin with "data:" and terminate with
    # a blank line so the client knows the event is complete.
    return f"data: {json_payload}\n\n"
