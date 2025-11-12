from __future__ import annotations

import math
import os
import threading
import time
from typing import Callable

import telebot

from .. import assets, state
from ..config import (
    OPT_PRICE_CUSTOM_BG,
    OPT_PRICE_CUSTOM_MUSIC,
    OPT_PRICE_TITLES,
    PAYMENT_GATE_ENABLED,
    SCENE_PRICE_10S,
)
from . import tochka


def calc_order_price(st: dict) -> tuple[int, dict]:
    total = 0
    breakdown = {"scenes": [], "options": []}

    for name in st.get("scenes", []):
        if state.is_free_hugs(name):
            price = 0
        else:
            meta = assets.SCENES.get(name, {})
            price = SCENE_PRICE_10S if int(meta.get("duration", 0)) >= 10 else 0
        breakdown["scenes"].append((name, price))
        total += price

    if st.get("bg") == assets.CUSTOM_BG_KEY and st.get("bg_custom_path"):
        breakdown["options"].append(("Свой фон", OPT_PRICE_CUSTOM_BG))
        total += OPT_PRICE_CUSTOM_BG

    if st.get("music") == assets.CUSTOM_MUSIC_KEY and st.get("custom_music_path"):
        breakdown["options"].append(("Своя музыка", OPT_PRICE_CUSTOM_MUSIC))
        total += OPT_PRICE_CUSTOM_MUSIC

    if st.get("titles_mode") == "custom":
        breakdown["options"].append(("Финальные титры", OPT_PRICE_TITLES))
        total += OPT_PRICE_TITLES

    return total, breakdown


def stars_amount_for_state(st: dict) -> tuple[int, int]:
    ratio = float(os.environ.get("STARS_PER_RUB", "0.5"))
    total_rub, _ = calc_order_price(st)
    if total_rub <= 0:
        return 0, 0
    stars = int(math.ceil(total_rub * ratio))
    return stars, total_rub


def payment_methods_keyboard() -> telebot.types.InlineKeyboardMarkup:
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        telebot.types.InlineKeyboardButton("⭐️ Оплата Stars Telegram", callback_data="pay_stars"),
        telebot.types.InlineKeyboardButton("💳 Оплата картой / СБП", callback_data="pay_tochka"),
    )
    kb.add(telebot.types.InlineKeyboardButton("🏠 В главное меню", callback_data="go_home"))
    return kb


def tochka_link_keyboard(operation_id: str, url: str) -> telebot.types.InlineKeyboardMarkup:
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    kb.add(telebot.types.InlineKeyboardButton("🔗 Открыть платёж", url=url))
    kb.add(
        telebot.types.InlineKeyboardButton(
            "🔁 Я оплатил(а) — проверить", callback_data=f"checkpay_{operation_id}"
        )
    )
    kb.add(telebot.types.InlineKeyboardButton("❌ Отмена", callback_data="pay_cancel"))
    return kb


def send_payment_quote(
    bot: telebot.TeleBot,
    uid: int,
    st: dict,
    on_paid: Callable[[int, dict], None],
) -> None:
    total, breakdown = calc_order_price(st)
    lines = ["💳 <b>Итог к оплате</b>\n"]
    if breakdown["scenes"]:
        lines.append("<b>Сюжеты:</b>")
        for name, price in breakdown["scenes"]:
            price_str = f"{price} ₽" if price > 0 else "бесплатно"
            lines.append(f"• {name} — <b>{price_str}</b>")
    else:
        lines.append("• Сюжеты: не выбраны")

    if breakdown["options"]:
        lines.append("\n<b>Опции:</b>")
        for label, price in breakdown["options"]:
            lines.append(f"• {label} — +{price} ₽")
    else:
        lines.append("\nОпции: нет")

    lines.append(f"\n<b>Итого: {total} ₽</b>")
    lines.append(
        "\n<i>Примечание: опции добавляются к итоговой цене даже при бесплатном сюжете 5 сек.</i>"
    )
    payload = "\n".join(lines)

    if total <= 0:
        bot.send_message(uid, payload)
        bot.send_message(uid, "Стоимость 0 ₽ — оплата не требуется. Продолжаем ✅")
        st["await_payment"] = False
        st["payment_confirmed"] = True
        on_paid(uid, st)
        return

    try:
        bot.send_message(uid, payload, reply_markup=payment_methods_keyboard())
    except Exception as exc:
        print(f"[PAY] send quote error: {exc}")


def start_auto_check_payment(
    bot: telebot.TeleBot,
    uid: int,
    op_id: str,
    on_paid: Callable[[int, dict], None],
    period_sec: int = 10,
    max_checks: int = 12,
) -> None:
    def _worker():
        try:
            for _ in range(max_checks):
                st = state.users.setdefault(uid, state.new_state())
                if not st.get("await_payment"):
                    return
                if st.get("payment_op_id") != op_id:
                    return

                try:
                    resp = tochka.get_payment_status(op_id)
                except Exception as exc:
                    print(f"[PAY] auto-check err: {exc}")
                    time.sleep(period_sec)
                    continue

                if tochka.is_paid_status(resp):
                    st["payment_confirmed"] = True
                    st["await_payment"] = False
                    bot.send_message(uid, "✅ Оплата получена. Запускаю генерацию.")
                    on_paid(uid, st)
                    return

                time.sleep(period_sec)
        except Exception as exc:
            print(f"[PAY] auto-check thread crash: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


__all__ = [
    "calc_order_price",
    "payment_methods_keyboard",
    "send_payment_quote",
    "start_auto_check_payment",
    "tochka_link_keyboard",
    "stars_amount_for_state",
]
