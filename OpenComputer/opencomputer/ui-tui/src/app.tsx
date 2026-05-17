// OpenComputer TUI — main Ink application (TypeScript source).
//
// Adapted for OpenComputer from hermes-agent/ui-tui.
// Original: MIT License (c) 2025 Nous Research — see THIRD_PARTY_LICENSE_HERMES.
//
// TUI-parity Milestone 2. The OC-native terminal UI: a conversation view
// with streaming, a slash-command palette, and a session-picker overlay —
// all driven by OCWireClient (opencomputer.gateway.wire_server, 27 RPCs).
// This is the typed source of the artifact that ships at ui-tui/dist/.

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";

import type {
  SessionRow,
  SlashCommandInfo,
  WireServerEvent,
} from "./protocol.js";
import { EVENT } from "./protocol.js";
import { OCWireClient } from "./wireClient.js";

// ─── theme ──────────────────────────────────────────────────────────

const theme = {
  accent: "cyan",
  user: "white",
  assistant: "green",
  tool: "yellow",
  muted: "gray",
  ok: "green",
  error: "red",
} as const;

// ─── model ──────────────────────────────────────────────────────────

type Role = "user" | "assistant" | "system" | "tool";
interface Turn {
  role: Role;
  text: string;
}
type Overlay = "none" | "slash" | "sessions";

export interface AppProps {
  client: OCWireClient;
  /** OC_TUI_RESUME value: "last", a session id/prefix, or "" for fresh. */
  resumeSpec?: string;
}

// ─── component ──────────────────────────────────────────────────────

export function App({ client, resumeSpec = "" }: AppProps): React.ReactElement {
  const { exit } = useApp();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(client.connected);
  const [streamBuf, setStreamBuf] = useState("");
  const [helloInfo, setHelloInfo] = useState("");
  const [slashList, setSlashList] = useState<SlashCommandInfo[]>([]);
  const [overlay, setOverlay] = useState<Overlay>("none");
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [pickIndex, setPickIndex] = useState(0);
  const sessionId = useRef<string | undefined>(undefined);

  const push = useCallback((t: Turn) => setTurns((prev) => [...prev, t]), []);
  const sys = useCallback(
    (text: string) => push({ role: "system", text }),
    [push],
  );

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
          setSlashList(s.commands);
        } catch {
          /* older wire-server without slash.list — ignore */
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
        } else {
          sys("OC_TUI_RESUME=last but no sessions found — starting fresh");
        }
      } else {
        // Treat as a literal id — load its transcript so past turns render.
        const r = await client.sessionResume(resumeSpec);
        sessionId.current = r.session_id;
        for (const m of r.messages) {
          push({ role: roleOf(m.role), text: m.text ?? "" });
        }
        sys(`resumed session ${r.session_id.slice(0, 12)}… (${r.message_count} msgs)`);
      }
    } catch (e) {
      sys(`resume failed: ${(e as Error).message}`);
    }
  }

  // ── session-picker overlay data ────────────────────────────────────
  const openSessionPicker = useCallback(async () => {
    try {
      const r = await client.sessionsList(30);
      setSessions(r.sessions);
      setPickIndex(0);
      setOverlay("sessions");
    } catch (e) {
      sys(`session list failed: ${(e as Error).message}`);
    }
  }, [client, sys]);

  async function resumePicked(row: SessionRow): Promise<void> {
    setOverlay("none");
    try {
      const r = await client.sessionResume(row.id);
      sessionId.current = r.session_id;
      setTurns([]);
      for (const m of r.messages) {
        push({ role: roleOf(m.role), text: m.text ?? "" });
      }
      sys(`resumed ${r.session_id.slice(0, 12)}… (${r.message_count} msgs)`);
    } catch (e) {
      sys(`resume failed: ${(e as Error).message}`);
    }
  }

  async function deletePicked(row: SessionRow): Promise<void> {
    try {
      const r = await client.sessionDelete(row.id);
      if (r.found) {
        setSessions((prev) => prev.filter((s) => s.id !== row.id));
        setPickIndex((i) => Math.max(0, i - 1));
      }
    } catch (e) {
      sys(`delete failed: ${(e as Error).message}`);
    }
  }

  // ── keyboard ───────────────────────────────────────────────────────
  useInput((raw, key) => {
    // Global quit.
    if (key.escape && overlay === "none") {
      exit();
      return;
    }
    if (key.ctrl && raw === "c") {
      exit();
      return;
    }
    // Overlay navigation.
    if (overlay === "sessions") {
      if (key.escape) {
        setOverlay("none");
      } else if (key.upArrow) {
        setPickIndex((i) => Math.max(0, i - 1));
      } else if (key.downArrow) {
        setPickIndex((i) => Math.min(sessions.length - 1, i + 1));
      } else if (key.return && sessions[pickIndex]) {
        void resumePicked(sessions[pickIndex]);
      } else if ((key.delete || key.backspace) && sessions[pickIndex]) {
        void deletePicked(sessions[pickIndex]);
      }
      return;
    }
    if (overlay === "slash" && key.escape) {
      setOverlay("none");
      return;
    }
    // Ctrl+R → session picker.
    if (key.ctrl && raw === "r") {
      void openSessionPicker();
      return;
    }
    // Composer.
    if (busy) return;
    if (key.return) {
      void send();
      return;
    }
    if (key.backspace || key.delete) {
      setInput((v) => v.slice(0, -1));
      setOverlay("none");
      return;
    }
    if (raw && !key.ctrl && !key.meta) {
      const next = input + raw;
      setInput(next);
      setOverlay(next.startsWith("/") ? "slash" : "none");
    }
  });

  // ── submit ─────────────────────────────────────────────────────────
  async function send(): Promise<void> {
    const msg = input.trim();
    if (!msg || busy || !connected) return;
    setInput("");
    setOverlay("none");

    if (msg.startsWith("/")) {
      const parts = msg.slice(1).split(/\s+/);
      const name = parts[0] ?? "";
      const args = parts.slice(1).join(" ");
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
          {"   Ctrl+R sessions · ESC quit"}
        </Text>
      </Box>

      {overlay === "slash" && slashList.length > 0 && (
        <SlashPalette commands={slashList} filter={input.slice(1)} />
      )}

      {overlay === "sessions" && (
        <SessionPicker sessions={sessions} index={pickIndex} />
      )}

      <Box flexDirection="column" marginBottom={1}>
        {turns.slice(-25).map((t, i) => (
          <Box key={i} marginBottom={t.role === "system" ? 0 : 1}>
            <Text color={colorFor(t.role)}>
              {prefixFor(t.role)}
              {t.text}
            </Text>
          </Box>
        ))}
        {streamBuf && (
          <Text color={theme.assistant}>
            {"› "}
            {streamBuf}
            <Text color={theme.muted}>▌</Text>
          </Text>
        )}
      </Box>

      <Box>
        <Text color={busy ? theme.muted : theme.accent}>{busy ? "… " : "> "}</Text>
        <Text>{input || (connected ? "" : "waiting for wire…")}</Text>
        {!busy && <Text color={theme.muted}>▌</Text>}
      </Box>
    </Box>
  );
}

// ─── sub-components ─────────────────────────────────────────────────

function SlashPalette({
  commands,
  filter,
}: {
  commands: SlashCommandInfo[];
  filter: string;
}): React.ReactElement {
  const matches = commands.filter((c) => c.name.startsWith(filter)).slice(0, 10);
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
