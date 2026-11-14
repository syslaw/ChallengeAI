
import re
import string


REDACTED = "[REDACTED]"
MAX_PENDING = 320


# Characters that can appear inside our supported PII patterns.
# Other characters, such as commas, colons, and parentheses,
# generally act as candidate boundaries.
PII_CHARS = frozenset(
    string.ascii_letters + string.digits + "._%+-@"
)

DIGITS = frozenset(string.digits)


# Practical ASCII email detection.
# Supports common local-part characters and domain names.
# This is not intended to validate every RFC-compliant email.
EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@"
    r"[A-Za-z0-9-]+"
    r"(?:\.[A-Za-z0-9-]+)*"
    r"\.[A-Za-z]{2,}"
)


# US Social Security numbers in the XXX-XX-XXXX format.
SSN_PATTERN = re.compile(
    r"(?<![0-9])[0-9]{3}-[0-9]{2}-[0-9]{4}(?![0-9])"
)


# Card-like sequences of 13 to 19 digits.
# Digits may be separated by a single space or hyphen.
CARD_PATTERN = re.compile(
    r"(?<![0-9])[0-9](?:[ -]?[0-9]){12,18}(?![0-9])"
)


def redact_pii(text: str) -> str:
    """
    Replace supported PII patterns in a complete candidate.

    Emails are processed first, followed by card numbers
    and SSNs. This prevents numeric matching from altering
    an email address before it can be detected.
    """

    text = EMAIL_PATTERN.sub(REDACTED, text)
    text = CARD_PATTERN.sub(REDACTED, text)
    text = SSN_PATTERN.sub(REDACTED, text)

    return text


class StreamingRedactor:
    """
    Incrementally redact PII across streaming text deltas.

    Only unresolved candidate text is retained between
    calls to feed(). The full response is never stored.
    """

    def __init__(self):
        # Text that has not yet been classified.
        self._pending = ""

        # True when we encounter a candidate that exceeds
        # our maximum buffer size.
        self._discarding = False

        # Tracks whether the preceding character was a digit.
        self._last_was_digit = False

        # A space following a digit might belong to a card
        # number. Wait for the next character to decide.
        self._waiting_for_digit = False

    @property
    def pending_length(self) -> int:
        """Expose the buffer length for testing."""

        return len(self._pending)

    def _append(self, char: str) -> str:
        """
        Add a character to the unresolved candidate.

        If the candidate becomes too large, redact it and
        discard its remaining characters until a boundary.
        """

        if self._discarding:
            return ""

        # Check before appending so the pending buffer
        # never exceeds MAX_PENDING.
        if len(self._pending) >= MAX_PENDING:
            self._pending = ""
            self._discarding = True
            return REDACTED

        self._pending += char
        return ""

    def _flush(self) -> str:
        """
        Classify the pending candidate and reset its state.

        Oversized candidates have already been replaced
        with [REDACTED], so do not emit them again.
        """

        if self._discarding:
            result = ""
        else:
            result = redact_pii(self._pending)

        self._pending = ""
        self._discarding = False
        self._last_was_digit = False
        self._waiting_for_digit = False

        return result

    def feed(self, text: str) -> str:
        """
        Accept a new text delta and return sanitized output.

        An empty result means that more characters are
        needed before the pending candidate can be released.
        """

        output = []

        for char in text:

            # A previously encountered space may belong
            # to a credit card. If the next character is
            # not a digit, the previous candidate is done.
            if self._waiting_for_digit:
                self._waiting_for_digit = False

                if char not in DIGITS:
                    output.append(self._flush())

            # Letters, digits, and supported punctuation
            # might be part of an email, SSN, or card.
            if char in PII_CHARS:
                output.append(self._append(char))
                self._last_was_digit = char in DIGITS

            # Preserve a space after a digit temporarily.
            # The next character determines whether the
            # numeric candidate continues.
            elif char == " " and self._last_was_digit:
                output.append(self._append(char))
                self._waiting_for_digit = True
                self._last_was_digit = False

            else:
                # A safe boundary has been reached.
                # Classify the candidate BEFORE releasing it.
                output.append(self._flush())

                # Preserve the boundary character itself.
                output.append(char)

        return "".join(output)

    def finish(self) -> str:
        """
        Flush the final candidate at the end of generation.

        This must be called before sending the upstream
        completion marker to the client.
        """

        return self._flush()
