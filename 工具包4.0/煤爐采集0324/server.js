// ---- Node version guard ----
const _nodeMajor = Number(process.versions.node.split('.')[0]);
if (_nodeMajor < 18 || _nodeMajor >= 25) {
  console.error(`錯誤：目前僅支援 Node.js 18~24，當前版本：${process.version}`);
  console.error('請安裝相容版本後重新啟動。下載地址：https://nodejs.org/');
  process.exit(1);
}

const express = require('express');
const path = require('path');
const fs = require('fs');
const archiver = require('archiver');
const XLSX = require('xlsx');
const db = require('./lib/db');
const mercari = require('./lib/mercari');
const downloader = require('./lib/downloader');
const coordinator = require('./lib/coordinator');

// ---- 商品簡述映射 ----
const STATE_MAP_ZH_TW = {
  '新品、未使用': '全新，未使用',
  '未使用に近い': '近全新',
  '目立った傷や汚れなし': '無明顯傷痕或污漬',
  'やや傷や汚れあり': '略有傷痕或污漬',
  '傷や汚れあり': '有傷痕或污漬',
  '全体的に状態が悪い': '整體狀況較差',
};

function mapStateToZhTW(state) {
  const s = String(state || '').trim();
  return STATE_MAP_ZH_TW[s] || s;
}

function humanAgo(ts) {
  if (!ts) return '';
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return sec + ' 秒前';
  if (sec < 3600) return Math.floor(sec / 60) + ' 分鐘前';
  if (sec < 86400) return Math.floor(sec / 3600) + ' 小時前';
  return Math.floor(sec / 86400) + ' 天前';
}

const app = express();
const PORT = process.env.PORT || 3030;

app.use(express.json({ limit: '50mb' }));

// ---- Request logging ----
app.use((req, _res, next) => {
  if (req.path !== '/api/events') console.log(`[http] ${req.method} ${req.path}`);
  next();
});

// ---- SSE connections for progress ----
const sseClients = new Set();

function broadcast(data) {
  const msg = `data: ${JSON.stringify(data)}\n\n`;
  for (const res of sseClients) {
    try { res.write(msg); } catch (_) { sseClients.delete(res); }
  }
}

// Throttle shop:progress emit per shop — no more than 1 per 250ms.
// Status changes (done/error/cancelled) always emit immediately.
const _progressLastEmit = new Map();
const _progressPending = new Map();
function emitProgress(payload) {
  const sid = payload.shopId;
  const now = Date.now();
  const last = _progressLastEmit.get(sid) || 0;
  if (now - last >= 250) {
    _progressLastEmit.set(sid, now);
    broadcast(payload);
    return;
  }
  // Coalesce — keep latest payload, schedule emit
  _progressPending.set(sid, payload);
  if (!_progressPending.get('_timer_' + sid)) {
    const wait = 250 - (now - last);
    const t = setTimeout(() => {
      const p = _progressPending.get(sid);
      _progressPending.delete(sid);
      _progressPending.delete('_timer_' + sid);
      if (p) {
        _progressLastEmit.set(sid, Date.now());
        broadcast(p);
      }
    }, wait);
    _progressPending.set('_timer_' + sid, t);
  }
}

app.get('/api/events', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.flushHeaders();
  sseClients.add(res);
  req.on('close', () => sseClients.delete(res));
});

// ---- Config ----
app.get('/api/config', (_req, res) => {
  const cfg = db.getAllConfig();
  cfg.imagesDir = downloader.getImagesDir();
  cfg.defaultImagesDir = downloader.DEFAULT_IMAGES_DIR;
  res.json(cfg);
});

app.post('/api/config', (req, res) => {
  const body = req.body || {};
  if (body.imageBase !== undefined) {
    db.setConfig('imageBase', body.imageBase);
    downloader.setImagesDir(body.imageBase);
  }
  if (body.useProxy !== undefined) db.setConfig('useProxy', body.useProxy ? '1' : '0');
  res.json({ ok: true, config: db.getAllConfig() });
});

app.post('/api/coordinator/config', (req, res) => {
  const { clientId } = req.body || {};
  db.setConfig('coordinatorEnabled', '1');
  db.setConfig('coordinatorUrl', 'http://<RELAY_IP_REDACTED>:3031');
  if (clientId !== undefined) db.setConfig('clientId', clientId);
  res.json({ ok: true });
});

// Pre-check a URL before collecting — used by UI to show "已被X採過" warning
app.post('/api/coordinator/precheck', async (req, res) => {
  const { url } = req.body || {};
  if (!url) return res.status(400).json({ error: '缺少 url' });
  let parsed = mercari.parseSellerUrl(url);
  if (!parsed) return res.json({ ok: false, error: '無法識別網址' });

  if (parsed.type === 'product') {
    const useProxy = db.getConfig('useProxy') === '1';
    try {
      const seller = await mercari.resolveProductToSeller(parsed.productId, useProxy);
      if (!seller) return res.json({ ok: false, error: '無法找到賣家' });
      parsed = { sellerId: seller.sellerId, type: seller.type };
    } catch (e) {
      return res.json({ ok: false, error: e.message });
    }
  }

  const result = await coordinator.checkShop(parsed.sellerId);
  res.json({ ok: true, sellerId: parsed.sellerId, type: parsed.type, status: result });
});

// Ping coordinator: UI uses this to show the connection indicator
// Cached for 30s to avoid hitting cloud on every page refresh / SSE reconnect.
let _pingCache = null;
let _pingCacheAt = 0;
let _pingInFlight = null;
app.get('/api/coordinator/ping', async (_req, res) => {
  const cfg = coordinator.getConfig();
  const autoReplaced = db.getConfig('clientId_auto_replaced') === '1';
  const oldId = db.getConfig('clientId_old');
  const meta = { clientId: cfg.clientId, enabled: cfg.enabled, url: cfg.url, autoReplaced, oldId };

  const now = Date.now();
  // Use cache if fresh (30s)
  if (_pingCache && now - _pingCacheAt < 30000) {
    return res.json({ ..._pingCache, ...meta, cached: true });
  }
  // De-dupe concurrent fetches
  if (!_pingInFlight) {
    _pingInFlight = coordinator.pingCoordinator().then(r => {
      _pingCache = r;
      _pingCacheAt = Date.now();
      _pingInFlight = null;
      return r;
    }).catch(e => {
      _pingInFlight = null;
      return { online: false, reason: e.message };
    });
  }
  const r = await _pingInFlight;
  res.json({ ...r, ...meta });
});

// Dismiss the auto-heal banner
app.post('/api/coordinator/ack-replaced', (_req, res) => {
  db.setConfig('clientId_auto_replaced', '0');
  res.json({ ok: true });
});

// Batch claim: parse URLs, optionally call /api/claim-batch on coordinator
app.post('/api/coordinator/claim-batch', async (req, res) => {
  const { urls } = req.body || {};
  if (!Array.isArray(urls) || !urls.length) return res.status(400).json({ error: '缺少 urls' });
  const useProxy = db.getConfig('useProxy') === '1';
  const shops = [];
  const errors = [];

  for (const url of urls) {
    let parsed = mercari.parseSellerUrl(url);
    if (!parsed) { errors.push({ url, error: '無法識別' }); continue; }
    if (parsed.type === 'product') {
      try {
        const seller = await mercari.resolveProductToSeller(parsed.productId, useProxy);
        if (!seller) { errors.push({ url, error: '找不到賣家' }); continue; }
        parsed = { sellerId: seller.sellerId, type: seller.type };
      } catch (e) {
        errors.push({ url, error: e.message });
        continue;
      }
    }
    const sellerUrl = parsed.type === 'shop'
      ? `https://jp.mercari.com/shops/profile/${parsed.sellerId}`
      : `https://jp.mercari.com/user/profile/${parsed.sellerId}`;
    shops.push({
      shop_id: parsed.sellerId,
      shop_name: parsed.sellerId,
      shop_url: sellerUrl,
      type: parsed.type,
      original_url: url,
    });
  }

  const claimResult = await coordinator.claimBatch(shops);
  res.json({ ok: true, errors, shops, claimResult });
});

// ---- Shops ----
app.get('/api/shops', (_req, res) => res.json(db.listShops()));

app.delete('/api/shops/:id', (req, res) => {
  try {
    const shopId = req.params.id;
    // Remove from pending queue
    const queueIndex = pendingQueue.findIndex(t => t.sellerId === shopId);
    if (queueIndex >= 0) {
      pendingQueue.splice(queueIndex, 1);
      queuedShopIds.delete(shopId);
    }
    // Cancel running job and mark for deletion after it finishes
    const job = activeJobs.get(shopId);
    if (job) {
      job.cancel = true;
      pendingDeletes.add(shopId);
    } else {
      // Not running, delete immediately
      db.deleteShop(shopId);
    }
    res.json({ ok: true });
  } catch (err) {
    console.error('[delete shop]', err);
    res.status(500).json({ ok: false, error: err.message || String(err) });
  }
});

app.post('/api/delete-shops', (req, res) => {
  try {
    const ids = Array.isArray(req.body.ids) ? req.body.ids : [];
    for (const id of ids) {
      // Remove from pending queue
      const queueIndex = pendingQueue.findIndex(t => t.sellerId === id);
      if (queueIndex >= 0) {
        pendingQueue.splice(queueIndex, 1);
        queuedShopIds.delete(id);
      }
      // Cancel running job and mark for deletion after it finishes
      const job = activeJobs.get(id);
      if (job) {
        job.cancel = true;
        pendingDeletes.add(id);
      } else {
        // Not running, delete immediately
        db.deleteShop(id);
      }
    }
    res.json({ ok: true, deleted: ids.length });
  } catch (err) {
    console.error('[delete shops]', err);
    res.status(500).json({ ok: false, error: err.message || String(err) });
  }
});

// ---- Products ----
app.get('/api/products', (req, res) => {
  const shopId = req.query.shop_id;
  const products = shopId ? db.listProducts(shopId) : db.listAllProducts();
  res.json(products);
});

app.post('/api/delete-products', (req, res) => {
  try {
    const ids = req.body.ids || [];
    db.deleteProducts(ids);   // 只删数据库，不删图片
    res.json({ ok: true });
  } catch (err) {
    console.error('[delete products]', err);
    res.status(500).json({ ok: false, error: err.message || String(err) });
  }
});

// ---- Export XLSX ----
app.post('/api/export/csv', (req, res) => {
  const ids = Array.isArray(req.body.ids) ? req.body.ids : [];
  const shopIds = Array.isArray(req.body.shopIds) ? req.body.shopIds : [];

  let products = [];
  if (ids.length) {
    products = db.getProductsByIds(ids);
  } else if (shopIds.length) {
    products = db.listProductsByShopIds(shopIds);
  } else {
    products = db.listAllProducts();
  }
  const imageBase = downloader.getImagesDir();

  let skippedEmpty = 0;
  const rows = [];

  for (const p of products) {
    if (!p.title && !p.price) {
      skippedEmpty++;
      continue;
    }

    const safeTitle = downloader.sanitizeName(p.title);
    const images = JSON.parse(p.images || '[]');
    const filenames = downloader.makeImageFilenames(safeTitle, images.length);
    const imgPaths = filenames.map(n => {
      if (!imageBase) return n;
      const base = imageBase.replace(/[\\/]+$/, '');
      return `${base}\\${safeTitle}\\${n}`;
    }).join('|');

    rows.push({
      '標題': safeTitle,
      '商品簡述': mapStateToZhTW(p.condition),
      '起標價': p.price || '',
      '說明': p.description || '',
      '圖片': imgPaths,
      '連結': p.source_url || '',
      '分類ID': p.category_id || ''
    });
  }

  if (skippedEmpty > 0) console.log(`[xlsx] skipped ${skippedEmpty} empty products`);

  const wb = XLSX.utils.book_new();
  const ws = XLSX.utils.json_to_sheet(rows, {
    header: ['標題', '商品簡述', '起標價', '說明', '圖片', '連結', '分類ID']
  });
  XLSX.utils.book_append_sheet(wb, ws, '商品資料');

  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '').slice(0, 14);
  const buf = XLSX.write(wb, { type: 'buffer', bookType: 'xlsx' });

  res.setHeader('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  res.setHeader('Content-Disposition', `attachment; filename="export_${stamp}.xlsx"`);
  res.send(buf);
});

// ---- Export ZIP ----
app.post('/api/export/zip', (req, res) => {
  const ids = Array.isArray(req.body.ids) ? req.body.ids : [];
  const shopIds = Array.isArray(req.body.shopIds) ? req.body.shopIds : [];

  let products = [];
  if (ids.length) {
    products = db.getProductsByIds(ids);
  } else if (shopIds.length) {
    products = db.listProductsByShopIds(shopIds);
  } else {
    products = db.listAllProducts();
  }

  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '').slice(0, 14);
  res.setHeader('Content-Type', 'application/zip');
  res.setHeader('Content-Disposition', `attachment; filename="images_${stamp}.zip"`);

  // Use store (no compression) for speed + compatibility; level 9 is too slow for large archives
  const archive = archiver('zip', { zlib: { level: 0 }, forceZip64: false, store: true });
  archive.on('error', err => {
    console.error('[zip] archive error:', err.message);
    try { res.status(500).end(err.message); } catch (_) {}
  });
  archive.on('warning', err => {
    console.warn('[zip] archive warning:', err.message);
  });
  archive.pipe(res);

  const added = new Set();
  for (const p of products) {
    const safeTitle = downloader.sanitizeName(p.title);
    if (!safeTitle || safeTitle === 'no-title' || added.has(safeTitle)) continue;
    const dir = path.join(downloader.getImagesDir(), safeTitle);
    if (fs.existsSync(dir)) {
      // Add individual files instead of directory (better encoding control)
      try {
        const files = fs.readdirSync(dir);
        for (const f of files) {
          const fp = path.join(dir, f);
          if (fs.statSync(fp).isFile()) {
            archive.file(fp, { name: `${safeTitle}/${f}` });
          }
        }
        added.add(safeTitle);
      } catch (e) {
        console.error('[zip] read dir error:', dir, e.message);
      }
    }
  }

  console.log(`[zip] archiving ${added.size} folders...`);
  archive.finalize().then(() => {
    console.log(`[zip] finalize complete`);
  }).catch(err => {
    console.error('[zip] finalize error:', err.message);
  });
});

// ---- Collect: start collecting a shop ----
const MAX_ACTIVE_SHOPS = 5;
const activeJobs = new Map(); // shopId → { cancel: false }
const pendingQueue = [];
const queuedShopIds = new Set();
const pendingDeletes = new Set(); // shopIds marked for deletion after job finishes

function enqueueCollectTask(task) {
  const { sellerId } = task;

  if (activeJobs.has(sellerId)) {
    return { ok: false, error: '此店鋪正在處理中' };
  }
  if (queuedShopIds.has(sellerId)) {
    return { ok: false, error: '此店鋪已在等待隊列中' };
  }

  db.upsertShop({ id: sellerId, name: sellerId, url: task.originalUrl, type: task.type });

  if (activeJobs.size < MAX_ACTIVE_SHOPS) {
    startCollectTask(task);
    return { ok: true, status: 'collecting' };
  }

  pendingQueue.push(task);
  queuedShopIds.add(sellerId);
  db.updateShopStatus(sellerId, 'pending', 0, 0);
  broadcast({ type: 'shop:status', shopId: sellerId, status: 'pending' });

  return { ok: true, status: 'pending', queueIndex: pendingQueue.length };
}

function startCollectTask(task) {
  const job = { cancel: false };
  activeJobs.set(task.sellerId, job);

  // 心跳定时器
  const heartbeatInterval = setInterval(() => {
    coordinator.heartbeat(task.sellerId);
  }, 30000);  // 30s — must beat cloud's 2min timeout with margin

  runCollection(task.sellerId, task.type, task.originalUrl, job)
    .catch(err => {
      console.error(`[collect] error for ${task.sellerId}:`, err);
      db.updateShopStatus(task.sellerId, 'error');
      broadcast({ type: 'shop:error', shopId: task.sellerId, error: err.message });
      coordinator.markFailed(task.sellerId, 'failed', err.message);
    })
    .finally(() => {
      clearInterval(heartbeatInterval);
      activeJobs.delete(task.sellerId);
      if (pendingDeletes.has(task.sellerId)) {
        pendingDeletes.delete(task.sellerId);
        db.deleteShop(task.sellerId);
        broadcast({ type: 'shop:deleted', shopId: task.sellerId });
      }
      pumpQueue();
    });
}

function pumpQueue() {
  while (activeJobs.size < MAX_ACTIVE_SHOPS && pendingQueue.length > 0) {
    const task = pendingQueue.shift();
    queuedShopIds.delete(task.sellerId);
    if (task.isRefresh) {
      startRefreshTask(task);
    } else {
      startCollectTask(task);
    }
  }
}

app.post('/api/collect', async (req, res) => {
  const { url, forceRedownload } = req.body || {};
  console.log(`[collect] 收到請求 | url=${url} | force=${forceRedownload}`);
  if (!url) return res.status(400).json({ error: '請輸入賣家網址' });

  let parsed = mercari.parseSellerUrl(url);
  console.log(`[collect] 解析結果:`, parsed);
  if (!parsed) return res.status(400).json({ error: '無法識別網址，請輸入 Mercari 賣家主頁或商品連結' });

  // If it's a product URL, resolve to seller first
  if (parsed.type === 'product') {
    console.log(`[collect] 偵測到商品URL，正在查找賣家...`);
    const useProxy = db.getConfig('useProxy') === '1';
    try {
      const seller = await mercari.resolveProductToSeller(parsed.productId, useProxy);
      if (!seller) return res.status(400).json({ error: '無法從商品頁面找到賣家資訊' });
      console.log(`[collect] 找到賣家: ${seller.sellerId} (${seller.type}) ${seller.productName || ''}`);
      parsed = { sellerId: seller.sellerId, type: seller.type };
    } catch (e) {
      console.error(`[collect] 查找賣家失敗:`, e.message);
      return res.status(400).json({ error: '查找賣家失敗: ' + e.message });
    }
  }

  const { sellerId, type } = parsed;
  const sellerUrl = type === 'shop'
    ? `https://jp.mercari.com/shops/profile/${sellerId}`
    : `https://jp.mercari.com/user/profile/${sellerId}`;

  // 协调器检查（强制重新下载时跳过）
  if (!forceRedownload) {
    const lockResult = await coordinator.requestLock(sellerId, sellerId, sellerUrl);
    console.log('[collect] lockResult:', JSON.stringify(lockResult));
    if (!lockResult.success) {
      // 如果是自己已经下载过，返回特殊状态让前端确认
      if (lockResult.reason === 'self_completed') {
        console.log('[collect] 返回 self_completed 给前端');
        return res.status(409).json({
          error: '您已經下載過此店鋪',
          reason: 'self_completed',
          needConfirm: true,
          completed_at: lockResult.completed_at
        });
      }
      // Build informative error message including who and when
      let msg;
      if (lockResult.error) {
        msg = lockResult.error;
      } else if (lockResult.reason === 'completed') {
        const ago = lockResult.completed_at ? humanAgo(lockResult.completed_at) : '';
        msg = `此店鋪已被「${lockResult.locked_by}」採集過${ago ? '(' + ago + ')' : ''},不會重複下載`;
      } else if (lockResult.reason === 'collecting') {
        const ago = lockResult.locked_at ? humanAgo(lockResult.locked_at) : '';
        msg = `「${lockResult.locked_by}」正在採集此店鋪${ago ? '(' + ago + '開始)' : ''},請等候或選別的賣家`;
      } else {
        msg = `店鋪不可採集 (${lockResult.reason})`;
      }
      return res.status(409).json({
        error: msg,
        reason: lockResult.reason,
        locked_by: lockResult.locked_by,
        locked_at: lockResult.locked_at,
        completed_at: lockResult.completed_at
      });
    }
  }

  const task = { sellerId, type, originalUrl: url.includes('/profile/') ? url : sellerUrl };
  const result = enqueueCollectTask(task);

  if (!result.ok) {
    return res.status(409).json(result);
  }

  res.json({ ok: true, shopId: sellerId, type, status: result.status });
});

app.post('/api/collect/cancel', (req, res) => {
  const { shopId } = req.body || {};
  const job = activeJobs.get(shopId);
  if (job) {
    job.cancel = true;
    broadcast({ type: 'shop:cancelled', shopId });
  }
  // Also remove from pending queue
  const queueIndex = pendingQueue.findIndex(t => t.sellerId === shopId);
  if (queueIndex >= 0) {
    pendingQueue.splice(queueIndex, 1);
    queuedShopIds.delete(shopId);
    db.updateShopStatus(shopId, 'cancelled', 0, 0);
    broadcast({ type: 'shop:status', shopId, status: 'cancelled' });
  }
  res.json({ ok: true });
});

app.post('/api/collect-batch', async (req, res) => {
  const urls = Array.isArray(req.body.urls) ? req.body.urls : [];
  if (!urls.length) return res.status(400).json({ error: '請提供 urls 陣列' });

  const useProxy = db.getConfig('useProxy') === '1';
  const results = [];

  for (const url of urls) {
    try {
      let parsed = mercari.parseSellerUrl(url);
      if (!parsed) {
        results.push({ url, ok: false, error: '無法識別網址' });
        continue;
      }

      if (parsed.type === 'product') {
        const seller = await mercari.resolveProductToSeller(parsed.productId, useProxy);
        if (!seller) {
          results.push({ url, ok: false, error: '無法從商品頁面找到賣家資訊' });
          continue;
        }
        parsed = { sellerId: seller.sellerId, type: seller.type };
      }

      const { sellerId, type } = parsed;
      const sellerUrl = type === 'shop'
        ? `https://jp.mercari.com/shops/profile/${sellerId}`
        : `https://jp.mercari.com/user/profile/${sellerId}`;

      const task = { sellerId, type, originalUrl: url.includes('/profile/') ? url : sellerUrl };
      const result = enqueueCollectTask(task);
      results.push({ url, shopId: sellerId, ...result });
    } catch (err) {
      results.push({ url, ok: false, error: err.message });
    }
  }

  res.json({ ok: true, results });
});

// ---- Refresh: re-fetch details for shop products with incomplete images ----
app.post('/api/refresh-shop', async (req, res) => {
  const { shopId } = req.body || {};
  if (!shopId) return res.status(400).json({ error: 'Missing shopId' });

  if (activeJobs.has(shopId)) {
    return res.status(409).json({ error: '此店鋪正在處理中' });
  }
  if (queuedShopIds.has(shopId)) {
    return res.status(409).json({ error: '此店鋪已在等待隊列中' });
  }

  const shop = db.listShops().find(s => s.id === shopId);
  if (!shop) return res.status(404).json({ error: '店鋪不存在' });

  const task = { sellerId: shopId, type: shop.type, originalUrl: shop.url, isRefresh: true };

  if (activeJobs.size < MAX_ACTIVE_SHOPS) {
    startRefreshTask(task);
    res.json({ ok: true, status: 'refreshing' });
  } else {
    pendingQueue.push(task);
    queuedShopIds.add(shopId);
    db.updateShopStatus(shopId, 'pending', 0, 0);
    broadcast({ type: 'shop:status', shopId, status: 'pending' });
    res.json({ ok: true, status: 'pending' });
  }
});

function startRefreshTask(task) {
  const job = { cancel: false };
  activeJobs.set(task.sellerId, job);

  db.updateShopStatus(task.sellerId, 'collecting', 0, 0);
  broadcast({ type: 'shop:status', shopId: task.sellerId, status: 'collecting' });

  refreshShopProducts(task.sellerId, job)
    .catch(err => {
      console.error(`[refresh] error for ${task.sellerId}:`, err);
      db.updateShopStatus(task.sellerId, 'error');
      broadcast({ type: 'shop:status', shopId: task.sellerId, status: 'error' });
      broadcast({ type: 'shop:log', shopId: task.sellerId, message: `✗ 刷新失敗: ${err.message}` });
    })
    .finally(() => {
      activeJobs.delete(task.sellerId);
      if (pendingDeletes.has(task.sellerId)) {
        pendingDeletes.delete(task.sellerId);
        db.deleteShop(task.sellerId);
        broadcast({ type: 'shop:deleted', shopId: task.sellerId });
      }
      pumpQueue();
    });
}

async function refreshShopProducts(shopId, job) {
  const useProxy = db.getConfig('useProxy') === '1';
  const log = (msg) => {
    console.log(`[refresh:${shopId}] ${msg}`);
    broadcast({ type: 'shop:log', shopId, message: msg });
  };

  const products = db.listProducts(shopId);
  // Find shop products with 0-1 images that need refreshing
  const needRefresh = products.filter(p => {
    if (!p.source_url.includes('/shops/')) return false;
    const imgs = JSON.parse(p.images || '[]');
    return imgs.length <= 1;
  });

  log(`開始刷新 | 共 ${products.length} 件商品，${needRefresh.length} 件需要重新獲取圖片`);

  if (!needRefresh.length) {
    log('無需刷新的商品');
    db.updateShopStatus(shopId, 'done');
    broadcast({ type: 'shop:status', shopId, status: 'done' });
    return;
  }

  let ok = 0, fail = 0;
  for (let i = 0; i < needRefresh.length; i++) {
    if (job.cancel) {
      log('用戶取消');
      db.updateShopStatus(shopId, 'cancelled');
      broadcast({ type: 'shop:status', shopId, status: 'cancelled' });
      return;
    }

    const p = needRefresh[i];
    const productId = p.mercari_id;
    try {
      log(`[${i + 1}/${needRefresh.length}] 重新獲取: ${productId} | ${(p.title || '').slice(0, 30)}`);
      const detail = await mercari.fetchShopProductDetail(productId, useProxy);
      if (detail && detail.images && detail.images.length > 1) {
        // Update DB with new images + description
        db.upsertProduct({
          mercari_id: productId,
          shop_id: shopId,
          title: detail.title || p.title,
          price: detail.price || p.price,
          condition: detail.condition || p.condition,
          description: detail.description || p.description,
          category_id: detail.category_id || p.category_id,
          images: detail.images,
          source_url: detail.source_url || p.source_url,
        });

        // Re-download images
        const localImages = await downloader.downloadProductImages(
          { title: detail.title || p.title, images: detail.images, source_url: detail.source_url },
          useProxy, 5, log
        );
        db.markImagesDownloaded(productId, localImages);

        ok++;
        log(`  ✓ ${detail.images.length} 張圖片`);
      } else {
        log(`  - API 返回 ${(detail?.images || []).length} 張圖，跳過`);
      }
    } catch (e) {
      fail++;
      log(`  ✗ ${e.message}`);
    }

    if (i < needRefresh.length - 1) await mercari.sleep(300);
  }

  log(`刷新完成 | ${ok} 件更新 | ${fail} 件失敗`);
  db.updateShopStatus(shopId, 'done');
  broadcast({ type: 'shop:status', shopId, status: 'done' });
}

async function runCollection(sellerId, type, originalUrl, job) {
  const useProxy = db.getConfig('useProxy') === '1';
  const log = (msg) => {
    console.log(`[${sellerId}] ${msg}`);
    broadcast({ type: 'shop:log', shopId: sellerId, message: msg });
  };

  // Upsert shop record
  db.upsertShop({ id: sellerId, name: sellerId, url: originalUrl, type });
  db.updateShopStatus(sellerId, 'discovering', 0, 0);
  broadcast({ type: 'shop:status', shopId: sellerId, status: 'discovering' });

  log(`開始采集 | 賣家: ${sellerId} | 類型: ${type} | 代理: ${useProxy ? '開啟' : '關閉'}`);

  // Step 1: Discover all products
  const t0 = Date.now();
  const productList = await mercari.fetchSellerProductsFromSearch(
    sellerId, type, useProxy, log
  );

  if (job.cancel) { log('用戶取消'); return; }

  let total = productList.length;
  log(`商品列表獲取完成 | ${total} 件 | 耗時 ${((Date.now() - t0) / 1000).toFixed(1)}s`);

  db.updateShopStatus(sellerId, 'collecting', total, 0);
  broadcast({ type: 'shop:status', shopId: sellerId, status: 'collecting', total, collected: 0 });

  if (total === 0) {
    log('該賣家無在售商品');
    db.updateShopStatus(sellerId, 'done', 0, 0);
    broadcast({ type: 'shop:status', shopId: sellerId, status: 'done', total: 0, collected: 0 });
    return;
  }

  // Step 2: Fetch details + download images (with concurrency)
  const CONCURRENCY = 5;
  const DELAY_MS = 400;
  let collected = 0;
  let skipped = 0;
  // Skip detail API for personal sellers after we detect it's failing for this seller
  // (Mercari's /items/get returns 404 for many personal sellers; search data has everything we need)
  let skipDetailApi = false;
  let fallbackCount = 0;
  let failed = 0;
  let i = 0;
  const t1 = Date.now();

  while (i < productList.length) {
    if (job.cancel) {
      log(`用戶取消 | 已完成 ${collected}/${total}`);
      db.updateShopStatus(sellerId, 'cancelled', total, collected);
      broadcast({ type: 'shop:status', shopId: sellerId, status: 'cancelled', total, collected });
      await coordinator.markFailed(sellerId, 'cancelled', '用户取消');
      return;
    }

    const batch = productList.slice(i, i + CONCURRENCY);
    i += CONCURRENCY;

    const batchSkipped = batch.length;
    const prevSkipped = skipped;

    await Promise.all(batch.map(async (item) => {
      if (job.cancel) return;

      // Skip if already collected
      const existing = db.getProduct(item.mercari_id);
      if (existing && existing.images_downloaded) {
        collected++;
        skipped++;
        db.updateShopStatus(sellerId, 'collecting', total, collected);
        emitProgress({ type: 'shop:progress', shopId: sellerId, total, collected, current: `${item.mercari_id} (已有)` });
        return;
      }

      try {
        let detail;

        if (item.type === 'shop') {
          // Shops products: fetch full detail via Shops API (includes all photos)
          log(`  取得商品詳情（Shops）: ${item.mercari_id}`);
          detail = await mercari.fetchShopProductDetail(item.mercari_id, useProxy);
          if (!detail) {
            // Final fallback: use search data
            detail = {
              title: item.title,
              price: item.price,
              condition: item.condition || '',
              description: '',
              images: item.searchImages || [],
              category_id: item.categoryId || '',
              source_url: item.source_url,
              isAuction: false,
            };
            log(`  Shops API 失敗，使用搜索數據（${detail.images.length}張圖）`);
          }
        } else {
          // Personal products: try detail API (full data with description + all photos)
          // If detail fails (no Mercari login token, or item gone), fall back to search data
          if (skipDetailApi) {
            detail = {
              title: item.title,
              price: item.price,
              condition: item.condition || '',
              description: '',
              images: item.searchImages || [],
              category_id: item.categoryId || '',
              source_url: item.source_url,
              isAuction: false,
            };
            fallbackCount++;
          } else {
            detail = await mercari.fetchProductDetail(item.source_url, useProxy);
            const detailEmpty = !detail || (!detail.title && !(detail.images || []).length);
            if (detailEmpty) {
              skipDetailApi = true;
              detail = {
                title: item.title,
                price: item.price,
                condition: item.condition || '',
                description: '',
                images: item.searchImages || [],
                category_id: item.categoryId || '',
                source_url: item.source_url,
                isAuction: false,
              };
              fallbackCount++;
              const hasToken = !!db.getConfig('mercariAccessToken');
              log(`  個人賣家詳情 API ${hasToken ? '失敗 (token 可能過期)' : '不可用 (請先打開 jp.mercari.com 登錄)'},本店鋪剩餘商品全部使用搜索數據`);
            }
          }
        }

        // 过滤条件：no data, auction, or completely empty result
        const isEmpty = !detail || (!detail.title && !(detail.images || []).length);
        if (isEmpty || detail.isAuction) {
          total--;
          const reason = isEmpty ? '無數據' : '拍賣品';
          log(`跳過 ${item.mercari_id} (${reason}),剩餘 ${total} 件`);
          db.updateShopStatus(sellerId, 'collecting', total, collected);
          emitProgress({ type: 'shop:progress', shopId: sellerId, total, collected, current: `${item.mercari_id} (跳過:${reason})` });
          return;
        }

        // Upsert product
        const upsertResult = db.upsertProduct({
          mercari_id: item.mercari_id,
          shop_id: sellerId,
          title: detail.title,
          price: detail.price,
          condition: detail.condition,
          description: detail.description,
          category_id: detail.category_id,
          images: detail.images,
          source_url: detail.source_url || item.source_url,
        });

        if (upsertResult.matched === 'title' || upsertResult.matched === 'photo') {
          const matchLabel = upsertResult.matched === 'photo' ? '圖片匹配' : '標題匹配';
          log(`  URL追蹤: ${upsertResult.oldId} → ${item.mercari_id} (${matchLabel}，同商品ID已更新)`);
        }

        // Download images
        const imgCount = (detail.images || []).length;
        const localImages = await downloader.downloadProductImages(
          { title: detail.title, images: detail.images, source_url: detail.source_url },
          useProxy, 5, log
        );
        db.markImagesDownloaded(item.mercari_id, localImages);

        collected++;
        db.updateShopStatus(sellerId, 'collecting', total, collected);
        emitProgress({ type: 'shop:progress', shopId: sellerId, total, collected, current: detail.title });
        log(`✓ [${collected}/${total}] ${detail.title.slice(0, 30)} | ¥${detail.price} | ${imgCount}張圖`);
      } catch (err) {
        collected++;
        failed++;
        console.error(`[collect] product ${item.mercari_id} error:`, err.message);
        log(`✗ [${collected}/${total}] ${item.mercari_id}: ${err.message}`);
        db.updateShopStatus(sellerId, 'collecting', total, collected);
      }
    }));

    // Only rate-limit if this batch had real API calls (not all skipped)
    const allSkipped = (skipped - prevSkipped) === batch.length;
    if (i < productList.length && !allSkipped) await mercari.sleep(DELAY_MS);
  }

  const elapsed = ((Date.now() - t1) / 1000).toFixed(1);
  const totalElapsed = ((Date.now() - t0) / 1000).toFixed(1);
  db.updateShopStatus(sellerId, 'done', total, collected);
  broadcast({ type: 'shop:status', shopId: sellerId, status: 'done', total, collected });
  log(`采集完成 | ${collected}/${total} 件 | 跳過 ${skipped} | 失敗 ${failed} | 詳情耗時 ${elapsed}s | 總耗時 ${totalElapsed}s`);
  if (fallbackCount > 0) {
    log(`(其中 ${fallbackCount} 件使用搜索數據 — 個人賣家詳情 API 不可用,描述為空)`);
  }

  // 通知协调器完成
  await coordinator.markCompleted(sellerId);
}

// ---- Web UI ----
app.get('/', (_req, res) => {
  res.setHeader('Content-Type', 'text/html; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(HTML);
});

// ---- Serve images ----
app.use('/images', (_req, res, next) => {
  express.static(downloader.getImagesDir())(_req, res, next);
});

app.listen(PORT, () => {
  // Apply saved image path on startup
  const savedImageBase = db.getConfig('imageBase');
  if (savedImageBase) downloader.setImagesDir(savedImageBase);
  // Clean up empty products on startup
  const cleaned = db.cleanupEmptyProducts();
  if (cleaned > 0) console.log(`[startup] 清理了 ${cleaned} 件空數據商品`);
  // Eager auto-heal clientId so UI never shows the stale value
  const cfg = coordinator.getConfig();
  console.log(`[startup] clientId: ${cfg.clientId}`);
  console.log(`圖片保存路徑: ${downloader.getImagesDir()}`);
  console.log(`煤爐采集器已啟動: http://localhost:${PORT}/`);
});

// Graceful shutdown
function shutdown() {
  console.log('\n正在關閉...');
  for (const [, job] of activeJobs) job.cancel = true;
  db.close();
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

// ---- HTML UI ----
const HTML = `<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>煤爐采集器</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0a0a;--card:#141414;--border:#222;--text:#e8e8e8;--dim:#888;--accent:#3b82f6;--accent2:#2563eb;--green:#22c55e;--red:#ef4444;--orange:#f59e0b}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Noto Sans SC','Noto Sans TC',sans-serif;background:var(--bg);color:var(--text);line-height:1.6}
.container{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:20px;font-weight:600;margin-bottom:16px;display:flex;align-items:center;gap:8px}
h1 span{font-size:12px;color:var(--dim);font-weight:400}

/* Input area */
.input-area{display:flex;gap:8px;margin-bottom:12px}
.input-area input,.input-area textarea{flex:1;background:#0d0d0d;border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px;outline:none;font-family:inherit}
.input-area input:focus,.input-area textarea:focus{border-color:var(--accent)}
.input-area button{padding:10px 20px;background:var(--accent);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:14px;white-space:nowrap}
.input-area button:hover{background:var(--accent2)}

/* Config row */
.config-row{display:flex;gap:8px;align-items:center;margin-bottom:20px;flex-wrap:wrap}
.config-row label{font-size:13px;color:var(--dim);display:flex;align-items:center;gap:4px;cursor:pointer}
.config-row input[type=text]{background:#0d0d0d;border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:13px;width:340px}
.btn-sm{padding:5px 12px;background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:6px;cursor:pointer;font-size:13px}
.btn-sm:hover{background:#1e1e1e}

/* Shop list */
.section-title{font-size:14px;color:var(--dim);margin:16px 0 8px;display:flex;align-items:center;justify-content:space-between}
.shop-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px;margin-bottom:8px}
.shop-header{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.shop-name{font-weight:500;font-size:15px;word-break:break-all}
.shop-status{font-size:12px;padding:3px 10px;border-radius:99px;white-space:nowrap}
.shop-status.discovering{background:#1e3a5f;color:#60a5fa}
.shop-status.collecting{background:#1e3a5f;color:#60a5fa}
.shop-status.done{background:#14532d;color:var(--green)}
.shop-status.error{background:#451a03;color:var(--red)}
.shop-status.cancelled{background:#422006;color:var(--orange)}
.shop-status.pending{background:#1c1c1c;color:var(--dim)}
.progress-bar{width:100%;height:6px;background:#1c1c1c;border-radius:3px;margin-top:8px;overflow:hidden}
.progress-fill{height:100%;background:var(--accent);border-radius:3px;transition:width .3s}
.shop-info{font-size:12px;color:var(--dim);margin-top:6px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.shop-actions{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.shop-log{font-size:12px;color:var(--dim);margin-top:6px;max-height:200px;overflow-y:auto;white-space:pre-wrap;font-family:Menlo,Consolas,monospace;background:#0a0a0a;border-radius:6px;padding:6px 8px}

/* Product table */
.product-section{margin-top:16px}
.toolbar{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;align-items:center}
.product-list{display:grid;gap:6px}
.product-row{display:grid;grid-template-columns:30px 1fr 90px 160px 60px;align-items:center;gap:8px;padding:8px 10px;background:var(--card);border:1px solid var(--border);border-radius:8px;font-size:13px}
.product-row.header{background:transparent;border:none;color:var(--dim);font-size:12px}
.product-title{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.product-title a{color:var(--accent);text-decoration:none}
.product-title a:hover{text-decoration:underline}

/* Toast */
.toast{position:fixed;top:20px;right:20px;padding:10px 20px;border-radius:8px;font-size:13px;color:#fff;z-index:9999;opacity:0;transition:opacity .3s;pointer-events:none;max-width:380px;line-height:1.4}
.toast.show{opacity:1}
.toast.success{background:var(--green)}
.toast.error{background:var(--red)}
.toast.warning{background:var(--orange)}

/* Modal */
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:99998;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .15s}
.modal-backdrop.show{opacity:1;pointer-events:all}
.modal-box{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:22px 24px;max-width:560px;min-width:320px;box-shadow:0 20px 60px rgba(0,0,0,0.5);transform:translateY(-10px);transition:transform .15s}
.modal-backdrop.show .modal-box{transform:translateY(0)}
.modal-text{font-size:14px;color:var(--text);line-height:1.6;white-space:pre-wrap;margin-bottom:20px;word-break:break-word}
.modal-buttons{display:flex;gap:8px;justify-content:flex-end}
.modal-buttons button{padding:8px 18px;border:1px solid var(--border);background:var(--card);color:var(--text);border-radius:6px;cursor:pointer;font-size:13px;min-width:72px}
.modal-buttons button:hover{background:#1e1e1e}
.modal-buttons button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.modal-buttons button.primary:hover{background:var(--accent2)}
.modal-buttons button.danger{background:var(--red);border-color:var(--red);color:#fff}
.modal-buttons button.danger:hover{filter:brightness(1.1)}

/* Empty state */
.empty{text-align:center;padding:40px;color:var(--dim);font-size:14px}

/* Responsive */
@media(max-width:700px){
  .product-row{grid-template-columns:30px 1fr 70px;font-size:12px}
  .product-row .hide-mobile{display:none}
}
</style>
</head>
<body>
<div id="toast" class="toast"></div>
<div class="container">
  <h1>煤爐采集器 <span>貼上賣家網址，自動采集全部商品</span></h1>

  <div id="welcomeBanner" style="display:none;background:#14532d;color:#bbf7d0;padding:10px 14px;border-radius:8px;margin-bottom:12px;font-size:13px;border:1px solid #166534"></div>

  <div class="input-area">
    <textarea id="urlInput" placeholder="賣家網址（一行一個,貼多個會自動批量認領）：&#10;https://jp.mercari.com/user/profile/...&#10;https://jp.mercari.com/shops/profile/..." rows="3" style="resize:vertical"></textarea>
    <button id="btnCollect" onclick="startCollect()">開始采集</button>
  </div>
  <div id="precheckResult" style="margin-bottom:12px;min-height:18px"></div>

  <div class="config-row">
    <label><input id="cfgProxy" type="checkbox" onchange="saveConfig()"/> 使用代理</label>
    <span style="color:var(--dim);font-size:13px">圖片保存路徑：</span>
    <input id="cfgBase" type="text" placeholder="留空則保存到程式目錄下 data/images/"/>
    <button class="btn-sm" onclick="saveConfig()">保存</button>
  </div>
  <div id="imgPathHint" style="font-size:12px;color:var(--dim);margin:-12px 0 12px;display:none"></div>

  <div class="config-row">
    <label style="color:var(--text)">
      <span id="coordStatusDot" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#888;margin-right:4px;vertical-align:middle"></span>
      協調器 <span id="coordStatusText" style="color:var(--dim);font-size:12px">檢查中...</span>
    </label>
    <span style="color:var(--dim);font-size:13px">　客戶端ID:</span>
    <input id="cfgClientId" type="text" placeholder="自動生成" style="width:280px"/>
    <button class="btn-sm" onclick="saveCoordinatorConfig()">保存</button>
    <a href="http://<RELAY_IP_REDACTED>:3031/" target="_blank" class="btn-sm" style="text-decoration:none">📊 團隊看板</a>
  </div>

  <div class="section-title">
    <span>店鋪列表</span>
    <span id="shopCount"></span>
  </div>
  <div class="toolbar" style="margin-bottom:10px">
    <button class="btn-sm" onclick="selectAllShops()">全選店鋪</button>
    <button class="btn-sm" onclick="selectNoneShops()">全不選</button>
    <button class="btn-sm" onclick="exportSelectedShopsXlsx()">導出已選店鋪 XLSX</button>
    <button class="btn-sm" onclick="exportSelectedShopsZip()">打包已選店鋪 ZIP</button>
    <button class="btn-sm" onclick="deleteSelectedShops()" style="color:var(--red)">刪除已選店鋪</button>
  </div>
  <div id="shopList"></div>

  <div class="product-section" id="productSection" style="display:none">
    <div class="section-title">
      <span id="productTitle">商品列表</span>
    </div>
    <div class="toolbar">
      <button class="btn-sm" onclick="selectAll()">全選</button>
      <button class="btn-sm" onclick="selectNone()">全不選</button>
      <button class="btn-sm" onclick="exportCsv()">導出 XLSX</button>
      <button class="btn-sm" onclick="exportZip()">打包 ZIP</button>
      <button class="btn-sm" onclick="deleteSelected()" style="color:var(--red)">刪除選中</button>
    </div>
    <div id="productList"></div>
  </div>
</div>

<script>/* v1.0.1 */
console.log('[JS] 脚本已加载 v1.0.1');
const API = '';
let shops = [];
let products = [];
let currentShopId = null;
let selectedShopIds = new Set();
let shopLogs = {};

// ---- SSE ----
const es = new EventSource(API + '/api/events');
es.onmessage = (e) => {
  try {
    const d = JSON.parse(e.data);
    handleEvent(d);
  } catch(_){}
};

// Throttled loadShops — batches rapid events into one fetch per 500ms.
// Without this, 4 parallel shops emitting 100+ events/sec each causes UI freeze.
let _loadShopsPending = false;
function scheduleLoadShops() {
  if (_loadShopsPending) return;
  _loadShopsPending = true;
  setTimeout(() => { _loadShopsPending = false; loadShops(); }, 500);
}

// Update only progress bar/counter in-place — no fetch, no full re-render
function updateShopProgress(d) {
  const card = document.getElementById('shop-' + d.shopId);
  if (!card) { scheduleLoadShops(); return; }  // shop card not yet rendered
  const fill = card.querySelector('.progress-fill');
  if (fill && d.total > 0) {
    fill.style.width = Math.round(d.collected / d.total * 100) + '%';
  }
  const info = card.querySelector('.shop-info');
  if (info) {
    const pct = d.total > 0 ? Math.round(d.collected / d.total * 100) : 0;
    // Replace just the count + pct spans, preserve type span
    const spans = info.querySelectorAll('span');
    if (spans[0]) spans[0].textContent = d.collected + ' / ' + d.total + ' 件';
    if (spans[1]) spans[1].textContent = pct + '%';
  }
}

function handleEvent(d) {
  if (d.type === 'shop:progress') {
    // In-place update — no fetch, no full re-render
    updateShopProgress(d);
  }
  if (d.type === 'shop:status') {
    scheduleLoadShops();
    if (d.shopId === currentShopId && d.status === 'done') {
      loadProducts(currentShopId);
    }
  }
  if (d.type === 'shop:deleted') {
    scheduleLoadShops();
    if (d.shopId === currentShopId) {
      currentShopId = null;
      document.getElementById('productSection').style.display = 'none';
    }
  }
  if (d.type === 'shop:log') {
    if (!shopLogs[d.shopId]) shopLogs[d.shopId] = [];
    shopLogs[d.shopId].push(d.message);
    if (shopLogs[d.shopId].length > 200) shopLogs[d.shopId].shift();
    renderShopLog(d.shopId);
  }
  if (d.type === 'shop:error') {
    scheduleLoadShops();
    if (d.error) showAlert('采集失敗:' + d.error);
  }
}

// ---- API calls ----
async function api(path, opts) {
  try {
    console.log('[api] 请求:', path, opts);
    const r = await fetch(API + path, opts);
    console.log('[api] 响应状态:', r.status, r.ok);
    if (!r.ok) {
      const text = await r.text().catch(() => '');
      console.log('[api] 响应文本:', text);
      try {
        const json = JSON.parse(text);
        console.log('[api] 解析JSON:', json);
        return json;
      } catch(_) {}
      return { error: 'HTTP ' + r.status + ': ' + text.slice(0, 200) };
    }
    return r.headers.get('content-type')?.includes('json') ? r.json() : r;
  } catch (e) {
    console.error('[api]', path, e);
    return { error: '網絡錯誤: ' + e.message };
  }
}

async function loadShops() {
  try {
    const r = await api('/api/shops');
    if (Array.isArray(r)) { shops = r; renderShops(); }
    else console.error('[loadShops] unexpected:', r);
  } catch(e) { console.error('[loadShops]', e); }
}

async function loadProducts(shopId) {
  currentShopId = shopId;
  try {
    const r = await api('/api/products?shop_id=' + encodeURIComponent(shopId));
    if (Array.isArray(r)) { products = r; renderProducts(); }
    else console.error('[loadProducts] unexpected:', r);
  } catch(e) { console.error('[loadProducts]', e); }
}

function showToast(msg, type, ms) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + (type || 'success') + ' show';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.className = 'toast', ms || 2500);
}

// Non-blocking replacement for native alert()
function showAlert(msg, type) {
  showToast(msg, type || 'error', 3500);
}

// Promise-returning replacement for native confirm() — non-blocking modal
function showConfirm(msg, opts) {
  opts = opts || {};
  // Clean any stuck modals from previous interactions (defensive)
  document.querySelectorAll('.modal-backdrop').forEach(el => el.remove());
  return new Promise(resolve => {
    const back = document.createElement('div');
    back.className = 'modal-backdrop';
    const okClass = opts.danger ? 'danger' : 'primary';
    back.innerHTML =
      '<div class="modal-box">' +
        '<div class="modal-text"></div>' +
        '<div class="modal-buttons">' +
          '<button class="modal-cancel">' + (opts.cancelText || '取消') + '</button>' +
          '<button class="' + okClass + ' modal-ok">' + (opts.okText || '確定') + '</button>' +
        '</div>' +
      '</div>';
    back.querySelector('.modal-text').textContent = msg;
    document.body.appendChild(back);
    requestAnimationFrame(() => back.classList.add('show'));

    let done = false;
    function close(result) {
      if (done) return; done = true;
      back.classList.remove('show');
      setTimeout(() => back.remove(), 200);
      document.removeEventListener('keydown', onKey);
      resolve(result);
    }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
      else if (e.key === 'Enter') { e.preventDefault(); close(true); }
    }
    back.querySelector('.modal-ok').onclick = () => close(true);
    back.querySelector('.modal-cancel').onclick = () => close(false);
    back.onclick = e => { if (e.target === back) close(false); };
    document.addEventListener('keydown', onKey);
    setTimeout(() => back.querySelector('.modal-ok').focus(), 50);
  });
}

async function loadConfig() {
  const cfg = await api('/api/config');
  document.getElementById('cfgProxy').checked = cfg.useProxy === '1';
  document.getElementById('cfgBase').value = cfg.imageBase || '';
  document.getElementById('cfgClientId').value = cfg.clientId || '';
  // Show current images path
  const hint = document.getElementById('imgPathHint');
  if (cfg.imagesDir) {
    const isCustom = cfg.imageBase && cfg.imagesDir !== cfg.defaultImagesDir;
    hint.innerHTML = '當前圖片保存位置：<b>' + esc(cfg.imagesDir) + '</b>'
      + (isCustom ? '' : '　（未設定自訂路徑，使用預設位置）');
    hint.style.display = 'block';
  }
}

async function saveConfig() {
  const r = await api('/api/config', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      useProxy: document.getElementById('cfgProxy').checked,
      imageBase: document.getElementById('cfgBase').value.trim()
    })
  });
  if (r && r.ok) {
    showToast('配置已保存');
  } else {
    showToast('保存失敗: ' + (r?.error || '未知錯誤'), 'error');
  }
}

async function saveCoordinatorConfig() {
  const clientId = document.getElementById('cfgClientId').value.trim();
  if (!clientId) {
    showToast('請輸入客戶端ID', 'error');
    return;
  }
  const r = await api('/api/coordinator/config', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ clientId })
  });
  if (r && r.ok) {
    showToast('客戶端ID已保存');
  } else {
    showToast('保存失敗: ' + (r?.error || '未知錯誤'), 'error');
  }
}

async function startCollect() {
  const btn = document.getElementById('btnCollect');
  const text = document.getElementById('urlInput').value.trim();
  if (!text) return showAlert('請輸入賣家網址');

  const urls = [...new Set(
    text.split(/[\\n\\r,\\t ]+/).map(s => s.trim()).filter(Boolean)
  )];

  btn.disabled = true;
  btn.textContent = '提交中...';

  try {
    if (urls.length === 1) {
      const r = await api('/api/collect', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ url: urls[0] })
      });
      console.log('[前端] 收到响应:', r);
      if (r.error) {
        if (r.needConfirm) {
          if (await showConfirm('您已經下載過此店鋪,是否要重新下載?')) {
            const r2 = await api('/api/collect', {
              method: 'POST',
              headers: {'Content-Type':'application/json'},
              body: JSON.stringify({ url: urls[0], forceRedownload: true })
            });
            if (r2.error) return showAlert(r2.error);
          } else {
            return;
          }
        } else {
          return showAlert(r.error);
        }
      }
    } else {
      // Batch: first pre-claim on cloud, show summary, then proceed for the OK ones
      const pre = await api('/api/coordinator/claim-batch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ urls })
      });
      if (pre.error) return showAlert(pre.error);

      const okList = (pre.claimResult?.results || []).filter(x => x.ok);
      const rejected = (pre.claimResult?.results || []).filter(x => !x.ok);
      const errors = pre.errors || [];

      let summary = '批量認領完成:\\n';
      summary += '  ✓ 可採: ' + okList.length + ' 個\\n';
      if (rejected.length) summary += '  ✗ 被佔: ' + rejected.length + ' 個 (' + rejected.map(r => r.shop_id + '→' + r.locked_by).slice(0,5).join(', ') + (rejected.length > 5 ? ' ...' : '') + ')\\n';
      if (errors.length) summary += '  ⚠ 無法識別: ' + errors.length + ' 個\\n';
      summary += '\\n確定開始採集這 ' + okList.length + ' 個店鋪嗎?';
      if (okList.length === 0) {
        showAlert('沒有可採的店鋪');
        return;
      }
      if (!(await showConfirm(summary, { okText: '開始採集' }))) {
        return;
      }

      const claimedUrls = okList.map(ok => {
        const matched = pre.shops.find(s => s.shop_id === ok.shop_id);
        return matched ? matched.original_url : null;
      }).filter(Boolean);

      const r = await api('/api/collect-batch', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ urls: claimedUrls })
      });
      if (r.error) return showAlert(r.error);
    }
    document.getElementById('urlInput').value = '';
    loadShops();
  } catch (e) {
    showAlert('請求失敗: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '開始采集';
  }
}

async function cancelCollect(shopId) {
  await api('/api/collect/cancel', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ shopId })
  });
}

async function deleteShop(shopId) {
  if (!(await showConfirm('確定刪除此店鋪及其商品資料?圖片不會刪除。', { danger: true, okText: '刪除' }))) return;
  const r = await api('/api/shops/' + encodeURIComponent(shopId), { method: 'DELETE' });
  if (!r || r.ok !== true) {
    showAlert('刪除失敗:' + (r?.error || '未知錯誤'));
    return;
  }
  selectedShopIds.delete(shopId);
  if (currentShopId === shopId) {
    currentShopId = null;
    document.getElementById('productSection').style.display = 'none';
  }
  loadShops();
}

// ---- Render ----
function renderShops() {
  const el = document.getElementById('shopList');
  document.getElementById('shopCount').textContent = shops.length ? shops.length + ' 個店鋪' : '';

  if (!shops.length) {
    el.innerHTML = '<div class="empty">尚未采集任何店鋪。貼上賣家網址開始采集。</div>';
    return;
  }

  el.innerHTML = shops.map(s => {
    const pct = s.total_products > 0 ? Math.round(s.collected_products / s.total_products * 100) : 0;
    const isActive = s.status === 'collecting' || s.status === 'discovering' || s.status === 'pending';
    return '<div class="shop-card" id="shop-'+s.id+'">'
      + '<div class="shop-header">'
      + '<input type="checkbox" class="shop-check" data-shop-id="'+s.id+'" onchange="toggleShopCheck(this)" '
      + (selectedShopIds.has(s.id) ? 'checked' : '') + ' style="margin-right:8px"/>'
      + '<span class="shop-name">' + esc(s.name || s.id) + '</span>'
      + '<span class="shop-status ' + s.status + '">' + statusText(s.status) + '</span>'
      + '</div>'
      + (isActive ? '<div class="progress-bar"><div class="progress-fill" style="width:'+pct+'%"></div></div>' : '')
      + '<div class="shop-info">'
      + '<span>' + s.collected_products + ' / ' + s.total_products + ' 件</span>'
      + (isActive ? '<span>' + pct + '%</span>' : '')
      + '<span>' + (s.type === 'shop' ? '店鋪' : '個人') + '</span>'
      + '</div>'
      + '<div id="log-'+s.id+'" class="shop-log" style="display:'+(shopLogs[s.id]?.length?'block':'none')+'">'
      + esc((shopLogs[s.id]||[]).join('\\n'))
      + '</div>'
      + '<div class="shop-actions">'
      + '<button class="btn-sm" onclick="loadProducts(\\''+s.id+'\\')">查看商品</button>'
      + (isActive ? '<button class="btn-sm" onclick="cancelCollect(\\''+s.id+'\\')" style="color:var(--orange)">取消</button>' : '')
      + (s.status === 'done' || s.status === 'error' || s.status === 'cancelled'
        ? '<button class="btn-sm" onclick="recollect(\\''+s.id+'\\')" style="color:var(--green)">重新采集</button>' : '')
      + (s.type === 'shop' && !isActive
        ? '<button class="btn-sm" onclick="refreshShop(\\''+s.id+'\\')" style="color:var(--accent)">刷新圖片</button>' : '')
      + '<button class="btn-sm" onclick="deleteShop(\\''+s.id+'\\')" style="color:var(--red)">刪除</button>'
      + '</div>'
      + '</div>';
  }).join('');
}

// Throttle log render — coalesce per shop, render at most every 300ms
const _logPending = {};
function renderShopLog(shopId) {
  if (_logPending[shopId]) return;
  _logPending[shopId] = true;
  setTimeout(() => {
    _logPending[shopId] = false;
    const el = document.getElementById('log-' + shopId);
    if (!el) return;
    const logs = shopLogs[shopId] || [];
    el.style.display = logs.length ? 'block' : 'none';
    el.textContent = logs.join('\\n');
    el.scrollTop = el.scrollHeight;
  }, 300);
}

function renderProducts() {
  const sec = document.getElementById('productSection');
  const shop = shops.find(s => s.id === currentShopId);
  document.getElementById('productTitle').textContent = '商品列表' + (shop ? '：' + (shop.name || shop.id) : '') + '（' + products.length + ' 件）';

  if (!products.length) {
    document.getElementById('productList').innerHTML = '<div class="empty">暫無商品</div>';
    sec.style.display = 'block';
    return;
  }

  let html = '<div class="product-row header">'
    + '<span></span><span>標題</span><span>價格</span><span class="hide-mobile">狀態</span><span></span>'
    + '</div>';

  for (const p of products) {
    html += '<div class="product-row">'
      + '<input type="checkbox" class="pck" value="'+p.id+'"/>'
      + '<span class="product-title"><a href="'+esc(p.source_url)+'" target="_blank">'+esc(p.title)+'</a></span>'
      + '<span>¥'+Number(p.price).toLocaleString()+'</span>'
      + '<span class="hide-mobile">'+esc(p.condition)+'</span>'
      + '<span>'+(p.images_downloaded?'✓':'')+'</span>'
      + '</div>';
  }
  document.getElementById('productList').innerHTML = html;
  sec.style.display = 'block';
}

function statusText(s) {
  const m = {discovering:'探索中',collecting:'采集中',done:'已完成',error:'錯誤',cancelled:'已取消',pending:'等待中'};
  return m[s] || s;
}

function esc(s) {
  const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML;
}

// ---- Actions ----
function selectAll() { document.querySelectorAll('.pck').forEach(c => c.checked = true); }
function selectNone() { document.querySelectorAll('.pck').forEach(c => c.checked = false); }

function getSelected() {
  return Array.from(document.querySelectorAll('.pck:checked')).map(c => parseInt(c.value, 10));
}

async function exportCsv() {
  const ids = getSelected();
  const body = JSON.stringify({ ids });
  const r = await fetch(API + '/api/export/csv', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body
  });
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url;
  a.download = r.headers.get('content-disposition')?.match(/filename="([^"]+)"/)?.[1] || 'export.csv';
  a.click(); URL.revokeObjectURL(url);
}

async function exportZip() {
  const ids = getSelected();
  const body = JSON.stringify({ ids });
  const r = await fetch(API + '/api/export/zip', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body
  });
  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a'); a.href = url;
  a.download = r.headers.get('content-disposition')?.match(/filename="([^"]+)"/)?.[1] || 'images.zip';
  a.click(); URL.revokeObjectURL(url);
}

async function deleteSelected() {
  const ids = getSelected();
  if (!ids.length) return showAlert('請先選擇商品');
  if (!(await showConfirm('確定刪除選中的 '+ids.length+' 件商品?', { danger: true, okText: '刪除' }))) return;
  await api('/api/delete-products', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ ids })
  });
  loadProducts(currentShopId);
  loadShops();
}

async function recollect(shopId) {
  const shop = shops.find(s => s.id === shopId);
  if (!shop) return;
  shopLogs[shopId] = [];
  const r = await api('/api/collect', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ url: shop.url })
  });
  if (r.error) {
    if (r.needConfirm) {
      if (await showConfirm('您已經下載過此店鋪,是否要重新下載?')) {
        const r2 = await api('/api/collect', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ url: shop.url, forceRedownload: true })
        });
        if (r2.error) showAlert(r2.error);
      }
    } else {
      showAlert(r.error);
    }
  }
  loadShops();
}

async function refreshShop(shopId) {
  shopLogs[shopId] = [];
  const r = await api('/api/refresh-shop', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ shopId })
  });
  if (r.error) showAlert(r.error);
  loadShops();
}

// ---- Shop selection ----
function toggleShopCheck(checkbox) {
  const shopId = checkbox.dataset.shopId;
  if (checkbox.checked) selectedShopIds.add(shopId);
  else selectedShopIds.delete(shopId);
}

function selectAllShops() {
  shops.forEach(s => selectedShopIds.add(s.id));
  renderShops();
}

function selectNoneShops() {
  selectedShopIds.clear();
  renderShops();
}

async function exportSelectedShopsXlsx() {
  const shopIds = Array.from(selectedShopIds);
  if (!shopIds.length) return showAlert('請先勾選店鋪');

  const r = await fetch(API + '/api/export/csv', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ shopIds })
  });

  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = r.headers.get('content-disposition')?.match(/filename="([^"]+)"/)?.[1] || 'export.xlsx';
  a.click();
  URL.revokeObjectURL(url);
}

async function exportSelectedShopsZip() {
  const shopIds = Array.from(selectedShopIds);
  if (!shopIds.length) return showAlert('請先勾選店鋪');

  const r = await fetch(API + '/api/export/zip', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ shopIds })
  });

  const blob = await r.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = r.headers.get('content-disposition')?.match(/filename="([^"]+)"/)?.[1] || 'images.zip';
  a.click();
  URL.revokeObjectURL(url);
}

async function deleteSelectedShops() {
  const ids = Array.from(selectedShopIds);
  if (!ids.length) return showAlert('請先勾選要刪除的店鋪');
  if (!(await showConfirm('確定刪除已選的 ' + ids.length + ' 個店鋪及其商品資料?圖片不會刪除。', { danger: true, okText: '刪除' }))) return;

  const r = await api('/api/delete-shops', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ ids })
  });

  if (!r || r.ok !== true) {
    showAlert('刪除失敗:' + (r?.error || '未知錯誤'));
    return;
  }

  ids.forEach(id => selectedShopIds.delete(id));
  if (currentShopId && ids.includes(currentShopId)) {
    currentShopId = null;
    document.getElementById('productSection').style.display = 'none';
  }
  loadShops();
}

// ---- Coordinator status indicator + welcome banner ----
async function refreshCoordinatorStatus() {
  try {
    const r = await fetch(API + '/api/coordinator/ping').then(r => r.json());
    const dot = document.getElementById('coordStatusDot');
    const txt = document.getElementById('coordStatusText');
    if (r.online) {
      dot.style.background = 'var(--green)';
      txt.textContent = r.legacy ? '已連線(舊版)' : '已連線';
      txt.style.color = 'var(--green)';
    } else {
      dot.style.background = 'var(--red)';
      txt.textContent = '離線 — 新採集會被阻擋(strict 模式)';
      txt.style.color = 'var(--red)';
    }
    // Sync clientId display with actual value (auto-heal feedback)
    const inp = document.getElementById('cfgClientId');
    if (r.clientId && inp.value !== r.clientId) inp.value = r.clientId;
    if (r.autoReplaced) showWelcomeBanner(r.clientId, r.oldId);
  } catch(e) {
    document.getElementById('coordStatusDot').style.background = 'var(--orange)';
    document.getElementById('coordStatusText').textContent = '未知';
  }
}
function showWelcomeBanner(newId, oldId) {
  const el = document.getElementById('welcomeBanner');
  el.innerHTML = '👋 系統已自動為你命名為 <b>' + esc(newId) + '</b>'
    + (oldId ? '(原 "' + esc(oldId) + '" 已升級)' : '')
    + ' · 你可以在 <a href="http://<RELAY_IP_REDACTED>:3031/" target="_blank" style="color:#bbf7d0;text-decoration:underline">團隊看板</a> 看到自己 · '
    + '<a href="#" onclick="dismissBanner();return false" style="color:#bbf7d0;text-decoration:underline">知道了</a>';
  el.style.display = 'block';
}
async function dismissBanner() {
  await api('/api/coordinator/ack-replaced', { method: 'POST' });
  document.getElementById('welcomeBanner').style.display = 'none';
}
refreshCoordinatorStatus();
setInterval(refreshCoordinatorStatus, 15000);

// ---- Pre-check on URL paste ----
let precheckTimer = null;
async function precheckUrls() {
  const text = document.getElementById('urlInput').value.trim();
  if (!text) {
    document.getElementById('precheckResult').innerHTML = '';
    return;
  }
  const urls = [...new Set(text.split(/[\\n\\r,\\t ]+/).map(s => s.trim()).filter(Boolean))];
  if (!urls.length) return;
  // Only precheck single URL for speed; for batches just show count
  if (urls.length > 1) {
    document.getElementById('precheckResult').innerHTML =
      '<span style="color:var(--dim);font-size:12px">📋 將提交 ' + urls.length + ' 個賣家 — 雲端會自動分配未被佔用的</span>';
    return;
  }
  try {
    const r = await fetch(API + '/api/coordinator/precheck', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ url: urls[0] })
    }).then(r => r.json());
    const el = document.getElementById('precheckResult');
    if (!r.ok) {
      el.innerHTML = '<span style="color:var(--dim);font-size:12px">' + esc(r.error || '無法預檢查') + '</span>';
      return;
    }
    const s = r.status;
    if (!s || !s.exists) {
      el.innerHTML = '<span style="color:var(--green);font-size:12px">✓ 此店鋪從未被採集過</span>';
      return;
    }
    if (s.status === 'completed') {
      el.innerHTML = '<span style="color:var(--orange);font-size:12px">⚠️ <b>' + esc(s.locked_by) + '</b> 已採過此店鋪 (' + fmtAgo(s.completed_at) + ',' + (s.collected_products||0) + ' 件)</span>';
    } else if (s.status === 'collecting') {
      el.innerHTML = '<span style="color:var(--accent);font-size:12px">⏳ <b>' + esc(s.locked_by) + '</b> 正在採集中 — 提交會被拒絕</span>';
    } else {
      el.innerHTML = '<span style="color:var(--dim);font-size:12px">' + esc(s.locked_by) + ' 之前嘗試失敗(' + s.status + ')</span>';
    }
  } catch(e) {}
}
function fmtAgo(ts) {
  if (!ts) return '';
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 60) return sec + '秒前';
  if (sec < 3600) return Math.floor(sec / 60) + '分鐘前';
  if (sec < 86400) return Math.floor(sec / 3600) + '小時前';
  return Math.floor(sec / 86400) + '天前';
}

document.getElementById('urlInput').addEventListener('input', () => {
  clearTimeout(precheckTimer);
  precheckTimer = setTimeout(precheckUrls, 500);
});

// ---- Init ----
loadConfig();
loadShops();

// Ctrl+Enter to start collect
document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter' && e.ctrlKey) startCollect();
});
</script>
</body>
</html>`;
