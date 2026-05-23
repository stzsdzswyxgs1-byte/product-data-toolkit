// Mercari Collector Coordinator v2.0
// Backward-compatible API with old client + new endpoints for dashboard/batch/check.
const express = require('express');
const path = require('path');
const fs = require('fs');
const Database = require('better-sqlite3');

const PORT = process.env.PORT || 3031;
const TIMEOUT_MS = 2 * 60 * 1000;   // 2 min lock timeout — crashed clients release within 2 min
const DATA_DIR = path.join(__dirname, 'data');
fs.mkdirSync(DATA_DIR, { recursive: true });
const DB_PATH = path.join(DATA_DIR, 'coordinator.db');
const LEGACY_JSON = path.join(DATA_DIR, 'shops.json');

const app = express();
app.use(express.json({ limit: '2mb' }));

// ---- DB setup ----
const db = new Database(DB_PATH);
db.pragma('journal_mode = WAL');
db.exec(`
  CREATE TABLE IF NOT EXISTS shops (
    shop_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    locked_by TEXT NOT NULL DEFAULT '',
    locked_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    completed_at INTEGER NOT NULL DEFAULT 0,
    shop_name TEXT NOT NULL DEFAULT '',
    shop_url TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    total_products INTEGER NOT NULL DEFAULT 0,
    collected_products INTEGER NOT NULL DEFAULT 0
  );
  CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    last_seen INTEGER NOT NULL DEFAULT 0,
    total_completed INTEGER NOT NULL DEFAULT 0
  );
  CREATE INDEX IF NOT EXISTS idx_shops_updated ON shops(updated_at);
  CREATE INDEX IF NOT EXISTS idx_shops_status ON shops(status);
  CREATE INDEX IF NOT EXISTS idx_shops_locked_by ON shops(locked_by);
`);

// One-time import from legacy shops.json (if present and DB is empty)
const existingCount = db.prepare('SELECT COUNT(*) AS c FROM shops').get().c;
if (existingCount === 0 && fs.existsSync(LEGACY_JSON)) {
  try {
    const parsed = JSON.parse(fs.readFileSync(LEGACY_JSON, 'utf8'));
    const ins = db.prepare(`
      INSERT OR REPLACE INTO shops
      (shop_id, status, locked_by, locked_at, updated_at, completed_at,
       shop_name, shop_url, note, last_error, total_products, collected_products)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    `);
    let n = 0;
    const tx = db.transaction(() => {
      for (const [id, info] of Object.entries(parsed)) {
        if (!info || typeof info !== 'object') continue;
        ins.run(
          info.shop_id || id,
          info.status || 'failed',
          info.locked_by || '',
          info.locked_at || 0,
          info.updated_at || 0,
          info.completed_at || 0,
          info.shop_name || '',
          info.shop_url || '',
          info.note || '',
          info.last_error || '',
          info.total_products || 0,
          info.collected_products || 0
        );
        n++;
      }
    });
    tx();
    console.log(`[migrate] imported ${n} shops from legacy shops.json`);
    // rename so we don't re-import
    fs.renameSync(LEGACY_JSON, LEGACY_JSON + '.imported.' + Date.now());
  } catch (e) {
    console.error('[migrate] failed:', e.message);
  }
}

const validStatuses = new Set(['collecting', 'completed', 'failed', 'failed_timeout', 'cancelled']);
const retryableStatuses = new Set(['failed', 'failed_timeout', 'cancelled']);

// Admin token — required for destructive endpoints (delete/reset/force-release).
// Set via env var ADMIN_TOKEN on systemd unit, or hardcode here (less secure).
const ADMIN_TOKEN = process.env.ADMIN_TOKEN || 'CHANGE_ME_admin_default';
function requireAdmin(req, res, next) {
  const token = req.get('X-Admin-Token') || req.body?.admin_key;
  if (token !== ADMIN_TOKEN) {
    return res.status(403).json({ ok: false, error: '需要管理員 token (X-Admin-Token header 或 body.admin_key)' });
  }
  next();
}

function cleanupExpiredLocks() {
  const now = Date.now();
  const expired = db.prepare(
    "SELECT shop_id FROM shops WHERE status = 'collecting' AND ? - updated_at > ?"
  ).all(now, TIMEOUT_MS);
  if (!expired.length) return 0;
  const upd = db.prepare(
    "UPDATE shops SET status = 'failed_timeout', note = ?, last_error = ?, updated_at = ? WHERE shop_id = ?"
  );
  const tx = db.transaction(() => {
    for (const r of expired) upd.run('heartbeat timeout', 'heartbeat timeout', now, r.shop_id);
  });
  tx();
  console.log(`[TIMEOUT] released ${expired.length} stale locks`);
  return expired.length;
}

function touchClient(clientId) {
  if (!clientId) return;
  db.prepare(
    `INSERT INTO clients (client_id, last_seen) VALUES (?, ?)
     ON CONFLICT(client_id) DO UPDATE SET last_seen = excluded.last_seen`
  ).run(clientId, Date.now());
}

// ---- Logging ----
app.use((req, _res, next) => {
  if (req.path !== '/api/events' && !req.path.startsWith('/static/')) {
    console.log(`[${new Date().toISOString()}] ${req.method} ${req.path}`);
  }
  next();
});

// ---- LEGACY-COMPATIBLE API (existing clients depend on this) ----
app.post('/api/lock', (req, res) => {
  cleanupExpiredLocks();
  const { shop_id, client_id, shop_name, shop_url } = req.body || {};
  if (!shop_id || !client_id) return res.status(400).json({ success: false, error: '缺少参数' });
  touchClient(client_id);

  const existing = db.prepare('SELECT * FROM shops WHERE shop_id = ?').get(shop_id);
  const now = Date.now();

  if (!existing) {
    db.prepare(`
      INSERT INTO shops (shop_id, status, locked_by, locked_at, updated_at, shop_name, shop_url)
      VALUES (?, 'collecting', ?, ?, ?, ?, ?)
    `).run(shop_id, client_id, now, now, shop_name || '', shop_url || '');
    return res.json({ success: true, status: 'collecting' });
  }

  if (existing.status === 'completed') {
    return res.json({
      success: false, reason: 'completed',
      locked_by: existing.locked_by, completed_at: existing.completed_at
    });
  }

  if (existing.status === 'collecting') {
    if (now - existing.updated_at > TIMEOUT_MS) {
      db.prepare(`
        UPDATE shops SET status='collecting', locked_by=?, locked_at=?, updated_at=?,
        completed_at=0, note='', last_error='', shop_name=COALESCE(NULLIF(?, ''), shop_name),
        shop_url=COALESCE(NULLIF(?, ''), shop_url) WHERE shop_id=?
      `).run(client_id, now, now, shop_name || '', shop_url || '', shop_id);
      return res.json({ success: true, status: 'collecting' });
    }
    if (existing.locked_by === client_id) {
      db.prepare('UPDATE shops SET updated_at = ? WHERE shop_id = ?').run(now, shop_id);
      return res.json({ success: true, status: 'collecting' });
    }
    return res.json({
      success: false, reason: 'collecting',
      locked_by: existing.locked_by, locked_at: existing.locked_at
    });
  }

  if (retryableStatuses.has(existing.status)) {
    db.prepare(`
      UPDATE shops SET status='collecting', locked_by=?, locked_at=?, updated_at=?,
      completed_at=0, note='', last_error='',
      shop_name=COALESCE(NULLIF(?, ''), shop_name),
      shop_url=COALESCE(NULLIF(?, ''), shop_url) WHERE shop_id=?
    `).run(client_id, now, now, shop_name || '', shop_url || '', shop_id);
    return res.json({ success: true, status: 'collecting' });
  }

  res.json({ success: false, reason: 'unknown_status' });
});

app.post('/api/complete', (req, res) => {
  const { shop_id, client_id, total_products, collected_products } = req.body || {};
  if (!shop_id || !client_id) return res.status(400).json({ success: false, error: '缺少参数' });
  touchClient(client_id);
  const existing = db.prepare('SELECT * FROM shops WHERE shop_id = ?').get(shop_id);
  if (!existing || existing.locked_by !== client_id) return res.json({ success: false, error: '无权限' });
  const now = Date.now();
  db.prepare(`
    UPDATE shops SET status='completed', completed_at=?, updated_at=?,
    total_products=COALESCE(?, total_products),
    collected_products=COALESCE(?, collected_products) WHERE shop_id=?
  `).run(now, now, total_products ?? null, collected_products ?? null, shop_id);
  db.prepare(`
    UPDATE clients SET total_completed = total_completed + 1 WHERE client_id = ?
  `).run(client_id);
  res.json({ success: true });
});

app.post('/api/cancel', (req, res) => {
  const { shop_id, client_id, status, note } = req.body || {};
  if (!shop_id || !client_id || !status) return res.status(400).json({ success: false, error: '缺少参数' });
  touchClient(client_id);
  const existing = db.prepare('SELECT * FROM shops WHERE shop_id = ?').get(shop_id);
  if (!existing || existing.locked_by !== client_id) return res.json({ success: false, error: '无权限' });
  db.prepare(`
    UPDATE shops SET status=?, note=?, last_error=?, updated_at=? WHERE shop_id=?
  `).run(status, note || '', note || '', Date.now(), shop_id);
  res.json({ success: true });
});

app.post('/api/heartbeat', (req, res) => {
  const { shop_id, client_id, total_products, collected_products } = req.body || {};
  if (!shop_id || !client_id) return res.status(400).json({ success: false, error: '缺少参数' });
  touchClient(client_id);
  const existing = db.prepare('SELECT * FROM shops WHERE shop_id = ?').get(shop_id);
  if (!existing || existing.locked_by !== client_id || existing.status !== 'collecting') {
    return res.json({ success: false, error: '无效心跳' });
  }
  db.prepare(`
    UPDATE shops SET updated_at=?,
    total_products=COALESCE(?, total_products),
    collected_products=COALESCE(?, collected_products) WHERE shop_id=?
  `).run(Date.now(), total_products ?? null, collected_products ?? null, shop_id);
  res.json({ success: true });
});

app.get('/api/status/:shop_id', (req, res) => {
  cleanupExpiredLocks();
  const shop = db.prepare('SELECT * FROM shops WHERE shop_id = ?').get(req.params.shop_id);
  if (!shop) return res.json({ exists: false });
  res.json({ exists: true, ...shop });
});

app.get('/api/list', (_req, res) => {
  cleanupExpiredLocks();
  const list = db.prepare('SELECT * FROM shops ORDER BY updated_at DESC').all();
  res.json({ shops: list });
});

app.post('/api/reset/:shop_id', requireAdmin, (req, res) => {
  db.prepare('DELETE FROM shops WHERE shop_id = ?').run(req.params.shop_id);
  res.json({ success: true });
});

// ---- NEW v2 ENDPOINTS ----

// Pre-check: client calls this BEFORE locking, to show "already collected by X" notice
// Returns whether the shop has been seen, by whom, and when.
app.get('/api/check', (req, res) => {
  cleanupExpiredLocks();
  const shopId = req.query.shop_id;
  if (!shopId) return res.status(400).json({ error: 'missing shop_id' });
  const shop = db.prepare('SELECT * FROM shops WHERE shop_id = ?').get(shopId);
  if (!shop) return res.json({ exists: false });
  res.json({
    exists: true,
    shop_id: shop.shop_id,
    status: shop.status,
    locked_by: shop.locked_by,
    completed_at: shop.completed_at,
    locked_at: shop.locked_at,
    updated_at: shop.updated_at,
    total_products: shop.total_products,
    collected_products: shop.collected_products,
  });
});

// Batch check: client posts an array of shop_ids, gets status for each
app.post('/api/check-batch', (req, res) => {
  cleanupExpiredLocks();
  const shopIds = Array.isArray(req.body?.shop_ids) ? req.body.shop_ids : [];
  if (!shopIds.length) return res.json({ results: [] });
  const placeholders = shopIds.map(() => '?').join(',');
  const rows = db.prepare(
    `SELECT shop_id, status, locked_by, completed_at, locked_at, updated_at, total_products
     FROM shops WHERE shop_id IN (${placeholders})`
  ).all(...shopIds);
  const map = Object.fromEntries(rows.map(r => [r.shop_id, r]));
  const results = shopIds.map(id => ({ shop_id: id, ...(map[id] || { status: 'new' }) }));
  res.json({ results });
});

// Batch claim: client wants to claim multiple shops in one shot.
// Returns which were claimed and which were rejected (with reason).
app.post('/api/claim-batch', (req, res) => {
  cleanupExpiredLocks();
  const { client_id, shops: shopList } = req.body || {};
  if (!client_id || !Array.isArray(shopList)) {
    return res.status(400).json({ error: 'need client_id and shops[]' });
  }
  touchClient(client_id);

  const results = [];
  const claimed = [];
  const now = Date.now();

  const sel = db.prepare('SELECT * FROM shops WHERE shop_id = ?');
  const ins = db.prepare(`
    INSERT INTO shops (shop_id, status, locked_by, locked_at, updated_at, shop_name, shop_url)
    VALUES (?, 'collecting', ?, ?, ?, ?, ?)
  `);
  const upd = db.prepare(`
    UPDATE shops SET status='collecting', locked_by=?, locked_at=?, updated_at=?,
    completed_at=0, note='', last_error='',
    shop_name=COALESCE(NULLIF(?, ''), shop_name),
    shop_url=COALESCE(NULLIF(?, ''), shop_url) WHERE shop_id=?
  `);

  const tx = db.transaction(() => {
    for (const item of shopList) {
      const sid = item && item.shop_id;
      if (!sid) { results.push({ shop_id: '', ok: false, reason: 'bad_input' }); continue; }
      const existing = sel.get(sid);
      if (!existing) {
        ins.run(sid, client_id, now, now, item.shop_name || '', item.shop_url || '');
        results.push({ shop_id: sid, ok: true, status: 'claimed' });
        claimed.push(sid);
        continue;
      }
      if (existing.status === 'completed') {
        results.push({
          shop_id: sid, ok: false, reason: 'completed',
          locked_by: existing.locked_by, completed_at: existing.completed_at
        });
        continue;
      }
      if (existing.status === 'collecting') {
        if (existing.locked_by === client_id) {
          results.push({ shop_id: sid, ok: true, status: 'already_yours' });
          continue;
        }
        results.push({
          shop_id: sid, ok: false, reason: 'in_progress',
          locked_by: existing.locked_by, locked_at: existing.locked_at
        });
        continue;
      }
      if (retryableStatuses.has(existing.status)) {
        upd.run(client_id, now, now, item.shop_name || '', item.shop_url || '', sid);
        results.push({ shop_id: sid, ok: true, status: 'reclaimed' });
        claimed.push(sid);
      } else {
        results.push({ shop_id: sid, ok: false, reason: 'unknown_status' });
      }
    }
  });
  tx();
  res.json({ ok: true, claimed: claimed.length, total: shopList.length, results });
});

// Dashboard: summary stats
app.get('/api/summary', (_req, res) => {
  cleanupExpiredLocks();
  const stats = db.prepare(`
    SELECT status, COUNT(*) AS n FROM shops GROUP BY status
  `).all();
  const clients = db.prepare(`
    SELECT client_id, last_seen, total_completed FROM clients ORDER BY last_seen DESC
  `).all();
  const totals = db.prepare(`
    SELECT COUNT(*) AS shops, COALESCE(SUM(collected_products), 0) AS products FROM shops
  `).get();
  const active = db.prepare(`
    SELECT * FROM shops WHERE status = 'collecting' ORDER BY locked_at ASC
  `).all();
  res.json({ stats, clients, totals, active, server_time: Date.now() });
});

// Admin: force-release a stale lock (visible button on dashboard)
app.post('/api/force-release', requireAdmin, (req, res) => {
  const shopId = req.body?.shop_id;
  if (!shopId) return res.status(400).json({ error: 'missing shop_id' });
  const existing = db.prepare('SELECT * FROM shops WHERE shop_id = ?').get(shopId);
  if (!existing) return res.status(404).json({ error: 'not found' });
  db.prepare(`
    UPDATE shops SET status='cancelled', note='force-released', last_error='force-released',
    updated_at=? WHERE shop_id=?
  `).run(Date.now(), shopId);
  res.json({ success: true });
});

// Admin: delete a record entirely
app.post('/api/delete', requireAdmin, (req, res) => {
  const shopId = req.body?.shop_id;
  if (!shopId) return res.status(400).json({ error: 'missing shop_id' });
  db.prepare('DELETE FROM shops WHERE shop_id = ?').run(shopId);
  res.json({ success: true });
});

// Admin: delete a team member from clients table (cleanup test/stale entries)
app.post('/api/delete-client', requireAdmin, (req, res) => {
  const clientId = req.body?.client_id;
  if (!clientId) return res.status(400).json({ error: 'missing client_id' });
  const r = db.prepare('DELETE FROM clients WHERE client_id = ?').run(clientId);
  res.json({ success: true, changes: r.changes });
});

// Health for monitoring
app.get('/health', (_req, res) => res.json({ ok: true, time: Date.now() }));

// Tampermonkey userscript hosting — clients auto-update from here
const TAMPERMONKEY_PATH = path.join(__dirname, 'tampermonkey.user.js');
app.get('/tampermonkey.user.js', (_req, res) => {
  res.setHeader('Content-Type', 'application/javascript; charset=utf-8');
  res.setHeader('Cache-Control', 'no-cache');
  try {
    res.end(fs.readFileSync(TAMPERMONKEY_PATH, 'utf8'));
  } catch (e) {
    res.status(404).end('// userscript not found on server');
  }
});
app.get('/install.user.js', (_req, res) => res.redirect('/tampermonkey.user.js'));

// ---- Dashboard HTML ----
app.get('/', (_req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(DASHBOARD_HTML);
});

// ---- Boot ----
cleanupExpiredLocks();
setInterval(cleanupExpiredLocks, 30 * 1000);

app.listen(PORT, '0.0.0.0', () => {
  console.log(`[coordinator v2] listening on :${PORT}`);
  console.log(`[coordinator v2] db: ${DB_PATH}`);
  console.log(`[coordinator v2] lock timeout: ${TIMEOUT_MS / 1000}s`);
});

process.on('SIGINT', () => { db.close(); process.exit(0); });
process.on('SIGTERM', () => { db.close(); process.exit(0); });

// ---- Dashboard HTML (embedded so single-file deploy) ----
const DASHBOARD_HTML = `<!doctype html>
<html lang="zh-Hant"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>煤爐采集 - 團隊看板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0a;--card:#141414;--border:#222;--text:#e8e8e8;--dim:#888;--accent:#3b82f6;--green:#22c55e;--red:#ef4444;--orange:#f59e0b;--yellow:#eab308}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans TC',sans-serif;background:var(--bg);color:var(--text);line-height:1.5}
.container{max-width:1300px;margin:0 auto;padding:20px}
h1{font-size:22px;font-weight:600;margin-bottom:4px}
h1 span{font-size:13px;color:var(--dim);font-weight:400;margin-left:8px}
.subtitle{color:var(--dim);font-size:13px;margin-bottom:20px}
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:20px}
.tab{padding:10px 18px;background:none;border:none;color:var(--dim);cursor:pointer;font-size:14px;border-bottom:2px solid transparent}
.tab.active{color:var(--text);border-bottom-color:var(--accent)}
.tab:hover{color:var(--text)}

.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat-label{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.stat-value{font-size:26px;font-weight:600;margin-top:6px}
.stat-value.green{color:var(--green)} .stat-value.orange{color:var(--orange)} .stat-value.red{color:var(--red)} .stat-value.blue{color:var(--accent)}

.section-title{font-size:14px;color:var(--dim);margin:18px 0 8px;font-weight:500;text-transform:uppercase;letter-spacing:.5px}
.toolbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.toolbar input{background:#0d0d0d;border:1px solid var(--border);color:var(--text);padding:7px 12px;border-radius:6px;font-size:13px;width:280px}
.toolbar select{background:#0d0d0d;border:1px solid var(--border);color:var(--text);padding:7px 10px;border-radius:6px;font-size:13px}
.toolbar button{padding:7px 14px;background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:6px;cursor:pointer;font-size:13px}
.toolbar button:hover{background:#1e1e1e}

table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border);border-radius:8px;overflow:hidden}
th{text-align:left;padding:10px 14px;background:#0d0d0d;color:var(--dim);font-size:12px;font-weight:500;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid var(--border)}
td{padding:10px 14px;border-top:1px solid var(--border);font-size:13px}
tr:hover td{background:#1a1a1a}
.shop-id{font-family:Menlo,Consolas,monospace;color:var(--accent)}
.shop-id a{color:inherit;text-decoration:none}
.shop-id a:hover{text-decoration:underline}
.status{font-size:11px;padding:3px 8px;border-radius:99px;display:inline-block;font-weight:500}
.status.collecting{background:#1e3a5f;color:#60a5fa}
.status.completed{background:#14532d;color:var(--green)}
.status.failed,.status.failed_timeout{background:#451a03;color:var(--red)}
.status.cancelled{background:#422006;color:var(--orange)}
.btn-x{background:none;border:none;color:var(--red);cursor:pointer;font-size:12px}
.btn-x:hover{text-decoration:underline}

.empty{text-align:center;padding:40px;color:var(--dim);font-size:14px}
.refresh-info{font-size:12px;color:var(--dim);margin-left:auto}
.client-badge{display:inline-block;padding:2px 8px;border-radius:99px;background:#1f1f1f;color:var(--text);font-size:11px;font-family:Menlo,monospace}
.progress{display:inline-block;min-width:80px;font-size:12px;color:var(--dim)}
.time-ago{color:var(--dim);font-size:12px}
</style>
</head><body>
<div class="container">
  <h1>煤爐采集 · 團隊看板 <span id="serverTime"></span></h1>
  <div class="subtitle">實時顯示誰在採什麼、歷史採集記錄、團隊成員活動。每 10 秒自動刷新。</div>

  <div class="tabs">
    <button class="tab active" data-tab="active" onclick="switchTab('active')">⚡ 正在採集</button>
    <button class="tab" data-tab="all" onclick="switchTab('all')">📋 全部歷史</button>
    <button class="tab" data-tab="clients" onclick="switchTab('clients')">👥 團隊成員</button>
  </div>

  <div class="cards" id="cards"></div>

  <div id="tab-active" class="tab-content">
    <div class="section-title">正在採集 (<span id="activeCount">0</span>)</div>
    <div id="activeList"></div>
  </div>

  <div id="tab-all" class="tab-content" style="display:none">
    <div class="toolbar">
      <input id="search" placeholder="搜索店鋪 ID 或採集者..." oninput="renderAll()"/>
      <select id="filterStatus" onchange="renderAll()">
        <option value="">所有狀態</option>
        <option value="collecting">採集中</option>
        <option value="completed">已完成</option>
        <option value="failed">失敗</option>
        <option value="failed_timeout">超時</option>
        <option value="cancelled">已取消</option>
      </select>
      <select id="filterClient" onchange="renderAll()">
        <option value="">所有採集者</option>
      </select>
      <span class="refresh-info" id="allCount">0 條</span>
    </div>
    <div id="allList"></div>
  </div>

  <div id="tab-clients" class="tab-content" style="display:none">
    <div class="section-title">團隊成員 (<span id="clientCount">0</span>)</div>
    <div id="clientsList"></div>
  </div>
</div>

<script>
let data = { shops: [], summary: null };
let activeTab = 'active';

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts);
  return d.toLocaleString('zh-TW', { hour12: false });
}
function fmtAgo(ts) {
  if (!ts) return '-';
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return sec + '秒前';
  if (sec < 3600) return Math.floor(sec / 60) + '分鐘前';
  if (sec < 86400) return Math.floor(sec / 3600) + '小時前';
  return Math.floor(sec / 86400) + '天前';
}
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function shopUrl(s) {
  return s.shop_url || \`https://jp.mercari.com/user/profile/\${s.shop_id}\`;
}

async function refresh() {
  try {
    const [list, summary] = await Promise.all([
      fetch('/api/list').then(r => r.json()),
      fetch('/api/summary').then(r => r.json())
    ]);
    data.shops = list.shops || [];
    data.summary = summary;
    render();
    document.getElementById('serverTime').textContent = '· ' + fmtTime(summary.server_time);
  } catch (e) {
    console.error('refresh failed', e);
  }
}

function render() {
  renderCards();
  if (activeTab === 'active') renderActive();
  if (activeTab === 'all') { rebuildClientFilter(); renderAll(); }
  if (activeTab === 'clients') renderClients();
}

function renderCards() {
  const s = data.summary;
  if (!s) return;
  const m = Object.fromEntries((s.stats || []).map(x => [x.status, x.n]));
  document.getElementById('cards').innerHTML = \`
    <div class="stat-card"><div class="stat-label">採集中</div><div class="stat-value blue">\${m.collecting || 0}</div></div>
    <div class="stat-card"><div class="stat-label">已完成</div><div class="stat-value green">\${m.completed || 0}</div></div>
    <div class="stat-card"><div class="stat-label">失敗</div><div class="stat-value red">\${(m.failed || 0) + (m.failed_timeout || 0)}</div></div>
    <div class="stat-card"><div class="stat-label">已取消</div><div class="stat-value orange">\${m.cancelled || 0}</div></div>
    <div class="stat-card"><div class="stat-label">總店鋪數</div><div class="stat-value">\${s.totals.shops}</div></div>
    <div class="stat-card"><div class="stat-label">已採商品</div><div class="stat-value">\${(s.totals.products || 0).toLocaleString()}</div></div>
    <div class="stat-card"><div class="stat-label">活躍成員</div><div class="stat-value">\${(s.clients || []).filter(c => Date.now() - c.last_seen < 10*60*1000).length}</div></div>
  \`;
}

function renderActive() {
  const active = (data.summary && data.summary.active) || [];
  document.getElementById('activeCount').textContent = active.length;
  if (!active.length) {
    document.getElementById('activeList').innerHTML = '<div class="empty">目前沒有人在採集</div>';
    return;
  }
  let html = '<table><thead><tr><th>店鋪 ID</th><th>採集者</th><th>進度</th><th>開始於</th><th>最近心跳</th><th>操作</th></tr></thead><tbody>';
  for (const s of active) {
    const pct = s.total_products > 0 ? Math.round(s.collected_products / s.total_products * 100) : 0;
    html += \`<tr>
      <td class="shop-id"><a href="\${esc(shopUrl(s))}" target="_blank">\${esc(s.shop_id)}</a></td>
      <td><span class="client-badge">\${esc(s.locked_by)}</span></td>
      <td class="progress">\${s.collected_products}/\${s.total_products || '?'} (\${pct}%)</td>
      <td class="time-ago">\${fmtAgo(s.locked_at)}</td>
      <td class="time-ago">\${fmtAgo(s.updated_at)}</td>
      <td><button class="btn-x" onclick="forceRelease('\${esc(s.shop_id)}')">強制釋放</button></td>
    </tr>\`;
  }
  html += '</tbody></table>';
  document.getElementById('activeList').innerHTML = html;
}

function rebuildClientFilter() {
  const sel = document.getElementById('filterClient');
  const cur = sel.value;
  const ids = [...new Set(data.shops.map(s => s.locked_by).filter(Boolean))].sort();
  sel.innerHTML = '<option value="">所有採集者</option>' + ids.map(id => \`<option value="\${esc(id)}">\${esc(id)}</option>\`).join('');
  if (ids.includes(cur)) sel.value = cur;
}

function renderAll() {
  const q = document.getElementById('search').value.trim().toLowerCase();
  const statusFilter = document.getElementById('filterStatus').value;
  const clientFilter = document.getElementById('filterClient').value;

  let list = data.shops;
  if (statusFilter) list = list.filter(s => s.status === statusFilter);
  if (clientFilter) list = list.filter(s => s.locked_by === clientFilter);
  if (q) list = list.filter(s =>
    s.shop_id.toLowerCase().includes(q) || (s.locked_by || '').toLowerCase().includes(q)
  );

  document.getElementById('allCount').textContent = list.length + ' 條';

  if (!list.length) {
    document.getElementById('allList').innerHTML = '<div class="empty">無記錄</div>';
    return;
  }

  let html = '<table><thead><tr><th>店鋪 ID</th><th>狀態</th><th>採集者</th><th>商品數</th><th>完成時間</th><th>操作</th></tr></thead><tbody>';
  for (const s of list.slice(0, 500)) {
    html += \`<tr>
      <td class="shop-id"><a href="\${esc(shopUrl(s))}" target="_blank">\${esc(s.shop_id)}</a></td>
      <td><span class="status \${esc(s.status)}">\${statusText(s.status)}</span></td>
      <td><span class="client-badge">\${esc(s.locked_by)}</span></td>
      <td>\${s.collected_products || 0}\${s.total_products ? ' / ' + s.total_products : ''}</td>
      <td class="time-ago">\${s.completed_at ? fmtAgo(s.completed_at) : fmtAgo(s.updated_at)}</td>
      <td>
        \${s.status === 'collecting' ? \`<button class="btn-x" onclick="forceRelease('\${esc(s.shop_id)}')">強制釋放</button> \` : ''}
        <button class="btn-x" onclick="del('\${esc(s.shop_id)}')">刪除</button>
      </td>
    </tr>\`;
  }
  html += '</tbody></table>';
  if (list.length > 500) html += '<div class="empty" style="padding:12px">顯示前 500 條,共 ' + list.length + ' 條 — 請用搜索/篩選</div>';
  document.getElementById('allList').innerHTML = html;
}

function renderClients() {
  const clients = (data.summary && data.summary.clients) || [];
  document.getElementById('clientCount').textContent = clients.length;
  if (!clients.length) {
    document.getElementById('clientsList').innerHTML = '<div class="empty">尚無成員記錄</div>';
    return;
  }
  let html = '<table><thead><tr><th>成員 ID</th><th>累計完成</th><th>最後在線</th><th>操作</th></tr></thead><tbody>';
  for (const c of clients) {
    const online = Date.now() - c.last_seen < 10 * 60 * 1000;
    html += \`<tr>
      <td><span class="client-badge">\${esc(c.client_id)}</span> \${online ? '🟢' : ''}</td>
      <td>\${c.total_completed} 個店鋪</td>
      <td class="time-ago">\${fmtAgo(c.last_seen)}</td>
      <td>\${online ? '' : '<button class="btn-x" onclick="delClient(\\''+esc(c.client_id)+'\\')">刪除</button>'}</td>
    </tr>\`;
  }
  html += '</tbody></table>';
  document.getElementById('clientsList').innerHTML = html;
}

async function delClient(clientId) {
  if (!confirm('確定刪除成員「' + clientId + '」?只是從看板隱藏,不影響採集記錄。')) return;
  if (await adminFetch('/api/delete-client', { client_id: clientId })) refresh();
}

function statusText(s) {
  return { collecting:'採集中', completed:'已完成', failed:'失敗', failed_timeout:'超時', cancelled:'已取消' }[s] || s;
}

function switchTab(t) {
  activeTab = t;
  for (const b of document.querySelectorAll('.tab')) b.classList.toggle('active', b.dataset.tab === t);
  for (const c of document.querySelectorAll('.tab-content')) c.style.display = 'none';
  document.getElementById('tab-' + t).style.display = 'block';
  render();
}

function getAdminToken() {
  let t = localStorage.getItem('adminToken');
  if (!t) {
    t = prompt('需要管理員 token (從服務器管理員處獲取)\\n會保存在瀏覽器,下次不再詢問');
    if (!t) return null;
    localStorage.setItem('adminToken', t.trim());
  }
  return localStorage.getItem('adminToken');
}

async function adminFetch(url, body) {
  const token = getAdminToken();
  if (!token) return null;
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Admin-Token': token },
    body: JSON.stringify(body)
  });
  if (r.status === 403) {
    localStorage.removeItem('adminToken');
    alert('Token 錯誤,已清除。請重試輸入');
    return null;
  }
  return r.json();
}

async function forceRelease(shopId) {
  if (!confirm('確定強制釋放此鎖?採集者下次心跳會失敗。')) return;
  if (await adminFetch('/api/force-release', { shop_id: shopId })) refresh();
}

async function del(shopId) {
  if (!confirm('確定刪除此記錄?歷史會消失。')) return;
  if (await adminFetch('/api/delete', { shop_id: shopId })) refresh();
}

refresh();
setInterval(refresh, 10000);
</script>
</body></html>`;
