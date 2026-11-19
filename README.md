# Guruh qo'riqchisi (Telegram bot)

Guruhda a'zo yozishdan oldin **guruh egasi belgilagan sondagi odamni qo'shishi** shart. Bot shu sonni sanaydi,
yetmaganlarning xabarini o'chiradi, yetganda esa "endi yozishingiz mumkin" deb xabar beradi.
Shuningdek, «kim kimni qo'shdi» / «chiqib ketdi» xizmat xabarlarini o'chiradi va guruhni **qo'riqlaydi**:
havola/reklama, forward, flood (tez-tez yozish) va taqiqlangan so'zlarni o'chiradi, qoidabuzarni ogohlantiradi,
takrorlansa vaqtincha mute qiladi.

## Qanday ishlaydi
- **Hisob** — faqat haqiqiy qo'shish sanaladi (`new_chat_members` da qo'shgan odam ≠ qo'shilgan odam). Havola orqali
  kirganlar, botlar sanalmaydi.
- **Bitta odam bitta marta** — qo'shilgan odam guruhda faqat birinchi qo'shgan kishining hisobiga yoziladi.
- **Aldab bo'lmaydi** — qo'shilgan odam guruhdan chiqib ketsa (yoki chiqarib yuborilsa), qo'shgan kishining hisobidan ayiriladi.
- **Blok** — shartni bajarmagan a'zoning xabari o'chiriladi va guruhda unga progress xabari chiqadi:
  `👥 Qo'shganingiz: 3/5`, progress bar va «➕ Yana 2 ta odam qo'shing». Pastida tugmalar:
  **🤖 Botga o'tish** (botning shaxsiy chatida o'z holatini ko'radi), **✅ Tasdiqlash** (hisobni qayta tekshiradi;
  yetgan bo'lsa xabar «tayyor, yozishingiz mumkin» ga almashadi) va **🆘 adminga yozish**.
  Xabar 60 soniyadan keyin o'zi yo'qoladi. Hisob avtomatik yuritiladi — «Tasdiqlash» faqat tekshirish uchun.
- **Adminlar, anonim admin, kanal postlari va /ozod qilinganlar** shartdan ozod.

## Sozlash (guruhda)
**Eng oson yo'l:** botni guruhga qo'shing — bot guruhga yozmaydi, balki **botni qo'shgan odamning o'ziga shaxsiy chatda** yozadi:
*«a'zolar yozishdan oldin nechta odam qo'shishi kerak?»* — **3 / 5 / 10 / 15 / 20 / 30 ta** yoki **🚫 Shartsiz**. Faqat guruh adminlari
bosa oladi. Fikrni o'zgartirsa, boshqa tugmani bosadi. Qolgan sozlamalar (flood, taqiqlar...) **🎛 Boshqa sozlamalar** (yoki guruhda `/panel`) orqali.
Bot admin bo'lmasa, xabarda buni eslatadi; admin qilingach limit hali tanlanmagan bo'lsa, tugmalar qayta chiqadi.
Agar qo'shgan odam botga hali `/start` bosmagan bo'lsa (Telegram botga birinchi yozishga ruxsat bermaydi), guruhda faqat bitta qisqa
**⚙️ Botda sozlash** tugmasi chiqadi — u botni ochib, xuddi shu savolni beradi (faqat adminlar uchun). Shuning uchun guruh egasi botga bir marta `/start` bossa, yaxshi.

Qo'lda sozlash:

1. Botni guruhga qo'shing va **admin** qiling — **«Xabarlarni o'chirish»** va (mute uchun) **«A'zolarni cheklash»** huquqlari kerak.
2. `/limit 5` — yozish uchun 5 ta odam qo'shish shart. `/limit 0` — o'chirish. (Faqat adminlar.)

| Buyruq | Kim | Vazifasi |
|---|---|---|
| `/limit N` | admin | kerakli odam soni (0 = o'chiq) |
| `/holat` | hamma | men nechta odam qo'shdim (shu guruhda) |
| `/qoshganlarim` | hamma (lichkada) | hamma guruhlar bo'yicha qo'shganlarim |
| `/murojaat sabab` | hamma | cheklov xato bo'lsa, adminga yozish |
| `/top` | hamma | eng ko'p qo'shganlar (top 10) |
| `/xizmat on\|off` | admin | «qo'shdi/chiqdi» xabarlarini o'chirish |
| `/bildirish on\|off` | admin | har qo'shishda qisqa progress xabari |
| `/ozod` (reply) | admin | shu odamni shartdan ozod qilish |
| `/ozod_bekor` (reply) | admin | ozodlikni qaytarib olish |
| `/sozlamalar` | admin | hamma sozlamalar holati |
| `/panel` | admin | tugmali boshqaruv paneli |
| `/taqiq` | admin | taqiqlar paneli (media, havola, forward) |
| `/link on\|off` | admin | havolalarni (http, t.me) taqiqlash |
| `/forward on\|off` | admin | forward xabarlarni taqiqlash |
| `/flood N` | admin | 10 soniyada N tadan ko'p xabar = flood (0 = o'chiq) |
| `/soz_qosh a, b` | admin | taqiqlangan so'zlar qo'shish (buyruq xabari o'chiriladi) |
| `/soz_ochir a` · `/sozlar` | admin | so'z o'chirish · soni (ro'yxat lichkaga yuboriladi) |
| `/jazo N M` | admin | N ta qoidabuzarlikdan keyin M daqiqa mute (standart 3 va 60) |

## Tugmalar (buyruq yozmasdan)
- **Shaxsiy chatda** botga `/start` yozilgach pastda doimiy tugmalar chiqadi: **ℹ️ Yordam · ➕ Guruhga qo'shish · 🆔 Mening ID im**.
  Super adminda qo'shimcha: **🛠 Admin panel · 📊 Guruhlar · 📈 Statistika · 📣 Reklama tarqatish**.
- **Guruhda** admin `/panel` yozadi — tugmali panel: yozish sharti (➖ 0 3 5 10 ➕), flood, xizmat xabarlari, progress xabari,
  🛡 Taqiqlar, ⚙️ Sozlamalar. Hech narsa yozish shart emas.
- **Telegram «Menu» tugmasi** (yozish maydoni yonida) har kim uchun o'z buyruqlari ro'yxatini ko'rsatadi:
  oddiy a'zoga `/holat`, `/top`, `/murojaat`; guruh adminlariga to'liq ro'yxat; super adminga shaxsiy chatda `/admin` ham.
  Ro'yxat bot har ishga tushganda avtomatik yangilanadi.

## Nechta odam qo'shdim? — botda ko'rish
- Guruhda odam qo'shilganda bot qisqa xabar yozadi (`➕ Ali: 3/5 — yana 2 ta`) va pastida **🤖 Botga o'tib ko'rish** tugmasi bo'ladi.
  Tugma botning shaxsiy chatini ochadi va o'sha guruhdagi hisobni (3/5, progress bar) ko'rsatadi. Tabrik xabarida ham shu tugma bor.
- Botning shaxsiy chatida pastdagi **👥 Qo'shganlarim** tugmasi (yoki `/qoshganlarim`) **hamma guruhlar bo'yicha** hisobni
  ko'rsatadi: har guruhda nechta odam qo'shgan, shart bo'lsa necha qolgan, jami son. **🔄 Yangilash** tugmasi hisobni yangilaydi.
- Qo'shilgan odamlarning ismi saqlanmaydi, faqat soni.

## Taqiqlar paneli (guruh admini)
`/taqiq` — tugmali panel: **havola, forward, rasm, video, stiker, GIF, ovozli xabar, dumaloq video, musiqa, fayl, kontakt,
joylashuv, so'rovnoma**. ✅ — ruxsat, 🚫 — taqiq; tugmani bosib almashtiriladi. Taqiqni buzgan xabar o'chiriladi,
ogohlantirish yoziladi, ko'p takrorlansa mute (`/jazo`). Adminlar va `/ozod` qilinganlar ozod.

## Super admin (bot egasi)
1. Botga **shaxsiy chatda** `/id` yozing — Telegram ID ingiz chiqadi.
2. `.env` ga yozing: `SUPER_ADMINS=123456789` (bir nechta bo'lsa vergul bilan) va botni qayta ishga tushiring.
3. Shaxsiy chatda `/admin` — panel:
   - **📊 Guruhlar** — bot ishlayotgan guruhlar ro'yxati (sahifalab). Guruhni bossangiz: nom, ID, a'zolar soni, sharti,
     taqiqlari, hisoblangan qo'shishlar, qo'shilgan sana. Shu yerdan:
     - **⏸ Xizmatni o'chirish / ▶️ yoqish** — o'chirilgan guruhda bot hech narsa qilmaydi (qarovul, hisob, buyruqlar to'xtaydi;
       guruhga xabar boradi), ma'lumotlar saqlanib qoladi;
     - **🚪 Guruhdan chiqish** — bot guruhni tark etadi.
   - **📈 Statistika** — faol/to'xtatilgan guruhlar, hisoblangan qo'shishlar, ochiq murojaatlar.
   - **📣 Reklama tarqatish** — bitta xabar (matn/rasm/video) yuborasiz, bot uni barcha **faol** guruhlarga nusxalaydi
     (xizmati o'chirilganlariga yubormaydi), oxirida hisobot beradi. Bot chiqarilgan guruhlar ro'yxatdan avtomatik olib tashlanadi.
     Bekor qilish: `/bekor`.
- Super admin buyruqlari faqat shaxsiy chatda ishlaydi va faqat `SUPER_ADMINS` dagilarga ochiq.

## Murojaat (cheklov xato bo'lsa)
Cheklangan yoki mute qilingan a'zo adminga yozishi mumkin:
- ogohlantirish/mute xabaridagi **«🆘 Xato bo'lsa — adminga yozish»** tugmasi (tugmani faqat o'sha odamning o'zi bosa oladi), yoki
- `/murojaat sabab` buyrug'i (mute qilinmagan, faqat odam qo'shish sharti bilan bloklangan a'zo uchun).

Murojaat guruhga va adminlarning shaxsiy chatiga (botni /start qilgan adminlarga) tushadi. Admin tugmalari:
**✅ Cheklovni olib tashlash** (mute olinadi, ogohlantirishlar nolga tushadi), **🔓 Doimiy ozod qilish** (shuningdek `/ozod`
ro'yxatiga qo'shadi) yoki **❌ Rad etish**. Faqat guruh adminlari hal qila oladi; birinchi bosgan adminniki kuchga kiradi.
Bir odam bir vaqtda bitta murojaat yubora oladi, hal qilingach 10 daqiqa kutadi. Hal qilingan murojaatlar 30 kundan keyin bazadan o'chadi.

## Qarovul qoidalari
- Har bir o'chirilgan xabar uchun ogohlantirish yoziladi ("1/3"). Limitga yetsa — mute, sanoq nolga tushadi.
  24 soat qoidabuzarlik bo'lmasa, sanoq yangidan boshlanadi.
- Tahrirlab keyin havola/taqiqlangan so'z qo'shishga ham ishlaydi.
- So'z filtri qism bo'yicha qidiradi (`so'k` → `so'kdi` ham topiladi), katta-kichik harf va `'ʻ’` belgilariga qaramaydi.
- Adminlar, `/ozod` qilinganlar, anonim admin va kanal postlari qarovuldan ham ozod.
- Hamma qarovul funksiyalari boshida **o'chiq**; admin yoqadi.

## Eslatma
- Bot **mavjud a'zolar ro'yxatini ko'ra olmaydi** (Telegram ruxsat bermaydi). Shuning uchun limit yoqilgach, eski a'zolar
  ham birinchi yozganida shartga duch keladi. Eski faol a'zolarni `/ozod` bilan ozod qilish mumkin.
- Bot faqat o'zi guruhda bo'lgandan keyingi qo'shishlarni sanaydi.

## Ishga tushirish

### 1) Lokal / Docker (sinov)
```bash
cp .env.example .env      # BOT_TOKEN ni yozing
docker compose up --build
```

### 2) VPS (systemd, root siz — savebot kabi)
```bash
git pull
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # BOT_TOKEN
mkdir -p ~/.config/systemd/user && cp guruh-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now guruh-bot
```

### 3) Sinov (token kerak emas)
```bash
pip install -r requirements.txt && python tests/test_sim.py
```

## BotFather
Privacy mode ahamiyatsiz — bot admin bo'lgani uchun barcha xabarlarni ko'radi. Xohlasangiz `/setjoingroups` yoqilgan bo'lsin.
