// OpenComputer TUI — build script (esbuild).
//
// Bundles the TypeScript source into self-contained ESM artifacts under
// ../dist/. Two outputs:
//   entry.js      — the full Ink TUI app (launched by `oc tui`)
//   wireClient.js — the standalone wire client (for tests / IDE bridges)
//
// The `banner` injects a `require` shim: `ws` is a CommonJS package and
// calls `require('events')` at runtime; an ESM bundle has no `require`
// unless we create one from `import.meta.url`.

import { writeFileSync } from "node:fs";

import * as esbuild from "esbuild";

const banner = {
  js: "import { createRequire as __cr } from 'node:module'; const require = __cr(import.meta.url);",
};

const common = {
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node22",
  banner,
};

await esbuild.build({
  ...common,
  entryPoints: ["entry.tsx"],
  outfile: "../dist/entry.js",
});

await esbuild.build({
  ...common,
  entryPoints: ["wireClient.ts"],
  outfile: "../dist/wireClient.js",
});

// Render-smoke harnesses (test artifacts — see tests/test_ui_tui_integration.py).
await esbuild.build({
  ...common,
  entryPoints: ["renderSmoke.tsx"],
  outfile: "../dist/renderSmoke.js",
});

await esbuild.build({
  ...common,
  entryPoints: ["overlaysSmoke.tsx"],
  outfile: "../dist/overlaysSmoke.js",
});

await esbuild.build({
  ...common,
  entryPoints: ["markdownSmoke.tsx"],
  outfile: "../dist/markdownSmoke.js",
});

// `oc tui` runs the bundle as ESM — the dist/ marker makes Node treat
// the .js files as modules without a per-file extension dance.
writeFileSync("../dist/package.json", JSON.stringify({ type: "module" }));

console.log("built ../dist/entry.js + wireClient.js + renderSmoke.js");
