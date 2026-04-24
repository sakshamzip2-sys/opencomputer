"""Bundled settings variants (III.3).

Three starter config.yaml profiles users can initialize via
``opencomputer config init --variant <lax|strict|sandbox>``. Mirrors
Claude Code's ``sources/claude-code/examples/settings/`` templates —
each variant pre-wires a security posture (permissive, conservative,
or sandboxed) on top of the same underlying :class:`Config` fields.

Adding a new variant: drop ``myvariant.yaml`` beside this module. The
CLI auto-discovers files here — no registration step needed.
"""
