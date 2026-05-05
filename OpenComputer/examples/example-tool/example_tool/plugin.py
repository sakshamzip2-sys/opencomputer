"""example-tool plugin entry — registers the WordCount tool.

This is the file OC's plugin loader imports when the plugin is
discovered. Two requirements:

1. Define a ``register(api)`` function that takes a ``PluginAPI``
   (duck-typed in tests) and registers tools, hooks, channels, etc.

2. Import everything you need from ``plugin_sdk`` ONLY — never from
   ``opencomputer.*``. The SDK boundary test will fail your plugin
   otherwise.
"""

from __future__ import annotations

import logging

from plugin_sdk.tool_contract import BaseTool, ToolSchema

from .tools import count

logger = logging.getLogger("opencomputer.ext.example_tool")


class WordCount(BaseTool):
    """Count words / sentences / chars in a text snippet."""

    @classmethod
    def schema(cls) -> ToolSchema:
        return ToolSchema(
            name="WordCount",
            description=(
                "Count characters, words, and sentences in a piece of text. "
                "Useful for word-count constraints, readability heuristics, "
                "and quick text analysis."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to analyze.",
                    }
                },
                "required": ["text"],
            },
        )

    async def run(self, *, text: str) -> dict:
        result = count(text)
        return {
            "chars": result.chars,
            "words": result.words,
            "sentences": result.sentences,
        }


def register(api) -> None:  # PluginAPI duck-typed
    """OpenComputer's plugin loader calls this exactly once at startup."""
    api.register_tool(WordCount)
    logger.info("example-tool plugin: WordCount registered")
