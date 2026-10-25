import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_URL = "sqlite+aiosqlite:///data/shop.db"

# URL MiniApp — заполни после деплоя (Bothost выдаст поддомен)
MINIAPP_URL = os.getenv("MINIAPP_URL", "https://example.bothost.ru")

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не задан!")
if not ADMIN_ID:
    raise RuntimeError("❌ ADMIN_ID не задан!")

CATEGORIES = [
    "Жижа",
    "Устройства",
    "Одноразки",
    "Снюс",
    "Пластинки",
    "Расходники",
    "Наборы",
]

REFERRAL_BONUS_PERCENT = 15
REFERRAL_MILESTONE = 10
REFERRAL_MILESTONE_DISCOUNT = 10
MAX_PROMOS_PER_ORDER = 2

REVIEWS_CHANNEL_ID = -1004357829679
REVIEWS_CHANNEL_USERNAME = "@OTZOV_DSS"

DELIVERY_PRICE = 500
