from __future__ import annotations

import logging

from .app import bot
from .config import ensure_directories
from .handlers import core  # noqa: F401 ensures handlers register


def run() -> None:
    ensure_directories()
    try:
        bot.remove_webhook()
    except Exception as exc:
        logging.warning("Webhook removal warning: %s", exc)

    print("Memory Forever (web build) started.")
    bot.infinity_polling(skip_pending=True, timeout=60)


__all__ = ["run"]
