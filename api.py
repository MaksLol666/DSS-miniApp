import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from api_services.security import rate_limit_middleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, text

from database.db import init_db, SessionLocal, engine
from database.models import Category, User
from config import CATEGORIES, ADMIN_ID
from api_services.auth import get_current_user

from api_services import products, cart, orders, profile, admin, captcha
import bot as bot_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("app")


async def run_migrations():
    migrations = [
        ("orders", "delivery_type", "TEXT DEFAULT 'pickup'"),
        ("orders", "delivery_name", "TEXT"),
        ("orders", "delivery_phone", "TEXT"),
        ("orders", "delivery_address", "TEXT"),
        ("users", "is_banned", "INTEGER DEFAULT 0"),
        ("users", "is_unreachable", "INTEGER DEFAULT 0"),
    ]
    async with engine.begin() as conn:
        for table, column, coltype in migrations:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
                log.info("Migration: added %s.%s", table, column)
            except Exception:
                pass


async def seed_categories():
    async with SessionLocal() as s:
        existing = (await s.execute(select(Category))).scalars().all()
        if existing:
            return
        for name in CATEGORIES:
            s.add(Category(name=name))
        await s.commit()
        log.info("Категории созданы: %s", ", ".join(CATEGORIES))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await run_migrations()
    await seed_categories()

    bot_task = asyncio.create_task(bot_module.start_bot())
    log.info("💨 Started bot + API")
    yield
    bot_task.cancel()
    try:
        await bot_task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)
app.middleware("http")(rate_limit_middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ API РОУТЕРЫ ============
app.include_router(products.router)
app.include_router(cart.router)
app.include_router(orders.router)
app.include_router(profile.router)
app.include_router(admin.router)
app.include_router(captcha.router)


@app.post("/api/auth")
async def authenticate_miniapp(user=Depends(get_current_user)):
    """Authenticate signed Telegram initData and ensure the user exists."""
    async with SessionLocal() as s:
        db_user = await s.get(User, user["id"])
        if db_user and db_user.is_banned:
            raise HTTPException(status_code=403, detail="Аккаунт заблокирован")
        if not db_user:
            db_user = User(
                id=user["id"],
                username=user.get("username") or user.get("first_name"),
            )
            s.add(db_user)
            await s.commit()
        elif user.get("username") and db_user.username != user.get("username"):
            db_user.username = user.get("username")
            await s.commit()
        return {
            "user_id": user["id"],
            "username": db_user.username,
            "is_admin": user["id"] == ADMIN_ID,
            "is_banned": False,
        }


# Явный эндпоинт для HealthCheck
@app.get("/api/health")
async def health():
    return {"ok": True}

@app.get("/health")
async def health_root():
    return {"ok": True}


# ============ FRONTEND (SPA fallback) ============
STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404)

    # Resolve the path before serving to prevent traversal outside /static.
    file_path = (STATIC_DIR / full_path).resolve()
    if file_path != STATIC_DIR.resolve() and STATIC_DIR.resolve() in file_path.parents and file_path.is_file():
        return FileResponse(file_path)

    # Missing assets should be real 404s, not HTML responses.
    if Path(full_path).suffix:
        raise HTTPException(status_code=404, detail="Static asset not found")

    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)

    raise HTTPException(status_code=404, detail="Frontend build not found")


# ============ ЗАПУСК ============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "3000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
