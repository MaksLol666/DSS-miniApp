import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import select, text

from database.db import init_db, SessionLocal, engine
from database.models import Category
from config import CATEGORIES

from api_services import products, cart, orders, profile, admin
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


@app.get("/api/health")
async def health():
    return {"ok": True}


# ============ FRONTEND (SPA fallback) ============
STATIC_DIR = Path("static")


@app.get("/{full_path:path}")
async def serve_frontend(full_path: str):
    """SPA fallback: всё, что не /api/*, отдаём через static/index.html."""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404)

    # если запрошен конкретный файл из static/ — отдаём его
    file_path = STATIC_DIR / full_path
    if file_path.is_file():
        return FileResponse(file_path)

    # иначе — index.html (React Router сам разберётся, какой компонент показать)
    index = STATIC_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)

    # frontend ещё не собран
    return {
        "ok": True,
        "msg": "Frontend not built yet. Push to frontend/ and wait for GitHub Actions.",
    }


# ============ ЗАПУСК ============
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
