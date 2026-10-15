"""
Реферальный Telegram-бот: 3 ⭐ за каждого приглашённого друга.

Установка:  pip install aiogram aiosqlite
Запуск:     BOT_TOKEN=xxx ADMIN_ID=123456 CHANNEL=@tour6767 python referral_bot.py

Бот должен быть админом в канале CHANNEL (нужно для проверки подписки).
Если проверка подписки не нужна — CHANNEL="" (пусто).
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
CHANNEL = os.getenv("CHANNEL", "@tour6767")  # "" = без проверки подписки
REWARD = 3          # звёзд за приглашённого
MIN_WITHDRAW = 15   # минимум для вывода
DB = os.getenv("DB_PATH", "bot.db")

router = Router()
log = logging.getLogger("refbot")


# ───────────────────────── БАЗА ─────────────────────────
async def init_db():
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
            """
        )
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
    b.button(text="📢 Турниры Brawl Stars", url=f"https://t.me/{CHANNEL.lstrip('@')}" if CHANNEL else "https://t.me/tour6767")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")]]
    )


def sub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{CHANNEL.lstrip('@')}")],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")],
        ]
    )


# ───────────────────────── ЛОГИКА ─────────────────────────
async def is_subscribed(bot: Bot, uid: int) -> bool:
    if not CHANNEL:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL, uid)
        return m.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("sub check failed: %s", e)
        return False


async def try_reward(bot: Bot, uid: int):
    """Начисляем 3 ⭐ пригласившему, один раз, после прохождения подписки."""
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT referrer_id, rewarded, first_name FROM users WHERE id=?", (uid,))
        u = await cur.fetchone()
        if not u or not u["referrer_id"] or u["rewarded"]:
            return
        await db.execute("UPDATE users SET rewarded=1 WHERE id=?", (uid,))
        await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (REWARD, u["referrer_id"]))
        await db.commit()
    try:
        await bot.send_message(
            u["referrer_id"],
            f"🎉 По твоей ссылке пришёл <b>{u['first_name']}</b>!\n+{REWARD} ⭐ на баланс.",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def send_menu(target: Message, uid: int, edit=False):
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

    if not await is_subscribed(bot, uid):
        await m.answer(
            "Чтобы пользоваться ботом, подпишись на канал с турнирами 👇",
            reply_markup=sub_kb(),
        )
        return
    await try_reward(bot, uid)
    await send_menu(m, uid)


@router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery, bot: Bot):
    if not await is_subscribed(bot, c.from_user.id):
        await c.answer("Ты ещё не подписан ❌", show_alert=True)
        return
    await try_reward(bot, c.from_user.id)
    await c.answer("Подписка подтверждена ✅")
    await send_menu(c.message, c.from_user.id, edit=True)


@router.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):
    await send_menu(c.message, c.from_user.id, edit=True)
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
        "&text=Заходи%20в%20бота%20—%20бесплатные%20турниры%20Brawl%20Stars%20и%20звёзды%20⭐"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться с другом", url=share)],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="menu")],
        ]
    )
    await c.message.edit_text(
        f"🔗 <b>Твоя реферальная ссылка:</b>\n\n<code>{url}</code>\n\n"
        f"За каждого друга, который запустит бота и подпишется на канал, "
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
        text = "👥 <b>Твои рефералы</b> (последние 15)\n\n" + "\n".join(lines) + \
               "\n\n✅ — начислено, ⏳ — ещё не подписался"
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
        f"🏆 <b>Топ-10 по приглашениям</b>\n\n{body}", reply_markup=back_kb(), parse_mode="HTML"
    )
    await c.answer()


@router.callback_query(F.data == "help")
async def help_(c: CallbackQuery):
    await c.message.edit_text(
        "ℹ️ <b>Как это работает</b>\n\n"
        "1️⃣ Открой «🔗 Моя ссылка» и отправь её друзьям\n"
        "2️⃣ Друг запускает бота и подписывается на канал\n"
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
            await c.answer(f"Минимум для вывода — {MIN_WITHDRAW} ⭐. Сейчас у тебя {bal}.", show_alert=True)
            return
        await db.execute("UPDATE users SET balance=0 WHERE id=?", (uid,))
        cur = await db.execute("INSERT INTO withdrawals(user_id, amount) VALUES(?,?)", (uid, bal))
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
        cur = await db.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (wid,))
        w = await cur.fetchone()
        if not w:
            await c.answer("Уже обработана")
            return
        if action == "wd_ok":
            await db.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (wid,))
            msg = f"💸 Заявка #{wid} выплачена: {w['amount']} ⭐. Спасибо, что с нами!"
        else:
            await db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (wid,))
            await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))
            msg = f"❌ Заявка #{wid} отклонена, {w['amount']} ⭐ вернули на баланс."
        await db.commit()
    await c.message.edit_text(c.message.html_text + f"\n\n→ {'ВЫПЛАЧЕНО' if action == 'wd_ok' else 'ОТКЛОНЕНО'}", parse_mode="HTML")
    try:
        await bot.send_message(w["user_id"], msg)
    except Exception:
        pass
    await c.answer()


# ───────────────────────── АДМИНКА ─────────────────────────
@router.message(Command("admin"))
async def admin(m: Message):
    if m.from_user.id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB) as db:
        users = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        refd = (await (await db.execute("SELECT COUNT(*) FROM users WHERE rewarded=1")).fetchone())[0]
        pend = (await (await db.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'")).fetchone())[0]
        paid = (await (await db.execute("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'")).fetchone())[0]
    await m.answer(
        f"🛠 <b>Админка</b>\n\n"
        f"Пользователей: {users}\n"
        f"Засчитанных рефералов: {refd}\n"
        f"Заявок в ожидании: {pend}\n"
        f"Выплачено всего: {paid} ⭐\n\n"
        f"Рассылка: <code>/broadcast текст</code>",
        parse_mode="HTML",
    )


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
