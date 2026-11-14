
import pytest

from task3_guardrail.redactor import (
    MAX_PENDING,
    StreamingRedactor,
    redact_pii,
)


# Each case defines the original text and the expected
# result after all supported PII has been replaced.
CASES = [
    (
        "Hello, how are you?",
        "Hello, how are you?",
    ),
    (
        "Contact alice@example.com for help.",
        "Contact [REDACTED] for help.",
    ),
    (
        "Email: alice.smith+work@example.com",
        "Email: [REDACTED]",
    ),
    (
        "SSN: 123-45-6789.",
        "SSN: [REDACTED].",
    ),
    (
        "Card: 4111 1111 1111 1111.",
        "Card: [REDACTED].",
    ),
    (
        "Card: 4111-1111-1111-1111.",
        "Card: [REDACTED].",
    ),
    (
        "Card: 4111111111111111.",
        "Card: [REDACTED].",
    ),
    (
        "I have 2 dogs and 3 cats.",
        "I have 2 dogs and 3 cats.",
    ),
    (
        "Email alice@example.com, SSN 123-45-6789, "
        "card 4111 1111 1111 1111.",
        "Email [REDACTED], SSN [REDACTED], "
        "card [REDACTED].",
    ),
]


@pytest.mark.parametrize("original, expected", CASES)
def test_complete_pii_patterns(original, expected):
    """Verify supported patterns and ordinary text."""

    assert redact_pii(original) == expected


@pytest.mark.parametrize("original, expected", CASES)
def test_every_possible_split(original, expected):
    """
    Verify that redaction does not depend on where the
    model chooses to split its response into deltas.
    """

    for split in range(len(original) + 1):

        redactor = StreamingRedactor()

        result = redactor.feed(original[:split])
        result += redactor.feed(original[split:])
        result += redactor.finish()

        assert result == expected, (
            f"Incorrect output at split {split}"
        )


@pytest.mark.parametrize("original, expected", CASES)
def test_single_character_deltas(original, expected):
    """
    Test the extreme case of one character per delta.
    """

    redactor = StreamingRedactor()

    result = "".join(
        redactor.feed(char)
        for char in original
    )

    result += redactor.finish()

    assert result == expected


def test_incremental_safe_output():
    """
    Safe text should be available before generation ends,
    while unresolved PII must remain buffered.
    """

    redactor = StreamingRedactor()

    assert redactor.feed("Contact ") == "Contact "

    assert redactor.feed("alice@") == ""
    assert redactor.feed("example") == ""

    assert redactor.feed(".com ") == "[REDACTED] "

    assert redactor.pending_length == 0


def test_final_flush():
    """Classify the final candidate without a delimiter."""

    redactor = StreamingRedactor()

    assert redactor.feed("alice@example.com") == ""

    assert redactor.finish() == "[REDACTED]"

    assert redactor.pending_length == 0

    # Calling finish again must not duplicate output.
    assert redactor.finish() == ""


def test_pending_buffer_limit():
    """
    Oversized candidates must not cause unbounded memory
    growth or leak unclassified text.
    """

    redactor = StreamingRedactor()

    output = []

    for _ in range(MAX_PENDING * 3):

        output.append(redactor.feed("a"))

        assert redactor.pending_length <= MAX_PENDING

    output.append(
        redactor.feed("@example.com normal text.")
    )

    output.append(redactor.finish())

    assert "".join(output) == (
        "[REDACTED] normal text."
    )


def test_redactor_instances_are_isolated():
    """
    Separate requests must never share pending PII state.
    """

    first = StreamingRedactor()
    second = StreamingRedactor()

    assert first.feed("alice@") == ""

    assert second.feed("Hello ") == "Hello "

    assert first.feed("example.com ") == "[REDACTED] "

    assert second.finish() == ""
    assert first.finish() == ""
