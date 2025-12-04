import os
import threading

import uvicorn

from bot.main import run as run_telegram_bot
from bot.web.app import create_app


def start_telegram_bot() -> None:
    """
    Запуск Telegram-бота в отдельном потоке.
    Использует существующую функцию run() из bot.main без изменений.
    """
    run_telegram_bot()


def start_web_api() -> None:
    """
    Запуск FastAPI-приложения через Uvicorn.
    Приложение создаётся функцией create_app() из bot.web.app.
    Порт берём из переменной окружения PORT (важно для Replit),
    по умолчанию 8000.
    """
    app = create_app()
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    # Бот запускаем в фоне, API — в основном потоке.
    bot_thread = threading.Thread(target=start_telegram_bot, daemon=True)
    bot_thread.start()

    # Блокирующий запуск веб-API.
    start_web_api()
