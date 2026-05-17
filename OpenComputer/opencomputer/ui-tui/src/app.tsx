// OpenComputer TUI — main Ink application (TypeScript source).
//
// Adapted for OpenComputer from hermes-agent/ui-tui.
// Original: MIT License (c) 2025 Nous Research — see THIRD_PARTY_LICENSE_HERMES.
//
// TUI-parity Milestone 2. The OC-native terminal UI: a markdown-rendered
// streaming conversation, a multiline composer, a slash-command palette, a
// session picker, and six overlays (model picker, skills hub, settings,
// agents, rollback, tools) — all driven by OCWireClient (27 RPCs).

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";

import { useEditor } from "./editor.js";
import { Markdown } from "./markdown.js";
import {
  AgentsOverlay,
  ModelPickerOverlay,
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
  SessionRow,
  SlashCommandInfo,
  SubagentInfo,
  ToolInfo,
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

/** Slash names handled client-side (open an overlay) — not sent to the server. */
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

/** Client-side slash commands surfaced in the palette alongside server ones. */
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
];

export interface AppProps {
  client: OCWireClient;
  /** OC_TUI_RESUME value: "last", a session id/prefix, or "" for fresh. */
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
  const [oi, setOi] = useState(0); // shared overlay selection index
  const [usage, setUsage] = useState("");

  // Per-overlay data (only one overlay is open at a time).
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [models, setModels] = useState<ModelRow[]>([]);
  const [skills, setSkills] = useState<SkillRow[]>([]);
  const [skillPreview, setSkillPreview] = useState("");
  const [settings, setSettings] = useState<ConfigRow[]>([]);
  const [agents, setAgents] = useState<SubagentInfo[]>([]);
  const [checkpoints, setCheckpoints] = useState<CheckpointInfo[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);

  const sessionId = useRef<string | undefined>(undefined);

  const push = useCallback((t: Turn) => setTurns((prev) => [...prev, t]), []);
  const sys = useCallback(
    (text: string) => push({ role: "system", text }),
    [push],
  );

  // The slash palette is pure derived state — shown whenever the composer
  // holds a single "/word" with no overlay open.
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
          // Dedup by name: a server command could collide with a
          // client-side one, and the palette keys rows by name —
          // duplicates there cause React key warnings. Client commands win.
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
        void refreshUsage();
      } else if (ev.event === EVENT.TOOL_CALL) {
        push({ role: "tool", text: `tool: ${String(payload.name ?? "")}` });
      } else if (ev.event === EVENT.ERROR) {
        push({ role: "system", text: `error: ${String(payload.error ?? "")}` });
        setBusy(false);
      }
    });

    return () => {
      offConn();
      offEv();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, resumeSpec]);

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
      /* usage is best-effort — never break the turn over it */
    }
  }

  // ── overlay openers (each fetches, never throws past sys()) ────────
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
    // Overlay-open state: navigation + per-overlay actions.
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
        ed.clear(); // ESC clears the composer; ESC again exits.
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
    // Composer.
    if (busy) return;
    if (key.return) {
      void send();
      return;
    }
    ed.onKey(raw, key); // multiline editing — see editor.ts
  });

  // ── submit ─────────────────────────────────────────────────────────
  async function send(): Promise<void> {
    const msg = ed.text.trim();
    if (!msg || busy || !connected) return;
    ed.clear();

    if (msg.startsWith("/")) {
      const parts = msg.slice(1).split(/\s+/);
      const name = (parts[0] ?? "").toLowerCase();
      const args = parts.slice(1).join(" ");

      // Client-side overlay commands — open the overlay, don't hit the server.
      if (name in OVERLAY_COMMANDS) {
        void openOverlay(OVERLAY_COMMANDS[name]!);
        return;
      }
      // Client-side action commands.
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
        if (!sid) {
          sys("/rename: no active session yet");
          return;
        }
        if (!args) {
          sys("usage: /rename <title>");
          return;
        }
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
        if (!sid) {
          sys("/fork: no active session yet");
          return;
        }
        try {
          const r = await client.sessionFork(sid, args);
          sys(`forked → ${r.new_session_id.slice(0, 12)}… (${r.messages_copied} msgs)`);
        } catch (e) {
          sys(`fork failed: ${(e as Error).message}`);
        }
        return;
      }
      // Anything else → server slash dispatch.
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
          {"   /model /skills /settings /agents /rollback /tools · Ctrl+N newline · ESC quit"}
        </Text>
      </Box>

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
        {turns.slice(-25).map((t, i) => (
          <Box
            key={i}
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
        {streamBuf && (
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

/** Multiline composer view — renders editor lines with the cursor. */
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
