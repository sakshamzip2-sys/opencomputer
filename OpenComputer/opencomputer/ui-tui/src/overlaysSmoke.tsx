// OpenComputer TUI — overlay render smoke check.
//
// Mounts all six overlay components with sample data and prints the
// combined frame. Verifies every overlay genuinely RENDERS — typecheck
// proves they compile, this proves the component trees mount and produce
// the expected panel output.
//
// Driven by tests/test_ui_tui_integration.py. A build/test artifact.

import { Box } from "ink";
import { render } from "ink-testing-library";

import {
  AgentsOverlay,
  ModelPickerOverlay,
  RollbackOverlay,
  SettingsOverlay,
  SkillsHubOverlay,
  ToolsOverlay,
} from "./overlays.js";

const tree = (
  <Box flexDirection="column">
    <ModelPickerOverlay
      rows={[
        { provider: "anthropic", model: "claude-opus-4-7", isCurrent: true },
        { provider: "openai", model: "gpt-5", isCurrent: false },
      ]}
      index={0}
    />
    <SkillsHubOverlay
      skills={[{ id: "demo", name: "demo-skill", description: "a demo" }]}
      index={0}
      preview="## Body\nthe skill body"
    />
    <SettingsOverlay
      entries={[{ key: "model.provider", value: "anthropic" }]}
      index={0}
    />
    <AgentsOverlay
      subagents={[
        {
          agent_id: "a1",
          goal: "investigate the bug",
          state: "running",
          display_state: "running",
          role: "leaf",
          depth: 0,
          started_at: "2026-05-17T00:00:00Z",
        },
      ]}
      index={0}
    />
    <RollbackOverlay
      checkpoints={[
        {
          id: "cp1",
          session_id: "s1",
          prompt_index: 3,
          label: "before-edit",
          created_at: 0,
          message_count: 6,
        },
      ]}
      index={0}
    />
    <ToolsOverlay
      tools={[{ name: "Edit", description: "edit a file" }]}
      index={0}
    />
  </Box>
);

const { lastFrame, unmount } = render(tree);

setTimeout(() => {
  const frame = lastFrame() ?? "";
  unmount();
  process.stdout.write(`FRAME_START\n${frame}\nFRAME_END\n`);
  process.exit(0);
}, 300);
