"""
Inline-меню МарИИи: Категория → Рецепты → Рецепт.

Принципы:
- Всегда работает с ОДНИМ сообщением: каждый клик редактирует то же самое
  сообщение через editMessageText. В чате не накапливается мусор.
- Кнопка «Закрыть» удаляет сообщение полностью.
- Длинные списки рецептов разбиваются на страницы по 10.
- Рецепт оформляется по референсу (КБЖУ, ингредиенты, инструкция).

Регистрация:
    from menu_ui import register_menu, send_root_menu
    register_menu(dp, menu_data)
    await send_root_menu(message)  # в команде /menu
"""

from __future__ import annotations

import logging
import re
from typing import Any

from aiogram import Dispatcher, F
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

log = logging.getLogger("menu_ui")

PAGE_SIZE = 10

_MENU_DATA: dict = {}
_CAT_LIST: list[str] = []
_RECIPES_BY_CAT: dict[int, list[str]] = {}  # cat_index → [recipe_ids]
_RECIPE_BY_ID: dict[str, dict] = {}


def _init_data(menu_data: dict):
    global _MENU_DATA, _CAT_LIST, _RECIPES_BY_CAT, _RECIPE_BY_ID
    _MENU_DATA = menu_data
    _CAT_LIST = list(menu_data["menu"].keys())
    _RECIPES_BY_CAT = {ci: list(menu_data["menu"][cat]) for ci, cat in enumerate(_CAT_LIST)}
    _RECIPE_BY_ID = {r["id"]: r for r in menu_data["recipes"]}


# ---------- Клавиатуры ----------

def kb_root() -> InlineKeyboardMarkup:
    rows = []
    for ci, cat in enumerate(_CAT_LIST):
        count = len(_RECIPES_BY_CAT.get(ci, []))
        rows.append([InlineKeyboardButton(
            text=f"{cat} ({count})",
            callback_data=f"m:c:{ci}:0",
        )])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_recipe_list(cat_idx: int, page: int) -> InlineKeyboardMarkup:
    rids = _RECIPES_BY_CAT.get(cat_idx, [])
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
        rows.append([InlineKeyboardButton(
            text=r["name"],
            callback_data=f"m:r:{rid}:{cat_idx}:{page}",
        )])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="← Стр", callback_data=f"m:c:{cat_idx}:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="m:noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton(text="Стр →", callback_data=f"m:c:{cat_idx}:{page+1}"))
        rows.append(nav)

    rows.append([InlineKeyboardButton(text="← К категориям", callback_data="m:root")])
    rows.append([InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_recipe(recipe_id: str, cat_idx: int, page: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="← К списку рецептов", callback_data=f"m:c:{cat_idx}:{page}")],
        [InlineKeyboardButton(text="⌂ В главное меню", callback_data="m:root")],
        [InlineKeyboardButton(text="✕ Закрыть меню", callback_data="m:close")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Тексты ----------

def text_root() -> str:
    return (
        "📖 МЕНЮ РЕЦЕПТОВ\n\n"
        f"Всего рецептов: {_MENU_DATA['total']}\n"
        "Выбери категорию:"
    )


def text_recipe_list(cat_idx: int, page: int) -> str:
    cat = _CAT_LIST[cat_idx]
    rids = _RECIPES_BY_CAT.get(cat_idx, [])
    total = len(rids)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_label = f" (стр. {page+1}/{total_pages})" if total_pages > 1 else ""
    return (
        f"📂 {cat}\n\n"
        f"Рецептов: {total}{page_label}\n"
        "Выбери рецепт:"
    )


def text_recipe(recipe_id: str) -> str:
    r = _RECIPE_BY_ID.get(recipe_id)
    if not r:
        return "Рецепт не найден."

    parts = [f"🍳 {r['name']}", ""]

    if "kcal" in r:
        portion = r.get("portion", "per_100g")
        label = "на порцию" if portion == "per_portion" else "на 100г"
        parts.append(f"📊 КБЖУ {label}:")
        parts.append(
            f"🔥 {r['kcal']:g} ккал  |  🥩 Б: {r['protein']:g}г  |  "
            f"🧈 Ж: {r['fat']:g}г  |  🍞 У: {r['carbs']:g}г"
        )
    else:
        parts.append("📊 КБЖУ: не указан (универсальное блюдо)")
    parts.append("")

    ings = r.get("ingredients") or []
    if ings:
        parts.append("🛒 Ингредиенты:")
        for ing in ings:
            parts.append(f"• {ing}")
        parts.append("")

    instr = (r.get("instructions") or "").strip()
    if instr:
        parts.append("👨‍🍳 ПРИГОТОВЛЕНИЕ")
        parts.append("")
        parts.append(instr)

    text = "\n".join(parts).strip()
    if len(text) > 4000:
        text = text[:3900] + "\n\n…(текст обрезан)"
    return text


# ---------- Навигация ----------

async def send_root_menu(message: Message):
    await message.answer(text_root(), reply_markup=kb_root(), parse_mode=None)


async def _safe_edit(query: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await query.message.edit_text(text, reply_markup=kb, parse_mode=None)
    except Exception as e:
        log.warning("edit_text failed (%s) — sending new", e)
        try:
            await query.message.answer(text, reply_markup=kb, parse_mode=None)
        except Exception as e2:
            log.error("send new also failed: %s", e2)


# ---------- Callback handlers ----------

async def _on_root(query: CallbackQuery):
    await _safe_edit(query, text_root(), kb_root())
    await query.answer()


async def _on_cat(query: CallbackQuery):
    # data = "m:c:CI:PAGE"
    try:
        _, _, ci, page = query.data.split(":")
        ci, page = int(ci), int(page)
    except (IndexError, ValueError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    if ci < 0 or ci >= len(_CAT_LIST):
        await query.answer("Категория не найдена", show_alert=True)
        return
    await _safe_edit(query, text_recipe_list(ci, page), kb_recipe_list(ci, page))
    await query.answer()


async def _on_recipe(query: CallbackQuery):
    # data = "m:r:RID:CI:PAGE"
    try:
        _, _, rid, ci, page = query.data.split(":")
        ci, page = int(ci), int(page)
    except (IndexError, ValueError):
        await query.answer("Сломанная кнопка", show_alert=True)
        return
    await _safe_edit(query, text_recipe(rid), kb_recipe(rid, ci, page))
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
    _init_data(menu_data)
    dp.callback_query.register(_on_root, F.data == "m:root")
    dp.callback_query.register(_on_close, F.data == "m:close")
    dp.callback_query.register(_on_noop, F.data == "m:noop")
    dp.callback_query.register(_on_cat, F.data.startswith("m:c:"))
    dp.callback_query.register(_on_recipe, F.data.startswith("m:r:"))
    log.info(
        "Меню зарегистрировано: %d категорий, %d рецептов",
        len(_CAT_LIST), len(_RECIPE_BY_ID),
    )
