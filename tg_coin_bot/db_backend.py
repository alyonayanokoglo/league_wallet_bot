"""Доступ к БД: MySQL (Railway: MYSQL_URL / отдельные MYSQL*) или SQLite (DB_PATH)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional
from urllib.parse import unquote, urlparse

import aiosqlite
import aiomysql
from aiomysql import DictCursor

USE_MYSQL = False
_pool: Optional[Any] = None
_sqlite_path: str = "bot.db"


def _mysql_connect_kwargs() -> dict[str, Any] | None:
    url = (os.getenv("MYSQL_URL") or os.getenv("DATABASE_URL") or "").strip()
    if url:
        lowered = url.lower()
        if lowered.startswith("mysql+pymysql://"):
            url = "mysql://" + url.split("://", 1)[1]
            lowered = url.lower()
        if not (lowered.startswith("mysql://") or lowered.startswith("mysql2://")):
            return None
        u = urlparse(url)
        if not u.hostname:
            return None
        path_db = (u.path or "").lstrip("/").split("?")[0]
        return {
            "host": u.hostname,
            "port": u.port or 3306,
            "user": unquote(u.username) if u.username else "root",
            "password": unquote(u.password) if u.password else "",
            "db": path_db or "railway",
        }

    host = os.getenv("MYSQLHOST") or os.getenv("MYSQL_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(os.getenv("MYSQLPORT") or os.getenv("MYSQL_PORT") or "3306"),
        "user": os.getenv("MYSQLUSER") or os.getenv("MYSQL_USER") or "root",
        "password": os.getenv("MYSQLPASSWORD") or os.getenv("MYSQL_PASSWORD") or "",
        "db": os.getenv("MYSQLDATABASE") or os.getenv("MYSQL_DATABASE") or "railway",
    }


async def init_db_backend(sqlite_path: str) -> None:
    global USE_MYSQL, _pool, _sqlite_path
    if _pool is not None:
        return
    _sqlite_path = sqlite_path
    kwargs = _mysql_connect_kwargs()
    if kwargs:
        _pool = await aiomysql.create_pool(
            charset="utf8mb4",
            autocommit=False,
            minsize=1,
            maxsize=10,
            **kwargs,
        )
        USE_MYSQL = True
    else:
        USE_MYSQL = False
        _pool = None


async def close_db_backend() -> None:
    global USE_MYSQL, _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None
        USE_MYSQL = False


def is_mysql() -> bool:
    return USE_MYSQL


class _MySQLSession:
    def __init__(self, cursor: Any, conn: Any) -> None:
        self._cursor = cursor
        self._conn = conn

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        await self._cursor.execute(sql.replace("?", "%s"), tuple(params))
        return self._cursor

    async def commit(self) -> None:
        await self._conn.commit()


class _SQLiteSession:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        return await self._db.execute(sql, tuple(params))

    async def commit(self) -> None:
        await self._db.commit()


@asynccontextmanager
async def db_session() -> AsyncIterator[_MySQLSession | _SQLiteSession]:
    if USE_MYSQL:
        if _pool is None:
            raise RuntimeError("Пул MySQL не инициализирован (init_db_backend).")
        async with _pool.acquire() as conn:
            async with conn.cursor(DictCursor) as cur:
                sess = _MySQLSession(cur, conn)
                try:
                    yield sess
                except Exception:
                    await conn.rollback()
                    raise
    else:
        async with aiosqlite.connect(_sqlite_path) as db:
            db.row_factory = aiosqlite.Row
            sess = _SQLiteSession(db)
            try:
                yield sess
            except Exception:
                await db.rollback()
                raise


def row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        raise ValueError("row is None")
    if isinstance(row, dict):
        return dict(row)
    return dict(row)
