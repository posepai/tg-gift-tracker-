#!/usr/bin/env python3
"""
demo_bot.py - демо для проверки дизайна: тот же интерфейс, что у настоящего бота (bot.py),
но на выдуманных данных. Юзербот и telethon не нужны.

  pip install aiogram aiosqlite
  export BOT_TOKEN=...      # токен ТЕСТОВОГО бота (не боевого: два процесса на одном токене конфликтуют)
  python demo_bot.py

Нужны только файлы bot.py, schema.py и demo_bot.py.
Команда /alerts присылает примеры алертов. Сообщение с премиум-эмодзи в ответ даёт их ID. Данные живут в памяти и сбрасываются при перезапуске.
Ссылки «Открыть» ведут на t.me/nft/..., но лотов с такими номерами может не быть.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sys
import time

import aiosqlite
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

import bot as ui
from schema import SCHEMA

COLLECTIONS = ["Plush Pepe", "Heart Locket", "Durov's Cap", "Precious Peach", "Astral Shard",
               "Bonded Ring", "Swiss Watch", "Loot Bag", "Ion Gem", "Nail Bracelet",
               "Scared Cat", "Toy Bear", "Jelly Bunny", "Neko Helmet", "Mighty Arm"]
MODELS = ["Aurora", "Ember", "Frost", "Onyx", "Pearl", "Rogue", "Sunny", "Tidal"]
BACKDROPS = ["Sky Blue", "Rust", "Emerald", "Pine Green", "Mocha", "Rose", "Graphite"]
OWNERS = ["@demo_alice", "@demo_bob", "@demo_carol", "@demo_dave", "Demo Eve"]


async def seed(db):
    """Заполняет БД выдуманными коллекциями, лотами и историей владельцев."""
    rnd = random.Random(7)
    now = int(time.time())
    for gift_id, title in enumerate(COLLECTIONS, 1):
        floor = rnd.choice([1_500, 3_200, 7_800, 12_000, 25_000, 60_000, 140_000, 420_000])
        lots = 6
        await db.execute("INSERT INTO collections (gift_id, title, floor_stars, on_resale, updated_at) VALUES (?,?,?,?,?)",
                         (gift_id, title, floor, lots + rnd.randint(4, 180), now))
        base = re.sub(r"[^A-Za-z0-9]", "", title)
        price = floor
        for i in range(lots):
            num = rnd.randint(100, 9000)
            slug = f"{base}-{num}"
            owner = rnd.choice(OWNERS)
            await db.execute(
                "INSERT OR IGNORE INTO listings (slug, gift_id, title, num, price_stars, owner_key, owner_label,"
                " model, model_rarity, backdrop, backdrop_rarity, symbol, symbol_rarity,"
                " orig_sender, orig_recipient, orig_date, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slug, gift_id, title, num, price, f"n:{owner}", owner,
                 rnd.choice(MODELS), str(rnd.randint(5, 40)), rnd.choice(BACKDROPS), str(rnd.randint(5, 40)),
                 "Star", str(rnd.randint(5, 40)),
                 rnd.choice(OWNERS), rnd.choice(OWNERS), now - rnd.randint(30, 400) * 86400, now, now))
            past = now - rnd.randint(1, 20) * 86400
            for j in range(rnd.randint(1, 3)):
                await db.execute("INSERT INTO owner_history (slug, owner_key, owner_label, seen_at) VALUES (?,?,?,?)",
                                 (slug, "n:x", rnd.choice(OWNERS), past + j * 3600))
            price = int(price * (1 + rnd.uniform(0.02, 0.09)))
    await db.commit()


async def sample_events(db) -> list:
    lots = await ui.q_all(db, "SELECT * FROM listings ORDER BY gift_id, price_stars LIMIT 3")
    a, b, c = lots
    def ev(kind, lot, old=None, new=None, **kw):
        return {"type": kind, "slug": lot["slug"], "gift_id": lot["gift_id"], "title": lot["title"], "num": lot["num"],
                "old_price": old, "new_price": new, "currency": "stars", "owner_label": lot["owner_label"],
                "url": f"https://t.me/nft/{lot['slug']}", **kw}
    return [
        ev("floor_drop", a, int(a["price_stars"] * 1.2), a["price_stars"], slug=None, url=None, num=None),
        ev("price_drop", b, int(b["price_stars"] * 1.25), b["price_stars"]),
        ev("new_listing", c, None, c["price_stars"]),
        ev("owner_change", a, None, a["price_stars"], owner_label="@demo_bob"),
    ]


@ui.router.message(Command("alerts"))
async def on_alerts(m: Message):
    for e in await sample_events(ui.DB):
        await m.answer(ui.fmt_event(e), reply_markup=ui.event_markup(e))


@ui.router.message()  # регистрируется последним: срабатывает, если ни одна команда не подошла
async def on_any(m: Message):
    """Пришли боту сообщение с премиум-эмодзи, он ответит их ID (для переменной ICONS)."""
    found = [e.custom_emoji_id for e in list(m.entities or []) + list(m.caption_entities or [])
             if getattr(e, "type", None) == "custom_emoji"]
    if not found:
        return
    ids = "\n".join(f"<code>{i}</code>" for i in dict.fromkeys(found))
    await m.answer(f"ID премиум-эмодзи:\n{ids}\n\nВ ICONS пиши так: <code>open:ID,follow:ID</code>\n"
                   "Роли: hot, cols, subs, open, owners, follow, unfollow, back, "
                   "floor_drop, price_drop, new_listing, owner_change")


async def main():
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        sys.exit("Нужен BOT_TOKEN тестового бота (получить у @BotFather)")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(SCHEMA)
        await db.executescript(ui.BOT_SCHEMA)
        await seed(db)
        ui.DB = db
        ui.WELCOME = ("🧪 <b>Демо</b>: данные выдуманные, юзербота нет. "
                      "Команда /alerts покажет примеры алертов. Пришли премиум-эмодзи, и я отвечу его ID.\n\n" + ui.WELCOME)
        bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()
        dp.include_router(ui.router)
        try:
            await dp.start_polling(bot)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
