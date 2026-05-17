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
  // Ink lazy-loads react-devtools-core only when DEV=true; never needed
  // in a shipped TUI, and it isn't installed.
  external: ["react-devtools-core"],
});

await esbuild.build({
  ...common,
  entryPoints: ["wireClient.ts"],
  outfile: "../dist/wireClient.js",
});

// `oc tui` runs the bundle as ESM — the dist/ marker makes Node treat
// the .js files as modules without a per-file extension dance.
writeFileSync("../dist/package.json", JSON.stringify({ type: "module" }));

console.log("built ../dist/entry.js + ../dist/wireClient.js");
