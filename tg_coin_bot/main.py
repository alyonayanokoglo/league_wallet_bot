import asyncio
import os
import random
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

import aiosqlite
from pymysql.err import IntegrityError as MySQLIntegrityError
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv
from db_backend import (
    close_db_backend,
    db_session,
    init_db_backend,
    is_mysql,
    row_to_dict,
)
from admin_handlers import register_admin_handlers
from activities_handlers import register_activities_handlers
from questions_handlers import register_questions_handlers
from quiz_handlers import register_quiz_handlers


BASE_DIR = Path(__file__).resolve().parent
# Явный путь: при запуске из другой cwd (например Railway) корневой .env не подхватится.
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "bot.db")
CAREER_DEMO_PHOTO_PATH = BASE_DIR / "img" / "1920х1080 (1) (1).png"
BALANCE_PHOTO_PATH = BASE_DIR / "img" / "1200Х600 (1) (1).png"

ADMIN_IDS = {
    int(admin_id.strip())
    for admin_id in os.getenv("ADMIN_IDS", "").split(",")
    if admin_id.strip()
}

ACTIVITY_WELCOME = "Регистрация: приветственное"
ACTIVITY_QUIZ_REWARD = "Квиз о банке"

# Лекторий по промокоду — отдельные активности. Ручное начисление без кода — одна активность,
# до 4 раз на человека (см. миграцию transactions без UNIQUE по паре активность-пользователь).
ACTIVITY_ADMIN_LECTURE_FALLBACK = "Лекция (админ)"
ACTIVITY_ADMIN_MANUAL = "Ручное начисление (админ)"


def _transactions_type_column() -> str:
    """В MySQL `type` — зарезервированное слово, нужны обратные кавычки."""
    return "`type`" if is_mysql() else "type"


def _activities_seed_rows() -> list[tuple[str, int, int, int]]:
    return [
        (ACTIVITY_WELCOME, 100, 1, 0),
        ("HR-свидание (общение с рекрутерами)", 100, 1, 0),
        ("ИТ-Дженга", 200, 1, 0),
        ("ИТ-МЕМО", 200, 1, 0),
        ("Скрипт-мастер", 200, 1, 0),
        ("Объяснительная", 200, 1, 0),
        ("Финансовые активы", 200, 1, 0),
        ("Финансы судьбы", 200, 1, 0),
        ("Подписка на Telegram‑канал", 100, 1, 0),
        ("Подписка на VK‑сообщество", 100, 1, 0),
        (ACTIVITY_QUIZ_REWARD, 10, 20, 0),
        ("Лекция 1", 400, 1, 1),
        ("Лекция 2", 400, 1, 1),
        ("Лекция 3", 400, 1, 1),
        (ACTIVITY_ADMIN_LECTURE_FALLBACK, 400, 4, 0),
        (ACTIVITY_ADMIN_MANUAL, 0, 999999, 0),
    ]


async def _mysql_init_schema_and_seed(db: Any) -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            username VARCHAR(255) NULL,
            name VARCHAR(255) NOT NULL,
            badge_id VARCHAR(255) NOT NULL,
            wallet_id VARCHAR(255) NOT NULL,
            balance INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_users_telegram (telegram_id),
            UNIQUE KEY uq_users_badge (badge_id),
            UNIQUE KEY uq_users_wallet (wallet_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS activities (
            id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(512) NOT NULL,
            points INT NOT NULL,
            is_active TINYINT NOT NULL DEFAULT 1,
            limit_per_user INT NOT NULL DEFAULT 1,
            is_lecture TINYINT NOT NULL DEFAULT 0,
            UNIQUE KEY uq_activities_title (title)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    cur = await db.execute(
        """
        SELECT COUNT(*) AS c FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'activities'
          AND COLUMN_NAME = 'is_lecture'
        """
    )
    row = await cur.fetchone()
    if row is not None and int(row_to_dict(row).get("c") or 0) == 0:
        await db.execute(
            "ALTER TABLE activities ADD COLUMN is_lecture TINYINT NOT NULL DEFAULT 0"
        )

    tc = _transactions_type_column()
    await db.execute(
        f"""
        CREATE TABLE IF NOT EXISTS transactions (
            id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id INT UNSIGNED NOT NULL,
            activity_id INT UNSIGNED NOT NULL,
            points INT NOT NULL,
            {tc} VARCHAR(32) NOT NULL,
            created_by_admin_tg_id BIGINT NULL,
            created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_transactions_user FOREIGN KEY (user_id) REFERENCES users (id),
            CONSTRAINT fk_transactions_activity FOREIGN KEY (activity_id) REFERENCES activities (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS karma_debits (
            id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id INT UNSIGNED NOT NULL,
            points INT NOT NULL,
            created_by_admin_tg_id BIGINT NOT NULL,
            created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_karma_user FOREIGN KEY (user_id) REFERENCES users (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS open_mic_questions (
            id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id INT UNSIGNED NOT NULL,
            text TEXT NOT NULL,
            created_at TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_open_mic_user FOREIGN KEY (user_id) REFERENCES users (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    activities = _activities_seed_rows()
    for title, points, limit_per_user, is_lecture in activities:
        await db.execute(
            """
            INSERT INTO activities (title, points, is_active, limit_per_user, is_lecture)
            VALUES (?, ?, 1, ?, ?)
            ON DUPLICATE KEY UPDATE
                points = VALUES(points),
                is_active = 1,
                limit_per_user = VALUES(limit_per_user),
                is_lecture = VALUES(is_lecture)
            """,
            (title, points, limit_per_user, is_lecture),
        )

    active_titles = [t for (t, _p, _l, _il) in activities]
    placeholders = ",".join(["?"] * len(active_titles))
    await db.execute(
        f"""
        UPDATE activities
        SET is_active = 0
        WHERE title NOT IN ({placeholders})
        """,
        active_titles,
    )
    await db.commit()


if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан BOT_TOKEN. Локально — в tg_coin_bot/.env; на Railway — "
        "Variables → BOT_TOKEN, подключи MySQL и задай MYSQL_URL (или DATABASE_URL "
        "вида mysql://...); без них бот использует локальный SQLite (DB_PATH)."
    )


class Registration(StatesGroup):
    waiting_for_badge_id = State()
    waiting_for_name = State()


def main_reply_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="Где взять карму"),
                KeyboardButton(text="Все активности"),
            ],
            [
                KeyboardButton(text="Мой баланс"),
                KeyboardButton(text="Списать карму"),
            ],
            [KeyboardButton(text="Квиз о банке")],
            [KeyboardButton(text="🎤 Задать вопрос (открытый микрофон)")],
            [KeyboardButton(text="Меню")],
        ],
        resize_keyboard=True,
    )


def is_admin(telegram_id: int) -> bool:
    return telegram_id in ADMIN_IDS


def career_demo_text(name: str) -> str:
    return (
        f"{name}, сейчас начинается демо-версия твоей карьеры в Совкомбанке. "
        "Что тебя ждёт:\n\n"
        "<b>Переговорки</b>\n"
        "• 16:00–20:00 — Карьерные консультации один на один с рекрутером\n\n"
        "<b>Зона интерактивов и комната отдыха</b>\n"
        "• 16:00–20:00 — Погружение в рабочие процессы. Участвуй в интерактивах, "
        "чтобы примерить профессии Совкомбанка на себе\n\n"
        "<b>Лекторий</b>\n"
        "• 17:00–17:30 — Ознакомительная встреча\n"
        "• 17:30–18:00 — Маршрут перестроен: карьерные «нет», которые приведут вас к работе мечты "
        "(Ксения Васильева)\n"
        "• 18:00–18:30 — Как не быть свайпнутым в цифровом мире: боремся за внимание рекрутеров, "
        "коллег и клиентов (Ольга Кадникова, Ольга Игнатович)\n"
        "• 18:30–19:00 — Спастись от деградации: инструкция по осознанному обучению в эпоху AI "
        "(Виктория Свищёва)\n"
        "• 20:00–21:00 — Открытый микрофон с Максимом Лутчаком\n"
        "• 21:00–22:00 — Встреча с друллегами у кулера и битва диджеев\n\n"
        "<b>Магазин мерча (товаров)</b>\n"
        "• 17:30–20:00 — …\n\n"
        "Хочешь узнать, где и как заработать карму на покупку мерча?"
    )


async def _migrate_transactions_drop_activity_unique(db: Any) -> None:
    """Снимаем UNIQUE(user_id, activity_id, type), чтобы limit_per_user > 1 работал через счётчик."""
    cursor = await db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'transactions'"
    )
    row = await cursor.fetchone()
    if row is None or row[0] is None:
        return
    ddl = row[0]
    if not re.search(
        r"UNIQUE\s*\(\s*user_id\s*,\s*activity_id\s*,\s*type\s*\)",
        ddl,
        re.IGNORECASE,
    ):
        return

    await db.execute(
        """
        CREATE TABLE transactions_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            activity_id INTEGER NOT NULL,
            points INTEGER NOT NULL,
            type TEXT NOT NULL,
            created_by_admin_tg_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (activity_id) REFERENCES activities(id)
        )
        """
    )
    await db.execute(
        """
        INSERT INTO transactions_new (
            id, user_id, activity_id, points, type, created_by_admin_tg_id, created_at
        )
        SELECT id, user_id, activity_id, points, type, created_by_admin_tg_id, created_at
        FROM transactions
        """
    )
    await db.execute("DROP TABLE transactions")
    await db.execute("ALTER TABLE transactions_new RENAME TO transactions")


async def init_db() -> None:
    await init_db_backend(DB_PATH)
    if is_mysql():
        async with db_session() as db:
            await _mysql_init_schema_and_seed(db)
        return

    async with db_session() as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                name TEXT NOT NULL,
                badge_id TEXT UNIQUE NOT NULL,
                wallet_id TEXT UNIQUE NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS activities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT UNIQUE NOT NULL,
                points INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                limit_per_user INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        cols_cursor = await db.execute("PRAGMA table_info(activities)")
        cols = await cols_cursor.fetchall()
        col_names = {row[1] for row in cols}
        if "is_lecture" not in col_names:
            await db.execute(
                "ALTER TABLE activities ADD COLUMN is_lecture INTEGER NOT NULL DEFAULT 0"
            )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                activity_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                type TEXT NOT NULL,
                created_by_admin_tg_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (activity_id) REFERENCES activities(id)
            )
            """
        )

        await _migrate_transactions_drop_activity_unique(db)

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS karma_debits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                points INTEGER NOT NULL,
                created_by_admin_tg_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )

        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS open_mic_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )

        activities = _activities_seed_rows()
        for title, points, limit_per_user, is_lecture in activities:
            await db.execute(
                """
                INSERT INTO activities (title, points, is_active, limit_per_user, is_lecture)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(title) DO UPDATE SET
                    points = excluded.points,
                    is_active = 1,
                    limit_per_user = excluded.limit_per_user,
                    is_lecture = excluded.is_lecture
                """,
                (title, points, limit_per_user, is_lecture),
            )

        active_titles = [t for (t, _p, _l, _is_lecture) in activities]
        placeholders = ",".join(["?"] * len(active_titles))
        await db.execute(
            f"""
            UPDATE activities
            SET is_active = 0
            WHERE title NOT IN ({placeholders})
            """,
            active_titles,
        )

        await db.commit()


async def get_open_mic_questions_count_for_user(user_id: int) -> int:
    async with db_session() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) AS count FROM open_mic_questions WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return 0
        return int(row_to_dict(row)["count"])


async def create_open_mic_question(user_id: int, text: str) -> None:
    async with db_session() as db:
        await db.execute(
            "INSERT INTO open_mic_questions (user_id, text) VALUES (?, ?)",
            (user_id, text),
        )
        await db.commit()


async def get_user_by_telegram_id(telegram_id: int) -> Optional[dict]:
    async with db_session() as db:
        cursor = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = await cursor.fetchone()
        return row_to_dict(row) if row else None


async def get_user_by_badge_id(badge_id: str) -> Optional[dict]:
    async with db_session() as db:
        cursor = await db.execute(
            "SELECT * FROM users WHERE badge_id = ?",
            (badge_id,),
        )
        row = await cursor.fetchone()
        return row_to_dict(row) if row else None


async def create_user(
    telegram_id: int,
    username: Optional[str],
    name: str,
    badge_id: str,
) -> dict:
    for _ in range(24):
        wallet_id = str(random.randint(1000000, 9999999))
        try:
            async with db_session() as db:
                await db.execute(
                    """
                    INSERT INTO users (telegram_id, username, name, badge_id, wallet_id, balance)
                    VALUES (?, ?, ?, ?, ?, 0)
                    """,
                    (telegram_id, username, name, badge_id, wallet_id),
                )
                await db.commit()
            break
        except (sqlite3.IntegrityError, MySQLIntegrityError):
            continue
    else:
        raise RuntimeError("Не удалось создать пользователя: не удалось выбрать уникальный wallet_id.")

    user = await get_user_by_telegram_id(telegram_id)
    if user is None:
        raise RuntimeError("Не удалось прочитать созданного пользователя из БД")
    return user


async def get_activities_for_admin_accrual() -> list[dict]:
    """Активности для кнопок начисления в админке: без регистрации, квиза и лекций."""
    async with db_session() as db:
        cursor = await db.execute(
            """
            SELECT * FROM activities
            WHERE is_active = 1
              AND is_lecture = 0
              AND title NOT IN (?, ?)
            ORDER BY points ASC
            """,
            (ACTIVITY_WELCOME, ACTIVITY_QUIZ_REWARD),
        )
        rows = await cursor.fetchall()
        return [row_to_dict(row) for row in rows]


async def get_activity_by_id(activity_id: int) -> Optional[dict]:
    async with db_session() as db:
        cursor = await db.execute(
            "SELECT * FROM activities WHERE id = ?",
            (activity_id,),
        )
        row = await cursor.fetchone()
        return row_to_dict(row) if row else None


async def grant_activity_once(
    user_id: int,
    activity_title: str,
    admin_tg_id: Optional[int],
) -> tuple[bool, str, Optional[dict]]:
    """Начисление по названию активности; число записей ограничено limit_per_user в БД."""
    tc = _transactions_type_column()
    async with db_session() as db:
        user_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        user_row = await user_cursor.fetchone()
        if not user_row:
            return False, "Участник не найден.", None

        activity_cursor = await db.execute(
            "SELECT * FROM activities WHERE title = ? AND is_active = 1",
            (activity_title,),
        )
        activity_row = await activity_cursor.fetchone()
        if not activity_row:
            return False, "Активность не найдена.", None

        user = row_to_dict(user_row)
        activity = row_to_dict(activity_row)
        activity_id = activity["id"]

        duplicate_cursor = await db.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM transactions
            WHERE user_id = ?
              AND activity_id = ?
              AND {tc} = 'accrual'
            """,
            (user_id, activity_id),
        )
        duplicate_row = await duplicate_cursor.fetchone()
        if duplicate_row is None:
            return False, "Не удалось проверить дубликаты начисления.", None

        cnt = int(row_to_dict(duplicate_row)["count"])
        lim = int(activity["limit_per_user"])
        if cnt >= lim:
            return (
                False,
                f"Лимит начислений за «{activity['title']}» исчерпан ({cnt}/{lim}).",
                None,
            )

        await db.execute(
            f"""
            INSERT INTO transactions (
                user_id,
                activity_id,
                points,
                {tc},
                created_by_admin_tg_id
            )
            VALUES (?, ?, ?, 'accrual', ?)
            """,
            (user_id, activity_id, activity["points"], admin_tg_id),
        )

        await db.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE id = ?
            """,
            (activity["points"], user_id),
        )

        await db.commit()

        updated_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        updated_user_row = await updated_cursor.fetchone()
        if updated_user_row is None:
            return False, "Участник не найден после начисления.", None

        result = {
            "user": row_to_dict(updated_user_row),
            "activity": activity,
            "points": activity["points"],
        }
        return True, "Начислено.", result


async def add_points(
    user_id: int,
    activity_id: int,
    admin_tg_id: int,
) -> tuple[bool, str, Optional[dict]]:
    tc = _transactions_type_column()
    async with db_session() as db:
        user_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        user_row = await user_cursor.fetchone()

        if not user_row:
            return False, "Участник не найден.", None

        activity_cursor = await db.execute(
            "SELECT * FROM activities WHERE id = ? AND is_active = 1",
            (activity_id,),
        )
        activity_row = await activity_cursor.fetchone()

        if not activity_row:
            return False, "Активность не найдена или неактивна.", None

        user = row_to_dict(user_row)
        activity = row_to_dict(activity_row)

        if int(activity.get("is_lecture") or 0):
            return (
                False,
                "Через админку нельзя начислять карму за лекции.",
                None,
            )

        if activity["title"] in (ACTIVITY_WELCOME, ACTIVITY_QUIZ_REWARD):
            return (
                False,
                "Через админку нельзя начислять карму за эту активность.",
                None,
            )

        duplicate_cursor = await db.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM transactions
            WHERE user_id = ?
              AND activity_id = ?
              AND {tc} = 'accrual'
            """,
            (user_id, activity_id),
        )
        duplicate_row = await duplicate_cursor.fetchone()
        if duplicate_row is None:
            return False, "Не удалось проверить дубликаты начисления.", None

        cnt = int(row_to_dict(duplicate_row)["count"])
        lim = int(activity["limit_per_user"])
        if cnt >= lim:
            return (
                False,
                f"Лимит начислений за «{activity['title']}» исчерпан для этого участника "
                f"({cnt}/{lim}).",
                None,
            )

        await db.execute(
            f"""
            INSERT INTO transactions (
                user_id,
                activity_id,
                points,
                {tc},
                created_by_admin_tg_id
            )
            VALUES (?, ?, ?, 'accrual', ?)
            """,
            (user_id, activity_id, activity["points"], admin_tg_id),
        )

        await db.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE id = ?
            """,
            (activity["points"], user_id),
        )

        await db.commit()

        updated_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        updated_user_row = await updated_cursor.fetchone()
        if updated_user_row is None:
            return False, "Участник не найден после начисления.", None

        result = {
            "user": row_to_dict(updated_user_row),
            "activity": activity,
            "points": activity["points"],
        }

        return True, "Карма начислена.", result


async def manual_add_points(
    user_id: int,
    points: int,
    admin_tg_id: int,
) -> tuple[bool, str, Optional[dict]]:
    if points <= 0:
        return False, "Укажи целое число баллов больше нуля.", None

    tc = _transactions_type_column()
    async with db_session() as db:
        user_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        user_row = await user_cursor.fetchone()
        if not user_row:
            return False, "Участник не найден.", None

        activity_cursor = await db.execute(
            "SELECT * FROM activities WHERE title = ? AND is_active = 1",
            (ACTIVITY_ADMIN_MANUAL,),
        )
        activity_row = await activity_cursor.fetchone()
        if not activity_row:
            return False, "Активность для ручного начисления не найдена.", None

        activity = row_to_dict(activity_row)

        await db.execute(
            f"""
            INSERT INTO transactions (
                user_id,
                activity_id,
                points,
                {tc},
                created_by_admin_tg_id
            )
            VALUES (?, ?, ?, 'accrual', ?)
            """,
            (user_id, activity["id"], points, admin_tg_id),
        )
        await db.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE id = ?
            """,
            (points, user_id),
        )
        await db.commit()

        updated_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        updated_user_row = await updated_cursor.fetchone()
        if updated_user_row is None:
            return False, "Участник не найден после начисления.", None

        return (
            True,
            "Карма начислена.",
            {
                "user": row_to_dict(updated_user_row),
                "points": points,
                "activity": activity,
            },
        )


async def deduct_karma(
    user_id: int,
    points: int,
    admin_tg_id: int,
) -> tuple[bool, str, Optional[dict]]:
    if points <= 0:
        return False, "Укажи целое число баллов больше нуля.", None

    async with db_session() as db:
        user_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        user_row = await user_cursor.fetchone()
        if not user_row:
            return False, "Участник не найден.", None

        user = row_to_dict(user_row)
        if int(user["balance"]) < points:
            return (
                False,
                f"Недостаточно кармы: на балансе {user['balance']}, "
                f"списать нельзя {points}.",
                None,
            )

        await db.execute(
            "UPDATE users SET balance = balance - ? WHERE id = ?",
            (points, user_id),
        )
        await db.execute(
            """
            INSERT INTO karma_debits (user_id, points, created_by_admin_tg_id)
            VALUES (?, ?, ?)
            """,
            (user_id, points, admin_tg_id),
        )
        await db.commit()

        updated_cursor = await db.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        )
        updated_row = await updated_cursor.fetchone()
        if updated_row is None:
            return False, "Не удалось прочитать баланс после списания.", None

        updated = row_to_dict(updated_row)
        return (
            True,
            "Списано.",
            {"user": updated, "points": points},
        )


bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

register_activities_handlers(
    dp=dp,
    bot=bot,
    main_reply_menu=main_reply_menu,
    get_user_by_telegram_id=get_user_by_telegram_id,
    grant_activity_once=grant_activity_once,
)
register_quiz_handlers(
    dp=dp,
    main_reply_menu=main_reply_menu,
    get_user_by_telegram_id=get_user_by_telegram_id,
    grant_activity_once=grant_activity_once,
    activity_quiz_reward=ACTIVITY_QUIZ_REWARD,
)
register_questions_handlers(
    dp=dp,
    main_reply_menu=main_reply_menu,
    get_user_by_telegram_id=get_user_by_telegram_id,
    get_questions_count_for_user=get_open_mic_questions_count_for_user,
    create_question=create_open_mic_question,
)
register_admin_handlers(
    dp=dp,
    bot=bot,
    is_admin=is_admin,
    get_user_by_badge_id=get_user_by_badge_id,
    get_activities_for_admin=get_activities_for_admin_accrual,
    add_points=add_points,
    manual_add_points=manual_add_points,
    deduct_karma=deduct_karma,
)


@dp.message(Command("myid"))
async def my_id(message: Message) -> None:
    if message.from_user is None:
        return
    await message.answer(
        f"Твой Telegram ID:\n\n`{message.from_user.id}`\n\n"
        "Скопируй его и добавь в ADMIN_IDS в файле .env, если ты сотрудник стенда.",
        parse_mode="Markdown",
    )


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    from_user = message.from_user
    if from_user is None:
        return

    user = await get_user_by_telegram_id(from_user.id)

    if user:
        await message.answer(
            f"Привет, {user['name']}!\n\n"
            f"Номер браслета: {user['badge_id']} (назови его при оплате мерча)\n"
            f"Баланс: {user['balance']} баллов кармы",
            reply_markup=main_reply_menu(),
        )
        return

    await state.set_state(Registration.waiting_for_badge_id)
    await message.answer(
        "Привет! Я Кошелёк кармы.\n\n"
        "Карма — внутренняя валюта Совкомбанка и Лиги Приключений. "
        "На неё ты сможешь купить классный мерч в магазине товаров.\n\n"
        "Введи номер со своего браслета (4 цифры), чтобы продолжить."
    )


@dp.message(Registration.waiting_for_badge_id)
async def registration_badge_id(message: Message, state: FSMContext) -> None:
    text = message.text
    if text is None:
        await message.answer("Напиши ID текстом.")
        return
    badge_id = text.strip()

    if len(badge_id) != 4 or not badge_id.isdigit():
        await message.answer("Номер браслета — ровно 4 цифры, без пробелов и букв.")
        return

    existing_badge = await get_user_by_badge_id(badge_id)
    if existing_badge:
        await message.answer(
            "Такой ID браслета уже зарегистрирован.\n\n"
            "Проверь номер или подойди к организатору."
        )
        return

    await state.update_data(badge_id=badge_id)
    await state.set_state(Registration.waiting_for_name)
    await message.answer("Супер! Как я могу к тебе обращаться?")


@dp.message(Registration.waiting_for_name)
async def registration_name(message: Message, state: FSMContext) -> None:
    from_user = message.from_user
    if from_user is None:
        return

    text = message.text
    if text is None:
        await message.answer("Напиши имя текстом.")
        return
    name = text.strip()

    if len(name) < 2:
        await message.answer("Напиши имя чуть подробнее.")
        return

    data = await state.get_data()
    badge_id = data["badge_id"]

    user = await create_user(
        telegram_id=from_user.id,
        username=from_user.username,
        name=name,
        badge_id=badge_id,
    )

    await state.clear()

    await message.answer_photo(FSInputFile(str(CAREER_DEMO_PHOTO_PATH)))
    await message.answer(
        career_demo_text(user["name"]),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Да!", callback_data="onboard:karma_info")]
            ]
        ),
    )


@dp.callback_query(F.data == "onboard:karma_info")
async def onboarding_karma(callback: CallbackQuery) -> None:
    if callback.from_user is None:
        await callback.answer()
        return
    cb_msg = callback.message
    if not isinstance(cb_msg, Message):
        await callback.answer()
        return

    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer("Сначала пройди регистрацию.", show_alert=True)
        return

    ok, msg, result = await grant_activity_once(
        user_id=user["id"],
        activity_title=ACTIVITY_WELCOME,
        admin_tg_id=None,
    )

    if not ok:
        balance_line = (
            f"На твоём балансе уже есть карма: {user['balance']} баллов.\n\n"
            if "уже было" in msg
            else ""
        )
        await cb_msg.answer(
            f"{user['name']}, приветственные баллы уже начислялись.\n\n"
            f"{balance_line}"
            "Открывай главное меню кнопкой «Меню» ниже.",
            reply_markup=main_reply_menu(),
        )
        await callback.answer()
        return

    assert result is not None
    u = result["user"]
    await cb_msg.answer(
        f"{u['name']}, лови первые {result['points']} баллов кармы! "
        "Её дают за участие в интерактивных зонах. Ты готов? "
        "Вперёд зарабатывать карму!",
        reply_markup=main_reply_menu(),
    )
    await callback.answer()


@dp.message(F.text == "Меню")
async def menu_button(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await get_user_by_telegram_id(message.from_user.id) if message.from_user else None
    if user:
        await message.answer(
            f"{user['name']}, ты в главном меню. Выбери раздел ниже.",
            reply_markup=main_reply_menu(),
        )
    else:
        await message.answer(
            "Главное меню. Чтобы продолжить, нажми /start.",
            reply_markup=main_reply_menu(),
        )


@dp.message(F.text == "Мой баланс")
async def my_balance(message: Message) -> None:
    from_user = message.from_user
    if from_user is None:
        return
    user = await get_user_by_telegram_id(from_user.id)
    if not user:
        await message.answer("Сначала нажми /start и зарегистрируйся.")
        return

    await message.answer_photo(FSInputFile(str(BALANCE_PHOTO_PATH)))
    await message.answer(
        f"На твоём балансе сейчас: {user['balance']} баллов кармы.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Меню", callback_data="nav:menu"),
                    InlineKeyboardButton(text="Списать карму", callback_data="bal:spend"),
                ]
            ]
        ),
    )


@dp.callback_query(F.data == "bal:spend")
async def balance_spend_inline(callback: CallbackQuery) -> None:
    cb_msg = callback.message
    if not isinstance(cb_msg, Message):
        await callback.answer()
        return
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user:
        await callback.answer()
        return
    await cb_msg.answer(
        "Чтобы списать карму, назови номер с браслета (4 цифры).\n\n"
        f"Твой номер: {user['badge_id']}",
        reply_markup=main_reply_menu(),
    )
    await callback.answer()


@dp.message(F.text == "Списать карму")
async def spend_karma(message: Message) -> None:
    from_user = message.from_user
    if from_user is None:
        return
    user = await get_user_by_telegram_id(from_user.id)
    if not user:
        await message.answer("Сначала нажми /start и зарегистрируйся.")
        return

    await message.answer(
        "Чтобы списать карму, назови номер с браслета (4 цифры).\n\n"
        f"Твой номер: {user['badge_id']}",
        reply_markup=main_reply_menu(),
    )


async def main() -> None:
    try:
        await init_db()
        # Если у бота ранее был настроен webhook, polling не будет получать апдейты.
        last_err: Optional[Exception] = None
        for attempt in range(1, 6):
            try:
                await bot.delete_webhook(drop_pending_updates=True, request_timeout=30)
                me = await bot.get_me(request_timeout=30)
                print(f"Bot started: @{me.username} (id={me.id})", flush=True)
                last_err = None
                break
            except Exception as e:
                last_err = e
                print(
                    f"Failed to reach Telegram API (attempt {attempt}/5): {e!r}",
                    flush=True,
                )
                await asyncio.sleep(min(2**attempt, 20))
        if last_err is not None:
            raise last_err
        await dp.start_polling(bot)
    finally:
        await close_db_backend()


if __name__ == "__main__":
    asyncio.run(main())
