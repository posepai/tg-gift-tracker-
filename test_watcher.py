"""Проверка логики воркера и бота без сети: поддельные объекты Telegram и заглушки
telethon, aiogram и aiosqlite. Запуск: python test_watcher.py"""
import asyncio, os, sqlite3, sys, time, types
os.environ.update(REQUEST_DELAY="0", TG_API_ID="1", TG_API_HASH="x", BOT_TOKEN="1:x", ALERT_POLL="0.05")

def module(name, **attrs):
    m = types.ModuleType(name); m.__dict__.update(attrs); sys.modules[name] = m; return m

# ---- заглушка telethon ----
class FloodWaitError(Exception):
    def __init__(s, seconds=1): s.seconds = seconds
class RPCError(Exception): pass
class Req:
    def __init__(s, **kw): s.kw = kw
class GetStarGiftsRequest(Req): pass
class GetResaleStarGiftsRequest(Req): pass
module("telethon", TelegramClient=object, functions=types.SimpleNamespace(payments=types.SimpleNamespace(
    GetStarGiftsRequest=GetStarGiftsRequest, GetResaleStarGiftsRequest=GetResaleStarGiftsRequest)))
module("telethon.errors", FloodWaitError=FloodWaitError, RPCError=RPCError)
module("telethon.sessions", StringSession=lambda s="": s)

# ---- заглушка aiosqlite поверх sqlite3 ----
class Cur:
    def __init__(s, c): s.c = c
    async def fetchall(s): return s.c.fetchall()
    async def fetchone(s): return s.c.fetchone()
class Conn:
    def __init__(s, p): s.conn = sqlite3.connect(p)
    row_factory = property(lambda s: s.conn.row_factory, lambda s, v: setattr(s.conn, "row_factory", v))
    async def execute(s, q, p=()): return Cur(s.conn.execute(q, p))
    async def executescript(s, q): s.conn.executescript(q)
    async def commit(s): s.conn.commit()
    async def __aenter__(s): return s
    async def __aexit__(s, *a): s.conn.close()
module("aiosqlite", Row=sqlite3.Row, connect=lambda p: Conn(p))

# ---- заглушка aiogram ----
class Magic:
    def __getattr__(s, n): return Magic()
    def __call__(s, *a, **k): return Magic()
    def __eq__(s, o): return Magic()
    def __hash__(s): return 1
class Router:
    def _deco(self, *f): return lambda fn: fn
    message = callback_query = _deco
class TelegramAPIError(Exception): pass
class TelegramForbiddenError(TelegramAPIError): pass
class TelegramBadRequest(TelegramAPIError): pass
class TelegramRetryAfter(TelegramAPIError):
    retry_after = 0
class Btn:
    def __init__(self, **kw): self.__dict__.update(kw)
class Markup:
    def __init__(self, inline_keyboard): self.inline_keyboard = inline_keyboard
module("aiogram", Bot=object, Dispatcher=object, F=Magic(), Router=Router)
module("aiogram.client"); module("aiogram.client.default", DefaultBotProperties=object)
module("aiogram.enums", ParseMode=types.SimpleNamespace(HTML="HTML"))
module("aiogram.exceptions", TelegramAPIError=TelegramAPIError, TelegramBadRequest=TelegramBadRequest,
       TelegramForbiddenError=TelegramForbiddenError, TelegramRetryAfter=TelegramRetryAfter)
module("aiogram.filters", Command=Magic(), CommandStart=Magic())
module("aiogram.types", CallbackQuery=object, Message=object, InlineKeyboardButton=Btn, InlineKeyboardMarkup=Markup)

import bot
import demo_bot
import schema
assert "gift_watcher" not in sys.modules, "bot.py и demo_bot.py не должны требовать telethon"
import gift_watcher as gw

# ---- поддельные объекты TL ----
def mk(cls_name, **kw): return type(cls_name, (), kw)()
def user(i, uname): return mk("User", id=i, username=uname, first_name="N", last_name=None)
def gift(slug, price, owner_id, sender=None):
    attrs = [mk("StarGiftAttributeModel", name="Mdl", rarity=mk("StarGiftAttributeRarity", permille=15)),
             mk("StarGiftAttributeBackdrop", name="Bd", rarity_permille=20),
             mk("StarGiftAttributePattern", name="Sym", rarity=mk("StarGiftAttributeRarityRare")),
             mk("StarGiftAttributeOriginalDetails", sender_id=mk("PeerUser", user_id=sender) if sender else None,
                recipient_id=mk("PeerUser", user_id=owner_id), date=1700000000)]
    return mk("StarGiftUnique", slug=slug, gift_id=1, title="Plush", num=int(slug.split("-")[1]),
              resell_amount=[mk("StarsAmount", amount=price, nanos=0)],
              owner_id=mk("PeerUser", user_id=owner_id), attributes=attrs)

state = {}
class FakeClient:
    async def __call__(self, req):
        if isinstance(req, GetStarGiftsRequest):
            return mk("StarGifts", gifts=[
                mk("StarGift", id=1, title="Plush", resell_min_stars=state["floor"], availability_resale=state["count"]),
                mk("StarGift", id=2, title="Empty", resell_min_stars=None, availability_resale=0)])
        assert req.kw["sort_by_price"] is True and req.kw["gift_id"] == 1
        return mk("ResaleStarGifts", gifts=state["listings"], users=[user(10, "alice"), user(11, "bob")], chats=[])

def buttons(markup): return [b for row in markup.inline_keyboard for b in row]

class FakeBot:
    def __init__(self, forbidden=()): self.sent, self.forbidden = [], set(forbidden)
    async def send_message(self, uid, text, reply_markup=None):
        if uid in self.forbidden: raise TelegramForbiddenError()
        self.sent.append((uid, text, reply_markup))
class FakeMsg:
    def __init__(self): self.edits, self.answers = [], []
    async def edit_text(self, text, reply_markup=None): self.edits.append((text, reply_markup))
    async def answer(self, text, reply_markup=None): self.answers.append((text, reply_markup))
class FakeCb:
    def __init__(self, data, uid): self.data, self.from_user, self.message, self.notice = data, types.SimpleNamespace(id=uid), FakeMsg(), "unset"
    async def answer(self, text=None): self.notice = text
class FakeM:
    def __init__(self, uid=1): self.from_user, self.answers = types.SimpleNamespace(id=uid), []
    async def answer(self, text, reply_markup=None): self.answers.append((text, reply_markup))

async def scalar(db, sql, p=()): return (await (await db.execute(sql, p)).fetchone())[0]

async def run():
    async with gw.aiosqlite.connect(":memory:") as db:
        db.row_factory = gw.aiosqlite.Row
        await db.executescript(gw.SCHEMA); await db.executescript(bot.BOT_SCHEMA)
        bot.DB = db
        c = FakeClient()

        # === воркер: baseline без событий ===
        state.update(floor=1000, count=3, listings=[gift("Plush-1", 1000, 10, sender=11), gift("Plush-2", 1200, 10), gift("Plush-3", 1500, 10)])
        await gw.cycle(c, db, 0)
        assert await scalar(db, "SELECT COUNT(*) FROM events") == 0 and await scalar(db, "SELECT COUNT(*) FROM listings") == 3
        r = dict(await (await db.execute("SELECT * FROM listings WHERE slug='Plush-1'")).fetchone())
        assert r["owner_label"] == "@alice" and r["model"] == "Mdl" and r["model_rarity"] == "15" and r["symbol_rarity"] == "rare" and r["orig_sender"] == "@bob", r

        # === воркер: floor_drop, new_listing, price_drop, owner_change, снятый лот ===
        state.update(floor=800, count=4, listings=[gift("Plush-4", 800, 10), gift("Plush-1", 1000, 11), gift("Plush-2", 1000, 10)])
        await gw.cycle(c, db, 1)
        evs = [dict(x) for x in await (await db.execute("SELECT * FROM events ORDER BY id")).fetchall()]
        assert sorted(e["type"] for e in evs) == ["floor_drop", "new_listing", "owner_change", "price_drop"], evs
        pd = [e for e in evs if e["type"] == "price_drop"][0]
        assert (pd["slug"], pd["old_price"], pd["new_price"]) == ("Plush-2", 1200, 1000), pd
        left = sorted(r["slug"] for r in await (await db.execute("SELECT slug FROM listings")).fetchall())
        assert left == ["Plush-1", "Plush-2", "Plush-4"], left
        oh = await (await db.execute("SELECT owner_label FROM owner_history WHERE slug='Plush-1' ORDER BY id")).fetchall()
        assert [x["owner_label"] for x in oh] == ["@alice", "@bob"]

        # === воркер: без изменений не опрашивает ===
        before = await scalar(db, "SELECT COUNT(*) FROM events")
        state["listings"] = None
        await gw.cycle(c, db, 2)
        assert await scalar(db, "SELECT COUNT(*) FROM events") == before
        assert gw.diff_listing({"price_stars": 1000, "owner_key": "u:1"}, {"price_stars": 980, "owner_key": "u:1"}, 5) == []
        assert gw.diff_listing({"price_stars": 1000, "owner_key": "u:1"}, {"price_stars": None, "price_ton": 5, "owner_key": "u:1"}, 5) == []

        # вторая коллекция для проверок бота
        await db.execute("INSERT OR REPLACE INTO collections VALUES (2,'Gem',5000,1,0)")
        await db.execute("INSERT INTO collections VALUES (3,'Empty',NULL,0,0)")
        await db.execute("INSERT INTO listings (slug,gift_id,title,num,price_stars,owner_label,first_seen,last_seen) VALUES ('Gem-7',2,'Gem',7,5000,'@carol',0,0)")
        await db.commit()

        # === бот: экраны ===
        t, m = await bot.view_hot()
        assert "Plush #4" in t and "800 ⭐" in t and "Gem #7" in t, t
        bs = buttons(m)
        assert any(getattr(b, "url", "") == "https://t.me/nft/Plush-4" and getattr(b, "style", "") == "success" for b in bs)
        assert any(getattr(b, "callback_data", "") == "own:Plush-4" for b in bs)
        t, m = await bot.view_cols(0)
        texts = [b.text for b in buttons(m)]
        assert texts[0].startswith("Plush") and texts[1].startswith("Gem") and not any("Empty" in x for x in texts), texts
        cb = FakeCb("c:1", 5)
        await bot.cb_collection(cb)
        text, markup = cb.message.edits[0]
        assert "Plush" in text and "800 ⭐" in text and any(b.text == "🔔 Следить" for b in buttons(markup)) and cb.notice is None
        assert [b.style for b in buttons(markup) if b.text == "🔔 Следить"] == ["primary"]
        cb = FakeCb("sub:1", 5)
        await bot.cb_sub(cb)
        assert await scalar(db, "SELECT COUNT(*) FROM subs WHERE user_id=5 AND gift_id=1") == 1 and cb.notice == "Слежу за коллекцией"
        assert [b.style for b in buttons(cb.message.edits[0][1]) if b.text == "🔕 Не следить"] == ["danger"]
        cb = FakeCb("subs", 5)
        await bot.cb_subs(cb)
        assert "❌ Plush" in [b.text for b in buttons(cb.message.edits[0][1])]
        assert all(b.style == "danger" for b in buttons(cb.message.edits[0][1]) if b.text.startswith("❌"))
        cb = FakeCb("own:Plush-1", 5)
        await bot.cb_owners(cb)
        otext = cb.message.answers[0][0]
        assert "Сейчас: @bob" in otext and "Первый получатель: @alice, отправитель: @bob" in otext and "@alice" in otext.split("Владельцы с момента")[1], otext
        assert "<blockquote expandable>Владельцы с момента" in otext and '<tg-time unix="1700000000"' in otext, otext
        m5 = FakeM(); await bot.on_start(m5); assert "маркетом подарков" in m5.answers[0][0]
        assert bot.fmt_price(2_500_000_000, "ton") == "2.50 TON" and bot.fmt_price(12345) == "12 345 ⭐"

        # === бот: рассылка ===
        bot.ADMIN_IDS = {99}
        fb = FakeBot()
        ev = lambda **kw: {"type": "floor_drop", "slug": None, "gift_id": 1, "title": "Plush", "num": None, "old_price": 1000,
                           "new_price": 800, "currency": "stars", "owner_label": None, "url": None, **kw}
        await bot.deliver(fb, db, ev())
        assert sorted(x[0] for x in fb.sent) == [5, 99] and "−20%" in fb.sent[0][1], fb.sent
        assert any(getattr(b, "callback_data", "") == "c:1" for b in buttons(fb.sent[0][2]))
        withts = bot.fmt_event(ev(type="new_listing", slug="Plush-9", num=9, ts=1700000000, owner_label="@bob", old_price=None, new_price=800))
        assert '<blockquote>владелец: @bob</blockquote>' in withts and withts.endswith('format="r">14.11.2023 22:13 UTC</tg-time>'), withts
        assert "<blockquote" not in bot.fmt_event(ev()) and "tg-time" not in bot.fmt_event(ev())
        fb = FakeBot(); await bot.deliver(fb, db, ev(type="owner_change", owner_label="@bob", slug="Plush-1", num=1))
        assert [x[0] for x in fb.sent] == [99]
        fb = FakeBot(); await bot.deliver(fb, db, ev(type="new_listing", gift_id=2, title="Gem", slug="Gem-7", num=7, url="https://t.me/nft/Gem-7", old_price=None, new_price=5000))
        assert [x[0] for x in fb.sent] == [99] and "Gem #7" in fb.sent[0][1]
        fb = FakeBot(forbidden={5}); await bot.deliver(fb, db, ev())
        assert [x[0] for x in fb.sent] == [99] and await scalar(db, "SELECT COUNT(*) FROM subs WHERE user_id=5") == 0

        # === бот: alert_loop помечает события отправленными, старые пропускает ===
        await db.execute("DELETE FROM events")
        now = int(time.time())
        await db.execute("INSERT INTO events (ts,type,gift_id,title,old_price,new_price,currency) VALUES (?,?,?,?,?,?,?)", (now - 7200, "floor_drop", 1, "Plush", 1000, 900, "stars"))
        await db.execute("INSERT INTO events (ts,type,gift_id,title,old_price,new_price,currency) VALUES (?,?,?,?,?,?,?)", (now, "floor_drop", 1, "Plush", 900, 700, "stars"))
        await db.commit()
        fb = FakeBot()
        try: await asyncio.wait_for(bot.alert_loop(fb, db), 0.4)
        except asyncio.TimeoutError: pass
        assert await scalar(db, "SELECT COUNT(*) FROM events WHERE sent=0") == 0
        assert len(fb.sent) == 1 and "900 ⭐ → 700 ⭐" in fb.sent[0][1], fb.sent
        # === премиум-иконки: кнопки и заголовки алертов ===
        assert bot.main_menu().inline_keyboard[0][0].text == "🔥 Дешёвые сейчас" and not hasattr(bot.main_menu().inline_keyboard[0][0], "icon_custom_emoji_id")
        bot.ICONS = {"open": "111", "follow": "222", "hot": "333", "floor_drop": "444", "back": "555"}
        try:
            hot = buttons(bot.main_menu())[0]
            assert hot.icon_custom_emoji_id == "333" and hot.text == "Дешёвые сейчас", hot.__dict__
            _, mk = await bot.view_collection(1, 77)
            fol = [b for b in buttons(mk) if getattr(b, "icon_custom_emoji_id", "") == "222"]
            assert len(fol) == 1 and fol[0].text == "Следить" and fol[0].style == "primary"
            opens = [b for b in buttons(mk) if getattr(b, "icon_custom_emoji_id", "") == "111"]
            assert opens and all(b.style == "success" and b.text.endswith("Открыть") for b in opens)
            assert [b.text for b in buttons(mk) if getattr(b, "icon_custom_emoji_id", "") == "555"] == ["Назад"]
            assert bot.fmt_event(ev()).startswith('<tg-emoji emoji-id="444">📉</tg-emoji> <b>Plush</b>')
        finally:
            bot.ICONS = {}
        assert bot.fmt_event(ev()).startswith("📉 <b>Plush</b>")
        assert bot.strip_lead_emoji("⬅ Назад") == "Назад" and bot.strip_lead_emoji("1. Открыть") == "1. Открыть" and bot.strip_lead_emoji("Открыть") == "Открыть"

        # === демо: ловец ID премиум-эмодзи ===
        fm2 = FakeM(); fm2.entities = [types.SimpleNamespace(type="bold"), types.SimpleNamespace(type="custom_emoji", custom_emoji_id="5368324170671202286")]; fm2.caption_entities = None
        await demo_bot.on_any(fm2)
        assert len(fm2.answers) == 1 and "<code>5368324170671202286</code>" in fm2.answers[0][0] and "open:ID" in fm2.answers[0][0]
        fm3 = FakeM(); fm3.entities = None; fm3.caption_entities = None
        await demo_bot.on_any(fm3); assert fm3.answers == []

        # === демо-бот: все экраны рендерятся, лимиты Telegram соблюдены ===
        async with gw.aiosqlite.connect(":memory:") as d2:
            d2.row_factory = gw.aiosqlite.Row
            await d2.executescript(schema.SCHEMA); await d2.executescript(bot.BOT_SCHEMA)
            await demo_bot.seed(d2)
            bot.DB = d2
            cols = await bot.q_all(d2, "SELECT gift_id FROM collections")
            assert len(cols) == 15 and await scalar(d2, "SELECT COUNT(*) FROM listings") >= 60
            screens = [await bot.view_hot(), await bot.view_cols(0), await bot.view_cols(1)]
            assert any(b.text == "⬅" for b in buttons(screens[2][1])) and not any(b.text == "➡" for b in buttons(screens[2][1]))
            for col in cols: screens.append(await bot.view_collection(col["gift_id"], 1))
            for r in (await bot.q_all(d2, "SELECT slug FROM listings"))[:25]: screens.append(await bot.view_owners(r["slug"]))
            fm = FakeM(); await demo_bot.on_alerts(fm)
            assert len(fm.answers) == 4 and "📉" in fm.answers[0][0] and "🆕" in fm.answers[2][0], fm.answers
            import re
            for text, markup in screens + fm.answers:
                assert len(text) < 4096, len(text)
                assert text.count("<blockquote") == text.count("</blockquote>")
                for inner in re.findall(r"<blockquote[^>]*>(.*?)</blockquote>", text, re.S):  # по документации внутри цитаты нельзя вкладывать другие сущности
                    assert not re.search(r"<(a |code|pre|tg-time|tg-emoji|blockquote)", inner), inner
                for b in (buttons(markup) if markup else []):
                    assert len(getattr(b, "callback_data", "").encode()) <= 64, b.callback_data
    print("ALL_TESTS_OK")
asyncio.run(run())
