"""
Реферальный Telegram-бот: 3 ⭐ за каждого приглашённого друга.
Версия 2: каналы ОП настраиваются командами в боте.

Переменные в Railway:
  BOT_TOKEN  - токен от @BotFather
  ADMIN_ID   - твой Telegram ID
  CHANNEL    - (необязательно) первый канал ОП при самом первом запуске, например @tour6767
  DB_PATH    - /data/bot.db (нужен Volume с путём /data)
  BTN_TEXT   - (необязательно) текст кнопки в меню, например 📢 Наш канал
  BTN_URL    - (необязательно) ссылка кнопки; пусто = кнопки нет

Команды админа:
  /admin  /addop @канал  /delop @канал  /listop  /broadcast текст
"""
import asyncio
import os
import logging

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
CHANNEL = os.getenv("CHANNEL", "")
BTN_TEXT = os.getenv("BTN_TEXT", "")
BTN_URL = os.getenv("BTN_URL", "")
REWARD = 3          # звёзд за приглашённого
MIN_WITHDRAW = 15   # минимум для вывода
DB = os.getenv("DB_PATH", "bot.db")

router = Router()
log = logging.getLogger("refbot")


# ───────────────────────── БАЗА ─────────────────────────
async def init_db():
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    async with aiosqlite.connect(DB) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referrer_id INTEGER,
                balance INTEGER DEFAULT 0,
                rewarded INTEGER DEFAULT 0,
                joined TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS withdrawals(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount INTEGER,
                status TEXT DEFAULT 'pending',
                created TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channels(username TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        # при самом первом запуске кладём CHANNEL в ОП (один раз)
        cur = await db.execute("SELECT 1 FROM meta WHERE key='seeded'")
        if not await cur.fetchone():
            if CHANNEL:
                ch = CHANNEL if CHANNEL.startswith("@") else "@" + CHANNEL
                await db.execute("INSERT OR IGNORE INTO channels VALUES(?)", (ch,))
            await db.execute("INSERT INTO meta VALUES('seeded','1')")
        await db.commit()


async def get_user(uid: int):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM users WHERE id=?", (uid,))
        return await cur.fetchone()


async def add_user(uid, username, first_name, referrer_id):
    """Возвращает True, если пользователь новый."""
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT 1 FROM users WHERE id=?", (uid,))
        if await cur.fetchone():
            return False
        await db.execute(
            "INSERT INTO users(id, username, first_name, referrer_id) VALUES(?,?,?,?)",
            (uid, username, first_name, referrer_id),
        )
        await db.commit()
        return True


async def ref_count(uid: int, only_rewarded=True) -> int:
    q = "SELECT COUNT(*) FROM users WHERE referrer_id=?"
    if only_rewarded:
        q += " AND rewarded=1"
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(q, (uid,))
        return (await cur.fetchone())[0]


# ───────────────────────── КЛАВИАТУРЫ ─────────────────────────
def main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🔗 Моя ссылка", callback_data="link")
    b.button(text="👥 Мои рефералы", callback_data="refs")
    b.button(text="🏆 Топ", callback_data="top")
    b.button(text="💸 Вывести ⭐", callback_data="withdraw")
    b.button(text="ℹ️ Как это работает", callback_data="help")
    if BTN_URL:
        b.button(text=BTN_TEXT or "📢 Наш канал", url=BTN_URL)
        b.adjust(2, 2, 2, 1)
    else:
        b.adjust(2, 2, 2)
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]]
    )


def sub_kb(missing: list) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📢 {ch}", url=f"https://t.me/{ch.lstrip('@')}")]
        for ch in missing
    ]
    rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ───────────────────────── ЛОГИКА ─────────────────────────
async def get_channels() -> list:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT username FROM channels")
        return [r[0] for r in await cur.fetchall()]


async def not_subscribed(bot: Bot, uid: int) -> list:
    """Список каналов ОП, на которые пользователь ещё не подписан."""
    missing = []
    for ch in await get_channels():
        try:
            m = await bot.get_chat_member(ch, uid)
            if m.status not in ("member", "administrator", "creator"):
                missing.append(ch)
        except Exception as e:
            # бот не админ в канале или канал не найден: людей не блокируем
            log.warning("ОП %s не проверить: %s", ch, e)
    return missing


async def try_reward(bot: Bot, uid: int):
    """Начисляем 3 ⭐ пригласившему, один раз, после прохождения ОП."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT referrer_id, rewarded, first_name FROM users WHERE id=?", (uid,)
        )
        u = await cur.fetchone()
        if not u or not u["referrer_id"] or u["rewarded"]:
            return
        await db.execute("UPDATE users SET rewarded=1 WHERE id=?", (uid,))
        await db.execute(
            "UPDATE users SET balance=balance+? WHERE id=?", (REWARD, u["referrer_id"])
        )
        await db.commit()
    try:
        await bot.send_message(
            u["referrer_id"],
            f"🎉 По твоей ссылке пришёл <b>{u['first_name']}</b>!\n+{REWARD} ⭐ на баланс.",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def send_menu(target: Message, edit=False):
    text = (
        "👋 <b>Добро пожаловать!</b>\n\n"
        f"Приглашай друзей и получай <b>{REWARD} ⭐</b> за каждого.\n"
        "Выбирай действие ниже 👇"
    )
    if edit:
        await target.edit_text(text, reply_markup=main_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=main_kb(), parse_mode="HTML")


# ───────────────────────── ХЕНДЛЕРЫ ─────────────────────────
@router.message(CommandStart())
async def start(m: Message, command: CommandObject, bot: Bot):
    uid = m.from_user.id
    ref = None
    if command.args and command.args.isdigit():
        r = int(command.args)
        if r != uid and await get_user(r):  # нельзя пригласить себя
            ref = r
    await add_user(uid, m.from_user.username, m.from_user.first_name, ref)

    missing = await not_subscribed(bot, uid)
    if missing:
        await m.answer(
            "Подпишись на каналы, чтобы пользоваться ботом 👇",
            reply_markup=sub_kb(missing),
        )
        return
    await try_reward(bot, uid)
    await send_menu(m)


@router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    missing = await not_subscribed(bot, c.from_user.id)
    if missing:
        try:
            await c.message.edit_reply_markup(reply_markup=sub_kb(missing))
        except Exception:
            pass
        await c.answer("Подписался не на все ❌", show_alert=True)
        return
    await try_reward(bot, c.from_user.id)
    await c.answer("Подписка подтверждена ✅")
    await send_menu(c.message, edit=True)


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):
    await send_menu(c.message, edit=True)
    await c.answer()


@router.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    n = await ref_count(c.from_user.id)
    await c.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"ID: <code>{u['id']}</code>\n"
        f"💰 Баланс: <b>{u['balance']} ⭐</b>\n"
        f"👥 Приглашено: <b>{n}</b>\n"
        f"📅 В боте с: {u['joined'][:10]}",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "link")
async def link(c: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    url = f"https://t.me/{me.username}?start={c.from_user.id}"
    share = (
        "https://t.me/share/url?url=" + url +
        "&text=Заходи%20в%20бота%20и%20получай%20звёзды%20⭐"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться с другом", url=share)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
        ]
    )
    await c.message.edit_text(
        f"🔗 <b>Твоя реферальная ссылка:</b>\n\n<code>{url}</code>\n\n"
        f"За каждого друга, который запустит бота и подпишется на каналы, "
        f"ты получишь <b>{REWARD} ⭐</b>.",
        reply_markup=kb,
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "refs")
async def refs(c: CallbackQuery):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT first_name, rewarded FROM users WHERE referrer_id=? ORDER BY joined DESC LIMIT 15",
            (c.from_user.id,),
        )
        rows = await cur.fetchall()
    if not rows:
        text = "У тебя пока нет рефералов. Забери ссылку в «🔗 Моя ссылка» и поделись!"
    else:
        lines = [f"{'✅' if r[1] else '⏳'} {r[0]}" for r in rows]
        text = (
            "👥 <b>Твои рефералы</b> (последние 15)\n\n" + "\n".join(lines)
            + "\n\n✅ — начислено, ⏳ — ещё не подписался"
        )
    await c.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    await c.answer()


@router.callback_query(F.data == "top")
async def top(c: CallbackQuery):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            """SELECT u.first_name, COUNT(r.id) AS n
               FROM users u JOIN users r ON r.referrer_id=u.id AND r.rewarded=1
               GROUP BY u.id ORDER BY n DESC LIMIT 10"""
        )
        rows = await cur.fetchall()
    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
    if rows:
        body = "\n".join(f"{medals[i]} {r[0]} — {r[1]} чел." for i, r in enumerate(rows))
    else:
        body = "Пока пусто. Стань первым!"
    await c.message.edit_text(
        f"🏆 <b>Топ-10 по приглашениям</b>\n\n{body}",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "help")
async def help_(c: CallbackQuery):
    await c.message.edit_text(
        "ℹ️ <b>Как это работает</b>\n\n"
        "1️⃣ Открой «🔗 Моя ссылка» и отправь её друзьям\n"
        "2️⃣ Друг запускает бота и подписывается на каналы\n"
        f"3️⃣ Тебе падает <b>{REWARD} ⭐</b>\n"
        f"4️⃣ От {MIN_WITHDRAW} ⭐ можно подать заявку на вывод\n\n"
        "⚠️ Накрутка и самоприглашения не засчитываются.",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await c.answer()


@router.callback_query(F.data == "withdraw")
async def withdraw(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT balance FROM users WHERE id=?", (uid,))
        bal = (await cur.fetchone())[0]
        if bal < MIN_WITHDRAW:
            await c.answer(
                f"Минимум для вывода — {MIN_WITHDRAW} ⭐. Сейчас у тебя {bal}.",
                show_alert=True,
            )
            return
        await db.execute("UPDATE users SET balance=0 WHERE id=?", (uid,))
        cur = await db.execute(
            "INSERT INTO withdrawals(user_id, amount) VALUES(?,?)", (uid, bal)
        )
        wid = cur.lastrowid
        await db.commit()

    await c.message.edit_text(
        f"✅ Заявка #{wid} на <b>{bal} ⭐</b> отправлена. Обычно обрабатываем до 24 часов.",
        reply_markup=back_kb(),
        parse_mode="HTML",
    )
    await c.answer()
    if ADMIN_ID:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="✅ Выплачено", callback_data=f"wd_ok:{wid}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"wd_no:{wid}"),
            ]]
        )
        uname = f"@{c.from_user.username}" if c.from_user.username else c.from_user.first_name
        await bot.send_message(
            ADMIN_ID,
            f"💸 Заявка #{wid}\nОт: {uname} (<code>{uid}</code>)\nСумма: <b>{bal} ⭐</b>",
            reply_markup=kb,
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith(("wd_ok:", "wd_no:")))
async def wd_decide(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID:
        await c.answer("Нет доступа", show_alert=True)
        return
    action, wid = c.data.split(":")
    wid = int(wid)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,)
        )
        w = await cur.fetchone()
        if not w:
            await c.answer("Уже обработана")
            return
        if action == "wd_ok":
            await db.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
            msg = f"💸 Заявка #{wid} выплачена: {w['amount']} ⭐. Спасибо, что с нами!"
        else:
            await db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
            await db.execute(
                "UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"])
            )
            msg = f"❌ Заявка #{wid} отклонена, {w['amount']} ⭐ вернули на баланс."
        await db.commit()
    await c.message.edit_text(
        c.message.html_text + f"\n\n→ {'ВЫПЛАЧЕНО' if action == 'wd_ok' else 'ОТКЛОНЕНО'}",
        parse_mode="HTML",
    )
    try:
        await bot.send_message(w["user_id"], msg)
    except Exception:
        pass
    await c.answer()


# ───────────────────────── АДМИНКА ─────────────────────────
def norm_channel(raw: str) -> str:
    ch = raw.strip().replace("https://t.me/", "")
    return ch if ch.startswith("@") else "@" + ch


@router.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB) as db:
        users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        refd = (await (await db.execute("SELECT COUNT(*) FROM users WHERE rewarded=1")).fetchone())[0]
        pend = (await (await db.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")).fetchone())[0]
        paid = (await (await db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'")).fetchone())[0]
    chs = await get_channels()
    await m.answer(
        f"🛠 <b>Админка</b>\n\n"
        f"Пользователей: {users}\n"
        f"Засчитанных рефералов: {refd}\n"
        f"Заявок в ожидании: {pend}\n"
        f"Выплачено всего: {paid} ⭐\n"
        f"Каналов в ОП: {len(chs)}\n\n"
        f"<b>Команды:</b>\n"
        f"/addop @канал — добавить в ОП\n"
        f"/delop @канал — убрать из ОП\n"
        f"/listop — список каналов ОП\n"
        f"/broadcast текст — рассылка",
        parse_mode="HTML",
    )


@router.message(Command("addop"))
async def addop(m: Message, command: CommandObject, bot: Bot):
    if m.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await m.answer("Напиши так: /addop @channel")
        return
    ch = norm_channel(command.args)
    try:
        me = await bot.get_me()
        st = (await bot.get_chat_member(ch, me.id)).status
        if st != "administrator":
            raise ValueError("not admin")
    except Exception:
        await m.answer(
            f"Не получилось добавить {ch}.\n"
            "Проверь: 1) канал публичный и ты написал @username правильно, "
            "2) бот добавлен в канал АДМИНИСТРАТОРОМ."
        )
        return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO channels VALUES(?)", (ch,))
        await db.commit()
    await m.answer(f"✅ {ch} добавлен в ОП")


@router.message(Command("delop"))
async def delop(m: Message, command: CommandObject):
    if m.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await m.answer("Напиши так: /delop @channel")
        return
    ch = norm_channel(command.args)
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM channels WHERE username=?", (ch,))
        await db.commit()
    await m.answer(f"🗑 {ch} убран из ОП")


@router.message(Command("listop"))
async def listop(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    chs = await get_channels()
    await m.answer("Каналы ОП:\n" + ("\n".join(chs) if chs else "пусто"))


@router.message(Command("broadcast"))
async def broadcast(m: Message, command: CommandObject, bot: Bot):
    if m.from_user.id != ADMIN_ID or not command.args:
        return
    async with aiosqlite.connect(DB) as db:
        ids = [r[0] for r in await (await db.execute("SELECT id FROM users")).fetchall()]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, command.args)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # лимиты Telegram
    await m.answer(f"Отправлено: {ok}/{len(ids)}")


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
