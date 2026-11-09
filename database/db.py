from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DB_URL
from database.models import Base

if DB_URL.startswith("sqlite"):
    Path("data").mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    DB_URL,
    echo=False,
    pool_pre_ping=True,
    connect_args={"timeout": 30} if DB_URL.startswith("sqlite") else {},
)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if DB_URL.startswith("sqlite"):
            result = await connection.exec_driver_sql("PRAGMA table_info(products)")
            columns = {row[1] for row in result.fetchall()}
            if "stock_qty" not in columns:
                await connection.exec_driver_sql("ALTER TABLE products ADD COLUMN stock_qty INTEGER NOT NULL DEFAULT 1")
            if "image_urls" not in columns:
                await connection.exec_driver_sql("ALTER TABLE products ADD COLUMN image_urls VARCHAR(4096) NOT NULL DEFAULT '[]'")
            if "cost_price" not in columns:
                await connection.exec_driver_sql("ALTER TABLE products ADD COLUMN cost_price FLOAT")
            await connection.exec_driver_sql("UPDATE products SET stock_qty = CASE WHEN in_stock = 1 THEN 1 ELSE 0 END WHERE stock_qty IS NULL OR stock_qty < 0")
            for table in ("cart_items", "order_items"):
                result = await connection.exec_driver_sql(f"PRAGMA table_info({table})")
                columns = {row[1] for row in result.fetchall()}
                if "quantity" not in columns:
                    await connection.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
                await connection.exec_driver_sql(f"UPDATE {table} SET quantity = 1 WHERE quantity IS NULL OR quantity < 1")
            result = await connection.exec_driver_sql("PRAGMA table_info(order_items)")
            order_item_columns = {row[1] for row in result.fetchall()}
            for column, sql_type in (("cost_price_snapshot", "FLOAT"), ("brand_snapshot", "VARCHAR(64)"), ("flavor_snapshot", "VARCHAR(64)")):
                if column not in order_item_columns:
                    await connection.exec_driver_sql(f"ALTER TABLE order_items ADD COLUMN {column} {sql_type}")
            result = await connection.exec_driver_sql("PRAGMA table_info(promocodes)")
            promo_columns = {row[1] for row in result.fetchall()}
            promo_migrations = (
                ("target_type", "VARCHAR(16) NOT NULL DEFAULT 'all'"),
                ("target_id", "INTEGER"),
                ("min_order", "FLOAT NOT NULL DEFAULT 0"),
                ("starts_at", "DATETIME"),
                ("expires_at", "DATETIME"),
            )
            for column, sql_type in promo_migrations:
                if column not in promo_columns:
                    await connection.exec_driver_sql(f"ALTER TABLE promocodes ADD COLUMN {column} {sql_type}")
            result = await connection.exec_driver_sql("PRAGMA table_info(orders)")
            order_columns = {row[1] for row in result.fetchall()}
            if "status" in order_columns:
                await connection.exec_driver_sql("UPDATE orders SET status = 'accepted' WHERE status = 'pending'")
                await connection.exec_driver_sql("UPDATE orders SET status = 'completed' WHERE status = 'approved'")

            result = await connection.exec_driver_sql("PRAGMA table_info(reviews)")
            review_columns = {row[1] for row in result.fetchall()}
            if "status" not in review_columns:
                await connection.exec_driver_sql("ALTER TABLE reviews ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'pending'")
