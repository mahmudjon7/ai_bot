"""Guruh qo'riqchisi: yozish uchun avval N ta odam qo'shish shart.

Nima qiladi:
  * "X Y ni qo'shdi" / "Z chiqib ketdi" xizmat xabarlarini o'chiradi;
  * kim nechta odam qo'shganini sanaydi (faqat haqiqiy qo'shish, havola orqali kirganlar sanalmaydi);
  * guruh egasi/admini /limit N bilan belgilagan sondan kam odam qo'shgan a'zoning xabarini o'chiradi
    va "yana X ta odam qo'shing" deb ogohlantiradi;
  * kerakli songa yetganda "endi yozishingiz mumkin" deb xabar beradi;
  * qo'shilgan odam guruhdan chiqib ketsa, qo'shgan kishining hisobidan ayiriladi (aldab bo'lmaydi).
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    User,
)
from dotenv import load_dotenv

from db import DB

log = logging.getLogger("guruh-bot")
router = Router()

GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
in_group = F.chat.type.in_(GROUPS)

# media turlari: (kalit, tugma matni, sabab matni). TARTIBNI O'ZGARTIRMANG — bitmask shunga bog'liq.
MEDIA_TYPES = [
    ("photo", "🖼 Rasm", "rasm"),
    ("video", "🎬 Video", "video"),
    ("sticker", "🎭 Stiker", "stiker"),
    ("animation", "🎞 GIF", "GIF"),
    ("voice", "🎤 Ovozli xabar", "ovozli xabar"),
    ("video_note", "⏺ Dumaloq video", "dumaloq video"),
    ("audio", "🎵 Musiqa", "musiqa"),
    ("document", "📎 Fayl", "fayl"),
    ("contact", "👤 Kontakt", "kontakt"),
    ("location", "📍 Joylashuv", "joylashuv"),
    ("poll", "📊 So'rovnoma", "so'rovnoma"),
]
MEDIA_BIT = {k: 1 << i for i, (k, _, _) in enumerate(MEDIA_TYPES)}
MEDIA_REASON = {k: r for k, _, r in MEDIA_TYPES}
MEDIA_LABEL = {k: lbl for k, lbl, _ in MEDIA_TYPES}

SUPER_ADMINS: set[int] = set()   # .env: SUPER_ADMINS=123456,789012
BROADCAST_DELAY = 0.07           # guruhlar orasidagi pauza (soniya) — Telegram limitidan oshmaslik uchun

ANON_ADMIN_ID = 1087968824   # @GroupAnonymousBot
TELEGRAM_SERVICE_ID = 777000  # bog'langan kanaldan avtomatik postlar

ADMIN_CACHE_TTL = 60      # soniya
WARN_COOLDOWN = 20        # bir odamga ogohlantirish orasidagi minimal vaqt
NO_RIGHTS_COOLDOWN = 600  # "huquq bering" eslatmasi orasidagi minimal vaqt

_admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}
_last_warn: dict[tuple[int, int], float] = {}
_last_no_rights: dict[int, float] = {}
_flood: dict[tuple[int, int], deque] = {}
_tasks: set[asyncio.Task] = set()

FLOOD_WINDOW = 10  # soniya
APPEAL_COOLDOWN = 600  # bir odam qayta murojaat qilishi uchun minimal vaqt (soniya)


# ---------------------------------------------------------------- yordamchilar
def name_of(u: User) -> str:
    return u.full_name or (f"@{u.username}" if u.username else str(u.id))


def mention(u: User) -> str:
    return f'<a href="tg://user?id={u.id}">{html.escape(name_of(u))}</a>'


def plural_odam(n: int) -> str:
    return f"{n} ta odam"


async def delete_later(bot: Bot, chat_id: int, message_id: int, ttl: int) -> None:
    await asyncio.sleep(ttl)
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


async def send_temp(bot: Bot, chat_id: int, text: str, ttl: int = 15, **kw) -> Message | None:
    """Xabar yuboradi va ttl soniyadan keyin o'chiradi (guruh toza turishi uchun)."""
    try:
        msg = await bot.send_message(chat_id, text, **kw)
    except TelegramAPIError as e:
        log.warning("send_message xatosi chat=%s: %s", chat_id, e)
        return None
    t = asyncio.create_task(delete_later(bot, chat_id, msg.message_id, ttl))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return msg


async def safe_delete(m: Message) -> bool:
    try:
        await m.delete()
        return True
    except TelegramAPIError as e:
        log.info("o'chirib bo'lmadi chat=%s msg=%s: %s", m.chat.id, m.message_id, e)
        return False


async def is_admin(bot: Bot, chat_id: int, user_id: int, fresh: bool = False) -> bool:
    key = (chat_id, user_id)
    now = time.monotonic()
    if not fresh:
        hit = _admin_cache.get(key)
        if hit and now - hit[0] < ADMIN_CACHE_TTL:
            return hit[1]
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
        ok = cm.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except TelegramAPIError:
        ok = False
    if len(_admin_cache) > 5000:
        _admin_cache.clear()
    _admin_cache[key] = (now, ok)
    return ok


async def sender_is_admin(m: Message, bot: Bot, fresh: bool = False) -> bool:
    if m.sender_chat and m.sender_chat.id == m.chat.id:  # anonim admin
        return True
    if not m.from_user:
        return False
    if m.from_user.id == ANON_ADMIN_ID:
        return True
    return await is_admin(bot, m.chat.id, m.from_user.id, fresh=fresh)


async def bot_can_delete(bot: Bot, chat_id: int) -> bool:
    try:
        me = await bot.get_chat_member(chat_id, bot.id)
    except TelegramAPIError:
        return False
    return me.status == ChatMemberStatus.ADMINISTRATOR and bool(
        getattr(me, "can_delete_messages", False)
    )


async def warn_no_rights(bot: Bot, chat_id: int) -> None:
    now = time.monotonic()
    if now - _last_no_rights.get(chat_id, -NO_RIGHTS_COOLDOWN) < NO_RIGHTS_COOLDOWN:
        return
    _last_no_rights[chat_id] = now
    await send_temp(
        bot,
        chat_id,
        "⚠️ Men xabarlarni o'chira olmayapman. Meni <b>admin</b> qiling va "
        "<b>«Xabarlarni o'chirish»</b> huquqini bering.",
        ttl=60,
    )


# ------------------------------------------------------------------ shaxsiy chat
HELP_TEXT = (
    "👋 <b>Salom!</b> Men guruhni tartibda ushlab turaman va qo'riqlayman.\n\n"
    "<b>Nima qilaman:</b>\n"
    "• «kim kimni qo'shdi» xabarlarini o'chiraman;\n"
    "• a'zo yozishdan oldin guruh egasi belgilagan sondagi odamni qo'shishi shart;\n"
    "• havola/reklama, forward, flood va taqiqlangan so'zlarni o'chiraman;\n"
    "• qoidabuzarni ogohlantiraman, ko'p takrorlansa — vaqtincha mute qilaman.\n\n"
    "<b>Sozlash:</b>\n"
    "1. Meni guruhga qo'shing va <b>admin</b> qiling (<i>xabarlarni o'chirish</i> va "
    "<i>a'zolarni cheklash</i> huquqlari bilan).\n"
    "2. Guruhda: <code>/limit 5</code> — yozish uchun 5 ta odam qo'shish shart.\n"
    "3. <code>/sozlamalar</code> — hamma sozlamalarni ko'rish.\n\n"
    "<b>Buyruqlar (guruhda, admin uchun):</b>\n"
    "/limit <i>N</i> — kerakli odam soni (0 = o'chiq)\n"
    "/panel — tugmali boshqaruv paneli (limit, flood, taqiqlar, sozlamalar)\n"
    "/taqiq — taqiqlar paneli: rasm, video, stiker, GIF, ovozli xabar, fayl, havola...\n"
    "/link on|off — havolalarni taqiqlash\n"
    "/forward on|off — forward xabarlarni taqiqlash\n"
    "/flood <i>N</i> — 10 soniyada N tadan ko'p xabar = flood (0 = o'chiq)\n"
    "/soz_qosh <i>so'z, so'z</i> · /soz_ochir <i>so'z</i> · /sozlar — taqiqlangan so'zlar\n"
    "/jazo <i>N M</i> — N ta ogohlantirishdan keyin M daqiqa mute (masalan <code>/jazo 3 60</code>)\n"
    "/xizmat on|off · /bildirish on|off\n"
    "/ozod · /ozod_bekor — (reply) shartlardan ozod qilish\n\n"
    "<b>Hamma uchun:</b> /holat — men nechta odam qo'shdim (guruhda) · /qoshganlarim — hamma guruhlar bo'yicha "
    "(shaxsiy chatda) · /top — eng ko'p qo'shganlar · "
    "/murojaat <i>sabab</i> — cheklov xato bo'lsa, adminga yozish (cheklov xabaridagi 🆘 tugma ham shu)"
)


@router.message(Command("start", "help"), F.chat.type == ChatType.PRIVATE)
async def cmd_start(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    arg = (command.args or "").strip()
    if command.command == "start" and arg.startswith("g") and arg[1:].lstrip("-").isdigit():
        await private_progress(m, bot, db, int(arg[1:]))
        return
    await m.answer(HELP_TEXT, reply_markup=main_kb(m.from_user.id))


# --------------------------------------------------------- bot guruhga qo'shilganda
@router.my_chat_member(in_group)
async def on_my_status(ev: ChatMemberUpdated, bot: Bot, db: DB) -> None:
    old, new = ev.old_chat_member.status, ev.new_chat_member.status
    await db.get_chat(ev.chat.id, ev.chat.title)
    if new in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        await db.set_active(ev.chat.id, False)  # bot guruhdan chiqarildi
        return
    await db.set_active(ev.chat.id, True)
    await db.set_title(ev.chat.id, ev.chat.title)
    if new in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR) and old in (
        ChatMemberStatus.LEFT,
        ChatMemberStatus.KICKED,
    ):
        await bot.send_message(
            ev.chat.id,
            "👋 Salom! Meni <b>admin</b> qiling (<i>xabarlarni o'chirish</i> huquqi bilan), "
            "so'ng yozing: <code>/limit 5</code> — a'zolar yozishdan oldin 5 ta odam qo'shishi shart bo'ladi.",
        )
    elif new == ChatMemberStatus.ADMINISTRATOR and old == ChatMemberStatus.MEMBER:
        can = getattr(ev.new_chat_member, "can_delete_messages", False)
        text = (
            "✅ Rahmat, admin bo'ldim. Endi <code>/limit 5</code> deb sozlang."
            if can
            else "⚠️ Admin bo'ldim, lekin <b>«Xabarlarni o'chirish»</b> huquqi yo'q — usiz ishlay olmayman."
        )
        await bot.send_message(ev.chat.id, text)


# ------------------------------------------------------------ xizmat xabarlari
@router.message(F.new_chat_members, in_group)
async def on_new_members(m: Message, bot: Bot, db: DB) -> None:
    s = await db.get_chat(m.chat.id, m.chat.title)
    adder = m.from_user
    members = m.new_chat_members or []

    if adder and adder.id != ANON_ADMIN_ID:
        await db.upsert_user(adder.id, name_of(adder))

    credited = 0
    # faqat haqiqiy "qo'shish" sanaladi: havola orqali kirgan odamda from_user == o'zi
    if adder and not adder.is_bot and adder.id not in (ANON_ADMIN_ID, TELEGRAM_SERVICE_ID):
        for u in members:
            if u.is_bot or u.id == adder.id:
                continue
            if await db.add_invite(m.chat.id, adder.id, u.id):
                credited += 1

    if credited and adder:
        cnt = await db.count(m.chat.id, adder.id)
        if s.required > 0:
            before = cnt - credited
            if before < s.required <= cnt:
                await send_temp(
                    bot,
                    m.chat.id,
                    f"🎉 {mention(adder)}, tabriklaymiz! Siz {plural_odam(cnt)} qo'shdingiz — "
                    f"endi guruhda <b>yozishingiz mumkin</b>.",
                    ttl=45,
                    reply_markup=await see_kb(bot, m.chat.id),
                )
            elif cnt < s.required and s.notify:
                await send_temp(
                    bot,
                    m.chat.id,
                    f"➕ {mention(adder)}: <b>{cnt}/{s.required}</b> — yozish uchun yana "
                    f"{plural_odam(s.required - cnt)} qo'shing.\n{progress_bar(cnt, s.required)}",
                    ttl=30,
                    reply_markup=await see_kb(bot, m.chat.id),
                )

    if s.delete_service:
        await safe_delete(m)


@router.message(F.left_chat_member, in_group)
async def on_left_member(m: Message, bot: Bot, db: DB) -> None:
    s = await db.get_chat(m.chat.id, m.chat.title)
    u = m.left_chat_member
    if u:
        await db.remove_invitee(m.chat.id, u.id)
    if s.delete_service:
        await safe_delete(m)


# ----------------------------------------------------------------- buyruqlar
async def _admin_only(m: Message, bot: Bot) -> bool:
    if await sender_is_admin(m, bot, fresh=True):
        return True
    await safe_delete(m)
    return False


@router.message(Command("limit"), in_group)
async def cmd_limit(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    s = await db.get_chat(m.chat.id, m.chat.title)
    arg = (command.args or "").strip()
    if not arg:
        cur = f"<b>{s.required}</b> ta odam" if s.required else "<b>o'chiq</b>"
        await send_temp(
            bot, m.chat.id,
            f"Hozirgi shart: {cur}.\nO'zgartirish: <code>/limit 5</code> (o'chirish: <code>/limit 0</code>)",
            ttl=30,
        )
        await safe_delete(m)
        return
    if not arg.isdigit() or not (0 <= int(arg) <= 100):
        await send_temp(bot, m.chat.id, "Raqam kiriting: 0 dan 100 gacha. Masalan: <code>/limit 5</code>", ttl=20)
        await safe_delete(m)
        return

    n = int(arg)
    await db.set_required(m.chat.id, n)
    if n == 0:
        text = "✅ Shart o'chirildi — hamma erkin yoza oladi."
    else:
        text = (
            f"✅ Endi guruhda yozish uchun <b>{plural_odam(n)}</b> qo'shish shart.\n"
            "Adminlar bu shartdan ozod."
        )
        if not await bot_can_delete(bot, m.chat.id):
            text += "\n\n⚠️ Diqqat: menda <b>«Xabarlarni o'chirish»</b> huquqi yo'q — avval meni shunday admin qiling."
    await send_temp(bot, m.chat.id, text, ttl=40)
    await safe_delete(m)


async def _toggle(m: Message, command: CommandObject, bot: Bot, db: DB, setter, label: str) -> None:
    if not await _admin_only(m, bot):
        return
    arg = (command.args or "").strip().lower()
    if arg not in {"on", "off"}:
        await send_temp(bot, m.chat.id, f"Foydalanish: <code>/{command.command} on</code> yoki <code>off</code>", ttl=20)
    else:
        await setter(m.chat.id, arg == "on")
        state = "yoqildi" if arg == "on" else "o'chirildi"
        await send_temp(bot, m.chat.id, f"✅ {label}: <b>{state}</b>", ttl=20)
    await safe_delete(m)


@router.message(Command("xizmat"), in_group)
async def cmd_service(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    await db.get_chat(m.chat.id, m.chat.title)
    await _toggle(m, command, bot, db, db.set_delete_service, "«Qo'shdi/chiqdi» xabarlarini o'chirish")


@router.message(Command("bildirish"), in_group)
async def cmd_notify(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    await db.get_chat(m.chat.id, m.chat.title)
    await _toggle(m, command, bot, db, db.set_notify, "Har qo'shishda qisqa xabar")


@router.message(Command("holat"), in_group)
async def cmd_status(m: Message, bot: Bot, db: DB) -> None:
    if not m.from_user or m.from_user.id == ANON_ADMIN_ID:
        await safe_delete(m)
        return
    s = await db.get_chat(m.chat.id, m.chat.title)
    cnt = await db.count(m.chat.id, m.from_user.id)
    if s.required <= 0:
        text = f"{mention(m.from_user)}, guruhda hozircha shart yo'q. Siz {plural_odam(cnt)} qo'shgansiz."
    elif cnt >= s.required or await db.is_exempt(m.chat.id, m.from_user.id) or await is_admin(
        bot, m.chat.id, m.from_user.id
    ):
        text = f"✅ {mention(m.from_user)}: <b>{cnt}/{s.required}</b> — siz yoza olasiz."
    else:
        text = (
            f"⛔ {mention(m.from_user)}: <b>{cnt}/{s.required}</b> — "
            f"yozish uchun yana {plural_odam(s.required - cnt)} qo'shing."
        )
    await send_temp(bot, m.chat.id, text, ttl=20)
    await safe_delete(m)


@router.message(Command("top"), in_group)
async def cmd_top(m: Message, bot: Bot, db: DB) -> None:
    rows = await db.top(m.chat.id, 10)
    if not rows:
        text = "Hali hech kim odam qo'shmagan."
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i] if i < 3 else f'{i + 1}.'} "
            f'<a href="tg://user?id={uid}">{html.escape(nm)}</a> — <b>{c}</b>'
            for i, (uid, nm, c) in enumerate(rows)
        ]
        text = "🏆 <b>Eng ko'p odam qo'shganlar:</b>\n" + "\n".join(lines)
    await send_temp(bot, m.chat.id, text, ttl=60)
    await safe_delete(m)


async def _exempt(m: Message, bot: Bot, db: DB, on: bool) -> None:
    if not await _admin_only(m, bot):
        return
    target = m.reply_to_message.from_user if m.reply_to_message else None
    if not target or target.is_bot:
        await send_temp(bot, m.chat.id, "Ozod qilinadigan odamning xabariga <b>javob (reply)</b> qilib yozing.", ttl=20)
    else:
        await db.upsert_user(target.id, name_of(target))
        await db.set_exempt(m.chat.id, target.id, on)
        verb = "shartdan ozod qilindi" if on else "uchun shart qayta kuchga kirdi"
        await send_temp(bot, m.chat.id, f"✅ {mention(target)} {verb}.", ttl=20)
    await safe_delete(m)


@router.message(Command("ozod"), in_group)
async def cmd_exempt(m: Message, bot: Bot, db: DB) -> None:
    await _exempt(m, bot, db, True)


@router.message(Command("ozod_bekor"), in_group)
async def cmd_unexempt(m: Message, bot: Bot, db: DB) -> None:
    await _exempt(m, bot, db, False)


# --------------------------------------------------------- qarovul sozlamalari
def _norm(t: str) -> str:
    return t.lower().replace("’", "'").replace("ʻ", "'").replace("‘", "'").replace("`", "'")


@router.message(Command("link"), in_group)
async def cmd_link(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    await db.get_chat(m.chat.id, m.chat.title)
    await _toggle(m, command, bot, db, lambda cid, on: db.set_value(cid, "block_links", int(on)),
                  "Havolalarni taqiqlash")


@router.message(Command("forward"), in_group)
async def cmd_forward(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    await db.get_chat(m.chat.id, m.chat.title)
    await _toggle(m, command, bot, db, lambda cid, on: db.set_value(cid, "block_forward", int(on)),
                  "Forward xabarlarni taqiqlash")


async def _int_args(m: Message, command: CommandObject, bot: Bot, count: int, usage: str) -> list[int] | None:
    parts = (command.args or "").split()
    if len(parts) != count or not all(x.isdigit() for x in parts):
        await send_temp(bot, m.chat.id, f"Foydalanish: <code>{usage}</code>", ttl=25)
        await safe_delete(m)
        return None
    return [int(x) for x in parts]


@router.message(Command("flood"), in_group)
async def cmd_flood(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    vals = await _int_args(m, command, bot, 1, "/flood 5   (0 = o'chiq)")
    if vals is None:
        return
    n = min(vals[0], 50)
    await db.set_value(m.chat.id, "flood_limit", n)
    text = (f"✅ Flood himoyasi: {FLOOD_WINDOW} soniyada <b>{n}</b> tadan ko'p xabar yozish taqiqlanadi."
            if n else "✅ Flood himoyasi o'chirildi.")
    await send_temp(bot, m.chat.id, text, ttl=30)
    await safe_delete(m)


@router.message(Command("jazo"), in_group)
async def cmd_penalty(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    vals = await _int_args(m, command, bot, 2, "/jazo 3 60   (3 ogohlantirish → 60 daqiqa mute)")
    if vals is None:
        return
    n, mins = max(1, min(vals[0], 10)), max(1, min(vals[1], 10080))
    await db.set_value(m.chat.id, "strike_limit", n)
    await db.set_value(m.chat.id, "mute_minutes", mins)
    await send_temp(bot, m.chat.id, f"✅ Jazo: <b>{n}</b> ta qoidabuzarlikdan keyin <b>{mins}</b> daqiqaga mute.", ttl=30)
    await safe_delete(m)


def _split_words(arg: str) -> list[str]:
    out = []
    for w in arg.replace("\n", ",").split(","):
        w = _norm(w.strip())
        if 2 <= len(w) <= 40:
            out.append(w)
    return out[:30]


@router.message(Command("soz_qosh"), in_group)
async def cmd_word_add(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    words = _split_words(command.args or "")
    if not words:
        await send_temp(bot, m.chat.id, "Foydalanish: <code>/soz_qosh so'z1, so'z2</code>", ttl=25)
    else:
        added = [w for w in words if await db.add_word(m.chat.id, w)]
        await send_temp(bot, m.chat.id, f"✅ Qo'shildi: <b>{len(added)}</b> ta so'z. Jami: {len(await db.words(m.chat.id))}.", ttl=25)
    await safe_delete(m)  # so'zning o'zi guruhda ko'rinib turmasin


@router.message(Command("soz_ochir"), in_group)
async def cmd_word_del(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    words = _split_words(command.args or "")
    if not words:
        await send_temp(bot, m.chat.id, "Foydalanish: <code>/soz_ochir so'z</code>", ttl=25)
    else:
        removed = [w for w in words if await db.remove_word(m.chat.id, w)]
        await send_temp(bot, m.chat.id, f"✅ O'chirildi: <b>{len(removed)}</b> ta so'z.", ttl=25)
    await safe_delete(m)


@router.message(Command("sozlar"), in_group)
async def cmd_words(m: Message, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    words = await db.words(m.chat.id)
    # ro'yxat guruhda ko'rinmasin deb faqat sonini aytamiz; admin lichkada ko'radi
    if m.from_user and words:
        try:
            await bot.send_message(m.from_user.id, "🤬 <b>Taqiqlangan so'zlar:</b>\n" + html.escape(", ".join(words)))
            note = "Ro'yxatni shaxsiy xabarda yubordim."
        except TelegramAPIError:
            note = "Ro'yxatni yuborish uchun avval menga shaxsiy chatda /start bosing."
    else:
        note = "Ro'yxat bo'sh."
    await send_temp(bot, m.chat.id, f"Taqiqlangan so'zlar: <b>{len(words)}</b> ta. {note}", ttl=25)
    await safe_delete(m)


async def settings_text(db: DB, chat_id: int, title: str | None = None) -> str:
    s = await db.get_chat(chat_id, title)
    yn = lambda v: "taqiqlangan" if v else "ruxsat"  # noqa: E731
    return (
        "⚙️ <b>Sozlamalar</b>\n"
        f"👥 Yozish sharti: {('<b>' + str(s.required) + '</b> ta odam') if s.required else 'o`chiq'}\n"
        f"🔗 Havolalar: <b>{yn(s.block_links)}</b>\n"
        f"↪️ Forward: <b>{yn(s.block_forward)}</b>\n"
        f"🖼 Taqiqlangan media: <b>{', '.join(MEDIA_REASON[k] for k in MEDIA_BIT if s.block_media & MEDIA_BIT[k]) or 'yo`q'}</b>\n"
        f"⚡ Flood: {('<b>' + str(s.flood_limit) + '</b> xabar / ' + str(FLOOD_WINDOW) + ' soniya') if s.flood_limit else 'o`chiq'}\n"
        f"🤬 Taqiqlangan so'zlar: <b>{len(await db.words(chat_id))}</b> ta\n"
        f"⚖️ Jazo: <b>{s.strike_limit}</b> ogohlantirish → <b>{s.mute_minutes}</b> daqiqa mute\n"
        f"🗑 Xizmat xabarlari: <b>{'o`chiriladi' if s.delete_service else 'qoladi'}</b>"
    ).replace("`", "'")


@router.message(Command("sozlamalar"), in_group)
async def cmd_settings(m: Message, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    await send_temp(bot, m.chat.id, await settings_text(db, m.chat.id, m.chat.title), ttl=60)
    await safe_delete(m)


# ------------------------------------------------------- /taqiq: media taqiqlari paneli
def taqiq_kb(chat_id: int, s) -> InlineKeyboardMarkup:
    items = [("link", "🔗 Havola", s.block_links), ("forward", "↪️ Forward", s.block_forward)]
    items += [(k, lbl, bool(s.block_media & MEDIA_BIT[k])) for k, lbl, _ in MEDIA_TYPES]
    rows, row = [], []
    for key, label, blocked in items:
        row.append(InlineKeyboardButton(
            text=f"{'🚫' if blocked else '✅'} {label}", callback_data=f"tq:{chat_id}:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✖️ Yopish", callback_data=f"tq:{chat_id}:x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


TAQIQ_TEXT = (
    "🛡 <b>Taqiqlar paneli</b>\n"
    "✅ — ruxsat, 🚫 — taqiq. Tugmani bosib almashtiring.\n"
    "Taqiqni buzgan xabar o'chiriladi va ogohlantirish yoziladi (adminlar bundan ozod)."
)


@router.message(Command("taqiq"), in_group)
async def cmd_taqiq(m: Message, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    s = await db.get_chat(m.chat.id, m.chat.title)
    await send_temp(bot, m.chat.id, TAQIQ_TEXT, ttl=300, reply_markup=taqiq_kb(m.chat.id, s))
    await safe_delete(m)


@router.callback_query(F.data.regexp(r"^tq:-?\d+:[a-z_]+$"))
async def cb_taqiq(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    _, chat_id, key = cb.data.split(":")
    chat_id = int(chat_id)
    if not await is_admin(bot, chat_id, cb.from_user.id, fresh=True):
        await cb.answer("Faqat guruh adminlari o'zgartira oladi.", show_alert=True)
        return
    msg = cb.message
    if key == "x":
        if msg:
            try:
                await bot.delete_message(msg.chat.id, msg.message_id)
            except TelegramAPIError:
                pass
        await cb.answer()
        return
    s = await db.get_chat(chat_id)
    if key == "link":
        await db.set_value(chat_id, "block_links", int(not s.block_links))
    elif key == "forward":
        await db.set_value(chat_id, "block_forward", int(not s.block_forward))
    elif key in MEDIA_BIT:
        await db.set_value(chat_id, "block_media", s.block_media ^ MEDIA_BIT[key])
    else:
        await cb.answer()
        return
    s = await db.get_chat(chat_id)
    if msg:
        try:
            await bot.edit_message_reply_markup(
                chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=taqiq_kb(chat_id, s))
        except TelegramAPIError:
            pass
    await cb.answer("Saqlandi")


# ------------------------------------------------- /panel: guruh admini uchun tugmali boshqaruv
FLOOD_STEPS = [0, 3, 5, 8]


def panel_kb(chat_id: int, s) -> InlineKeyboardMarkup:
    def btn(text: str, act: str, arg: int = 0) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=f"pn:{chat_id}:{act}:{arg}")

    cur = s.required
    nxt = FLOOD_STEPS[(FLOOD_STEPS.index(s.flood_limit) + 1) % len(FLOOD_STEPS)] if s.flood_limit in FLOOD_STEPS else 0
    mark = lambda v: "✅" if v else "❌"  # noqa: E731
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"👥 Yozish sharti: {cur} ta odam" if cur else "👥 Yozish sharti: o'chiq",
            callback_data="pn:noop")],
        [btn("➖", "lim", max(0, cur - 1)), btn("0", "lim", 0), btn("3", "lim", 3),
         btn("5", "lim", 5), btn("10", "lim", 10), btn("➕", "lim", min(100, cur + 1))],
        [btn(f"⚡ Flood: {s.flood_limit} xabar / {FLOOD_WINDOW}s" if s.flood_limit else "⚡ Flood: o'chiq",
             "flood", nxt)],
        [btn(f"🗑 Xizmat xabarlari {mark(s.delete_service)}", "svc"),
         btn(f"➕ Progress xabari {mark(s.notify)}", "ntf")],
        [btn("🛡 Taqiqlar", "tq"), btn("⚙️ Sozlamalar", "st")],
        [btn("✖️ Yopish", "x")],
    ])


PANEL_TEXT = (
    "🎛 <b>Boshqaruv paneli</b>\n"
    "Tugmalar bilan sozlang. Qo'shimcha: /jazo, /soz_qosh, /link, /forward, /ozod."
)


@router.message(Command("panel"), in_group)
async def cmd_panel(m: Message, bot: Bot, db: DB) -> None:
    if not await _admin_only(m, bot):
        return
    s = await db.get_chat(m.chat.id, m.chat.title)
    await send_temp(bot, m.chat.id, PANEL_TEXT, ttl=300, reply_markup=panel_kb(m.chat.id, s))
    await safe_delete(m)


@router.callback_query(F.data == "pn:noop")
async def cb_panel_noop(cb: CallbackQuery) -> None:
    await cb.answer()


@router.callback_query(F.data.regexp(r"^pn:-?\d+:[a-z]+:\d+$"))
async def cb_panel(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    _, chat_id, act, arg = cb.data.split(":")
    chat_id, arg = int(chat_id), int(arg)
    if not await is_admin(bot, chat_id, cb.from_user.id, fresh=True):
        await cb.answer("Faqat guruh adminlari o'zgartira oladi.", show_alert=True)
        return
    msg = cb.message
    s = await db.get_chat(chat_id)
    toast = "Saqlandi"
    if act == "lim":
        await db.set_value(chat_id, "required", max(0, min(100, arg)))
        if arg and not await bot_can_delete(bot, chat_id):
            toast = "⚠️ Menda «Xabarlarni o'chirish» huquqi yo'q — meni shunday admin qiling!"
    elif act == "flood":
        await db.set_value(chat_id, "flood_limit", max(0, min(50, arg)))
    elif act == "svc":
        await db.set_value(chat_id, "delete_service", int(not s.delete_service))
    elif act == "ntf":
        await db.set_value(chat_id, "notify", int(not s.notify))
    elif act == "tq":
        await send_temp(bot, chat_id, TAQIQ_TEXT, ttl=300, reply_markup=taqiq_kb(chat_id, s))
        await cb.answer()
        return
    elif act == "st":
        await send_temp(bot, chat_id, await settings_text(db, chat_id), ttl=60)
        await cb.answer()
        return
    elif act == "x":
        if msg:
            try:
                await bot.delete_message(msg.chat.id, msg.message_id)
            except TelegramAPIError:
                pass
        await cb.answer()
        return
    else:
        await cb.answer()
        return
    if msg:
        try:
            await bot.edit_message_reply_markup(
                chat_id=msg.chat.id, message_id=msg.message_id,
                reply_markup=panel_kb(chat_id, await db.get_chat(chat_id)))
        except TelegramAPIError:
            pass
    await cb.answer(toast, show_alert=toast.startswith("⚠️"))


# ---------------------------------------------- xizmat o'chirilgan guruhlar (super admin)
class ServiceSwitch(BaseMiddleware):
    """Super admin guruh xizmatini o'chirgan bo'lsa, bot u yerda hech narsa qilmaydi."""

    async def __call__(self, handler, event, data):
        db: DB = data["db"]
        if isinstance(event, Message):
            if event.chat.type in GROUPS and db.is_disabled(event.chat.id):
                if (event.text or "").startswith("/"):
                    key = (event.chat.id, 0)
                    now = time.monotonic()
                    if now - _last_warn.get(key, -60) >= 60:
                        _last_warn[key] = now
                        await send_temp(
                            data["bot"], event.chat.id,
                            "⏸ Bot xizmati bu guruhda to'xtatilgan. Qayta yoqish uchun bot egasiga murojaat qiling.",
                            ttl=15,
                        )
                return None
        elif isinstance(event, CallbackQuery) and event.data:
            parts = event.data.split(":")
            cid = None
            try:
                if parts[0] in ("tq", "ck", "pn") and len(parts) >= 3:
                    cid = int(parts[1])
                elif parts[0] == "ap" and parts[1] == "n":
                    cid = int(parts[2])
            except (ValueError, IndexError):
                cid = None
            if cid is not None and db.is_disabled(cid):
                await event.answer("⏸ Bot xizmati bu guruhda to'xtatilgan.", show_alert=True)
                return None
        return await handler(event, data)


router.message.outer_middleware(ServiceSwitch())
router.edited_message.outer_middleware(ServiceSwitch())
router.callback_query.outer_middleware(ServiceSwitch())


# ------------------------------------------------ «Qo'shganlarim»: hamma guruhlar bo'yicha hisob
BTN_MINE = "👥 Qo'shganlarim"


async def mine_view(db: DB, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    groups = await db.user_groups(uid)
    if not groups:
        text = ("👥 <b>Qo'shganlarim</b>\n\nSiz hali hech qaysi guruhda odam qo'shmagansiz.\n"
                "Guruhga o'ting va do'stlaringizni <i>«Odam qo'shish»</i> orqali qo'shing — hisob shu yerda ko'rinadi.")
    else:
        lines = []
        for g in groups:
            c, r = g["count"], g["required"]
            name = html.escape(_short(g["title"], 34))
            if r <= 0:
                lines.append(f"📍 <b>{name}</b>\n   {c} ta odam (guruhda shart yo'q)")
            elif c >= r:
                lines.append(f"📍 <b>{name}</b>\n   ✅ <b>{c}/{r}</b> — yozishingiz mumkin")
            else:
                lines.append(f"📍 <b>{name}</b>\n   ⛔ <b>{c}/{r}</b>  {progress_bar(c, r)}\n   ➕ yana {plural_odam(r - c)} qo'shing")
        total = sum(g["count"] for g in groups)
        text = "👥 <b>Siz qo'shgan odamlar</b>\n\n" + "\n\n".join(lines) + f"\n\nJami: <b>{total}</b> ta"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Yangilash", callback_data="my:0")]])
    return text, kb


@router.message(Command("qoshganlarim"), F.chat.type == ChatType.PRIVATE)
async def cmd_mine(m: Message, db: DB) -> None:
    text, kb = await mine_view(db, m.from_user.id)
    await m.answer(text, reply_markup=kb)


@router.message(F.chat.type == ChatType.PRIVATE, F.text == BTN_MINE)
async def btn_mine(m: Message, db: DB) -> None:
    _ad_wait.pop(m.from_user.id, None)
    text, kb = await mine_view(db, m.from_user.id)
    await m.answer(text, reply_markup=kb)


@router.callback_query(F.data == "my:0")
async def cb_mine(cb: CallbackQuery, db: DB) -> None:
    text, kb = await mine_view(db, cb.from_user.id)
    if cb.message:
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramAPIError:
            pass  # o'zgarish yo'q bo'lsa Telegram xato qaytaradi
    await cb.answer("Yangilandi")


# ------------------------------------------------- progress xabari va tugmalar
def progress_bar(cnt: int, req: int, width: int = 10) -> str:
    if req <= 0:
        return ""
    ratio = min(1.0, cnt / req)
    filled = int(ratio * width)
    return "▰" * filled + "▱" * (width - filled) + f" {int(ratio * 100)}%"


def need_text(who: str, cnt: int, req: int) -> str:
    left = max(0, req - cnt)
    return (
        f"⛔ {who}, guruhda yozish uchun <b>{plural_odam(req)}</b> qo'shishingiz kerak.\n\n"
        f"👥 Qo'shganingiz: <b>{cnt}/{req}</b>\n"
        f"{progress_bar(cnt, req)}\n"
        f"➕ Yana <b>{plural_odam(left)}</b> qo'shing."
    )


def done_text(who: str, cnt: int, req: int) -> str:
    if req <= 0:
        return f"✅ {who}, guruhda hozircha shart yo'q — <b>yozishingiz mumkin</b>."
    return f"✅ {who}, tayyor! <b>{cnt}/{req}</b> — endi guruhda <b>yozishingiz mumkin</b>."


async def see_kb(bot: Bot, chat_id: int) -> InlineKeyboardMarkup:
    """«Botga o'tib ko'rish» — botning shaxsiy chatida shu guruhdagi hisobni ochadi."""
    me = await bot.me()
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Botga o'tib ko'rish", url=f"https://t.me/{me.username}?start=g{chat_id}")
    ]])


async def need_kb(bot: Bot, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    me = await bot.me()
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Botga o'tish", url=f"https://t.me/{me.username}?start=g{chat_id}"),
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ck:{chat_id}:{user_id}"),
        ],
        [InlineKeyboardButton(text="🆘 Xato bo'lsa — adminga yozish", callback_data=f"ap:n:{chat_id}:{user_id}")],
    ])


async def has_pass(bot: Bot, db: DB, chat_id: int, user_id: int, cnt: int, req: int) -> bool:
    """Foydalanuvchi guruhda yoza oladimi (shart bajarilgan / ozod / admin)."""
    return (
        req <= 0
        or cnt >= req
        or await db.is_exempt(chat_id, user_id)
        or await is_admin(bot, chat_id, user_id, fresh=True)
    )


@router.callback_query(F.data.regexp(r"^ck:-?\d+:\d+$"))
async def cb_check(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    """«✅ Tasdiqlash» — hisobni qayta tekshiradi va xabarni yangilaydi."""
    _, chat_id, user_id = cb.data.split(":")
    chat_id, user_id = int(chat_id), int(user_id)
    if cb.from_user.id != user_id:
        await cb.answer("Bu tugma siz uchun emas.")
        return
    s = await db.get_chat(chat_id)
    cnt = await db.count(chat_id, user_id)
    ok = await has_pass(bot, db, chat_id, user_id, cnt, s.required)
    who = mention(cb.from_user)
    if ok:
        await cb.answer(f"✅ Tasdiqlandi! Endi guruhda yozishingiz mumkin. ({cnt}/{s.required})" if s.required
                        else "✅ Guruhda hozircha shart yo'q — yozishingiz mumkin.", show_alert=True)
        new_text, kb = done_text(who, cnt, s.required), None
    else:
        await cb.answer(f"⛔ {cnt}/{s.required} — yana {plural_odam(s.required - cnt)} qo'shing.", show_alert=True)
        new_text, kb = need_text(who, cnt, s.required), await need_kb(bot, chat_id, user_id)
    msg = cb.message
    if msg:
        if msg.chat.type == ChatType.PRIVATE:
            await _private_status_edit(bot, msg, chat_id, user_id, cnt, s.required, ok)
        else:
            try:
                await bot.edit_message_text(new_text, chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=kb)
            except TelegramAPIError:
                pass  # matn o'zgarmagan bo'lsa Telegram xato qaytaradi — zarar yo'q


async def _group_title(bot: Bot, chat_id: int) -> str:
    try:
        return (await bot.get_chat(chat_id)).title or "Guruh"
    except TelegramAPIError:
        return "Guruh"


def _private_text(title: str, cnt: int, req: int, ok: bool) -> str:
    head = f"📍 <b>{html.escape(title)}</b>\n\n"
    if req <= 0:
        return head + "Bu guruhda hozircha odam qo'shish sharti yo'q — yozishingiz mumkin."
    if ok:
        return head + f"✅ <b>{cnt}/{req}</b>\n{progress_bar(cnt, req)}\n\nEndi guruhda <b>yozishingiz mumkin</b>."
    return head + (
        f"👥 Qo'shganingiz: <b>{cnt}/{req}</b>\n{progress_bar(cnt, req)}\n"
        f"➕ Yana <b>{plural_odam(req - cnt)}</b> qo'shing.\n\n"
        "<b>Qanday qo'shiladi:</b> guruhni oching → guruh nomiga bosing → "
        "<i>«Odam qo'shish»</i> → do'stlaringizni tanlang.\n"
        "<i>Havola orqali kirganlar hisobga olinmaydi.</i>"
    )


def _private_kb(chat_id: int, user_id: int, ok: bool) -> InlineKeyboardMarkup:
    mine = [InlineKeyboardButton(text="👥 Hamma guruhlar bo'yicha", callback_data="my:0")]
    if ok:
        return InlineKeyboardMarkup(inline_keyboard=[mine])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"ck:{chat_id}:{user_id}")],
        mine,
        [InlineKeyboardButton(text="🆘 Adminga yozish", callback_data=f"ap:n:{chat_id}:{user_id}")],
    ])


async def _private_status_edit(bot: Bot, msg, chat_id: int, user_id: int, cnt: int, req: int, ok: bool) -> None:
    title = await _group_title(bot, chat_id)
    try:
        await bot.edit_message_text(
            _private_text(title, cnt, req, ok), chat_id=msg.chat.id, message_id=msg.message_id,
            reply_markup=_private_kb(chat_id, user_id, ok),
        )
    except TelegramAPIError:
        pass


async def private_progress(m: Message, bot: Bot, db: DB, chat_id: int) -> None:
    """Guruhdagi «🤖 Botga o'tish» tugmasidan kelgan odamga o'z holatini ko'rsatadi."""
    u = m.from_user
    # faqat o'sha guruh a'zosiga ko'rsatamiz (ixtiyoriy ID lar bazaga yozilib ketmasligi uchun)
    try:
        cm = await bot.get_chat_member(chat_id, u.id)
        if cm.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
            raise TelegramAPIError(method=None, message="a'zo emas")
    except TelegramAPIError:
        await m.answer("Siz bu guruhning a'zosi emassiz yoki bot u yerda yo'q.")
        return
    s = await db.get_chat(chat_id)
    cnt = await db.count(chat_id, u.id)
    ok = await has_pass(bot, db, chat_id, u.id, cnt, s.required)
    title = await _group_title(bot, chat_id)
    await m.answer(_private_text(title, cnt, s.required, ok), reply_markup=_private_kb(chat_id, u.id, ok))


# ----------------------------------------------------------------- murojaatlar
def appeal_kb(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    """Cheklangan a'zoga: «Xato bo'lsa, adminga yozing»."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆘 Xato bo'lsa — adminga yozish", callback_data=f"ap:n:{chat_id}:{user_id}")
    ]])


def admin_kb(appeal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Cheklovni olib tashlash", callback_data=f"ap:ok:{appeal_id}")],
        [InlineKeyboardButton(text="🔓 Doimiy ozod qilish", callback_data=f"ap:ex:{appeal_id}")],
        [InlineKeyboardButton(text="❌ Rad etish", callback_data=f"ap:no:{appeal_id}")],
    ])


def full_permissions() -> ChatPermissions:
    return ChatPermissions(**{
        name: True for name, f in ChatPermissions.model_fields.items() if f.annotation in (bool, bool | None)
    })


async def lift_restriction(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Mute ni olib tashlaydi: guruhning standart ruxsatlarini qaytaradi."""
    try:
        chat = await bot.get_chat(chat_id)
        perms = getattr(chat, "permissions", None) or full_permissions()
        await bot.restrict_chat_member(chat_id, user_id, permissions=perms)
        return True
    except TelegramAPIError as e:
        log.info("cheklovni olib bo'lmadi chat=%s user=%s: %s", chat_id, user_id, e)
        return False


async def create_appeal(bot: Bot, db: DB, chat_id: int, chat_title: str | None, user: User,
                        reason: str | None) -> str:
    """'ok' | 'pending' | 'cooldown'"""
    last = await db.last_appeal(chat_id, user.id)
    if last:
        _, status, created = last
        if status == "pending":
            return "pending"
        if time.time() - created < APPEAL_COOLDOWN:
            return "cooldown"

    await db.upsert_user(user.id, name_of(user))
    appeal_id = await db.create_appeal(chat_id, user.id, name_of(user), reason)
    s = await db.get_chat(chat_id)
    cnt = await db.count(chat_id, user.id)
    lines = [
        "🆘 <b>Murojaat</b>",
        f"👤 {mention(user)} (<code>{user.id}</code>)",
        f"👥 Qo'shgan odamlar: <b>{cnt}</b>" + (f" / {s.required}" if s.required else ""),
    ]
    if reason:
        lines.append(f"📝 Sabab: {html.escape(reason[:300])}")
    lines.append("\nCheklov xato bo'lgan bo'lsa, tugmalardan birini bosing (faqat adminlar).")
    body = "\n".join(lines)

    # 1) guruhning o'zida (hamma adminlar ko'radi) — asosiy, ishonchli yo'l
    try:
        gm = await bot.send_message(chat_id, body, reply_markup=admin_kb(appeal_id))
        await db.set_appeal_msg(appeal_id, gm.message_id)
    except TelegramAPIError as e:
        log.warning("murojaatni guruhga yuborib bo'lmadi: %s", e)

    # 2) adminlarga shaxsiy xabar (botni /start qilganlarga)
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramAPIError:
        admins = []
    for a in admins:
        if a.user.is_bot:
            continue
        try:
            await bot.send_message(
                a.user.id, f"<b>{html.escape(chat_title or 'Guruh')}</b>\n{body}", reply_markup=admin_kb(appeal_id)
            )
        except TelegramAPIError:
            pass  # admin botni hali /start qilmagan
    return "ok"


@router.callback_query(F.data.startswith("ap:n:"))
async def cb_appeal_new(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    try:
        _, _, chat_id, user_id = cb.data.split(":")
        chat_id, user_id = int(chat_id), int(user_id)
    except ValueError:
        await cb.answer()
        return
    if cb.from_user.id != user_id:
        await cb.answer("Bu tugma siz uchun emas.", show_alert=False)
        return
    title = cb.message.chat.title if cb.message and getattr(cb.message, "chat", None) else None
    res = await create_appeal(bot, db, chat_id, title, cb.from_user, None)
    text = {
        "ok": "✅ Murojaatingiz adminlarga yuborildi. Javobni kuting.",
        "pending": "⏳ Murojaatingiz allaqachon yuborilgan, adminlar ko'rib chiqadi.",
        "cooldown": "⏳ Yaqinda murojaat qilgansiz. Bir ozdan keyin qayta urinib ko'ring.",
    }[res]
    await cb.answer(text, show_alert=True)


@router.message(Command("murojaat"), in_group)
async def cmd_appeal(m: Message, command: CommandObject, bot: Bot, db: DB) -> None:
    u = m.from_user
    if not u or u.id == ANON_ADMIN_ID:
        await safe_delete(m)
        return
    res = await create_appeal(bot, db, m.chat.id, m.chat.title, u, (command.args or "").strip() or None)
    text = {
        "ok": f"✅ {mention(u)}, murojaatingiz adminlarga yuborildi.",
        "pending": f"⏳ {mention(u)}, murojaatingiz allaqachon yuborilgan.",
        "cooldown": f"⏳ {mention(u)}, yaqinda murojaat qilgansiz — bir ozdan keyin qayta yozing.",
    }[res]
    await send_temp(bot, m.chat.id, text, ttl=20)
    await safe_delete(m)


@router.callback_query(F.data.regexp(r"^ap:(ok|ex|no):\d+$"))
async def cb_appeal_resolve(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    _, action, aid = cb.data.split(":")
    ap = await db.get_appeal(int(aid))
    if not ap:
        await cb.answer("Murojaat topilmadi.", show_alert=True)
        return
    if not await is_admin(bot, ap["chat_id"], cb.from_user.id, fresh=True):
        await cb.answer("Faqat guruh adminlari hal qila oladi.", show_alert=True)
        return

    msg = cb.message
    if ap["status"] != "pending":
        await cb.answer("Bu murojaat allaqachon hal qilingan.", show_alert=True)
        if msg:
            try:
                await bot.edit_message_reply_markup(chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=None)
            except TelegramAPIError:
                pass
        return

    chat_id, uid = ap["chat_id"], ap["user_id"]
    admin_name = name_of(cb.from_user)  # verdict oddiy matn sifatida tahrirlanadi (HTML emas)
    user_link = f'<a href="tg://user?id={uid}">{html.escape(ap["user_name"])}</a>'

    if action == "no":
        await db.set_appeal_status(ap["id"], "rejected")
        verdict = f"❌ Rad etildi — {admin_name}"
        notice = f"❌ {user_link}, murojaatingiz rad etildi."
        toast = "Rad etildi."
    else:
        unmuted = await lift_restriction(bot, chat_id, uid)
        await db.reset_strikes(chat_id, uid)
        if action == "ex":
            await db.set_exempt(chat_id, uid, True)
            await db.set_appeal_status(ap["id"], "exempted")
            verdict = f"🔓 Doimiy ozod qilindi — {admin_name}"
            notice = f"🔓 {user_link}, admin sizni shartlardan ozod qildi. Yozishingiz mumkin."
        else:
            await db.set_appeal_status(ap["id"], "approved")
            verdict = f"✅ Cheklov olib tashlandi — {admin_name}"
            notice = f"✅ {user_link}, admin cheklovingizni olib tashladi."
        if not unmuted:
            verdict += "\n⚠️ Mute ni olib bo'lmadi (menda «A'zolarni cheklash» huquqi yo'q yoki mute yo'q edi)."
        toast = "Bajarildi."

    # shu xabarni yangilaymiz, guruhdagi murojaat xabarini esa birozdan keyin o'chiramiz
    if msg:
        try:
            await bot.edit_message_text(
                (msg.text or "🆘 Murojaat") + f"\n\n<b>{verdict}</b>",
                chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=None, parse_mode=None,
            )
        except TelegramAPIError:
            pass
    if ap["group_msg_id"]:
        t = asyncio.create_task(delete_later(bot, chat_id, ap["group_msg_id"], 30))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
    await send_temp(bot, chat_id, notice, ttl=40)
    await cb.answer(toast)


# ------------------------------------------------------------ asosiy "darvoza"
def has_link(m: Message) -> bool:
    ents = list(m.entities or []) + list(m.caption_entities or [])
    if any(e.type in ("url", "text_link") for e in ents):
        return True
    txt = (m.text or m.caption or "").lower()
    return "t.me/" in txt or "http://" in txt or "https://" in txt


def detect_violation(m: Message, s, words: list[str], edited: bool = False) -> str | None:
    """Qoidabuzarlik sababini qaytaradi (arzon tekshiruvlar, API chaqiruvsiz)."""
    u = m.from_user
    if s.flood_limit > 0 and not edited:
        now = time.monotonic()
        dq = _flood.setdefault((m.chat.id, u.id), deque())
        dq.append(now)
        while dq and now - dq[0] > FLOOD_WINDOW:
            dq.popleft()
        if len(_flood) > 5000:
            _flood.clear()
        if len(dq) > s.flood_limit:
            return "juda tez-tez yozmang (flood)"
    if s.block_media:
        ct = str(getattr(m.content_type, "value", m.content_type))
        if s.block_media & MEDIA_BIT.get(ct, 0):
            return f"{MEDIA_REASON[ct]} yuborish taqiqlangan"
    if s.block_forward and m.forward_origin is not None:
        return "forward xabar yuborish taqiqlangan"
    if s.block_links and has_link(m):
        return "havola yuborish taqiqlangan"
    if words:
        txt = _norm(m.text or m.caption or "")
        if txt and any(w in txt for w in words):
            return "taqiqlangan so'z ishlatildi"
    return None


async def punish(m: Message, bot: Bot, db: DB, s, reason: str) -> None:
    u = m.from_user
    if not await safe_delete(m):
        await warn_no_rights(bot, m.chat.id)
        return
    n = await db.add_strike(m.chat.id, u.id)
    limit = max(1, s.strike_limit)
    if n >= limit:
        await db.reset_strikes(m.chat.id, u.id)
        until = datetime.now(timezone.utc) + timedelta(minutes=s.mute_minutes)
        try:
            await bot.restrict_chat_member(
                m.chat.id, u.id, permissions=ChatPermissions(can_send_messages=False), until_date=until
            )
            text = f"🔇 {mention(u)} {s.mute_minutes} daqiqaga <b>mute</b> qilindi ({reason})."
        except TelegramAPIError as e:
            log.warning("mute xatosi chat=%s user=%s: %s", m.chat.id, u.id, e)
            text = (f"⚠️ {mention(u)}: {reason}. Mute qila olmadim — menga "
                    "<b>«A'zolarni cheklash»</b> huquqini bering.")
        await send_temp(bot, m.chat.id, text, ttl=600, reply_markup=appeal_kb(m.chat.id, u.id))
        return
    key = (m.chat.id, u.id)
    now = time.monotonic()
    if now - _last_warn.get(key, -WARN_COOLDOWN) < WARN_COOLDOWN:
        return
    if len(_last_warn) > 5000:
        _last_warn.clear()
    _last_warn[key] = now
    await send_temp(
        bot, m.chat.id,
        f"⚠️ {mention(u)}, {reason}.\nOgohlantirish: <b>{n}/{limit}</b> — {limit} ga yetsa "
        f"{s.mute_minutes} daqiqaga mute qilinadi.",
        ttl=25,
        reply_markup=appeal_kb(m.chat.id, u.id),
    )


async def _guard(m: Message, bot: Bot, db: DB, edited: bool = False) -> None:
    u = m.from_user
    if u is None or u.is_bot or u.id in (ANON_ADMIN_ID, TELEGRAM_SERVICE_ID):
        return
    if m.sender_chat or m.is_automatic_forward:  # kanal nomidan / anonim admin
        return

    s = await db.get_chat(m.chat.id, m.chat.title)
    words = await db.words(m.chat.id)
    guard_on = s.block_links or s.block_forward or s.block_media or s.flood_limit > 0 or bool(words)
    if s.required <= 0 and not guard_on:
        return

    # 1) arzon tekshiruv (API chaqirmasdan) — qoidabuzarlik bormi?
    need_invites = False
    cnt = 0
    reason: str | None = None
    if s.required > 0 and not edited:
        cnt = await db.count(m.chat.id, u.id)
        need_invites = cnt < s.required
    if not need_invites and guard_on:
        reason = detect_violation(m, s, words, edited=edited)
    if not need_invites and reason is None:
        return

    # 2) faqat qoidabuzarlik bo'lsa — ozod/adminligini tekshiramiz
    if await db.is_exempt(m.chat.id, u.id) or await is_admin(bot, m.chat.id, u.id):
        return
    await db.upsert_user(u.id, name_of(u))

    if need_invites:
        if not await safe_delete(m):
            await warn_no_rights(bot, m.chat.id)
            return
        key = (m.chat.id, u.id)
        now = time.monotonic()
        if now - _last_warn.get(key, -WARN_COOLDOWN) < WARN_COOLDOWN:
            return
        if len(_last_warn) > 5000:
            _last_warn.clear()
        _last_warn[key] = now
        await send_temp(
            bot, m.chat.id,
            need_text(mention(u), cnt, s.required),
            ttl=60,
            reply_markup=await need_kb(bot, m.chat.id, u.id),
        )
        return
    await punish(m, bot, db, s, reason)


@router.message(in_group)
async def gate(m: Message, bot: Bot, db: DB) -> None:
    await _guard(m, bot, db)


@router.edited_message(in_group)
async def gate_edited(m: Message, bot: Bot, db: DB) -> None:
    await _guard(m, bot, db, edited=True)


# ============================================================ SUPER ADMIN (bot egasi)
# Faqat shaxsiy chatda ishlaydi. ID lar .env dagi SUPER_ADMINS da.
SA_PAGE = 8
TASHKENT = timezone(timedelta(hours=5))
_ad_wait: dict[int, dict | None] = {}  # None = reklama xabari kutilmoqda; dict = yuborilishi tasdiqlanishi kutilmoqda


def is_super(uid: int) -> bool:
    return uid in SUPER_ADMINS


def sa_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Guruhlar", callback_data="sa:l:0"),
         InlineKeyboardButton(text="📈 Statistika", callback_data="sa:s")],
        [InlineKeyboardButton(text="📣 Reklama tarqatish", callback_data="sa:ad")],
    ])


def _short(t: str, n: int = 28) -> str:
    return t if len(t) <= n else t[: n - 1] + "…"


async def sa_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        pass  # matn o'zgarmagan bo'lsa Telegram xato qaytaradi


@router.message(Command("id"), F.chat.type == ChatType.PRIVATE)
async def cmd_id(m: Message) -> None:
    await m.answer(f"Sizning Telegram ID: <code>{m.from_user.id}</code>")


@router.message(Command("admin"), F.chat.type == ChatType.PRIVATE)
async def cmd_admin(m: Message) -> None:
    uid = m.from_user.id
    if not is_super(uid):
        extra = ("" if SUPER_ADMINS else
                 f"\n\nSuper admin hali belgilanmagan. Sizning ID: <code>{uid}</code> — uni .env dagi "
                 "<code>SUPER_ADMINS</code> ga yozing va botni qayta ishga tushiring.")
        await m.answer("⛔ Bu bo'lim faqat bot egasi uchun." + extra)
        return
    _ad_wait.pop(uid, None)
    await m.answer("🛠 <b>Super admin paneli</b>", reply_markup=sa_menu_kb())


@router.message(Command("bekor"), F.chat.type == ChatType.PRIVATE)
async def cmd_cancel(m: Message) -> None:
    if _ad_wait.pop(m.from_user.id, "x") != "x":
        await m.answer("Bekor qilindi.")


async def view_list(db: DB, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await db.count_chats()
    pages = max(1, -(-total // SA_PAGE))
    page = max(0, min(page, pages - 1))
    rows = await db.list_chats(page * SA_PAGE, SA_PAGE)
    kb = [[InlineKeyboardButton(
        text=f"{'🟢' if r['enabled'] else '⏸'} {_short(r['title'])}",
        callback_data=f"sa:g:{r['chat_id']}")] for r in rows]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"sa:l:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="sa:noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"sa:l:{page + 1}"))
    kb.append(nav)
    kb.append([InlineKeyboardButton(text="⬅️ Menyu", callback_data="sa:m")])
    text = (f"📊 <b>Bot ishlayotgan guruhlar:</b> {total} ta\n🟢 faol · ⏸ xizmat to'xtatilgan"
            if total else "Hozircha bot hech qaysi guruhda yo'q.")
    return text, InlineKeyboardMarkup(inline_keyboard=kb)


async def sa_list(cb: CallbackQuery, db: DB, page: int) -> None:
    text, kb = await view_list(db, page)
    await sa_edit(cb, text, kb)


async def view_stats(db: DB) -> tuple[str, InlineKeyboardMarkup]:
    st = await db.stats()
    text = (
        "📈 <b>Statistika</b>\n"
        f"Faol guruhlar: <b>{st['groups']}</b> (shundan xizmati to'xtatilgan: {st['disabled']})\n"
        f"Bot chiqarilgan guruhlar: {st['left']}\n"
        f"Hisoblangan qo'shishlar: <b>{st['invites']}</b>\n"
        f"Ochiq murojaatlar: {st['pending']}\n"
        f"Ismi saqlangan foydalanuvchilar: {st['users']}"
    )
    return text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Menyu", callback_data="sa:m")]])


async def sa_detail(cb: CallbackQuery, bot: Bot, db: DB, chat_id: int) -> None:
    info = await db.chat_info(chat_id)
    if not info:
        await cb.answer("Guruh topilmadi.", show_alert=True)
        return
    members = "—"
    if info["active"]:
        try:  # jonli ma'lumot: a'zolar soni va nom
            chat = await bot.get_chat(chat_id)
            members = str(await bot.get_chat_member_count(chat_id))
            if chat.title and chat.title != info["title"]:
                await db.set_title(chat_id, chat.title)
                info["title"] = chat.title
        except (TelegramForbiddenError, TelegramBadRequest):
            await db.set_active(chat_id, False)  # bot guruhdan chiqarilgan
            info["active"] = 0
        except TelegramAPIError:
            pass
    if not info["active"]:
        status = "🔴 bot guruhda yo'q"
    elif not info["enabled"]:
        status = "⏸ xizmat to'xtatilgan"
    else:
        status = "🟢 faol"
    blocked = [MEDIA_REASON[k] for k in MEDIA_BIT if info["block_media"] & MEDIA_BIT[k]]
    if info["block_links"]:
        blocked.append("havola")
    if info["block_forward"]:
        blocked.append("forward")
    joined = (datetime.fromtimestamp(info["joined_at"], TASHKENT).strftime("%d.%m.%Y %H:%M")
              if info["joined_at"] else "—")
    none = "yo'q"
    req_text = f"{info['required']} ta odam" if info["required"] else none
    text = (
        f"📍 <b>{html.escape(info['title'])}</b>\n"
        f"ID: <code>{chat_id}</code>\n"
        f"Holat: <b>{status}</b>\n"
        f"👥 A'zolar: <b>{members}</b>\n"
        f"➕ Yozish sharti: <b>{req_text}</b>\n"
        f"🛡 Taqiqlar: {html.escape(', '.join(blocked)) or none}\n"
        f"📈 Hisoblangan qo'shishlar: <b>{info['invites']}</b>\n"
        f"🗓 Qo'shilgan: {joined}"
    )
    rows = []
    if info["active"]:
        rows.append([InlineKeyboardButton(
            text="▶️ Xizmatni yoqish" if not info["enabled"] else "⏸ Xizmatni o'chirish",
            callback_data=f"sa:t:{chat_id}")])
        rows.append([InlineKeyboardButton(text="🚪 Guruhdan chiqish", callback_data=f"sa:q:{chat_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Ro'yxat", callback_data="sa:l:0")])
    await sa_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


async def broadcast(bot: Bot, db: DB, admin_chat: int, src_chat: int, src_msg: int, ids: list[int]) -> None:
    ok = fail = removed = 0
    for cid in ids:
        sent = False
        for attempt in (1, 2):
            try:
                await bot.copy_message(cid, src_chat, src_msg)
                sent = True
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                await db.set_active(cid, False)  # bot chiqarilgan
                removed += 1
                break
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    await db.set_active(cid, False)
                    removed += 1
                break
            except TelegramAPIError:
                break
        ok += sent
        fail += not sent
        await asyncio.sleep(BROADCAST_DELAY)
    text = f"✅ Reklama tarqatildi.\nYuborildi: <b>{ok}</b>\nXato: <b>{fail}</b>"
    if removed:
        text += f"\n(bot chiqarilgan {removed} ta guruh ro'yxatdan olib tashlandi)"
    try:
        await bot.send_message(admin_chat, text)
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("sa:"))
async def cb_super(cb: CallbackQuery, bot: Bot, db: DB) -> None:
    uid = cb.from_user.id
    if not is_super(uid):
        await cb.answer("⛔ Faqat bot egasi uchun.", show_alert=True)
        return
    if not cb.message or cb.message.chat.type != ChatType.PRIVATE:
        await cb.answer()
        return
    parts = cb.data.split(":")
    act = parts[1]
    arg = parts[2] if len(parts) > 2 else ""

    if act == "noop":
        await cb.answer()
    elif act == "m":
        _ad_wait.pop(uid, None)
        await sa_edit(cb, "🛠 <b>Super admin paneli</b>", sa_menu_kb())
        await cb.answer()
    elif act == "l":
        await sa_list(cb, db, int(arg or 0))
        await cb.answer()
    elif act == "g":
        await sa_detail(cb, bot, db, int(arg))
        await cb.answer()
    elif act == "t":  # xizmatni o'chirish / yoqish
        cid = int(arg)
        info = await db.chat_info(cid)
        if not info:
            await cb.answer("Guruh topilmadi.", show_alert=True)
            return
        new_state = not info["enabled"]
        await db.set_value(cid, "enabled", int(new_state))
        try:
            await bot.send_message(
                cid, "▶️ Bot xizmati qayta yoqildi." if new_state
                else "⏸ Bot xizmati bu guruhda to'xtatildi. Qayta yoqish uchun bot egasiga murojaat qiling.")
        except TelegramAPIError:
            pass
        await sa_detail(cb, bot, db, cid)
        await cb.answer("Yoqildi" if new_state else "O'chirildi")
    elif act == "q":  # guruhdan chiqish — tasdiq
        info = await db.chat_info(int(arg))
        name = html.escape(info["title"]) if info else arg
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Ha, chiqish", callback_data=f"sa:qy:{arg}"),
            InlineKeyboardButton(text="❌ Yo'q", callback_data=f"sa:g:{arg}")]])
        await sa_edit(cb, f"🚪 Bot <b>{name}</b> guruhidan chiqib ketsinmi?", kb)
        await cb.answer()
    elif act == "qy":
        cid = int(arg)
        try:
            await bot.leave_chat(cid)
        except TelegramAPIError as e:
            log.info("leave_chat xatosi %s: %s", cid, e)
        await db.set_active(cid, False)
        await sa_list(cb, db, 0)
        await cb.answer("Guruhdan chiqildi")
    elif act == "s":
        text, kb = await view_stats(db)
        await sa_edit(cb, text, kb)
        await cb.answer()
    elif act == "ad":
        _ad_wait[uid] = None
        n = len(await db.active_chat_ids())
        await sa_edit(
            cb,
            f"📣 <b>Reklama tarqatish</b>\n\nReklama xabarini yuboring (matn, rasm, video — istalgani, "
            f"<b>bitta xabar</b>). U <b>{n} ta faol guruhga</b> nusxalanadi.\nBekor qilish: /bekor",
            InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor", callback_data="sa:adno")]]),
        )
        await cb.answer()
    elif act == "adno":
        _ad_wait.pop(uid, None)
        await sa_edit(cb, "Bekor qilindi.\n\n🛠 <b>Super admin paneli</b>", sa_menu_kb())
        await cb.answer()
    elif act == "adgo":
        ad = _ad_wait.pop(uid, None)
        if not isinstance(ad, dict):
            await cb.answer("Reklama topilmadi — qaytadan boshlang.", show_alert=True)
            return
        ids = await db.active_chat_ids()
        t = asyncio.create_task(broadcast(bot, db, cb.message.chat.id, ad["chat_id"], ad["message_id"], ids))
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        await sa_edit(cb, f"🚀 Reklama {len(ids)} ta guruhga yuborilmoqda... Tugagach xabar beraman.", None)
        await cb.answer()
    else:
        await cb.answer()


# ------------------------------------------------ shaxsiy chatdagi pastki tugmalar
BTN_ADD = "➕ Guruhga qo'shish"
BTN_HELP = "ℹ️ Yordam"
BTN_ID = "🆔 Mening ID im"
BTN_ADMIN = "🛠 Admin panel"
BTN_GROUPS = "📊 Guruhlar"
BTN_STATS = "📈 Statistika"
BTN_AD = "📣 Reklama tarqatish"


def main_kb(uid: int) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_MINE)], [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_HELP)],
            [KeyboardButton(text=BTN_ID)]]
    if is_super(uid):
        rows = [
            [KeyboardButton(text=BTN_ADMIN)],
            [KeyboardButton(text=BTN_GROUPS), KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_AD)],
        ] + rows
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


_private = F.chat.type == ChatType.PRIVATE
SUPER_ONLY = "⛔ Bu bo'lim faqat bot egasi uchun."


@router.message(_private, F.text == BTN_HELP)
async def btn_help(m: Message) -> None:
    _ad_wait.pop(m.from_user.id, None)
    await m.answer(HELP_TEXT, reply_markup=main_kb(m.from_user.id))


@router.message(_private, F.text == BTN_ADD)
async def btn_add(m: Message, bot: Bot) -> None:
    _ad_wait.pop(m.from_user.id, None)
    me = await bot.me()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="➕ Guruhni tanlash",
        url=f"https://t.me/{me.username}?startgroup=true&admin=delete_messages+restrict_members",
    )]])
    await m.answer("Botni guruhingizga qo'shing. Telegram uni <b>admin</b> qilishni ham so'raydi — "
                   "<i>xabarlarni o'chirish</i> va <i>a'zolarni cheklash</i> huquqlarini bering.", reply_markup=kb)


@router.message(_private, F.text == BTN_ID)
async def btn_id(m: Message) -> None:
    await m.answer(f"Sizning Telegram ID: <code>{m.from_user.id}</code>")


@router.message(_private, F.text == BTN_ADMIN)
async def btn_admin(m: Message) -> None:
    await cmd_admin(m)


@router.message(_private, F.text == BTN_GROUPS)
async def btn_groups(m: Message, db: DB) -> None:
    if not is_super(m.from_user.id):
        await m.answer(SUPER_ONLY)
        return
    _ad_wait.pop(m.from_user.id, None)
    text, kb = await view_list(db, 0)
    await m.answer(text, reply_markup=kb)


@router.message(_private, F.text == BTN_STATS)
async def btn_stats(m: Message, db: DB) -> None:
    if not is_super(m.from_user.id):
        await m.answer(SUPER_ONLY)
        return
    _ad_wait.pop(m.from_user.id, None)
    text, kb = await view_stats(db)
    await m.answer(text, reply_markup=kb)


@router.message(_private, F.text == BTN_AD)
async def btn_ad(m: Message, db: DB) -> None:
    if not is_super(m.from_user.id):
        await m.answer(SUPER_ONLY)
        return
    _ad_wait[m.from_user.id] = None
    n = len(await db.active_chat_ids())
    await m.answer(
        f"📣 <b>Reklama tarqatish</b>\n\nReklama xabarini yuboring (matn, rasm, video — istalgani, "
        f"<b>bitta xabar</b>). U <b>{n} ta faol guruhga</b> nusxalanadi.\nBekor qilish: /bekor",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Bekor", callback_data="sa:adno")]]),
    )


def _waiting_ad(m: Message) -> bool:
    return bool(m.from_user) and is_super(m.from_user.id) and _ad_wait.get(m.from_user.id, "x") is None


@router.message(F.chat.type == ChatType.PRIVATE, _waiting_ad)
async def capture_ad(m: Message, db: DB) -> None:
    """Super admin yuborgan keyingi xabar reklama sifatida olinadi (buyruqlar esa yuqoridagi handlerlarga tushadi)."""
    _ad_wait[m.from_user.id] = {"chat_id": m.chat.id, "message_id": m.message_id}
    n = len(await db.active_chat_ids())
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Yuborish ({n} ta guruh)", callback_data="sa:adgo"),
        InlineKeyboardButton(text="❌ Bekor", callback_data="sa:adno"),
    ]])
    await m.reply(f"Shu reklamani <b>{n} ta guruhga</b> yuboraymi?", reply_markup=kb)


# ------------------------------------------------------------------------ main
async def setup_commands(bot: Bot) -> None:
    """Telegram'dagi "Menu" tugmasi ostidagi buyruqlar ro'yxati (kim nimani ko'radi)."""
    from aiogram.types import (
        BotCommand, BotCommandScopeAllChatAdministrators, BotCommandScopeAllGroupChats,
        BotCommandScopeChat, BotCommandScopeDefault,
    )

    def cmds(*pairs: tuple[str, str]) -> list[BotCommand]:
        return [BotCommand(command=c, description=d) for c, d in pairs]

    private = cmds(("start", "Boshlash va yordam"), ("qoshganlarim", "Men qo'shgan odamlar (hamma guruhlar)"),
                    ("help", "Yordam"), ("id", "Mening Telegram ID im"))
    member = cmds(("holat", "Men nechta odam qo'shdim"), ("top", "Eng ko'p odam qo'shganlar"),
                  ("murojaat", "Cheklov xato bo'lsa, adminga yozish"))
    admin = cmds(
        ("panel", "Tugmali boshqaruv paneli"), ("taqiq", "Rasm, video, stiker... taqiqlari"),
        ("limit", "Yozish uchun kerakli odam soni"), ("sozlamalar", "Hamma sozlamalar"),
        ("link", "Havolalarni taqiqlash on/off"), ("forward", "Forward taqiqi on/off"),
        ("flood", "Flood himoyasi"), ("jazo", "Ogohlantirish va mute sozlamasi"),
        ("soz_qosh", "Taqiqlangan so'z qo'shish"), ("soz_ochir", "Taqiqlangan so'zni o'chirish"),
        ("sozlar", "Taqiqlangan so'zlar soni"), ("xizmat", "Qo'shdi/chiqdi xabarlarini o'chirish"),
        ("bildirish", "Har qo'shishda qisqa xabar"), ("ozod", "Odamni shartdan ozod qilish (reply)"),
        ("ozod_bekor", "Ozodlikni qaytarib olish (reply)"),
    ) + member
    jobs = [
        (private, BotCommandScopeDefault()),
        (member, BotCommandScopeAllGroupChats()),
        (admin, BotCommandScopeAllChatAdministrators()),
    ]
    for uid in SUPER_ADMINS:
        jobs.append((private + cmds(("admin", "Super admin paneli")), BotCommandScopeChat(chat_id=uid)))
    for commands, scope in jobs:
        try:
            await bot.set_my_commands(commands, scope=scope)
        except TelegramAPIError as e:
            log.warning("buyruqlar menyusini o'rnatib bo'lmadi (%s): %s", type(scope).__name__, e)


async def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.environ["BOT_TOKEN"]
    global SUPER_ADMINS
    SUPER_ADMINS = {int(x) for x in os.getenv("SUPER_ADMINS", "").replace(",", " ").split() if x.lstrip("-").isdigit()}
    log.info("Super adminlar: %s", sorted(SUPER_ADMINS) or "belgilanmagan (/id bilan ID ni bilib oling)")
    db = DB(os.getenv("DB_PATH", "data/guruh.db"), int(os.getenv("DEFAULT_REQUIRED", "0")))
    os.makedirs(os.path.dirname(db.path) or ".", exist_ok=True)
    await db.connect()

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp["db"] = db
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.me()
    log.info("Bot ishga tushdi: @%s", me.username)
    await setup_commands(bot)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "edited_message", "my_chat_member", "callback_query"])
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
