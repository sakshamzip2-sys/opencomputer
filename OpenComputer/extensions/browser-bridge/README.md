# Browser Bridge — OpenComputer Layer 4 (minimal)

Captures tab navigation from Chrome / Brave / Edge and forwards each visit
to the local OpenComputer agent. Powers the "agent already knows what I'm
working on" awareness in Layer 4 of the Layered Awareness MVP.

## Install (Chrome / Brave / Edge)

1. Open `chrome://extensions/` (or `brave://extensions/`).
2. Toggle "Developer mode" on.
3. Click "Load unpacked".
4. Select the `extensions/browser-bridge/extension/` directory.
5. Note the extension ID Chrome assigns.

## Pair the extension with your OC agent

```bash
opencomputer profile bridge token
```

Copy the printed token. Then in Chrome's DevTools console (with the
extension's background page focused — go to `chrome://extensions/`,
click "Service worker" under OpenComputer Browser Bridge):

```javascript
chrome.storage.local.set({ ocBridgeToken: '<paste-token-here>' })
```

The extension immediately starts forwarding visits. Verify via:

```bash
opencomputer profile bridge status
```

You should see `Listener: REACHABLE`.

## What gets sent

URL + page title + timestamp. **No page content, no form data, no
cookies.** The listener is bound to `127.0.0.1` only — nothing leaves
your machine.

## Disabling temporarily

Disable the extension in `chrome://extensions/`. Or revoke the
capability:

```bash
opencomputer consent revoke ingestion.browser_extension
```
