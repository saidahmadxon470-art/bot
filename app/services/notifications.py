import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.config import get_settings


logger = logging.getLogger(__name__)


def submission_review_keyboard(submission_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlash va ball berish", callback_data=f"admin:approve:{submission_id}"),
        InlineKeyboardButton(text="❌ Rad etish", callback_data=f"admin:reject:{submission_id}"),
    ]])


async def send_submission_review_to_admins(
    bot: Bot,
    admin_ids: set[int],
    image: bytes,
    filename: str,
    submission_id: int,
    task_id: int,
    task_title: str,
    reward_points: int,
    user_telegram_id: int,
    username: str | None,
) -> int:
    if not admin_ids:
        logger.error("Submission %s was not sent to admins: ADMIN_IDS is empty", submission_id)
        return 0

    caption = (
        f"📥 Yangi hisobot · Submission #{submission_id}\n"
        f"🎯 Task #{task_id}: {task_title}\n"
        f"👤 User ID: {user_telegram_id} · @{username or 'username yo‘q'}\n"
        f"⭐ Mukofot: {reward_points} ball"
    )
    keyboard = submission_review_keyboard(submission_id)
    sent = 0
    for admin_id in admin_ids:
        try:
            upload = BufferedInputFile(image, filename=filename)
            if len(image) <= 10_000_000:
                await bot.send_photo(admin_id, upload, caption=caption, reply_markup=keyboard)
            else:
                await bot.send_document(admin_id, upload, caption=caption, reply_markup=keyboard)
            sent += 1
        except TelegramAPIError:
            logger.exception("Could not deliver submission %s to admin %s", submission_id, admin_id)

    if sent == 0:
        logger.error("Submission %s was not delivered to any configured admin", submission_id)
    return sent


async def send_user_message(
    telegram_id: int,
    message: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    token = get_settings().bot_token.get_secret_value()
    if not token:
        return False
    try:
        async with Bot(token) as bot:
            await bot.send_message(telegram_id, message, reply_markup=reply_markup)
        return True
    except Exception:
        return False


async def send_user_messages(
    telegram_ids: list[int],
    message: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    token = get_settings().bot_token.get_secret_value()
    if not token:
        return 0
    sent = 0
    async with Bot(token) as bot:
        for telegram_id in dict.fromkeys(telegram_ids):
            try:
                await bot.send_message(telegram_id, message, reply_markup=reply_markup)
                sent += 1
            except TelegramAPIError:
                logger.exception("Could not deliver Telegram notification to user %s", telegram_id)
                continue
    return sent


async def send_task_announcement(
    is_public: bool,
    telegram_ids: list[int],
    message: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> int:
    return await send_user_messages(
        telegram_ids,
        message,
        reply_markup if is_public else None,
    )