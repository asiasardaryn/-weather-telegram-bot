import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "weather_bot.db"


def init_db(default_city_name, default_lat, default_lon, default_timezone):
    """Create the settings table if needed and seed the single row on first run."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                chat_id INTEGER,
                city_name TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                scheduled_time TEXT,
                timezone TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO settings
                (id, chat_id, city_name, latitude, longitude, scheduled_time, timezone)
            VALUES (1, NULL, ?, ?, ?, NULL, ?)
            """,
            (default_city_name, default_lat, default_lon, default_timezone),
        )
        conn.commit()
    finally:
        conn.close()


def get_settings():
    """Returns a dict: chat_id, city_name, latitude, longitude, scheduled_time, timezone."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return dict(row)
    finally:
        conn.close()


def set_chat_id(chat_id):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE settings SET chat_id = ? WHERE id = 1", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def set_city(city_name, latitude, longitude, timezone):
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE settings SET city_name = ?, latitude = ?, longitude = ?, timezone = ? WHERE id = 1",
            (city_name, latitude, longitude, timezone),
        )
        conn.commit()
    finally:
        conn.close()


def set_schedule(time_str):
    """time_str is 'HH:MM', or None to unsubscribe."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("UPDATE settings SET scheduled_time = ? WHERE id = 1", (time_str,))
        conn.commit()
    finally:
        conn.close()
