// Adapted for OpenComputer 2026-05-07 from hermes-agent/ui-tui
// Original: MIT License (c) 2025 Nous Research

import React, { useEffect, useRef, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import { OCWireClient, type SlashCommand, type WireEvent } from "./gatewayClient.js";
import { banner, theme } from "./theme.js";

interface Turn {
  role: "user" | "assistant" | "tool" | "system";
  text: string;
}

interface AppProps {
  client: OCWireClient;
}

export const App: React.FC<AppProps> = ({ client }) => {
  const { exit } = useApp();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(client.connected);
  const [streamBuf, setStreamBuf] = useState("");
  const [helloInfo, setHelloInfo] = useState<string>("");
  const [slashList, setSlashList] = useState<SlashCommand[]>([]);
  const [showSlashPalette, setShowSlashPalette] = useState(false);
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
        setStreamBuf((prev) => prev + String(payload.text ?? payload.content ?? ""));
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
      }
    });
    return () => { offConn(); offEv(); };
  }, [client]);

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
