// i18n scaffold — Hermes-followup A1 (2026-05-07).
//
// English is the only complete locale shipped today. The structure
// is in place so additional locales can be added by dropping a new
// dict into LOCALES and the picker auto-discovers them.
//
// Usage:
//   <span data-i18n="header.connected">connected</span>
//   <input data-i18n-placeholder="chat.placeholder" placeholder="Type…">
//
// On page load, OCi18n.applyAll() walks the DOM and substitutes any
// element with a data-i18n* attribute. Calling OCi18n.setLocale(id)
// re-applies under a new locale and persists the choice.

(function () {
  const LOCALES = {
    en: {
      label: 'English',
      strings: {
        'header.connecting':       'connecting…',
        'header.connected':        'connected',
        'header.disconnected':     'disconnected',
        'tabs.chat':               'Chat',
        'tabs.plugins':            'Plugins',
        'tabs.models':             'Models',
        'tabs.calls':              'LLM Calls',
        'sidebar.sessions':        'Sessions',
        'sidebar.no_sessions':     '(no sessions yet)',
        'chat.placeholder':        'Type your message…',
        'chat.send':               'Send',
        'chat.hint_prefix':        'Type a message and press Enter. The dashboard talks to the wire server at',
        'chat.no_reply':           '(no reply)',
        'calls.title':             'Recent LLM Calls',
        'calls.empty':             'No LLM calls recorded yet. Start a chat to populate.',
        'calls.col.model':         'Model',
        'calls.col.provider':      'Provider',
        'calls.col.tokens_in':     'Tokens In',
        'calls.col.tokens_out':    'Tokens Out',
        'calls.col.cost':          'Cost',
        'calls.col.when':          'When',
        'calls.unknown_cost':      '—',
        'mgmt.title':              'Management',
        'mgmt.gateway_restart':    'Restart Gateway Daemon',
        'mgmt.gateway_restart_help': 'Re-execs the gateway process. In-flight conversations are dropped.',
        'mgmt.confirm_restart':    'Restart the gateway daemon? In-flight conversations will be dropped.',
        'mgmt.restart_ok':         'Gateway restart requested.',
        'mgmt.restart_failed':     'Gateway restart failed:',
      },
    },
    // Stub locales — same key set, English fallthrough until translated.
    es: { label: 'Español', strings: {} },
    fr: { label: 'Français', strings: {} },
    ja: { label: '日本語', strings: {} },
    zh: { label: '中文', strings: {} },
  };

  const STORAGE_KEY = 'oc-dashboard-locale';
  const DEFAULT_LOCALE = 'en';

  function getActiveLocale() {
    let l;
    try { l = localStorage.getItem(STORAGE_KEY); } catch { /* ignore */ }
    if (l && LOCALES[l]) return l;
    return DEFAULT_LOCALE;
  }

  function t(key) {
    const locale = getActiveLocale();
    const direct = LOCALES[locale]?.strings?.[key];
    if (direct) return direct;
    // Fall through to English when the active locale is missing the key.
    return LOCALES[DEFAULT_LOCALE].strings[key] ?? key;
  }

  function applyAll(root) {
    root = root || document;
    root.querySelectorAll('[data-i18n]').forEach((el) => {
      el.textContent = t(el.dataset.i18n);
    });
    root.querySelectorAll('[data-i18n-placeholder]').forEach((el) => {
      el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    root.querySelectorAll('[data-i18n-title]').forEach((el) => {
      el.title = t(el.dataset.i18nTitle);
    });
  }

  function setLocale(locale) {
    if (!LOCALES[locale]) return;
    try { localStorage.setItem(STORAGE_KEY, locale); } catch { /* ignore */ }
    applyAll();
    document.documentElement.lang = locale;
  }

  function listLocales() {
    return Object.entries(LOCALES).map(([id, l]) => ({ id, label: l.label }));
  }

  function renderLocalePicker(slotId) {
    const slot = document.getElementById(slotId);
    if (!slot) return;
    const select = document.createElement('select');
    select.className = 'locale-picker';
    select.title = 'Language';
    for (const { id, label } of listLocales()) {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = label;
      if (id === getActiveLocale()) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener('change', (e) => setLocale(e.target.value));
    slot.appendChild(select);
  }

  // Apply once on first script execution so static pages render in the
  // persisted locale before any imperative JS runs.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => applyAll());
  } else {
    applyAll();
  }

  window.OCi18n = {
    t, applyAll, setLocale, getActiveLocale, listLocales, renderLocalePicker, LOCALES,
  };
})();
