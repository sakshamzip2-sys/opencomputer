// Stub for `react-devtools-core` — ink imports it lazily for the dev panel
// support, which we don't need in a bundled CLI distribution. The real
// package pulls in a 6MB+ tree of websocket / IDE-bridge code, none of
// which is reachable from `oc tui`'s code path. Aliasing it to this stub
// at bundle time keeps the produced `dist/entry.js` self-contained at
// ~1.7 MB and avoids "Cannot find package 'react-devtools-core'" at
// runtime when the wheel is installed without `node_modules/`.
export default { connectToDevTools: () => {} };
