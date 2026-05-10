// Adapted for OpenComputer 2026-05-07 from hermes-agent/ui-tui
// Original: MIT License (c) 2025 Nous Research

import React, { useEffect, useRef, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import { OCWireClient, type SlashCommand, type WireEvent } from "./gatewayClient.js";
import { banner, theme } from "./theme.js";
import {
  MemoryPanel,
  seedFromStatusEntry,
  type MemoryWritePayload,
} from "./components/memoryPanel.js";

interface Turn {
  role: "user" | "assistant" | "tool" | "system";
  text: string;
}

interface AppProps {
  client: OCWireClient;
  // OC_TUI_RESUME contract: "" = fresh, "last" = most recent session,
  // anything else = treated as a session-id (or id prefix). Mirrors
  // HERMES_TUI_RESUME from hermes-agent.
  resumeSpec?: string;
}

export const App: React.FC<AppProps> = ({ client, resumeSpec = "" }) => {
  const { exit } = useApp();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(client.connected);
  const [streamBuf, setStreamBuf] = useState("");
  const [helloInfo, setHelloInfo] = useState<string>("");
  const [slashList, setSlashList] = useState<SlashCommand[]>([]);
  const [showSlashPalette, setShowSlashPalette] = useState(false);
  // Tier-C: per-target map of memory status. Seeded from `memory.status`
  // RPC on connect (so the panel renders MEMORY.md + USER.md from the
  // first frame, not after the first write). memory.write events update
  // the matching entry in-place.
  const [memoryEntries, setMemoryEntries] = useState<Record<string, MemoryWritePayload>>({});
  const sessionId = useRef<string | undefined>(undefined);

  useEffect(() => {
    const offConn = client.onConnected(async (ok) => {
      setConnected(ok);
      if (ok) {
        try {
          const h = await client.hello();
          setHelloInfo(`${h.server} v${h.version} (${h.methods.length} methods)`);
          setTurns((t) => [
            ...t,
            { role: "system", text: `connected to ${client.serverUrl} — ${h.server} v${h.version}` },
          ]);
          // Pre-fetch slash commands for the palette
          try {
            const s = await client.slashList();
            setSlashList(s.commands);
          } catch {
            // older wire-server without slash.list — ignore
          }
          // Tier-C+: seed the memory panel with current cap status so
          // MEMORY.md / USER.md are visible from the first frame, not
          // after the first write event. Older wire-servers without
          // memory.status reject as "unknown method"; the panel stays
          // empty and updates lazily from memory.write events instead.
          try {
            const status = await client.memoryStatus();
            const seeded: Record<string, MemoryWritePayload> = {};
            for (const entry of status.entries) {
              seeded[entry.target] = seedFromStatusEntry(entry);
            }
            setMemoryEntries(seeded);
          } catch {
            // older wire-server without memory.status — empty panel
            // until the first memory.write event arrives.
          }
          // Resume target plumbing: if OC_TUI_RESUME is set, seed
          // sessionId.current so the first ``client.chat()`` call
          // routes into that session instead of starting fresh.
          if (resumeSpec && !sessionId.current) {
            try {
              if (resumeSpec === "last") {
                const r = (await client.sessionsList(1)) as { sessions: Array<{ session_id?: string; id?: string }> };
                const first = r.sessions?.[0];
                const sid = first?.session_id ?? first?.id;
                if (sid) {
                  sessionId.current = sid;
                  setTurns((t) => [...t, { role: "system", text: `resumed latest session: ${sid.slice(0, 12)}…` }]);
                } else {
                  setTurns((t) => [...t, { role: "system", text: "OC_TUI_RESUME=last but no recent sessions found — starting fresh" }]);
                }
              } else {
                // Treat as a literal session id (or prefix). Wire chat
                // accepts the prefix; the dispatch layer resolves it.
                sessionId.current = resumeSpec;
                setTurns((t) => [...t, { role: "system", text: `resuming session: ${resumeSpec}` }]);
              }
            } catch (e) {
              setTurns((t) => [...t, { role: "system", text: `resume failed: ${(e as Error).message}` }]);
            }
          }
        } catch (e) {
          setTurns((t) => [...t, { role: "system", text: `hello failed: ${(e as Error).message}` }]);
        }
      } else {
        setTurns((t) => [...t, { role: "system", text: "disconnected — reconnecting…" }]);
      }
    });
    const offEv = client.onEvent((ev: WireEvent) => {
      const payload = (ev.payload ?? {}) as Record<string, unknown>;
      if (ev.event === "assistant.message" || ev.event === "turn.assistant") {
        // The wire server emits assistant.message with key `delta` (see
        // opencomputer/gateway/wire_server.py:_handle_chat:_on_chunk). The
        // legacy `text` / `content` keys are kept as fallbacks for any
        // future server variant or hand-crafted client. Pre-2026-05-10 only
        // text/content were checked, so every chat reply was silently
        // dropped — the WS event arrived but reading the wrong key gave
        // undefined → "" → nothing accumulated → turn.end fired with empty
        // streamBuf → no assistant line shown.
        setStreamBuf((prev) => prev + String(payload.delta ?? payload.text ?? payload.content ?? ""));
      } else if (ev.event === "turn.end") {
        setStreamBuf((prev) => {
          if (prev) setTurns((t) => [...t, { role: "assistant", text: prev }]);
          return "";
        });
        setBusy(false);
      } else if (ev.event === "tool.call") {
        setTurns((t) => [
          ...t,
          { role: "tool", text: `tool: ${String(payload.name ?? "")}` },
        ]);
      } else if (ev.event === "error") {
        setTurns((t) => [
          ...t,
          { role: "system", text: `error: ${String(payload.error ?? "")}` },
        ]);
        setBusy(false);
      } else if (ev.event === "memory.write") {
        // Tier-C of 2026-05-10 memory-observability design.
        // Schema mirrors gateway/protocol_v2.MemoryWritePayload. Update
        // the per-target entry so MEMORY.md and USER.md state both stay
        // visible (writing one file doesn't hide the other).
        const update = payload as unknown as MemoryWritePayload;
        if (update.target) {
          setMemoryEntries((prev) => ({ ...prev, [update.target]: update }));
        }
      }
    });
    return () => { offConn(); offEv(); };
  }, [client, resumeSpec]);

  useInput((rawInput, key) => {
    if (key.escape) exit();
    if (key.ctrl && rawInput === "c") exit();
    if (rawInput === "/" && input === "") {
      setShowSlashPalette(true);
    }
  });

  async function send() {
    const msg = input.trim();
    if (!msg || busy || !connected) return;
    setInput("");
    setShowSlashPalette(false);

    if (msg.startsWith("/")) {
      const parts = msg.slice(1).split(/\s+/);
      const name = parts[0];
      const args = parts.slice(1).join(" ");
      setTurns((t) => [...t, { role: "user", text: msg }]);
      try {
        const r = await client.slashDispatch(name, args);
        setTurns((t) => [...t, { role: "system", text: r.output || "(no output)" }]);
      } catch (e) {
        setTurns((t) => [
          ...t,
          { role: "system", text: `slash error: ${(e as Error).message}` },
        ]);
      }
      return;
    }

    setTurns((t) => [...t, { role: "user", text: msg }]);
    setBusy(true);
    setStreamBuf("");
    try {
      await client.chat(msg, sessionId.current);
    } catch (e) {
      setTurns((t) => [
        ...t,
        { role: "system", text: `wire error: ${(e as Error).message}` },
      ]);
      setBusy(false);
    }
  }

  return (
    <Box flexDirection="column">
      <Box flexDirection="column" marginBottom={1}>
        <Text color={theme.accent}>{banner}</Text>
        <Text color={theme.muted}>
          {helloInfo || "connecting…"}
          {"  "}
          <Text color={connected ? theme.success : theme.error}>
            ● {connected ? "connected" : "disconnected"}
          </Text>
          {"   ESC or Ctrl+C to quit"}
        </Text>
        <MemoryPanel entries={memoryEntries} />
      </Box>

      {showSlashPalette && slashList.length > 0 && (
        <Box flexDirection="column" borderStyle="round" borderColor={theme.muted} marginBottom={1}>
          <Text color={theme.accent}> Slash commands ({slashList.length}):</Text>
          {slashList.slice(0, 10).map((c) => (
            <Text key={c.name}>
              <Text color={theme.accent}>  /{c.name}</Text>
              <Text color={theme.muted}> — {c.description}</Text>
            </Text>
          ))}
          {slashList.length > 10 && (
            <Text color={theme.muted}>  …and {slashList.length - 10} more</Text>
          )}
        </Box>
      )}

      <Box flexDirection="column" marginBottom={1}>
        {turns.slice(-25).map((t, i) => (
          <Box key={i} marginBottom={t.role === "system" ? 0 : 1}>
            <Text color={
              t.role === "user" ? theme.user :
              t.role === "assistant" ? theme.assistant :
              t.role === "tool" ? theme.tool :
              theme.muted
            }>
              {t.role === "system" ? "» " : t.role === "user" ? "‹ " : t.role === "assistant" ? "› " : "* "}
              {t.text}
            </Text>
          </Box>
        ))}
        {streamBuf && (
          <Text color={theme.assistant}>› {streamBuf}<Text color={theme.muted}>▌</Text></Text>
        )}
      </Box>

      <Box>
        <Text color={busy ? theme.muted : theme.accent}>{busy ? "…" : ">"} </Text>
        <TextInput
          value={input}
          onChange={(v) => {
            setInput(v);
            if (v === "") setShowSlashPalette(false);
          }}
          onSubmit={send}
          placeholder={connected ? "Type a message or /slash command" : "waiting for wire…"}
        />
      </Box>
    </Box>
  );
};
