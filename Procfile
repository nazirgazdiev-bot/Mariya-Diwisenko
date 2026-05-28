# Два режима — выбирается через Custom Start Command в Railway Settings.
#
# Режим 1 (для теста через Telegram):
#   python telegram_bot.py
#
# Режим 2 (для продакшена с Salebot — FastAPI webhook):
#   uvicorn main:app --host 0.0.0.0 --port $PORT
#
# По умолчанию здесь Telegram-режим (для удобства тестирования).
# Когда будешь готов к Salebot — поменяй Custom Start Command в Railway.
web: python telegram_bot.py
