import asyncio
import html
import logging
import os
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
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
/ban [@username] [причина] — бан
/ban [telegram_id] [причина] — бан по ID
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
        conn.execute("""CREATE TABLE IF NOT EXISTS known_users (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS known_users_username ON known_users(chat_id, username)")


@dataclass
class StoredUser:
    id: int
    full_name: str
    is_bot: bool = False


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
    try:
        member = await bot.get_chat_member(message.chat.id, user.id)
    except TelegramBadRequest:
        return True
    if member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
        await message.answer("Админов не трогаю.")
        return False
    return True


def reason(args: str | None) -> str:
    return html.escape(args.strip()) if args and args.strip() else "без причины"


def username_arguments(args: str | None) -> tuple[str | None, str | None]:
    match = re.fullmatch(r"@([A-Za-z0-9_]{5,32})(?:\s+(.+))?", (args or "").strip(), flags=re.DOTALL)
    return (match.group(1).lower(), match.group(2) or "") if match else (None, None)


def id_arguments(args: str | None) -> tuple[int | None, str | None]:
    match = re.fullmatch(r"(\d{5,20})(?:\s+(.+))?", (args or "").strip(), flags=re.DOTALL)
    return (int(match.group(1)), match.group(2) or "") if match else (None, None)


def known_user(chat_id: int, username: str) -> StoredUser | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, full_name FROM known_users WHERE chat_id = ? AND lower(username) = ? ORDER BY updated_at DESC LIMIT 1",
            (chat_id, username.lower()),
        ).fetchone()
    return StoredUser(id=row[0], full_name=row[1]) if row else None


def known_user_by_id(chat_id: int, user_id: int) -> StoredUser | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT user_id, full_name FROM known_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    return StoredUser(id=row[0], full_name=row[1]) if row else None


async def ban_target(message: Message, command_args: str | None):
    replied_user = target(message)
    if replied_user:
        return replied_user, command_args
    username, ban_reason = username_arguments(command_args)
    if username:
        user = known_user(message.chat.id, username)
        if not user:
            await message.answer("Не знаю этого пользователя. Он должен хотя бы раз написать в этом чате.")
            return None, None
        return user, ban_reason
    user_id, ban_reason = id_arguments(command_args)
    if user_id:
        return known_user_by_id(message.chat.id, user_id) or StoredUser(id=user_id, full_name=str(user_id)), ban_reason
    await message.answer("Ответьте на сообщение или используйте: /ban @username [причина], /ban telegram_id [причина]")
    return None, None


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
    user, ban_reason = await ban_target(message, command.args)
    if not user or not await protect_admin(message, bot, user):
        return
    await bot.ban_chat_member(message.chat.id, user.id)
    await message.answer(f"{display(user)} — бан. {reason(ban_reason)}")


@router.message(Command("unban"))
async def unban(message: Message, bot: Bot, command: CommandObject):
    if not await require(message, bot, "can_restrict_members"):
        return
    with suppress(ValueError, TypeError):
        user_id = int((command.args or "").strip())
        await bot.unban_chat_member(message.chat.id, user_id, only_if_banned=True)
        await message.answer("Разбанен.")
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
    await message.answer(f"{display(user)} — мут на {duration_label}. {reason(mute_reason)}")


@router.message(Command("unmute"))
async def unmute(message: Message, bot: Bot):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user or not await protect_admin(message, bot, user):
        return
    await bot.restrict_chat_member(message.chat.id, user.id, ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True, can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True, can_change_info=True, can_invite_users=True, can_pin_messages=True))
    await message.answer(f"{display(user)} — мут снят.")


@router.message(Command("kick"))
async def kick(message: Message, bot: Bot, command: CommandObject):
    if not await require(message, bot, "can_restrict_members"):
        return
    user = await require_target(message)
    if not user or not await protect_admin(message, bot, user):
        return
    await bot.ban_chat_member(message.chat.id, user.id)
    await bot.unban_chat_member(message.chat.id, user.id)
    await message.answer(f"{display(user)} — кик. {reason(command.args)}")


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
        await message.answer(f"{display(user)} — {count}/{WARN_LIMIT}, бан.")
    else:
        await message.answer(f"{display(user)} — предупреждение {count}/{WARN_LIMIT}. {reason(command.args)}")


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
    await message.answer("Предупреждение снято." if row else "У него нет предупреждений.")


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
    await message.answer(f"{display(user)} — {len(rows)}/{WARN_LIMIT}\n" + "\n".join(f"{i + 1}. {r[0]}" for i, r in enumerate(rows)))


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
        await bot.send_message(message.chat.id, f"Удалено: {deleted}")


@router.message()
async def remember_user(message: Message):
    """Запоминает участников, чтобы их можно было модерировать по @username."""
    if not is_group(message) or not message.from_user:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT INTO known_users(chat_id, user_id, username, full_name, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username = excluded.username,
                full_name = excluded.full_name,
                updated_at = excluded.updated_at""",
            (
                message.chat.id,
                message.from_user.id,
                message.from_user.username,
                message.from_user.full_name,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


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
