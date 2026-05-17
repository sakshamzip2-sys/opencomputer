// OpenComputer TUI — main Ink application (TypeScript source).
//
// Adapted for OpenComputer from hermes-agent/ui-tui.
// Original: MIT License (c) 2025 Nous Research — see THIRD_PARTY_LICENSE_HERMES.
//
// TUI-parity Milestone 2. The OC-native terminal UI: a markdown-rendered
// streaming conversation with tool results, retry banners and live
// permission prompts; a multiline composer with input history; scrollable
// history; a slash-command palette; a session picker and six overlays.

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";

import { useEditor } from "./editor.js";
import { Markdown } from "./markdown.js";
import {
  AgentsOverlay,
  ModelPickerOverlay,
  PermissionPrompt,
  RollbackOverlay,
  SettingsOverlay,
  SkillsHubOverlay,
  ToolsOverlay,
  type ConfigRow,
  type ModelRow,
  type SkillRow,
} from "./overlays.js";
import type {
  CheckpointInfo,
  PermissionRequestPayload,
  SessionRow,
  SlashCommandInfo,
  StreamRetryPayload,
  SubagentInfo,
  ToolInfo,
  ToolResultPayload,
  WireServerEvent,
} from "./protocol.js";
import { EVENT } from "./protocol.js";
import { theme } from "./theme.js";
import { Spinner } from "./widgets.js";
import { OCWireClient } from "./wireClient.js";

// ─── model ──────────────────────────────────────────────────────────

type Role = "user" | "assistant" | "system" | "tool";
interface Turn {
  role: Role;
  text: string;
}
type Overlay =
  | "none"
  | "sessions"
  | "model"
  | "skills"
  | "settings"
  | "agents"
  | "rollback"
  | "tools";

/** How many turns are visible at once; older ones scroll via PageUp. */
const WINDOW = 25;
/** Tool output longer than this is clipped in the transcript. */
const TOOL_OUTPUT_CAP = 400;

const OVERLAY_COMMANDS: Record<string, Overlay> = {
  model: "model",
  models: "model",
  skills: "skills",
  settings: "settings",
  config: "settings",
  agents: "agents",
  subagents: "agents",
  rollback: "rollback",
  checkpoints: "rollback",
  tools: "tools",
};

const CLIENT_SLASH: SlashCommandInfo[] = [
  { name: "model", description: "open the model picker" },
  { name: "skills", description: "open the skills hub" },
  { name: "settings", description: "open the settings panel" },
  { name: "agents", description: "open the subagents overlay" },
  { name: "rollback", description: "open the checkpoint overlay" },
  { name: "tools", description: "open the tools inspector" },
  { name: "set", description: "/set <key> <value> — set a config value" },
  { name: "rename", description: "/rename <title> — rename this session" },
  { name: "fork", description: "/fork — branch this session" },
  { name: "steer", description: "/steer <text> — nudge a running turn" },
];

export interface AppProps {
  client: OCWireClient;
  resumeSpec?: string;
}

// ─── component ──────────────────────────────────────────────────────

export function App({ client, resumeSpec = "" }: AppProps): React.ReactElement {
  const { exit } = useApp();
  const ed = useEditor();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(client.connected);
  const [streamBuf, setStreamBuf] = useState("");
  const [helloInfo, setHelloInfo] = useState("");
  const [slashList, setSlashList] = useState<SlashCommandInfo[]>(CLIENT_SLASH);
  const [overlay, setOverlay] = useState<Overlay>("none");
  const [oi, setOi] = useState(0);
  const [usage, setUsage] = useState("");
  // Functional-defect fixes (M2 batch 7):
  const [retry, setRetry] = useState(""); // stream.retry banner ("" = none)
  const [permReq, setPermReq] = useState<PermissionRequestPayload | null>(null);
  const [scrollOffset, setScrollOffset] = useState(0); // 0 = pinned to bottom
  const [history, setHistory] = useState<string[]>([]); // submitted inputs
  const [histIdx, setHistIdx] = useState(-1); // -1 = not recalling
  const [queue, setQueue] = useState<string[]>([]); // messages typed while busy

  // Per-overlay data.
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [models, setModels] = useState<ModelRow[]>([]);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [skillPreview, setSkillPreview] = useState("");
  const [settings, setSettings] = useState<ConfigRow[]>([]);
  const [agents, setAgents] = useState<SubagentInfo[]>([]);
  const [checkpoints, setCheckpoints] = useState<CheckpointInfo[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);

  const sessionId = useRef<string | undefined>(undefined);

  // A new turn snaps the viewport back to the bottom.
  const push = useCallback((t: Turn) => {
    setTurns((prev) => [...prev, t]);
    setScrollOffset(0);
  }, []);
  const sys = useCallback(
    (text: string) => push({ role: "system", text }),
    [push],
  );

  const showPalette = overlay === "none" && /^\/\S*$/.test(ed.text);

  // ── connection + event lifecycle ──────────────────────────────────
  useEffect(() => {
    const offConn = client.onConnected(async (ok) => {
      setConnected(ok);
      if (!ok) {
        sys("disconnected — reconnecting…");
        return;
      }
      try {
        const h = await client.hello(sessionId.current);
        setHelloInfo(`${h.server} (${h.methods.length} methods)`);
        sys(`connected to ${client.serverUrl} — ${h.server}`);
        try {
          const s = await client.slashList();
          const seen = new Set<string>();
          const merged = [...CLIENT_SLASH, ...s.commands].filter((c) => {
            if (seen.has(c.name)) return false;
            seen.add(c.name);
            return true;
          });
          setSlashList(merged);
        } catch {
          /* older wire-server without slash.list — keep client commands */
        }
        await applyResume();
      } catch (e) {
        sys(`hello failed: ${(e as Error).message}`);
      }
    });

    const offEv = client.onEvent((ev: WireServerEvent) => {
      const payload = (ev.payload ?? {}) as Record<string, unknown>;
      if (ev.event === EVENT.ASSISTANT_MESSAGE || ev.event === "turn.assistant") {
        const delta = String(payload.delta ?? payload.text ?? payload.content ?? "");
        setStreamBuf((prev) => prev + delta);
      } else if (ev.event === EVENT.TURN_END) {
        setStreamBuf((prev) => {
          if (prev) push({ role: "assistant", text: prev });
          return "";
        });
        setBusy(false);
        setRetry(""); // a finished turn clears any stale retry banner
        void refreshUsage();
      } else if (ev.event === EVENT.TOOL_CALL) {
        push({ role: "tool", text: `⚙ ${String(payload.name ?? "tool")}` });
      } else if (ev.event === EVENT.TOOL_RESULT) {
        // Functional fix #1 — tool output was previously invisible.
        const p = payload as ToolResultPayload;
        let body = String(p.content ?? "");
        if (body.length > TOOL_OUTPUT_CAP) {
          body = `${body.slice(0, TOOL_OUTPUT_CAP)}… (${body.length} chars)`;
        }
        push({
          role: "tool",
          text: `${p.is_error ? "✗" : "→"} ${body || "(no output)"}`,
        });
      } else if (ev.event === EVENT.STREAM_RETRY) {
        // Functional fix #3 — retries were a silent frozen spinner.
        const p = payload as StreamRetryPayload;
        if (p.exhausted) {
          setRetry("");
        } else {
          setRetry(
            `⟳ ${p.error_kind ?? "transient error"} — retry ` +
              `${p.next_attempt ?? "?"}/${p.max_attempts ?? "?"} ` +
              `in ${p.delay_seconds ?? 0}s`,
          );
        }
      } else if (ev.event === EVENT.PERMISSION_REQUEST) {
        // Functional fix #2 — the turn used to hang with no prompt.
        setPermReq(payload as unknown as PermissionRequestPayload);
      } else if (ev.event === EVENT.MEMORY_WRITE) {
        sys(
          `memory: ${String(payload.action ?? "write")} → ` +
            `${String(payload.target ?? "")}`,
        );
      } else if (ev.event === EVENT.EVOLUTION_TUNING_CHANGED) {
        sys("evolution: self-tuning thresholds updated");
      } else if (ev.event === EVENT.PROFILE_SWAP) {
        sys(
          `profile: ${String(payload.from_profile ?? "?")} → ` +
            `${String(payload.to_profile ?? "?")}`,
        );
      } else if (ev.event === EVENT.ERROR) {
        push({ role: "system", text: `error: ${String(payload.error ?? "")}` });
        setBusy(false);
        setRetry("");
      }
    });

    return () => {
      offConn();
      offEv();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, resumeSpec]);

  // ── queued-message drain ──────────────────────────────────────────
  // Messages typed while a turn was running are queued; when the turn
  // ends (busy → false) the next one is dispatched, in order.
  useEffect(() => {
    if (busy || queue.length === 0 || !connected) return;
    const next = queue[0];
    setQueue((q) => q.slice(1));
    if (next) void sendText(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [busy, queue, connected]);

  // ── resume plumbing ────────────────────────────────────────────────
  async function applyResume(): Promise<void> {
    if (!resumeSpec || sessionId.current) return;
    try {
      if (resumeSpec === "last") {
        const r = await client.sessionMostRecent();
        if (r.found && r.session_id) {
          sessionId.current = r.session_id;
          sys(`resumed latest session: ${r.session_id.slice(0, 12)}…`);
          void refreshUsage();
        } else {
          sys("OC_TUI_RESUME=last but no sessions found — starting fresh");
        }
      } else {
        const r = await client.sessionResume(resumeSpec);
        sessionId.current = r.session_id;
        for (const m of r.messages) {
          push({ role: roleOf(m.role), text: m.text ?? "" });
        }
        sys(`resumed session ${r.session_id.slice(0, 12)}… (${r.message_count} msgs)`);
        void refreshUsage();
      }
    } catch (e) {
      sys(`resume failed: ${(e as Error).message}`);
    }
  }

  async function refreshUsage(): Promise<void> {
    const sid = sessionId.current;
    if (!sid) return;
    try {
      const u = await client.sessionUsage(sid);
      if (u.found) {
        const cost = u.cost_usd != null ? ` $${u.cost_usd.toFixed(4)}` : "";
        setUsage(`↑${u.input_tokens ?? 0} ↓${u.output_tokens ?? 0}${cost}`);
      }
    } catch {
      /* usage is best-effort */
    }
  }

  // ── permission resolution (functional fix #2) ─────────────────────
  async function resolvePermission(
    decision: "allow_once" | "allow_always" | "deny",
  ): Promise<void> {
    const req = permReq;
    setPermReq(null);
    if (!req) return;
    try {
      await client.permissionResponse(
        req.request_id,
        req.session_id,
        req.capability_id,
        decision,
      );
      sys(`permission ${decision.replace("_", " ")} — ${req.capability_id}`);
    } catch (e) {
      sys(`permission response failed: ${(e as Error).message}`);
    }
  }

  // ── input history (functional fix #5) ─────────────────────────────
  function recallPrev(): void {
    if (history.length === 0) return;
    const i = histIdx < 0 ? history.length - 1 : Math.max(0, histIdx - 1);
    setHistIdx(i);
    ed.setText(history[i] ?? "");
  }
  function recallNext(): void {
    if (histIdx < 0) return;
    const i = histIdx + 1;
    if (i >= history.length) {
      setHistIdx(-1);
      ed.clear();
    } else {
      setHistIdx(i);
      ed.setText(history[i] ?? "");
    }
  }

  // ── overlay openers ────────────────────────────────────────────────
  const openOverlay = useCallback(
    async (kind: Overlay): Promise<void> => {
      setOi(0);
      try {
        if (kind === "sessions") {
          setSessions((await client.sessionsList(30)).sessions);
        } else if (kind === "model") {
          const r = await client.modelOptions();
          const rows: ModelRow[] = [];
          for (const p of r.providers) {
            for (const m of p.models) {
              rows.push({
                provider: p.name,
                model: m,
                isCurrent: p.name === r.provider && m === r.model,
              });
            }
          }
          setModels(rows);
        } else if (kind === "skills") {
          const r = await client.skillsList();
          const raw = (r as { skills?: unknown[] }).skills ?? [];
          setSkills(
            raw.map((s) => {
              const o = s as Record<string, unknown>;
              return {
                id: String(o.id ?? o.name ?? ""),
                name: String(o.name ?? o.id ?? ""),
                description: String(o.description ?? ""),
              };
            }),
          );
          setSkillPreview("");
        } else if (kind === "settings") {
          const rows: ConfigRow[] = [];
          for (const key of ["model.provider", "model.model"]) {
            const r = await client.configGet(key);
            if (r.found) rows.push({ key, value: String(r.value) });
          }
          setSettings(rows);
        } else if (kind === "agents") {
          setAgents((await client.subagentsList(50)).subagents);
        } else if (kind === "rollback") {
          const sid = sessionId.current;
          setCheckpoints(
            sid ? (await client.checkpointsList(sid)).checkpoints : [],
          );
        } else if (kind === "tools") {
          setTools((await client.toolsList()).tools);
        }
        setOverlay(kind);
      } catch (e) {
        sys(`${kind} overlay failed: ${(e as Error).message}`);
        setOverlay("none");
      }
    },
    [client, sys],
  );

  // ── overlay action helpers ─────────────────────────────────────────
  function overlayLen(): number {
    switch (overlay) {
      case "sessions":
        return sessions.length;
      case "model":
        return models.length;
      case "skills":
        return skills.length;
      case "settings":
        return settings.length;
      case "agents":
        return agents.length;
      case "rollback":
        return checkpoints.length;
      case "tools":
        return tools.length;
      default:
        return 0;
    }
  }

  async function resumeSession(row: SessionRow): Promise<void> {
    setOverlay("none");
    try {
      const r = await client.sessionResume(row.id);
      sessionId.current = r.session_id;
      setTurns([]);
      for (const m of r.messages) {
        push({ role: roleOf(m.role), text: m.text ?? "" });
      }
      sys(`resumed ${r.session_id.slice(0, 12)}… (${r.message_count} msgs)`);
      void refreshUsage();
    } catch (e) {
      sys(`resume failed: ${(e as Error).message}`);
    }
  }

  async function deleteSession(row: SessionRow): Promise<void> {
    try {
      const r = await client.sessionDelete(row.id);
      if (r.found) {
        setSessions((prev) => prev.filter((s) => s.id !== row.id));
        setOi((i) => Math.max(0, i - 1));
      }
    } catch (e) {
      sys(`delete failed: ${(e as Error).message}`);
    }
  }

  async function selectModel(row: ModelRow): Promise<void> {
    setOverlay("none");
    try {
      const r = await client.modelSet(row.provider, row.model);
      sys(
        r.ok
          ? `model set: ${row.provider} / ${row.model} (restart to apply)`
          : "model.set: no change",
      );
    } catch (e) {
      sys(`model.set failed: ${(e as Error).message}`);
    }
  }

  async function previewSkill(row: SkillRow): Promise<void> {
    try {
      const r = await client.skillShow(row.id);
      setSkillPreview(r.found ? r.body : "(skill body not found)");
    } catch (e) {
      sys(`skill.show failed: ${(e as Error).message}`);
    }
  }

  async function deleteCheckpoint(cp: CheckpointInfo): Promise<void> {
    try {
      const r = await client.checkpointsDelete(cp.id);
      if (r.found) {
        setCheckpoints((prev) => prev.filter((c) => c.id !== cp.id));
        setOi((i) => Math.max(0, i - 1));
      }
    } catch (e) {
      sys(`checkpoint delete failed: ${(e as Error).message}`);
    }
  }

  // ── keyboard ───────────────────────────────────────────────────────
  useInput((raw, key) => {
    // A pending permission request preempts ALL other input — the agent
    // is blocked until the user decides (functional fix #2).
    if (permReq) {
      if (raw === "a") void resolvePermission("allow_once");
      else if (raw === "A") void resolvePermission("allow_always");
      else if (raw === "d") void resolvePermission("deny");
      return;
    }

    // Scrollback works in every state (functional fix #4).
    if (key.pageUp) {
      const maxOff = Math.max(0, turns.length - WINDOW);
      setScrollOffset((o) => Math.min(maxOff, o + 10));
      return;
    }
    if (key.pageDown) {
      setScrollOffset((o) => Math.max(0, o - 10));
      return;
    }

    // Overlay-open state.
    if (overlay !== "none") {
      if (key.escape) {
        setOverlay("none");
      } else if (key.upArrow) {
        setOi((i) => Math.max(0, i - 1));
      } else if (key.downArrow) {
        setOi((i) => Math.min(overlayLen() - 1, i + 1));
      } else if (key.return) {
        if (overlay === "sessions" && sessions[oi]) void resumeSession(sessions[oi]);
        else if (overlay === "model" && models[oi]) void selectModel(models[oi]);
        else if (overlay === "skills" && skills[oi]) void previewSkill(skills[oi]);
      } else if (key.delete || key.backspace) {
        if (overlay === "sessions" && sessions[oi]) void deleteSession(sessions[oi]);
        else if (overlay === "rollback" && checkpoints[oi])
          void deleteCheckpoint(checkpoints[oi]);
      }
      return;
    }

    // No overlay open.
    if (key.escape) {
      if (busy) {
        const sid = sessionId.current;
        if (sid) {
          void client.sessionInterrupt(sid).catch(() => {});
          sys("interrupt sent");
        }
        return;
      }
      if (ed.text) {
        ed.clear();
        return;
      }
      exit();
      return;
    }
    if (key.ctrl && raw === "c") {
      exit();
      return;
    }
    if (key.ctrl && raw === "r") {
      void openOverlay("sessions");
      return;
    }
    // Tab completes the current slash command from the palette.
    if (key.tab && showPalette) {
      const f = ed.text.slice(1);
      const match = slashList.find((c) => c.name.startsWith(f));
      if (match) ed.setText(`/${match.name} `);
      return;
    }
    if (key.return) {
      const m = ed.text.trim();
      if (busy) {
        // A turn is running — queue the message; the drain effect sends
        // it when the turn ends. Input is no longer dropped while busy.
        if (m) {
          setQueue((q) => [...q, m]);
          ed.clear();
          sys(`queued: ${m.length > 40 ? `${m.slice(0, 40)}…` : m}`);
        }
      } else {
        void send();
      }
      return;
    }
    // Typing is allowed while busy — compose the next message ahead.
    const consumed = ed.onKey(raw, key);
    if (consumed) return;
    // Editor declined the key — at the top/bottom line, ↑↓ recall history.
    if (key.upArrow) recallPrev();
    else if (key.downArrow) recallNext();
  });

  // ── submit ─────────────────────────────────────────────────────────
  async function send(): Promise<void> {
    const msg = ed.text.trim();
    if (!msg || busy || !connected) return;
    ed.clear();
    setHistory((h) => (h[h.length - 1] === msg ? h : [...h, msg]));
    setHistIdx(-1);
    void sendText(msg);
  }

  /** Dispatch one already-trimmed message — a slash command or a chat turn.
   *  Shared by the live composer and the queued-message drain. */
  async function sendText(msg: string): Promise<void> {
    if (msg.startsWith("/")) {
      const parts = msg.slice(1).split(/\s+/);
      const name = (parts[0] ?? "").toLowerCase();
      const args = parts.slice(1).join(" ");

      if (name in OVERLAY_COMMANDS) {
        void openOverlay(OVERLAY_COMMANDS[name]!);
        return;
      }
      if (name === "set") {
        const [k, ...rest] = args.split(/\s+/);
        if (!k || rest.length === 0) {
          sys("usage: /set <key> <value>");
          return;
        }
        push({ role: "user", text: msg });
        try {
          const r = await client.configSet(k, rest.join(" "));
          sys(r.ok ? `config set: ${k}` : "config.set: no change");
        } catch (e) {
          sys(`config.set failed: ${(e as Error).message}`);
        }
        return;
      }
      if (name === "rename") {
        const sid = sessionId.current;
        if (!sid) return void sys("/rename: no active session yet");
        if (!args) return void sys("usage: /rename <title>");
        try {
          await client.sessionRename(sid, args);
          sys(`session renamed → ${args}`);
        } catch (e) {
          sys(`rename failed: ${(e as Error).message}`);
        }
        return;
      }
      if (name === "fork") {
        const sid = sessionId.current;
        if (!sid) return void sys("/fork: no active session yet");
        try {
          const r = await client.sessionFork(sid, args);
          sys(`forked → ${r.new_session_id.slice(0, 12)}… (${r.messages_copied} msgs)`);
        } catch (e) {
          sys(`fork failed: ${(e as Error).message}`);
        }
        return;
      }
      if (name === "steer") {
        const sid = sessionId.current;
        if (!sid) return void sys("/steer: no active session yet");
        if (!args) return void sys("usage: /steer <text>");
        try {
          const r = await client.steerSubmit(sid, args);
          sys(`steer queued (${(r as { queued_chars?: number }).queued_chars ?? 0} chars)`);
        } catch (e) {
          sys(`steer failed: ${(e as Error).message}`);
        }
        return;
      }
      push({ role: "user", text: msg });
      try {
        const r = await client.slashDispatch(name, args);
        sys(r.output || "(no output)");
      } catch (e) {
        sys(`slash error: ${(e as Error).message}`);
      }
      return;
    }

    push({ role: "user", text: msg });
    setBusy(true);
    setStreamBuf("");
    try {
      await client.chat(msg, sessionId.current);
    } catch (e) {
      sys(`wire error: ${(e as Error).message}`);
      setBusy(false);
    }
  }

  // ── render ─────────────────────────────────────────────────────────
  const end = turns.length - scrollOffset;
  const start = Math.max(0, end - WINDOW);
  const visibleTurns = turns.slice(start, end);

  return (
    <Box flexDirection="column">
      <Box flexDirection="column" marginBottom={1}>
        <Text color={theme.accent}>OpenComputer TUI</Text>
        <Text color={theme.muted}>
          {helloInfo || "connecting…"}
          {"  "}
          <Text color={connected ? theme.ok : theme.error}>
            {connected ? "● connected" : "● disconnected"}
          </Text>
          {usage ? `   ${usage}` : ""}
          {"   /-commands · Ctrl+N newline · PgUp/PgDn scroll · ESC quit"}
        </Text>
      </Box>

      {permReq && (
        <PermissionPrompt
          capabilityId={permReq.capability_id}
          context={permReq.context ?? ""}
          scope={permReq.scope}
        />
      )}
      {retry && (
        <Box marginBottom={1}>
          <Text color={theme.warn}>{retry}</Text>
        </Box>
      )}

      {showPalette && slashList.length > 0 && (
        <SlashPalette commands={slashList} filter={ed.text.slice(1)} />
      )}
      {overlay === "sessions" && <SessionPicker sessions={sessions} index={oi} />}
      {overlay === "model" && <ModelPickerOverlay rows={models} index={oi} />}
      {overlay === "skills" && (
        <SkillsHubOverlay skills={skills} index={oi} preview={skillPreview} />
      )}
      {overlay === "settings" && <SettingsOverlay entries={settings} index={oi} />}
      {overlay === "agents" && <AgentsOverlay subagents={agents} index={oi} />}
      {overlay === "rollback" && (
        <RollbackOverlay checkpoints={checkpoints} index={oi} />
      )}
      {overlay === "tools" && <ToolsOverlay tools={tools} index={oi} />}

      <Box flexDirection="column" marginBottom={1}>
        {start > 0 && (
          <Text color={theme.muted}>{`  ↑ ${start} earlier — PgUp to scroll`}</Text>
        )}
        {visibleTurns.map((t, i) => (
          <Box
            key={start + i}
            marginBottom={t.role === "system" ? 0 : 1}
            flexDirection="column"
          >
            {t.role === "assistant" ? (
              <Markdown text={t.text} />
            ) : (
              <Text color={colorFor(t.role)}>
                {prefixFor(t.role)}
                {t.text}
              </Text>
            )}
          </Box>
        ))}
        {scrollOffset > 0 && (
          <Text color={theme.muted}>
            {`  ↓ ${scrollOffset} newer — PgDn to scroll`}
          </Text>
        )}
        {streamBuf && scrollOffset === 0 && (
          <Box flexDirection="column">
            <Markdown text={streamBuf} />
            <Text color={theme.muted}>▌</Text>
          </Box>
        )}
      </Box>

      {busy ? (
        <Spinner label="thinking… (ESC interrupts)" />
      ) : (
        <Composer ed={ed} connected={connected} />
      )}
    </Box>
  );
}

// ─── sub-components ─────────────────────────────────────────────────

function Composer({
  ed,
  connected,
}: {
  ed: ReturnType<typeof useEditor>;
  connected: boolean;
}): React.ReactElement {
  if (ed.text === "") {
    return (
      <Box>
        <Text color={theme.accent}>{"> "}</Text>
        <Text color={theme.muted}>
          {connected ? "type a message or /command" : "waiting for wire…"}
        </Text>
        <Text color={theme.muted}>▌</Text>
      </Box>
    );
  }
  return (
    <Box flexDirection="column">
      {ed.lines.map((line, r) => {
        const prefix = r === 0 ? "> " : "  ";
        if (r !== ed.cursorRow) {
          return (
            <Text key={r}>
              <Text color={theme.accent}>{prefix}</Text>
              {line}
            </Text>
          );
        }
        const before = line.slice(0, ed.cursorCol);
        const after = line.slice(ed.cursorCol);
        return (
          <Text key={r}>
            <Text color={theme.accent}>{prefix}</Text>
            {before}
            <Text color={theme.muted}>▌</Text>
            {after}
          </Text>
        );
      })}
    </Box>
  );
}

function SlashPalette({
  commands,
  filter,
}: {
  commands: SlashCommandInfo[];
  filter: string;
}): React.ReactElement {
  const matches = commands.filter((c) => c.name.startsWith(filter)).slice(0, 12);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="gray" marginBottom={1}>
      <Text color="cyan"> Slash commands ({matches.length}):</Text>
      {matches.map((c) => (
        <Text key={c.name}>
          <Text color="cyan">{"  /" + c.name}</Text>
          <Text color="gray">{" — " + c.description}</Text>
        </Text>
      ))}
    </Box>
  );
}

function SessionPicker({
  sessions,
  index,
}: {
  sessions: SessionRow[];
  index: number;
}): React.ReactElement {
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="cyan" marginBottom={1}>
      <Text color="cyan"> Sessions — ↑↓ move · Enter resume · Del delete · ESC close</Text>
      {sessions.length === 0 && <Text color="gray">  (no sessions)</Text>}
      {sessions.slice(0, 15).map((s, i) => (
        <Text key={s.id} color={i === index ? "cyan" : "white"}>
          {(i === index ? "› " : "  ") +
            (s.title || s.id.slice(0, 12)) +
            (s.message_count ? `  (${s.message_count} msgs)` : "")}
        </Text>
      ))}
    </Box>
  );
}

// ─── helpers ────────────────────────────────────────────────────────

function roleOf(role: string): Role {
  return role === "user" || role === "assistant" || role === "tool"
    ? role
    : "system";
}

function colorFor(role: Role): string {
  return role === "user"
    ? theme.user
    : role === "assistant"
      ? theme.assistant
      : role === "tool"
        ? theme.tool
        : theme.muted;
}

function prefixFor(role: Role): string {
  return role === "system"
    ? "» "
    : role === "user"
      ? "‹ "
      : role === "assistant"
        ? "› "
        : "* ";
}
