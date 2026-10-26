import os
from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = _int_env("ADMIN_ID", 0)
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://dss-shop.bothost.ru").strip()
# Keep the same relative database path as the original project to preserve data.
DB_URL = os.getenv("DB_URL", "sqlite+aiosqlite:///data/shop.db").strip()
CATEGORIES = ["Жижа", "Устройства", "Одноразки", "Снюс", "Пластинки", "Расходники", "Наборы"]
REFERRAL_BONUS_PERCENT = 15
REFERRAL_MILESTONE = 10
REFERRAL_MILESTONE_DISCOUNT = 10
MAX_PROMOS_PER_ORDER = 2
DELIVERY_PRICE = 500
REVIEWS_CHANNEL_ID = os.getenv("REVIEWS_CHANNEL_ID", "-1004357829679").strip()
REVIEWS_CHANNEL_USERNAME = os.getenv("REVIEWS_CHANNEL_USERNAME", "@OTZOV_DSS").strip()
