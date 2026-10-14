import asyncio
import hashlib
import io
import logging
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from PIL import Image, UnidentifiedImageError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import AdminAction, FraudScore, MatchingMode, OCRResult, PointTransaction, Setting, Submission, SubmissionStatus, Task, TaskAssignment, TaskStatus, TransactionType, User, VerificationResult
from app.services.notifications import send_submission_review_to_admins, send_task_announcement, send_user_message, submission_review_keyboard
from app.services.points import award_submission
from app.services.runtime_settings import load_runtime_settings
from app.services.tasks import available_tasks, get_available_task

settings = get_settings()
logger = logging.getLogger(__name__)
router = Router()
redis: Redis | None = None
queue: ArqRedis | asyncio.Queue[int] | None = None
local_attempts: dict[int, deque[float]] = defaultdict(deque)


class SubmissionFlow(StatesGroup):
    awaiting_screenshot = State()


class AdminTaskFlow(StatesGroup):
    task_text = State()
    audience = State()
    assignee = State()


class AdminTaskEditFlow(StatesGroup):
    task_text = State()


class AdminPointsFlow(StatesGroup):
    amount = State()


class AdminReviewFlow(StatesGroup):
    rejection_reason = State()


def is_admin(telegram_id: int | None) -> bool:
    return telegram_id is not None and telegram_id in settings.admin_id_set


def parse_admin_task_text(task_text: str) -> tuple[str, str]:
    normalized_text = task_text.strip()
    first_line = next((line.strip() for line in normalized_text.splitlines() if line.strip()), "")
    return first_line[:200] or "Jamoa topshirig‘i", normalized_text


def user_task_title(title: str, task_text: str) -> str:
    if not title.startswith("/"):
        return title
    return next(
        (line.strip()[:200] for line in task_text.splitlines()
         if line.strip() and not line.strip().startswith("/")),
        "Topshiriq",
    )


def user_task_text(title: str, task_text: str) -> str:
    if title.startswith("/"):
        lines = task_text.splitlines()
        if lines and lines[0].strip().startswith("/"):
            return "\n".join(lines[1:]).strip() or "Topshiriq tafsilotlari admin tomonidan beriladi."
    return task_text


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Topshiriqlar")],
        [KeyboardButton(text="📤 Mening topshiriqlarim")],
        [KeyboardButton(text="⭐ Ballarim")],
        [KeyboardButton(text="🏆 Reyting"), KeyboardButton(text="👤 Profil")],
        [KeyboardButton(text="📊 Statistikam")],
        [KeyboardButton(text="📜 Qoidalar"), KeyboardButton(text="❓ Yordam")],
    ], resize_keyboard=True)


def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Topshiriq berish", callback_data="admin:new_task")],
        [InlineKeyboardButton(text="📋 Topshiriqlarni ko‘rish / tahrirlash", callback_data="admin:tasks:0")],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin:users:0")],
    ])


def admin_commands() -> list[BotCommand]:
    return [
        BotCommand(command="admin", description="Admin menyusini ochish"),
        BotCommand(command="admin_users", description="Foydalanuvchilar ro‘yxati"),
        BotCommand(command="admin_pending", description="Kutilayotgan hisobotlar"),
        BotCommand(command="admin_newtask", description="Yangi topshiriq berish"),
        BotCommand(command="admin_tasks", description="Topshiriqlarni ko‘rish va tahrirlash"),
    ]


async def register_admin_commands(bot: Bot) -> None:
    commands = admin_commands()
    for scope in (BotCommandScopeDefault(), BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats()):
        await bot.delete_my_commands(scope=scope)
    for admin_telegram_id in settings.admin_id_set:
        await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=admin_telegram_id))


async def register_user(message: Message) -> User:
    assert message.from_user is not None
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user is None:
            user = User(telegram_id=message.from_user.id)
            session.add(user)
        user.username = message.from_user.username
        user.first_name = message.from_user.first_name or ""
        user.last_name = message.from_user.last_name or ""
        user.last_activity_at = datetime.now(UTC)
        await session.commit()
        await session.refresh(user)
        return user


@router.message(F.text == "/start")
async def start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await register_user(message)
    if user.is_blocked:
        await message.answer("Akkauntingiz vaqtincha bloklangan.")
        return
    await message.answer("Xush kelibsiz! Faol topshiriqlardan birini tanlang.", reply_markup=main_menu())


@router.message(F.text == "📋 Topshiriqlar")
async def show_tasks(message: Message) -> None:
    if message.from_user is None:
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        tasks = await available_tasks(session, user.id if user else None)
        if not tasks:
            await message.answer("Hozircha faol topshiriqlar yo‘q.")
            return
        rows = []
        now = datetime.now(UTC)
        for task in tasks:
            remaining = task.ends_at - now.replace(tzinfo=None) if task.ends_at.tzinfo is None else task.ends_at - now
            hours = max(0, int(remaining.total_seconds() // 3600))
            rows.append([InlineKeyboardButton(
                text=f"{user_task_title(task.title, task.advertisement_text)[:40]} · {task.reward_points} ⭐ · {hours} soat qoldi",
                callback_data=f"task:{task.id}",
            )])
    await message.answer("📢 Faol topshiriqlar", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("task:"))
async def task_detail(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    if callback.from_user is None:
        await callback.answer()
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        task = await get_available_task(session, task_id, user.id if user else None)
        if task is None:
            await callback.answer("Bu topshiriq faol emas yoki muddati tugagan.", show_alert=True)
            return
        text = (
            f"📋 {user_task_title(task.title, task.advertisement_text)}\n\n"
            f"📝 Topshiriq:\n{user_task_text(task.title, task.advertisement_text)}\n\n"
            f"⭐ Mukofot: {task.reward_points} ball\n⏰ Muddati: {task.ends_at:%d-%m-%Y %H:%M UTC}\n\n"
            f"📌 Talablar:\n{task.requirements or 'Reklamani o‘zgartirmasdan joylashtiring; postni o‘chirmang; aniq screenshot yuboring.'}"
        )
    await state.set_state(SubmissionFlow.awaiting_screenshot)
    await state.update_data(task_id=task_id)
    if task.advertisement_image_path and Path(task.advertisement_image_path).is_file():
        await callback.message.answer_photo(FSInputFile(task.advertisement_image_path), caption=f"📢 {task.title[:180]}")
    await callback.message.answer(f"{text}\n\nBajargach screenshotni shu chatga yuboring.")
    await callback.answer("Topshiriq ochildi")


async def enforce_rate_limit(user_id: int) -> bool:
    async with SessionLocal() as session:
        runtime_settings = await load_runtime_settings(session)
    if settings.local_mode:
        now = time.monotonic()
        attempts = local_attempts[user_id]
        while attempts and now - attempts[0] >= 24 * 60 * 60:
            attempts.popleft()
        minute_count = sum(now - attempt < 60 for attempt in attempts)
        if minute_count >= runtime_settings.submissions_per_minute or len(attempts) >= runtime_settings.submissions_per_day:
            return False
        attempts.append(now)
        return True

    assert redis is not None
    minute_key = f"rate:{user_id}:minute:{int(datetime.now(UTC).timestamp() // 60)}"
    day_key = f"rate:{user_id}:day:{datetime.now(UTC):%Y%m%d}"
    async with redis.pipeline(transaction=True) as pipeline:
        pipeline.incr(minute_key)
        pipeline.expire(minute_key, 120)
        pipeline.incr(day_key)
        pipeline.expire(day_key, 172800)
        minute_count, _, day_count, _ = await pipeline.execute()
    return minute_count <= runtime_settings.submissions_per_minute and day_count <= runtime_settings.submissions_per_day


async def receive_image(message: Message, state: FSMContext, bot: Bot) -> None:
    if await state.get_state() != SubmissionFlow.awaiting_screenshot:
        await message.answer("❌ Avval faol topshiriqlardan birini tanlang.", reply_markup=main_menu())
        return
    if message.from_user is None:
        return
    if not await enforce_rate_limit(message.from_user.id):
        await message.answer("Yuborish limiti oshdi. Birozdan keyin urinib ko‘ring.")
        return

    file_id = message.photo[-1].file_id if message.photo else message.document.file_id if message.document and (message.document.mime_type or "").startswith("image/") else None
    file_size = message.photo[-1].file_size if message.photo else message.document.file_size if message.document else None
    if file_id is None:
        await message.answer("Faqat photo yoki image document qabul qilinadi.")
        return
    if file_size is not None and file_size > settings.max_upload_bytes:
        await message.answer("Rasm hajmi limitdan katta.")
        return

    data = await state.get_data()
    task_id = int(data["task_id"])
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        task = await get_available_task(session, task_id, user.id if user else None)
        already_approved = await session.scalar(select(Submission.id).where(
            Submission.task_id == task_id,
            Submission.user_id == user.id if user else False,
            Submission.status == SubmissionStatus.APPROVED,
        )) if user else None
        if user is None or user.is_blocked or task is None or already_approved:
            await state.clear()
            await message.answer("Bu topshiriq mavjud emas yoki uni avval tasdiqlatgansiz.", reply_markup=main_menu())
            return

        telegram_file = await bot.get_file(file_id)
        buffer = io.BytesIO()
        await bot.download_file(telegram_file.file_path, buffer)
        raw = buffer.getvalue()
        if len(raw) > settings.max_upload_bytes:
            await message.answer("Rasm hajmi limitdan katta.")
            return
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
                suffix = ".jpg" if image.format == "JPEG" else ".png" if image.format == "PNG" else ".webp" if image.format == "WEBP" else None
        except (UnidentifiedImageError, OSError):
            suffix = None
        if suffix is None:
            await message.answer("Yaroqli PNG, JPEG yoki WebP rasm yuboring.")
            return

        digest = hashlib.sha256(raw).hexdigest()
        destination = Path(settings.upload_dir) / datetime.now(UTC).strftime("%Y/%m")
        destination.mkdir(parents=True, exist_ok=True)
        image_path = destination / f"{digest}{suffix}"
        image_path.write_bytes(raw)
        submission = Submission(
            task_id=task.id,
            user_id=user.id,
            image_path=str(image_path.resolve()),
            sha256=digest,
            status=SubmissionStatus.SUBMITTED,
        )
        session.add(submission)
        await session.commit()
        await session.refresh(submission)
        submission_id = submission.id
        task_title = task.title
        reward_points = task.reward_points
        username = user.username

    delivered_to_admins = await send_submission_review_to_admins(
        bot=bot,
        admin_ids=settings.admin_id_set,
        image=raw,
        filename=image_path.name,
        submission_id=submission_id,
        task_id=task_id,
        task_title=task_title,
        reward_points=reward_points,
        user_telegram_id=message.from_user.id,
        username=username,
    )
    try:
        assert queue is not None
        if settings.local_mode:
            assert isinstance(queue, asyncio.Queue)
            await queue.put(submission_id)
        else:
            assert isinstance(queue, ArqRedis)
            await queue.enqueue_job("verify_submission_job", submission_id)
    except Exception:
        logger.exception("Verification queue failed for submission %s; leaving it for admin review", submission_id)
        async with SessionLocal() as session:
            submission = await session.get(Submission, submission_id)
            if submission:
                submission.status = SubmissionStatus.MANUAL_REVIEW
                submission.reject_reason = "Verification queue vaqtincha ishlamayapti"
                await session.commit()
    await state.clear()
    if delivered_to_admins:
        await message.answer("✅ Screenshot adminlarga yuborildi. Javobni kuting.", reply_markup=main_menu())
    else:
        await message.answer(
            "⚠️ Screenshot saqlandi, lekin adminlarga xabar yetkazilmadi. "
            "Iltimos, admin bilan bog‘laning.",
            reply_markup=main_menu(),
        )


@router.message(SubmissionFlow.awaiting_screenshot, F.photo)
async def receive_photo(message: Message, state: FSMContext, bot: Bot) -> None:
    await receive_image(message, state, bot)


@router.message(SubmissionFlow.awaiting_screenshot, F.document)
async def receive_document(message: Message, state: FSMContext, bot: Bot) -> None:
    await receive_image(message, state, bot)


@router.message(F.photo | F.document)
async def image_without_task(message: Message) -> None:
    await message.answer("❌ Avval faol topshiriqlardan birini tanlang.", reply_markup=main_menu())


@router.message(F.text == "📤 Mening topshiriqlarim")
async def my_submissions(message: Message) -> None:
    assert message.from_user
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        submissions = list((await session.scalars(select(Submission).where(Submission.user_id == user.id).order_by(Submission.created_at.desc()).limit(15))).all()) if user else []
        task_ids = {item.task_id for item in submissions}
        tasks = {task.id: task.title for task in (await session.scalars(select(Task).where(Task.id.in_(task_ids)))).all()} if task_ids else {}
    if not submissions:
        await message.answer("Sizda topshirilgan topshiriqlar yo‘q.")
        return
    status_labels = {
        SubmissionStatus.APPROVED: "Tasdiqlandi",
        SubmissionStatus.REJECTED: "Rad etildi",
        SubmissionStatus.SUBMITTED: "Ko‘rib chiqilmoqda",
        SubmissionStatus.PROCESSING: "Ko‘rib chiqilmoqda",
        SubmissionStatus.MANUAL_REVIEW: "Ko‘rib chiqilmoqda",
    }
    lines = [
        f"#{item.task_id} {tasks.get(item.task_id, 'Topshiriq')} · "
        f"{status_labels[item.status]} · {item.created_at:%d-%m %H:%M}"
        + (f"\nSabab: {item.reject_reason}" if item.status == SubmissionStatus.REJECTED and item.reject_reason else "")
        for item in submissions
    ]
    await message.answer("📤 Mening topshiriqlarim\n\n" + "\n".join(lines))


@router.message(F.text == "⭐ Ballarim")
async def my_points(message: Message) -> None:
    assert message.from_user
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        history = list((await session.scalars(select(PointTransaction).where(PointTransaction.user_id == user.id).order_by(PointTransaction.created_at.desc()).limit(10))).all()) if user else []
    lines = [f"{'+' if item.amount > 0 else ''}{item.amount} ⭐ {item.description}" for item in history]
    await message.answer(f"⭐ Balansingiz: {user.balance if user else 0} ⭐\n\n" + ("\n".join(lines) if lines else "Hali ball tranzaksiyalari yo‘q."))


@router.message(F.text == "🏆 Reyting")
async def leaderboard(message: Message) -> None:
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User).where(User.is_blocked.is_(False)).order_by(User.balance.desc()).limit(10))).all())
    lines = [f"{index}. @{user.username}" if user.username else f"{index}. {user.first_name or user.telegram_id}" for index, user in enumerate(users, 1)]
    lines = [f"{line} — {users[index - 1].balance} ⭐" for index, line in enumerate(lines, 1)]
    await message.answer("🏆 Reyting\n\n" + "\n".join(lines))


@router.message(F.text == "📊 Statistikam")
async def my_stats(message: Message) -> None:
    assert message.from_user
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        submissions = list((await session.scalars(select(Submission).where(Submission.user_id == user.id))).all()) if user else []
    approved = sum(item.status == SubmissionStatus.APPROVED for item in submissions)
    rejected = sum(item.status == SubmissionStatus.REJECTED for item in submissions)
    pending = sum(item.status in (SubmissionStatus.SUBMITTED, SubmissionStatus.PROCESSING, SubmissionStatus.MANUAL_REVIEW) for item in submissions)
    await message.answer(f"📊 Jami: {len(submissions)}\n✅ Tasdiqlangan: {approved}\n❌ Rad etilgan: {rejected}\n⏳ Tekshiruvda: {pending}")


@router.message(F.text == "👤 Profil")
async def my_profile(message: Message) -> None:
    assert message.from_user
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == message.from_user.id))
        if user is None:
            await message.answer("Avval /start buyrug‘ini yuboring.")
            return
        submissions = list((await session.scalars(select(Submission).where(Submission.user_id == user.id))).all())
        rank = (await session.scalar(select(func.count(User.id)).where(User.balance > user.balance)) or 0) + 1
    approved = sum(item.status == SubmissionStatus.APPROVED for item in submissions)
    rejected = sum(item.status == SubmissionStatus.REJECTED for item in submissions)
    pending = len(submissions) - approved - rejected
    await message.answer(
        f"👤 {user.first_name} {user.last_name}\nUsername: @{user.username or '—'}\n"
        f"Telegram ID: {user.telegram_id}\n⭐ Ball: {user.balance}\n🏆 Reyting: #{rank}\n"
        f"📤 Jami task: {len(submissions)} · ✅ {approved} · ❌ {rejected} · ⏳ {pending}\n"
        f"📅 Ro‘yxatdan o‘tgan: {user.created_at:%d-%m-%Y}"
    )


@router.message(F.text == "📜 Qoidalar")
async def rules(message: Message) -> None:
    await message.answer("Faqat admin yaratgan va sizga berilgan faol topshiriqlarni bajaring. Ko‘rsatmalarga amal qiling va ish natijasini aniq, to‘liq screenshot bilan yuboring. Har bir topshiriq faqat bir marta mukofotlanadi.")


@router.message(F.text == "❓ Yordam")
async def help_message(message: Message) -> None:
    await message.answer("Topshiriqlar bo‘limidan task tanlang, bajarishni boshlang va ko‘rsatmalarga muvofiq screenshot yuboring. Avtomatik tekshiruv noaniq bo‘lsa, admin ko‘rib chiqadi.")


@router.message(Command("admin"))
async def admin_home(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    await state.clear()
    await message.answer("🔐 Admin boshqaruvi", reply_markup=admin_menu())


async def render_admin_users(message: Message, offset: int, edit: bool = False) -> None:
    page_size = 15
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(User.id))) or 0
        users = list((await session.scalars(
            select(User).order_by(User.created_at.desc()).offset(offset).limit(page_size)
        )).all())
    current_page = offset // page_size + 1
    page_count = max(1, (total + page_size - 1) // page_size)
    keyboard_rows = [[InlineKeyboardButton(
        text=f"{'⛔ ' if user.is_blocked else ''}{(user.first_name + ' ' + user.last_name).strip() or user.username or 'Ismsiz'} · {user.balance}⭐",
        callback_data=f"admin:user:{user.telegram_id}",
    )] for user in users]
    navigation = []
    if offset > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin:users:{max(0, offset-page_size)}"))
    if offset + page_size < total:
        navigation.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin:users:{offset+page_size}"))
    if navigation:
        keyboard_rows.append(navigation)
    keyboard_rows.append([InlineKeyboardButton(text="🏆 Reyting", callback_data="admin:leaderboard")])
    keyboard_rows.append([InlineKeyboardButton(text="🔐 Admin menyu", callback_data="admin:home")])
    text = f"👥 Foydalanuvchilar: {total}\nSahifa {current_page}/{page_count}\n\nProfil va amallarni ochish uchun foydalanuvchini tanlang."
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(Command("admin_users"))
async def admin_users_command(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    await render_admin_users(message, 0)


@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users_page(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    offset = max(0, int(callback.data.rsplit(":", 1)[1]))
    await render_admin_users(callback.message, offset, edit=True)
    await callback.answer()


async def load_admin_user_detail(telegram_id: int):
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            return None
        counts = dict((await session.execute(
            select(Submission.status, func.count(Submission.id))
            .where(Submission.user_id == user.id)
            .group_by(Submission.status)
        )).all())
        rank = (await session.scalar(select(func.count(User.id)).where(
            User.is_blocked.is_(False),
            User.balance > user.balance,
        )) or 0) + 1
        approved = counts.get(SubmissionStatus.APPROVED, 0)
        waiting = sum(counts.get(status, 0) for status in (
            SubmissionStatus.SUBMITTED,
            SubmissionStatus.PROCESSING,
            SubmissionStatus.MANUAL_REVIEW,
        ))
        name = f"{user.first_name} {user.last_name}".strip() or "Ismsiz"
        text = (
            f"👤 {name}\nUsername: @{user.username or 'yo‘q'}\nID: {user.telegram_id}\n"
            f"⭐ Ball: {user.balance} · 🏆 Reyting: #{rank}\n"
            f"📤 Topshiriqlar: {sum(counts.values())} · ✅ {approved} · ⏳ {waiting}\n"
            f"Holati: {'Bloklangan' if user.is_blocked else 'Faol'}"
        )
        blocked = user.is_blocked
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Shaxsiy topshiriq", callback_data=f"admin:user_task:{telegram_id}")],
        [InlineKeyboardButton(text="⭐ Ball qo‘shish / ayirish", callback_data=f"admin:user_points:{telegram_id}")],
        [InlineKeyboardButton(
            text="🔓 Blokdan chiqarish" if blocked else "🚫 Bloklash",
            callback_data=f"admin:user_block:{telegram_id}",
        )],
        [InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin:users:0")],
    ])
    return text, keyboard


@router.callback_query(F.data == "admin:leaderboard")
async def admin_leaderboard(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    async with SessionLocal() as session:
        users = list((await session.scalars(
            select(User).where(User.is_blocked.is_(False))
            .order_by(User.balance.desc(), User.created_at.asc()).limit(20)
        )).all())
    rows = [[InlineKeyboardButton(
        text=f"{index}. {(user.first_name + ' ' + user.last_name).strip() or user.username or user.telegram_id} · {user.balance}⭐",
        callback_data=f"admin:user:{user.telegram_id}",
    )] for index, user in enumerate(users, 1)]
    rows.append([InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin:users:0")])
    await callback.message.answer(
        "🏆 Reyting · eng ko‘p ball to‘plagan 20 foydalanuvchi",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:"))
async def admin_user_detail(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    details = await load_admin_user_detail(telegram_id)
    if details is None:
        await callback.answer("Foydalanuvchi topilmadi", show_alert=True)
        return
    text, keyboard = details
    await callback.message.answer(text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_block:"))
async def admin_toggle_user_block(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
        if user is None:
            await callback.answer("Foydalanuvchi topilmadi", show_alert=True)
            return
        user.is_blocked = not user.is_blocked
        blocked = user.is_blocked
        session.add(AdminAction(
            admin_telegram_id=callback.from_user.id,
            action="BLOCK_USER" if blocked else "UNBLOCK_USER",
            entity_type="user",
            entity_id=user.id,
            details=f"telegram_id={telegram_id}",
        ))
        await session.commit()
    details = await load_admin_user_detail(telegram_id)
    if details:
        await callback.message.edit_text(details[0], reply_markup=details[1])
    await callback.answer("Foydalanuvchi bloklandi" if blocked else "Blokdan chiqarildi")


@router.callback_query(F.data.startswith("admin:user_task:"))
async def admin_start_user_task(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(
            User.telegram_id == telegram_id,
            User.is_blocked.is_(False),
        ))
    if user is None:
        await callback.answer("Foydalanuvchi topilmadi yoki bloklangan", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminTaskFlow.task_text)
    await state.update_data(assigned_telegram_id=telegram_id)
    await callback.message.answer(
        f"{user.first_name or user.username or telegram_id} uchun topshiriq nomi va matnini yuboring.\n"
        "Birinchi qator — nomi, keyingi qatorlar — matni."
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user_points:"))
async def admin_start_user_points(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        await callback.answer("Foydalanuvchi topilmadi", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminPointsFlow.amount)
    await state.update_data(points_user_id=telegram_id)
    await callback.message.answer(
        f"{user.first_name or user.username or telegram_id} · balans {user.balance} ⭐\n"
        "Ball qo‘shish uchun musbat, ayirish uchun manfiy butun son yuboring (misol: 50 yoki -10)."
    )
    await callback.answer()


@router.message(AdminPointsFlow.amount)
async def admin_adjust_user_points(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        return
    try:
        amount = int((message.text or "").strip())
    except ValueError:
        await message.answer("Butun son yuboring. Masalan: 50 yoki -10.")
        return
    if amount == 0:
        await message.answer("Ball miqdori 0 bo‘lmasin.")
        return
    data = await state.get_data()
    telegram_id = int(data["points_user_id"])
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id).with_for_update())
        if user is None:
            await state.clear()
            await message.answer("Foydalanuvchi topilmadi.")
            return
        if user.balance + amount < 0:
            await message.answer(f"Balans {user.balance} ⭐. Bundan ko‘p ayirib bo‘lmaydi.")
            return
        user.balance += amount
        session.add(PointTransaction(
            user_id=user.id,
            amount=amount,
            type=TransactionType.ADMIN_ADJUSTMENT,
            description=f"Admin tomonidan ball {'qo‘shildi' if amount > 0 else 'ayirildi'}",
        ))
        session.add(AdminAction(
            admin_telegram_id=message.from_user.id,
            action="ADJUST_USER_POINTS",
            entity_type="user",
            entity_id=user.id,
            details=f"amount={amount}",
        ))
        balance = user.balance
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Ball yangilandi: {balance} ⭐")
    await send_user_message(
        telegram_id,
        f"⭐ Balansingiz o‘zgardi: {amount:+d} ball. Yangi balans: {balance} ⭐",
    )


async def render_admin_tasks(
    message: Message,
    state: FSMContext,
    offset: int = 0,
    edit: bool = False,
) -> None:
    page_size = 8
    previous = await state.get_data()
    for message_id in previous.get("admin_task_card_ids", []):
        try:
            await message.bot.delete_message(message.chat.id, message_id)
        except TelegramAPIError:
            logger.warning("Could not remove old task card message %s", message_id, exc_info=True)
    if not edit and previous.get("admin_task_list_message_id"):
        try:
            await message.bot.delete_message(
                message.chat.id,
                previous["admin_task_list_message_id"],
            )
        except TelegramAPIError:
            logger.warning("Could not remove old task list message", exc_info=True)

    async with SessionLocal() as session:
        total = await session.scalar(
            select(func.count(Task.id)).where(Task.deleted_at.is_(None))
        ) or 0
        if total and offset >= total:
            offset = max(0, ((total - 1) // page_size) * page_size)
        tasks = list((await session.scalars(
            select(Task).where(Task.deleted_at.is_(None))
            .order_by(Task.created_at.desc(), Task.id.desc())
            .offset(offset).limit(page_size)
        )).all())
    card_ids = []
    for task in tasks:
        text = (
            f"📋 {task.title}\n"
            f"Holati: {task.status.value} · Mukofot: {task.reward_points} ⭐\n"
            f"Tugash vaqti: {task.ends_at:%d-%m-%Y %H:%M UTC}\n\n"
            f"{task.advertisement_text[:3000]}"
        )
        card = await message.answer(
            text,
            reply_markup=admin_task_actions_keyboard(task.id),
        )
        card_ids.append(card.message_id)

    rows = []
    navigation = []
    if offset > 0:
        navigation.append(InlineKeyboardButton(
            text="⬅️ Oldingi",
            callback_data=f"admin:tasks:{max(0, offset-page_size)}",
        ))
    if offset + page_size < total:
        navigation.append(InlineKeyboardButton(
            text="Keyingi ➡️",
            callback_data=f"admin:tasks:{offset+page_size}",
        ))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Admin menyu", callback_data="admin:home")])
    page_count = max(1, (total + page_size - 1) // page_size)
    text = f"📋 Topshiriqlar · {total} ta · {offset // page_size + 1}/{page_count}"
    if not tasks:
        text = "Hozircha topshiriqlar yo‘q."
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        list_message = await message.answer(text, reply_markup=markup)
        message_id = list_message.message_id
    if edit:
        message_id = message.message_id
    await state.update_data(
        admin_task_card_ids=card_ids,
        admin_task_list_message_id=message_id,
        admin_task_offset=offset,
    )


def admin_task_actions_keyboard(task_id: int, confirm_delete: bool = False) -> InlineKeyboardMarkup:
    if confirm_delete:
        buttons = [
            InlineKeyboardButton(
                text="✅ Ha, o‘chirish",
                callback_data=f"admin:task_delete:{task_id}",
            ),
            InlineKeyboardButton(
                text="Bekor qilish",
                callback_data=f"admin:task_delete_cancel:{task_id}",
            ),
        ]
    else:
        buttons = [
            InlineKeyboardButton(
                text="✏️ Tahrirlash",
                callback_data=f"admin:task_edit:{task_id}",
            ),
            InlineKeyboardButton(
                text="🗑 O‘chirish",
                callback_data=f"admin:task_delete_prompt:{task_id}",
            ),
        ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


@router.message(Command("admin_tasks"))
async def admin_tasks_command(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    await render_admin_tasks(message, state)


@router.callback_query(F.data.startswith("admin:tasks:"))
async def admin_tasks_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    offset = max(0, int(callback.data.rsplit(":", 1)[1]))
    await render_admin_tasks(callback.message, state, offset, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:task_delete_prompt:"))
async def admin_task_delete_prompt(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    task_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None or task.deleted_at is not None:
            await callback.answer("Topshiriq topilmadi", show_alert=True)
            return
        title = task.title
    await callback.message.edit_reply_markup(
        reply_markup=admin_task_actions_keyboard(task_id, confirm_delete=True),
    )
    await callback.answer("O‘chirish uchun pastdagi tasdiqlash tugmasini bosing.")


@router.callback_query(F.data.startswith("admin:task_delete_cancel:"))
async def admin_task_delete_cancel(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    task_id = int(callback.data.rsplit(":", 1)[1])
    await callback.message.edit_reply_markup(
        reply_markup=admin_task_actions_keyboard(task_id),
    )
    await callback.answer("O‘chirish bekor qilindi.")


@router.callback_query(F.data.startswith("admin:task_delete:"))
async def admin_task_delete(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    task_id = int(callback.data.rsplit(":", 1)[1])
    async with SessionLocal() as session:
        task = await session.get(Task, task_id, with_for_update=True)
        if task is None or task.deleted_at is not None:
            await callback.answer("Topshiriq topilmadi", show_alert=True)
            return
        title = task.title
        task.deleted_at = datetime.now(UTC)
        task.status = TaskStatus.PAUSED
        session.add(AdminAction(
            admin_telegram_id=callback.from_user.id,
            action="DELETE_TASK",
            entity_type="task",
            entity_id=task.id,
            details=f"Soft-deleted task: {title}",
        ))
        await session.commit()

    page_data = await state.get_data()
    offset = int(page_data.get("admin_task_offset", 0))
    await render_admin_tasks(callback.message, state, offset)
    await callback.answer("Topshiriq o‘chirildi, hisobotlar tarixi saqlandi.")


@router.callback_query(F.data.startswith("admin:task_edit:"))
async def admin_task_edit_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    task_id = int(callback.data.rsplit(":", 1)[1])
    list_state = await state.get_data()
    for message_id in [
        *list_state.get("admin_task_card_ids", []),
        list_state.get("admin_task_list_message_id"),
    ]:
        if message_id is None or message_id == callback.message.message_id:
            continue
        try:
            await callback.bot.delete_message(callback.message.chat.id, message_id)
        except TelegramAPIError:
            logger.warning("Could not clear task list message %s before editing", message_id, exc_info=True)
    await callback.message.edit_reply_markup(reply_markup=None)
    async with SessionLocal() as session:
        task = await session.get(Task, task_id)
        if task is None:
            await callback.answer("Topshiriq topilmadi", show_alert=True)
            return
        current_text = f"{task.title}\n{task.advertisement_text}"
    await state.clear()
    await state.set_state(AdminTaskEditFlow.task_text)
    await state.update_data(task_id=task_id)
    await callback.message.answer(
        "Tahrirlash uchun topshiriq nomini birinchi qatorda, matnini keyingi qatorlarda yuboring.\n\n"
        f"Hozirgi matn:\n{current_text}"
    )
    await callback.answer()


@router.message(AdminTaskEditFlow.task_text)
async def admin_task_edit_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        return
    task_text = (message.text or "").strip()
    if not task_text or len(task_text) > 3500:
        await message.answer("Topshiriq matnini 1–3500 belgi oralig‘ida yuboring.")
        return
    title, body = parse_admin_task_text(task_text)
    if title.startswith("/") or not body:
        await message.answer("Nom slash-buyruq bo‘lmasin. Nom va topshiriq matnini qayta yuboring.")
        return
    data = await state.get_data()
    async with SessionLocal() as session:
        task = await session.get(Task, int(data["task_id"]), with_for_update=True)
        if task is None:
            await state.clear()
            await message.answer("Topshiriq topilmadi; ro‘yxat yangilandi.", reply_markup=admin_menu())
            return
        task.title = title
        task.advertisement_text = body
        session.add(AdminAction(
            admin_telegram_id=message.from_user.id if message.from_user else None,
            action="EDIT_TASK",
            entity_type="task",
            entity_id=task.id,
            details=title,
        ))
        await session.commit()
    await state.clear()
    await message.answer(f"✅ “{title}” topshirig‘i yangilandi.", reply_markup=admin_menu())


@router.message(Command("admin_newtask"))
async def admin_new_task_command(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    await state.clear()
    await state.set_state(AdminTaskFlow.task_text)
    await message.answer("Topshiriq matni va shartlarini bitta xabarda yuboring. Birinchi qator sarlavha bo‘ladi.")


@router.callback_query(F.data == "admin:home")
async def admin_home_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    await state.clear()
    await callback.message.answer("🔐 Admin boshqaruvi", reply_markup=admin_menu())
    await callback.answer()


@router.callback_query(F.data == "admin:new_task")
async def admin_new_task_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminTaskFlow.task_text)
    await callback.message.answer("Topshiriq matni va shartlarini bitta xabarda yuboring. Birinchi qator sarlavha bo‘ladi.")
    await callback.answer()


@router.message(AdminTaskFlow.task_text)
async def admin_task_text(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        return
    task_text = (message.text or "").strip()
    if not task_text:
        await message.answer("Topshiriq matnini oddiy xabar qilib yuboring.")
        return
    if len(task_text) > 3500:
        await message.answer("Topshiriq matni juda uzun. 3500 belgigacha qisqartiring.")
        return
    title, task_body = parse_admin_task_text(task_text)
    if title.startswith("/"):
        await message.answer("Topshiriq nomi slash buyruq bo‘lmasin. Nom va matnni qayta yuboring.")
        return
    ends_at = datetime.now(UTC) + timedelta(hours=settings.default_task_duration_hours)
    await state.update_data(
        title=title,
        advertisement_text=task_body,
        reward_points=settings.default_task_reward_points,
        ends_at=ends_at.isoformat(),
    )
    task_state = await state.get_data()
    if task_state.get("assigned_telegram_id") is not None:
        await save_admin_task(message, state, int(task_state["assigned_telegram_id"]))
        return
    await state.set_state(AdminTaskFlow.audience)
    await message.answer(
        f"✅ {title}\n⭐ {settings.default_task_reward_points} ball · "
        f"⏰ {settings.default_task_duration_hours} soat\n\nKimga yuboray?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Hammaga yuborish", callback_data="admin:audience:all")],
            [InlineKeyboardButton(text="Bir foydalanuvchiga", callback_data="admin:audience:user")],
        ]),
    )


async def save_admin_task(message: Message, state: FSMContext, assigned_telegram_id: int | None) -> None:
    data = await state.get_data()
    recipient_ids: list[int] = []
    async with SessionLocal() as session:
        assignee = None
        if assigned_telegram_id is not None:
            assignee = await session.scalar(select(User).where(User.telegram_id == assigned_telegram_id))
            if assignee is None:
                await message.answer("Bu Telegram ID bo‘yicha foydalanuvchi topilmadi. U avval botda /start bosishi kerak.")
                await state.set_state(AdminTaskFlow.assignee)
                return
        task = Task(
            title=data["title"],
            description="Admin orqali yaratilgan topshiriq",
            advertisement_text=data["advertisement_text"],
            reward_points=data["reward_points"],
            starts_at=datetime.now(UTC),
            ends_at=datetime.fromisoformat(data["ends_at"]),
            requirements="Topshiriq matnidagi ko‘rsatmalarni bajaring va natijani tasdiqlovchi aniq screenshot yuboring.",
            matching_mode=MatchingMode.EXACT_TEXT,
            similarity_threshold=0.9,
            status=TaskStatus.ACTIVE,
            is_public=assigned_telegram_id is None,
        )
        session.add(task)
        await session.flush()
        if assignee:
            session.add(TaskAssignment(task_id=task.id, user_id=assignee.id))
            recipient_ids = [assignee.telegram_id]
        else:
            recipient_ids = list((await session.scalars(
                select(User.telegram_id).where(User.is_blocked.is_(False))
            )).all())
        session.add(AdminAction(
            admin_telegram_id=message.from_user.id if message.from_user else None,
            action="CREATE_TASK",
            entity_type="task",
            entity_id=task.id,
            details=f"audience={'ALL' if assignee is None else assigned_telegram_id}",
        ))
        task_id = task.id
        await session.commit()
    await state.clear()
    audience = "barcha foydalanuvchilarga" if assigned_telegram_id is None else f"Telegram ID {assigned_telegram_id} ga"
    announcement = (
        f"{'📢 Yangi topshiriq' if assigned_telegram_id is None else '📩 Sizga shaxsiy topshiriq berildi'} "
        f"#{task_id}: {data['title']}\n\n"
        f"📝 {data['advertisement_text']}\n\n"
        f"⭐ Mukofot: {data['reward_points']} ball\n"
        f"⏰ Tugash vaqti: {datetime.fromisoformat(data['ends_at']):%d-%m-%Y %H:%M UTC}\n\n"
        + (
            "Batafsil ma’lumotni ko‘rish va bajarishni boshlash uchun tugmani bosing."
            if assigned_telegram_id is None
            else "Topshiriq “📋 Topshiriqlar” bo‘limingizda ham mavjud. Bajarib bo‘lgach screenshot yuboring."
        )
    )
    sent_count = await send_task_announcement(
        is_public=assigned_telegram_id is None,
        telegram_ids=recipient_ids,
        message=announcement,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 Topshiriqni ochish", callback_data=f"task:{task_id}")
        ]]),
    )
    if assigned_telegram_id is not None and sent_count == 0:
        delivery_status = (
            f"⚠️ #{task_id} shaxsiy topshiriq yaratildi, lekin Telegram xabarni "
            f"ID {assigned_telegram_id} ga yetkazmadi. Topshiriq uning botidagi "
            "“📋 Topshiriqlar” bo‘limida ko‘rinadi; yuborish sababini server logidan tekshiring."
        )
    elif sent_count < len(set(recipient_ids)):
        delivery_status = (
            f"⚠️ #{task_id} topshiriq yaratildi. Xabar {sent_count}/{len(set(recipient_ids))} "
            "foydalanuvchiga yuborildi; yuborilmaganlar tafsiloti server logida."
        )
    else:
        delivery_status = (
            f"✅ #{task_id} topshiriq {audience} berildi. "
            f"Xabar {sent_count} foydalanuvchiga yuborildi."
        )
    await message.answer(delivery_status, reply_markup=admin_menu())


async def show_assignable_users(message: Message, offset: int = 0, edit: bool = False) -> None:
    page_size = 10
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count(User.id)).where(User.is_blocked.is_(False))) or 0
        users = list((await session.scalars(
            select(User).where(User.is_blocked.is_(False))
            .order_by(User.username.is_(None), User.username, User.first_name, User.telegram_id)
            .offset(offset).limit(page_size)
        )).all())
    rows = [[InlineKeyboardButton(
        text=f"{('@' + user.username) if user.username else (user.first_name or 'Ismsiz')}",
        callback_data=f"admin:assign_user:{user.telegram_id}",
    )] for user in users]
    navigation = []
    if offset > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin:assignees:{max(0, offset-page_size)}"))
    if offset + page_size < total:
        navigation.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin:assignees:{offset+page_size}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="Bekor qilish", callback_data="admin:task_cancel")])
    text = f"Topshiriq beriladigan foydalanuvchini tanlang · jami {total}:"
    if not users:
        text = "Topshiriq berish uchun ro‘yxatdan o‘tgan a’zo yo‘q. Ular botda /start bosishi kerak."
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("admin:audience:"))
async def admin_task_audience(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    if await state.get_state() != AdminTaskFlow.audience.state:
        await callback.answer("Avval yangi topshiriq yaratishni boshlang.", show_alert=True)
        return
    audience = callback.data.rsplit(":", 1)[1]
    if audience == "all":
        await save_admin_task(callback.message, state, None)
    elif audience == "user":
        await state.set_state(AdminTaskFlow.assignee)
        await show_assignable_users(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:assignees:"))
async def admin_assignable_users_page(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    if await state.get_state() != AdminTaskFlow.assignee.state:
        await callback.answer("Avval task yaratish oynasini oching.", show_alert=True)
        return
    offset = max(0, int(callback.data.rsplit(":", 1)[1]))
    await show_assignable_users(callback.message, offset, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:assign_user:"))
async def admin_assign_task_to_user(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    if await state.get_state() != AdminTaskFlow.assignee.state:
        await callback.answer("Avval task yaratish oynasini oching.", show_alert=True)
        return
    telegram_id = int(callback.data.rsplit(":", 1)[1])
    await save_admin_task(callback.message, state, telegram_id)
    await callback.answer()


@router.callback_query(F.data == "admin:task_cancel")
async def admin_cancel_task(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    await state.clear()
    await callback.message.edit_text("Topshiriq yaratish bekor qilindi.")
    await callback.answer()


@router.message(Command("admin_pending"))
async def admin_pending_command(message: Message) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await message.answer("Bu buyruq faqat administratorlar uchun.")
        return
    await send_pending_submissions(message)


@router.callback_query(F.data == "admin:pending")
async def admin_pending_callback(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    await send_pending_submissions(callback.message)
    await callback.answer()


async def send_pending_submissions(message: Message) -> None:
    async with SessionLocal() as session:
        submissions = list((await session.scalars(
            select(Submission).where(Submission.status == SubmissionStatus.MANUAL_REVIEW)
            .order_by(Submission.created_at.asc()).limit(20)
        )).all())
        entries = []
        for submission in submissions:
            user = await session.get(User, submission.user_id)
            task = await session.get(Task, submission.task_id)
            result = await session.scalar(select(VerificationResult).where(VerificationResult.submission_id == submission.id))
            ocr = await session.scalar(select(OCRResult).where(OCRResult.submission_id == submission.id))
            fraud = await session.scalar(select(FraudScore).where(FraudScore.submission_id == submission.id))
            entries.append((submission, user, task, result, ocr, fraud))
    if not entries:
        await message.answer("Tasdiq kutilayotgan topshiriqlar yo‘q.")
        return
    for submission, user, task, result, ocr, fraud in entries:
        caption = (
            f"🕵️ Submission #{submission.id} · Task #{submission.task_id}\n"
            f"User ID: {user.telegram_id if user else '—'} · @{user.username or 'username yo‘q' if user else '—'}\n"
            f"Mukofot: {task.reward_points if task else 0} ball\n"
            f"Matn mosligi: {result.text_similarity:.0%}" if result and result.text_similarity is not None else
            f"🕵️ Submission #{submission.id} · Task #{task.id if task else submission.task_id}\nUser ID: {user.telegram_id if user else '—'} · @ {user.username or 'username yo‘q' if user else '—'} · Mukofot: {task.reward_points if task else 0} ball"
        )
        caption += f"\nFraud: {fraud.score if fraud else result.fraud_score if result else '—'}"
        recognized_text = ocr.text if ocr else result.ocr_text if result else ""
        if recognized_text:
            caption += f"\nOCR: {recognized_text[:350]}"
        actions = submission_review_keyboard(submission.id)
        if Path(submission.image_path).is_file():
            await message.answer_photo(FSInputFile(submission.image_path), caption=caption[:1024], reply_markup=actions)
        else:
            await message.answer(caption[:4000], reply_markup=actions)


@router.callback_query(F.data.startswith("admin:approve:"))
async def admin_approve_submission(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    submission_id = int(callback.data.rsplit(":", 1)[1])
    try:
        async with SessionLocal() as session:
            item = await session.scalar(select(Submission).where(Submission.id == submission_id).with_for_update())
            if item is None:
                await callback.answer("Submission topilmadi", show_alert=True)
                return
            user = await session.get(User, item.user_id)
            task = await session.get(Task, item.task_id)
            changed = await award_submission(session, submission_id)
            session.add(AdminAction(
                admin_telegram_id=callback.from_user.id,
                action="APPROVE_SUBMISSION",
                entity_type="submission",
                entity_id=submission_id,
                details=f"admin_id={callback.from_user.id}",
            ))
            balance = user.balance if user else 0
            reward = task.reward_points if task else 0
            task_id = task.id if task else item.task_id
            telegram_id = user.telegram_id if user else None
            await session.commit()
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    if changed and telegram_id is not None:
        await send_user_message(telegram_id, f"✅ Topshirig'ingiz admin tomonidan tasdiqlandi!\n\n🎯 Task #{task_id}\n⭐ +{reward} ball\n💰 Balansingiz: {balance} ⭐")
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Tasdiqlandi" if changed else "Oldin tasdiqlangan")


@router.callback_query(F.data.startswith("admin:reject:"))
async def admin_reject_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Ruxsat yo‘q", show_alert=True)
        return
    submission_id = int(callback.data.rsplit(":", 1)[1])
    await state.set_state(AdminReviewFlow.rejection_reason)
    await state.update_data(submission_id=submission_id)
    await callback.message.answer(f"Submission #{submission_id} uchun rad etish sababini yozing.")
    await callback.answer()


@router.message(AdminReviewFlow.rejection_reason)
async def admin_reject_submission(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id if message.from_user else None):
        await state.clear()
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Sabab kamida 3 belgi bo‘lsin.")
        return
    data = await state.get_data()
    submission_id = int(data["submission_id"])
    async with SessionLocal() as session:
        item = await session.scalar(select(Submission).where(Submission.id == submission_id).with_for_update())
        if item is None or item.status == SubmissionStatus.APPROVED:
            await state.clear()
            await message.answer("Submission topilmadi yoki avval tasdiqlangan.")
            return
        item.status = SubmissionStatus.REJECTED
        item.reject_reason = reason
        item.reviewed_at = datetime.now(UTC)
        user = await session.get(User, item.user_id)
        task_id = item.task_id
        telegram_id = user.telegram_id if user else None
        session.add(AdminAction(
            admin_telegram_id=message.from_user.id,
            action="REJECT_SUBMISSION",
            entity_type="submission",
            entity_id=item.id,
            details=reason,
        ))
        await session.commit()
    await state.clear()
    if telegram_id is not None:
        await send_user_message(
            telegram_id,
            f"❌ Topshirig'ingiz admin tomonidan rad etildi.\n\n🎯 Task #{task_id}\nSabab: {reason}",
            InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔁 Qayta urinish", callback_data=f"task:{task_id}")
            ]]),
        )
    await message.answer(f"Submission #{submission_id} rad etildi.", reply_markup=admin_menu())


async def local_verification_worker(work_queue: asyncio.Queue[int]) -> None:
    from app.worker import verify_submission_job

    while True:
        submission_id = await work_queue.get()
        try:
            await verify_submission_job({}, submission_id)
        except Exception:
            logging.exception("Local verification failed for submission %s", submission_id)
        finally:
            work_queue.task_done()


async def main() -> None:
    global redis, queue
    token = settings.bot_token.get_secret_value()
    if not token:
        raise RuntimeError("BOT_TOKEN must be configured in .env")
    local_worker: asyncio.Task | None = None
    if settings.local_mode:
        queue = asyncio.Queue()
        storage = MemoryStorage()
        local_worker = asyncio.create_task(local_verification_worker(queue))
    else:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        queue = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        storage = RedisStorage.from_url(settings.redis_url)
    bot = Bot(token)
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    try:
        try:
            await register_admin_commands(bot)
        except Exception:
            logging.exception("Admin Telegram command menu could not be registered")
        if settings.local_mode:
            logging.warning("Running in local mode: SQLite/in-memory state; do not use for production")
        await dispatcher.start_polling(bot)
    finally:
        if local_worker is not None:
            local_worker.cancel()
            await asyncio.gather(local_worker, return_exceptions=True)
        elif isinstance(queue, ArqRedis):
            await queue.close()
        if redis is not None:
            await redis.aclose()
        await storage.close()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
