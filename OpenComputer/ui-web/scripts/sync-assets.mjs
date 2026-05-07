#!/usr/bin/env node
// Copies fonts + ds-assets out of @nous-research/ui's dist/ into our
// public/ so Vite serves them. Mirrors Hermes's web/package.json:6
// "sync-assets" script. Idempotent.

import { existsSync, mkdirSync, rmSync, cpSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, "..");
const DS_ROOT = path.resolve(ROOT, "node_modules/@nous-research/ui/dist");
const PUBLIC_DIR = path.resolve(ROOT, "public");

if (!existsSync(DS_ROOT)) {
  console.warn(
    `[sync-assets] @nous-research/ui not installed yet at ${DS_ROOT} — skipping`,
  );
  process.exit(0);
}

for (const sub of ["fonts", "assets"]) {
  const src = path.join(DS_ROOT, sub);
  // Hermes's pattern names them "fonts" and "ds-assets" on the public side.
  const destName = sub === "assets" ? "ds-assets" : sub;
  const dest = path.join(PUBLIC_DIR, destName);
  if (existsSync(dest)) rmSync(dest, { recursive: true, force: true });
  if (existsSync(src)) {
    mkdirSync(path.dirname(dest), { recursive: true });
    cpSync(src, dest, { recursive: true });
    console.log(`[sync-assets] copied ${src} → ${dest}`);
  } else {
    console.warn(`[sync-assets] not found: ${src}`);
  }
}
