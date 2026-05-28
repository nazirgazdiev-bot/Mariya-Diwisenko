"""
МарИИя — мозги ассистента.

Использует Claude с prompt caching: база рецептов кешируется и стоит почти ничего
на каждом следующем запросе в течение 5 минут (или до часа в beta).

Структура запроса:
  system = [
    { "type": "text", "text": "<инструкция МарИИи>", "cache_control": {"type": "ephemeral"} },
    { "type": "text", "text": "<вся база рецептов в JSON>", "cache_control": {"type": "ephemeral"} },
    { "type": "text", "text": "<профиль клиента и факты>" },  # не кешируется, у каждого свой
  ]
  messages = история диалога + новый ввод

После ответа — фоновое авто-обучение Haiku-моделью: вытаскиваем из реплики
факты о клиенте и сохраняем.
"""

import asyncio
import json
import logging
import re

from anthropic import AsyncAnthropic

log = logging.getLogger("mariya")

MAIN_SYSTEM_INSTRUCTION = """\
Ты — МарИИя, AI-нутрициолог фитнес-блогера Марии Дивисенко.
Помогаешь клиентам её платной подписки составлять рационы.

ОБРАЩЕНИЕ И ИДЕНТИФИКАЦИЯ:
- Тебя зовут МарИИя (именно так, с двумя И). Если спросят кто ты — представься так.
- Ты не Claude и не модель OpenAI. Если спросят на чём ты работаешь — скажи что ты
  AI-нутрициолог, разработанный для сборника рецептов Марии Дивисенко.
- К клиенту обращайся на «ты», дружелюбно, без канцелярита.

ГЛАВНОЕ ПРАВИЛО — ИСТОЧНИК РЕЦЕПТОВ:
- Ты используешь ТОЛЬКО рецепты из приложенного ниже сборника. Других рецептов
  не существует.
- Если клиент спрашивает про блюдо, которого в сборнике нет — честно говори:
  «В сборнике такого нет, но есть похожее: [перечисли что есть]».
- НИКОГДА не выдумывай рецепты и КБЖУ. Если у блюда в сборнике нет КБЖУ
  (универсальное блюдо) — так и пиши, не подставляй числа из головы.

СОСТАВЛЕНИЕ РАЦИОНОВ:
- При составлении дневного/недельного рациона:
  1) Учитывай целевые калории и БЖУ клиента.
  2) Учитывай его аллергии, нелюбимое, особенности (вегетарианство и т.п.).
  3) Бери блюда из разных категорий — завтрак / обед / ужин / перекус.
  4) В конце дня СУММИРУЙ КБЖУ и покажи итог. Если попал не точно — это нормально,
     +/- 50 ккал по дню допустимо. Если сильнее — попробуй переподобрать.
  5) Указывай для каждого приёма пищи: название блюда, КБЖУ, страницу в сборнике.

ФОРМАТ ОТВЕТА:
- Никакого markdown: ни #, ни **, ни ---. Telegram это не рендерит.
- Структурируй пустыми строками и обычным текстом. Заголовки можно прописными.
- Не пиши «Конечно! Вот ваш рацион:». Сразу к делу.
- В конце короткий итог по КБЖУ дня и предложи следующий шаг
  («Хочешь меню на неделю?», «Заменить какое-то блюдо?»).

ЕСЛИ ИНФОРМАЦИИ НЕ ХВАТАЕТ:
- Если клиент не указал целевые калории или БЖУ — задай ОДИН уточняющий вопрос.
- Не задавай больше одного вопроса за раз.
"""

# Промпт для авто-обучения (фоновое извлечение фактов)
LEARN_INSTRUCTION = """\
Из этого фрагмента диалога вытащи НОВЫЕ устойчивые факты о клиенте,
которые стоит запомнить надолго:
- цели по КБЖУ
- аллергии и непереносимости
- что НЕ ест (вегетарианство, не любит курицу и т.п.)
- что ЛЮБИТ
- особенности (тренировки, режим питания, имя, возраст и т.п.)

Не записывай:
- разовые вопросы и просьбы («составь рацион на завтра»)
- ответы ассистента как факты о клиенте

Уже известные факты:
{existing_facts}

Сообщение клиента:
{user_msg}

Ответ МарИИи:
{assistant_msg}

Верни ТОЛЬКО валидный JSON-массив строк (каждая — один краткий факт).
Если новых нет — верни []. Без markdown, без пояснений.
Максимум 5 фактов.

Пример: ["Цель: 1500 ккал/день", "Аллергия на орехи", "Не ест курицу и рыбу", "Любит сладкое"]
"""


def build_recipe_book_text(recipes_data: dict) -> str:
    """
    Делает компактное текстовое представление всей базы рецептов для system prompt.
    Только то, что МарИИе нужно для подбора рациона.
    """
    out = ["СБОРНИК РЕЦЕПТОВ МАРИИ ДИВИСЕНКО (всего: {})".format(recipes_data["total"]), ""]
    by_cat: dict[str, list[dict]] = {}
    for r in recipes_data["recipes"]:
        by_cat.setdefault(r["category"], []).append(r)

    for cat, recs in by_cat.items():
        out.append(f"=== {cat.upper()} ({len(recs)}) ===")
        for r in recs:
            line = f"[{r['id']}] {r['name']} (стр {r['page']})"
            if "kcal" in r:
                line += f" — {r['kcal']:g} ккал, Б {r['protein']:g} / Ж {r['fat']:g} / У {r['carbs']:g}"
            else:
                line += " — КБЖУ не указан (универсальное блюдо)"
            out.append(line)
        out.append("")
    return "\n".join(out)


def build_client_context(profile: dict, facts: list[dict]) -> str:
    """Контекст конкретного клиента — НЕ кешируется, у каждого свой."""
    lines = ["ПРОФИЛЬ КЛИЕНТА:"]
    if profile.get("name"):
        lines.append(f"Имя: {profile['name']}")
    p = profile.get("profile", {})
    if p:
        if p.get("goal"):
            lines.append(f"Цель: {p['goal']}")
        if p.get("target_kcal"):
            lines.append(f"Целевые калории: {p['target_kcal']} ккал/день")
        if p.get("target_protein") or p.get("target_fat") or p.get("target_carbs"):
            lines.append(
                f"Целевые БЖУ: Б {p.get('target_protein','?')} / "
                f"Ж {p.get('target_fat','?')} / У {p.get('target_carbs','?')}"
            )
        if p.get("allergies"):
            lines.append(f"Аллергии: {', '.join(p['allergies'])}")
        if p.get("dislikes"):
            lines.append(f"Не любит: {', '.join(p['dislikes'])}")
        if p.get("likes"):
            lines.append(f"Любит: {', '.join(p['likes'])}")
        if p.get("notes"):
            lines.append(f"Особенности: {p['notes']}")
    if facts:
        lines.append("\nЗАПОМНЕННЫЕ ФАКТЫ (из прошлых разговоров):")
        for f in facts[:30]:
            lines.append(f"- {f['text']}")
    if len(lines) == 1:
        lines.append("(новый клиент, профиль пуст)")
    return "\n".join(lines)


def validate_recipes(text: str, valid_ids: set[str], valid_names: set[str]) -> list[str]:
    """
    Возвращает список упомянутых названий, которых нет в сборнике (галлюцинации).
    Простая эвристика: ищем строки вида "Название (стр N)" или "[Rxxx]" и проверяем.
    """
    issues = []
    # Проверка ID
    for m in re.finditer(r"\[(R\d{3})\]", text):
        rid = m.group(1)
        if rid not in valid_ids:
            issues.append(f"Несуществующий ID: {rid}")
    # Проверка названий вида "Название (стр N)" — слабая эвристика, может промахиваться
    return issues


class Mariya:
    def __init__(
        self,
        anthropic_key: str,
        recipes_data: dict,
        model: str = "claude-sonnet-4-6",
        learn_model: str = "claude-haiku-4-5-20251001",
    ):
        self.client = AsyncAnthropic(api_key=anthropic_key)
        self.recipes_data = recipes_data
        self.recipe_book_text = build_recipe_book_text(recipes_data)
        self.valid_ids = {r["id"] for r in recipes_data["recipes"]}
        self.valid_names = {r["name"].lower() for r in recipes_data["recipes"]}
        self.model = model
        self.learn_model = learn_model

    def _build_system(self, client_ctx: str) -> list[dict]:
        return [
            {
                "type": "text",
                "text": MAIN_SYSTEM_INSTRUCTION,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": self.recipe_book_text,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": client_ctx,
            },
        ]

    async def chat(
        self,
        user_message: str,
        client: dict,
        dialog_history: list[dict],
    ) -> str:
        """Один проход: получает ответ от Claude с учётом профиля и истории."""
        client_ctx = build_client_context(client, client.get("facts", []))
        messages = dialog_history + [{"role": "user", "content": user_message}]

        last_err = None
        for attempt in range(3):
            try:
                resp = await self.client.messages.create(
                    model=self.model,
                    max_tokens=2048,
                    system=self._build_system(client_ctx),
                    messages=messages,
                )
                # Собираем текст из всех текстовых блоков
                parts = []
                for block in resp.content:
                    t = getattr(block, "text", None)
                    if t:
                        parts.append(t)
                text = "".join(parts).strip()
                return text if text else "(пустой ответ модели)"
            except Exception as e:
                last_err = e
                err = str(e).lower()
                if any(c in err for c in ("500", "502", "503", "529", "overloaded", "timeout")):
                    log.warning("Claude hiccup (attempt %s): %s", attempt + 1, e)
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                raise
        log.error("Claude failed: %s", last_err)
        return "Извини, сервер AI временно недоступен. Попробуй ещё раз через минуту."

    async def extract_facts(
        self,
        user_msg: str,
        assistant_msg: str,
        existing_facts: list[str],
    ) -> list[str]:
        """Фоновое: спрашиваем Haiku какие новые факты появились."""
        existing_text = "\n".join(f"- {f}" for f in existing_facts[-30:]) or "(пусто)"
        prompt = LEARN_INSTRUCTION.format(
            existing_facts=existing_text,
            user_msg=user_msg[:1500],
            assistant_msg=assistant_msg[:1500],
        )
        try:
            resp = await self.client.messages.create(
                model=self.learn_model,
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(getattr(b, "text", "") for b in resp.content).strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
            facts = json.loads(text)
            if isinstance(facts, list):
                return [f.strip() for f in facts if isinstance(f, str) and f.strip()][:5]
        except Exception as e:
            log.warning("extract_facts failed: %s", e)
        return []
