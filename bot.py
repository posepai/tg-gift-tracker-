#!/usr/bin/env python3
"""
bot.py - бот на aiogram 3 и воркер маркета в одном процессе.

  python bot.py

Бот только читает данные воркера (gift_watcher.py) из общей SQLite-базы и шлёт алерты.
Ничего не покупает: кнопка «Открыть» ведёт на страницу подарка, платит человек сам.

Переменные окружения:
  BOT_TOKEN      токен от @BotFather
  ADMIN_IDS      твои Telegram id через запятую: получают ВСЕ события (удобно для проверки)
  ICONS          премиум-иконки: open:ID,follow:ID,... (см. README)
  DB_PATH        путь к базе (на Railway это файл на Volume, например /data/gifts.db)
  TG_API_ID, TG_API_HASH, TG_STRING_SESSION   для воркера, см. gift_watcher.py
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import sys
import time
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest,
                                TelegramForbiddenError, TelegramRetryAfter)
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from schema import SCHEMA

log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
# Премиум-эмодзи: ICONS="open:ID,follow:ID,..." (роли перечислены в README). Нужен Telegram Premium у владельца бота.
ICONS = {k.strip(): v.strip() for k, v in (p.split(":", 1) for p in os.getenv("ICONS", "").split(",") if ":" in p)}
ALERT_POLL = float(os.getenv("ALERT_POLL", "5"))  # как часто смотреть новые события, сек
PAGE = 8  # коллекций на странице
LOTS = 8  # лотов в выдаче
STALE_EVENT_SECONDS = 3600  # события старше часа при старте не рассылаем

BOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS subs (
    user_id INTEGER, gift_id INTEGER,
    PRIMARY KEY (user_id, gift_id)
);
"""

WELCOME = (
    "Я слежу за маркетом подарков Telegram: показываю дешёвые лоты и историю владельцев "
    "и предупреждаю, когда цена падает.\n\n"
    "Открой коллекцию и нажми «🔔 Следить», чтобы получать алерты. "
    "Покупаешь сам: кнопка «Открыть» ведёт на страницу подарка."
)

router = Router()
DB = None  # соединение бота с БД, выставляется в amain()


# ---------- БД ----------

async def q_all(db, sql: str, params=()) -> list:
    cur = await db.execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


async def collections_page(db, page: int):
    total = (await q_all(db, "SELECT COUNT(*) AS n FROM collections WHERE on_resale > 0"))[0]["n"]
    pages = max(1, -(-total // PAGE))
    page = min(max(page, 0), pages - 1)
    rows = await q_all(db, "SELECT * FROM collections WHERE on_resale > 0"
                           " ORDER BY floor_stars IS NULL, floor_stars LIMIT ? OFFSET ?", (PAGE, page * PAGE))
    return rows, page, pages


async def cheapest_lots(db, gift_id=None, limit: int = LOTS) -> list:
    sql = "SELECT * FROM listings WHERE price_stars IS NOT NULL"
    params: list = []
    if gift_id is not None:
        sql += " AND gift_id = ?"
        params.append(gift_id)
    return await q_all(db, sql + " ORDER BY price_stars LIMIT ?", (*params, limit))


async def is_subscribed(db, user_id: int, gift_id: int) -> bool:
    return bool(await q_all(db, "SELECT 1 AS x FROM subs WHERE user_id=? AND gift_id=?", (user_id, gift_id)))


async def toggle_sub(db, user_id: int, gift_id: int) -> bool:
    if await is_subscribed(db, user_id, gift_id):
        await db.execute("DELETE FROM subs WHERE user_id=? AND gift_id=?", (user_id, gift_id))
        on = False
    else:
        await db.execute("INSERT INTO subs (user_id, gift_id) VALUES (?,?)", (user_id, gift_id))
        on = True
    await db.commit()
    return on


async def remove_sub(db, user_id: int, gift_id: int):
    await db.execute("DELETE FROM subs WHERE user_id=? AND gift_id=?", (user_id, gift_id))
    await db.commit()


# ---------- форматирование ----------

def fmt_price(value, currency: str = "stars") -> str:
    if value is None:
        return "-"
    if currency == "ton":
        return f"{value / 1e9:.2f} TON"
    return f"{value:,}".replace(",", " ") + " ⭐"


def fmt_dt(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M") + " UTC"


def pct(old, new) -> int:
    return round((old - new) / old * 100) if old else 0


def strip_lead_emoji(text: str) -> str:
    """'🔥 Дешёвые' -> 'Дешёвые': когда задана премиум-иконка, обычный эмодзи в начале не нужен."""
    head, _, rest = text.partition(" ")
    return rest if rest and head and not head[0].isalnum() else text


def make_button(text: str, role=None, style=None, **kw) -> InlineKeyboardButton:
    """Кнопка с необязательными цветом (style) и премиум-иконкой (по роли из ICONS).
    style: "primary" (синяя), "success" (зелёная), "danger" (красная)."""
    extra = {}
    if style:
        extra["style"] = style
    shown = text
    if role and ICONS.get(role):
        extra["icon_custom_emoji_id"] = ICONS[role]
        shown = strip_lead_emoji(text)
    try:
        return InlineKeyboardButton(text=shown, **kw, **extra)
    except Exception:  # aiogram не принял доп. поля: рисуем обычную кнопку
        return InlineKeyboardButton(text=text, **kw)


def url_button(text: str, url: str, role=None) -> InlineKeyboardButton:
    return make_button(text, role, "success", url=url)  # зелёная


def cb_button(text: str, data: str, style=None, role=None) -> InlineKeyboardButton:
    return make_button(text, role, style, callback_data=data)


def emo(role: str, fallback: str) -> str:
    """Премиум-эмодзи в тексте сообщения (если задано в ICONS), иначе обычное."""
    eid = ICONS.get(role)
    return f'<tg-emoji emoji-id="{html.escape(eid)}">{fallback}</tg-emoji>' if eid else fallback


def fmt_event(e: dict) -> str:
    name = html.escape(e.get("title") or "Подарок")
    lot = f"{name} #{e['num']}" if e.get("num") else name
    cur = e.get("currency") or "stars"
    old, new = fmt_price(e.get("old_price"), cur), fmt_price(e.get("new_price"), cur)
    drop = pct(e.get("old_price"), e.get("new_price")) if e.get("old_price") and e.get("new_price") else 0
    kind = e["type"]
    if kind == "floor_drop":
        head = f"{emo('floor_drop', '📉')} <b>{name}</b>: нижняя цена упала {old} → {new} (−{drop}%)"
    elif kind == "price_drop":
        head = f"{emo('price_drop', '💸')} <b>{lot}</b>: {old} → {new} (−{drop}%)"
    elif kind == "new_listing":
        head = f"{emo('new_listing', '🆕')} <b>{lot}</b> выставлен за {new}"
    elif kind == "owner_change":
        head = f"{emo('owner_change', '🔁')} <b>{lot}</b>: новый владелец {html.escape(e.get('owner_label') or '?')}"
    else:
        return f"{html.escape(kind)}: {lot}"
    extra = ""
    if kind in ("price_drop", "new_listing") and e.get("owner_label"):
        extra += f"\n<blockquote>владелец: {html.escape(e['owner_label'])}</blockquote>"
    if e.get("ts"):  # «5 минут назад» в часовом поясе пользователя; тег снаружи цитаты (вложенность запрещена)
        extra += f'\n<tg-time unix="{int(e["ts"])}" format="r">{fmt_dt(e["ts"])}</tg-time>'
    return head + extra


def event_markup(e: dict):
    row = []
    if e.get("url"):
        row.append(url_button("Открыть", e["url"], role="open"))
    if e.get("slug"):
        row.append(cb_button("👤 Владельцы", f"own:{e['slug']}", role="owners"))
    if e["type"] == "floor_drop" and e.get("gift_id") is not None:
        row.append(cb_button("Показать лоты", f"c:{e['gift_id']}", "primary"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


def lots_text(lots: list, show_title: bool = False) -> str:
    lines = []
    for i, l in enumerate(lots, 1):
        name = f"{html.escape(l['title'] or '?')} #{l['num']}" if show_title else f"#{l['num']}"
        traits = " · ".join(html.escape(x) for x in (l.get("model"), l.get("backdrop")) if x)
        card = (traits + "\n" if traits else "") + f"владелец: {html.escape(l.get('owner_label') or '-')}"
        lines.append(f"{i}. <b>{name}</b> · {fmt_price(l['price_stars'])}\n<blockquote>{card}</blockquote>")
    return "\n".join(lines)


def lots_markup(lots: list, back: str, gift_id=None, subscribed: bool = False) -> InlineKeyboardMarkup:
    rows = []
    for i, l in enumerate(lots, 1):
        rows.append([url_button(f"{i}. Открыть", f"https://t.me/nft/{l['slug']}", role="open"),
                     cb_button(f"👤 {i}. Владельцы", f"own:{l['slug']}", role="owners")])
    tail = [cb_button("⬅ Назад", back, role="back")]
    if gift_id is not None:
        tail.append(cb_button("🔕 Не следить", f"sub:{gift_id}", "danger", role="unfollow") if subscribed
                    else cb_button("🔔 Следить", f"sub:{gift_id}", "primary", role="follow"))
    rows.append(tail)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [cb_button("🔥 Дешёвые сейчас", "hot", role="hot")],
        [cb_button("🗂 Коллекции", "cols:0", role="cols")],
        [cb_button("🔔 Мои подписки", "subs", role="subs")],
    ])


# ---------- экраны (текст + клавиатура) ----------

async def view_hot():
    lots = await cheapest_lots(DB)
    if not lots:
        return "Данных пока нет: воркер делает первый снимок маркета, загляни через несколько минут.", \
            InlineKeyboardMarkup(inline_keyboard=[[cb_button("⬅ Меню", "menu", role="back")]])
    return "🔥 <b>Самые дешёвые лоты сейчас</b>\n\n" + lots_text(lots, show_title=True), lots_markup(lots, "menu")


async def view_cols(page: int):
    rows, page, pages = await collections_page(DB, page)
    kb = [[cb_button(f"{r['title']} · от {fmt_price(r['floor_stars'])} · {r['on_resale']}", f"c:{r['gift_id']}")]
          for r in rows]
    nav = []
    if page > 0:
        nav.append(cb_button("⬅", f"cols:{page - 1}"))
    nav.append(cb_button(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(cb_button("➡", f"cols:{page + 1}"))
    kb.append(nav)
    kb.append([cb_button("⬅ Меню", "menu", role="back")])
    text = "🗂 <b>Коллекции с лотами на продаже</b>\nСортировка по нижней цене, последняя цифра в кнопке - число лотов."
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def view_collection(gift_id: int, user_id: int):
    rows = await q_all(DB, "SELECT * FROM collections WHERE gift_id=?", (gift_id,))
    if not rows:
        return "Коллекция не найдена.", InlineKeyboardMarkup(inline_keyboard=[[cb_button("⬅ Назад", "cols:0", role="back")]])
    c = rows[0]
    lots = await cheapest_lots(DB, gift_id)
    head = (f"<b>{html.escape(c['title'] or str(gift_id))}</b>\n"
            f"Нижняя цена: {fmt_price(c['floor_stars'])} · лотов: {c['on_resale']}\n\n")
    text = head + (lots_text(lots) if lots else "Лотов пока нет в снимке.")
    return text, lots_markup(lots, "cols:0", gift_id, await is_subscribed(DB, user_id, gift_id))


async def view_subs(user_id: int):
    rows = await q_all(DB, "SELECT s.gift_id AS gift_id, c.title AS title FROM subs s"
                           " LEFT JOIN collections c ON c.gift_id = s.gift_id WHERE s.user_id=? ORDER BY c.title",
                       (user_id,))
    text = ("🔔 <b>Ты следишь за коллекциями</b>\nНажми, чтобы отписаться:" if rows
            else "Подписок нет. Открой коллекцию и нажми «🔔 Следить».")
    kb = [[cb_button(f"❌ {r['title'] or r['gift_id']}", f"unsub:{r['gift_id']}", "danger")] for r in rows]
    kb.append([cb_button("⬅ Меню", "menu", role="back")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def view_owners(slug: str):
    lot = await q_all(DB, "SELECT * FROM listings WHERE slug=?", (slug,))
    hist = await q_all(DB, "SELECT owner_label, seen_at FROM owner_history WHERE slug=? ORDER BY id", (slug,))
    lines = [f"👤 <b>{html.escape(slug)}</b>"]
    if lot:
        l = lot[0]
        lines.append(f"Сейчас: {html.escape(l.get('owner_label') or '-')}")
        if l.get("orig_recipient"):
            first = f"Первый получатель: {html.escape(l['orig_recipient'])}"
            if l.get("orig_sender"):
                first += f", отправитель: {html.escape(l['orig_sender'])}"
            if l.get("orig_date"):
                first += f' (<tg-time unix="{int(l["orig_date"])}" format="D">{fmt_dt(l["orig_date"])}</tg-time>)'
            lines.append(first)
    if hist:
        rows = [f"• {fmt_dt(h['seen_at'])}: {html.escape(h['owner_label'] or '-')}" for h in hist]
        lines.append("<blockquote expandable>Владельцы с момента, как бот следит за лотом:\n" + "\n".join(rows) + "</blockquote>")
    elif not lot:
        lines.append("Этого лота нет в снимке (его могли купить или снять с продажи).")
    markup = InlineKeyboardMarkup(inline_keyboard=[[url_button("Открыть", f"https://t.me/nft/{slug}", role="open")]])
    return "\n".join(lines), markup


# ---------- хендлеры ----------

async def show(c: CallbackQuery, text: str, markup, notice=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await c.answer(notice)


@router.message(CommandStart())
async def on_start(m: Message):
    await m.answer(WELCOME, reply_markup=main_menu())


@router.message(Command("search"))
async def on_search(m: Message):
    await m.answer("Что показать?", reply_markup=main_menu())


@router.message(Command("subs"))
async def on_subs(m: Message):
    text, markup = await view_subs(m.from_user.id)
    await m.answer(text, reply_markup=markup)


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery):
    await show(c, "Что показать?", main_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "hot")
async def cb_hot(c: CallbackQuery):
    await show(c, *await view_hot())


@router.callback_query(F.data.startswith("cols:"))
async def cb_cols(c: CallbackQuery):
    await show(c, *await view_cols(int(c.data.split(":")[1])))


@router.callback_query(F.data.startswith("c:"))
async def cb_collection(c: CallbackQuery):
    await show(c, *await view_collection(int(c.data.split(":")[1]), c.from_user.id))


@router.callback_query(F.data.startswith("sub:"))
async def cb_sub(c: CallbackQuery):
    gift_id = int(c.data.split(":")[1])
    on = await toggle_sub(DB, c.from_user.id, gift_id)
    await show(c, *await view_collection(gift_id, c.from_user.id), notice="Слежу за коллекцией" if on else "Отписал")


@router.callback_query(F.data == "subs")
async def cb_subs(c: CallbackQuery):
    await show(c, *await view_subs(c.from_user.id))


@router.callback_query(F.data.startswith("unsub:"))
async def cb_unsub(c: CallbackQuery):
    await remove_sub(DB, c.from_user.id, int(c.data.split(":")[1]))
    await show(c, *await view_subs(c.from_user.id), notice="Отписал")


@router.callback_query(F.data.startswith("own:"))
async def cb_owners(c: CallbackQuery):
    text, markup = await view_owners(c.data.split(":", 1)[1])
    await c.message.answer(text, reply_markup=markup)
    await c.answer()


# ---------- рассылка алертов ----------

async def send_safe(bot, db, uid: int, text: str, markup):
    for attempt in range(2):
        try:
            await bot.send_message(uid, text, reply_markup=markup)
            return
        except TelegramForbiddenError:  # пользователь заблокировал бота
            await db.execute("DELETE FROM subs WHERE user_id=?", (uid,))
            await db.commit()
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramAPIError as e:
            log.warning("send to %s failed: %s", uid, e)
            return


async def deliver(bot, db, e: dict):
    targets = set(ADMIN_IDS)
    if e["type"] != "owner_change" and e.get("gift_id") is not None:  # смена владельца шумная, только админам
        targets |= {r["user_id"] for r in await q_all(db, "SELECT user_id FROM subs WHERE gift_id=?", (e["gift_id"],))}
    text, markup = fmt_event(e), event_markup(e)
    for uid in targets:
        await send_safe(bot, db, uid, text, markup)
        await asyncio.sleep(0.05)  # держимся далеко от лимита 30 сообщений в секунду


async def alert_loop(bot, db):
    await db.execute("UPDATE events SET sent=1 WHERE sent=0 AND ts < ?", (int(time.time()) - STALE_EVENT_SECONDS,))
    await db.commit()
    while True:
        try:
            events = await q_all(db, "SELECT * FROM events WHERE sent=0 ORDER BY id LIMIT 50")
            for e in events:
                await deliver(bot, db, e)
                await db.execute("UPDATE events SET sent=1 WHERE id=?", (e["id"],))
            if events:
                await db.commit()
        except Exception:
            log.exception("alert_loop")
        await asyncio.sleep(ALERT_POLL)


# ---------- запуск ----------

async def amain():
    global DB
    if not BOT_TOKEN:
        sys.exit("Нужен BOT_TOKEN (получить у @BotFather)")
    import gift_watcher as gw  # только здесь: демо-режиму telethon не нужен
    client = await gw.connect_client()
    async with aiosqlite.connect(gw.DB_PATH) as wdb, aiosqlite.connect(gw.DB_PATH) as bdb:
        for d in (wdb, bdb):
            d.row_factory = aiosqlite.Row
            await d.execute("PRAGMA busy_timeout=10000")
        await wdb.executescript(SCHEMA)
        await bdb.executescript(BOT_SCHEMA)
        DB = bdb
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()
        dp.include_router(router)
        tasks = [asyncio.create_task(gw.worker_loop(client, wdb)),
                 asyncio.create_task(alert_loop(bot, bdb))]
        try:
            await dp.start_polling(bot)
        finally:
            for t in tasks:
                t.cancel()
            await client.disconnect()
            await bot.session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(amain())
