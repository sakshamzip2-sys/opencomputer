// OpenComputer TUI — streaming-safe markdown renderer (TypeScript source).
//
// TUI-parity Milestone 2. Renders assistant text as markdown in the
// terminal: headings, fenced code blocks, bullet / numbered lists, inline
// `code` and **bold**.
//
// Deliberately LINE-BASED, not a full AST parser: assistant output streams
// token-by-token, so the renderer is constantly handed *incomplete*
// markdown — an unclosed code fence, a half-typed **bold**. A line-based
// pass renders best-effort and never throws on a partial document, which
// a strict parser would. Mirrors hermes-agent's streamingMarkdown.tsx.

import React from "react";
import { Box, Text } from "ink";

import { theme } from "./theme.js";

const FENCE = /^\s*```/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^(\s*)[-*]\s+(.*)$/;
const NUMBERED = /^(\s*)(\d+\.)\s+(.*)$/;

/** Split one line into styled segments for inline `code` and **bold**. */
function renderInline(s: string, keyBase: string): React.ReactNode[] {
  const out: React.ReactNode[] = [];
  // Code spans first — their content must not be re-parsed for bold.
  s.split(/(`[^`]+`)/g).forEach((part, i) => {
    if (part.length >= 2 && part.startsWith("`") && part.endsWith("`")) {
      out.push(
        <Text key={`${keyBase}-c${i}`} color={theme.code}>
          {part.slice(1, -1)}
        </Text>,
      );
      return;
    }
    // Bold within the remaining plain text.
    part.split(/(\*\*[^*]+\*\*)/g).forEach((bp, j) => {
      if (bp.length >= 4 && bp.startsWith("**") && bp.endsWith("**")) {
        out.push(
          <Text key={`${keyBase}-b${i}-${j}`} bold color={theme.bold}>
            {bp.slice(2, -2)}
          </Text>,
        );
      } else if (bp) {
        out.push(<Text key={`${keyBase}-t${i}-${j}`}>{bp}</Text>);
      }
    });
  });
  return out;
}

export function Markdown({ text }: { text: string }): React.ReactElement {
  const rendered: React.ReactNode[] = [];
  let inCode = false;

  text.split("\n").forEach((line, i) => {
    const key = `md-${i}`;

    if (FENCE.test(line)) {
      // Toggle the fence; the ``` line itself isn't rendered. An unclosed
      // fence (streaming) simply leaves inCode true for the rest — correct.
      inCode = !inCode;
      return;
    }
    if (inCode) {
      rendered.push(
        <Text key={key} color={theme.codeBlock}>
          {"  " + line}
        </Text>,
      );
      return;
    }

    const h = HEADING.exec(line);
    if (h) {
      rendered.push(
        <Text key={key} bold color={theme.heading}>
          {h[2]}
        </Text>,
      );
      return;
    }

    const b = BULLET.exec(line);
    if (b) {
      rendered.push(
        <Text key={key}>
          <Text color={theme.bullet}>{b[1] + "• "}</Text>
          {renderInline(b[2] ?? "", key)}
        </Text>,
      );
      return;
    }

    const n = NUMBERED.exec(line);
    if (n) {
      rendered.push(
        <Text key={key}>
          <Text color={theme.bullet}>{n[1] + n[2] + " "}</Text>
          {renderInline(n[3] ?? "", key)}
        </Text>,
      );
      return;
    }

    rendered.push(<Text key={key}>{renderInline(line, key)}</Text>);
  });

  return <Box flexDirection="column">{rendered}</Box>;
}
