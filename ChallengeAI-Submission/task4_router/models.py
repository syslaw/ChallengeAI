
"""Request validation and token estimation for Task 4."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from task4_router.config import (
    DEFAULT_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MAX_PROMPT_CHARACTERS,
)


class CompletionRequest(BaseModel):
    """Supported non-streaming text completion request."""

    # Reject unknown fields rather than silently forwarding
    # unsupported provider options.
    #
    # Strict mode prevents unexpected type coercion,
    # such as converting "500" into an integer.
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
    )

    prompt: str = Field(
        min_length=1,
        max_length=MAX_PROMPT_CHARACTERS,
    )

    max_tokens: int = Field(
        default=DEFAULT_OUTPUT_TOKENS,
        ge=1,
        le=MAX_OUTPUT_TOKENS,
    )

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, value: str) -> str:
        """Reject blank prompts and invalid Unicode."""

        if not value.strip():
            raise ValueError("Prompt must not be blank.")

        # Ensure the prompt can be encoded safely before
        # it reaches token estimation or HTTP serialization.
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "Prompt contains invalid Unicode."
            ) from None

        # Preserve the original prompt without modifying it.
        return value

    def estimated_input_tokens(self) -> int:
        """Return a conservative, deterministic token estimate.

        UTF-8 byte length is used instead of an approximate
        characters-per-token ratio.

        This is an admission estimate, not exact model-
        specific tokenization.
        """

        return len(self.prompt.encode("utf-8"))

    def reserved_tokens(self) -> int:
        """Return the token budget required before routing."""

        return self.estimated_input_tokens() + self.max_tokens