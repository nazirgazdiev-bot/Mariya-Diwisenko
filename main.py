"""
FastAPI-сервис МарИИи.

Эндпоинты:
- POST /chat              — основной диалог (вызывается из Salebot)
- POST /profile           — задать профиль клиента вручную (КБЖУ, аллергии)
- POST /forget            — стереть память диалога клиента
- POST /reset             — полный сброс клиента (профиль + диалог + факты)
- GET  /health            — проверка живости
- GET  /                  — короткое info

Авторизация:
- Все POST-эндпоинты требуют header `X-API-Key`, равный env-переменной API_SECRET.
  Это чтобы никто посторонний не дёргал наш сервис.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from mariya import Mariya
from storage import Storage

load_dotenv()

# Конфиг
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
API_SECRET = os.environ.get("API_SECRET", "")  # обязательный для приёма запросов
DB_PATH = os.environ.get("DB_PATH", "mariya_data.db")
# menu.json — структура меню + все рецепты (заменил старый recipes.json).
MENU_PATH = os.environ.get("MENU_PATH", os.environ.get("RECIPES_PATH", "menu.json"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
LEARN_MODEL = os.environ.get("LEARN_MODEL", "claude-haiku-4-5-20251001")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

storage: Storage | None = None
mariya: Mariya | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global storage, mariya
    menu_path = Path(MENU_PATH)
    if not menu_path.exists():
        raise RuntimeError(
            f"Меню не найдено: {menu_path}. "
            "Убедись что menu.json лежит рядом с main.py."
        )
    menu_data = json.loads(menu_path.read_text(encoding="utf-8"))
    if "menu" not in menu_data:
        raise RuntimeError(
            f"Файл {menu_path} — это старый recipes.json без структуры категорий. "
            "Обнови файл на свежий menu.json."
        )
    log.info(
        "Загружено: %d рецептов, %d категорий",
        menu_data["total"], len(menu_data["menu"]),
    )

    storage = Storage(DB_PATH)
    await storage.init()
    mariya = Mariya(
        anthropic_key=ANTHROPIC_API_KEY,
        recipes_data=menu_data,
        model=MODEL,
        learn_model=LEARN_MODEL,
    )
    log.info("МарИИя готова (model=%s, db=%s)", MODEL, DB_PATH)
    if not API_SECRET:
        log.warning("API_SECRET не задан! Любой может дёргать сервис. Не оставляй так в проде!")
    yield


app = FastAPI(title="МарИИя — AI-нутрициолог", lifespan=lifespan)


# ---------- Авторизация ----------

def check_auth(x_api_key: str | None):
    if not API_SECRET:
        return  # если секрет не задан — пропускаем (только для dev)
    if x_api_key != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ---------- Модели запросов ----------

class ChatRequest(BaseModel):
    user_id: str  # salebot_user_id или любой стабильный идентификатор клиента
    message: str
    name: str | None = None  # необязательно, имя клиента из Salebot


class ChatResponse(BaseModel):
    reply: str


class ProfileRequest(BaseModel):
    user_id: str
    name: str | None = None
    goal: str | None = None  # "похудение" / "набор" / "поддержание"
    target_kcal: int | None = None
    target_protein: int | None = None
    target_fat: int | None = None
    target_carbs: int | None = None
    allergies: list[str] | None = None
    dislikes: list[str] | None = None
    likes: list[str] | None = None
    notes: str | None = None


class UserRequest(BaseModel):
    user_id: str


# ---------- Эндпоинты ----------

@app.get("/")
async def root():
    return {
        "name": "МарИИя",
        "version": "1.0",
        "recipes_loaded": mariya.recipes_data["total"] if mariya else 0,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, x_api_key: str | None = Header(default=None)):
    check_auth(x_api_key)
    if not req.message.strip():
        return ChatResponse(reply="Привет! Напиши, чем тебе помочь — например, попроси составить рацион на день.")

    # Загружаем профиль клиента
    client = await storage.get_client(req.user_id)
    if req.name and not client.get("name"):
        client["name"] = req.name
        await storage.upsert_client(req.user_id, req.name, client["profile"])

    # История диалога (последние 40 реплик = 20 ходов)
    history = await storage.get_dialog(req.user_id, limit=40)

    # Генерация ответа
    reply = await mariya.chat(req.message, client, history)

    # Сохраняем обе реплики
    await storage.add_dialog(req.user_id, "user", req.message)
    await storage.add_dialog(req.user_id, "assistant", reply)

    # Фоновое авто-обучение
    asyncio.create_task(_auto_learn(req.user_id, req.message, reply, client))

    return ChatResponse(reply=reply)


async def _auto_learn(user_id: str, user_msg: str, assistant_msg: str, client: dict):
    """Фоновое: извлекаем факты из последней реплики и сохраняем."""
    try:
        existing = [f["text"] for f in client.get("facts", [])]
        # Добавляем явный профиль как "уже известное"
        p = client.get("profile", {})
        if p.get("target_kcal"):
            existing.append(f"Целевые калории: {p['target_kcal']}")
        if p.get("allergies"):
            existing.append(f"Аллергии: {', '.join(p['allergies'])}")
        if p.get("dislikes"):
            existing.append(f"Не любит: {', '.join(p['dislikes'])}")
        new_facts = await mariya.extract_facts(user_msg, assistant_msg, existing)
        if new_facts:
            await storage.add_facts(user_id, new_facts)
            log.info("Auto-learned for %s: %s", user_id, new_facts)
    except Exception as e:
        log.warning("auto_learn error: %s", e)


@app.post("/profile")
async def set_profile(req: ProfileRequest, x_api_key: str | None = Header(default=None)):
    """Задать профиль клиента явно (используется Salebot'ом при онбординге)."""
    check_auth(x_api_key)
    client = await storage.get_client(req.user_id)
    profile = client.get("profile", {})
    # Обновляем только то, что передали
    for field in ("goal", "target_kcal", "target_protein", "target_fat", "target_carbs", "notes"):
        v = getattr(req, field)
        if v is not None:
            profile[field] = v
    for field in ("allergies", "dislikes", "likes"):
        v = getattr(req, field)
        if v is not None:
            profile[field] = v
    name = req.name or client.get("name")
    await storage.upsert_client(req.user_id, name, profile)
    return {"ok": True, "profile": profile}


@app.post("/forget")
async def forget(req: UserRequest, x_api_key: str | None = Header(default=None)):
    """Стирает диалог и авто-факты, профиль сохраняется."""
    check_auth(x_api_key)
    await storage.clear_dialog(req.user_id)
    await storage.clear_facts(req.user_id)
    return {"ok": True, "message": "Память диалога и факты стёрты, профиль сохранён."}


@app.post("/reset")
async def reset(req: UserRequest, x_api_key: str | None = Header(default=None)):
    """Полный сброс клиента."""
    check_auth(x_api_key)
    await storage.full_reset(req.user_id)
    return {"ok": True, "message": "Полный сброс выполнен."}
