import asyncio
import html
import logging
import os
import re
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import ChatPermissions, Message
from dotenv import load_dotenv


load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = Path("bot.db")
WARN_LIMIT = 3
RULES = "Правила ещё не настроены."

router = Router()

HELP = """<b>Модерация</b>
/ban [причина] — бан
/unban id — разбан
/mute [30m|2h|3d] [причина] — мут
/unmute — снять мут
/kick [причина] — кик
/warn [причина] — предупреждение
/unwarn — снять предупреждение
/warnings — список предупреждений
/purge — удалить сообщения от реплая до команды
/rules — правила"""


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            moderator_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")


def is_group(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def target(message: Message):
    return message.reply_to_message.from_user if message.reply_to_message else None


def display(user) -> str:
    name = html.escape(user.full_name or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def parse_duration(value: str | None) -> timedelta | None:
    if not value:
        return timedelta(hours=1)
    match = re.fullmatch(r"(\d{1,4})\s*([mhd])", value.strip().lower())
    if not match:
        return None
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount) if unit == "h" else timedelta(days=amount)


async def require(message: Message, bot: Bot, admin_field: str) -> bool:
    if not is_group(message):
        await message.answer("Только в группе.")
        return False
    actor = await bot.get_chat_member(message.chat.id, message.from_user.id)
    actor_allowed = actor.status == ChatMemberStatus.CREATOR or (
        actor.status == ChatMemberStatus.ADMINISTRATOR and getattr(actor, admin_field, False)
    )
    if not actor_allowed:
        await message.answer("Нет прав.")
        return False
    me = await bot.get_me()
    bot_member = await bot.get_chat_member(message.chat.id, me.id)
    bot_allowed = bot_member.status == ChatMemberStatus.CREATOR or getattr(bot_member, admin_field, False)
    if not bot_allowed:
        await message.answer("У бота нет нужных прав.")
        return False
    return True


async def require_target(message: Message):
    user = target(message)
    if not user:
        await message.answer("Ответьте этой командой на сообщение участника.")
        return None
    if user.is_bot:
        await message.answer("Ботов не модерирую.")
        return None
    return user


async def protect_admin(message: Message, bot: Bot, user) -> bool:
    member = await bot.get_chat_member(message.chat.id, user.id)
    if member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        await message.answer("Админов не трогаю.")
        return False
    return True


def reason(args: str | None) -> str:
    return html.escape(args.strip()) if args and args.strip() else "без причины"


def mute_arguments(args: str | None) -> tuple[timedelta | None, str | None]:
    """Возвращает время и причину; None во времени означает неверный формат."""
    text = (args or "").strip()
    if not text:
        return timedelta(hours=1), ""
    match = re.fullmatch(r"(\d{1,4}\s*[mhd])(?:\s+(.+))?", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return parse_duration(match.group(1)), match.group(2) or ""
    if re.match(r"\d", text):
        return None, None
    return timedelta(hours=1), text


@router.message(Command("help"))
async def help_command(message: Message):
    await message.answer(HELP)


@router.message(Command("rules"))
async def rules(message: Message):
    await message.answer(RULES)


@router.message(Command("ban"))
async def ban(message: Message, bot: Bot, command: CommandObject):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user or not await protect_admin(message, bot, user):
        return
    await bot.ban_chat_member(message.chat.id, user.id)
    await message.answer(f"🚫 {display(user)} — бан. {reason(command.args)}")


@router.message(Command("unban"))
async def unban(message: Message, bot: Bot, command: CommandObject):
    if not await require(message, bot, "can_restrict_members"):
        return
    with suppress(ValueError, TypeError):
        user_id = int((command.args or "").strip())
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
        await message.answer("✅ Разбанен.")
        return
    await message.answer("Использование: /unban <telegram_id>")


@router.message(Command("mute"))
async def mute(message: Message, bot: Bot, command: CommandObject):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user or not await protect_admin(message, bot, user):
        return
    duration, mute_reason = mute_arguments(command.args)
    if duration is None:
        await message.answer("Время: /mute 15m, /mute 2 h или /mute 3d. Причину можно написать с новой строки.")
        return
    until = datetime.now(timezone.utc) + duration
    await bot.restrict_chat_member(message.chat.id, user.id, ChatPermissions(can_send_messages=False), until_date=until)
    raw_duration = re.match(r"\d{1,4}\s*[mhd]", (command.args or "").strip(), re.IGNORECASE)
    duration_label = raw_duration.group(0).replace(" ", "") if raw_duration else "1h"
    await message.answer(f"🔇 {display(user)} — мут на {duration_label}. {reason(mute_reason)}")


@router.message(Command("unmute"))
async def unmute(message: Message, bot: Bot):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user or not await protect_admin(message, bot, user):
        return
    await bot.restrict_chat_member(message.chat.id, user.id, ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=True, can_invite_users=True, can_pin_messages=True))
    await message.answer(f"🔊 {display(user)} — мут снят.")


@router.message(Command("kick"))
async def kick(message: Message, bot: Bot, command: CommandObject):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user or not await protect_admin(message, bot, user):
        return
    await bot.ban_chat_member(message.chat.id, user.id)
    await bot.unban_chat_member(message.chat.id, user.id)
    await message.answer(f"👢 {display(user)} — кик. {reason(command.args)}")


@router.message(Command("warn"))
async def warn(message: Message, bot: Bot, command: CommandObject):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user or not await protect_admin(message, bot, user):
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO warnings(chat_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)", (message.chat.id, user.id, message.from_user.id, reason(command.args), datetime.now(timezone.utc).isoformat()))
        count = conn.execute("SELECT COUNT(*) FROM warnings WHERE chat_id = ? AND user_id = ?", (message.chat.id, user.id)).fetchone()[0]
    if count >= WARN_LIMIT:
        await bot.ban_chat_member(message.chat.id, user.id)
        await message.answer(f"🚫 {display(user)} — {count}/{WARN_LIMIT}, бан.")
    else:
        await message.answer(f"⚠️ {display(user)} — предупреждение {count}/{WARN_LIMIT}. {reason(command.args)}")


@router.message(Command("unwarn"))
async def unwarn(message: Message, bot: Bot):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user:
        return
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1", (message.chat.id, user.id)).fetchone()
        if row:
            conn.execute("DELETE FROM warnings WHERE id = ?", (row[0],))
    await message.answer("✅ Предупреждение снято." if row else "У него нет предупреждений.")


@router.message(Command("warnings"))
async def warnings(message: Message):
    user = target(message)
    if not user:
        await message.answer("Ответьте на сообщение участника.")
        return
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT reason FROM warnings WHERE chat_id = ? AND user_id = ? ORDER BY id", (message.chat.id, user.id)).fetchall()
    if not rows:
        await message.answer(f"У {display(user)} нет предупреждений.")
        return
    await message.answer(f"⚠️ {display(user)} — {len(rows)}/{WARN_LIMIT}\n" + "\n".join(f"{i + 1}. {r[0]}" for i, r in enumerate(rows)))


@router.message(Command("purge"))
async def purge(message: Message, bot: Bot):
    if not await require(message, bot, "can_delete_messages"):
        return
    if not message.reply_to_message:
        await message.answer("Ответьте на первое сообщение, которое надо удалить.")
        return
    start, end = message.reply_to_message.message_id, message.message_id
    if end - start > 100:
        await message.answer("За раз можно удалить до 100 сообщений.")
        return
    deleted = 0
    for message_id in range(start, end + 1):
        with suppress(Exception):
            await bot.delete_message(message.chat.id, message_id)
            deleted += 1
    with suppress(Exception):
        await bot.send_message(message.chat.id, f"🧹 Удалено: {deleted}")


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("В .env не задан BOT_TOKEN")
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    asyncio.run(main())
