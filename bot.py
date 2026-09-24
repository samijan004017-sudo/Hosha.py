import os
import re
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command, CommandObject
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    BotCommand, ChatMemberUpdated
)
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from openai import AsyncOpenAI

from sqlalchemy import (
    String, BigInteger, Integer, Boolean, DateTime, Text, ForeignKey,
    select, func, delete
)
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = os.getenv("8458055842:AAEDoEfD9cBpxTZNDXp7sZlxpgIdFMCFyOU", "").strip()
OPENAI_API_KEY = os.getenv("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", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", os.getenv("7575502917", "")).split(",")
    if x.strip().isdigit()
}
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@NOORI_HOSHA_BOT")

TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-5.6-luna")
IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2")

FREE_MINUTES_PER_HOUR = 3
PAID_BLOCK_MINUTES = 5
PAID_BLOCK_POINTS = 3
HISTORY_TTL_HOURS = 48
MAX_HISTORY_MESSAGES = 20

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing.")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is missing.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("ai-telegram-bot")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ============================================================
# DATABASE
# ============================================================
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), default="")
    first_name: Mapped[str] = mapped_column(String(255), default="")
    language: Mapped[str] = mapped_column(String(10), default="fa")
    points: Mapped[int] = mapped_column(Integer, default=0)
    invited_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    invite_count: Mapped[int] = mapped_column(Integer, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str] = mapped_column(String(30), default="normal")
    free_hour_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    free_seconds_used: Mapped[int] = mapped_column(Integer, default=0)
    paid_seconds_used: Mapped[int] = mapped_column(Integer, default=0)
    active_usage_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Vote(Base):
    __tablename__ = "votes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    vote: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class ForceChannel(Base):
    __tablename__ = "force_channels"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    invite_link: Mapped[str] = mapped_column(String(1000), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

class Admin(Base):
    __tablename__ = "admins"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class GroupSetting(Base):
    __tablename__ = "group_settings"
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    require_mention: Mapped[bool] = mapped_column(Boolean, default=True)

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# In-memory only for pending menu states; durable data lives in DB.
pending_admin_action: dict[int, str] = {}

# ============================================================
# HELPERS
# ============================================================
def now():
    return datetime.now(timezone.utc)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        for aid in ADMIN_IDS:
            if not await is_admin(db, aid):
                db.add(Admin(user_id=aid))
        await db.commit()

async def is_admin(db: AsyncSession, user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    row = await db.get(Admin, user_id)
    return row is not None

async def get_user(db: AsyncSession, tg_user) -> User:
    u = await db.get(User, tg_user.id)
    if not u:
        u = User(
            id=tg_user.id,
            username=tg_user.username or "",
            first_name=tg_user.first_name or "",
        )
        db.add(u)
        await db.commit()
        await db.refresh(u)
    else:
        u.username = tg_user.username or ""
        u.first_name = tg_user.first_name or ""
        u.last_activity = now()
        await db.commit()
    return u

def t(lang: str, fa: str, en: str) -> str:
    return fa if lang != "en" else en

def main_menu(lang="fa"):
    if lang == "en":
        rows = [
            [("🤖 Chat AI", "chat"), ("🎨 Create Image", "image")],
            [("🖼️ Image Tools", "image_tools"), ("👤 My Info", "me")],
            [("🎁 Invite Friends", "invite"), ("💬 Support", "support")],
            [("🌐 فارسی", "lang_fa"), ("👥 Add to Group", "add_group")],
        ]
    else:
        rows = [
            [("🤖 چت با هوش مصنوعی", "chat"), ("🎨 ساخت عکس", "image")],
            [("🖼️ ابزار عکس", "image_tools"), ("👤 معلومات من", "me")],
            [("🎁 دعوت دوستان", "invite"), ("💬 پشتیبانی", "support")],
            [("🌐 English", "lang_en"), ("👥 افزودن به گروه", "add_group")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a, callback_data=b) for a, b in row]
        for row in rows
    ])

def back_button(lang="fa"):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Back" if lang == "en" else "🔙 برگشت", callback_data="home")]
    ])

def admin_menu():
    rows = [
        [("📊 آمار", "adm_stats"), ("📢 ارسال همگانی", "adm_broadcast")],
        [("👥 کاربران", "adm_users"), ("👨‍💼 ادمین‌ها", "adm_admins")],
        [("🔒 عضویت اجباری", "adm_force"), ("💬 پشتیبانی", "adm_support")],
        [("⭐ امتیازات", "adm_points"), ("👍👎 رأی‌ها", "adm_votes")],
        [("👥 گروه‌ها", "adm_groups"), ("🚫 بن کاربران", "adm_ban")],
        [("🤖 مدل AI", "adm_model"), ("🏠 منوی اصلی", "home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=a, callback_data=b) for a, b in row]
        for row in rows
    ])

async def send_home(message: Message, lang="fa"):
    await message.answer(
        t(lang, "🏠 منوی اصلی\nیک گزینه را انتخاب کنید:", "🏠 Main menu\nChoose an option:"),
        reply_markup=main_menu(lang)
    )

async def user_language(db, uid):
    u = await db.get(User, uid)
    return u.language if u else "fa"

async def force_channels(db):
    result = await db.execute(select(ForceChannel).where(ForceChannel.enabled == True))
    return result.scalars().all()

async def check_force_membership(uid: int) -> tuple[bool, list[ForceChannel]]:
    async with SessionLocal() as db:
        channels = await force_channels(db)
    missing = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch.chat_id, uid)
            if member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
                missing.append(ch)
        except Exception:
            missing.append(ch)
    return len(missing) == 0, missing

async def force_markup(missing):
    rows = []
    for ch in missing:
        if ch.invite_link:
            rows.append([InlineKeyboardButton(text=f"📢 {ch.title}", url=ch.invite_link)])
    rows.append([InlineKeyboardButton(text="✅ بررسی عضویت", callback_data="check_membership")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def require_access(message_or_call):
    uid = message_or_call.from_user.id
    ok, missing = await check_force_membership(uid)
    if ok:
        return True
    text = "🔒 برای استفاده از ربات، ابتدا در کانال‌های زیر عضو شوید و سپس «بررسی عضویت» را بزنید:"
    markup = await force_markup(missing)
    if isinstance(message_or_call, CallbackQuery):
        await message_or_call.message.answer(text, reply_markup=markup)
        await message_or_call.answer()
    else:
        await message_or_call.answer(text, reply_markup=markup)
    return False

async def add_conversation(db, uid, role, content):
    db.add(Conversation(user_id=uid, role=role, content=content))
    await db.commit()

async def get_history(db, uid):
    result = await db.execute(
        select(Conversation)
        .where(Conversation.user_id == uid)
        .order_by(Conversation.created_at.desc())
        .limit(MAX_HISTORY_MESSAGES)
    )
    rows = list(reversed(result.scalars().all()))
    return [{"role": r.role, "content": r.content} for r in rows]

# ============================================================
# USAGE / POINTS
# ============================================================
async def consume_ai_time(db: AsyncSession, u: User, requested_seconds: int = 60) -> tuple[bool, str]:
    """
    Time accounting:
    - Each rolling hour has 180 free seconds.
    - Once free time is exhausted, each 300 seconds of paid usage costs 3 points.
    - Usage is measured between AI requests in an active session. A request counts at least 1 second
      and at most requested_seconds (60 by default), avoiding huge charges from idle time.
    """
    current = now()

    if not u.free_hour_start or current - u.free_hour_start >= timedelta(hours=1):
        u.free_hour_start = current
        u.free_seconds_used = 0
        u.paid_seconds_used = 0
        u.active_usage_start = current

    if not u.active_usage_start:
        u.active_usage_start = current
        u.last_activity = current
        await db.commit()
        return True, ""

    elapsed = max(1, min(requested_seconds, int((current - u.active_usage_start).total_seconds())))
    free_left = max(0, FREE_MINUTES_PER_HOUR * 60 - u.free_seconds_used)

    free_part = min(elapsed, free_left)
    paid_part = elapsed - free_part

    # Calculate newly crossed 5-minute blocks.
    old_paid = u.paid_seconds_used
    new_paid = old_paid + paid_part
    old_blocks = old_paid // (PAID_BLOCK_MINUTES * 60)
    new_blocks = new_paid // (PAID_BLOCK_MINUTES * 60)
    blocks_to_pay = max(0, new_blocks - old_blocks)
    cost = blocks_to_pay * PAID_BLOCK_POINTS

    if cost > u.points:
        u.active_usage_start = current
        u.last_activity = current
        await db.commit()
        return False, f"امتیاز کافی ندارید. هر {PAID_BLOCK_MINUTES} دقیقه استفاده = {PAID_BLOCK_POINTS} ⭐."

    u.free_seconds_used += free_part
    u.paid_seconds_used += paid_part
    u.points -= cost
    u.active_usage_start = current
    u.last_activity = current
    await db.commit()
    return True, ""

# ============================================================
# START / COMMANDS
# ============================================================
@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    async with SessionLocal() as db:
        u = await get_user(db, message.from_user)
        ref = (command.args or "").strip()
        if ref.isdigit():
            inviter_id = int(ref)
            if inviter_id != u.id and u.invited_by is None:
                inviter = await db.get(User, inviter_id)
                if inviter:
                    u.invited_by = inviter_id
                    inviter.points += 1
                    inviter.invite_count += 1
                    await db.commit()

        ok, missing = await check_force_membership(u.id)
        if not ok:
            await message.answer(
                "🔒 لطفاً ابتدا عضو کانال‌های اجباری شوید:",
                reply_markup=await force_markup(missing)
            )
            return

        # First-start language selection.
        if not u.language or u.language == "fa":
            # We still show a clear first language chooser.
            await message.answer(
                "سلام 👋\nزبان خود را انتخاب کنید:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🇦🇫 دری", callback_data="lang_fa"),
                     InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")]
                ])
            )
            return

        await send_home(message, u.language)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
    await message.answer("👨‍💼 پنل مدیریت:", reply_markup=admin_menu())

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 ID: <code>{message.from_user.id}</code>", parse_mode="HTML")

@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    async with SessionLocal() as db:
        u = await get_user(db, message.from_user)
        u.mode = "normal"
        u.active_usage_start = None
        await db.commit()
    await message.answer("✅ حالت فعلی متوقف شد.")

# ============================================================
# CALLBACKS: LANGUAGE / HOME / USER
# ============================================================
@dp.callback_query(F.data.in_({"lang_fa", "lang_en"}))
async def set_language(call: CallbackQuery):
    lang = "fa" if call.data == "lang_fa" else "en"
    async with SessionLocal() as db:
        u = await get_user(db, call.from_user)
        u.language = lang
        u.mode = "normal"
        await db.commit()
    await call.message.edit_text(
        t(lang, "✅ زبان دری انتخاب شد.\n🏠 منوی اصلی:", "✅ English selected.\n🏠 Main menu:"),
        reply_markup=main_menu(lang)
    )
    await call.answer()

@dp.callback_query(F.data == "home")
async def cb_home(call: CallbackQuery):
    async with SessionLocal() as db:
        lang = await user_language(db, call.from_user.id)
    await call.message.edit_text("🏠 منوی اصلی:", reply_markup=main_menu(lang))
    await call.answer()

@dp.callback_query(F.data == "check_membership")
async def cb_check_membership(call: CallbackQuery):
    if await require_access(call):
        await call.message.answer("✅ عضویت شما تأیید شد.")
        async with SessionLocal() as db:
            lang = await user_language(db, call.from_user.id)
        await send_home(call.message, lang)
    await call.answer()

@dp.callback_query(F.data == "me")
async def cb_me(call: CallbackQuery):
    if not await require_access(call):
        return
    async with SessionLocal() as db:
        u = await get_user(db, call.from_user)
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={u.id}"
        lang = u.language
        text = (
            f"👤 معلومات شما\n\n"
            f"نام: {u.first_name}\n"
            f"آیدی: @{u.username or '-'}\n"
            f"شناسه عددی: <code>{u.id}</code>\n"
            f"⭐ امتیاز: {u.points}\n"
            f"👥 تعداد دعوت‌ها: {u.invite_count}\n\n"
            f"🔗 لینک دعوت:\n{link}"
        ) if lang == "fa" else (
            f"👤 Your information\n\n"
            f"Name: {u.first_name}\n"
            f"Username: @{u.username or '-'}\n"
            f"Numeric ID: <code>{u.id}</code>\n"
            f"⭐ Points: {u.points}\n"
            f"👥 Invites: {u.invite_count}\n\n"
            f"🔗 Referral link:\n{link}"
        )
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=back_button(lang))
    await call.answer()

@dp.callback_query(F.data == "invite")
async def cb_invite(call: CallbackQuery):
    if not await require_access(call):
        return
    async with SessionLocal() as db:
        u = await get_user(db, call.from_user)
        me = await bot.get_me()
        link = f"https://t.me/{me.username}?start={u.id}"
        lang = u.language
        text = (
            f"🎁 دعوت دوستان\n\n"
            f"هر کاربر جدیدی که با لینک اختصاصی شما وارد شود، 1 ⭐ برای شما می‌آورد.\n\n"
            f"🔗 لینک شما:\n{link}\n\n"
            f"⭐ امتیاز فعلی: {u.points}"
        )
    await call.message.edit_text(text, reply_markup=back_button(lang))
    await call.answer()

@dp.callback_query(F.data == "chat")
async def cb_chat(call: CallbackQuery):
    if not await require_access(call):
        return
    async with SessionLocal() as db:
        u = await get_user(db, call.from_user)
        u.mode = "chat"
        u.active_usage_start = now()
        await db.commit()
        lang = u.language
    await call.message.answer(
        t(lang, "🤖 سوال خود را بفرستید. برای خروج /stop را بزنید.",
           "🤖 Send your question. Use /stop to exit.")
    )
    await call.answer()

@dp.callback_query(F.data == "image")
async def cb_image(call: CallbackQuery):
    if not await require_access(call):
        return
    async with SessionLocal() as db:
        u = await get_user(db, call.from_user)
        u.mode = "image"
        u.active_usage_start = now()
        await db.commit()
        lang = u.language
    await call.message.answer(
        t(lang, "🎨 توضیحات تصویر را بنویسید.", "🎨 Describe the image you want.")
    )
    await call.answer()

@dp.callback_query(F.data == "image_tools")
async def cb_image_tools(call: CallbackQuery):
    async with SessionLocal() as db:
        lang = await user_language(db, call.from_user.id)
    await call.message.answer(
        t(lang, "🖼️ ابزار عکس در نسخه فعلی شامل ساخت تصویر با متن است.",
           "🖼️ Current image tools include text-to-image generation."),
        reply_markup=back_button(lang)
    )
    await call.answer()

@dp.callback_query(F.data == "support")
async def cb_support(call: CallbackQuery):
    if not await require_access(call):
        return
    async with SessionLocal() as db:
        u = await get_user(db, call.from_user)
        u.mode = "support"
        await db.commit()
        lang = u.language
    await call.message.answer(
        t(lang, "💬 پیام خود را بفرستید. ادمین از طریق Reply پاسخ می‌دهد.",
           "💬 Send your message. An admin will reply through Telegram Reply.")
    )
    await call.answer()

@dp.callback_query(F.data == "add_group")
async def cb_add_group(call: CallbackQuery):
    me = await bot.get_me()
    await call.message.answer(
        f"👥 ربات را به گروه اضافه کنید:\nhttps://t.me/{me.username}?startgroup=true"
    )
    await call.answer()

# ============================================================
# VOTES
# ============================================================
@dp.callback_query(F.data.in_({"like", "dislike"}))
async def vote(call: CallbackQuery):
    vote_value = "like" if call.data == "like" else "dislike"
    async with SessionLocal() as db:
        existing = await db.execute(
            select(Vote).where(
                Vote.user_id == call.from_user.id,
                Vote.message_id == call.message.message_id
            )
        )
        row = existing.scalar_one_or_none()
        if row:
            row.vote = vote_value
        else:
            db.add(Vote(user_id=call.from_user.id, message_id=call.message.message_id, vote=vote_value))
        await db.commit()
    await call.answer("👍 ثبت شد." if vote_value == "like" else "👎 ثبت شد.")

def vote_markup():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 پسندیدم", callback_data="like"),
         InlineKeyboardButton(text="👎 نپسندیدم", callback_data="dislike")]
    ])

# ============================================================
# TEXT / AI / SUPPORT
# ============================================================
async def notify_admins_support(message: Message):
    async with SessionLocal() as db:
        result = await db.execute(select(Admin))
        admins = result.scalars().all()
    header = (
        "📩 پیام جدید پشتیبانی\n"
        f"USER_ID: {message.from_user.id}\n"
        f"نام: {message.from_user.full_name}\n"
        f"آیدی: @{message.from_user.username or '-'}\n\n"
        f"متن سوال:\n{message.text or '[پیام غیرمتنی]'}"
    )
    for a in admins:
        try:
            await bot.send_message(a.user_id, header)
        except Exception:
            pass

@dp.message(F.text, F.chat.type == ChatType.PRIVATE)
async def on_text(message: Message):
    uid = message.from_user.id

    # Admin broadcast/reply handling.
    async with SessionLocal() as db:
        admin_user = await is_admin(db, message.from_user.id)

    if admin_user and pending_admin_action.get(message.from_user.id) == "broadcast":
        pending_admin_action.pop(message.from_user.id, None)
        async with SessionLocal() as db:
            result = await db.execute(select(User.id))
            ids = [x[0] for x in result.all()]
        sent, failed = 0, 0
        for target in ids:
            try:
                await bot.send_message(target, message.text)
                sent += 1
                await asyncio.sleep(0.03)
            except Exception:
                failed += 1
        await message.answer(f"📢 ارسال همگانی تمام شد.\\n✅ {sent}\\n❌ {failed}")
        return

    if admin_user and message.reply_to_message:
        text = message.reply_to_message.text or ""
        m = re.search(r"USER_ID:\s*(\d+)", text)
        if m:
            target = int(m.group(1))
            try:
                await bot.send_message(target, f"👨‍💼 پاسخ ادمین:\n\n{message.text}")
                await message.answer("✅ پاسخ برای کاربر ارسال شد.")
            except Exception as e:
                await message.answer(f"❌ ارسال نشد: {e}")
            return

    async with SessionLocal() as db:
        u = await get_user(db, message.from_user)
        if u.is_banned:
            return
        lang = u.language
        mode = u.mode

        if mode == "support":
            await notify_admins_support(message)
            await message.answer(t(lang, "✅ پیام شما برای ادمین ارسال شد.", "✅ Sent to support."))
            u.mode = "normal"
            await db.commit()
            return

        if mode == "image":
            allowed, reason = await consume_ai_time(db, u)
            if not allowed:
                await message.answer("❌ " + reason)
                return
            prompt = message.text.strip()
            try:
                result = await openai_client.images.generate(
                    model=IMAGE_MODEL,
                    prompt=prompt,
                    size="1024x1024"
                )
                item = result.data[0]
                url = getattr(item, "url", None)
                b64 = getattr(item, "b64_json", None)
                if url:
                    await message.answer_photo(url, caption="🎨 تصویر ساخته شد.")
                elif b64:
                    import base64
                    from io import BytesIO
                    bio = BytesIO(base64.b64decode(b64))
                    bio.name = "image.png"
                    await message.answer_photo(bio, caption="🎨 تصویر ساخته شد.")
                else:
                    await message.answer("❌ سرویس تصویر خروجی قابل ارسال برنگرداند.")
            except Exception:
                log.exception("image generation failed")
                await message.answer("❌ ساخت تصویر انجام نشد. مدل تصویر/API access را بررسی کنید.")
            u.mode = "normal"
            await db.commit()
            return

        if mode == "chat":
            allowed, reason = await consume_ai_time(db, u)
            if not allowed:
                await message.answer("❌ " + reason)
                return

            await add_conversation(db, uid, "user", message.text)
            history = await get_history(db, uid)
            try:
                response = await openai_client.responses.create(
                    model=TEXT_MODEL,
                    instructions=(
                        "You are a helpful Telegram AI assistant. "
                        "If the user writes Dari, answer in natural Dari. "
                        "If English, answer in English. Be concise but useful."
                    ),
                    input=history,
                )
                answer = response.output_text or "پاسخی دریافت نشد."
                await add_conversation(db, uid, "assistant", answer)
                await message.answer(answer, reply_markup=vote_markup())
                # Admin receives every user question.
                await _notify_question(uid, message, answer)
            except Exception:
                log.exception("AI response failed")
                await message.answer("❌ در ارتباط با هوش مصنوعی خطایی رخ داد.")
            return

    await send_home(message, lang)

async def _notify_question(uid, message, answer):
    async with SessionLocal() as db:
        result = await db.execute(select(Admin))
        admins = result.scalars().all()
    text = (
        "👤 کاربر\n"
        f"نام: {message.from_user.full_name}\n"
        f"آیدی: @{message.from_user.username or '-'}\n"
        f"ID: {uid}\n\n"
        f"📝 متن سوال:\n{message.text}\n\n"
        f"🤖 پاسخ AI:\n{answer}"
    )
    for a in admins:
        try:
            await bot.send_message(a.user_id, text)
        except Exception:
            pass

# ============================================================
# NON-TEXT SUPPORT
# ============================================================
@dp.message(F.photo | F.voice | F.document | F.video | F.audio | F.sticker)
async def on_media(message: Message):
    async with SessionLocal() as db:
        u = await get_user(db, message.from_user)
        if u.mode == "support":
            await notify_admins_support(message)
            await message.answer("✅ پیام شما برای ادمین ارسال شد.")
            u.mode = "normal"
            await db.commit()
        else:
            await message.answer("ℹ️ برای این نوع پیام، از بخش پشتیبانی استفاده کنید یا متن سوال را بفرستید.")

# ============================================================
# GROUP AI
# ============================================================
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def group_handler(message: Message):
    if not message.from_user:
        return
    me = await bot.get_me()
    async with SessionLocal() as db:
        setting = await db.get(GroupSetting, message.chat.id)
        if not setting or not setting.enabled:
            return
        if setting.require_mention:
            mentioned = f"@{me.username}".lower() in (message.text or "").lower()
            if not mentioned:
                return

    clean = re.sub(rf"@{re.escape(me.username)}", "", message.text or "", flags=re.I).strip()
    if not clean:
        await message.reply("🤖 سوال خود را بعد از منشن کردن ربات بنویسید.")
        return

    try:
        response = await openai_client.responses.create(
            model=TEXT_MODEL,
            instructions="Answer the group member's question clearly and briefly in the same language.",
            input=clean,
        )
        answer = response.output_text or "پاسخی دریافت نشد."
        await message.reply(answer)
    except Exception:
        log.exception("group AI failed")
        await message.reply("❌ خطا در پاسخ‌گویی AI.")

@dp.my_chat_member()
async def bot_added_to_group(event: ChatMemberUpdated):
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if event.new_chat_member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        async with SessionLocal() as db:
            setting = await db.get(GroupSetting, event.chat.id)
            if not setting:
                db.add(GroupSetting(chat_id=event.chat.id, enabled=False, require_mention=True))
                await db.commit()

# ============================================================
# ADMIN PANEL
# ============================================================
@dp.callback_query(F.data.startswith("adm_"))
async def admin_callbacks(call: CallbackQuery):
    async with SessionLocal() as db:
        if not await is_admin(db, call.from_user.id):
            await call.answer("دسترسی ندارید.", show_alert=True)
            return

        action = call.data

        if action == "adm_stats":
            users_count = await db.scalar(select(func.count(User.id)))
            conv_count = await db.scalar(select(func.count(Conversation.id)))
            likes = await db.scalar(select(func.count(Vote.id)).where(Vote.vote == "like"))
            dislikes = await db.scalar(select(func.count(Vote.id)).where(Vote.vote == "dislike"))
            await call.message.edit_text(
                f"📊 آمار ربات\n\n"
                f"👥 کاربران: {users_count}\n"
                f"💬 پیام‌های AI: {conv_count}\n"
                f"👍 پسندیده: {likes}\n"
                f"👎 نپسندیده: {dislikes}",
                reply_markup=admin_menu()
            )

        elif action == "adm_broadcast":
            pending_admin_action[call.from_user.id] = "broadcast"
            await call.message.answer("📢 پیام همگانی را بفرستید. برای لغو /cancel")

        elif action == "adm_users":
            users_count = await db.scalar(select(func.count(User.id)))
            await call.message.edit_text(
                f"👥 تعداد کاربران: {users_count}\n\n"
                "برای مدیریت پیشرفته کاربران می‌توانید از /admin استفاده کنید.",
                reply_markup=admin_menu()
            )

        elif action == "adm_admins":
            result = await db.execute(select(Admin))
            admins = result.scalars().all()
            ids = "\n".join(str(a.user_id) for a in admins) or "-"
            await call.message.edit_text(
                f"👨‍💼 ادمین‌ها:\n{ids}\n\n"
                "برای افزودن: /addadmin USER_ID\n"
                "برای حذف: /deladmin USER_ID",
                reply_markup=admin_menu()
            )

        elif action == "adm_force":
            result = await db.execute(select(ForceChannel))
            channels = result.scalars().all()
            txt = "🔒 عضویت اجباری\n\n"
            if channels:
                txt += "\n".join(f"{c.id}. {c.title} | {c.chat_id}" for c in channels)
            else:
                txt += "هیچ کانالی تنظیم نشده."
            txt += "\n\n/addchannel CHAT_ID | TITLE | INVITE_LINK\n/delchannel ID"
            await call.message.edit_text(txt, reply_markup=admin_menu())

        elif action == "adm_support":
            await call.message.edit_text(
                "💬 پشتیبانی فعال است.\nپیام‌های کاربران با USER_ID برای ادمین‌ها ارسال می‌شود؛ با Reply پاسخ دهید.",
                reply_markup=admin_menu()
            )

        elif action == "adm_points":
            await call.message.edit_text(
                f"⭐ سیستم امتیاز:\n"
                f"دعوت موفق = 1 امتیاز\n"
                f"{PAID_BLOCK_MINUTES} دقیقه استفاده = {PAID_BLOCK_POINTS} امتیاز\n"
                f"رایگان هر ساعت = {FREE_MINUTES_PER_HOUR} دقیقه",
                reply_markup=admin_menu()
            )

        elif action == "adm_votes":
            likes = await db.scalar(select(func.count(Vote.id)).where(Vote.vote == "like"))
            dislikes = await db.scalar(select(func.count(Vote.id)).where(Vote.vote == "dislike"))
            await call.message.edit_text(
                f"👍 {likes}\n👎 {dislikes}",
                reply_markup=admin_menu()
            )

        elif action == "adm_groups":
            result = await db.execute(select(GroupSetting))
            groups = result.scalars().all()
            txt = "👥 گروه‌ها\n\n"
            txt += "\n".join(f"{g.chat_id}: {'فعال' if g.enabled else 'خاموش'}" for g in groups) or "-"
            txt += "\n\n/enablegroup CHAT_ID\n/disablegroup CHAT_ID"
            await call.message.edit_text(txt, reply_markup=admin_menu())

        elif action == "adm_ban":
            await call.message.edit_text(
                "🚫 مدیریت بن\n/ban USER_ID\n/unban USER_ID",
                reply_markup=admin_menu()
            )

        elif action == "adm_model":
            await call.message.edit_text(
                f"🤖 مدل متن: {TEXT_MODEL}\n🎨 مدل تصویر: {IMAGE_MODEL}",
                reply_markup=admin_menu()
            )

    await call.answer()

# Admin commands
@dp.message(Command("addadmin"))
async def add_admin(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("استفاده: /addadmin USER_ID")
            return
        uid = int(parts[1])
        if not await is_admin(db, uid):
            db.add(Admin(user_id=uid))
            await db.commit()
        await message.answer("✅ ادمین اضافه شد.")

@dp.message(Command("deladmin"))
async def del_admin(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("استفاده: /deladmin USER_ID")
            return
        uid = int(parts[1])
        if uid in ADMIN_IDS:
            await message.answer("❌ ادمین اصلی ENV را نمی‌شود از اینجا حذف کرد.")
            return
        row = await db.get(Admin, uid)
        if row:
            await db.delete(row)
            await db.commit()
        await message.answer("✅ ادمین حذف شد.")

@dp.message(Command("ban"))
async def ban_user(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("استفاده: /ban USER_ID")
            return
        u = await db.get(User, int(parts[1]))
        if u:
            u.is_banned = True
            await db.commit()
            await message.answer("🚫 کاربر بن شد.")

@dp.message(Command("unban"))
async def unban_user(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("استفاده: /unban USER_ID")
            return
        u = await db.get(User, int(parts[1]))
        if u:
            u.is_banned = False
            await db.commit()
            await message.answer("✅ کاربر از بن خارج شد.")

@dp.message(Command("addchannel"))
async def add_channel(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        raw = message.text.partition(" ")[2]
        parts = [x.strip() for x in raw.split("|")]
        if len(parts) != 3 or not parts[0].lstrip("-").isdigit():
            await message.answer("استفاده:\n/addchannel CHAT_ID | TITLE | INVITE_LINK")
            return
        cid, title, link = int(parts[0]), parts[1], parts[2]
        existing = await db.execute(select(ForceChannel).where(ForceChannel.chat_id == cid))
        row = existing.scalar_one_or_none()
        if row:
            row.title, row.invite_link, row.enabled = title, link, True
        else:
            db.add(ForceChannel(chat_id=cid, title=title, invite_link=link, enabled=True))
        await db.commit()
        await message.answer("✅ کانال عضویت اجباری اضافه/فعال شد.")

@dp.message(Command("delchannel"))
async def del_channel(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("استفاده: /delchannel ID")
            return
        row = await db.get(ForceChannel, int(parts[1]))
        if row:
            await db.delete(row)
            await db.commit()
        await message.answer("✅ کانال حذف شد.")

@dp.message(Command("enablegroup"))
async def enable_group(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("استفاده: /enablegroup CHAT_ID")
            return
        cid = int(parts[1])
        row = await db.get(GroupSetting, cid)
        if not row:
            row = GroupSetting(chat_id=cid, enabled=True, require_mention=True)
            db.add(row)
        else:
            row.enabled = True
        await db.commit()
        await message.answer("✅ AI گروه فعال شد.")

@dp.message(Command("disablegroup"))
async def disable_group(message: Message):
    async with SessionLocal() as db:
        if not await is_admin(db, message.from_user.id):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
            await message.answer("استفاده: /disablegroup CHAT_ID")
            return
        row = await db.get(GroupSetting, int(parts[1]))
        if row:
            row.enabled = False
            await db.commit()
        await message.answer("✅ AI گروه خاموش شد.")

@dp.message(Command("cancel"))
async def cancel_admin_action(message: Message):
    pending_admin_action.pop(message.from_user.id, None)
    await message.answer("لغو شد.")

@dp.message(F.text)
async def admin_broadcast_handler(message: Message):
    # This handler runs after on_text in dispatcher; it only acts for pending broadcast.
    uid = message.from_user.id
    if pending_admin_action.get(uid) != "broadcast":
        return
    async with SessionLocal() as db:
        if not await is_admin(db, uid):
            return
        pending_admin_action.pop(uid, None)
        result = await db.execute(select(User.id))
        ids = [x[0] for x in result.all()]
    sent = 0
    failed = 0
    for target in ids:
        try:
            await bot.send_message(target, message.text)
            sent += 1
            await asyncio.sleep(0.03)
        except Exception:
            failed += 1
    await message.answer(f"📢 ارسال همگانی تمام شد.\n✅ {sent}\n❌ {failed}")

# ============================================================
# CLEANUP
# ============================================================
async def cleanup_loop():
    while True:
        try:
            cutoff = now() - timedelta(hours=HISTORY_TTL_HOURS)
            async with SessionLocal() as db:
                await db.execute(delete(Conversation).where(Conversation.created_at < cutoff))
                await db.commit()
            log.info("Old conversations cleaned.")
        except Exception:
            log.exception("cleanup failed")
        await asyncio.sleep(3600)

async def setup_commands():
    await bot.set_my_commands([
        BotCommand(command="start", description="شروع / Start"),
        BotCommand(command="admin", description="پنل مدیریت"),
        BotCommand(command="id", description="نمایش شناسه"),
        BotCommand(command="stop", description="خروج از حالت فعلی"),
    ])

async def main():
    await init_db()
    await setup_commands()
    asyncio.create_task(cleanup_loop())
    log.info("Bot started.")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
