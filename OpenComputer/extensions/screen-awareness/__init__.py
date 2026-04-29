"""Screen-awareness — event-driven screen capture for self-understanding.

Captures the primary screen via OCR at three event triggers:
- User submits a message (BEFORE_MESSAGE_WRITE filtered to role=user)
- LLM about to call a GUI-mutating tool (PRE_TOOL_USE)
- GUI-mutating tool returns (POST_TOOL_USE)

Default OFF. Opt-in via per-profile state file + F1 EXPLICIT consent
grant for ``introspection.ambient_screen``. Mirrors privacy contract of
Phase 1 ambient-sensors (sensitive-app denylist, lock/sleep skip, AST
no-egress test, OCR text only — no image bytes persisted).
"""
