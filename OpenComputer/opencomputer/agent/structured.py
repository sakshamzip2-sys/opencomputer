"""Structured-outputs helper — schema-validated LLM responses.

Wraps :meth:`plugin_sdk.BaseProvider.complete` with the new
``response_schema`` kwarg (Subsystem C, 2026-05-02) so callers can
write::

    result = await parse_structured(
        response_model=MyPydanticModel,
        messages=[Message(role="user", content="...")],
        provider=provider,
        model="claude-opus-4-7",
    )
    assert isinstance(result, MyPydanticModel)

Without this helper, every caller would re-implement the same dance:
generate JSON Schema from the Pydantic model, pack it as
``JsonSchemaSpec``, parse the response text as JSON, validate against
the model, raise on mismatch.

Provider-agnostic: works for any provider that accepts the
``response_schema`` kwarg. Anthropic and OpenAI providers wire it to
their native schema-enforcement (server-side guarantees). Providers
without native support pass through as no-op — callers should add
explicit JSON instructions to the prompt as a backup, and this helper
will still parse + validate post-hoc (raising
:class:`StructuredOutputError` on schema violations the model produced
because the provider didn't enforce).
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from plugin_sdk.core import Message
from plugin_sdk.provider_contract import BaseProvider, JsonSchemaSpec


class StructuredOutputError(Exception):
    """Raised when a structured-output response can't be parsed/validated.

    Common causes:
      * Provider returned non-JSON text (only happens with providers
        that don't enforce schema natively — Anthropic/OpenAI shouldn't
        produce this on a successful 2xx response).
      * JSON parsed successfully but failed Pydantic validation
        (schema mismatch the LLM somehow snuck through).
    """

    def __init__(self, message: str, *, raw_text: str | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text


async def parse_structured[T: BaseModel](
    *,
    response_model: type[T],
    messages: list[Message],
    provider: BaseProvider,
    model: str,
    system: str = "",
    max_tokens: int = 4096,
    name: str | None = None,
) -> T:
    """Call ``provider.complete`` with ``response_schema``, parse + validate.

    Parameters
    ----------
    response_model:
        Pydantic ``BaseModel`` subclass describing the expected output
        shape. The model's ``model_json_schema()`` is sent to the
        provider for server-side enforcement.
    messages:
        User/assistant message list as you'd pass to ``complete``.
    provider:
        Any :class:`plugin_sdk.BaseProvider`.
    model:
        Model id to dispatch to.
    system:
        Optional system prompt.
    max_tokens:
        Maximum response tokens.
    name:
        Optional schema name (surfaced to OpenAI's ``json_schema.name``
        field). Defaults to the Pydantic model's class name lowercased.

    Returns
    -------
    An instance of ``response_model`` populated from the validated
    response.

    Raises
    ------
    StructuredOutputError
        If the provider's response can't be parsed as JSON, or if
        Pydantic validation fails.
    """
    schema = response_model.model_json_schema()
    spec: JsonSchemaSpec = {
        "schema": schema,
        "name": name or response_model.__name__.lower(),
    }

    response = await provider.complete(
        model=model,
        messages=messages,
        system=system,
        max_tokens=max_tokens,
        response_schema=spec,
    )

    text = (response.message.content or "").strip()
    if not text:
        raise StructuredOutputError(
            "provider returned empty content (no JSON to parse)",
            raw_text=text,
        )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(
            f"provider response was not valid JSON: {exc}",
            raw_text=text,
        ) from exc

    try:
        return response_model.model_validate(parsed)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"response failed schema validation: {exc}",
            raw_text=text,
        ) from exc


__all__ = ["parse_structured", "StructuredOutputError"]
