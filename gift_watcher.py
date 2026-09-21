#!/usr/bin/env python3
"""
gift_watcher.py - userbot-воркер для маркета подарков Telegram (MTProto, Telethon).
Только чтение: покупок и переводов нет. Событие содержит ссылку t.me/nft/<slug>,
покупает человек сам.

Раз в POLL_SECONDS:
  1. payments.getStarGifts -> нижняя цена (resell_min_stars) и число лотов по каждой коллекции.
  2. Для коллекций, где что-то изменилось (или которые в WATCH_GIFT_IDS), забирает самые
     дешёвые лоты: payments.getResaleStarGifts(sort_by_price).
  3. Сравнивает со снимком в SQLite и пишет события в таблицу events:
     new_listing, price_drop, floor_drop, owner_change.
Бот на aiogram потом читает listings/events из этой же БД (WAL, читать можно параллельно).

Запуск (всё делается с телефона: Termux для входа, Railway для постоянной работы):
  pip install -U telethon aiosqlite
  export TG_API_ID=... TG_API_HASH=...     # my.telegram.org, лучше отдельный аккаунт
  python gift_watcher.py login             # один раз: телефон, код, 2FA -> печатает TG_STRING_SESSION
  export TG_STRING_SESSION=...             # то, что напечатал login
  python gift_watcher.py once              # один цикл и вывод дешёвых лотов (проверка)
  python gift_watcher.py                   # только воркер, без бота
Воркер вместе с ботом запускает bot.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Optional

import aiosqlite
from telethon import TelegramClient, functions
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession

log = logging.getLogger("gift_watcher")

API_ID = int(os.getenv("TG_API_ID", "0"))
API_HASH = os.getenv("TG_API_HASH", "")
STRING_SESSION = os.getenv("TG_STRING_SESSION", "")  # получить: python gift_watcher.py login
DB_PATH = os.getenv("DB_PATH", "gifts.db")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
TOP_N = int(os.getenv("TOP_N", "20"))  # сколько самых дешёвых лотов брать по коллекции
MIN_DROP_PCT = float(os.getenv("MIN_DROP_PCT", "5"))  # порог падения цены для события, %
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.5"))  # пауза между запросами, лучше не уменьшать
DEEP_EVERY = int(os.getenv("DEEP_EVERY", "10"))  # раз в N циклов перечитывать все коллекции с лотами
WATCH_GIFT_IDS = {int(x) for x in os.getenv("WATCH_GIFT_IDS", "").split(",") if x.strip()}

from schema import SCHEMA  # noqa: E402  (таблицы воркера)


# ---------- разбор объектов Telegram (чистые функции, без сети) ----------

def peer_key(peer) -> Optional[str]:
    if peer is None:
        return None
    name = type(peer).__name__
    if name == "PeerUser":
        return f"u:{peer.user_id}"
    if name == "PeerChannel":
        return f"c:{peer.channel_id}"
    if name == "PeerChat":
        return f"g:{peer.chat_id}"
    return None


def build_labels(users, chats) -> dict:
    """Ключ пира -> человекочитаемое имя, из users/chats ответа."""
    labels = {}
    for u in users or []:
        username = getattr(u, "username", None)
        if username:
            label = f"@{username}"
        else:
            parts = (getattr(u, "first_name", None), getattr(u, "last_name", None))
            label = " ".join(p for p in parts if p) or f"id{u.id}"
        labels[f"u:{u.id}"] = label
    for c in chats or []:
        prefix = "c" if type(c).__name__ == "Channel" else "g"
        labels[f"{prefix}:{c.id}"] = getattr(c, "title", None) or f"id{c.id}"
    return labels


def owner_of(gift, labels):
    key = peer_key(getattr(gift, "owner_id", None))
    if key:
        return key, labels.get(key, key)
    address = getattr(gift, "owner_address", None)
    if address:
        return f"a:{address}", f"TON {address[:6]}...{address[-4:]}"
    name = getattr(gift, "owner_name", None)
    if name:
        return f"n:{name}", name
    return None, None


def extract_prices(resell_amount):
    """resell_amount: список StarsAmount (Stars) и/или StarsTonAmount (nanoTON)."""
    stars = ton = None
    for a in resell_amount or []:
        name = type(a).__name__
        if name == "StarsAmount":
            stars = int(a.amount)
        elif name == "StarsTonAmount":
            ton = int(a.amount)
    return stars, ton


def rarity_of(attr) -> Optional[str]:
    v = getattr(attr, "rarity_permille", None)
    if v is None:
        r = getattr(attr, "rarity", None)
        if r is not None:
            v = getattr(r, "permille", None)
            if v is None:
                return type(r).__name__.replace("StarGiftAttributeRarity", "").lower() or None
    return None if v is None else str(v)


def to_ts(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    return int(value)


def extract_attrs(attributes, labels) -> dict:
    out = {"model": None, "model_rarity": None, "backdrop": None, "backdrop_rarity": None,
           "symbol": None, "symbol_rarity": None,
           "orig_sender": None, "orig_recipient": None, "orig_date": None}
    for a in attributes or []:
        name = type(a).__name__
        if name == "StarGiftAttributeModel":
            out["model"], out["model_rarity"] = a.name, rarity_of(a)
        elif name == "StarGiftAttributeBackdrop":
            out["backdrop"], out["backdrop_rarity"] = a.name, rarity_of(a)
        elif name == "StarGiftAttributePattern":
            out["symbol"], out["symbol_rarity"] = a.name, rarity_of(a)
        elif name == "StarGiftAttributeOriginalDetails":
            s, r = peer_key(getattr(a, "sender_id", None)), peer_key(getattr(a, "recipient_id", None))
            out["orig_sender"] = labels.get(s, s) if s else None
            out["orig_recipient"] = labels.get(r, r) if r else None
            out["orig_date"] = to_ts(getattr(a, "date", None))
    return out


def parse_listing(gift, labels) -> dict:
    stars, ton = extract_prices(getattr(gift, "resell_amount", None))
    owner_key, owner_label = owner_of(gift, labels)
    row = {
        "slug": gift.slug, "gift_id": gift.gift_id, "title": gift.title, "num": gift.num,
        "price_stars": stars, "price_ton": ton,
        "owner_key": owner_key, "owner_label": owner_label,
    }
    row.update(extract_attrs(getattr(gift, "attributes", None), labels))
    return row


def price_of(row: dict):
    """(цена, валюта): Stars в приоритете, иначе TON в nanoTON."""
    if row.get("price_stars") is not None:
        return row["price_stars"], "stars"
    if row.get("price_ton") is not None:
        return row["price_ton"], "ton"
    return None, None


def dropped(old, new, min_pct: float) -> bool:
    return bool(old and new and new < old and (old - new) / old * 100 >= min_pct)


def diff_listing(old: Optional[dict], new: dict, min_pct: float):
    """Список событий (тип, старая цена, новая цена) для одного лота."""
    nv, nc = price_of(new)
    if old is None:
        return [("new_listing", None, nv)]
    events = []
    ov, oc = price_of(old)
    if oc == nc and dropped(ov, nv, min_pct):
        events.append(("price_drop", ov, nv))
    if old.get("owner_key") and new.get("owner_key") and old["owner_key"] != new["owner_key"]:
        events.append(("owner_change", None, nv))
    return events


# ---------- работа с БД ----------

async def add_event(db, etype, now, **kw):
    await db.execute(
        "INSERT INTO events (ts, type, slug, gift_id, title, num, old_price, new_price, currency, owner_label, url)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (now, etype, kw.get("slug"), kw.get("gift_id"), kw.get("title"), kw.get("num"),
         kw.get("old_price"), kw.get("new_price"), kw.get("currency"), kw.get("owner_label"), kw.get("url")))


async def save_listing(db, row: dict, now: int):
    await db.execute(
        """INSERT INTO listings (slug, gift_id, title, num, price_stars, price_ton, owner_key, owner_label,
               model, model_rarity, backdrop, backdrop_rarity, symbol, symbol_rarity,
               orig_sender, orig_recipient, orig_date, first_seen, last_seen)
           VALUES (:slug, :gift_id, :title, :num, :price_stars, :price_ton, :owner_key, :owner_label,
               :model, :model_rarity, :backdrop, :backdrop_rarity, :symbol, :symbol_rarity,
               :orig_sender, :orig_recipient, :orig_date, :now, :now)
           ON CONFLICT(slug) DO UPDATE SET
               price_stars=excluded.price_stars, price_ton=excluded.price_ton,
               owner_key=excluded.owner_key, owner_label=excluded.owner_label,
               last_seen=excluded.last_seen""",
        {**row, "now": now})


async def prune_listings(db, gift_id, seen_slugs, max_price, complete):
    """Убирает лоты, которых больше нет на маркете (проданы или сняты).
    Список отсортирован по цене, поэтому всё дешевле max_price обязано было прийти в ответе.
    Если ответ неполный (упёрлись в TOP_N), чистим только этот ценовой диапазон."""
    cur = await db.execute("SELECT slug, price_stars FROM listings WHERE gift_id=?", (gift_id,))
    for row in await cur.fetchall():
        if row["slug"] in seen_slugs:
            continue
        in_window = row["price_stars"] is not None and max_price is not None and row["price_stars"] < max_price
        if complete or in_window:
            await db.execute("DELETE FROM listings WHERE slug=?", (row["slug"],))


async def save_collection(db, c: dict, now: int):
    await db.execute(
        "INSERT INTO collections (gift_id, title, floor_stars, on_resale, updated_at) VALUES (?,?,?,?,?)"
        " ON CONFLICT(gift_id) DO UPDATE SET title=excluded.title, floor_stars=excluded.floor_stars,"
        " on_resale=excluded.on_resale, updated_at=excluded.updated_at",
        (c["gift_id"], c["title"], c["floor"], c["on_resale"], now))


# ---------- запросы к Telegram ----------

async def tg_call(client, request):
    """Вызов с ожиданием FloodWait."""
    while True:
        try:
            return await client(request)
        except FloodWaitError as e:
            log.warning("FloodWait %s c, жду", e.seconds)
            await asyncio.sleep(e.seconds + 1)


async def fetch_collections(client) -> list:
    # hash=0 каждый раз: нижние цены меняются постоянно, кэш по хэшу мог бы их скрыть
    res = await tg_call(client, functions.payments.GetStarGiftsRequest(hash=0))
    out = []
    for g in getattr(res, "gifts", []) or []:
        if type(g).__name__ != "StarGift":
            continue
        out.append({"gift_id": g.id,
                    "title": getattr(g, "title", None) or f"gift {g.id}",
                    "floor": getattr(g, "resell_min_stars", None),
                    "on_resale": getattr(g, "availability_resale", None) or 0})
    return out


async def poll_collection(client, db, c: dict, emit: bool, now: int):
    res = await tg_call(client, functions.payments.GetResaleStarGiftsRequest(
        gift_id=c["gift_id"], offset="", limit=TOP_N, sort_by_price=True))
    labels = build_labels(getattr(res, "users", None), getattr(res, "chats", None))
    gifts = [g for g in res.gifts if type(g).__name__ == "StarGiftUnique"]
    seen = []
    for g in gifts:
        new = parse_listing(g, labels)
        cur = await db.execute("SELECT * FROM listings WHERE slug=?", (new["slug"],))
        row = await cur.fetchone()
        old = dict(row) if row else None
        price, currency = price_of(new)
        if emit:
            for etype, ov, nv in diff_listing(old, new, MIN_DROP_PCT):
                await add_event(db, etype, now, slug=new["slug"], gift_id=new["gift_id"], title=new["title"],
                                num=new["num"], old_price=ov, new_price=nv, currency=currency,
                                owner_label=new["owner_label"], url=f"https://t.me/nft/{new['slug']}")
        if old is None or old["owner_key"] != new["owner_key"]:
            await db.execute("INSERT INTO owner_history (slug, owner_key, owner_label, seen_at) VALUES (?,?,?,?)",
                             (new["slug"], new["owner_key"], new["owner_label"], now))
        if old is None or price_of(old) != (price, currency):
            await db.execute("INSERT INTO price_history (slug, price_stars, price_ton, seen_at) VALUES (?,?,?,?)",
                             (new["slug"], new["price_stars"], new["price_ton"], now))
        await save_listing(db, new, now)
        seen.append(new)
    prices = [x["price_stars"] for x in seen if x["price_stars"] is not None]
    await prune_listings(db, c["gift_id"], {x["slug"] for x in seen},
                         max(prices) if prices else None, complete=len(gifts) < TOP_N)


async def cycle(client, db, n: int):
    now = int(time.time())
    cur = await db.execute("SELECT * FROM collections")
    prev = {r["gift_id"]: dict(r) for r in await cur.fetchall()}
    baseline = not prev  # первый запуск: только снимок, без событий
    collections = await fetch_collections(client)
    log.info("коллекций: %d, baseline=%s", len(collections), baseline)

    for c in collections:
        p = prev.get(c["gift_id"])
        if not c["on_resale"]:
            await save_collection(db, c, now)
            continue
        changed = p is None or p["floor_stars"] != c["floor"] or p["on_resale"] != c["on_resale"]
        forced = c["gift_id"] in WATCH_GIFT_IDS or n % DEEP_EVERY == 0
        if not (changed or forced):
            continue
        emit = bool(p) and not baseline
        if emit and dropped(p["floor_stars"], c["floor"], MIN_DROP_PCT):
            await add_event(db, "floor_drop", now, gift_id=c["gift_id"], title=c["title"],
                            old_price=p["floor_stars"], new_price=c["floor"], currency="stars")
            await db.commit()  # не держим блокировку записи, пока ждём сеть (бот пишет в ту же БД)
        try:
            await poll_collection(client, db, c, emit, now)
        except RPCError as e:
            log.warning("%s: %s", c["title"], e)
            continue
        await save_collection(db, c, now)
        await db.commit()
        await asyncio.sleep(REQUEST_DELAY)
    await db.commit()


async def print_cheapest(db, limit: int = 10):
    print("\nНижние цены по коллекциям (Stars):")
    cur = await db.execute("SELECT title, floor_stars, on_resale FROM collections"
                           " WHERE on_resale > 0 AND floor_stars IS NOT NULL ORDER BY floor_stars LIMIT ?", (limit,))
    for r in await cur.fetchall():
        print(f"  {r['floor_stars']:>8}  {r['title']} (лотов: {r['on_resale']})")
    print("\nСамые дешёвые лоты:")
    cur = await db.execute("SELECT title, num, price_stars, owner_label, slug FROM listings"
                           " WHERE price_stars IS NOT NULL ORDER BY price_stars LIMIT ?", (limit,))
    for r in await cur.fetchall():
        print(f"  {r['price_stars']:>8}  {r['title']} #{r['num']}  владелец: {r['owner_label']}  t.me/nft/{r['slug']}")
    cur = await db.execute("SELECT type, COUNT(*) AS n FROM events GROUP BY type")
    print("\nСобытий в БД:", {r["type"]: r["n"] for r in await cur.fetchall()} or "нет")


def check_config():
    if not API_ID or not API_HASH:
        sys.exit("Нужны переменные TG_API_ID и TG_API_HASH (my.telegram.org)")
    if not hasattr(functions.payments, "GetResaleStarGiftsRequest"):
        sys.exit("Telethon слишком старый для маркета подарков: pip install -U telethon")


async def login():
    """Интерактивный вход. Печатает строку сессии, её кладут в TG_STRING_SESSION."""
    check_config()
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.start()  # спросит телефон, код и пароль 2FA
    print("\nСкопируй в переменную TG_STRING_SESSION. Это ключ от аккаунта, никому его не показывай:\n")
    print(client.session.save())
    await client.disconnect()


async def connect_client() -> TelegramClient:
    check_config()
    if not STRING_SESSION:
        sys.exit("Нет TG_STRING_SESSION. Получи её командой: python gift_watcher.py login")
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        sys.exit("TG_STRING_SESSION недействительна (сессию завершили?). Сделай login заново")
    return client


async def worker_loop(client, db):
    n = 0
    while True:
        try:
            await cycle(client, db, n)
        except Exception:
            log.exception("цикл упал, повторю в следующий раз")
        n += 1
        await asyncio.sleep(POLL_SECONDS)


async def main(cmd: str):
    if cmd == "login":
        return await login()
    client = await connect_client()
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.executescript(SCHEMA)
            if cmd == "once":
                await cycle(client, db, 0)
                await print_cheapest(db)
            else:
                await worker_loop(client, db)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "run"))
