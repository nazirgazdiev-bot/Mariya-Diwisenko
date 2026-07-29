import asyncio
import html
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
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
LEARN_MODEL = os.environ.get("LEARN_MODEL", "claude-haiku-4-5-20251001")
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID")  # legacy: один администратор
ADMIN_USER_IDS = {
    user_id.strip()
    for user_id in os.environ.get("ADMIN_USER_IDS", ADMIN_USER_ID or "").split(",")
    if user_id.strip()
}

def user_is_admin(user_id: int | str) -> bool:
    return str(user_id) in ADMIN_USER_IDS
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
            [KeyboardButton(text="🍽 Рецепты"), KeyboardButton(text="⭐ Избранное")],
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
            # Передаём раздел, из которого открыт рецепт, чтобы кнопка «Назад»
            # возвращала именно в текущий список. Это важно для рецептов,
            # которые одновременно находятся в нескольких подкатегориях.
            buttons.append([
                InlineKeyboardButton(
                    text=name,
                    callback_data=f"rec:{rid}:{cat_idx}:{sub_idx}",
                )
            ])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"back:cat:{cat_idx}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def recipe_keyboard(cat_idx: int, sub_idx: int, recipe_id: str | None = None, is_fav: bool = False):
    rows = []
    if recipe_id is not None:
        rows.append([InlineKeyboardButton(
            text="💛 В избранном" if is_fav else "⭐ В избранное",
            callback_data=f"fav:{'del' if is_fav else 'add'}:{recipe_id}",
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад к списку", callback_data=f"back:sub:{cat_idx}:{sub_idx}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

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
    def display_number(value) -> str:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value).replace(".", ",")

    def kbju_line(values: dict) -> str:
        parts = []
        if "kcal" in values:
            parts.append(f"🔥 {display_number(values['kcal'])} ккал")
        else:
            parts.append("🔥 калорийность не указана")
        parts.append(
            f"🥩 Б: {display_number(values['protein'])}г  |  "
            f"🧈 Ж: {display_number(values['fat'])}г  |  "
            f"🍞 У: {display_number(values['carbs'])}г"
        )
        return "  |  ".join(parts)

    name = html.escape(recipe.get("name", ""))
    ingredients = recipe.get("ingredients", [])
    ingredient_lines = []
    for ingredient in ingredients:
        ingredient = html.escape(ingredient)
        if ingredient.endswith(":"):
            ingredient_lines.append(f"\n<b>{ingredient}</b>")
        else:
            ingredient_lines.append(f"• {ingredient}")
    ingredients_text = "\n".join(ingredient_lines).lstrip()
    instructions = (
        recipe.get("instructions", "")
        .replace("ПРИГОТОВЛЕНИЕ", "")
        .strip()
    )
    if len(instructions) > 2000:
        instructions = instructions[:1997] + "..."
    instructions = html.escape(instructions)

    text = f"<b>🍳 {name}</b>\n\n"
    variants = recipe.get("kbju_variants", [])
    if variants:
        text += "📊 <b>КБЖУ:</b>\n"
        for variant in variants:
            text += f"<b>{html.escape(variant['label'])}:</b>\n"
            text += f"{kbju_line(variant)}\n"
        text += "\n"
    elif all(field in recipe for field in ("protein", "fat", "carbs")):
        label = html.escape(recipe.get("kbju_label", "На 100 г"))
        text += f"📊 <b>КБЖУ — {label}:</b>\n"
        text += f"{kbju_line(recipe)}\n\n"
    else:
        text += "📊 <b>КБЖУ в сборнике не указано</b>\n\n"

    if ingredients_text:
        text += f"🛒 <b>Ингредиенты:</b>\n{ingredients_text}\n\n"
    text += f"👨‍🍳 <b>Приготовление:</b>\n{instructions}"
    return text

def find_cat_sub_for_recipe(recipe: dict):
    """Возвращает первый фактический путь рецепта из структуры меню."""
    menu_paths = recipe.get("menu_paths", [])
    if menu_paths:
        cat_name, separator, sub_name = menu_paths[0].partition("/")
        if separator and cat_name in CATEGORIES:
            cat_idx = CATEGORIES.index(cat_name)
            subcats = STRUCTURE.get(cat_name, [])
            if sub_name in subcats:
                return cat_idx, subcats.index(sub_name)

    # Фолбэк для старых/неполных записей: ищем рецепт непосредственно в MENU.
    recipe_id = recipe.get("id")
    for cat_idx, cat_name in enumerate(CATEGORIES):
        for sub_idx, sub_name in enumerate(STRUCTURE.get(cat_name, [])):
            if recipe_id in MENU.get(cat_name, {}).get(sub_name, []):
                return cat_idx, sub_idx
    return 0, 0

# ─── Воронка продаж: тексты, тарифы, клавиатуры ──────────────────────────────

FUNNEL_CHECK_INTERVAL = 25          # секунд между проверками фоновой задачи

# Тестовый режим воронки: включается переменной окружения FUNNEL_TEST_MODE=1
# на Railway (без правки кода). Схлопывает все задержки между дожимами до
# FUNNEL_TEST_INTERVAL_SEC секунд (по умолчанию 300 = 5 минут), чтобы можно
# было быстро прогнать всю цепочку глазами. НЕ ЗАБЫТЬ ВЫКЛЮЧИТЬ после теста —
# иначе в проде дожимы будут лететь раз в 5 минут вместо реальных дней.
FUNNEL_TEST_MODE = os.environ.get("FUNNEL_TEST_MODE", "0") == "1"
FUNNEL_TEST_INTERVAL = int(os.environ.get("FUNNEL_TEST_INTERVAL_SEC", "300"))

# Отдельный тест ПОСТ-ОПЛАТНОЙ серии (продление/win-back), НЕЗАВИСИМ от
# FUNNEL_TEST_MODE: если RENEWAL_TEST_INTERVAL_SEC > 0, то все 5 этапов
# продления идут подряд с этим интервалом (в секундах), игнорируя реальные
# день/час из Miro. 0 = боевой режим (реальные даты от paid_until).
# Заодно этот режим открывает /testpay всем (чтобы Назир мог сам выдать
# себе доступ и посмотреть рецепты/МарИИю и серию после оплаты).
RENEWAL_TEST_INTERVAL = int(os.environ.get("RENEWAL_TEST_INTERVAL_SEC", "0"))
# Открывает команду /testpay ВСЕМ (а не только админу) на время теста —
# чтобы Назир и Мария могли сами выдать себе доступ. ОБЯЗАТЕЛЬНО выключить
# (TESTPAY_OPEN=0 или удалить) перед боевым запуском с реальным ботом.
TESTPAY_OPEN = os.environ.get("TESTPAY_OPEN", "0") == "1"

def dbg_delay(real_delay):
    """В тестовом режиме подменяет реальную задержку на FUNNEL_TEST_INTERVAL."""
    return FUNNEL_TEST_INTERVAL if FUNNEL_TEST_MODE else real_delay

TIERS = {
    "1m": {"title": "1 месяц — 1290₽", "label": "1 месяц", "days": 30, "price": 1290},
    "3m": {"title": "3 месяца — 3490₽ (экономия 380₽)", "label": "3 месяца", "days": 90, "price": 3490},
    "6m": {"title": "6 месяцев — 5990₽ (экономия 1750₽)", "label": "6 месяцев", "days": 180, "price": 5990},
}

# Сегменты для /broadcast: ключ команды -> бизнес-сегмент в Storage.
BROADCAST_SEGMENTS = {
    "all": "all",
    "paid": "active",
    "unpaid": "trial",
    "expired": "expired",
    "firstpaid": "firstpaid",
    "renewed": "renewed",
    "notrenewed": "notrenewed",
}
BROADCAST_DELAY = 0.05  # ~20 сообщений/сек — с запасом от лимита Telegram (~30/сек)

WELCOME_FUNNEL_TEXT = (
    "<b>Привет! 💜 Это Мария — рада видеть тебя здесь.</b>\n\n"
    "Ты зашла в моего бота с рецептами и личным ИИ-ассистентом. "
    "Тут собрано всё, чем я сама пользуюсь каждый день:\n\n"
    "250+ моих фирменных ПП-рецептов и МарИИя — умный помощник, который считает КБЖУ "
    "и собирает рационы под тебя из моего сборника рецептов\n\n"
    "<b>Сейчас за пару минут покажу, как это работает 👋</b>"
)

STEP1_VIDEO_TEXT = (
    "Смотри коротенькое видео — за 2 минуты покажу тебе всё: как искать рецепты, "
    "как работает МарИИя и как всё это будет экономить тебе кучу времени и нервов "
    "каждый день 👋"
)

TARIFF_CARD_TEXT = (
    "<b>🔥 Что ты получишь внутри бота:</b>\n\n"
    "🍽 250+ моих фирменных ПП-рецептов — разбиты по категориям: завтраки, мясо, "
    "десерты и другое. Захотела — открыла — приготовила. "
    "(рецепты будут пополняться постоянно)\n\n"
    "🤖 МарИИя — мой личный ИИ-ассистент. Считает твоё КБЖУ по моему методу "
    "и собирает тебе готовый рацион на день, неделю или месяц. Под твои цели, "
    "вкусы и даже аллергии. И всё это — только из моих проверенных рецептов.\n\n"
    "Это как иметь меня в кармане в режиме 24/7 🤍\n\n"
    "<b>Для тех, кто уже покупал мои продукты — специальные цены 🤍</b>\n\n"
    "<b>Выбирай доступ и погнали 👇</b>\n"
    "▪️ 1 месяц — 1290 ₽\n"
    "▪️ 3 месяца — 3490 ₽ (экономия 380 ₽)\n"
    "▪️ 6 месяцев — 5990 ₽ (экономия 1750 ₽)"
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

DOZHIM_4_TEXT = (
    "<b>Давай честно про деньги 💬</b>\n\n"
    "Знаю, что многих останавливает мысль: «а вдруг куплю и не буду пользоваться, "
    "как с теми марафонами».\n\n"
    "Я тебя понимаю. Поэтому скажу прямо: это не курс, который надо проходить, "
    "и не марафон, где нужно успевать. Это инструмент, который просто всегда под рукой.\n\n"
    "Захотела рецепт — открыла. Нужно меню — спросила МарИИю. Никаких дедлайнов "
    "и чувства вины, что ты что-то не успела.\n\n"
    "1290 ₽ в месяц — это меньше, чем ты тратишь на спонтанные продукты, которые "
    "покупаешь, не зная что готовить, и которые потом портятся в холодильнике.\n\n"
    "<b>Попробуй, не понравится — верну деньги 👇</b>"
)

DOZHIM_5_TEXT = (
    "Знаю, о чём некоторые думают:\n\n"
    "«да этот бот выдаст мне овсянку на воде, которую я есть не буду» 😄\n\n"
    "Расслабься — МарИИя гибкая.\n\n"
    "Не любишь рыбу? Скажи — заменит. Аллергия на молочку? Учтёт. "
    "Хочешь сегодня что-то сладкое на завтрак? Впишет.\n\n"
    "Ты говоришь, что любишь и что не хочешь — а она подстраивает меню под ТЕБЯ. "
    "Мой бот — это не жёсткий план из которого хочется сбежать и сорваться. "
    "Это питание под твои желания.\n\n"
    "<b>Проверь сама по самым лучшим условиям сейчас 👇</b>"
)

DOZHIM_6_TEXT = (
    "<b>Пока ты думаешь — девочки уже вовсю готовят по боту 👆</b>\n\n"
    "Самое приятное для меня — что многие пишут одно и то же: «наконец-то я "
    "перестала мучиться вопросом что приготовить, а вес начал уходить».\n\n"
    "Ровно для этого всё и создавалось!\n\n"
    "<b>Присоединяйся к нам, и готовь без гемора 😅</b>"
)

DOZHIM_7_TEXT = (
    "<b>Последнее напоминание от меня 💔</b>\n\n"
    "Если ты дочитала до сюда — значит, тема питания тебя правда волнует. "
    "И ты уже устала от этого хаоса: то диета, то срыв, то «с понедельника».\n\n"
    "Бот — это способ навести порядок в еде раз и навсегда. Без весов, без "
    "мучений, без «что приготовить». Всё уже собрано и продумано за тебя.\n\n"
    "Сегодня — напоминаю про вступление последний раз, далее бот отключается 💔\n\n"
    "<b>Погнали кушать вкусно и легко 👇</b>"
)

PAID_TEXT = (
    "<b>Красотка, ты в деле! 🎉 Доступ открыт.</b>\n\n"
    "С чего советую начать:\n\n"
    "1️⃣ Загляни в «Рецепты» — полистай категории, сохрани в избранное что приглянулось\n\n"
    "2️⃣ Нажми «Спросить МарИИю» и попроси собрать тебе рацион на день/неделю — "
    "просто напиши свою цель (любимые продукты, на что аллергия, свою цель и КБЖУ)\n\n"
    "<i>P.s. если не знаешь КБЖУ МарИИя также сможет рассчитать тебе его, просто напиши "
    "свой рост/вес, цель (похудение, набор) и желаемый вес, МарИИя высчитает КБЖУ "
    "под тебя по моей методике!</i>\n\n"
    "<b>Пользуйся в удовольствие. Я вложила сюда всю свою систему питания — "
    "теперь она твоя 🤍</b>"
)

# ─── Воронка продления (после оплаты) — сверено с Miro 2026-07-15 ────────────
# Расписание считается от paid_until (см. renewal_target_msk), не от текущего
# момента — переживает рестарты бота и правильно работает даже если сервис
# не поднимался в момент, когда должно было прийти напоминание.

RENEWAL_3D_TEXT = (
    "<b>Привет! 🤍 Напоминаю: твой доступ к боту заканчивается через 3 дня</b>\n\n"
    "За это время МарИИя наверняка стала твоим помощником — считала КБЖУ, "
    "собирала меню, избавляла от вечного «что готовить».\n\n"
    "<b>Чтобы ничего из этого не потерять — продли доступ заранее 👇</b>\n\n"
    "▪️ 1 месяц — 1290₽\n"
    "▪️ 3 месяца — 3490₽ (экономия 380₽)\n"
    "▪️ 6 месяцев — 5990₽ (экономия 1750₽)"
)

RENEWAL_1D_TEXT = (
    "<b>Твой доступ закрывается уже завтра ⏳</b>\n\n"
    "Совсем скоро рецепты и МарИИя станут недоступны. А значит — снова считать "
    "самой, снова ломать голову над меню, снова этот вопрос «что бы приготовить».\n\n"
    "<b>Не возвращайся к хаосу — продли за пару секунд 👇</b>"
)

RENEWAL_DAYOF_TEXT = (
    "<b>Сегодня последний день твоего доступа 🤍</b>\n\n"
    "После полуночи бот закроется. Если хочешь и дальше готовить по моим "
    "рецептам и держать питание под контролем с МарИИей — самое время продлить.\n\n"
    "<b>Одно нажатие — и остаёшься с помощником 👇</b>"
)

RENEWAL_1H_TEXT = (
    "<b>⏰ Остался час.</b>\n\n"
    "Через час доступ закроется, и МарИИя попрощается с тобой. Если не "
    "успеешь продлить — придётся начинать оплату заново позже.\n\n"
    "<b>Сохрани доступ прямо сейчас, и перестань париться о том что приготовить, "
    "это займёт 10 секунд 👇</b>"
)

RENEWAL_WINBACK_TEXT = (
    "<b>Скучаешь по МарИИе?</b>\n\n"
    "🤍Твой доступ закрылся пару дней назад. Если поймала себя на том, что "
    "снова не знаешь что приготовить и считаешь всё вручную — возвращайся, "
    "я всегда тебе рада.\n\n"
    "Если все таки решишься вернуться — у меня для тебя небольшой подарок 🎁\n\n"
    "При возобновлении подписки на бота — тренировочный комплекс для зала "
    "в подарок)\n\n"
    "<b>Вернуться к рецептам и МарИИе и получить бонус 👇</b>"
)

NEW_TARIFF_TEXT_TMPL = (
    "<b>🤍 {name}, выбери тариф для оформления подписки:</b>\n\n"
    "<b>Для тех, кто уже покупал мои продукты — специальные цены:</b>\n\n"
    "▪️ 1 месяц — 1290₽\n"
    "▪️ 3 месяца — 3490₽ (экономия 380₽)\n"
    "▪️ 6 месяцев — 5990₽ (экономия 1750₽)"
)

RENEWAL_TARIFF_TEXT_TMPL = (
    "<b>🤍 {name}, выбери тариф для продления подписки:</b>\n\n"
    "▪️ 1 месяц — 1290₽\n"
    "▪️ 3 месяца — 3490₽ (экономия 380₽)\n"
    "▪️ 6 месяцев — 5990₽ (экономия 1750₽)"
)

# Этап 1: за 3 дня в 12:00 МСК | 2: за 1 день в 12:00 МСК |
# 3: в день списания в 10:00 МСК | 4: за 1 час до отключения (23:00 МСК того
# же дня — ровно за час до полуночного отключения) | 5: win-back через 2 дня
# после окончания подписки в 12:00 МСК.
RENEWAL_STAGE_TEXTS = {
    1: RENEWAL_3D_TEXT,
    2: RENEWAL_1D_TEXT,
    3: RENEWAL_DAYOF_TEXT,
    4: RENEWAL_1H_TEXT,
    5: RENEWAL_WINBACK_TEXT,
}
RENEWAL_STAGE_PHOTOS = {
    # Карточка с ценами временно отключена: на ней остались старые тарифы.
    1: None,
    2: "renewal_1day.png",
    3: "renewal_dayof.png",
    4: None,  # в Miro у этой карточки нет картинки — только текст
    5: "renewal_winback.png",
}


def tariffs_keyboard():
    """Кнопки выбора тарифа — по просьбе Кирилла 2026-07-16 БЕЗ цены/выгоды
    в самой кнопке (это идёт только в тексте сообщения над кнопками)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=TIERS[t]["label"], callback_data=f"tier:{t}")]
        for t in ("1m", "3m", "6m")
    ])

def pay_cta_keyboard(text: str):
    """Кнопка дожимов (3-9) и кнопка самой TARIFF_CARD_TEXT — ведёт СРАЗУ на
    карточку выбора тарифа для НОВОЙ подписки ("🤍 Имя, выбери тариф для
    оформления подписки"), минуя повторный показ TARIFF_CARD_TEXT."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="show_new_tariffs")]
    ])

def intro_cta_keyboard(text: str):
    """Кнопка ТОЛЬКО у шага 1 (видео-текст) — ведёт на TARIFF_CARD_TEXT
    (шаг 2, "🔥 Что ты получишь внутри бота"), а не сразу на тарифы."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data="show_tariffs")]
    ])


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def iso_in(seconds: float) -> str:
    return (now_utc() + timedelta(seconds=seconds)).isoformat()

MSK = timezone(timedelta(hours=3))

def seconds_until_msk(days_ahead: int, hour: int, minute: int = 0) -> float:
    """Секунд от текущего момента до `hour:minute` по МСК через `days_ahead`
    календарных дней (от сегодняшней даты по МСК). Нужно для гибридной
    относительно-абсолютной схемы дожимов из Miro (день N в HH:MM МСК),
    которая идёт после первых трёх дожимов на чистых относительных задержках."""
    now_msk = now_utc().astimezone(MSK)
    target_date = (now_msk + timedelta(days=days_ahead)).date()
    target = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, tzinfo=MSK,
    )
    return max((target - now_msk).total_seconds(), 60.0)


def renewal_target_msk(paid_until_iso: str, stage: int) -> datetime | None:
    """Абсолютное время (МСК) очередного этапа воронки продления, считая от
    даты окончания подписки paid_until. Стадии 1-5 — см. RENEWAL_STAGE_TEXTS."""
    paid_dt = datetime.fromisoformat(paid_until_iso)
    if paid_dt.tzinfo is None:
        paid_dt = paid_dt.replace(tzinfo=timezone.utc)
    base_date = paid_dt.astimezone(MSK).date()
    if stage == 1:
        d, h, m = base_date - timedelta(days=3), 12, 0
    elif stage == 2:
        d, h, m = base_date - timedelta(days=1), 12, 0
    elif stage == 3:
        d, h, m = base_date, 10, 0
    elif stage == 4:
        d, h, m = base_date, 23, 0
    elif stage == 5:
        d, h, m = base_date + timedelta(days=2), 12, 0
    else:
        return None
    return datetime(d.year, d.month, d.day, h, m, tzinfo=MSK)


def next_renewal_schedule(paid_until_iso: str, from_stage: int) -> tuple[int, str] | None:
    """Ищет ближайший ещё не наступивший этап продления начиная с from_stage.
    Пропускает этапы, чьё время уже прошло (короткий тариф, тестовая оплата
    через /testpay и т.п.), чтобы не заваливать пользователя просроченными
    напоминаниями. None — этапы закончились (после win-back)."""
    now = now_utc()
    if RENEWAL_TEST_INTERVAL > 0 and from_stage <= 5:
        # Тест пост-оплатной серии: игнорируем реальные день/час из Miro,
        # следующий этап через RENEWAL_TEST_INTERVAL секунд.
        return from_stage, (now + timedelta(seconds=RENEWAL_TEST_INTERVAL)).isoformat()
    for stage in range(from_stage, 6):
        target_msk = renewal_target_msk(paid_until_iso, stage)
        if target_msk is None:
            continue
        target_utc = target_msk.astimezone(timezone.utc)
        if target_utc > now:
            return stage, target_utc.isoformat()
    return None


def renewal_cta_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Продлить подписку ✅", callback_data="show_renewal_tariffs")]
    ])


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


async def clear_open_recipe(user_id: str, chat_id: int):
    """Удаляет все сообщения текущей карточки рецепта, включая отдельное фото."""
    if not storage:
        return
    state = await storage.get_ui_state(user_id)
    for message_id in state["recipe_message_ids"]:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            # Сообщение уже могло быть удалено кнопкой «Назад» или самим юзером.
            pass
    if state["recipe_message_ids"]:
        await storage.set_recipe_message_ids(user_id, [])


async def clear_open_menu(user_id: str, chat_id: int):
    """Удаляет текущее inline-меню: категории, подкатегории или список рецептов."""
    if not storage:
        return
    state = await storage.get_ui_state(user_id)
    for message_id in state["menu_message_ids"]:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    if state["menu_message_ids"]:
        await storage.set_menu_message_ids(user_id, [])


async def enter_recipe_mode(user_id: str, chat_id: int):
    """Закрывает МарИИю и убирает предыдущую карточку или inline-меню."""
    if not storage:
        return
    await storage.set_mariya_mode(user_id, False)
    await clear_open_recipe(user_id, chat_id)
    await clear_open_menu(user_id, chat_id)


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
        snapshot = await storage.report_snapshot()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, sheets_client.write_dashboard_snapshot, metrics, snapshot
        )
    except Exception:
        log.exception("Не удалось обновить дашборд Google Sheets")


def _to_float(value) -> float | None:
    """Продамус присылает числа строками ('1290.00' и т.п.) — безопасный парсинг."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

# ─── Внутренняя диагностика для логов (не публикуется через HTTP) ─────────────
_diag = {
    "polling_attempts": 0,
    "last_error": None,
    "last_error_at": None,
    "last_started_at": None,
    "prodamus_demo_mode": None,  # выставится после чтения PRODAMUS_DEMO_MODE ниже
    "last_webhook_at": None,
    "last_webhook_sig_valid": None,
    "last_webhook_order_num": None,
    "last_webhook_status": None,
    "last_webhook_result": None,
    "last_payment_link_order_id": None,
    "last_funnel_error": None,
    "last_funnel_error_step": None,
    "last_funnel_error_at": None,
    "last_funnel_step_sent": None,
    "funnel_test_mode": None,  # выставится ниже, после чтения FUNNEL_TEST_MODE
}
_diag["funnel_test_mode"] = FUNNEL_TEST_MODE
_diag["funnel_test_interval_sec"] = FUNNEL_TEST_INTERVAL if FUNNEL_TEST_MODE else None
_diag["renewal_test_interval_sec"] = RENEWAL_TEST_INTERVAL if RENEWAL_TEST_INTERVAL > 0 else None
_diag["testpay_open"] = TESTPAY_OPEN


# ─── Воронка продаж: логика ───────────────────────────────────────────────────

async def has_access(user_id: str) -> bool:
    """Активна ли подписка. Просроченную active сразу переводит в expired."""
    # Администраторы имеют постоянный доступ без покупки и paid_until.
    if user_is_admin(user_id):
        return True
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

async def send_welcome(chat_id: int):
    """Приветственное сообщение — первое, что видит юзер. Если есть фото-обложка
    (photos/step1_intro.jpg), крепим его сюда с подписью вместо отдельного фото
    и отдельного текста."""
    welcome_photo = os.path.join(PHOTOS_DIR, "step1_intro.jpg")
    if os.path.exists(welcome_photo):
        await bot.send_photo(
            chat_id, FSInputFile(welcome_photo),
            caption=WELCOME_FUNNEL_TEXT, reply_markup=main_keyboard(),
        )
    else:
        await bot.send_message(chat_id, WELCOME_FUNNEL_TEXT, reply_markup=main_keyboard())

async def send_tariff_card(chat_id: int):
    """Шаг 2 воронки — маркетинговая карточка '🔥 Что ты получишь внутри
    бота' + tariff_card.png. Кнопка на ней ведёт на карточку выбора тарифа
    (show_new_tariffs), а не на оплату напрямую."""
    tariff_photo = os.path.join(PHOTOS_DIR, "tariff_card.png")
    kb = pay_cta_keyboard("Оплатить доступ ✅")
    if os.path.exists(tariff_photo):
        await bot.send_photo(chat_id, FSInputFile(tariff_photo), caption=TARIFF_CARD_TEXT, reply_markup=kb)
    else:
        await bot.send_message(chat_id, TARIFF_CARD_TEXT, reply_markup=kb)

async def send_new_tariff_card(chat_id: int, name: str | None):
    """Выбор тарифа для новой подписки.

    Временно отправляется без старой карточки с ценами: актуальные спеццены
    находятся в тексте и в кнопках оплаты.
    """
    text = NEW_TARIFF_TEXT_TMPL.format(name=html.escape(name or "Привет"))
    await bot.send_message(chat_id, text, reply_markup=tariffs_keyboard())

async def send_renewal_tariff_card(chat_id: int, name: str | None):
    text = RENEWAL_TARIFF_TEXT_TMPL.format(name=html.escape(name or "Привет"))
    # Фото-карточка временно отключена, потому что на ней старые цены.
    await bot.send_message(chat_id, text, reply_markup=tariffs_keyboard())

async def send_renewal_stage(chat_id: int, stage: int):
    text = RENEWAL_STAGE_TEXTS[stage]
    photo_name = RENEWAL_STAGE_PHOTOS.get(stage)
    kb = renewal_cta_keyboard()
    if photo_name:
        photo_path = os.path.join(PHOTOS_DIR, photo_name)
        if os.path.exists(photo_path):
            await bot.send_photo(chat_id, FSInputFile(photo_path), caption=text, reply_markup=kb)
            return
    await bot.send_message(chat_id, text, reply_markup=kb)

async def send_funnel_step(chat_id: int, step: int) -> tuple[int | None, float | None]:
    """Шлёт сообщение шага воронки. Возвращает (следующий шаг, задержка в сек).

    Схема сверена вручную с Miro-доской 2026-07-14 (не по памяти/скриншотам,
    а через accessibility-дерево доски — так надёжнее). Первые три дожима идут
    на чистых относительных задержках от заявки (30 мин / 1 час / 2 часа),
    дальше — гибридная схема с абсолютными день-N-в-HH:MM МСК таймкодами
    (см. seconds_until_msk)."""
    if step == 1:
        await bot.send_message(
            chat_id, STEP1_VIDEO_TEXT,
            reply_markup=intro_cta_keyboard("Перейти к оплате бота"),
        )
        return 2, 15  # тариф-карта — ЧЕРЕЗ 15 СЕКУНД
    if step == 2:
        await send_tariff_card(chat_id)
        return 3, dbg_delay(30 * 60)  # дожим 1 — через 30 минут после заявки
    if step == 3:
        dozhim1_photo = os.path.join(PHOTOS_DIR, "dozhim1.jpg")
        kb = pay_cta_keyboard("Получить доступ")
        if os.path.exists(dozhim1_photo):
            await bot.send_photo(chat_id, FSInputFile(dozhim1_photo), caption=DOZHIM_1_TEXT, reply_markup=kb)
        else:
            await bot.send_message(chat_id, DOZHIM_1_TEXT, reply_markup=kb)
        return 4, dbg_delay(30 * 60)  # дожим 2 — через 1 час после заявки (ещё +30 мин)
    if step == 4:
        # В Miro у дожима 2 нет фото — только текст (плейсхолдер "видео на
        # 10-15 сек" не реализован отдельным шагом). Раньше сюда по ошибке
        # прикреплялась медиагруппа из фото6+фото8 — убрано.
        kb = pay_cta_keyboard("Получить доступ к боту")
        await bot.send_message(chat_id, DOZHIM_2_TEXT, reply_markup=kb)
        return 5, dbg_delay(60 * 60)  # дожим 3 — через 2 часа после заявки (ещё +1 час)
    if step == 5:
        dozhim3_photo = os.path.join(PHOTOS_DIR, "dozhim3.jpg")
        kb = pay_cta_keyboard("Попробовать бота")
        if os.path.exists(dozhim3_photo):
            await bot.send_photo(chat_id, FSInputFile(dozhim3_photo), caption=DOZHIM_3_TEXT, reply_markup=kb)
        else:
            await bot.send_message(chat_id, DOZHIM_3_TEXT, reply_markup=kb)
        return 6, dbg_delay(seconds_until_msk(1, 12))  # день 2 в 12 МСК
    if step == 6:
        demo_photo = os.path.join(PHOTOS_DIR, "dozhim2_demo.png")
        kb = pay_cta_keyboard("Получить доступ")
        if os.path.exists(demo_photo):
            await bot.send_photo(chat_id, FSInputFile(demo_photo), caption=DOZHIM_5_TEXT, reply_markup=kb)
        else:
            await bot.send_message(chat_id, DOZHIM_5_TEXT, reply_markup=kb)
        return 7, dbg_delay(seconds_until_msk(0, 19))  # день 2 в 19 МСК (тот же день)
    if step == 7:
        kb = pay_cta_keyboard("Попробовать бота")
        # Старая картинка содержала цену 1690 ₽/мес, поэтому на время акции
        # отправляем актуальный текст без неё.
        await bot.send_message(chat_id, DOZHIM_4_TEXT, reply_markup=kb)
        return 8, dbg_delay(seconds_until_msk(1, 17))  # день 3 в 17 МСК
    if step == 8:
        testimonial_photo = os.path.join(PHOTOS_DIR, "dozhim2_testimonial.png")
        kb = pay_cta_keyboard("Оплатить бота")
        if os.path.exists(testimonial_photo):
            await bot.send_photo(chat_id, FSInputFile(testimonial_photo), caption=DOZHIM_6_TEXT, reply_markup=kb)
        else:
            await bot.send_message(chat_id, DOZHIM_6_TEXT, reply_markup=kb)
        return 9, dbg_delay(seconds_until_msk(1, 15))  # день 4 в 15 МСК
    if step == 9:
        last_photo = os.path.join(PHOTOS_DIR, "dozhim7_last.png")
        kb = pay_cta_keyboard("Оплатить бота")
        if os.path.exists(last_photo):
            await bot.send_photo(chat_id, FSInputFile(last_photo), caption=DOZHIM_7_TEXT, reply_markup=kb)
        else:
            await bot.send_message(chat_id, DOZHIM_7_TEXT, reply_markup=kb)
        return 10, None  # последний дожим — дальше не шлём
    return None, None

async def mark_paid(
    user_id: str,
    tier: str,
    paid_until_override: str | None = None,
    *,
    amount: float | None = None,
    commission_sum: float | None = None,
    order_id: str | None = None,
 ) -> bool:
    """Ветка "после оплаты": вызывается вебхуком Продамуса (и /testpay для теста).
    Открытие доступа в БД — критическая часть и должна прокидывать исключение
    наверх (чтобы вебхук вернул ошибку и Продамус повторил попытку).
    Отправка уведомления пользователю, запись в SQLite/Google Sheets — best-effort:
    если юзер заблокировал бота или Sheets недоступен, это не должно выглядеть
    как "оплата не прошла".
    paid_until_override — короткий срок доступа для теста (чтобы за пару минут
    увидеть полный цикл: доступ → напоминания → истечение → win-back)."""
    if order_id and await storage.payment_exists(order_id):
        log.info("Повторный вебхук оплаты проигнорирован: order_id=%s", order_id)
        return False

    days = TIERS[tier]["days"]
    if paid_until_override:
        paid_until = paid_until_override
    else:
        # При досрочном продлении оставшиеся оплаченные дни не сгорают.
        base = now_utc()
        current = await storage.get_subscription(user_id)
        current_until = (current or {}).get("paid_until")
        if current_until:
            try:
                current_dt = datetime.fromisoformat(current_until)
                if current_dt.tzinfo is None:
                    current_dt = current_dt.replace(tzinfo=timezone.utc)
                if current_dt > base:
                    base = current_dt
            except ValueError:
                log.warning(
                    "Некорректный paid_until у user_id=%s: %s",
                    user_id, current_until,
                )
        paid_until = (base + timedelta(days=days)).isoformat()

    renewal_schedule = next_renewal_schedule(paid_until, 1)
    final_amount = amount if amount is not None else TIERS[tier]["price"]
    final_commission = commission_sum or 0
    processed = await storage.activate_payment(
        user_id=user_id,
        tier=tier,
        paid_until=paid_until,
        renewal_next_at=renewal_schedule[1] if renewal_schedule else None,
        amount=final_amount,
        commission_sum=final_commission,
        order_id=order_id,
    )
    if not processed:
        log.info("Повторный платёж не записан: order_id=%s", order_id)
        return False

    log.info("Оплата: user_id=%s tier=%s до %s", user_id, tier, paid_until)
    # Журнал в Google Sheets (если подключен).
    await _sheets_log_payment(
        user_id=user_id, tier=tier, amount=final_amount,
        commission_sum=final_commission, status="success", order_id=order_id or "",
    )

    try:
        paid_photo = os.path.join(PHOTOS_DIR, "paid.png")
        if os.path.exists(paid_photo):
            await bot.send_photo(int(user_id), FSInputFile(paid_photo), caption=PAID_TEXT, reply_markup=main_keyboard())
        else:
            await bot.send_message(int(user_id), PAID_TEXT, reply_markup=main_keyboard())
    except TelegramForbiddenError:
        log.info("Оплата прошла, но юзер заблокировал бота: user_id=%s", user_id)
        await storage.set_blocked(user_id, True)
    except Exception:
        log.exception("Оплата прошла, но не удалось отправить уведомление user_id=%s", user_id)
    return True

# ─── Вебхук Продамуса ─────────────────────────────────────────────────────────

async def handle_prodamus_webhook(request: web.Request) -> web.Response:
    raw_body = await request.text()
    _diag["last_webhook_at"] = datetime.now(timezone.utc).isoformat()
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
            processed = await mark_paid(
                user_id, tier,
                amount=amount, commission_sum=commission_sum, order_id=order_num,
            )
            _diag["last_webhook_result"] = (
                "mark_paid ok" if processed else "duplicate ignored"
            )
        except Exception as e:
            log.exception("Продамус: ошибка активации user_id=%s tier=%s", user_id, tier)
            _diag["last_webhook_result"] = f"mark_paid error: {e}"
            return web.Response(status=500, text="error")
    else:
        log.info("Продамус webhook: статус %s не success или тариф %s неизвестен — игнор", payment_status, tier)
        _diag["last_webhook_result"] = f"ignored (status={payment_status})"

    return web.Response(status=200, text="success")

async def run_polling_safe():
    """Обёртка над polling с безопасным автоматическим перезапуском."""
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
    app.router.add_get("/health", lambda request: web.json_response({"status": "ok"}))
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
        except Exception as e:
            # прочая ошибка отправки — останавливаем воронку, чтобы не долбить
            log.exception("Не отправился шаг воронки user_id=%s step=%s", uid, sub["funnel_step"])
            _diag["last_funnel_error"] = f"{type(e).__name__}: {e}"
            _diag["last_funnel_error_step"] = sub["funnel_step"]
            _diag["last_funnel_error_at"] = datetime.now(timezone.utc).isoformat()
            await storage.set_funnel_step(uid, sub["funnel_step"], None)
            continue
        _diag["last_funnel_step_sent"] = sub["funnel_step"]
        if next_step is None:
            await storage.set_funnel_step(uid, sub["funnel_step"], None)
        else:
            await storage.set_funnel_step(uid, next_step, iso_in(delay) if delay else None)

    # 2. Просроченные подписки → expired
    for sub in await storage.expired_active_subscriptions(now_iso):
        await storage.upsert_subscription(sub["user_id"], status="expired")
        log.info("Подписка истекла: user_id=%s", sub["user_id"])

    # 3. Многоступенчатая воронка продления: за 3 дня / за 1 день / в день
    # списания / за 1 час / win-back (см. RENEWAL_STAGE_TEXTS).
    for sub in await storage.due_renewal_users(now_iso):
        uid = sub["user_id"]
        stage = (sub["renewal_stage"] or 0) + 1
        try:
            await send_renewal_stage(int(uid), stage)
        except TelegramForbiddenError:
            log.info("Продление: user_id=%s заблокировал бота", uid)
            await storage.set_blocked(uid, True)
        except Exception:
            log.exception("Не отправился шаг продления user_id=%s stage=%s", uid, stage)
        nxt = next_renewal_schedule(sub["paid_until"], stage + 1) if sub["paid_until"] else None
        await storage.upsert_subscription(
            uid,
            renewal_stage=stage,
            renewal_next_at=nxt[1] if nxt else None,
        )

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
    existing_sub = None
    if storage:
        await enter_recipe_mode(uid, message.chat.id)
        existing_sub = await storage.get_subscription(uid)
        await storage.set_blocked(uid, False)  # написал нам — точно не заблокирован
        name = message.from_user.first_name
        client = await storage.get_client(uid)
        await storage.upsert_client(
            uid,
            name,
            client.get("profile", {}),
            username=message.from_user.username,
        )
        if existing_sub is None:
            await storage.add_event(uid, "bot_started")

    if not storage:
        await send_welcome(message.chat.id)
        return

    if await has_access(uid):
        await message.answer(
            "👋 Привет! Это сборник полезных рецептов Марии Дивисенко.\n\n"
            "🍽 <b>Рецепты</b> — просматривать 250+ рецептов по категориям\n"
            "🤖 <b>Спросить МарИИю</b> — AI-нутрициолог: рационы, расчёт КБЖУ, советы\n\n"
            "Выбирай что нужно 👇",
            reply_markup=main_keyboard(),
        )
    else:
        # Новый юзер ИЛИ есть запись, но не оплачено — (пере)запускаем воронку
        # с самого начала: приветствие → (через 5 сек) видео-шаг → (через
        # 15 сек) карточка "Что ты получишь". НЕ показываем тарифы сразу —
        # они идут только по кнопке / в свой черёд по воронке.
        await storage.upsert_subscription(uid, status="trial", funnel_step=0)
        await send_welcome(message.chat.id)
        await storage.set_funnel_step(uid, 1, iso_in(5))

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
    await message.answer(
        "Профиль, история и избранное очищены. Оплата и доступ сохранены."
    )

# ─── Воронка: колбэки и тестовая оплата ──────────────────────────────────────

@dp.callback_query(F.data == "show_tariffs")
async def cb_show_tariffs(callback: CallbackQuery):
    if storage:
        await storage.add_event(str(callback.from_user.id), "offer_opened")
    await send_tariff_card(callback.message.chat.id)
    await callback.answer()

@dp.callback_query(F.data == "show_new_tariffs")
async def cb_show_new_tariffs(callback: CallbackQuery):
    if storage:
        await storage.add_event(str(callback.from_user.id), "tariff_opened")
    await send_new_tariff_card(callback.message.chat.id, callback.from_user.first_name)
    await callback.answer()

@dp.callback_query(F.data == "show_renewal_tariffs")
async def cb_show_renewal_tariffs(callback: CallbackQuery):
    if storage:
        await storage.add_event(str(callback.from_user.id), "renewal_tariff_opened")
    await send_renewal_tariff_card(callback.message.chat.id, callback.from_user.first_name)
    await callback.answer()

@dp.callback_query(F.data.startswith("tier:"))
async def cb_choose_tier(callback: CallbackQuery):
    tier = callback.data.split(":")[1]
    if tier not in TIERS:
        await callback.answer("Неизвестный тариф")
        return
    uid = str(callback.from_user.id)
    name = html.escape(callback.from_user.first_name or "Привет")
    link = await generate_payment_link(uid, tier)
    if storage and link.startswith("http"):
        await storage.add_event(uid, "payment_link_created", {"tier": tier})
    # Персонализированный формат по просьбе Назира: ссылка не идёт "голой".
    caption = (
        f"{name}, вот твоя ссылка на оплату 👇\n\n"
        "Сразу после оплаты в боте появится функционал с рецептами и твоим "
        "личным AI-ассистентом МарИИей"
    )
    if link.startswith("http"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=link)]
        ])
        await callback.message.answer(caption, reply_markup=kb)
    else:
        await callback.message.answer(f"{caption}\n\n{link}")
    await callback.answer()

@dp.message(Command("testpay"))
async def cmd_testpay(message: Message):
    """Тестовая заглушка оплаты: /testpay 1m|3m|6m.

    В production команда полностью выключена при TESTPAY_OPEN=0, в том числе
    для администратора. Ускоренный тест серии продления отдельно разрешается
    только при RENEWAL_TEST_INTERVAL_SEC>0.
    """
    if not storage:
        return
    is_admin = user_is_admin(message.from_user.id)
    if not TESTPAY_OPEN and RENEWAL_TEST_INTERVAL <= 0:
        return
    if not is_admin and RENEWAL_TEST_INTERVAL <= 0:
        return
    parts = message.text.split()
    tier = parts[1] if len(parts) > 1 else "1m"
    if tier not in TIERS:
        await message.answer("Тариф: 1m, 3m или 6m")
        return
    # В тесте пост-оплатной серии выдаём КОРОТКИЙ доступ, чтобы он реально
    # истёк по ходу теста (примерно на этапе "день списания") — так видно,
    # что логика окончания подписки работает: доступ закрывается, меню
    # рецептов/МарИИи перестаёт пускать. Иначе (30 дней) доступ бы остался.
    override = None
    if not is_admin and RENEWAL_TEST_INTERVAL > 0:
        override = (now_utc() + timedelta(seconds=RENEWAL_TEST_INTERVAL * 3 + 30)).isoformat()
    await mark_paid(
        str(message.from_user.id), tier, paid_until_override=override,
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
    Сегменты перечислены в Google Sheets на листе «Сегменты и рассылки».
    Только для админа. Заблокировавших бота пропускает автоматически."""
    if not storage:
        return
    if not user_is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3 or parts[1] not in BROADCAST_SEGMENTS:
        await message.answer(
            "Использование: /broadcast сегмент текст\n\n"
            "Сегменты:\n"
            "all — все зарегистрированные\n"
            "paid — активная подписка\n"
            "unpaid — ни разу не платили\n"
            "firstpaid — оплатили впервые\n"
            "renewed — оплатили и продлили\n"
            "notrenewed — оплатили и не продлили\n"
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

@dp.callback_query(F.data.startswith("fav:"))
async def toggle_favorite(callback: CallbackQuery):
    if not storage:
        await callback.answer()
        return
    if not await has_access(str(callback.from_user.id)):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    _, action, recipe_id = callback.data.split(":", 2)
    uid = str(callback.from_user.id)
    if action == "add":
        await storage.add_favorite(uid, recipe_id)
        is_fav = True
        note = "Добавлено в избранное ⭐"
    else:
        await storage.remove_favorite(uid, recipe_id)
        is_fav = False
        note = "Убрано из избранного"
    recipe = RECIPES.get(recipe_id)
    if recipe:
        cat_idx, sub_idx = find_cat_sub_for_recipe(recipe)
        try:
            await callback.message.edit_reply_markup(
                reply_markup=recipe_keyboard(cat_idx, sub_idx, recipe_id, is_fav)
            )
        except Exception:
            pass
    await callback.answer(note)

@dp.message(F.text == "⭐ Избранное")
async def show_favorites(message: Message):
    if not storage:
        return
    uid = str(message.from_user.id)
    if not await has_access(uid):
        await send_tariff_card(message.chat.id)
        return
    await enter_recipe_mode(uid, message.chat.id)
    fav_ids = [r for r in await storage.get_favorites(uid) if r in RECIPES]
    if not fav_ids:
        sent = await message.answer(
            "⭐ <b>Избранное пустое</b>\n\n"
            "Открой любой рецепт и нажми «⭐ В избранное» — он появится здесь, "
            "чтобы был всегда под рукой."
        )
        await storage.set_menu_message_ids(uid, [sent.message_id])
        return
    buttons = []
    for rid in fav_ids:
        name = RECIPES[rid]["name"]
        if len(name) > 40:
            name = name[:37] + "..."
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"rec:{rid}")])
    sent = await message.answer(
        "⭐ <b>Твоё избранное:</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await storage.set_menu_message_ids(uid, [sent.message_id])

@dp.message(F.text == "🍽 Рецепты")
async def show_categories(message: Message):
    uid = str(message.from_user.id)
    if not await has_access(uid):
        await send_tariff_card(message.chat.id)
        return
    await enter_recipe_mode(uid, message.chat.id)
    sent = await message.answer(
        "📂 <b>Выбери категорию:</b>",
        reply_markup=categories_keyboard(),
    )
    await storage.set_menu_message_ids(uid, [sent.message_id])

@dp.callback_query(F.data.startswith("cat:"))
async def show_subcategories(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if not await has_access(uid):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    await storage.set_mariya_mode(uid, False)
    cat_idx = int(callback.data.split(":")[1])
    cat = CATEGORIES[cat_idx]
    await callback.message.edit_text(
        f"📁 <b>{cat}</b>\n\nВыбери подкатегорию:",
        reply_markup=subcategories_keyboard(cat_idx),
    )
    await storage.set_menu_message_ids(uid, [callback.message.message_id])
    await callback.answer()

@dp.callback_query(F.data.startswith("sub:"))
async def show_recipes(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if not await has_access(uid):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    await storage.set_mariya_mode(uid, False)
    _, cat_idx, sub_idx = callback.data.split(":")
    cat_idx, sub_idx = int(cat_idx), int(sub_idx)
    cat = CATEGORIES[cat_idx]
    sub = STRUCTURE[cat][sub_idx]
    await callback.message.edit_text(
        f"📋 <b>{sub}</b>\n\nВыбери рецепт:",
        reply_markup=recipes_keyboard(cat_idx, sub_idx),
    )
    await storage.set_menu_message_ids(uid, [callback.message.message_id])
    await callback.answer()

@dp.callback_query(F.data.startswith("rec:"))
async def show_recipe(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if not await has_access(uid):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    parts = callback.data.split(":")
    recipe_id = parts[1]
    recipe = RECIPES.get(recipe_id)
    if not recipe:
        await callback.answer("Рецепт не найден")
        return
    await enter_recipe_mode(uid, callback.message.chat.id)
    cat_idx, sub_idx = find_cat_sub_for_recipe(recipe)
    if len(parts) == 4:
        try:
            origin_cat_idx, origin_sub_idx = int(parts[2]), int(parts[3])
            origin_cat = CATEGORIES[origin_cat_idx]
            origin_sub = STRUCTURE[origin_cat][origin_sub_idx]
            if recipe_id in MENU[origin_cat][origin_sub]:
                cat_idx, sub_idx = origin_cat_idx, origin_sub_idx
        except (ValueError, IndexError, KeyError):
            # Повреждённый callback не должен ломать открытие рецепта:
            # используем проверенный первый путь из menu_paths/MENU.
            pass
    text = format_recipe(recipe)
    is_fav = await storage.is_favorite(uid, recipe_id) if storage else False
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
    recipe_message_ids = []
    if photo_path:
        try:
            if len(text) <= 1024:
                # влезает в лимит подписи Telegram — шлём одним сообщением
                sent = await bot.send_photo(
                    callback.message.chat.id,
                    FSInputFile(photo_path),
                    caption=text,
                    reply_markup=recipe_keyboard(cat_idx, sub_idx, recipe_id, is_fav),
                )
                recipe_message_ids.append(sent.message_id)
                photo_sent_with_caption = True
            else:
                sent = await bot.send_photo(
                    callback.message.chat.id,
                    FSInputFile(photo_path),
                )
                recipe_message_ids.append(sent.message_id)
        except Exception:
            # не глотаем молча: фото не ушло — логируем и шлём хотя бы текст
            log.exception("Не удалось отправить фото %s (%s)", recipe_id, photo_path)

    if not photo_sent_with_caption:
        sent = await bot.send_message(
            callback.message.chat.id,
            text,
            reply_markup=recipe_keyboard(cat_idx, sub_idx, recipe_id, is_fav),
        )
        recipe_message_ids.append(sent.message_id)
    await storage.set_recipe_message_ids(uid, recipe_message_ids)
    await callback.answer()

@dp.callback_query(F.data.startswith("back:"))
async def handle_back(callback: CallbackQuery):
    uid = str(callback.from_user.id)
    if not await has_access(uid):
        await callback.answer()
        await send_tariff_card(callback.message.chat.id)
        return
    await storage.set_mariya_mode(uid, False)
    parts = callback.data.split(":")
    menu_message_id = None
    if parts[1] == "main":
        await callback.message.edit_text("📂 <b>Выбери категорию:</b>", reply_markup=categories_keyboard())
        menu_message_id = callback.message.message_id
    elif parts[1] == "cat":
        cat_idx = int(parts[2])
        cat = CATEGORIES[cat_idx]
        await callback.message.edit_text(
            f"📁 <b>{cat}</b>\n\nВыбери подкатегорию:",
            reply_markup=subcategories_keyboard(cat_idx),
        )
        menu_message_id = callback.message.message_id
    elif parts[1] == "sub":
        cat_idx, sub_idx = int(parts[2]), int(parts[3])
        cat = CATEGORIES[cat_idx]
        sub = STRUCTURE[cat][sub_idx]
        state = await storage.get_ui_state(uid)
        if callback.message.message_id in state["recipe_message_ids"]:
            # У длинного рецепта фото и текст идут двумя сообщениями.
            # Удаляем оба, а не только сообщение с кнопкой «Назад».
            await clear_open_recipe(uid, callback.message.chat.id)
            sent = await bot.send_message(
                callback.message.chat.id,
                f"📋 <b>{sub}</b>\n\nВыбери рецепт:",
                reply_markup=recipes_keyboard(cat_idx, sub_idx),
            )
            menu_message_id = sent.message_id
        else:
            try:
                await callback.message.edit_text(
                    f"📋 <b>{sub}</b>\n\nВыбери рецепт:",
                    reply_markup=recipes_keyboard(cat_idx, sub_idx),
                )
                menu_message_id = callback.message.message_id
            except Exception:
                try:
                    await callback.message.delete()
                except Exception:
                    pass
                sent = await bot.send_message(
                    callback.message.chat.id,
                    f"📋 <b>{sub}</b>\n\nВыбери рецепт:",
                    reply_markup=recipes_keyboard(cat_idx, sub_idx),
                )
                menu_message_id = sent.message_id
    if menu_message_id is not None:
        await storage.set_menu_message_ids(uid, [menu_message_id])
    await callback.answer()

# ─── МарИИя ───────────────────────────────────────────────────────────────────

@dp.message(F.text == "🤖 Спросить МарИИю")
async def mariya_intro(message: Message):
    uid = str(message.from_user.id)
    if not await has_access(uid):
        await send_tariff_card(message.chat.id)
        return
    await enter_recipe_mode(uid, message.chat.id)
    await storage.set_mariya_mode(uid, True)
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

    state = await storage.get_ui_state(uid)
    if not state["mariya_mode"]:
        return

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
        learn_model=LEARN_MODEL,
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
