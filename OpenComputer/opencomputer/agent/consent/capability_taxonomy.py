"""F1 built-in capability taxonomy. Later phases extend.

Capability IDs are flat dotted strings. scope_filter (when used) is a
shell-style path or directory prefix; matching happens in ConsentGate.check
via `startswith` check.
"""
from plugin_sdk import ConsentTier

F1_CAPABILITIES: dict[str, ConsentTier] = {
    "consent.grant": ConsentTier.EXPLICIT,
    "consent.revoke": ConsentTier.IMPLICIT,
    # F8 (voice — Phase 1.1 of catch-up plan)
    "voice.synthesize": ConsentTier.IMPLICIT,
    "voice.transcribe": ConsentTier.IMPLICIT,
    # F9 (channels — Phase 1.3 of catch-up plan).
    # Auto-granted by `opencomputer pair <platform>` after format + live
    # check pass. Tier EXPLICIT means: user-confirmed but not per-message.
    "channel.send.telegram": ConsentTier.EXPLICIT,
    "channel.send.discord": ConsentTier.EXPLICIT,
    "channel.send.slack": ConsentTier.EXPLICIT,
    # F7 (GUI control — Phase 2.1 + 2.2 of catch-up plan).
    # macOS-only. Tier PER_ACTION because each click / script can mutate
    # arbitrary OS state — confirm every call until user explicitly
    # promotes the grant. Destructive-keyword denylist on AppleScript
    # is defence-in-depth, NOT the primary gate (consent is).
    "gui.point_click": ConsentTier.PER_ACTION,
    "gui.applescript_run": ConsentTier.PER_ACTION,
    # Phase 5.B-3 of catch-up plan — procedural-memory loop.
    # The agent observes its own tool-use patterns and drafts new
    # SKILL.md files for repeated patterns. Drafts go to quarantine;
    # the user must explicitly approve via the CLI before activation.
    # EXPLICIT tier means user opted in once (revocable); per-draft
    # approval is a *separate* user action, not consent-gate enforced.
    "procedural_memory.write_skill": ConsentTier.EXPLICIT,
    # Phase 8.A of catch-up plan — web UI dashboard.
    # Default localhost-only; non-localhost binding requires this capability.
    # Tier EXPLICIT means user opted in once; we don't re-prompt every start.
    "dashboard.bind_external": ConsentTier.EXPLICIT,
    # MVP — Layered Awareness ingestion sources (2026-04-26).
    # Per-source consent so user can revoke any single ingestion path
    # via `opencomputer consent revoke <id>` without affecting others.
    #
    # ingestion.recent_files is IMPLICIT because it reads only file
    # metadata (path, mtime, size_bytes) — never content. Implementations
    # under this grant MUST NOT open file contents; that requires a
    # separate EXPLICIT capability (added when content reads are wired).
    "ingestion.recent_files": ConsentTier.IMPLICIT,
    "ingestion.git_log": ConsentTier.IMPLICIT,
    "ingestion.calendar": ConsentTier.EXPLICIT,
    "ingestion.browser_history": ConsentTier.EXPLICIT,
    "ingestion.messages": ConsentTier.EXPLICIT,
    "ingestion.browser_extension": ConsentTier.EXPLICIT,
}

# Reserved for later phases (documented, not enforced here):
# F2: read_files.metadata, read_files.content
# F6: scrape.github, scrape.linkedin, scrape.reddit, scrape.twitter, scrape.open_license
# F7: read_clipboard, read_screen.motif, read_mail.metadata, exec_applescript
#     (read_browser_history is now realized as ingestion.browser_history above)
# F9: exec_shell, exec_network, write_file
