from __future__ import annotations

import telebot

from .config import settings

if not settings.telegram_bot_token or not settings.runway_api_key:
    print("⚠️ Задай TELEGRAM_BOT_TOKEN и RUNWAY_API_KEY в Secrets.")

bot = telebot.TeleBot(settings.telegram_bot_token, parse_mode="HTML")

__all__ = ["bot"]
