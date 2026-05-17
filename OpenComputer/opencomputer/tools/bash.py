"""Bash tool — run a shell command with a timeout.

M2 (T2.6, 2026-05-16): the Bash tool can route its command through a
resolved sandbox backend instead of the host. The agent loop's
``_resolve_sandbox_backend`` publishes the resolved
:class:`~plugin_sdk.SandboxStrategy` on
``runtime.custom["sandbox_backend_strategy"]`` just before each dispatch;
:meth:`BashTool.execute` reads it and — when present — runs the command
inside that backend. With no ``sandbox.backend`` configured the key is
never set and the existing host path runs byte-identically. The runtime
is propagated by the loop via :meth:`BashTool.set_runtime`, mirroring the
``DelegateTool`` / ``CronTool`` runtime-propagation pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os

from opencomputer.security.tirith import (
    TirithResult,
    format_findings_for_user,
)
from opencomputer.security.tirith import (
    check_command as tirith_check_command,
)
from plugin_sdk.core import ToolCall, ToolResult
from plugin_sdk.runtime_context import DEFAULT_RUNTIME_CONTEXT, RuntimeContext
from plugin_sdk.sandbox import SandboxConfig, SandboxResult, SandboxUnavailable
from plugin_sdk.tool_contract import BaseTool, ToolSchema

_log = logging.getLogger("opencomputer.tools.bash")

#: M2 (T2.6) — ``runtime.custom`` key under which the agent loop
#: publishes the resolved :class:`~plugin_sdk.SandboxStrategy` for the
#: current tool call. Kept in sync with
#: ``opencomputer.agent.loop.AgentLoop._SANDBOX_STRATEGY_KEY``.
_SANDBOX_STRATEGY_KEY = "sandbox_backend_strategy"

#: M3 (T3.3) — ``runtime.custom`` key carrying the pooled-container key
#: for a reuse-scoped call. Kept in sync with
#: ``opencomputer.agent.loop.AgentLoop._SANDBOX_CONTAINER_KEY``.
_SANDBOX_CONTAINER_KEY = "sandbox_container_key"

#: Hermes-parity infrastructure-var blocklist. These keys are stripped
#: from the BashTool subprocess env regardless of user passthrough.
#:
#: Rationale (Hermes spec, "What Each Sandbox Filters"): the agent's own
#: provider keys, gateway tokens, and OpenComputer-internal control vars
#: must not leak into shell-spawned children (npm install, git push,
#: arbitrary user scripts) — those callers should never need them, and
#: prompt-injected commands could exfiltrate them.
#:
#: Third-party tool API keys (NPM_TOKEN, AWS_ACCESS_KEY_ID, GH_TOKEN
#: when used by the user's own scripts) are NOT in this list — users
#: routinely need them in shell subprocesses.
_OC_INFRASTRUCTURE_VARS: frozenset[str] = frozenset({
    # OC + Hermes control vars
    # (matches every var with these prefixes too — see _strip_infra_env_vars).
    # Channel platform tokens — the gateway needs these, but user shells don't.
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "MATTERMOST_BOT_TOKEN",
    "MATRIX_ACCESS_TOKEN",
    "WHATSAPP_API_TOKEN",
    "SIGNAL_BOT_TOKEN",
    "SMS_BOT_TOKEN",
    "EMAIL_BOT_TOKEN",
    "DINGTALK_BOT_TOKEN",
    "FEISHU_BOT_TOKEN",
    "WECOM_BOT_TOKEN",
    "TEAMS_BOT_TOKEN",
    # Gateway control
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS",
    "OPENCOMPUTER_ALLOW_ROOT_GATEWAY",
})

#: Variable-name prefixes that imply OC / Hermes infrastructure ownership.
#: Any env var starting with one of these is stripped.
_OC_INFRASTRUCTURE_PREFIXES: tuple[str, ...] = (
    "OPENCOMPUTER_",
    "HERMES_",
    "OC_",  # OpenComputer-prefixed knobs
)


def _strip_infra_env_vars(env: dict[str, str] | None) -> dict[str, str] | None:
    """Strip OC infrastructure vars from a copy of ``env``.

    Returns ``None`` unchanged so the caller's "use parent env" fallback
    still works. Only OC's own vars are removed — third-party tool keys
    (NPM_TOKEN, AWS_*, etc.) pass through untouched because user scripts
    routinely need them.
    """
    if env is None:
        return None
    out = dict(env)
    for k in list(out.keys()):
        if k in _OC_INFRASTRUCTURE_VARS:
            del out[k]
            continue
        if any(k.startswith(p) for p in _OC_INFRASTRUCTURE_PREFIXES):
            del out[k]
    return out


def _format_bash_output(cmd: str, exit_code: int, out: str, err: str) -> str:
    """Build the Bash tool's result body — the one canonical shape.

    Both the host path and the sandbox path render through this so a
    sandboxed command produces byte-identical output framing to a
    host-run one. Stderr is appended only when non-empty (the host path
    has always behaved this way).
    """
    combined = (
        f"$ {cmd}\n"
        f"exit={exit_code}\n"
        f"--- stdout ---\n{out}"
    )
    if err:
        combined += f"\n--- stderr ---\n{err}"
    return combined


class BashTool(BaseTool):
    parallel_safe = False  # side effects
    # Item 3 (2026-05-02): Bash schema enumerates command/timeout_s; closed.
    strict_mode = True

    #: M2 (T2.6) — class-level "current runtime", set by ``AgentLoop``
    #: before each tool-dispatch batch (same pattern as
    #: ``DelegateTool._current_runtime`` / ``CronTool._current_runtime``).
    #: ``execute`` reads ``custom['sandbox_backend_strategy']`` off it to
    #: decide whether to route the command through a sandbox backend.
    #: Defaults to the shared no-flags context — with that default the
    #: key is absent and the host path runs, so a Bash tool used outside
    #: an ``AgentLoop`` (direct ``BashTool().execute(...)``, tests) keeps
    #: the exact pre-M2 host behavior.
    _current_runtime: RuntimeContext = DEFAULT_RUNTIME_CONTEXT

    @classmethod
    def set_runtime(cls, runtime: RuntimeContext) -> None:
        """Set the runtime context. Called by ``AgentLoop`` alongside
        ``DelegateTool.set_runtime`` / ``CronTool.set_runtime``.

        The loop passes its live ``RuntimeContext`` by reference so the
        per-call ``custom['sandbox_backend_strategy']`` write performed
        by ``_resolve_sandbox_backend`` immediately before dispatch is
        visible here.
        """
        cls._current_runtime = runtime

    def _resolved_sandbox_strategy(self) -> object | None:
        """Return the sandbox strategy resolved for this call, or ``None``.

        Reads ``runtime.custom['sandbox_backend_strategy']`` — published
        by ``AgentLoop._resolve_sandbox_backend``. ``None`` (the default,
        and the value whenever no ``sandbox.backend`` is configured)
        means "run on the host", which is the byte-identical no-op path.
        Read defensively so a missing / malformed runtime can never break
        command execution.
        """
        runtime = self._current_runtime
        custom = getattr(runtime, "custom", None)
        if not isinstance(custom, dict):
            return None
        return custom.get(_SANDBOX_STRATEGY_KEY)

    def _resolved_container_key(self) -> str | None:
        """Return the pooled-container key for this call, or ``None``.

        Reads ``runtime.custom['sandbox_container_key']`` — published by
        ``AgentLoop._resolve_sandbox_backend`` for a reuse-scoped call
        (session / agent / shared scope with the keying id present).
        ``None`` (the default, and the value for tool / none scope) means
        "transient container per call". Read defensively so a missing /
        malformed runtime can never break command execution.
        """
        runtime = self._current_runtime
        custom = getattr(runtime, "custom", None)
        if not isinstance(custom, dict):
            return None
        key = custom.get(_SANDBOX_CONTAINER_KEY)
        return key if isinstance(key, str) and key else None

    def _current_session_id(self) -> str:
        """Return the active session id, or ``""`` when none is published.

        The agent loop publishes the session id on
        ``runtime.custom['session_id']`` (see ``AgentLoop`` —
        ``_new_custom`` carries ``session_id``). The sandbox cost guard
        keys per-session spend on it. Read defensively — a missing /
        malformed runtime yields ``""``, which the cost guard treats as
        "no session to bill" (it records nothing).
        """
        runtime = self._current_runtime
        custom = getattr(runtime, "custom", None)
        if not isinstance(custom, dict):
            return ""
        sid = custom.get("session_id")
        return sid if isinstance(sid, str) else ""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="Bash",
            description=(
                "Execute a bash command in /bin/bash with a configurable timeout, "
                "returning stdout+stderr+exit code. Use for git, package managers (npm/"
                "pip/uv/cargo), build/test runners, and any shell pipeline. CAUTION: "
                "this can mutate the filesystem and run arbitrary code — review the "
                "command before invoking. Prefer Read/Edit/Write/Grep/Glob over Bash "
                "'cat'/'sed'/'echo'/'grep'/'find' since the dedicated tools give you "
                "line-numbered output and structured errors. Avoid long sleeps, "
                "interactive prompts, and `git push --force` unless explicitly asked. "
                "For long-running processes, use StartProcess instead so the call "
                "doesn't block the agent loop."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Max execution time in seconds (default 60, max 600).",
                        "minimum": 1,
                        "maximum": 600,
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, call: ToolCall) -> ToolResult:
        args = call.arguments
        cmd = args.get("command", "")
        timeout = min(int(args.get("timeout_s", 60)), 600)
        if not cmd.strip():
            return ToolResult(
                tool_call_id=call.id, content="Error: empty command", is_error=True
            )
        # Hardline blocklist — non-bypassable. Fires before profile
        # scoping and any consent gate so a tripped hardline never
        # produces a user-visible approval prompt. See
        # opencomputer/security/hardline.py for the pattern list.
        from opencomputer.security.hardline import (
            check_command as _check_hardline,
        )

        _hardline_hit = _check_hardline(cmd)
        if _hardline_hit is not None:
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    f"Refused: {_hardline_hit.reason} "
                    f"(hardline pattern '{_hardline_hit.pattern_id}'). "
                    f"This pattern is non-bypassable."
                ),
                is_error=True,
            )

        # OpenClaw-parity per-command pattern rules. Operators declare
        # allow/ask/deny verdicts in
        # ``security.approvals.command_rules``. ``deny`` short-circuits
        # before Tirith — denials are deterministic and don't depend
        # on a binary being installed. ``allow`` is consulted later
        # by the consent gate; here we just record the verdict.
        try:
            from opencomputer.security.approvals import (
                load_approvals_from_active_config as _load_appr,
            )

            _appr_cfg = _load_appr()
            _verdict = _appr_cfg.evaluate_command(cmd)
        except Exception:  # noqa: BLE001 — never let approvals break exec
            _verdict = None
        if _verdict == "deny":
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Refused: command matched a deny rule in "
                    "security.approvals.command_rules. Edit "
                    "config.yaml or remove the matching pattern to "
                    "permit this command."
                ),
                is_error=True,
            )

        # Hermes parity: Tirith pre-exec scan. Subprocess call is
        # synchronous — wrapped in to_thread so the agent loop's async
        # dispatch isn't blocked. fail_open default per Tirith config;
        # binary absent → action='allow' under fail_open and is a no-op.
        try:
            tirith_result: TirithResult = await asyncio.to_thread(
                tirith_check_command, cmd,
            )
        except Exception:  # noqa: BLE001 — never let scan break exec
            tirith_result = TirithResult(action="allow")

        if tirith_result.action == "block":
            findings_text = (
                format_findings_for_user(tirith_result)
                or tirith_result.summary
                or "blocked by Tirith"
            )
            return ToolResult(
                tool_call_id=call.id,
                content=(
                    "Refused: Tirith pre-exec scan flagged this command.\n"
                    f"{findings_text}"
                ),
                is_error=True,
            )

        # warn: don't refuse, but surface findings as a prefix to the
        # tool result so the model + user see them. allow: silent.
        warn_prefix = ""
        if tirith_result.action == "warn":
            findings_text = format_findings_for_user(tirith_result)
            if findings_text:
                warn_prefix = (
                    "[Tirith warning — command allowed but flagged]\n"
                    f"{findings_text}\n---\n"
                )

        # M2 (T2.6): if the agent loop's resolver picked a sandbox
        # backend for this call, run the command inside it instead of on
        # the host. ``_resolved_sandbox_strategy`` returns ``None``
        # whenever no ``sandbox.backend`` is configured (the default), in
        # which case control falls through to the unchanged host path
        # below — byte-identical to pre-M2 behavior.
        #
        # ``_execute_in_sandbox`` returns a ``ToolResult`` on success (or
        # on a loud ``sandbox.fallback=error`` failure), or ``None`` when
        # the backend was unreachable AND ``sandbox.fallback=local`` — in
        # which case execution falls through to the host path below,
        # exactly as the resolver's fallback policy prescribes.
        strategy = self._resolved_sandbox_strategy()
        if strategy is not None:
            sandboxed = await self._execute_in_sandbox(
                call=call,
                cmd=cmd,
                timeout=timeout,
                strategy=strategy,
                warn_prefix=warn_prefix,
            )
            if sandboxed is not None:
                return sandboxed
            # Backend unreachable + sandbox.fallback=local — fall through
            # to the host path, but SURFACE the lost containment on the
            # result. ``_execute_in_sandbox`` already logged a WARNING;
            # the resolver contract is "never silently downgrade", so the
            # model + user must see it on the result too (a log line is
            # invisible at the surface that matters).
            warn_prefix += (
                "[sandbox unavailable — ran on the HOST without "
                "containment; set sandbox.fallback=error to refuse "
                "instead]\n"
            )

        # Scope HOME / XDG_* to the active profile's home/ subdir so
        # spawned subprocesses (git, ssh, npm, etc.) get per-profile
        # tool-config isolation for credentials and caches. The parent
        # process keeps its real HOME — see _apply_profile_override
        # in cli.py for the architectural rationale.
        try:
            from opencomputer.profiles import (
                read_active_profile,
                scope_subprocess_env,
            )

            scoped = scope_subprocess_env(
                os.environ.copy(), profile=read_active_profile()
            )
            env = _strip_infra_env_vars(scoped)
        except Exception:
            # If profile scoping fails for any reason, fall back to a
            # bare strip of the parent env so a tool-internal token can
            # never leak into the subprocess.
            env = _strip_infra_env_vars(os.environ.copy())

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            exit_code = proc.returncode or 0
        except TimeoutError:
            # PR-A Feature 1: terminate the proc so partial output can
            # be captured; the call site treats timeout same as cancel.
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except (TimeoutError, ProcessLookupError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: command timed out after {timeout}s",
                is_error=True,
            )
        except asyncio.CancelledError:
            # PR-A Feature 1 — Steer Replan-with-Context. The agent
            # loop's cancel-aware dispatch fires CancelledError on the
            # _run_one task; we terminate the subprocess and pre-build
            # a result with whatever stdout was captured before re-
            # raising so the loop's _make_cancelled_result helper can
            # surface partial output to the model on replan.
            partial_stdout = ""
            if proc is not None:
                try:
                    if proc.returncode is None:
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=1.0)
                        except (TimeoutError, ProcessLookupError):
                            try:
                                proc.kill()
                            except ProcessLookupError:
                                pass
                    # Drain whatever already buffered. communicate() may
                    # have queued data on the pipe even though the await
                    # was cancelled.
                    try:
                        if proc.stdout is not None:
                            buf = await asyncio.wait_for(
                                proc.stdout.read(), timeout=0.5,
                            )
                            partial_stdout = buf.decode("utf-8", errors="replace")
                    except (TimeoutError, Exception):  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass
            # Stash the partial output on the cancelled task so the
            # dispatcher's _make_cancelled_result can pick it up.
            current_task = asyncio.current_task()
            if current_task is not None:
                # Attach as an attribute on the task object — read by
                # _make_cancelled_result via getattr fallback.
                try:
                    object.__setattr__(
                        current_task, "_pr_a_partial_stdout", partial_stdout,
                    )
                except (AttributeError, TypeError):
                    pass
            raise
        except Exception as e:
            return ToolResult(
                tool_call_id=call.id,
                content=f"Error: {type(e).__name__}: {e}",
                is_error=True,
            )

        out = stdout.decode("utf-8", errors="replace") if stdout else ""
        err = stderr.decode("utf-8", errors="replace") if stderr else ""
        combined = _format_bash_output(cmd, exit_code, out, err)
        return ToolResult(
            tool_call_id=call.id,
            content=warn_prefix + combined,
            is_error=exit_code != 0,
        )

    async def _execute_in_sandbox(
        self,
        *,
        call: ToolCall,
        cmd: str,
        timeout: int,
        strategy: object,
        warn_prefix: str,
    ) -> ToolResult | None:
        """Run ``cmd`` inside the resolved sandbox backend ``strategy``.

        ``strategy`` is the :class:`~plugin_sdk.SandboxStrategy` the agent
        loop's resolver chose for this call. The shell command is wrapped
        as ``["/bin/sh", "-c", cmd]`` — the same ``sh -c`` shape
        ``asyncio.create_subprocess_shell`` uses on the host path — so the
        sandboxed command sees equivalent shell semantics.

        Returns a :class:`~plugin_sdk.core.ToolResult` mapped from the
        backend's :class:`~plugin_sdk.SandboxResult` in the exact framing
        :func:`_format_bash_output` produces for the host path.

        Returns ``None`` in exactly one case — the backend turned out to
        be unreachable at run time (``SandboxUnavailable`` / a transport
        error from, e.g., E2B ``create()``) AND ``sandbox.fallback`` is
        ``local``: the caller then falls through to the host path. Under
        the default ``sandbox.fallback=error`` an unreachable backend
        yields an error ``ToolResult`` (fail loud — OC never silently
        downgrades containment).

        T2.8 — sandbox cost guard. A paid backend (``e2b``) bills per
        running second. Before the run, if the active session is already
        over its sandbox spend cap, this returns a loud error
        ``ToolResult`` *without* running the command (a sandbox would
        only run up more cost). After a run completes,
        ``duration × rate(backend)`` is recorded against the session's
        sandbox spend. Local backends cost ``$0`` and the cap is never
        hit by them. Cost-guard failures never break execution — they are
        swallowed to a WARNING (counter-telemetry pattern).
        """
        from opencomputer.sandbox.resolver import (
            SANDBOX_FALLBACK_LOCAL,
            fallback_policy,
        )

        backend_name = getattr(strategy, "name", None) or type(strategy).__name__

        # T2.8 — refuse before the run if this session is already over its
        # sandbox spend cap. A sandboxed run that overran the cap would
        # only add more cost; fail loud instead. ``_check_sandbox_cap``
        # returns an error ``ToolResult`` when over cap, else ``None``.
        over_cap = self._check_sandbox_cap(call=call, backend_name=backend_name)
        if over_cap is not None:
            return over_cap
        # ``sh -c`` mirrors the host path's ``create_subprocess_shell``,
        # which spawns ``/bin/sh -c <cmd>`` on POSIX. ``sh`` (not ``bash``)
        # is what is universally present inside minimal sandbox images.
        argv = ["/bin/sh", "-c", cmd]
        # Default-deny network posture (SandboxConfig's own default). The
        # wall-clock cap is the tool's resolved ``timeout``. ``run`` is a
        # required ABC method; strategies decode/limit env themselves.
        sandbox_cfg = SandboxConfig(
            cpu_seconds_limit=timeout,
            network_allowed=False,
            # M3 (T3.3): a reuse-scoped call carries a pooled-container
            # key; ``None`` (tool / none scope) → transient container.
            container_key=self._resolved_container_key(),
        )

        run = getattr(strategy, "run", None)
        if not callable(run):
            # Defensive: a malformed object on the runtime key is not a
            # usable backend. Treat exactly like an unreachable backend.
            _log.warning(
                "sandbox: resolved backend %r has no callable run(); "
                "treating as unreachable",
                backend_name,
            )
            return self._handle_sandbox_unreachable(
                call=call,
                backend_name=backend_name,
                reason="resolved backend object is not a usable strategy",
                fallback=fallback_policy(self._active_config()),
                fallback_local=SANDBOX_FALLBACK_LOCAL,
            )

        try:
            result: SandboxResult = await run(
                argv, config=sandbox_cfg, stdin=None, cwd=None
            )
        except SandboxUnavailable as exc:
            # The backend could not even start (missing dependency / key /
            # an unreachable host). This is the run-time half of the
            # fallback policy the resolver's docstring describes.
            return self._handle_sandbox_unreachable(
                call=call,
                backend_name=backend_name,
                reason=str(exc),
                fallback=fallback_policy(self._active_config()),
                fallback_local=SANDBOX_FALLBACK_LOCAL,
            )
        except asyncio.CancelledError:
            # Cooperative cancellation (the loop's steer/replan path) —
            # propagate untouched, same as the host path does.
            raise
        except Exception as exc:  # noqa: BLE001 — a backend transport error
            # e.g. E2B's ``AsyncSandbox.create()`` raising a network /
            # auth error. Treat as "backend unreachable" and apply the
            # fallback policy rather than crashing dispatch.
            _log.warning(
                "sandbox: backend %r failed to run the command: %s",
                backend_name,
                exc,
            )
            return self._handle_sandbox_unreachable(
                call=call,
                backend_name=backend_name,
                reason=f"{type(exc).__name__}: {exc}",
                fallback=fallback_policy(self._active_config()),
                fallback_local=SANDBOX_FALLBACK_LOCAL,
            )

        # Map the SandboxResult onto the Bash tool's ToolResult, using the
        # SAME framing the host path produces.
        exit_code = int(getattr(result, "exit_code", 0) or 0)
        out = str(getattr(result, "stdout", "") or "")
        err = str(getattr(result, "stderr", "") or "")
        duration = float(getattr(result, "duration_seconds", 0.0) or 0.0)
        _log.debug(
            "sandbox: ran Bash command via backend %r (exit=%d, %.2fs)",
            backend_name,
            exit_code,
            duration,
        )
        # T2.8 — record this run's spend against the session's sandbox
        # cost guard (``duration × rate(backend)``). ``$0`` for a free
        # local backend. A recording failure must never break the tool
        # result — swallow to a WARNING (counter-telemetry pattern).
        self._record_sandbox_spend(
            backend_name=backend_name, duration_seconds=duration
        )
        combined = _format_bash_output(cmd, exit_code, out, err)
        return ToolResult(
            tool_call_id=call.id,
            content=warn_prefix + combined,
            is_error=exit_code != 0,
        )

    def _handle_sandbox_unreachable(
        self,
        *,
        call: ToolCall,
        backend_name: str,
        reason: str,
        fallback: str,
        fallback_local: str,
    ) -> ToolResult | None:
        """Apply ``sandbox.fallback`` when the backend is unreachable.

        * ``local`` → log a WARNING and return ``None`` so the caller
          falls through to the host path (the operator explicitly opted
          into host fallback).
        * ``error`` (default) → return a loud error ``ToolResult``; OC
          never silently downgrades containment.
        """
        if fallback == fallback_local:
            _log.warning(
                "sandbox: backend %r is unreachable (%s); "
                "sandbox.fallback=local — running the command on the "
                "HOST without containment",
                backend_name,
                reason,
            )
            return None
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Error: sandbox backend {backend_name!r} is unavailable "
                f"({reason}), and sandbox.fallback='error' (the default). "
                "Configure a reachable sandbox.backend, or set "
                "sandbox.fallback=local to permit running on the host."
            ),
            is_error=True,
        )

    def _check_sandbox_cap(
        self, *, call: ToolCall, backend_name: str
    ) -> ToolResult | None:
        """T2.8 — refuse a sandboxed run when the session is over its cap.

        Returns an error :class:`~plugin_sdk.core.ToolResult` when the
        active session has already spent its sandbox budget, so the caller
        does not run the command. Returns ``None`` to permit the run
        (under cap, no session id to bill, or — defensively — any
        cost-guard read failure: a guard hiccup must never *block* a
        legitimate command).

        A sandbox's cost is unknown until it has run, so the pre-run gate
        checks only what is *already* spent (``projected_cost_usd=0``):
        it stops a session that has already crossed the line from running
        up still more cost, which is the cap's intent.
        """
        session_id = self._current_session_id()
        if not session_id:
            # No session to bill — the cap is a per-session ceiling.
            return None
        try:
            from opencomputer.cost_guard import get_default_sandbox_cost_guard

            guard = get_default_sandbox_cost_guard()
            # A free local backend has rate $0 — it can never push a
            # session over cap, so skip the check entirely for it.
            if guard.rate_for(backend_name) <= 0:
                return None
            decision = guard.check_session_budget(session_id)
        except Exception as exc:  # noqa: BLE001 — a guard hiccup must not block exec
            _log.warning(
                "sandbox cost guard: pre-run cap check failed (%s); "
                "permitting the run",
                exc,
            )
            return None
        if decision.allowed:
            return None
        return ToolResult(
            tool_call_id=call.id,
            content=(
                f"Refused: {decision.reason}. The {backend_name!r} sandbox "
                f"backend bills per running second and this session has "
                f"spent ${decision.session_spend_usd:.4f} of its "
                f"${decision.session_cap_usd:.2f} sandbox cap. Raise the "
                "cap (the `sandbox.session_cap_usd` value in the profile's "
                "cost_guard.json) or start a new session."
            ),
            is_error=True,
        )

    def _record_sandbox_spend(
        self, *, backend_name: str, duration_seconds: float
    ) -> None:
        """T2.8 — record one sandboxed run's spend against the session.

        Costs ``duration × rate(backend)``; ``$0`` for a free local
        backend. A recording failure (corrupt cost file, disk error)
        must never break tool execution — it is swallowed to a WARNING,
        the same counter-telemetry pattern the rest of the codebase uses
        for per-session counters.
        """
        session_id = self._current_session_id()
        if not session_id:
            return
        try:
            from opencomputer.cost_guard import get_default_sandbox_cost_guard

            cost = get_default_sandbox_cost_guard().record_run(
                session_id,
                backend=backend_name,
                duration_seconds=duration_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — cost telemetry must never break exec
            _log.warning(
                "sandbox cost guard: failed to record run spend for "
                "backend %r (%s)",
                backend_name,
                exc,
            )
            return
        if cost > 0:
            _log.debug(
                "sandbox cost guard: recorded $%.6f for a %.2fs %r run "
                "(session %s)",
                cost,
                duration_seconds,
                backend_name,
                session_id,
            )

    @staticmethod
    def _active_config() -> object | None:
        """Load the active :class:`~opencomputer.agent.config.Config`.

        Used only on the sandbox failure path to read ``sandbox.fallback``.
        Returns ``None`` on any failure — :func:`fallback_policy` treats a
        ``None`` config as the safe ``error`` default, so a config-load
        hiccup can never silently downgrade containment.
        """
        try:
            from opencomputer.agent.config_store import load_config

            return load_config()
        except Exception:  # noqa: BLE001 — config absence must fail safe
            return None
