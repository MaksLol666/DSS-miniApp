import hashlib
import hmac
import json
from urllib.parse import parse_qs
from typing import Optional

from fastapi import Header, HTTPException
from config import BOT_TOKEN, ADMIN_ID


def verify_init_data(init_data: str) -> Optional[dict]:
    """Проверяет подпись initData. Возвращает user dict или None."""
    if not init_data:
        return None

    try:
        parsed = parse_qs(init_data)
    except Exception:
        return None

    hash_str = parsed.pop("hash", [None])[0]
    if not hash_str:
        return None

    data_check_string = "\n".join(
        f"{k}={v[0]}" for k, v in sorted(parsed.items())
    )
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if calc_hash != hash_str:
        return None

    user_raw = parsed.get("user", [None])[0]
    if not user_raw:
        return None

    try:
        return json.loads(user_raw)
    except Exception:
        return None


async def get_current_user(x_init_data: str = Header(...)) -> dict:
    """Dependency: возвращает {id, username, is_admin, ...} или 401."""
    user = verify_init_data(x_init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid initData")
    user["is_admin"] = (user["id"] == ADMIN_ID)
    return user


async def require_admin(x_init_data: str = Header(...)) -> dict:
    user = await get_current_user(x_init_data)
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
