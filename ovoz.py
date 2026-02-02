
import asyncio
import aiosqlite
import logging
from collections import defaultdict
from typing import Optional, Tuple, List

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaVideo, InputMediaDocument
)

# ================== SOZLAMALAR ==================
TOKEN = "8261172068:AAESmLWKwH74zKKu_IMMHSynU_wGOb6eyNo"   # <-- TOKENNI BU YERGA QO'YING (yangisini!)

CHANNEL_ID = -1003855350317      # <-- kanal ID
ADMIN_IDS = {2001525037}             # <-- admin user_id lar

DB_PATH = "votes.db"

MEDIA_GROUP_WAIT = 1.3               # albom yig'ish (sek)
AFTER_SEND_EDIT_DELAY = 1.2          # kanalga yuborgandan keyin edit qilish (sek)
# =================================================

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()

# Albom bufferlar
media_buffer: defaultdict[Tuple[int, str], List[Message]] = defaultdict(list)
media_tasks: dict[Tuple[int, str], asyncio.Task] = {}


# ---------- Keyboard ----------
def vote_kb(video_id: int, votes: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❤️ {votes} ta ovoz", callback_data=f"vote:{video_id}")]
    ])


# ---------- DB ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS videos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_msg_id INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS votes(
                video_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (video_id, user_id)
            )
        """)
        # IG/YT gate (3 bosishda ochiladi)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS igyt_gate(
                user_id INTEGER PRIMARY KEY,
                step INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.commit()


async def add_video_stub() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("INSERT INTO videos(channel_msg_id) VALUES(0)")
        await db.commit()
        return int(cur.lastrowid)


async def set_video_msg(video_id: int, channel_msg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE videos SET channel_msg_id=? WHERE id=?", (channel_msg_id, video_id))
        await db.commit()


async def get_channel_msg_id(video_id: int) -> Optional[int]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT channel_msg_id FROM videos WHERE id=?", (video_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None


async def has_voted(video_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM votes WHERE video_id=? AND user_id=?",
            (video_id, user_id)
        ) as cur:
            return (await cur.fetchone()) is not None


async def add_vote(video_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO votes(video_id, user_id) VALUES(?, ?)",
            (video_id, user_id)
        )
        await db.commit()


async def remove_vote(video_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM votes WHERE video_id=? AND user_id=?",
            (video_id, user_id)
        )
        await db.commit()


async def vote_count(video_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM votes WHERE video_id=?",
            (video_id,)
        ) as cur:
            (cnt,) = await cur.fetchone()
            return int(cnt)


# ---------- IG/YT gate ----------
async def gate_step(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO igyt_gate(user_id, step) VALUES(?, 0)", (user_id,))
        await db.commit()
        async with db.execute("SELECT step FROM igyt_gate WHERE user_id=?", (user_id,)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def gate_set_step(user_id: int, step: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO igyt_gate(user_id, step) VALUES(?, 0)", (user_id,))
        await db.execute("UPDATE igyt_gate SET step=? WHERE user_id=?", (step, user_id))
        await db.commit()


# ---------- Subscription check (Telegram kanal) ----------
async def is_user_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logging.exception("get_chat_member error: %s", e)
        return False


# ---------- Helpers ----------
def is_video_doc(m: Message) -> bool:
    return bool(m.document and (m.document.mime_type or "").startswith("video/"))


def get_video_file_id(m: Message) -> Optional[str]:
    if m.video:
        return m.video.file_id
    if is_video_doc(m):
        return m.document.file_id
    return None


def pick_caption(msgs: List[Message]) -> tuple[Optional[str], Optional[list]]:
    for m in msgs:
        if m.caption:
            return m.caption, m.caption_entities
    return None, None


async def safe_edit_kb(bot: Bot, chat_id: int, message_id: int, kb: InlineKeyboardMarkup):
    """
    Albomdan keyin message "tayyor" bo'lishi kechikadi.
    Shuning uchun retry bilan edit qilamiz.
    """
    for attempt in range(1, 7):  # 6 marta urinadi
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=kb)
            return
        except Exception as e:
            s = str(e).lower()
            if "message is not modified" in s:
                return
            # albomda ko‘p uchraydigan kechikish xatolari -> retry
            if any(x in s for x in ["message to edit not found", "message_id_invalid", "message can't be edited"]):
                await asyncio.sleep(1.0)
                continue
            logging.exception("edit kb error (attempt %s): %s", attempt, e)
            await asyncio.sleep(1.0)


async def post_single_video(bot: Bot, m: Message):
    file_id = get_video_file_id(m)
    if not file_id:
        return

    video_id = await add_video_stub()

    # caption aynan sizniki
    sent = await bot.send_video(
        chat_id=CHANNEL_ID,
        video=file_id,
        caption=m.caption,
        caption_entities=m.caption_entities
    )

    await set_video_msg(video_id, sent.message_id)

    await asyncio.sleep(AFTER_SEND_EDIT_DELAY)
    cnt = await vote_count(video_id)
    await safe_edit_kb(bot, CHANNEL_ID, sent.message_id, vote_kb(video_id, cnt))


async def flush_media_group(bot: Bot, admin_chat_id: int, mg_key: Tuple[int, str]):
    msgs = media_buffer.pop(mg_key, [])
    media_tasks.pop(mg_key, None)

    if not msgs:
        return

    msgs.sort(key=lambda x: x.message_id)

    caption, caption_entities = pick_caption(msgs)

    medias = []
    for i, msg in enumerate(msgs):
        fid = get_video_file_id(msg)
        if not fid:
            continue
        if i == 0:
            # caption faqat 1-chisida (admin yuborgani)
            medias.append(InputMediaVideo(media=fid, caption=caption, caption_entities=caption_entities))
        else:
            medias.append(InputMediaVideo(media=fid))

    # 2 tadan kam bo‘lsa -> single qilib yuboramiz
    if len(medias) < 2:
        await post_single_video(bot, msgs[0])
        return

    video_id = await add_video_stub()

    sent_msgs = await bot.send_media_group(chat_id=CHANNEL_ID, media=medias)

    first_msg = sent_msgs[0]
    await set_video_msg(video_id, first_msg.message_id)

    await asyncio.sleep(AFTER_SEND_EDIT_DELAY)
    cnt = await vote_count(video_id)
    await safe_edit_kb(bot, CHANNEL_ID, first_msg.message_id, vote_kb(video_id, cnt))


async def _delayed_flush(bot: Bot, admin_chat_id: int, mg_key: Tuple[int, str]):
    try:
        await asyncio.sleep(MEDIA_GROUP_WAIT)
        await flush_media_group(bot, admin_chat_id, mg_key)
    except asyncio.CancelledError:
        pass


# ================== HANDLERS ==================
@dp.message(Command("start"))
async def start(m: Message):
    await m.answer(
        "✅ Bot ishlayapti.\n"
        "Admin 1 ta video yuborsa — kanalga caption + ❤️.\n"
        "Admin 2+ video albom yuborsa — kanalga albom + ❤️."
    )


@dp.message(Command("me"))
async def me(m: Message):
    await m.answer(f"Sizning user_id: {m.from_user.id}")


# Admin yuborgan video (single yoki album)
@dp.message(F.video | F.document)
async def admin_upload(m: Message, bot: Bot):
    if m.from_user.id not in ADMIN_IDS:
        return

    if not get_video_file_id(m):
        return

    # Albom bo‘lsa
    if m.media_group_id:
        mg_key = (m.chat.id, str(m.media_group_id))
        media_buffer[mg_key].append(m)

        old = media_tasks.get(mg_key)
        if old and not old.done():
            old.cancel()

        media_tasks[mg_key] = asyncio.create_task(_delayed_flush(bot, m.chat.id, mg_key))
        return

    # Single bo‘lsa
    await post_single_video(bot, m)


# ❤️ Vote: TG obuna + IG/YT 3-bosish gate + toggle
@dp.callback_query(F.data.startswith("vote:"))
async def on_vote(cb: CallbackQuery, bot: Bot):
    user_id = cb.from_user.id
    video_id = int(cb.data.split("vote:", 1)[1])

    # 1) Telegram kanalga obuna tekshiruv (real)
    if not await is_user_subscribed(bot, user_id):
        await cb.answer("❗ Siz kanalga obuna bo‘lmagansiz. Ovoz berish mumkin emas.", show_alert=True)
        return

    # 2) IG/YT gate (real tekshiruv yo‘q, 3 bosishda ochiladi)
    step = await gate_step(user_id)
    if step < 3:
        if step == 0:
            await gate_set_step(user_id, 1)
            await cb.answer(
                "❗ Siz Instagram va YouTube kanaliga obuna bo‘lmadingiz.\n"
                "Iltimos obuna bo‘lib qaytadan ❤️ bosing.",
                show_alert=True
            )
            return
        if step == 1:
            await gate_set_step(user_id, 2)
            await cb.answer(
                "❗ Siz YouTube kanaliga obuna bo‘lmadingiz.\n"
                "Iltimos obuna bo‘lib qaytadan ❤️ bosing.",
                show_alert=True
            )
            return
        if step == 2:
            await gate_set_step(user_id, 3)
            # 3-marta: endi pastga o‘tib vote qiladi

    # 3) Toggle vote
    if await has_voted(video_id, user_id):
        await remove_vote(video_id, user_id)
        action_text = "Ovozingiz qaytarib olindi ❌"
    else:
        await add_vote(video_id, user_id)
        action_text = "Ovozingiz qabul qilindi ✅"

    # 4) Tugmani yangilash
    cnt = await vote_count(video_id)
    ch_msg_id = await get_channel_msg_id(video_id)
    if ch_msg_id:
        await safe_edit_kb(bot, CHANNEL_ID, ch_msg_id, vote_kb(video_id, cnt))

    await cb.answer(action_text)


@dp.message(Command("results"))
async def results(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT v.id, COUNT(t.user_id) AS cnt
            FROM videos v
            LEFT JOIN votes t ON t.video_id = v.id
            GROUP BY v.id
            ORDER BY cnt DESC, v.id ASC
            LIMIT 50
        """) as cur:
            rows = await cur.fetchall()

    if not rows:
        await m.answer("Hali video yo‘q.")
        return

    text = "🏆 Reyting:\n"
    for i, (vid, cnt) in enumerate(rows, start=1):
        text += f"{i}) Video #{vid} — {cnt} ovoz\n"
    await m.answer(text)


async def main():
    await init_db()
    bot = Bot(TOKEN)
    logging.info("✅ Bot started polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
