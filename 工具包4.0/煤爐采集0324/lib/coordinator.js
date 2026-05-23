const axios = require('axios');
const os = require('os');
const db = require('./db');

function autoClientId() {
  const user = process.env.USERNAME || process.env.USER || 'user';
  const host = os.hostname() || 'pc';
  return `${user}-${host}`.replace(/[^A-Za-z0-9_.-]/g, '_');
}

function isBadClientId(s) {
  if (!s) return true;
  if (s.length < 3) return true;                 // single/double letter
  if (/^[A-Za-z]$/.test(s)) return true;         // single letter
  if (/^(test|temp|abc|user|client|pc)$/i.test(s)) return true;
  return false;
}

function getConfig() {
  let clientId = db.getConfig('clientId');
  if (isBadClientId(clientId)) {
    const old = clientId || '';
    clientId = autoClientId();
    db.setConfig('clientId', clientId);
    db.setConfig('clientId_auto_replaced', '1');
    db.setConfig('clientId_old', old);
    console.log(`[coordinator] auto-healed clientId: "${old}" → "${clientId}"`);
  }
  return {
    enabled: db.getConfig('coordinatorEnabled') === '1',
    url: db.getConfig('coordinatorUrl'),
    clientId,
    mode: db.getConfig('coordinatorMode') || 'strict'
  };
}

async function request(method, path, data = null, retries = 3) {
  const config = getConfig();
  if (!config.enabled || !config.url) return null;

  for (let i = 0; i < retries; i++) {
    try {
      const opts = { method, url: `${config.url}${path}`, timeout: 5000 };
      if (data) opts.data = data;
      const res = await axios(opts);
      return res.data;
    } catch (err) {
      if (i === retries - 1) throw err;
      await new Promise(r => setTimeout(r, 1000));
    }
  }
}

async function checkShop(shopId) {
  const config = getConfig();
  if (!config.enabled || !config.url) return null;
  try {
    const res = await axios.get(`${config.url}/api/check?shop_id=${encodeURIComponent(shopId)}`, { timeout: 5000 });
    return res.data;
  } catch (err) {
    console.error('[coordinator] checkShop failed:', err.message);
    return null;
  }
}

async function checkBatch(shopIds) {
  const config = getConfig();
  if (!config.enabled || !config.url) return null;
  try {
    const res = await axios.post(`${config.url}/api/check-batch`, { shop_ids: shopIds }, { timeout: 8000 });
    return res.data;
  } catch (err) {
    console.error('[coordinator] checkBatch failed:', err.message);
    return null;
  }
}

async function claimBatch(shops) {
  const config = getConfig();
  if (!config.enabled || !config.url) {
    return { ok: false, error: '协调器未启用' };
  }
  try {
    const res = await axios.post(`${config.url}/api/claim-batch`, {
      client_id: config.clientId,
      shops
    }, { timeout: 10000 });
    return res.data;
  } catch (err) {
    console.error('[coordinator] claimBatch failed:', err.message);
    if (config.mode === 'strict') {
      return { ok: false, error: '协调服务器不可用,已停止新任务' };
    }
    return { ok: true, offline: true, results: shops.map(s => ({ shop_id: s.shop_id, ok: true, status: 'offline' })) };
  }
}

async function requestLock(shopId, shopName, shopUrl) {
  const config = getConfig();
  if (!config.enabled) return { success: true, offline: true };

  try {
    const result = await request('POST', '/api/lock', {
      shop_id: shopId,
      client_id: config.clientId,
      shop_name: shopName,
      shop_url: shopUrl
    });

    console.log('[coordinator] requestLock result:', JSON.stringify(result));

    if (!result.success && result.reason === 'completed' && result.locked_by === config.clientId) {
      return {
        success: false,
        reason: 'self_completed',
        locked_by: result.locked_by,
        completed_at: result.completed_at
      };
    }

    return result;
  } catch (err) {
    console.error('[coordinator] requestLock failed:', err.message);
    if (config.mode === 'strict') {
      return { success: false, error: '协调服务器不可用,已停止新任务' };
    }
    return { success: true, offline: true };
  }
}

async function markCompleted(shopId, totalProducts, collectedProducts) {
  const config = getConfig();
  if (!config.enabled) return;

  try {
    await request('POST', '/api/complete', {
      shop_id: shopId,
      client_id: config.clientId,
      total_products: totalProducts,
      collected_products: collectedProducts
    });
  } catch (err) {
    console.error('[coordinator] markCompleted failed:', err.message);
  }
}

async function markFailed(shopId, reason, note) {
  const config = getConfig();
  if (!config.enabled) return;

  try {
    await request('POST', '/api/cancel', {
      shop_id: shopId,
      client_id: config.clientId,
      status: reason || 'failed',
      note: note || ''
    });
  } catch (err) {
    console.error('[coordinator] markFailed failed:', err.message);
  }
}

async function heartbeat(shopId, totalProducts, collectedProducts) {
  const config = getConfig();
  if (!config.enabled) return;

  try {
    await request('POST', '/api/heartbeat', {
      shop_id: shopId,
      client_id: config.clientId,
      total_products: totalProducts,
      collected_products: collectedProducts
    }, 1);
  } catch (err) {
    // 心跳失败不影响采集
  }
}

async function pingCoordinator() {
  const config = getConfig();
  if (!config.enabled || !config.url) return { online: false, reason: 'disabled' };
  try {
    const res = await axios.get(`${config.url}/health`, { timeout: 3000 });
    if (res.data && res.data.ok) return { online: true, time: res.data.time };
    // Fallback: try /api/list which old v1 server has
    await axios.get(`${config.url}/api/list`, { timeout: 3000 });
    return { online: true, legacy: true };
  } catch (err) {
    try {
      await axios.get(`${config.url}/api/list`, { timeout: 3000 });
      return { online: true, legacy: true };
    } catch (_) {
      return { online: false, reason: err.message };
    }
  }
}

module.exports = {
  requestLock,
  markCompleted,
  markFailed,
  heartbeat,
  getConfig,
  checkShop,
  checkBatch,
  claimBatch,
  pingCoordinator,
};
