// OpenComputer TUI — small shared widgets (TypeScript source).
//
// TUI-parity Milestone 2. Reusable presentational bits used across the app.

import React, { useEffect, useState } from "react";
import { Text } from "ink";

import { theme } from "./theme.js";

const SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/**
 * An animated braille spinner for the busy state.
 *
 * The interval is cleared on unmount — Ink re-renders on every frame, so a
 * leaked timer would keep the process alive after the TUI exits.
 */
export function Spinner({ label = "" }: { label?: string }): React.ReactElement {
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    const timer = setInterval(
      () => setFrame((f) => (f + 1) % SPINNER_FRAMES.length),
      80,
    );
    return () => clearInterval(timer);
  }, []);

  return (
    <Text color={theme.accent}>
      {SPINNER_FRAMES[frame]}
      {label ? ` ${label}` : ""}
    </Text>
  );
}
