// ==UserScript==
// @name         Mercari → 煤爐采集器
// @namespace    http://tampermonkey.net/
// @version      2.3
// @description  Mercari 賣家頁加採集按鈕 + 顯示協調器狀態
// @match        https://jp.mercari.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @connect      <RELAY_IP_REDACTED>
// @updateURL    http://<RELAY_IP_REDACTED>:3031/tampermonkey.user.js
// @downloadURL  http://<RELAY_IP_REDACTED>:3031/tampermonkey.user.js
// ==/UserScript==

(function () {
  'use strict';

  const COLLECTOR_URL = 'http://127.0.0.1:3030/api/collect';
  const COORDINATOR_URL = 'http://<RELAY_IP_REDACTED>:3031';
  const STATE = { lastCheckedId: null, lastResult: null };

  // ---- helpers ----
  function gmFetch(method, url, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method, url,
        headers: { 'Content-Type': 'application/json' },
        data: body ? JSON.stringify(body) : undefined,
        timeout: 8000,
        onload: r => {
          try { resolve(JSON.parse(r.responseText)); }
          catch (_) { resolve({ raw: r.responseText, status: r.status }); }
        },
        onerror: e => reject(e),
        ontimeout: () => reject(new Error('timeout')),
      });
    });
  }

  function parseSellerId() {
    const u = location.pathname;
    let m = u.match(/\/user\/profile\/([A-Za-z0-9_-]+)/);
    if (m) return { id: m[1], type: 'personal' };
    m = u.match(/\/shops\/profile\/([A-Za-z0-9_-]+)/);
    if (m) return { id: m[1], type: 'shop' };
    return null;
  }

  function fmtAgo(ts) {
    if (!ts) return '';
    const sec = Math.floor((Date.now() - ts) / 1000);
    if (sec < 60) return sec + '秒前';
    if (sec < 3600) return Math.floor(sec / 60) + '分鐘前';
    if (sec < 86400) return Math.floor(sec / 3600) + '小時前';
    return Math.floor(sec / 86400) + '天前';
  }

  // ---- main send button ----
  function ensureButton() {
    if (document.getElementById('send-to-collector-btn')) return;

    const btn = document.createElement('button');
    btn.id = 'send-to-collector-btn';
    btn.textContent = '發送到采集器';
    btn.style.cssText = `
      position: fixed;
      right: 20px;
      bottom: 20px;
      z-index: 999999;
      padding: 12px 16px;
      background: #ff5a5f;
      color: white;
      border: none;
      border-radius: 8px;
      cursor: pointer;
      font-size: 14px;
      font-weight: 500;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      transition: all 0.2s;
    `;
    btn.onmouseover = () => { btn.style.transform = 'scale(1.05)'; };
    btn.onmouseout = () => { btn.style.transform = 'scale(1)'; };
    btn.onclick = () => sendToCollector(btn);
    document.body.appendChild(btn);
  }

  async function sendToCollector(btn) {
    btn.disabled = true;
    btn.textContent = '發送中...';
    try {
      const data = await gmFetch('POST', COLLECTOR_URL, { url: location.href });
      if (data.error) {
        if (data.needConfirm) {
          if (confirm('您已經下載過此店鋪,是否要重新下載?')) {
            const data2 = await gmFetch('POST', COLLECTOR_URL, { url: location.href, forceRedownload: true });
            if (data2.error) throw new Error(data2.error);
          } else {
            btn.disabled = false;
            btn.textContent = '發送到采集器';
            return;
          }
        } else {
          throw new Error(data.error);
        }
      }
      btn.textContent = '✓ 已發送';
      btn.style.background = '#22c55e';
      setTimeout(() => {
        btn.textContent = '發送到采集器';
        btn.style.background = '#ff5a5f';
        btn.disabled = false;
      }, 2000);
    } catch (err) {
      alert('發送失敗: ' + (err.message || err));
      btn.disabled = false;
      btn.textContent = '發送到采集器';
    }
  }

  // ---- status chip: shows "已被 XX 採過" or "從未採過" ----
  function ensureStatusChip() {
    let chip = document.getElementById('coordinator-status-chip');
    if (chip) return chip;

    chip = document.createElement('div');
    chip.id = 'coordinator-status-chip';
    chip.style.cssText = `
      position: fixed;
      right: 20px;
      bottom: 80px;
      z-index: 999998;
      padding: 8px 14px;
      background: #1a1a1a;
      color: #fff;
      border: 1px solid #333;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 500;
      max-width: 280px;
      line-height: 1.4;
      box-shadow: 0 2px 8px rgba(0,0,0,0.3);
      cursor: default;
      transition: opacity 0.2s;
    `;
    document.body.appendChild(chip);
    return chip;
  }

  function setChip(text, color) {
    const chip = ensureStatusChip();
    chip.textContent = text;
    chip.style.background = color || '#1a1a1a';
  }

  async function refreshStatus() {
    const seller = parseSellerId();
    const chip = document.getElementById('coordinator-status-chip');
    if (!seller) {
      if (chip) chip.style.display = 'none';
      return;
    }
    if (chip) chip.style.display = 'block';
    if (STATE.lastCheckedId === seller.id && STATE.lastResult) {
      applyResult(STATE.lastResult, seller);
      return;
    }
    setChip('🔄 檢查中...', '#1a1a1a');
    try {
      const r = await gmFetch('GET', `${COORDINATOR_URL}/api/check?shop_id=${encodeURIComponent(seller.id)}`);
      STATE.lastCheckedId = seller.id;
      STATE.lastResult = r;
      applyResult(r, seller);
    } catch (e) {
      setChip('⚠️ 雲端離線', '#7c2d12');
    }
  }

  function applyResult(r, seller) {
    const btn = document.getElementById('send-to-collector-btn');
    if (!r || !r.exists) {
      setChip(`✓ 此店鋪從未採過 (${seller.id})`, '#14532d');
      if (btn) { btn.style.background = '#ff5a5f'; btn.textContent = '發送到采集器'; }
      return;
    }
    const by = r.locked_by || '?';
    const when = fmtAgo(r.completed_at || r.updated_at);
    const count = r.collected_products ? `${r.collected_products} 件` : '';
    if (r.status === 'completed') {
      setChip(`⚠️ ${by} ${when}採過 ${count}`, '#7c2d12');
      if (btn) { btn.style.background = '#7c2d12'; btn.textContent = '已採(點重採)'; }
    } else if (r.status === 'collecting') {
      setChip(`⏳ ${by} 正在採集中(${when}開始)`, '#1e3a5f');
      if (btn) { btn.style.background = '#1e3a5f'; btn.textContent = '採集中...'; btn.disabled = true; }
    } else {
      setChip(`✗ ${by} ${when}失敗(${r.status})`, '#422006');
      if (btn) { btn.style.background = '#f59e0b'; btn.textContent = '重試採集'; }
    }
  }

  // ---- SPA navigation detection ----
  let lastUrl = location.href;
  setInterval(() => {
    ensureButton();
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      STATE.lastCheckedId = null;
      STATE.lastResult = null;
    }
    refreshStatus();
  }, 1500);
})();
