const axios = require('axios');
const crypto = require('crypto');
const dns = require('dns');
try { dns.setDefaultResultOrder('ipv4first'); } catch (_) {}
const { HttpsProxyAgent } = require('https-proxy-agent');

// ---- proxy setup ----
const SOCKS_HOST = process.env.SOCKS_HOST || '127.0.0.1';
const SOCKS_PORT = process.env.SOCKS_PORT || '10808';
const SOCKS_SCHEME = process.env.SOCKS_SCHEME || 'socks5h';
const SOCKS_URL = process.env.SOCKS_URL || `${SOCKS_SCHEME}://${SOCKS_HOST}:${SOCKS_PORT}`;
const proxyAgent = new HttpsProxyAgent(SOCKS_URL);

// ---- DPOP token generation ----
const { publicKey: dpopPub, privateKey: dpopPriv } = crypto.generateKeyPairSync('ec', { namedCurve: 'P-256' });
const dpopJwk = dpopPub.export({ format: 'jwk' });

function createDpopToken(method, url) {
  const header = { typ: 'dpop+jwt', alg: 'ES256', jwk: { kty: dpopJwk.kty, crv: dpopJwk.crv, x: dpopJwk.x, y: dpopJwk.y } };
  const payload = { iat: Math.floor(Date.now() / 1000), jti: crypto.randomUUID(), htu: url, htm: method };
  const hB = Buffer.from(JSON.stringify(header)).toString('base64url');
  const pB = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const sig = crypto.sign('sha256', Buffer.from(`${hB}.${pB}`), { key: dpopPriv, dsaEncoding: 'ieee-p1363' });
  return `${hB}.${pB}.${sig.toString('base64url')}`;
}

// ---- common headers ----
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

const API_HEADERS = {
  'User-Agent': UA,
  'Accept': 'application/json, text/plain, */*',
  'Content-Type': 'application/json',
  'X-Platform': 'web',
  'Origin': 'https://jp.mercari.com',
  'Referer': 'https://jp.mercari.com/',
};

const HTML_HEADERS = {
  'User-Agent': UA,
  'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
  'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
  'Cache-Control': 'no-cache',
};

// ---- API helpers ----
function apiOpts(method, url, useProxy) {
  const opts = {
    timeout: 30000,
    headers: { ...API_HEADERS, Dpop: createDpopToken(method, url) },
  };
  if (useProxy) opts.httpsAgent = proxyAgent;
  return opts;
}

async function fetchHtml(url, useProxy) {
  const opts = { timeout: 30000, headers: { ...HTML_HEADERS, Referer: url }, responseType: 'text' };
  if (useProxy) opts.httpsAgent = proxyAgent;
  const res = await axios.get(url, opts);
  return typeof res.data === 'string' ? res.data : String(res.data || '');
}

// ---- parse seller URL ----
function parseSellerUrl(url) {
  // Strip language prefix (e.g. /en/, /zh-Hant/, /ko/) for Taiwan/overseas access
  const u = String(url || '').trim().replace(/mercari\.com\/(?:en|zh-Hant|zh-TW|ko)\//, 'mercari.com/');
  // New format: jp.mercari.com/user/profile/{id}
  let m = u.match(/\/user\/profile\/([A-Za-z0-9_-]+)/);
  if (m) return { sellerId: m[1], type: 'personal' };
  m = u.match(/\/shops\/profile\/([A-Za-z0-9_-]+)/);
  if (m) return { sellerId: m[1], type: 'shop' };
  m = u.match(/\/profile\/([A-Za-z0-9_-]+)/);
  if (m) return { sellerId: m[1], type: 'personal' };
  // Old format: mercari.com/jp/u/{id}
  m = u.match(/\/jp\/u\/(\d+)/);
  if (m) return { sellerId: m[1], type: 'personal' };
  // Product URLs → need to look up seller
  // New format: /item/m{id}
  m = u.match(/\/items?\/(m[A-Za-z0-9]+)/);
  if (m) return { productId: m[1], type: 'product' };
  m = u.match(/\/shops\/product\/([A-Za-z0-9_-]+)/);
  if (m) return { productId: m[1], type: 'product' };
  // mercari-shops.com/products/{id}
  m = u.match(/mercari-shops\.com\/products\/([A-Za-z0-9_-]+)/);
  if (m) return { productId: m[1], type: 'product' };
  return null;
}

// ---- resolve product URL to seller ----
async function resolveProductToSeller(productId, useProxy) {
  // Method 1: personal item API (works for /item/m* IDs)
  if (/^m\d+$/.test(productId)) {
    try {
      const detailUrl = `https://api.mercari.jp/items/get?id=${encodeURIComponent(productId)}`;
      const res = await axios.get(detailUrl, apiOpts('GET', detailUrl, useProxy));
      const d = res.data?.data;
      if (d && d.seller && d.seller.id) {
        return { sellerId: String(d.seller.id), type: 'personal', productName: d.name || '' };
      }
    } catch (_) {}
  }

  // Method 2: get title from HTML, then search API to find seller
  let title = '';
  try {
    const productUrl = productId.startsWith('m')
      ? `https://jp.mercari.com/item/${productId}`
      : `https://jp.mercari.com/shops/product/${productId}`;
    const html = await fetchHtml(productUrl, useProxy);
    const m = html.match(/property="og:title"\s+content="([^"]*)"/i)
           || html.match(/name="og:title"\s+content="([^"]*)"/i);
    if (m) title = decodeHtmlEntities(m[1]).replace(/\s*-\s*[^-]+$/, '').trim();
  } catch (_) {}

  if (!title) return null;

  // Search with title as keyword to find the exact product
  const apiUrl = 'https://api.mercari.jp/v2/entities:search';
  const body = {
    pageSize: 30,
    searchSessionId: crypto.randomUUID(),
    searchCondition: {
      keyword: title.slice(0, 60), excludeKeyword: '',
      sort: 'SORT_SCORE', order: 'ORDER_DESC',
      status: ['STATUS_ON_SALE'],
      sizeId: [], categoryId: [], brandId: [], sellerId: [],
      priceMin: 0, priceMax: 0,
      itemConditionId: [], shippingPayerId: [],
      shippingFromArea: [], shippingMethod: [],
      colorId: [], hasCoupon: false,
      attributes: [], itemTypes: [], skuIds: [],
    },
    defaultDatasets: ['DATASET_TYPE_MERCARI', 'DATASET_TYPE_BEYOND'],
    serviceFrom: 'suruga',
    withItemBrand: true, withItemSize: false,
    withItemPromotions: true, withItemSizes: true, withShopname: true,
  };

  try {
    const res = await axios.post(apiUrl, body, apiOpts('POST', apiUrl, useProxy));
    const items = res.data.items || [];
    const match = items.find(i => i.id === productId);
    if (match && match.sellerId) {
      const isShop = match.itemType === 'ITEM_TYPE_BEYOND' || !!match.shop;
      return {
        sellerId: match.sellerId,
        type: isShop ? 'shop' : 'personal',
        productName: match.name || '',
      };
    }
  } catch (_) {}

  return null;
}

// ---- condition ID → text ----
const CONDITION_MAP = {
  1: '新品、未使用', 2: '未使用に近い', 3: '目立った傷や汚れなし',
  4: 'やや傷や汚れあり', 5: '傷や汚れあり', 6: '全体的に状態が悪い',
};

// ---- fetch seller products via Mercari search API ----
async function fetchSellerProductsFromSearch(sellerId, sellerType, useProxy, onProgress) {
  const apiUrl = 'https://api.mercari.jp/v2/entities:search';
  const products = [];
  const seen = new Set();
  let pageToken = '';
  let pageNum = 0;

  if (onProgress) onProgress(`代理: ${useProxy ? SOCKS_URL : '不使用'}`);
  if (onProgress) onProgress(`API: POST ${apiUrl}`);
  if (onProgress) onProgress(`賣家ID: ${sellerId} | 類型: ${sellerType}`);

  while (true) {
    pageNum++;
    if (onProgress) onProgress(`正在獲取商品列表：第 ${pageNum} 頁...`);

    const body = {
      pageSize: 120,
      searchSessionId: crypto.randomUUID(),
      searchCondition: {
        keyword: '', excludeKeyword: '',
        sort: 'SORT_CREATED_TIME', order: 'ORDER_DESC',
        status: ['STATUS_ON_SALE'],
        sizeId: [], categoryId: [], brandId: [],
        sellerId: [sellerId],
        priceMin: 0, priceMax: 0,
        itemConditionId: [], shippingPayerId: [],
        shippingFromArea: [], shippingMethod: [],
        colorId: [], hasCoupon: false,
        attributes: [], itemTypes: ['ITEM_TYPE_MERCARI'], skuIds: [],
      },
      defaultDatasets: ['DATASET_TYPE_MERCARI', 'DATASET_TYPE_BEYOND'],
      serviceFrom: 'suruga',
      withItemBrand: true,
      withItemSize: false,
      withItemPromotions: true,
      withItemSizes: true,
      withShopname: false,
    };
    if (pageToken) body.pageToken = pageToken;

    let data;
    const t0 = Date.now();
    try {
      const res = await axios.post(apiUrl, body, apiOpts('POST', apiUrl, useProxy));
      data = res.data;
      if (onProgress) onProgress(`第 ${pageNum} 頁請求成功 | ${(Date.now() - t0)}ms | 返回 ${(data.items||[]).length} 件 | 總計 ${data.meta?.numFound || '?'} 件`);
    } catch (e) {
      const status = e.response?.status || '';
      const errMsg = e.response?.data?.message || e.message;
      console.error(`[mercari] API search error (page ${pageNum}):`, errMsg);
      if (onProgress) onProgress(`✗ 第 ${pageNum} 頁請求失敗 | ${(Date.now() - t0)}ms | HTTP ${status} | ${errMsg}`);
      if (pageNum === 1) {
        let msg;
        if (e.message.includes('ECONNREFUSED')) {
          msg = `代理連接失敗 (${SOCKS_URL})，請檢查代理是否已啟動，或關閉「使用代理」選項`;
        } else if (e.message.includes('ETIMEDOUT') || e.message.includes('timeout')) {
          msg = `連接超時，請檢查網絡或代理設置`;
        } else {
          msg = `搜索API請求失敗: HTTP ${status} ${errMsg}`;
        }
        throw new Error(msg);
      }
      break;
    }

    const items = data.items || [];
    let newCount = 0;
    for (const item of items) {
      const id = item.id || '';
      if (!id || seen.has(id)) continue;

      seen.add(id);
      newCount++;

      const isShop = item.itemType === 'ITEM_TYPE_BEYOND' || !!item.shop;
      const condId = parseInt(item.itemConditionId || 0, 10);

      // Extract image from search results (shops only get 1 from search)
      let searchImages = [];
      if (item.photos && item.photos.length) {
        searchImages = item.photos.map(p => {
          const url = (typeof p === 'string' ? p : p.uri || p.url || '');
          return url.split('?')[0].replace(/@\w+$/, '');
        }).filter(Boolean);
      }
      if (!searchImages.length && item.thumbnails && item.thumbnails.length) {
        searchImages = item.thumbnails.map(u =>
          u.replace('/-/small/', '/-/large/').split('?')[0].replace(/@\w+$/, '')
        ).filter(Boolean);
      }

      products.push({
        mercari_id: id,
        source_url: isShop
          ? `https://jp.mercari.com/shops/product/${id}`
          : `https://jp.mercari.com/item/${id}`,
        type: isShop ? 'shop' : 'personal',
        title: item.name || '',
        price: parseInt(item.price || 0, 10) || 0,
        categoryId: item.categoryId || '',
        itemConditionId: item.itemConditionId || '',
        condition: CONDITION_MAP[condId] || '',
        searchImages,
      });
    }

    if (onProgress) onProgress(`第 ${pageNum} 頁：${newCount} 件新商品（累計 ${products.length}）`);

    pageToken = data.meta?.nextPageToken || '';
    if (!pageToken || newCount === 0) {
      if (onProgress) onProgress(pageToken ? '已全部獲取（無新商品）' : '已到最後一頁');
      break;
    }
    await sleep(300);
  }

  return products;
}

// ---- fetch Shops product detail via v1 API ----
async function fetchShopProductDetail(productId, useProxy) {
  const apiUrl = `https://api.mercari.jp/v1/marketplaces/shops/products/${encodeURIComponent(productId)}?view=FULL`;
  console.log(`[mercari] fetchShopProductDetail: GET ${apiUrl}`);
  const t0 = Date.now();

  try {
    const res = await axios.get(apiUrl, apiOpts('GET', apiUrl.split('?')[0], useProxy));
    const d = res.data;
    const ms = Date.now() - t0;

    // Extract photos - strip @webp suffix to get JPEG
    const photos = (d.productDetail?.photos || []).map(url => {
      return String(url || '').replace(/@\w+$/, '');
    }).filter(Boolean);

    const price = parseInt(d.price || 0, 10) || 0;

    // Get sale price if exists
    const salePrice = parseInt(d.productDetail?.timeSaleDetails?.price || 0, 10) || 0;

    console.log(`[mercari] shop detail OK | ${ms}ms | ${photos.length} photos | ¥${price} | ${d.displayName?.slice(0, 40)}`);

    return {
      title: d.displayName || '',
      price: salePrice > 0 ? salePrice : price,
      condition: '',
      description: d.productDetail?.description || '',
      images: photos,
      category_id: '',
      source_url: `https://jp.mercari.com/shops/product/${productId}`,
      isAuction: false,
      shopProductName: d.name || productId,
      createTime: d.createTime || '',
      updateTime: d.updateTime || '',
    };
  } catch (e) {
    const ms = Date.now() - t0;
    const status = e.response?.status || '';
    console.error(`[mercari] shop detail FAIL | ${ms}ms | HTTP ${status} | ${e.message}`);

    // Fallback to mercari-shops.com HTML which has all images
    return fetchShopProductFromShopsSite(productId, useProxy);
  }
}

// ---- fallback: mercari-shops.com product page (has all images server-rendered) ----
async function fetchShopProductFromShopsSite(productId, useProxy) {
  const pageUrl = `https://mercari-shops.com/products/${encodeURIComponent(productId)}`;
  console.log(`[mercari] fetchShopProductFromShopsSite: ${pageUrl}`);

  try {
    const html = await fetchHtml(pageUrl, useProxy);

    // Extract images
    const imgRe = /https?:\/\/assets\.mercari-shops-static\.com\/-\/large\/plain\/[A-Za-z0-9_-]+\.\w+/gi;
    const images = [];
    const seen = new Set();
    let m;
    while ((m = imgRe.exec(html)) !== null) {
      const u = m[0];
      if (!seen.has(u)) { seen.add(u); images.push(u); }
    }

    // Extract title from og:title
    const titleMatch = html.match(/property="og:title"\s+content="([^"]*)"/i);
    let title = titleMatch ? decodeHtmlEntities(titleMatch[1]) : '';
    // Remove shop name suffix
    title = title.replace(/\s*-\s*[^-]+$/, '').trim();

    // Extract price
    let price = 0;
    const priceMatch = html.match(/[¥￥][\s]*([0-9,]+)/);
    if (priceMatch) price = parseInt(priceMatch[1].replace(/,/g, ''), 10) || 0;

    console.log(`[mercari] shops site fallback: ${images.length} images | ¥${price} | ${title.slice(0, 40)}`);

    return {
      title,
      price,
      condition: '',
      description: '',
      images,
      category_id: '',
      source_url: `https://jp.mercari.com/shops/product/${productId}`,
      isAuction: false,
    };
  } catch (e) {
    console.error(`[mercari] shops site fallback FAIL: ${e.message}`);
    return null;
  }
}

// ---- fetch product detail via API ----
async function fetchProductDetail(urlOrId, useProxy) {
  // Extract item ID from URL or use as-is
  let itemId = urlOrId;
  const m = String(urlOrId).match(/\/item\/(m[0-9]+)/);
  if (m) itemId = m[1];

  const apiUrl = `https://api.mercari.jp/items/get?id=${encodeURIComponent(itemId)}&include_auction=true`;

  try {
    const res = await axios.get(apiUrl, apiOpts('GET', 'https://api.mercari.jp/items/get', useProxy));
    const d = res.data?.data;
    if (!d) return null;

    // Extract full-size photo URLs
    const images = (d.photos || []).map(u => {
      if (typeof u === 'string') return u.split('?')[0];
      return '';
    }).filter(Boolean).slice(0, 10);

    // Category: use the deepest category
    let categoryId = '';
    if (d.item_category_ntiers) {
      categoryId = String(d.item_category_ntiers.id || '');
    } else if (d.item_category) {
      categoryId = String(d.item_category.id || '');
    }

    const condition = d.item_condition?.name || CONDITION_MAP[d.item_condition?.id] || '';

    // Check for auction — 关键字段: auction_info (需要 include_auction=true 参数)
    const isAuction = !!(d.auction_info);

    if (isAuction) {
      console.log(`[${itemId}] 拍卖商品 — auction_info:`, JSON.stringify(d.auction_info));
    }

    return {
      title: d.name || '',
      price: parseInt(d.price || 0, 10) || 0,
      condition,
      description: d.description || '',
      images,
      category_id: categoryId,
      source_url: `https://jp.mercari.com/item/${itemId}`,
      isAuction,
    };
  } catch (e) {
    // Fallback: try HTML scraping for shops or if API fails
    console.error(`[mercari] API detail error for ${itemId}:`, e.message);
    return fetchProductDetailFromHtml(urlOrId, useProxy);
  }
}

// ---- fallback: HTML-based detail fetch ----
async function fetchProductDetailFromHtml(url, useProxy) {
  if (!url.startsWith('http')) {
    url = `https://jp.mercari.com/item/${url}`;
  }

  const html = await fetchHtml(url, useProxy);
  const itemId = url.match(/item\/([^/?]+)/)?.[1];

  // Try __NEXT_DATA__ (legacy — may not exist on newer pages)
  const ndMatch = html.match(/<script\s+id="__NEXT_DATA__"[^>]*>([\s\S]*?)<\/script>/i);
  if (ndMatch) {
    try {
      const nextData = JSON.parse(ndMatch[1]);
      const detail = extractProductFromNextData(nextData, url);
      if (detail && detail.title) return detail;
    } catch (_) {}
  }

  // Meta tag fallback
  const getMeta = (prop) => {
    const m = html.match(new RegExp(`(?:property|name)="${prop}"\\s+content="([^"]*)"`, 'i'));
    return m ? m[1] : '';
  };

  const title = getMeta('og:title') || (() => {
    const m = html.match(/<h1[^>]*>([^<]+)<\/h1>/i);
    return m ? m[1].trim() : '';
  })();

  let price = 0;
  const priceStr = getMeta('product:price:amount') || getMeta('og:price:amount');
  if (priceStr) price = parseInt(priceStr.replace(/[^\d]/g, ''), 10) || 0;

  const images = [];
  const ogImg = getMeta('og:image');
  if (ogImg) images.push(ogImg.split('?')[0]);

  const imgRe = /src="(https?:\/\/[^"]*mercdn[^"]*photos?[^"]*)"/gi;
  let im;
  while ((im = imgRe.exec(html)) !== null) {
    const u = im[1].split('?')[0];
    if (!images.includes(u)) images.push(u);
  }

  // Don't use broad HTML includes for auction detection - causes false positives
  // Only rely on structured data (API / __NEXT_DATA__)

  return {
    title: decodeHtmlEntities(title),
    price,
    condition: '',
    description: '',
    images: images.slice(0, 10),
    category_id: '',
    source_url: url,
    isAuction: false,
  };
}

// ---- deep search helper (for __NEXT_DATA__ parsing) ----
function deepFind(obj, predicate, maxDepth = 15) {
  const results = [];
  const seen = new WeakSet();
  function walk(o, depth) {
    if (!o || typeof o !== 'object' || depth > maxDepth || seen.has(o)) return;
    seen.add(o);
    if (predicate(o)) results.push(o);
    for (const v of Object.values(o)) {
      if (v && typeof v === 'object') walk(v, depth + 1);
    }
  }
  walk(obj, 0);
  return results;
}

function extractProductFromNextData(nextData, sourceUrl) {
  const candidates = deepFind(nextData, obj => {
    if (!obj || typeof obj !== 'object') return false;
    const hasPhotos = Array.isArray(obj.photos) || Array.isArray(obj.productPhotos);
    const hasName = obj.name || obj.productName || obj.title;
    const hasPrice = obj.price !== undefined || obj.sellPrice !== undefined;
    return hasPhotos && hasName && hasPrice;
  });

  if (!candidates.length) return null;

  let best = candidates[0];
  for (const c of candidates) {
    const score = (c.photos ? c.photos.length : 0) + (c.description ? 1 : 0) + (c.name ? 1 : 0);
    const bestScore = (best.photos ? best.photos.length : 0) + (best.description ? 1 : 0) + (best.name ? 1 : 0);
    if (score > bestScore) best = c;
  }

  // 打印完整对象用于调试（拍卖商品 vs 正常商品对比）
  if (best.id === 'm90000000004' || best.id === 'm90000000003' || best.id === 'm90000000006') {
    console.log(`\n========== [NEXT_DATA FULL OBJECT ${best.id}] ==========`);
    console.log(JSON.stringify(best, null, 2));
    console.log(`========== [END ${best.id}] ==========\n`);
  }

  let images = [];
  for (const arr of [best.photos, best.productPhotos, best.itemPhotos]) {
    if (Array.isArray(arr) && arr.length) {
      images = arr.map(p => {
        if (typeof p === 'string') return p;
        if (p && typeof p === 'object') return p.url || p.imageUrl || p.original || p.large || '';
        return '';
      }).filter(Boolean);
      if (images.length) break;
    }
  }
  images = images.map(u => u.split('?')[0]).slice(0, 10);

  let condition = '';
  if (best.itemCondition || best.condition) {
    const raw = best.itemCondition || best.condition;
    if (typeof raw === 'object' && raw.name) condition = raw.name;
    else if (typeof raw === 'object' && raw.id) condition = CONDITION_MAP[raw.id] || String(raw.id);
    else condition = String(raw);
  }

  return {
    title: best.name || best.productName || best.title || '',
    price: parseInt(best.price || best.sellPrice || 0, 10) || 0,
    condition,
    description: best.description || best.itemDescription || '',
    images,
    category_id: '',
    source_url: sourceUrl,
    isAuction: !!(
      best.isAuction ||
      (best.auction && typeof best.auction === 'object') ||
      best.biddingStatus ||
      best.auctionStatus ||
      best.itemType === 'auction' ||
      best.type === 'auction' ||
      best.saleType === 'auction' ||
      (best.status && String(best.status).toLowerCase().includes('auction')) ||
      (best.status && String(best.status).toLowerCase().includes('bidding'))
    ),
  };
}

function decodeHtmlEntities(str) {
  return String(str || '')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&#x27;/g, "'");
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

module.exports = {
  parseSellerUrl,
  resolveProductToSeller,
  fetchSellerProductsFromSearch,
  fetchProductDetail,
  fetchShopProductDetail,
  fetchHtml,
  proxyAgent,
  sleep,
};
