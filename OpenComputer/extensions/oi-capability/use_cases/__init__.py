# ruff: noqa: N999  # directory name 'oi-capability' has a hyphen (required by plugin manifest)
"""Use-case function libraries for the OI Capability plugin.

These modules compose OI tools into higher-level patterns. They are NOT
registered as tools — they are callable from tests and from Session A's
eventual Phase 5 wiring.

Available modules:
  - autonomous_refactor           — Autonomous code refactoring/migration helper
  - life_admin                    — Calendar management and scheduling
  - personal_knowledge_management — PKM helper for notes and action items
  - proactive_security_monitoring — Security scanning and threat detection
  - dev_flow_assistant            — Development workflow helpers
  - email_triage                  — Email classification and draft generation
  - context_aware_code_suggestions — Code context gathering
  - temporal_pattern_recognition  — Temporal usage pattern analysis
"""
