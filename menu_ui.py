"""
Inline-меню МарИИи: Категория → Подкатегория → Рецепты → Рецепт.

Принципы:
- Всегда работает с ОДНИМ сообщением: каждый клик редактирует то же самое
  сообщение через editMessageText. В чате не накапливается мусор.
- Кнопка «Закрыть» удаляет сообщение полностью.
- Длинные списки рецептов разбиваются на страницы по 10.
- Рецепт оформляется красиво по референсу (КБЖУ, ингредиенты, инструкция).

Регистрация:
    from menu_ui import register_menu, send_root_menu
    register_menu(dp, menu_data)

    # В команде /menu:
    await send_root_menu(message)
"""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

log = logging.getLogger("menu_ui")

# Сколько рецептов на странице в списке подкатегории
PAGE_SIZE = 10

# Глобально сохраняем загруженные данные меню — для всех обработчиков.
# Заполняется в register_menu().
_MENU_DATA: dict = {}
_CAT_LIST: list[str] = []           # ["Завтраки", "Первые блюда", ...]
_SUBS_BY_CAT: dict[int, list[str]] = {}  # cat_index → [subcategory names]
_RECIPES_BY_PATH: dict[tuple[int, int], list[str]] = {}  # (cat_idx, sub_idx) → [recipe_ids]
_RECIPE_BY_ID: dict[str, dict] = {}


def _init_data(menu_data: dict):
    """Распарсивает menu.json в удобные для меню структуры."""
    global _MENU_DATA, _CAT_LIST, _SUBS_BY_CAT, _RECIPES_BY_PATH, _RECIPE_BY_ID
    _MENU_DATA = menu_data
    _CAT_LIST = list(menu_data["menu"].keys())
    _SUBS_BY_CAT = {}
    _RECIPES_BY_PATH = {}
    for ci, cat in enumerate(_CAT_LIST):
        subs = list(menu_data["menu"][cat].keys())
        _SUBS_BY_CAT[ci] = subs
        for si, sub in enumerate(subs):
            _RECIPES_BY_PATH[(ci, si)] = list(menu_data["menu"][cat][sub])
    _RECIPE_BY_ID = {r["id"]: r for r in menu_data["recipes"]}


# ---------- Клавиатуры ----------

def kb_root() -> InlineKeyboardMarkup:
    """Главное меню: список категорий + кнопка закрытия."""
    rows = []
    for ci, cat in enumerate(_CAT_LIST):
        rows.append([InlineKeyboardButton(text=cat, callback_data=f"m:c:{ci}")])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_subcats(cat_idx: int) -> InlineKeyboardMarkup:
    """Подкатегории выбранной категории."""
    rows = []
    for si, sub in enumerate(_SUBS_BY_CAT.get(cat_idx, [])):
        count = len(_RECIPES_BY_PATH.get((cat_idx, si), []))
        if count == 0:
            continue
        rows.append([InlineKeyboardButton(
            text=f"{sub} ({count})",
            callback_data=f"m:s:{cat_idx}:{si}:0",
        )])
    rows.append([InlineKeyboardButton(text="← К категориям", callback_data="m:root")])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_recipe_list(cat_idx: int, sub_idx: int, page: int) -> InlineKeyboardMarkup:
    """Список рецептов подкатегории с пагинацией."""
    rids = _RECIPES_BY_PATH.get((cat_idx, sub_idx), [])
    total = len(rids)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)

    rows = []
    for rid in rids[start:end]:
        r = _RECIPE_BY_ID.get(rid)
        if not r:
            continue
        rows.append([InlineKeyboardButton(text=r["name"], callback_data=f"m:r:{rid}:{cat_idx}:{sub_idx}:{page}")])

    # Пагинация
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="← Стр", callback_data=f"m:s:{cat_idx}:{sub_idx}:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="m:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="Стр →", callback_data=f"m:s:{cat_idx}:{sub_idx}:{page+1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="← К подкатегориям", callback_data=f"m:c:{cat_idx}")])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_recipe(recipe_id: str, cat_idx: int, sub_idx: int, page: int) -> InlineKeyboardMarkup:
    """Кнопки под открытым рецептом."""
    rows = [
        [InlineKeyboardButton(text="← К списку рецептов", callback_data=f"m:s:{cat_idx}:{sub_idx}:{page}")],
        [InlineKeyboardButton(text="⌂ В главное меню", callback_data="m:root")],
        [InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Тексты экранов ----------

def text_root() -> str:
    return (
        "📖 МЕНЮ РЕЦЕПТОВ\n\n"
        f"Всего рецептов: {_MENU_DATA['total']}\n"
        "Выбери категорию:"
    )


def text_subcats(cat_idx: int) -> str:
    cat = _CAT_LIST[cat_idx]
    subs = _SUBS_BY_CAT.get(cat_idx, [])
    total = sum(len(_RECIPES_BY_PATH.get((cat_idx, si), [])) for si in range(len(subs)))
    return (
        f"📂 {cat.upper()}\n\n"
        f"Всего рецептов в категории: {total}\n"
        "Выбери подкатегорию:"
    )


def text_recipe_list(cat_idx: int, sub_idx: int, page: int) -> str:
    cat = _CAT_LIST[cat_idx]
    sub = _SUBS_BY_CAT[cat_idx][sub_idx]
    rids = _RECIPES_BY_PATH.get((cat_idx, sub_idx), [])
    total = len(rids)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_label = f" (стр. {page+1}/{total_pages})" if total_pages > 1 else ""
    return (
        f"📂 {cat} → {sub}\n\n"
        f"Рецептов: {total}{page_label}\n"
        "Выбери рецепт:"
    )


def _format_kbju_line(r: dict) -> str:
    if "kcal" not in r:
        return "КБЖУ: не указан (универсальное блюдо)"
    portion = r.get("portion", "per_100g")
    label = "на порцию" if portion == "per_portion" else "на 100 г"
    return (
        f"🥗 КБЖУ ({label}):\n"
        f"• Калории: {r['kcal']:g} ккал\n"
        f"• Белки: {r['protein']:g} г\n"
        f"• Жиры: {r['fat']:g} г\n"
        f"• Углеводы: {r['carbs']:g} г"
    )


def text_recipe(recipe_id: str) -> str:
    r = _RECIPE_BY_ID.get(recipe_id)
    if not r:
        return "Рецепт не найден."

    name = r["name"].upper()
    parts = [f"🍳 {name}", ""]
    parts.append(_format_kbju_line(r))
    parts.append("")

    ings = r.get("ingredients") or []
    if ings:
        parts.append("🛒 Ингредиенты:")
        for ing in ings:
            parts.append(f"• {ing}")
        parts.append("")

    instr = (r.get("instructions") or "").strip()
    if instr:
        parts.append("👨‍🍳 Приготовление:")
        parts.append(instr)
        parts.append("")

    # Путь в меню (для контекста — если клиент пришёл сюда из AI-чата)
    if r.get("menu_paths"):
        parts.append("📍 Где в меню: " + " · ".join(r["menu_paths"][:2]))

    text = "\n".join(parts).strip()
    # Telegram лимит 4096 символов
    if len(text) > 4000:
        text = text[:3900] + "\n\n…(текст обрезан)"
    return text


# ---------- Отправка/навигация ----------

async def send_root_menu(message: Message):
    """Открывает главное меню (используется из команды /menu)."""
    await message.answer(text_root(), reply_markup=kb_root(), parse_mode=None)


async def _safe_edit(query: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    """Редактирует текущее сообщение. Если не получилось — отправляет новое."""
    try:
        await query.message.edit_text(text, reply_markup=kb, parse_mode=None)
    except Exception as e:
        log.warning("edit_text failed (%s) — sending new message", e)
        try:
            await query.message.answer(text, reply_markup=kb, parse_mode=None)
        except Exception as e2:
            log.error("send new message also failed: %s", e2)


# ---------- Обработчики callback ----------

async def _on_root(query: CallbackQuery):
    await _safe_edit(query, text_root(), kb_root())
    await query.answer()


async def _on_cat(query: CallbackQuery):
    # data = "m:c:NN"
    try:
        ci = int(query.data.split(":")[2])
    except (IndexError, ValueError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    if ci < 0 or ci >= len(_CAT_LIST):
        await query.answer("Категория не найдена", show_alert=True)
        return
    await _safe_edit(query, text_subcats(ci), kb_subcats(ci))
    await query.answer()


async def _on_sub(query: CallbackQuery):
    # data = "m:s:CI:SI:PAGE"
    try:
        _, _, ci, si, page = query.data.split(":")
        ci, si, page = int(ci), int(si), int(page)
    except (IndexError, ValueError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    await _safe_edit(query, text_recipe_list(ci, si, page), kb_recipe_list(ci, si, page))
    await query.answer()


async def _on_recipe(query: CallbackQuery):
    # data = "m:r:RID:CI:SI:PAGE"
    try:
        _, _, rid, ci, si, page = query.data.split(":")
        ci, si, page = int(ci), int(si), int(page)
    except (IndexError, ValueError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    await _safe_edit(query, text_recipe(rid), kb_recipe(rid, ci, si, page))
    await query.answer()


async def _on_close(query: CallbackQuery):
    try:
        await query.message.delete()
    except Exception as e:
        log.warning("delete on close failed: %s", e)
        try:
            await query.message.edit_text("Меню закрыто.", reply_markup=None, parse_mode=None)
        except Exception:
            pass
    await query.answer("Меню закрыто")


async def _on_noop(query: CallbackQuery):
    await query.answer()


# ---------- Регистрация ----------

def register_menu(dp: Dispatcher, menu_data: dict):
    """Подключает обработчики меню к диспетчеру aiogram."""
    _init_data(menu_data)
    dp.callback_query.register(_on_root, F.data == "m:root")
    dp.callback_query.register(_on_close, F.data == "m:close")
    dp.callback_query.register(_on_noop, F.data == "m:noop")
    dp.callback_query.register(_on_cat, F.data.startswith("m:c:"))
    dp.callback_query.register(_on_sub, F.data.startswith("m:s:"))
    dp.callback_query.register(_on_recipe, F.data.startswith("m:r:"))
    log.info(
        "Меню зарегистрировано: %d категорий, %d рецептов",
        len(_CAT_LIST), len(_RECIPE_BY_ID),
    )
