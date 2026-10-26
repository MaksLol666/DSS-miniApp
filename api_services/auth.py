import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl
from typing import Optional

from fastapi import Header, HTTPException
from config import BOT_TOKEN, ADMIN_ID

TELEGRAM_INIT_DATA_MAX_AGE_SECONDS = int(
    os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "86400")
)


def verify_init_data(init_data: str) -> Optional[dict]:
    """Validate Telegram Mini App initData and return its user payload."""
    if not init_data:
        return None

    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except (ValueError, TypeError):
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs.items())
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_hash, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
        now = int(time.time())
        if auth_date <= 0 or auth_date > now + 30:
            return None
        if now - auth_date > TELEGRAM_INIT_DATA_MAX_AGE_SECONDS:
            return None

        user_raw = pairs.get("user")
        if not user_raw:
            return None
        user = json.loads(user_raw)
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            return None
        return user
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def get_current_user(
    x_init_data: str = Header(default="", alias="X-Init-Data")
) -> dict:
    """Dependency: returns the authenticated Telegram user or HTTP 401."""
    user = verify_init_data(x_init_data)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Недействительные данные Telegram. Перезапусти Mini App.",
        )
    user["is_admin"] = user["id"] == ADMIN_ID
    return user


async def require_admin(
    x_init_data: str = Header(default="", alias="X-Init-Data")
) -> dict:
    user = await get_current_user(x_init_data)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Доступ только для администратора")
    return user
