const Database = require('better-sqlite3');
const path = require('path');
const fs = require('fs');

const DATA_DIR = path.join(__dirname, '..', 'data');
const DB_PATH = path.join(DATA_DIR, 'collector.db');

let _db = null;

function getDb() {
  if (_db) return _db;
  fs.mkdirSync(DATA_DIR, { recursive: true });
  _db = new Database(DB_PATH);
  _db.pragma('journal_mode = WAL');
  _db.pragma('foreign_keys = ON');
  migrate(_db);
  return _db;
}

function migrate(db) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS shops (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL DEFAULT '',
      url TEXT NOT NULL DEFAULT '',
      type TEXT NOT NULL DEFAULT 'personal',
      total_products INTEGER NOT NULL DEFAULT 0,
      collected_products INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
      updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
    );

    CREATE TABLE IF NOT EXISTS products (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      mercari_id TEXT NOT NULL UNIQUE,
      shop_id TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT '',
      price INTEGER NOT NULL DEFAULT 0,
      condition TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      category_id TEXT NOT NULL DEFAULT '',
      images TEXT NOT NULL DEFAULT '[]',
      source_url TEXT NOT NULL DEFAULT '',
      local_images TEXT NOT NULL DEFAULT '[]',
      images_downloaded INTEGER NOT NULL DEFAULT 0,
      collected_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
      exported INTEGER NOT NULL DEFAULT 0,
      previous_ids TEXT NOT NULL DEFAULT '[]',
      FOREIGN KEY (shop_id) REFERENCES shops(id)
    );

    CREATE TABLE IF NOT EXISTS config (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_products_shop_id ON products(shop_id);
    CREATE INDEX IF NOT EXISTS idx_products_mercari_id ON products(mercari_id);
  `);

  // Add previous_ids column if missing (migration for existing DBs)
  try {
    db.prepare("SELECT previous_ids FROM products LIMIT 0").get();
  } catch (_) {
    db.exec("ALTER TABLE products ADD COLUMN previous_ids TEXT NOT NULL DEFAULT '[]'");
  }

  // Add photo_hash column for tracking products across ID changes
  try {
    db.prepare("SELECT photo_hash FROM products LIMIT 0").get();
  } catch (_) {
    db.exec("ALTER TABLE products ADD COLUMN photo_hash TEXT NOT NULL DEFAULT ''");
    db.exec("CREATE INDEX IF NOT EXISTS idx_products_photo_hash ON products(photo_hash)");
  }

  // seed default config
  const upsertCfg = db.prepare('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)');
  upsertCfg.run('imageBase', '');
  upsertCfg.run('useProxy', '1');
  upsertCfg.run('coordinatorEnabled', '1');
  upsertCfg.run('coordinatorUrl', '');
  upsertCfg.run('clientId', '');
  upsertCfg.run('coordinatorMode', 'strict');
}

// ---- config ----
function getConfig(key) {
  const db = getDb();
  const row = db.prepare('SELECT value FROM config WHERE key = ?').get(key);
  return row ? row.value : '';
}

function setConfig(key, value) {
  const db = getDb();
  db.prepare('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)').run(key, String(value));
}

function getAllConfig() {
  const db = getDb();
  const rows = db.prepare('SELECT key, value FROM config').all();
  const out = {};
  for (const r of rows) out[r.key] = r.value;
  return out;
}

// ---- shops ----
function upsertShop({ id, name, url, type }) {
  const db = getDb();
  const existing = db.prepare('SELECT id FROM shops WHERE id = ?').get(id);
  if (existing) {
    db.prepare("UPDATE shops SET name=?, url=?, type=?, updated_at=datetime('now','localtime') WHERE id=?")
      .run(name || '', url || '', type || 'personal', id);
  } else {
    db.prepare('INSERT INTO shops (id, name, url, type) VALUES (?, ?, ?, ?)')
      .run(id, name || '', url || '', type || 'personal');
  }
}

function updateShopStatus(id, status, total, collected) {
  const db = getDb();
  const sets = ["status=?", "updated_at=datetime('now','localtime')"];
  const params = [status];
  if (total !== undefined) { sets.push('total_products=?'); params.push(total); }
  if (collected !== undefined) { sets.push('collected_products=?'); params.push(collected); }
  params.push(id);
  db.prepare(`UPDATE shops SET ${sets.join(',')} WHERE id=?`).run(...params);
}

function getShop(id) {
  return getDb().prepare('SELECT * FROM shops WHERE id = ?').get(id);
}

function listShops() {
  return getDb().prepare('SELECT * FROM shops ORDER BY created_at DESC').all();
}

function deleteShop(id) {
  const db = getDb();
  db.prepare('DELETE FROM products WHERE shop_id = ?').run(id);
  db.prepare('DELETE FROM shops WHERE id = ?').run(id);
}

// ---- photo hash: stable fingerprint from first image URL ----
// Image URLs contain unique asset hashes that survive product ID changes
// e.g. https://assets.mercari-shops-static.com/-/large/plain/2JKWsYbvafNVJtZjWxrSb2.jpg
// e.g. https://static.mercdn.net/item/detail/orig/photos/m96393024291_1.jpg
function extractPhotoHash(images) {
  if (!Array.isArray(images) || !images.length) return '';
  const url = String(images[0] || '');
  // Shops: extract the hash before extension (e.g. 2JKWsYbvafNVJtZjWxrSb2)
  const m1 = url.match(/\/([A-Za-z0-9_-]{15,})\.\w+(?:@\w+)?$/);
  if (m1) return m1[1];
  // Personal: extract photo filename (e.g. m96393024291_1)
  const m2 = url.match(/\/(m\d+_\d+)\.\w+/);
  if (m2) return m2[1];
  return '';
}

// ---- products ----
function upsertProduct(p) {
  const db = getDb();
  const photoHash = extractPhotoHash(p.images);

  const existing = db.prepare('SELECT id, mercari_id, previous_ids FROM products WHERE mercari_id = ?').get(p.mercari_id);
  if (existing) {
    db.prepare(`UPDATE products SET
      title=?, price=?, condition=?, description=?, category_id=?,
      images=?, source_url=?, photo_hash=?, collected_at=datetime('now','localtime')
      WHERE mercari_id=?`).run(
      p.title || '', p.price || 0, p.condition || '', p.description || '',
      p.category_id || '', JSON.stringify(p.images || []),
      p.source_url || '', photoHash, p.mercari_id
    );
    return { id: existing.id, matched: 'exact' };
  }

  // For shop products: try to match existing product whose ID changed
  // Priority: 1) photo hash (most reliable) 2) title (fallback)
  if (p.shop_id) {
    let oldRecord = null;
    let matchType = '';

    // Match by first photo hash — photos survive ID changes even if title is edited
    if (photoHash) {
      oldRecord = db.prepare(
        'SELECT id, mercari_id, previous_ids FROM products WHERE shop_id = ? AND photo_hash = ? AND mercari_id != ?'
      ).get(p.shop_id, photoHash, p.mercari_id);
      if (oldRecord) matchType = 'photo';
    }

    // Fallback: match by title
    if (!oldRecord && p.title) {
      oldRecord = db.prepare(
        'SELECT id, mercari_id, previous_ids FROM products WHERE shop_id = ? AND title = ? AND mercari_id != ?'
      ).get(p.shop_id, p.title, p.mercari_id);
      if (oldRecord) matchType = 'title';
    }

    if (oldRecord) {
      const prevIds = JSON.parse(oldRecord.previous_ids || '[]');
      if (!prevIds.includes(oldRecord.mercari_id)) {
        prevIds.push(oldRecord.mercari_id);
      }
      console.log(`[db] URL tracking: ${oldRecord.mercari_id} → ${p.mercari_id} (${matchType} match: ${(p.title || '').slice(0, 30)})`);

      db.prepare(`UPDATE products SET
        mercari_id=?, title=?, price=?, condition=?, description=?, category_id=?,
        images=?, source_url=?, photo_hash=?, previous_ids=?, collected_at=datetime('now','localtime')
        WHERE id=?`).run(
        p.mercari_id, p.title || '', p.price || 0, p.condition || '', p.description || '',
        p.category_id || '', JSON.stringify(p.images || []),
        p.source_url || '', photoHash, JSON.stringify(prevIds), oldRecord.id
      );
      return { id: oldRecord.id, matched: matchType, oldId: oldRecord.mercari_id };
    }
  }

  const info = db.prepare(`INSERT INTO products
    (mercari_id, shop_id, title, price, condition, description, category_id, images, source_url, photo_hash)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).run(
    p.mercari_id, p.shop_id || '', p.title || '', p.price || 0,
    p.condition || '', p.description || '', p.category_id || '',
    JSON.stringify(p.images || []), p.source_url || '', photoHash
  );
  return { id: info.lastInsertRowid, matched: 'new' };
}

function markImagesDownloaded(mercariId, localImages) {
  getDb().prepare('UPDATE products SET images_downloaded=1, local_images=? WHERE mercari_id=?')
    .run(JSON.stringify(localImages || []), mercariId);
}

function getProduct(mercariId) {
  const db = getDb();
  // Check current ID first, then check previous_ids for URL-tracked products
  const exact = db.prepare('SELECT * FROM products WHERE mercari_id = ?').get(mercariId);
  if (exact) return exact;
  // Search in previous_ids (for shop products whose IDs changed)
  const all = db.prepare("SELECT * FROM products WHERE previous_ids LIKE ?").get(`%${mercariId}%`);
  if (all) {
    const prevIds = JSON.parse(all.previous_ids || '[]');
    if (prevIds.includes(mercariId)) return all;
  }
  return null;
}

function listProducts(shopId) {
  return getDb().prepare('SELECT * FROM products WHERE shop_id = ? ORDER BY collected_at DESC').all(shopId);
}

function listAllProducts() {
  return getDb().prepare('SELECT * FROM products ORDER BY collected_at DESC').all();
}

function countCollected(shopId) {
  const row = getDb().prepare('SELECT COUNT(*) as c FROM products WHERE shop_id = ?').get(shopId);
  return row ? row.c : 0;
}

function deleteProducts(ids) {
  if (!ids || !ids.length) return;
  const db = getDb();
  const placeholders = ids.map(() => '?').join(',');
  db.prepare(`DELETE FROM products WHERE id IN (${placeholders})`).run(...ids);
}

// Clean up products with no data (sold/removed items)
function cleanupEmptyProducts() {
  const db = getDb();
  const result = db.prepare("DELETE FROM products WHERE title = '' AND price = 0").run();
  return result.changes;
}

function getProductsByIds(ids) {
  if (!ids || !ids.length) return [];
  const db = getDb();
  const placeholders = ids.map(() => '?').join(',');
  return db.prepare(`SELECT * FROM products WHERE id IN (${placeholders})`).all(...ids);
}

function listProductsByShopIds(shopIds) {
  if (!Array.isArray(shopIds) || !shopIds.length) return [];
  const db = getDb();
  const placeholders = shopIds.map(() => '?').join(',');
  return db.prepare(
    `SELECT * FROM products WHERE shop_id IN (${placeholders}) ORDER BY shop_id, collected_at DESC`
  ).all(...shopIds);
}

function close() {
  if (_db) { _db.close(); _db = null; }
}

module.exports = {
  getDb, getConfig, setConfig, getAllConfig,
  upsertShop, updateShopStatus, getShop, listShops, deleteShop,
  upsertProduct, markImagesDownloaded, getProduct, listProducts, listAllProducts,
  countCollected, deleteProducts, cleanupEmptyProducts, getProductsByIds,
  listProductsByShopIds,
  close, DATA_DIR,
};
