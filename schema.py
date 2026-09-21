"""Схема БД воркера (таблицы, которые пишет gift_watcher.py и читает бот).
Отдельный файл без зависимостей: его импортируют и воркер, и бот, и демо."""

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS collections (
    gift_id INTEGER PRIMARY KEY,
    title TEXT,
    floor_stars INTEGER,
    on_resale INTEGER,
    updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS listings (
    slug TEXT PRIMARY KEY,
    gift_id INTEGER, title TEXT, num INTEGER,
    price_stars INTEGER, price_ton INTEGER,
    owner_key TEXT, owner_label TEXT,
    model TEXT, model_rarity TEXT,
    backdrop TEXT, backdrop_rarity TEXT,
    symbol TEXT, symbol_rarity TEXT,
    orig_sender TEXT, orig_recipient TEXT, orig_date INTEGER,
    first_seen INTEGER, last_seen INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listings_gift_price ON listings (gift_id, price_stars);
CREATE TABLE IF NOT EXISTS owner_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT, owner_key TEXT, owner_label TEXT, seen_at INTEGER
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT, price_stars INTEGER, price_ton INTEGER, seen_at INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, type TEXT, slug TEXT, gift_id INTEGER, title TEXT, num INTEGER,
    old_price INTEGER, new_price INTEGER, currency TEXT,
    owner_label TEXT, url TEXT, sent INTEGER DEFAULT 0
);
"""
