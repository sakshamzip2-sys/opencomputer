"""F1 consent layer — core module, always loaded.

Lives in core (not extensions/) because the gate MUST NOT be disable-able.
If consent were a plugin and the user ran `opencomputer plugin disable
user-model-consent`, every privileged tool call would silently bypass
the security boundary. Keeping this in `opencomputer.agent.consent` and
invoking it from AgentLoop before any PreToolUse hook makes the gate
non-bypassable.

See ~/.claude/plans/i-want-you-to-twinkly-squirrel.md §Architectural
approach for the full rationale (Critical #1 in the audit section).
"""
from opencomputer.agent.consent.keyring_adapter import KeyringAdapter
from opencomputer.agent.consent.store import ConsentStore

__all__ = ["ConsentStore", "KeyringAdapter"]
