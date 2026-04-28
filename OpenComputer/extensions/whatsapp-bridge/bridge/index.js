// OpenComputer WhatsApp Baileys bridge (PR 6.2).
//
// Speaks plain HTTP to the Python adapter on 127.0.0.1 and owns the
// encrypted WhatsApp multi-device websocket via Baileys. Designed to be
// spawned as a subprocess by the Python supervisor — env vars carry the
// configuration:
//
//   WHATSAPP_BRIDGE_HOST    bind host (default 127.0.0.1)
//   WHATSAPP_BRIDGE_PORT    bind port (default 3001)
//   WHATSAPP_BRIDGE_AUTH_DIR path to persist Baileys session creds
//
// Endpoints:
//   GET  /health                 — liveness probe
//   GET  /messages?since=&timeout= — long-poll inbound queue
//   POST /send {to, text}         — outbound send; returns {id}
//
// Echo suppression: every successful /send pushes the resulting id
// into recentlySentIds; on every inbound delivery we drop envelopes
// whose id is in that set (and discard from the set after match) so
// the Python side never sees its own outbound bouncing back.

'use strict';

const http = require('http');
const path = require('path');
const fs = require('fs');

const HOST = process.env.WHATSAPP_BRIDGE_HOST || '127.0.0.1';
const PORT = parseInt(process.env.WHATSAPP_BRIDGE_PORT || '3001', 10);
const AUTH_DIR = process.env.WHATSAPP_BRIDGE_AUTH_DIR ||
  path.join(process.env.HOME || '.', '.opencomputer', 'whatsapp-bridge');

// In-memory inbound queue. Capped — if Python is gone for a long time
// we drop the oldest envelopes rather than blow up RAM.
const INBOUND = [];
const INBOUND_CAP = 1000;
const recentlySentIds = new Set();
const RECENT_CAP = 2048;

let nextId = 1;
function _genId() { return `bridge-${Date.now()}-${nextId++}`; }

function _trimRecent() {
  if (recentlySentIds.size <= RECENT_CAP) return;
  // Drop the oldest ~25%.
  const arr = Array.from(recentlySentIds);
  const drop = Math.floor(arr.length / 4);
  for (let i = 0; i < drop; i++) recentlySentIds.delete(arr[i]);
}

function _enqueueInbound(env) {
  if (env && env.id && recentlySentIds.has(env.id)) {
    // Self-echo — drop on the bridge side.
    recentlySentIds.delete(env.id);
    return;
  }
  INBOUND.push(env);
  if (INBOUND.length > INBOUND_CAP) INBOUND.splice(0, INBOUND.length - INBOUND_CAP);
}

// ---------- Baileys integration ----------
// Wrapped in a try/catch so if `npm install` hasn't run, the bridge can
// still serve /health (and Python can detect the missing dep).

let sock = null;
let baileysReady = false;
let lastQr = null;

async function startBaileys() {
  let baileys;
  try {
    baileys = require('@whiskeysockets/baileys');
  } catch (e) {
    process.stderr.write(
      'whatsapp-bridge: @whiskeysockets/baileys not installed. ' +
      'Run "npm install" inside the bridge dir.\n'
    );
    return;
  }
  const { default: makeWASocket, useMultiFileAuthState, DisconnectReason } = baileys;

  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  sock = makeWASocket({
    auth: state,
    printQRInTerminal: false,
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      lastQr = qr;
      // The Python supervisor scrapes lines starting with "QR:".
      process.stdout.write(`QR: ${qr}\n`);
    }
    if (connection === 'open') {
      baileysReady = true;
      process.stdout.write('READY\n');
    } else if (connection === 'close') {
      baileysReady = false;
      const status = lastDisconnect && lastDisconnect.error &&
        lastDisconnect.error.output && lastDisconnect.error.output.statusCode;
      if (status !== DisconnectReason.loggedOut) {
        // Reconnect after a short delay.
        setTimeout(() => startBaileys().catch(() => {}), 2000);
      }
    }
  });

  sock.ev.on('messages.upsert', (m) => {
    if (!m || !m.messages) return;
    for (const msg of m.messages) {
      if (!msg || !msg.message) continue;
      const text =
        msg.message.conversation ||
        (msg.message.extendedTextMessage && msg.message.extendedTextMessage.text) ||
        '';
      const env = {
        id: msg.key.id,
        chat: msg.key.remoteJid,
        sender: msg.participant || msg.key.remoteJid,
        fromMe: !!msg.key.fromMe,
        text,
        timestamp: (msg.messageTimestamp ? Number(msg.messageTimestamp) : Date.now() / 1000),
      };
      _enqueueInbound(env);
    }
  });
}

// ---------- HTTP plane ----------

function _readJson(req) {
  return new Promise((resolve, reject) => {
    let buf = '';
    req.on('data', (chunk) => { buf += chunk; });
    req.on('end', () => {
      if (!buf) return resolve({});
      try { resolve(JSON.parse(buf)); } catch (e) { reject(e); }
    });
    req.on('error', reject);
  });
}

function _drain(since, timeoutMs) {
  // Synchronous drain (fast path).
  const out = [];
  let take = false;
  for (const env of INBOUND) {
    if (!since || take) { out.push(env); continue; }
    if (env.id === since) { take = true; }
  }
  if (out.length) {
    // Remove drained envelopes from the queue.
    INBOUND.splice(0, INBOUND.length);
    return Promise.resolve(out);
  }
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      if (INBOUND.length || Date.now() - start >= timeoutMs) {
        const drained = INBOUND.splice(0, INBOUND.length);
        return resolve(drained);
      }
      setTimeout(tick, 200);
    };
    tick();
  });
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${HOST}:${PORT}`);
    if (req.method === 'GET' && url.pathname === '/health') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, ready: baileysReady }));
      return;
    }
    if (req.method === 'GET' && url.pathname === '/messages') {
      const since = url.searchParams.get('since');
      const timeout = parseInt(url.searchParams.get('timeout') || '25', 10);
      const drained = await _drain(since, Math.min(timeout * 1000, 30000));
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify(drained));
      return;
    }
    if (req.method === 'POST' && url.pathname === '/send') {
      const body = await _readJson(req);
      const to = String(body.to || '').trim();
      const text = String(body.text || '');
      if (!to || !text) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: 'to + text required' }));
        return;
      }
      const id = _genId();
      recentlySentIds.add(id);
      _trimRecent();
      if (sock && baileysReady) {
        try {
          await sock.sendMessage(to, { text });
        } catch (e) {
          res.writeHead(502, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: String(e && e.message || e) }));
          return;
        }
      }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ id }));
      return;
    }
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'not found' }));
  } catch (e) {
    res.writeHead(500, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: String(e && e.message || e) }));
  }
});

server.listen(PORT, HOST, () => {
  process.stdout.write(`whatsapp-bridge: http listening on http://${HOST}:${PORT}\n`);
  startBaileys().catch((e) => {
    process.stderr.write(`whatsapp-bridge: baileys boot failed: ${e}\n`);
  });
});

process.on('SIGTERM', () => { try { server.close(); } catch (_e) {} process.exit(0); });
process.on('SIGINT', () => { try { server.close(); } catch (_e) {} process.exit(0); });
