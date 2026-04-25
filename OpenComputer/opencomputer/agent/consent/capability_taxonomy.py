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
}

# Reserved for later phases (documented, not enforced here):
# F2: read_files.metadata, read_files.content
# F6: scrape.github, scrape.linkedin, scrape.reddit, scrape.twitter, scrape.open_license
# F7: read_clipboard, read_screen.motif, read_mail.metadata, read_browser_history, exec_applescript
# F9: exec_shell, exec_network, write_file
