from __future__ import annotations

import requests

from ..config import settings


class TochkaError(RuntimeError):
    pass


def create_payment_link(amount_rub: int | float, purpose: str) -> tuple[str, str]:
    assert settings.tochka_jwt, "TOCHKA_JWT не задан"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.tochka_jwt}",
    }
    payload = {
        "Data": {
            "merchantId": settings.tochka_merchant_id,
            "customerCode": settings.tochka_customer_code,
            "amount": f"{float(amount_rub):.2f}",
            "purpose": purpose[:255],
            "redirectUrl": settings.tochka_ok_url,
            "failRedirectUrl": settings.tochka_fail_url,
            "paymentMode": ["card", "sbp"],
            "ttl": 10080,
        }
    }
    resp = requests.post(
        f"{settings.tochka_api_base}/payments", headers=headers, json=payload, timeout=60
    )
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        raise TochkaError(f"Create payment {resp.status_code}: {getattr(resp, 'text', '')}")

    info = data.get("Data") or {}
    op_id = info.get("operationId") or info.get("operationID") or ""
    link = info.get("paymentLink") or ""

    if not (op_id and link):
        raise TochkaError(f"Create payment: неполный ответ: {data}")
    return op_id, link


def get_payment_status(op_id: str) -> dict:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {settings.tochka_jwt}"}
    resp = requests.get(
        f"{settings.tochka_api_base}/payments/{op_id}", headers=headers, timeout=60
    )
    resp.raise_for_status()
    return resp.json()


def is_paid_status(resp_json: dict) -> bool:
    data = resp_json.get("Data") or {}
    op = None
    if isinstance(data.get("Operation"), list) and data["Operation"]:
        op = data["Operation"][0]
    status = (op or data).get("status") or ""
    return status.upper() in {"APPROVED", "COMPLETED"}


__all__ = ["create_payment_link", "get_payment_status", "is_paid_status", "TochkaError"]
