"""Adapter: chatgpt_app/new_chat — open a fresh chat in the ChatGPT desktop app.

Strategy.INTERCEPT — drives the Electron-wrapped ChatGPT app over CDP.
v0.4 starter: navigate to the new-chat URL within the app context. v0.5's
deeper Electron layer (DEFERRED.md §C) adds first-class Electron helpers.

Pre-req: launch the ChatGPT desktop app with
``--remote-debugging-port=19223`` (or set the ``CHATGPT_CDP_PORT`` env var).
"""

from __future__ import annotations

from extensions.adapter_runner import Strategy, adapter


@adapter(
    site="chatgpt_app",
    name="new_chat",
    description=(
        "Open a new chat in the ChatGPT desktop app via Electron Chrome DevTools "
        "Protocol. Requires the desktop app already running with --remote-debugging-port. "
        "Use to compose a fresh prompt; not for sending or reading existing chats."
    ),
    domain="chat.openai.com",
    strategy=Strategy.INTERCEPT,
    browser=True,
    args=[
        {"name": "prompt", "type": "string", "default": "", "help": "Initial prompt to send"},
    ],
    columns=["status", "url"],
)
async def run(args, ctx):
    # Navigate to the in-app new-chat route. Real Electron control would
    # send a keyboard shortcut (Cmd-N); v0.4 uses a navigate fallback
    # so the adapter is testable without a running app.
    target_url = "https://chat.openai.com/?model=auto"
    nav = await ctx.navigate(target_url)
    status = "ok" if isinstance(nav, dict) else "unknown"

    prompt = (args.get("prompt") or "").strip()
    if prompt:
        # Best-effort: focus the prompt textarea and type. The exact
        # selector varies — this is a starting point.
        js = (
            "(() => { const el = document.querySelector('textarea, "
            "[contenteditable=true]'); if (el) { el.focus(); "
            f"el.value = {prompt!r}; el.dispatchEvent(new Event('input')); "
            "return 'typed'; } return 'no-textarea'; })()"
        )
        try:
            await ctx.evaluate(js)
        except Exception:  # noqa: BLE001
            status = "no-textarea"

    return [{"status": status, "url": target_url}]
