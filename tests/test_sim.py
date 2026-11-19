"""Soxta Telegram bilan sinov: haqiqiy token kerak emas.

Ishga tushirish:  python tests/test_sim.py
"""
import asyncio
import itertools
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import (
    AnswerCallbackQuery, CopyMessage, DeleteMessage, EditMessageReplyMarkup, EditMessageText, GetChat,
    GetChatAdministrators, GetChatMember, GetChatMemberCount, GetMe, LeaveChat, RestrictChatMember,
    SendMessage,
)
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import (
    MessageId,
    ChatMemberAdministrator,
    ChatFullInfo,
    ChatMemberMember,
    ChatPermissions,
    Message,
    Update,
    User,
)

import bot as botmod
from db import DB

CHAT = -1001
BOT_ID = 123
ADMINS = {1}
calls: list[tuple[str, dict]] = []
FAIL_COPY: set[int] = set()
_mid = itertools.count(1000)


def user(uid, name="U", is_bot=False):
    return User(id=uid, is_bot=is_bot, first_name=f"{name}{uid}")



def make_admin(uid, is_bot=False):
    fields = {
        name: False
        for name, f in ChatMemberAdministrator.model_fields.items()
        if f.annotation in (bool, "bool") and f.is_required()
    }
    fields.update(status="administrator", user=user(uid, "bot" if is_bot else "U", is_bot),
                  can_delete_messages=True)
    return ChatMemberAdministrator(**fields)


class FakeSession(BaseSession):
    async def close(self):
        pass

    async def stream_content(self, *a, **k):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        data = method.model_dump(exclude_none=True)
        calls.append((type(method).__name__, data))
        if isinstance(method, (DeleteMessage, RestrictChatMember, AnswerCallbackQuery,
                               EditMessageText, EditMessageReplyMarkup)):
            return True
        if isinstance(method, GetChatMemberCount):
            return 42
        if isinstance(method, LeaveChat):
            return True
        if isinstance(method, CopyMessage):
            if method.chat_id in FAIL_COPY:
                raise TelegramForbiddenError(method=method, message="Forbidden: bot was kicked from the supergroup chat")
            return MessageId(message_id=next(_mid))
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="bot", username="testbot")
        if isinstance(method, GetChat):
            return ChatFullInfo.model_construct(
                id=method.chat_id, type="supergroup",
                title={-1002: "Ikkinchi guruh", -1003: "Yoqub guruhi"}.get(method.chat_id, "Test guruh"),
                permissions=ChatPermissions(can_send_messages=True, can_send_polls=False))
        if isinstance(method, GetChatAdministrators):
            return [make_admin(a) for a in sorted(ADMINS)]
        if isinstance(method, GetChatMember):
            uid = method.user_id
            if uid == BOT_ID or uid in ADMINS:
                return make_admin(uid, is_bot=(uid == BOT_ID))
            return ChatMemberMember(status="member", user=user(uid))
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_mid), date=datetime.now(),
                chat={"id": method.chat_id, "type": "supergroup", "title": "T"},
                text=method.text,
            )
        raise NotImplementedError(method)


_upd = itertools.count(1)


def msg(**kw):
    base = {
        "message_id": next(_mid),
        "date": int(datetime.now().timestamp()),
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test guruh"},
    }
    base.update(kw)
    return Update.model_validate({"update_id": next(_upd), "message": base})


def pm(uid, t, **kw):
    base = {"message_id": next(_mid), "date": int(datetime.now().timestamp()),
            "chat": {"id": uid, "type": "private", "first_name": f"U{uid}"}, "from": frm(uid), "text": t}
    base.update(kw)
    return Update.model_validate({"update_id": next(_upd), "message": base})


def cbq(uid, data, msg_id=555, chat=CHAT, chat_type="supergroup"):
    return Update.model_validate({"update_id": next(_upd), "callback_query": {
        "id": str(next(_upd)), "from": frm(uid), "chat_instance": "x", "data": data,
        "message": {"message_id": msg_id, "date": int(datetime.now().timestamp()),
                    "chat": {"id": chat, "type": chat_type, "title": "Test guruh"}, "text": "xabar"}}})


def edited(uid, t, **kw):
    base = {
        "message_id": next(_mid),
        "date": int(datetime.now().timestamp()),
        "edit_date": int(datetime.now().timestamp()),
        "chat": {"id": CHAT, "type": "supergroup", "title": "Test guruh"},
        "from": frm(uid), "text": t,
    }
    base.update(kw)
    return Update.model_validate({"update_id": next(_upd), "edited_message": base})


def frm(u):
    return {"id": u, "is_bot": False, "first_name": f"U{u}"}


def text(uid, t, **kw):
    return msg(**{"from": frm(uid), "text": t, **kw})


def entities_cmd(t):
    return [{"type": "bot_command", "offset": 0, "length": len(t.split()[0])}]


def cmd(uid, t, **kw):
    return text(uid, t, entities=entities_cmd(t), **kw)


def added(adder, members):
    return msg(**{"from": frm(adder), "new_chat_members": [frm(m) for m in members]})


def left(uid):
    return msg(**{"from": frm(uid), "left_chat_member": frm(uid)})


def kinds(since):
    return [(n, d) for n, d in calls[since:]]


def deleted(since):
    return [d for n, d in calls[since:] if n == "DeleteMessage"]


def sent(since):
    return [d["text"] for n, d in calls[since:] if n == "SendMessage"]


async def main():
    botmod._last_warn.clear()
    tmp = tempfile.mkdtemp()
    db = DB(os.path.join(tmp, "t.db"))
    await db.connect()
    bot = Bot(f"{BOT_ID}:AAA", session=FakeSession(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp["db"] = db
    dp.include_router(botmod.router)
    botmod._tasks.clear()

    async def feed(u):
        n = len(calls)
        await dp.feed_update(bot, u)
        return n

    ok = 0

    def check(cond, label):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + label)
        assert cond, label
        ok += 1

    # 1) oddiy foydalanuvchi /limit ni o'zgartira olmaydi
    n = await feed(cmd(10, "/limit 3"))
    check((await db.get_chat(CHAT)).required == 0, "oddiy a'zo /limit o'rnata olmaydi")
    check(len(deleted(n)) == 1, "uning buyrug'i o'chirildi")

    # 2) admin /limit 3
    n = await feed(cmd(1, "/limit 3"))
    check((await db.get_chat(CHAT)).required == 3, "admin /limit 3 o'rnatdi")

    # 3) a'zo yozsa - o'chiriladi va ogohlantiriladi
    n = await feed(text(10, "salom"))
    check(len(deleted(n)) == 1, "0/3 — xabar o'chirildi")
    check(any("0/3" in t for t in sent(n)), "ogohlantirish 0/3 ko'rsatadi")

    # 4) cooldown: ikkinchi xabar o'chadi, lekin ogohlantirish takrorlanmaydi
    n = await feed(text(10, "yana"))
    check(len(deleted(n)) == 1 and not sent(n), "ogohlantirish spam qilinmaydi (cooldown)")

    # 5) 1 ta odam qo'shdi
    n = await feed(added(10, [20]))
    check(await db.count(CHAT, 10) == 1, "10 -> 1 ta hisoblandi")
    check(any("1/3" in t for t in sent(n)), "progress 1/3")
    check(len(deleted(n)) == 1, "«qo'shdi» xizmat xabari o'chirildi")

    # 6) havola orqali kirgan hisoblanmaydi
    await feed(added(30, [30]))
    check(await db.count(CHAT, 30) == 0, "havola orqali kirish sanalmaydi")

    # 7) botni qo'shish sanalmaydi
    await feed(msg(**{"from": frm(10), "new_chat_members": [{"id": 999, "is_bot": True, "first_name": "b"}]}))
    check(await db.count(CHAT, 10) == 1, "bot qo'shish sanalmaydi")

    # 8) boshqa odam allaqachon qo'shilgan odamni qayta "qo'shsa" ikki marta sanalmaydi
    await feed(added(11, [20]))
    check(await db.count(CHAT, 11) == 0 and await db.count(CHAT, 10) == 1, "bitta odam ikki marta sanalmaydi")

    # 9) yana 2 ta -> 3/3, tabrik
    n = await feed(added(10, [21, 22]))
    check(await db.count(CHAT, 10) == 3, "10 -> 3 ta")
    check(any("yozishingiz mumkin" in t for t in sent(n)), "tabrik xabari yuborildi")

    # 10) endi yoza oladi
    n = await feed(text(10, "endi yozaman"))
    check(not deleted(n) and not sent(n), "3/3 — xabar o'chirilmadi")

    # 11) qo'shilgan odam chiqib ketsa, hisob kamayadi va yana bloklanadi
    await feed(left(21))
    check(await db.count(CHAT, 10) == 2, "chiqib ketgach hisobdan ayirildi")
    n = await feed(text(10, "yana yozaman"))
    check(len(deleted(n)) == 1, "2/3 — yana bloklandi")

    # 12) admin erkin yozadi
    n = await feed(text(1, "admin xabari"))
    check(not deleted(n), "admin shartdan ozod")

    # 13) /ozod (reply)
    n = await feed(cmd(1, "/ozod", reply_to_message={
        "message_id": 5, "date": int(datetime.now().timestamp()),
        "chat": {"id": CHAT, "type": "supergroup"}, "from": frm(50), "text": "x"}))
    n = await feed(text(50, "men ozodman"))
    check(not deleted(n), "/ozod qilingan odam yoza oladi")

    # 14) /holat va /top
    n = await feed(cmd(10, "/holat"))
    check(any("2/3" in t for t in sent(n)), "/holat 2/3 ko'rsatadi")
    n = await feed(cmd(10, "/top"))
    check(any("Eng ko'p" in t for t in sent(n)), "/top ishlaydi")

    # 15) anonim admin / kanal nomidan xabar
    n = await feed(msg(**{"from": frm(1087968824), "sender_chat": {"id": CHAT, "type": "supergroup"}, "text": "anon"}))
    check(not deleted(n), "anonim admin xabari tegilmaydi")

    # 16) /limit 0 -> hamma yoza oladi
    await feed(cmd(1, "/limit 0"))
    n = await feed(text(77, "hamma yozadi"))
    check(not deleted(n), "/limit 0 — shart o'chiq")

    # 17) /xizmat off -> xizmat xabari o'chirilmaydi
    await feed(cmd(1, "/xizmat off"))
    n = await feed(added(10, [60]))
    check(not any(True for d in deleted(n) if d["message_id"]), "/xizmat off — xizmat xabari qoladi")
    check(await db.count(CHAT, 10) == 3, "hisob baribir yuritiladi")

    # ================= QAROVUL FUNKSIYALARI =================
    def restricted(since):
        return [d for n, d in calls[since:] if n == "RestrictChatMember"]

    LINK = [{"type": "url", "offset": 4, "length": 10}]

    # 18) havola taqiqi
    n = await feed(cmd(10, "/link on"))   # oddiy a'zo: e'tiborsiz
    check(not (await db.get_chat(CHAT)).block_links, "oddiy a'zo /link ni yoqa olmaydi")
    await feed(cmd(1, "/link on"))
    check((await db.get_chat(CHAT)).block_links, "admin /link on")
    n = await feed(text(77, "oddiy xabar"))
    check(not deleted(n), "havolasiz xabar tegilmaydi")
    n = await feed(text(77, "kir t.me/abc", entities=LINK))
    check(len(deleted(n)) == 1 and any("1/3" in t for t in sent(n)), "havola o'chirildi, 1/3 ogohlantirish")
    n = await feed(text(1, "admin t.me/abc", entities=LINK))
    check(not deleted(n), "admin havola yubora oladi")

    # 19) 3 ta qoidabuzarlik = mute
    await feed(text(77, "kir t.me/abc", entities=LINK))
    n = await feed(text(77, "kir t.me/abc", entities=LINK))
    r = restricted(n)
    check(len(r) == 1 and r[0]["user_id"] == 77 and "until_date" in r[0], "3-qoidabuzarlikda mute qilindi")
    check(any("mute" in t for t in sent(n)), "mute haqida xabar berildi")

    # 20) tahrirlab havola qo'shish
    n = await feed(text(81, "salom"))
    n = await feed(edited(81, "kir t.me/abc", entities=LINK))
    check(len(deleted(n)) == 1, "tahrirlab qo'shilgan havola o'chirildi")

    # 21) forward
    fwd = {"type": "hidden_user", "date": int(datetime.now().timestamp()), "sender_user_name": "x"}
    n = await feed(text(78, "forward", forward_origin=fwd))
    check(not deleted(n), "forward taqiqi o'chiq — tegilmaydi")
    await feed(cmd(1, "/forward on"))
    n = await feed(text(78, "forward", forward_origin=fwd))
    check(len(deleted(n)) == 1, "/forward on — forward o'chirildi")

    # 22) flood
    await feed(cmd(1, "/flood 3"))
    ds = []
    for i in range(4):
        n = await feed(text(79, f"msg{i}"))
        ds.append(len(deleted(n)))
    check(ds == [0, 0, 0, 1], f"flood: 4-xabar o'chirildi {ds}")

    # 23) taqiqlangan so'zlar
    n = await feed(cmd(1, "/soz_qosh Yomon, qo`pol"))
    check(len(deleted(n)) == 1, "so'zlar qo'shilgan buyruq guruhdan o'chirildi")
    check(sorted(await db.words(CHAT)) == ["qo'pol", "yomon"], "so'zlar normallashtirib saqlandi")
    n = await feed(text(80, "bu YOMON gap"))
    check(len(deleted(n)) == 1, "so'z katta-kichik harfga qaramay topildi")
    n = await feed(text(80, "yaxshi gap"))
    check(not deleted(n), "toza xabar tegilmaydi")
    await feed(cmd(1, "/soz_ochir yomon"))
    check(await db.words(CHAT) == ["qo'pol"], "/soz_ochir ishladi")

    # 24) jazo va sozlamalar paneli
    await feed(cmd(1, "/jazo 2 30"))
    st = await db.get_chat(CHAT)
    check((st.strike_limit, st.mute_minutes) == (2, 30), "/jazo 2 30")
    n = await feed(cmd(1, "/sozlamalar"))
    check(any("Jazo" in t and "30" in t for t in sent(n)), "/sozlamalar paneli")

    # 25) ozod qilingan odamga qarovul tegmaydi
    n = await feed(text(50, "ozod t.me/abc", entities=LINK))
    check(not deleted(n), "/ozod — qarovuldan ham ozod")

    # 26) eski bazani migratsiya qilish
    import aiosqlite
    old = os.path.join(tmp, "old.db")
    async with aiosqlite.connect(old) as c:
        await c.execute("CREATE TABLE chats (chat_id INTEGER PRIMARY KEY, title TEXT, required INTEGER NOT NULL DEFAULT 0, "
                        "delete_service INTEGER NOT NULL DEFAULT 1, notify INTEGER NOT NULL DEFAULT 1)")
        await c.execute("INSERT INTO chats (chat_id, required) VALUES (5, 4)")
        await c.commit()
    db2 = DB(old)
    await db2.connect()
    st = await db2.get_chat(5)
    check(st.required == 4 and st.strike_limit == 3 and st.mute_minutes == 60, "eski baza migratsiya qilindi")
    await db2.close()

    # ================= MUROJAAT TIZIMI =================
    def sends(since):
        return [d for n, d in calls[since:] if n == "SendMessage"]

    def has_cb(d, prefix):
        kb = (d.get("reply_markup") or {}).get("inline_keyboard", [])
        return any(b.get("callback_data", "").startswith(prefix) for row in kb for b in row)

    # 27) mute xabarida murojaat tugmasi bor
    await feed(cmd(1, "/limit 0"))
    await feed(text(94, "a t.me/abc", entities=LINK))
    n = await feed(text(94, "b t.me/abc", entities=LINK))
    check(len(restricted(n)) == 1, "94 mute qilindi")
    check(any(has_cb(d, f"ap:n:{CHAT}:94") for d in sends(n)), "mute xabarida 🆘 tugma bor")

    # 28) boshqa odam bu tugmani bosa olmaydi, egasi bosadi
    n = await feed(cbq(95, f"ap:n:{CHAT}:94"))
    check(await db.last_appeal(CHAT, 94) is None, "begona odam bosgan tugma murojaat yaratmaydi")
    n = await feed(cbq(94, f"ap:n:{CHAT}:94"))
    ap = await db.last_appeal(CHAT, 94)
    check(ap is not None and ap[1] == "pending", "94 murojaat yubordi")
    s_ = sends(n)
    check(any(d["chat_id"] == CHAT and has_cb(d, "ap:ok:") for d in s_), "murojaat guruhga admin tugmalari bilan tushdi")
    check(any(d["chat_id"] == 1 for d in s_), "adminga shaxsiy xabar ham ketdi")
    aid = ap[0]

    # 29) takroriy bosish yangi murojaat yaratmaydi
    n = await feed(cbq(94, f"ap:n:{CHAT}:94"))
    check(not sends(n), "takroriy murojaat yuborilmadi (pending)")

    # 30) oddiy a'zo hal qila olmaydi
    n = await feed(cbq(95, f"ap:ok:{aid}"))
    check((await db.get_appeal(aid))["status"] == "pending" and not restricted(n), "oddiy a'zo murojaatni hal qila olmaydi")

    # 31) admin "tiklash" bosadi -> mute olinadi, strike nolga
    n = await feed(cbq(1, f"ap:ok:{aid}"))
    r = restricted(n)
    check(len(r) == 1 and r[0]["user_id"] == 94 and r[0]["permissions"].get("can_send_messages") is True,
          "admin tiklash — 94 ga yozish ruxsati qaytarildi")
    check(r[0]["permissions"].get("can_send_polls") is False, "guruhning standart ruxsatlari saqlandi (so'rovnoma yopiq)")
    check((await db.get_appeal(aid))["status"] == "approved", "holat approved")
    check(any("olib tashladi" in t for t in sent(n)), "guruhga xabar: cheklov olib tashlandi")

    # 32) allaqachon hal qilingan murojaat qayta bosilsa
    n = await feed(cbq(1, f"ap:ok:{aid}"))
    check(not restricted(n), "ikkinchi bosish hech narsa qilmaydi")

    # 33) cheklov (odam qo'shish sharti) + doimiy ozod qilish
    await feed(cmd(1, "/limit 2"))
    n = await feed(text(96, "salom"))
    check(len(deleted(n)) == 1 and any(has_cb(d, f"ap:n:{CHAT}:96") for d in sends(n)), "odam qo'shish ogohlantirishida 🆘 tugma bor")
    n = await feed(cmd(96, "/murojaat men 2 ta odam qo'shdim, hisoblanmabdi"))
    ap = await db.last_appeal(CHAT, 96)
    check(ap and ap[1] == "pending", "/murojaat buyrug'i ham ishlaydi")
    check(any("hisoblanmabdi" in d["text"] for d in sends(n) if d["chat_id"] == CHAT), "sabab adminga ko'rsatildi")
    n = await feed(cbq(1, f"ap:ex:{ap[0]}"))
    check(await db.is_exempt(CHAT, 96), "doimiy ozod qilindi")
    n = await feed(text(96, "endi yozaman"))
    check(not deleted(n), "ozod qilingandan keyin yoza oladi")

    # 34) rad etish
    await feed(text(97, "salom"))
    await feed(cbq(97, f"ap:n:{CHAT}:97"))
    ap = await db.last_appeal(CHAT, 97)
    n = await feed(cbq(1, f"ap:no:{ap[0]}"))
    check((await db.get_appeal(ap[0]))["status"] == "rejected" and not restricted(n), "rad etildi, cheklov qoldi")
    n = await feed(text(97, "yana"))
    check(len(deleted(n)) == 1, "rad etilgach hamon bloklangan")
    n = await feed(cbq(97, f"ap:n:{CHAT}:97"))
    check(not sends(n), "darrov qayta murojaat qilib bo'lmaydi (cooldown)")

    # ================= PROGRESS XABARI VA TUGMALAR =================
    def edits(since):
        return [d for n, d in calls[since:] if n == "EditMessageText"]

    def answers(since):
        return [d for n, d in calls[since:] if n == "AnswerCallbackQuery"]

    def buttons(d):
        kb = (d.get("reply_markup") or {}).get("inline_keyboard", [])
        return [b for row in kb for b in row]

    await feed(cmd(1, "/limit 3"))
    await feed(added(120, [121]))
    n = await feed(text(120, "salom"))
    warn = [d for d in sends(n) if "Qo'shganingiz" in d["text"]]
    check(len(warn) == 1, "guruhda progress xabari chiqdi")
    w = warn[0]
    check("1/3" in w["text"] and "▰" in w["text"] and "2 ta odam" in w["text"], "xabarda 1/3, progress bar va «yana 2 ta»")
    urls = [b.get("url") for b in buttons(w) if b.get("url")]
    check(urls == [f"https://t.me/testbot?start=g{CHAT}"], "«Botga o'tish» tugmasi botning lichkasiga olib boradi")
    check(any(b.get("callback_data") == f"ck:{CHAT}:120" for b in buttons(w)), "«Tasdiqlash» tugmasi bor")

    # begona odam bosolmaydi
    n = await feed(cbq(130, f"ck:{CHAT}:120"))
    check(not edits(n) and "siz uchun emas" in answers(n)[0].get("text", ""), "begona odam «Tasdiqlash» ni bosa olmaydi")

    # hali yetmagan: 1/3 deb javob beradi
    n = await feed(cbq(120, f"ck:{CHAT}:120"))
    a = answers(n)[0]
    check(a.get("show_alert") and "1/3" in a["text"], "yetmagan: alert 1/3 ko'rsatadi")
    check(edits(n) and "1/3" in edits(n)[0]["text"] and edits(n)[0].get("reply_markup"), "xabar yangilandi, tugmalar qoldi")

    # yetarli odam qo'shgach
    await feed(added(120, [122, 123]))
    n = await feed(cbq(120, f"ck:{CHAT}:120"))
    check("Tasdiqlandi" in answers(n)[0]["text"], "yetgach: «Tasdiqlandi»")
    check(edits(n) and "yozishingiz mumkin" in edits(n)[0]["text"] and not edits(n)[0].get("reply_markup"),
          "xabar «tayyor» ga almashdi, tugmalar olib tashlandi")

    # lichkada deep link
    n = await feed(pm(140, f"/start g{CHAT}", entities=[{"type": "bot_command", "offset": 0, "length": 6}]))
    pv = [d for d in sends(n) if d["chat_id"] == 140]
    check(len(pv) == 1 and "0/3" in pv[0]["text"] and "Test guruh" in pv[0]["text"], "lichkada: guruh nomi va 0/3")
    check(any(b.get("callback_data") == f"ck:{CHAT}:140" for b in buttons(pv[0])), "lichkada ham «Tasdiqlash» tugmasi")
    n = await feed(cbq(140, f"ck:{CHAT}:140", chat=140, chat_type="private"))
    check(edits(n) and edits(n)[0]["chat_id"] == 140, "lichkadagi xabar yangilandi")
    n = await feed(pm(141, "/start", entities=[{"type": "bot_command", "offset": 0, "length": 6}]))
    check(any("Salom" in d["text"] for d in sends(n)), "oddiy /start — yordam matni")

    # ================= MEDIA TAQIQLARI (/taqiq) =================
    await feed(cmd(1, "/limit 0"))
    n = await feed(cmd(1, "/taqiq"))
    panel = [d for d in sends(n) if "Taqiqlar paneli" in d["text"]]
    check(len(panel) == 1 and any(b["callback_data"] == f"tq:{CHAT}:photo" for b in buttons(panel[0])), "/taqiq paneli tugmalar bilan chiqdi")
    n = await feed(cmd(200, "/taqiq"))
    check(not [d for d in sends(n) if "Taqiqlar paneli" in d["text"]], "oddiy a'zo /taqiq och olmaydi")
    n = await feed(cbq(200, f"tq:{CHAT}:photo"))
    check((await db.get_chat(CHAT)).block_media == 0, "oddiy a'zo taqiqni o'zgartira olmaydi")
    n = await feed(cbq(1, f"tq:{CHAT}:photo"))
    check((await db.get_chat(CHAT)).block_media == botmod.MEDIA_BIT["photo"], "admin rasmni taqiqladi")
    kb_edit = [d for n_, d in calls[n:] if n_ == "EditMessageReplyMarkup"]
    check(kb_edit and any("🚫 🖼 Rasm" in b["text"] for row in kb_edit[0]["reply_markup"]["inline_keyboard"] for b in row),
          "tugma 🚫 holatiga o'zgardi")

    photo = {"photo": [{"file_id": "a", "file_unique_id": "b", "width": 10, "height": 10}]}
    n = await feed(msg(**{"from": frm(200), **photo}))
    check(len(deleted(n)) == 1 and any("rasm yuborish taqiqlangan" in t for t in sent(n)), "rasm o'chirildi va sabab aytildi")
    n = await feed(text(200, "oddiy matn"))
    check(not deleted(n), "matn tegilmaydi")
    n = await feed(msg(**{"from": frm(1), **photo}))
    check(not deleted(n), "admin rasm yubora oladi")
    sticker = {"sticker": {"file_id": "s", "file_unique_id": "u", "type": "regular", "width": 5, "height": 5,
                           "is_animated": False, "is_video": False}}
    n = await feed(msg(**{"from": frm(201), **sticker}))
    check(not deleted(n), "stiker taqiqlanmagan — tegilmaydi")
    await feed(cbq(1, f"tq:{CHAT}:sticker"))
    n = await feed(msg(**{"from": frm(202), **sticker}))
    check(len(deleted(n)) == 1, "stiker taqiqlangach o'chadi")
    await feed(cbq(1, f"tq:{CHAT}:photo"))
    check((await db.get_chat(CHAT)).block_media == botmod.MEDIA_BIT["sticker"], "rasm taqiqi qaytarildi, stiker qoldi")
    n = await feed(cmd(1, "/sozlamalar"))
    check(any("stiker" in t for t in sent(n)), "/sozlamalar taqiqlangan media'ni ko'rsatadi")
    was = (await db.get_chat(CHAT)).block_links
    await feed(cbq(1, f"tq:{CHAT}:link"))
    check((await db.get_chat(CHAT)).block_links != was, "paneldan havola taqiqi almashadi")
    n = await feed(cbq(1, f"tq:{CHAT}:x"))
    check(any(nm == "DeleteMessage" for nm, _ in calls[n:]), "«Yopish» paneli o'chiradi")

    # ================= SUPER ADMIN =================
    botmod.SUPER_ADMINS = {500}
    botmod.BROADCAST_DELAY = 0
    CHAT2, CHAT3, CHAT4 = -1002, -1003, -1004
    await db.get_chat(CHAT2, "Ikkinchi guruh")
    await db.get_chat(CHAT3, "Yoqub guruhi")
    START = [{"type": "bot_command", "offset": 0, "length": 6}]

    n = await feed(pm(501, "/admin", entities=START))
    check(any("faqat bot egasi" in d["text"] for d in sends(n)), "oddiy foydalanuvchiga /admin yopiq")
    n = await feed(pm(500, "/admin", entities=START))
    check(any(b.get("callback_data") == "sa:l:0" for d in sends(n) for b in buttons(d)), "super admin menyusi ochildi")
    n = await feed(cbq(501, "sa:l:0", chat=501, chat_type="private"))
    check(not edits(n) and "Faqat bot egasi" in answers(n)[0]["text"], "begona odam admin tugmalarini bosa olmaydi")

    n = await feed(cbq(500, "sa:l:0", chat=500, chat_type="private"))
    e = edits(n)[0]
    check("3 ta" in e["text"], "guruhlar ro'yxati: 3 ta guruh")
    gbtn = [b["callback_data"] for row in e["reply_markup"]["inline_keyboard"] for b in row if b["callback_data"].startswith("sa:g:")]
    check(len(gbtn) == 3, "har bir guruh uchun tugma bor")

    n = await feed(cbq(500, f"sa:g:{CHAT2}", chat=500, chat_type="private"))
    e = edits(n)[0]
    check("Ikkinchi guruh" in e["text"] and "42" in e["text"] and "faol" in e["text"], "guruh tafsiloti: nom, a'zolar soni, holat")

    # xizmatni o'chirish
    n = await feed(cbq(500, f"sa:t:{CHAT2}", chat=500, chat_type="private"))
    check(db.is_disabled(CHAT2), "xizmat o'chirildi")
    check(any(d["chat_id"] == CHAT2 and "to'xtatildi" in d["text"] for d in sends(n)), "guruhga to'xtatildi xabari ketdi")
    n = await feed(msg(**{"chat": {"id": CHAT2, "type": "supergroup", "title": "Ikkinchi guruh"},
                          "from": frm(1), "text": "/limit 4", "entities": entities_cmd("/limit 4")}))
    check((await db.get_chat(CHAT2)).required == 0 and any("to'xtatilgan" in t for t in sent(n)),
          "xizmat o'chiq guruhda bot buyruqqa javob bermaydi")
    n = await feed(msg(**{"chat": {"id": CHAT2, "type": "supergroup", "title": "Ikkinchi guruh"}, "from": frm(300), "text": "x t.me/a",
                          "entities": LINK}))
    check(not deleted(n), "xizmat o'chiq guruhda qarovul ishlamaydi")
    await feed(cbq(500, f"sa:t:{CHAT2}", chat=500, chat_type="private"))
    check(not db.is_disabled(CHAT2), "xizmat qayta yoqildi")

    # statistika
    n = await feed(cbq(500, "sa:s", chat=500, chat_type="private"))
    check("Faol guruhlar: <b>3</b>" in edits(n)[0]["text"], "statistika: 3 faol guruh")

    # reklama
    await feed(cbq(500, f"sa:t:{CHAT3}", chat=500, chat_type="private"))   # CHAT3 ni o'chirib qo'yamiz — reklama bormasligi kerak
    n = await feed(cbq(500, "sa:ad", chat=500, chat_type="private"))
    check("Reklama" in edits(n)[0]["text"], "reklama rejimi yoqildi")
    n = await feed(pm(501, "bu reklama emas"))
    check(not sends(n), "begona odam yuborgan xabar reklama bo'lmaydi")
    n = await feed(pm(500, "🔥 Reklama matni"))
    ask = [d for d in sends(n) if d["chat_id"] == 500]
    check(ask and "2 ta guruhga" in ask[0]["text"] and any(b["callback_data"] == "sa:adgo" for b in buttons(ask[0])),
          "reklama tasdiqlash so'raldi (xizmati o'chiq guruh sanalmaydi)")
    FAIL_COPY.add(CHAT2)   # bot CHAT2 dan chiqarilgan deb faraz qilamiz
    n = await feed(cbq(500, "sa:adgo", chat=500, chat_type="private"))
    await asyncio.sleep(0.3)
    cp = [d for nm, d in calls[n:] if nm == "CopyMessage"]
    check(sorted(d["chat_id"] for d in cp) == sorted([CHAT, CHAT2]), "reklama faqat faol guruhlarga yuborildi")
    rep = [d for d in sends(n) if d["chat_id"] == 500 and "Reklama tarqatildi" in d["text"]]
    check(rep and "Yuborildi: <b>1</b>" in rep[0]["text"] and "Xato: <b>1</b>" in rep[0]["text"], "yakuniy hisobot: 1 ta yuborildi, 1 ta xato")
    check((await db.chat_info(CHAT2))["active"] == 0, "bot chiqarilgan guruh ro'yxatdan olib tashlandi")

    # guruhdan chiqish
    n = await feed(cbq(500, f"sa:qy:{CHAT}", chat=500, chat_type="private"))
    check(any(nm == "LeaveChat" for nm, _ in calls[n:]) and (await db.chat_info(CHAT))["active"] == 0, "guruhdan chiqish ishladi")

    # bot guruhga qo'shilishi / chiqarilishi
    def mcm(old, new):
        bu = {"id": BOT_ID, "is_bot": True, "first_name": "bot", "username": "testbot"}
        return Update.model_validate({"update_id": next(_upd), "my_chat_member": {
            "chat": {"id": CHAT4, "type": "supergroup", "title": "Yangi guruh"}, "from": frm(1),
            "date": int(datetime.now().timestamp()),
            "old_chat_member": {"status": old, "user": bu}, "new_chat_member": {"status": new, "user": bu}}})
    await feed(mcm("left", "member"))
    info = await db.chat_info(CHAT4)
    check(info and info["active"] == 1 and info["title"] == "Yangi guruh", "bot guruhga qo'shildi — ro'yxatga tushdi")
    await feed(mcm("member", "left"))
    check((await db.chat_info(CHAT4))["active"] == 0, "bot chiqarilsa — nofaol")

    print(f"\nHammasi o'tdi: {ok} ta tekshiruv")
    await db.close()


asyncio.run(main())
