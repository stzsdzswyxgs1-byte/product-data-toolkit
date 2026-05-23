const axios = require('axios');
const fs = require('fs');
const path = require('path');
const { HttpsProxyAgent } = require('https-proxy-agent');

const SOCKS_URL = process.env.SOCKS_URL
  || `${process.env.SOCKS_SCHEME || 'socks5h'}://${process.env.SOCKS_HOST || '127.0.0.1'}:${process.env.SOCKS_PORT || '10808'}`;
const proxyAgent = new HttpsProxyAgent(SOCKS_URL);

const DATA_DIR = path.join(__dirname, '..', 'data');
const DEFAULT_IMAGES_DIR = path.join(DATA_DIR, 'images');

let _imagesDir = DEFAULT_IMAGES_DIR;

function setImagesDir(dir) {
  _imagesDir = dir || DEFAULT_IMAGES_DIR;
}

function getImagesDir() {
  return _imagesDir;
}

const HEADERS = {
  'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
  'Accept-Language': 'ja,en-US;q=0.9,en;q=0.8',
};

function sanitizeName(s) {
  let name = String(s || 'no-title').replace(/[\\/:*?"<>|]/g, '').replace(/\s+/g, ' ').trim();
  // Truncate to 80 chars to stay within Windows MAX_PATH (260)
  if (name.length > 80) name = name.slice(0, 80).trim();
  return name || 'no-title';
}

function makeImageFilenames(title, count) {
  const names = [];
  for (let i = 1; i <= count; i++) {
    names.push(i === 1 ? `${title}.jpg` : `${title} (${i}).jpg`);
  }
  return names;
}

async function downloadImage(url, filePath, referer, useProxy, log) {
  const fname = path.basename(filePath);
  const opts = {
    responseType: 'arraybuffer',
    timeout: 30000,
    headers: { ...HEADERS, Referer: referer || 'https://jp.mercari.com/' },
  };
  if (useProxy) opts.httpsAgent = proxyAgent;

  const dir = path.dirname(filePath);
  fs.mkdirSync(dir, { recursive: true });

  // Skip if already downloaded
  if (fs.existsSync(filePath)) {
    const stat = fs.statSync(filePath);
    if (log) log(`  圖片跳過（已存在 ${(stat.size/1024).toFixed(1)}KB）: ${fname}`);
    return 'skipped';
  }

  // Retry once on failure
  for (let attempt = 0; attempt < 2; attempt++) {
    const t0 = Date.now();
    try {
      const res = await axios.get(url, opts);
      fs.writeFileSync(filePath, res.data);
      const size = res.data.length;
      const ms = Date.now() - t0;
      if (log) log(`  圖片下載成功: ${fname} | ${(size/1024).toFixed(1)}KB | ${ms}ms${attempt > 0 ? ' (重試成功)' : ''}`);
      return 'ok';
    } catch (err) {
      const ms = Date.now() - t0;
      const status = err.response?.status || '';
      if (attempt === 0) {
        if (log) log(`  圖片下載失敗（第1次）: ${fname} | ${ms}ms | HTTP ${status} | ${err.message} → 1秒後重試`);
        await new Promise(r => setTimeout(r, 1000));
      } else {
        if (log) log(`  圖片下載失敗（重試也失敗）: ${fname} | ${ms}ms | HTTP ${status} | ${err.message}`);
        throw err;
      }
    }
  }
}

// Concurrent download with limit
async function downloadProductImages(product, useProxy, concurrency = 5, log) {
  const images = product.images || [];
  if (!images.length) {
    if (log) log(`  此商品無圖片`);
    return [];
  }

  const safeTitle = sanitizeName(product.title);
  const folder = path.join(_imagesDir, safeTitle);
  const filenames = makeImageFilenames(safeTitle, images.length);
  const localPaths = [];

  if (log) log(`  開始下載 ${images.length} 張圖片 → ${folder}`);

  let okCount = 0, skipCount = 0, failCount = 0;
  const t0 = Date.now();

  // Process in batches
  let i = 0;
  let batchNum = 0;
  while (i < images.length) {
    batchNum++;
    const batch = [];
    const batchStart = i;
    for (let j = 0; j < concurrency && i < images.length; j++, i++) {
      const idx = i;
      const fp = path.join(folder, filenames[idx]);
      localPaths.push(fp);
      batch.push(
        downloadImage(images[idx], fp, product.source_url, useProxy, log)
          .then(result => {
            if (result === 'skipped') skipCount++;
            else okCount++;
          })
          .catch(err => {
            failCount++;
            console.error(`[download] failed: ${filenames[idx]} - ${err.message}`);
          })
      );
    }
    await Promise.all(batch);
  }

  const elapsed = Date.now() - t0;
  if (log) log(`  圖片下載完成: ${okCount}成功 ${skipCount}跳過 ${failCount}失敗 | ${elapsed}ms`);

  return filenames.map(n => path.join(safeTitle, n));
}

module.exports = {
  downloadProductImages,
  sanitizeName,
  makeImageFilenames,
  setImagesDir,
  getImagesDir,
  DEFAULT_IMAGES_DIR,
};
