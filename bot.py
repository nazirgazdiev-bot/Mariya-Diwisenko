import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from aiogram.client.default import DefaultBotProperties
from aiohttp import web
from dotenv import load_dotenv

from mariya import Mariya
from prodamus import ProdamusClient
from sheets import SheetsClient
from storage import Storage

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
DB_PATH = os.environ.get("DB_PATH", "mariya_data.db")
MENU_PATH = os.environ.get("MENU_PATH", "menu.json")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID")  # для тестовой команды /testpay
PRODAMUS_SECRET_KEY = os.environ.get("PRODAMUS_SECRET_KEY")
PRODAMUS_SHOP_URL = os.environ.get("PRODAMUS_SHOP_URL", "https://pprecepty.payform.ru/")
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))
# Google Sheets: включается только если заданы ОБЕ переменные (аккаунт клиента, не хардкод)
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID")
SHEETS_SYNC_INTERVAL = 5 * 60  # секунд между обновлениями дашборда
# Привязываем к расположению bot.py, а не к рабочему каталогу,
# чтобы фото находились при запуске из любого CWD
PHOTOS_DIR = os.environ.get(
    "PHOTOS_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "photos"),
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("recipe-bot")

# ─── Загрузка меню ───────────────────────────────────────────────────────────

with open(MENU_PATH, "r", encoding="utf-8") as f:
    data = json.load(f)

STRUCTURE = data["structure"]
MENU = data["menu"]
RECIPES = {r["id"]: r for r in data["recipes"]}
CATEGORIES = list(STRUCTURE.keys())

# ─── Клавиатуры ──────────────────────────────────────────────────────────────

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🍽 Рецепты")],
            [KeyboardButton(text="🤖 Спросить МарИИю")],
        ],
        resize_keyboard=True,
    )

def categories_keyboard():
    buttons = [
        [InlineKeyboardButton(text=cat, callback_data=f"cat:{i}")]
        for i, cat in enumerate(CATEGORIES)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def subcategories_keyboard(cat_idx: int):
    cat = CATEGORIES[cat_idx]
    subcats = STRUCTURE[cat]
    buttons = [
        [InlineKeyboardButton(text=sub, callback_data=f"sub:{cat_idx}:{j}")]
        for j, sub in enumerate(subcats)
    ]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def recipes_keyboard(cat_idx: int, sub_idx: int):
    cat = CATEGORIES[cat_idx]
    sub = STRUCTURE[cat][sub_idx]
    recipe_ids = MENU[cat][sub]
    buttons = []
    for rid in recipe_ids:
        if rid in RECIPES:
            name = RECIPES[rid]["name"]
            if len(name) > 40:
                name = name[:37] + "..."
            buttons.append([InlineKeyboardButton(text=name, callback_data=f"rec:{rid}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"back:cat:{cat_idx}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def recipe_keyboard(cat_idx: int, sub_idx: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад к списку", callback_data=f"back:sub:{cat_idx}:{sub_idx}")]
    ])

# ─── Фото рецептов ────────────────────────────────────────────────────────────

def get_photo_path(recipe_id: str) -> str | None:
    """Путь к фото рецепта (photos/<id>.jpg) или None, если файла нет.
    Фолбэк — поиск без учёта регистра и с другими расширениями."""
    candidate = os.path.join(PHOTOS_DIR, f"{recipe_id}.jpg")
    if os.path.exists(candidate):
        return candidate
    if os.path.isdir(PHOTOS_DIR):
        rid = recipe_id.lower()
        for fn in os.listdir(PHOTOS_DIR):
            stem, ext = os.path.splitext(fn)
            if stem.lower() == rid and ext.lower() in (".jpg", ".jpeg", ".png"):
                return os.path.join(PHOTOS_DIR, fn)
    return None

# ─── Форматирование рецепта ───────────────────────────────────────────────────

def format_recipe(recipe: dict) -> str:
    name = recipe.get("name", "")
    kcal = recipe.get("kcal", "—")
    protein = recipe.get("protein", "—")
    fat = recipe.get("fat", "—")
    carbs = recipe.get("carbs", "—")
    ingredients = recipe.get("ingredients", [])
    ingredients_text = "\n".join(f"• {ing}" for ing in ingredients)
    instructions = recipe.get("instructions", "").replace("ПРИГОТОВЛЕНИЕ", "").strip()
    if len(instructions) > 2000:
        instructions = instructions[:1997] + "..."

    text = f"<b>🍳 {name}</b>\n\n"
    text += f"📊 <b>КБЖУ на 100г:</b>\n"
    text += f"🔥 {kcal} ккал  |  🥩 Б: {protein}г  |  🧈 Ж: {fat}г  |  🍞 У: {carbs}г\n\n"
    text += f"🛒 <b>Ингредиенты:</b>\n{ingredients_text}\n\n"
    text += f"👨‍🍳 <b>Приготовление:</b>\n{instructions}"
    return text

def find_cat_sub_for_recipe(recipe: dict):
    cat_idx, sub_idx = 0, 0
    cat_name = recipe.get("category", "")
    menu_paths = recipe.get("menu_paths", [])
    if cat_name in CATEGORIES:
        cat_idx = CATEGORIES.index(cat_name)
    if menu_paths:
        parts = menu_paths[0].split("/")
        if len(parts) > 1:
            sub_name = parts[1]
            cat = CATEGORIES[cat_idx]
            subcats = STRUCTURE.get(cat, [])
            if sub_name in subcats:
                sub_idx = subcats.index(sub_name)
    return cat_idx, sub_idx

# ─── Воронка продаж: тексты, тарифы, клавиатуры ──────────────────────────────

FUNNEL_CHECK_INTERVAL = 25          # секунд между проверками фоновой задачи
RENEWAL_REMIND_BEFORE = 2 * 86400   # напоминать о продлении за 2 дня

TIERS = {
    "1m": {"title": "1 месяц — 1690₽", "days": 30, "price": 1690},
    "3m": {"title": "3 месяца — 3990₽ (выгоднее на 20%)", "days": 90, "price": 3990},
    "6m": {"title": "6 месяцев — 6990₽ (выгоднее на 30%)", "days": 180, "price": 6990},
}

# Сегменты для /broadcast: ключ команды -> статус подписки (None = все).
BROADCAST_SEGMENTS = {
    "all": None,
    "paid": "active",
    "unpaid": "trial",
    "expired": "expired",
}
BROADCAST_DELAY = 0.05  # ~20 сообщений/сек — с запасом от лимита Telegram (~30/сек)

WELCOME_FUNNEL_TEXT = (
    "Привет! 💜 Это Мария — рада видеть тебя здесь.\n\n"
    "Ты зашла в моего бота с рецептами и личным ИИ-ассистентом. "
    "Тут собрано всё, чем я сама пользуюсь каждый день:\n\n"
    "250+ моих фирменных ПП-рецептов и МарИИя — умный помощник, который считает КБЖУ "
    "и собирает рационы под тебя из моего сборника рецептов\n\n"
    "Сейчас за пару минут покажу, как это работает 👋"
)

STEP1_VIDEO_TEXT = (
    "Смотри коротенькое видео — за 2 минуты покажу тебе всё: как искать рецепты, "
    "как работает МарИИя и как всё это будет экономить тебе кучу времени и нервов "
    "каждый день 👋"
)

TARIFF_CARD_TEXT = (
    "🔥 Что ты получишь внутри бота:\n\n"
    "🍽 250+ моих фирменных ПП-рецептов — разбиты по категориям: завтраки, мясо, "
    "десерты и другое. Захотела — открыла — приготовила. "
    "(рецепты будут пополняться постоянно)\n\n"
    "🤖 МарИИя — мой личный ИИ-ассистент. Считает твоё КБЖУ по моему методу "
    "и собирает тебе готовый рацион на день, неделю или месяц. Под твои цели, "
    "вкусы и даже аллергии. И всё это — только из моих проверенных рецептов.\n\n"
    "Это как иметь меня в кармане в режиме 24/7 💜\n\n"
    "Выбирай доступ и погнали 👇"
)

DOZHIM_1_TEXT = (
    "<b>Смотри, честно 🙌</b>\n\n"
    "Сколько времени в день ты тратишь только на то, чтобы решить — что приготовить? "
    "А потом ещё найти рецепт, прикинуть, впишется ли это в твое КБЖУ...\n\n"
    "По чуть-чуть — а в сумме это часы каждую неделю. И каждый день по новой этот "
    "мучительный вопрос: «ну что же поесть?»\n\n"
    "А потом вообще выгорание и фраза «ну нахер эти подсчеты»\n\n"
    "Именно чтобы закрыть это, я и сделала бота. Открыла → выбрала → приготовила. "
    "Или попросила МарИИю собрать меню — и вообще не думаешь.\n\n"
    "<b>Скорее забирай доступ по лучшим условиям 👇</b>"
)

DOZHIM_2_TEXT = (
    "<b>А теперь про то, что бесит вообще всех 🤯</b>\n\n"
    "Взвешивать каждый кусок. Заносить в приложение. Считать белки-жиры-углеводы. "
    "Вести дневник питания, который бросаешь через 3 дня.\n\n"
    "Знакомо? Так вот — считать больше НЕ надо. За тебя это делает МарИИя.\n\n"
    "Пишешь ей свою цель — она сама рассчитывает твоё КБЖУ по моему методу и выдаёт "
    "готовый рацион. Тебе остаётся только готовить и есть.\n\n"
    "<b>Никаких весов и таблиц. Попробуешь? 👇</b>"
)

DOZHIM_3_TEXT = (
    "<b>🔥 Один из самых частых вопросов ко мне:</b>\n\n"
    "«Маша, я же готовлю на всю семью. Мне что, отдельно себе варить? "
    "На это нет ни времени, ни сил.»\n\n"
    "Понимаю как никто. Поэтому в моём боте много рецептов, которые едят все — "
    "и муж, и дети, и ты. Вкусно и при этом в твою цель.\n\n"
    "А если не знаешь, что выбрать — МарИИя соберёт меню, которое подойдёт и тебе, "
    "и семье. Готовишь один раз, а не стоишь у плиты круглосуточно...."
)

PAID_TEXT = (
    "<b>Красотка, ты в деле! 🎉 Доступ открыт.</b>\n\n"
    "С чего советую начать:\n"
    "1️⃣ Загляни в «Рецепты» — полистай категории, сохрани в избранное что приглянулось\n"
    "2️⃣ Нажми «Спросить МарИИю» и попроси собрать тебе рацион на день/неделю — "
    "просто напиши свою цель (любимые продукты, на что аллергия, свою цель и КБЖУ)\n\n"
    "P.s. если не знаешь КБЖУ МарИИя тоже сможет рассчитать тебе его, просто напиши "
    "свой рост/вес, цель (похудение, набор) и желаемый вес, МарИИя высчитает КБЖУ "
    "под тебя по моей методике!\n\n"
    "<b>Пользуйся в удовольствие. Я вложила сюда всю свою систему питания — "
    "теперь она твоя 💫</b>"
)

# TODO: точный текст пришлёт Мария — пока нейтральная заглушка
RENEWAL_TEXT = (
    "⏰ Твоя подписка скоро закончится — вместе с ней закроется доступ к рецептам "
    "и МарИИе.\n\nЧтобы ничего не потерять, продли доступ заранее 👇"
)


def tariffs_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TIERS[t]["title"], callback_data=f"tier:{t}")]
        for t in ("1m", "3m", "6m")
    ])

def pay_cta_keyboard(text: str):
    """Одна кнопка, ведущая на карточку с тарифами."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="show_tariffs")]
    ])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso_in(seconds: float) -> str:
    return (now_utc() + timedelta(seconds=seconds)).isoformat()


prodamus_client = ProdamusClient(PRODAMUS_SECRET_KEY or "", PRODAMUS_SHOP_URL) if PRODAMUS_SECRET_KEY else None
# Тестовый режим Продамуса: платёж проходит без реальной карты, подпись при этом
# остаётся боевой (не влияет на проверку вебхука) — включается только через env var,
# чтобы не забыть выключить перед реальными платежами.
PRODAMUS_DEMO_MODE = os.environ.get("PRODAMUS_DEMO_MODE", "0") == "1"

async def generate_payment_link(user_id: str, tier: str) -> str:
    """Генерирует ссылку на оплату Продамуса.
    order_id кодирует user_id и tier, чтобы вебхук потом понял,
    кому и какой тариф активировать."""
    if not prodamus_client:
        return "ссылка на оплату скоро тут"
    order_id = f"{user_id}_{tier}_{secrets.token_hex(4)}"
    data = {
        "do": "pay",
        "order_id": order_id,
        "products": [
            {
                "name": TIERS[tier]["title"],
                "price": TIERS[tier]["price"],
                "quantity": 1,
            }
        ],
    }
    if PRODAMUS_DEMO_MODE:
        data["demo_mode"] = 1
        log.warning("Продамус: ссылка сгенерирована в ДЕМО-режиме (PRODAMUS_DEMO_MODE=1)")
    _diag["last_payment_link_order_id"] = order_id
    _diag["prodamus_demo_mode"] = PRODAMUS_DEMO_MODE
    return prodamus_client.build_payment_link(data)


# ─── Инициализация бота ───────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
storage: Storage = None
mariya: Mariya = None
sheets_client: SheetsClient | None = None


async def _sheets_log_payment(user_id: str, tier: str, amount, commission_sum, status: str, order_id: str):
    """gspread синхронный — уводим вызов в поток, чтобы не блокировать event loop."""
    if not sheets_client:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, sheets_client.log_payment, user_id, tier, amount, commission_sum, status, order_id
        )
    except Exception:
        log.exception("Не удалось записать платёж в Google Sheets user_id=%s order_id=%s", user_id, order_id)


async def _sheets_write_dashboard():
    if not sheets_client or not storage:
        return
    try:
        metrics = await storage.dashboard_metrics()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, sheets_client.write_dashboard_snapshot, metrics)
    except Exception:
        log.exception("Не удалось обновить дашборд Google Sheets")


def _to_float(value) -> float | None:
    """Продамус присылает числа строками ('1690.00' и т.п.) — безопасный парсинг."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

# ─── Диагностика поллинга и вебхука (временно, чтобы обойти проблему с логами Railway) ──
_diag = {
    "polling_attempts": 0,
    "last_error": None,
    "last_error_at": None,
    "last_started_at": None,
    "bot_token_prefix": BOT_TOKEN.split(":")[0] if BOT_TOKEN else None,
    "prodamus_demo_mode": None,  # выставится после чтения PRODAMUS_DEMO_MODE ниже
    "last_webhook_at": None,
    "last_webhook_raw": None,
    "last_webhook_sig_valid": None,
    "last_webhook_order_num": None,
    "last_webhook_status": None,
    "last_webhook_result": None,
    "last_payment_link_order_id": None,
}


# ─── Воронка продаж: логика ───────────────────────────────────────────────────

async def has_access(user_id: str) -> bool:
    """Активна ли подписка. Просроченную active сразу переводит в expired."""
    if not storage:
        return True  # без БД пейволл не работает — не блокируем бота
    sub = await storage.get_subscription(user_id)
    if not sub or sub["status"] != "active":
        return False
    paid_until = sub.get("paid_until")
    if paid_until and paid_until <= now_utc().isoformat():
        await storage.upsert_subscription(user_id, status="expired")
        return False
    return True

async def send_tariff_card(chat_id: int):
    await bot.send_message(chat_id, TARIFF_CARD_TEXT, reply_markup=tariffs_keyboard())

async def send_funnel_step(chat_id: int, step: int) -> tuple[int | None, float | None]:
    """Шлёт сообщение шага воронки. Возвращает (следующий шаг, задержка в сек)."""
    if step == 1:
        await bot.send_message(
            chat_id, STEP1_VIDEO_TEXT,
            reply_markup=pay_cta_keyboard("Перейти к оплате"),
        )
        return 2, 10
    if step == 2:
        await send_tariff_card(chat_id)
        return 3, 30 * 60
    if step == 3:
        await bot.send_message(
            chat_id, DOZHIM_1_TEXT,
            reply_markup=pay_cta_keyboard("Получить доступ"),
        )
        return 4, 30 * 60
    if step == 4:
        await bot.send_message(
            chat_id, DOZHIM_2_TEXT,
            reply_markup=pay_cta_keyboard("Получить доступ к боту"),
        )
        return 5, 60 * 60
    if step == 5:
        await bot.send_message(
            chat_id, DOZHIM_3_TEXT,
            reply_markup=pay_cta_keyboard("Попробовать бота"),
        )
        return 6, None  # последний дожим — дальше не шлём
    return None, None

async def mark_paid(
    user_id: str,
    tier: str,
    *,
    amount: float | None = None,
    commission_sum: float | None = None,
    order_id: str | None = None,
):
    """Ветка "после оплаты": вызывается вебхуком Продамуса (и /testpay для теста).
    Открытие доступа в БД — критическая часть и должна прокидывать исключение
    наверх (чтобы вебхук вернул ошибку и Продамус повторил попытку).
    Отправка уведомления пользователю и запись в Google Sheets — best-effort:
    если юзер заблокировал бота или Sheets недоступен, это не должно выглядеть
    как "оплата не прошла"."""
    days = TIERS[tier]["days"]
    paid_until = (now_utc() + timedelta(days=days)).isoformat()
    await storage.upsert_subscription(
        user_id,
        status="active",
        tier=tier,
        paid_until=paid_until,
        funnel_next_at=None,
        renewal_reminder_sent=0,
    )
    log.info("Оплата: user_id=%s tier=%s до %s", user_id, tier, paid_until)

    final_amount = amount if amount is not None else TIERS[tier]["price"]
    final_commission = commission_sum or 0

    # Собственная история платежей в SQLite — источник метрик "продлил/не продлил",
    # не зависит от того, подключены ли Google Sheets.
    await storage.add_payment(
        user_id=user_id, tier=tier, amount=final_amount,
        commission_sum=final_commission, order_id=order_id or "", status="success",
    )
    # Журнал в Google Sheets (если подключен).
    await _sheets_log_payment(
        user_id=user_id, tier=tier, amount=final_amount,
        commission_sum=final_commission, status="success", order_id=order_id or "",
    )

    try:
        await bot.send_message(int(user_id), PAID_TEXT, reply_markup=main_keyboard())
    except TelegramForbiddenError:
        log.info("Оплата прошла, но юзер заблокировал бота: user_id=%s", user_id)
        await storage.set_blocked(user_id, True)
    except Exception:
        log.exception("Оплата прошла, но не удалось отправить уведомление user_id=%s", user_id)

# ─── Вебхук Продамуса ─────────────────────────────────────────────────────────

async def handle_prodamus_webhook(request: web.Request) -> web.Response:
    raw_body = await request.text()
    _diag["last_webhook_at"] = datetime.now(timezone.utc).isoformat()
    _diag["last_webhook_raw"] = raw_body[:1000]
    if not prodamus_client:
        log.error("Продамус: пришёл вебхук, но PRODAMUS_SECRET_KEY не настроен")
        _diag["last_webhook_result"] = "not configured"
        return web.Response(status=500, text="not configured")

    try:
        body = prodamus_client.parse(raw_body)
    except Exception:
        log.exception("Продамус: не смог распарсить тело вебхука: %s", raw_body[:500])
        _diag["last_webhook_result"] = "bad body"
        return web.Response(status=400, text="bad body")

    sign_header = request.headers.get("Sign", "")
    sig_valid = prodamus_client.verify(body, sign_header)
    _diag["last_webhook_sig_valid"] = sig_valid
    if not sig_valid:
        log.warning("Продамус: неверная подпись вебхука, тело=%s", raw_body[:500])
        _diag["last_webhook_result"] = "bad signature"
        return web.Response(status=400, text="bad signature")

    order_num = body.get("order_num", "")
    payment_status = body.get("payment_status", "")
    _diag["last_webhook_order_num"] = order_num
    _diag["last_webhook_status"] = payment_status
    parts = order_num.split("_")
    if len(parts) < 2:
        log.warning("Продамус: не распознал order_num=%s", order_num)
        _diag["last_webhook_result"] = "unrecognized order_num"
        return web.Response(status=200, text="ignored")

    user_id, tier = parts[0], parts[1]
    log.info("Продамус webhook: order_num=%s status=%s", order_num, payment_status)

    if payment_status == "success" and tier in TIERS:
        amount = _to_float(body.get("sum"))
        commission_sum = _to_float(body.get("commission_sum"))
        try:
            await mark_paid(
                user_id, tier,
                amount=amount, commission_sum=commission_sum, order_id=order_num,
            )
            _diag["last_webhook_result"] = "mark_paid ok"
        except Exception as e:
            log.exception("Продамус: ошибка активации user_id=%s tier=%s", user_id, tier)
            _diag["last_webhook_result"] = f"mark_paid error: {e}"
            return web.Response(status=500, text="error")
    else:
        log.info("Продамус webhook: статус %s не success или тариф %s неизвестен — игнор", payment_status, tier)
        _diag["last_webhook_result"] = f"ignored (status={payment_status})"

    return web.Response(status=200, text="success")

async def handle_debug(request: web.Request) -> web.Response:
    import json as _json
    return web.Response(
        status=200,
        content_type="application/json",
        text=_json.dumps(_diag, ensure_ascii=False, default=str),
    )

async def run_polling_safe():
    """Обёртка над dp.start_polling: ловит и запоминает любую ошибку,
    чтобы её можно было увидеть через /debug (логи Railway сейчас не видны)."""
    while True:
        _diag["polling_attempts"] += 1
        _diag["last_started_at"] = datetime.now(timezone.utc).isoformat()
        try:
            await dp.start_polling(bot)
        except Exception as e:
            _diag["last_error"] = f"{type(e).__name__}: {e}"
            _diag["last_error_at"] = datetime.now(timezone.utc).isoformat()
            log.exception("Поллинг упал, перезапуск через 5с")
            await asyncio.sleep(5)

async def prodamus_webhook_server():
    app = web.Application()
    app.router.add_post("/prodamus/webhook", handle_prodamus_webhook)
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    app.router.add_get("/debug", handle_debug)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    log.info("Вебхук Продамуса слушает на порту %s", WEBHOOK_PORT)
    while True:
        await asyncio.sleep(3600)

async def funnel_tick():
    """Один проход фоновой проверки: шаги воронки, истечение, напоминания."""
    now_iso = now_utc().isoformat()

    # 1. Кто созрел по funnel_next_at — шлём следующий шаг
    for sub in await storage.due_funnel_users(now_iso):
        uid = sub["user_id"]
        try:
            next_step, delay = await send_funnel_step(int(uid), sub["funnel_step"])
        except TelegramForbiddenError:
            log.info("Воронка: user_id=%s заблокировал бота — останавливаем", uid)
            await storage.set_blocked(uid, True)
            await storage.set_funnel_step(uid, sub["funnel_step"], None)
            continue
        except Exception:
            # прочая ошибка отправки — останавливаем воронку, чтобы не долбить
            log.exception("Не отправился шаг воронки user_id=%s step=%s", uid, sub["funnel_step"])
            await storage.set_funnel_step(uid, sub["funnel_step"], None)
            continue
        if next_step is None:
            await storage.set_funnel_step(uid, sub["funnel_step"], None)
        else:
            await storage.set_funnel_step(uid, next_step, iso_in(delay) if delay else None)

    # 2. Просроченные подписки → expired
    for sub in await storage.expired_active_subscriptions(now_iso):
        await storage.upsert_subscription(sub["user_id"], status="expired")
        log.info("Подписка истекла: user_id=%s", sub["user_id"])

    # 3. Дожим на продление: paid_until истекает в ближайшие 2 дня
    deadline_iso = iso_in(RENEWAL_REMIND_BEFORE)
    for sub in await storage.expiring_subscriptions(now_iso, deadline_iso):
        uid = sub["user_id"]
        try:
            await bot.send_message(
                int(uid), RENEWAL_TEXT,
                reply_markup=pay_cta_keyboard("Продлить подписку"),
            )
        except TelegramForbiddenError:
            log.info("Напоминание о продлении: user_id=%s заблокировал бота", uid)
            await storage.set_blocked(uid, True)
        except Exception:
            log.exception("Не отправилось напоминание о продлении user_id=%s", uid)
        # флаг ставим в любом случае, чтобы не ретраить каждый тик
        await storage.upsert_subscription(uid, renewal_reminder_sent=1)

async def funnel_worker():
    while True:
        try:
            await funnel_tick()
        except Exception:
            log.exception("Ошибка фоновой задачи воронки")
        await asyncio.sleep(FUNNEL_CHECK_INTERVAL)

async def sheets_sync_worker():
    """Каждые 5 минут обновляет лист «Дашборд» (если Google Sheets подключен).
    Если не подключен — просто ждёт, ничего не делая (дёшево)."""
    while True:
        await _sheets_write_dashboard()
        await asyncio.sleep(SHEETS_SYNC_INTERVAL)

# ─── Команды ─────────────────────────────────────────────────────────────────

@dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = str(message.from_user.id)
    if storage:
        await storage.set_blocked(uid, False)  # написал нам — точно не заблокирован
        name = message.from_user.first_name
        client = await storage.get_client(uid)
        if not client.get("name"):
            await storage.upsert_client(uid, name, client.get("profile", {}))

    if not storage:
        await message.answer(WELCOME_FUNNEL_TEXT, reply_markup=main_keyboard())
        return

    sub = await storage.get_subscription(uid)
    if sub is None:
        # Новый юзер — запускаем воронку: шаг 1 через 5 секунд
        await storage.upsert_subscription(uid, status="trial", funnel_step=0)
        await message.answer(WELCOME_FUNNEL_TEXT, reply_markup=main_keyboard())
        await storage.set_funnel_step(uid, 1, iso_in(5))
    elif await has_access(uid):
        await message.answer(
            "👋 Привет! Это сборник полезных рецептов Марии Дивисенко.\n\n"
            "🍽 <b>Рецепты</b> — просматривать 250+ рецептов по категориям\n"
            "🤖 <b>Спросить МарИИю</b> — AI-нутрициолог: рационы, расчёт КБЖУ, советы\n\n"
            "Выбирай что нужно 👇",
            reply_markup=main_keyboard(),
        )
    else:
        # Запись есть, но не оплачено — приветствуем и сразу показываем тарифы
        await message.answer(WELCOME_FUNNEL_TEXT, reply_markup=main_keyboard())
        await send_tariff_card(message.chat.id)

@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    if not storage:
        await message.answer("База данных не подключена.")
        return
    uid = str(message.from_user.id)
    client = await storage.get_client(uid)
    profile = client.get("profile", {})
    facts = client.get("facts", [])
    lines = [f"👤 <b>Профиль {client.get('name') or 'клиента'}</b>\n"]
    if profile.get("goal"):
        lines.append(f"Цель: {profile['goal']}")
    if profile.get("target_kcal"):
        lines.append(f"Калории: {profile['target_kcal']} ккал/день")
    if profile.get("allergies"):
        lines.append(f"Аллергии: {', '.join(profile['allergies'])}")
    if profile.get("dislikes"):
        lines.append(f"Не ест: {', '.join(profile['dislikes'])}")
    if facts:
        lines.append("\n📝 <b>Что я помню:</b>")
        for f in facts[:10]:
            lines.append(f"• {f['text']}")
    if len(lines) == 1:
        lines.append("(пока ничего не знаю о тебе)")
    await message.answer("\n".join(lines))

@dp.message(Command("forget"))
async def cmd_forget(message: Message):
    if storage:
        await storage.clear_dialog(str(message.from_user.id))
    await message.answer("История диалога очищена. Факты о тебе сохранены.")

@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    if storage:
        await storage.full_reset(str(message.from_user.id))
    await message.answer("Полный сброс. Начинаем с чистого листа.")

# ─── Воронка: колбэки и тестовая оплата ──────────────────────────────────────

@dp.callback_query(F.data == "show_tariffs")
async def cb_show_tariffs(callback: CallbackQuery):
    await send_tariff_card(callback.message.chat.id)
    await callback.answer()

@dp.callback_query(F.data.startswith("tier:"))
async def cb_choose_tier(callback: CallbackQuery):
    tier = callback.data.split(":")[1]
    if tier not in TIERS:
        await callback.answer("Неизвестный тариф")
        return
    uid = str(callback.from_user.id)
    link = await generate_payment_link(uid, tier)
    title = TIERS[tier]["title"]
    if link.startswith("http"):
        # Когда подключим Продамус — ссылка станет настоящей и уйдёт кнопкой
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=link)]
        ])
        await callback.message.answer(f"Оплата: <b>{title}</b> 👇", reply_markup=kb)
    else:
        await callback.message.answer(f"💳 <b>{title}</b>\n\n{link}")
    await callback.answer()

@dp.message(Command("testpay"))
async def cmd_testpay(message: Message):
    """Тестовая заглушка оплаты: /testpay 1m|3m|6m. Только для админа.
    Будет удалена после подключения вебхука Продамуса."""
    if not storage:
        return
    if not ADMIN_USER_ID or str(message.from_user.id) != ADMIN_USER_ID:
        return
    parts = message.text.split()
    tier = parts[1] if len(parts) > 1 else "1m"
    if tier not in TIERS:
        await message.answer("Тариф: 1m, 3m или 6m")
        return
    await mark_paid(
        str(message.from_user.id), tier,
        order_id=f"TEST_{secrets.token_hex(3)}",
    )

# ─── Рассылки (админ) ─────────────────────────────────────────────────────────

async def _run_broadcast(user_ids: list[str], text: str, admin_chat_id: int):
    """Шлёт текст по списку user_id с паузой между сообщениями (см. BROADCAST_DELAY),
    чтобы не словить ограничение Telegram на массовую рассылку. Работает в фоне —
    не блокирует бота на время долгой рассылки (при 1-2к получателей это минуты)."""
    sent = failed = blocked_count = 0
    for uid in user_ids:
        try:
            await bot.send_message(int(uid), text)
            sent += 1
        except TelegramForbiddenError:
            blocked_count += 1
            await storage.set_blocked(uid, True)
        except TelegramRetryAfter as e:
            log.warning("Рассылка: flood control, ждём %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(int(uid), text)
                sent += 1
            except Exception:
                failed += 1
                log.exception("Рассылка: не удалось отправить (после retry) user_id=%s", uid)
        except Exception:
            failed += 1
            log.exception("Рассылка: не удалось отправить user_id=%s", uid)
        await asyncio.sleep(BROADCAST_DELAY)

    try:
        await bot.send_message(
            admin_chat_id,
            f"📨 Рассылка завершена.\n"
            f"Отправлено: {sent}\n"
            f"Заблокировали бота: {blocked_count}\n"
            f"Ошибок: {failed}",
        )
    except Exception:
        log.exception("Не удалось отправить итог рассылки админу")

@dp.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """Рассылка по сегменту: /broadcast сегмент текст сообщения.
    Сегменты: all (все), paid (активная подписка), unpaid (не платили),
    expired (истёкшая подписка). Только для админа. Заблокировавших бота
    юзеров пропускает автоматически. Отправка идёт в фоне с паузами."""
    if not storage:
        return
    if not ADMIN_USER_ID or str(message.from_user.id) != ADMIN_USER_ID:
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or parts[1] not in BROADCAST_SEGMENTS:
        await message.answer(
            "Использование: /broadcast сегмент текст\n\n"
            "Сегменты:\n"
            "all — все зарегистрированные\n"
            "paid — активная подписка\n"
            "unpaid — ни разу не платили\n"
            "expired — подписка истекла"
        )
        return
    segment, text = parts[1], parts[2]
    user_ids = await storage.users_in_segment(BROADCAST_SEGMENTS[segment])
    if not user_ids:
        await message.answer("В этом сегменте сейчас никого нет.")
        return
    await message.answer(f"🚀 Рассылка запущена: {len(user_ids)} получателей ({segment}).")
    asyncio.create_task(_run_broadcast(user_ids, text, message.chat.id))

# ─── Меню рецептов ────────────────────────────────────────────────────────────

@dp.message(F.text == "🍽 Рецепты")
async def show_categories(message: Message):
    if not await has_access(str(message.from_user.id)):
        await send_tariff_card(message.chat.id)
        return
    await message.answer("📂 <b>Выбери категорию:</b>", reply_markup=categories_keyboard())

@dp.callback_query(F.data.startswith("cat:"))
async def show_subcategories(callback: CallbackQuery):
    if not await has_access(str(callback.from_user.id)):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    cat_idx = int(callback.data.split(":")[1])
    cat = CATEGORIES[cat_idx]
    await callback.message.edit_text(
        f"📁 <b>{cat}</b>\n\nВыбери подкатегорию:",
        reply_markup=subcategories_keyboard(cat_idx),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("sub:"))
async def show_recipes(callback: CallbackQuery):
    if not await has_access(str(callback.from_user.id)):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    _, cat_idx, sub_idx = callback.data.split(":")
    cat_idx, sub_idx = int(cat_idx), int(sub_idx)
    cat = CATEGORIES[cat_idx]
    sub = STRUCTURE[cat][sub_idx]
    await callback.message.edit_text(
        f"📋 <b>{sub}</b>\n\nВыбери рецепт:",
        reply_markup=recipes_keyboard(cat_idx, sub_idx),
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("rec:"))
async def show_recipe(callback: CallbackQuery):
    if not await has_access(str(callback.from_user.id)):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    recipe_id = callback.data[4:]
    recipe = RECIPES.get(recipe_id)
    if not recipe:
        await callback.answer("Рецепт не найден")
        return
    cat_idx, sub_idx = find_cat_sub_for_recipe(recipe)
    text = format_recipe(recipe)
    try:
        await callback.message.delete()
    except Exception:
        pass

    photo_path = get_photo_path(recipe_id)
    log.info(
        "Фото рецепта %s: PHOTOS_DIR=%s путь=%s exists=%s",
        recipe_id, PHOTOS_DIR,
        photo_path or os.path.join(PHOTOS_DIR, f"{recipe_id}.jpg"),
        bool(photo_path and os.path.exists(photo_path)),
    )
    photo_sent_with_caption = False
    if photo_path:
        try:
            if len(text) <= 1024:
                # влезает в лимит подписи Telegram — шлём одним сообщением
                await callback.message.answer_photo(
                    FSInputFile(photo_path),
                    caption=text,
                    reply_markup=recipe_keyboard(cat_idx, sub_idx),
                    protect_content=True,
                )
                photo_sent_with_caption = True
            else:
                await callback.message.answer_photo(
                    FSInputFile(photo_path),
                    protect_content=True,
                )
        except Exception:
            # не глотаем молча: фото не ушло — логируем и шлём хотя бы текст
            log.exception("Не удалось отправить фото %s (%s)", recipe_id, photo_path)

    if not photo_sent_with_caption:
        await callback.message.answer(
            text,
            reply_markup=recipe_keyboard(cat_idx, sub_idx),
            protect_content=True,
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("back:"))
async def handle_back(callback: CallbackQuery):
    if not await has_access(str(callback.from_user.id)):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    parts = callback.data.split(":")
    if parts[1] == "main":
        await callback.message.edit_text("📂 <b>Выбери категорию:</b>", reply_markup=categories_keyboard())
    elif parts[1] == "cat":
        cat_idx = int(parts[2])
        cat = CATEGORIES[cat_idx]
        await callback.message.edit_text(
            f"📁 <b>{cat}</b>\n\nВыбери подкатегорию:",
            reply_markup=subcategories_keyboard(cat_idx),
        )
    elif parts[1] == "sub":
        cat_idx, sub_idx = int(parts[2]), int(parts[3])
        cat = CATEGORIES[cat_idx]
        sub = STRUCTURE[cat][sub_idx]
        try:
            await callback.message.edit_text(
                f"📋 <b>{sub}</b>\n\nВыбери рецепт:",
                reply_markup=recipes_keyboard(cat_idx, sub_idx),
            )
        except Exception:
            await callback.message.answer(
                f"📋 <b>{sub}</b>\n\nВыбери рецепт:",
                reply_markup=recipes_keyboard(cat_idx, sub_idx),
            )
    await callback.answer()

# ─── МарИИя ───────────────────────────────────────────────────────────────────

@dp.message(F.text == "🤖 Спросить МарИИю")
async def mariya_intro(message: Message):
    if not await has_access(str(message.from_user.id)):
        await send_tariff_card(message.chat.id)
        return
    await message.answer(
        "🤖 <b>МарИИя — AI-нутрициолог</b>\n\n"
        "Я помогу:\n"
        "• Посчитать твою норму КБЖУ\n"
        "• Составить рацион по твоим целям\n"
        "• Подобрать рецепты под аллергии и предпочтения\n\n"
        "Просто напиши что тебе нужно 👇\n\n"
        "<i>Команды: /profile — профиль | /forget — очистить диалог | /reset — полный сброс</i>"
    )

@dp.message(F.text)
async def handle_text(message: Message):
    if not mariya or not storage:
        await message.answer("Ассистент временно недоступен.")
        return

    uid = str(message.from_user.id)
    await storage.set_blocked(uid, False)  # написал нам — точно не заблокирован

    if not await has_access(uid):
        await send_tariff_card(message.chat.id)
        return

    user_text = message.text.strip()

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    client = await storage.get_client(uid)
    history = await storage.get_dialog(uid, limit=40)

    reply = await mariya.chat(user_text, client, history)

    await storage.add_dialog(uid, "user", user_text)
    await storage.add_dialog(uid, "assistant", reply)

    await message.answer(reply, parse_mode=None)

    # Фоновое обучение
    existing_facts = [f["text"] for f in client.get("facts", [])]
    new_facts = await mariya.extract_facts(user_text, reply, existing_facts)
    if new_facts:
        await storage.add_facts(uid, new_facts)

# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    global storage, mariya, sheets_client
    storage = Storage(DB_PATH)
    await storage.init()
    mariya = Mariya(
        anthropic_key=ANTHROPIC_API_KEY,
        recipes_data=data,
        model=MODEL,
    )

    if GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID:
        try:
            sheets_client = SheetsClient(GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_SHEET_ID)
            log.info("Google Sheets интеграция включена (sheet_id=%s)", GOOGLE_SHEET_ID)
        except Exception:
            log.exception("Не удалось инициализировать Google Sheets — бот работает без него")
            sheets_client = None
    else:
        log.info("Google Sheets интеграция выключена (нет GOOGLE_SERVICE_ACCOUNT_JSON/GOOGLE_SHEET_ID)")

    log.info("Бот запущен")
    await asyncio.gather(
        run_polling_safe(),
        funnel_worker(),
        prodamus_webhook_server(),
        sheets_sync_worker(),
    )

if __name__ == "__main__":
    asyncio.run(main())
