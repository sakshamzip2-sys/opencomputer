// Theme system — Hermes-followup A1 (2026-05-07).
//
// Switches the dashboard's colour palette at runtime by setting CSS
// custom properties on :root. Each theme is a flat dict of var → colour.
// Persisted in localStorage so the choice survives reloads.
//
// To use: include this file BEFORE _dashboard.js (or anywhere; just
// once). Pages opt-in by referencing the CSS vars rather than hardcoded
// colours — see index.html's <style> block for the canonical mapping.

(function () {
  const THEMES = {
    dark: {
      label: 'Dark',
      vars: {
        '--bg':            '#111111',
        '--bg-elev':       '#1a1a1a',
        '--bg-hover':      '#222222',
        '--fg':            '#dddddd',
        '--fg-muted':      '#888888',
        '--fg-faint':      '#555555',
        '--border':        '#333333',
        '--accent':        '#2563eb',
        '--accent-hover':  '#1d4ed8',
        '--ok':            '#4ade80',
        '--bad':           '#ef4444',
        '--user':          '#93c5fd',
        '--assistant':     '#4ade80',
      },
    },
    light: {
      label: 'Light',
      vars: {
        '--bg':            '#fafafa',
        '--bg-elev':       '#ffffff',
        '--bg-hover':      '#f0f0f0',
        '--fg':            '#222222',
        '--fg-muted':      '#666666',
        '--fg-faint':      '#999999',
        '--border':        '#dddddd',
        '--accent':        '#2563eb',
        '--accent-hover':  '#1d4ed8',
        '--ok':            '#16a34a',
        '--bad':           '#dc2626',
        '--user':          '#1d4ed8',
        '--assistant':     '#16a34a',
      },
    },
    solarized: {
      label: 'Solarized',
      vars: {
        '--bg':            '#002b36',
        '--bg-elev':       '#073642',
        '--bg-hover':      '#0a4250',
        '--fg':            '#93a1a1',
        '--fg-muted':      '#586e75',
        '--fg-faint':      '#465a61',
        '--border':        '#073642',
        '--accent':        '#268bd2',
        '--accent-hover':  '#1e6caa',
        '--ok':            '#859900',
        '--bad':           '#dc322f',
        '--user':          '#6c71c4',
        '--assistant':     '#2aa198',
      },
    },
    monokai: {
      label: 'Monokai',
      vars: {
        '--bg':            '#272822',
        '--bg-elev':       '#1e1f1c',
        '--bg-hover':      '#3e3d32',
        '--fg':            '#f8f8f2',
        '--fg-muted':      '#75715e',
        '--fg-faint':      '#5b594b',
        '--border':        '#3e3d32',
        '--accent':        '#f92672',
        '--accent-hover':  '#bf1d59',
        '--ok':            '#a6e22e',
        '--bad':           '#f92672',
        '--user':          '#66d9ef',
        '--assistant':     '#a6e22e',
      },
    },
  };

  const STORAGE_KEY = 'oc-dashboard-theme';
  const DEFAULT_THEME = 'dark';

  function getActiveTheme() {
    let t;
    try { t = localStorage.getItem(STORAGE_KEY); } catch { /* localStorage may be denied */ }
    if (t && THEMES[t]) return t;
    return DEFAULT_THEME;
  }

  function applyTheme(name) {
    const theme = THEMES[name] || THEMES[DEFAULT_THEME];
    const root = document.documentElement;
    for (const [k, v] of Object.entries(theme.vars)) {
      root.style.setProperty(k, v);
    }
    root.dataset.theme = name;
    try { localStorage.setItem(STORAGE_KEY, name); } catch { /* ignore */ }
  }

  function listThemes() {
    return Object.entries(THEMES).map(([id, t]) => ({ id, label: t.label }));
  }

  // Render a theme picker into a slot element.
  function renderThemePicker(slotId) {
    const slot = document.getElementById(slotId);
    if (!slot) return;
    const select = document.createElement('select');
    select.className = 'theme-picker';
    select.title = 'Theme';
    for (const { id, label } of listThemes()) {
      const opt = document.createElement('option');
      opt.value = id;
      opt.textContent = label;
      if (id === getActiveTheme()) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener('change', (e) => applyTheme(e.target.value));
    slot.appendChild(select);
  }

  // Apply on load BEFORE first paint (the script-tag order in HTML
  // ensures CSS-var resolution happens with the persisted theme set).
  applyTheme(getActiveTheme());

  window.OCThemes = { applyTheme, getActiveTheme, listThemes, renderThemePicker, THEMES };
})();
