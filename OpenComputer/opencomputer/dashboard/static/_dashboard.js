// Shared dashboard helpers (Wave 6.D-α). Loaded by every static page so the
// auth-token attach + fetch wrappers don't need duplicating.
//
// The session token is injected into every static page by the FastAPI host
// (see opencomputer/dashboard/server.py — `__SESSION_TOKEN__` placeholder
// substitution). It's intentionally NOT printed visibly in the DOM — only
// captured via window.OC_TOKEN during page load.

(function () {
  // Attach token to every fetch automatically.
  const token = window.OC_TOKEN || '';
  function authHeaders() {
    if (!token) return {};
    return { Authorization: 'Bearer ' + token };
  }

  // GET helper. Returns parsed JSON or throws Error('http <status>').
  async function ocGet(url) {
    const r = await fetch(url, { headers: authHeaders() });
    if (!r.ok) throw new Error('http ' + r.status);
    return r.json();
  }

  // POST helper. Body is auto-JSON-encoded if provided.
  async function ocPost(url, body) {
    const opts = {
      method: 'POST',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(url, opts);
    const text = await r.text();
    let parsed;
    try { parsed = text ? JSON.parse(text) : null; } catch { parsed = { detail: text }; }
    if (!r.ok) {
      const err = new Error('http ' + r.status + ': ' + (parsed?.detail || text));
      err.status = r.status;
      err.body = parsed;
      throw err;
    }
    return parsed;
  }

  // Tiny formatter for big numbers.
  function fmtNum(n) {
    if (n === null || n === undefined) return '—';
    if (typeof n !== 'number') return String(n);
    if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
    if (n >= 1_000) return (n / 1_000).toFixed(1) + 'k';
    return String(n);
  }

  function fmtMs(n) {
    if (n === null || n === undefined) return '—';
    if (n >= 10_000) return (n / 1000).toFixed(1) + 's';
    return Math.round(n) + 'ms';
  }

  // Build the shared nav. Pages call this to render the top bar.
  function renderNav(active) {
    const tabs = [
      { id: 'chat', label: 'Chat', href: '/' },
      { id: 'plugins', label: 'Plugins', href: '/static/plugins.html' },
      { id: 'models', label: 'Models', href: '/static/models.html' },
    ];
    const html = tabs.map(t => {
      const cls = t.id === active ? 'tab active' : 'tab';
      return `<a class="${cls}" href="${t.href}">${t.label}</a>`;
    }).join('');
    const slot = document.getElementById('nav-tabs');
    if (slot) slot.innerHTML = html;
  }

  // Status pill helper — colour-codes by category.
  function statusPill(text, kind) {
    const cls = kind ? 'pill pill-' + kind : 'pill';
    return `<span class="${cls}">${text}</span>`;
  }

  // Subscribe to a Server-Sent Events stream (Wave 6.D-β).
  // Returns an object with a .close() method. The browser auto-reconnects
  // on disconnect, so the caller doesn't need to manage retries — but
  // when the page unloads, .close() is the polite way to drop the
  // connection.
  function subscribeStream(url, onMessage, onError) {
    if (!('EventSource' in window)) {
      console.warn('EventSource not supported — SSE updates disabled');
      return { close: () => {} };
    }
    // Token attached via query string (EventSource can't set headers).
    const sep = url.includes('?') ? '&' : '?';
    const fullUrl = token ? url + sep + 'token=' + encodeURIComponent(token) : url;
    const es = new EventSource(fullUrl);
    es.addEventListener('change', (e) => {
      try {
        const data = JSON.parse(e.data);
        onMessage(data);
      } catch (err) {
        console.error('SSE parse error', err);
      }
    });
    es.onerror = (e) => {
      if (onError) onError(e);
    };
    return { close: () => es.close() };
  }

  window.OCDash = {
    ocGet, ocPost, fmtNum, fmtMs, renderNav, statusPill, subscribeStream,
  };
})();
