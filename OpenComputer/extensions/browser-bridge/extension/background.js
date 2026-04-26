// OpenComputer Browser Bridge — Chrome MV3 service worker.
// Forwards tab navigation events to the local OC agent's listener.
//
// Token must be set via chrome.storage.local before events flow.
// Set via the install-time UI (post-MVP) or manually for now:
//   chrome.storage.local.set({ ocBridgeToken: '<paste token>' })

const ENDPOINT = 'http://127.0.0.1:18791/browser-event';

async function getToken() {
  const result = await chrome.storage.local.get(['ocBridgeToken']);
  return result.ocBridgeToken || '';
}

async function postVisit(url, title) {
  const token = await getToken();
  if (!token) {
    return;  // bridge disabled until user pastes token
  }
  if (!url || url.startsWith('chrome://') || url.startsWith('about:')) {
    return;  // skip browser-internal URLs
  }
  try {
    await fetch(ENDPOINT, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({
        url,
        title: title || '',
        visit_time: Date.now() / 1000,
      }),
    });
  } catch (err) {
    // local listener not running — silently drop. No retries in MVP.
  }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === 'complete' && tab.url) {
    postVisit(tab.url, tab.title);
  }
});
