"""
storage.py
----------
Petite base SQLite locale pour stocker l'historique.
Pas de serveur, pas de cloud : un simple fichier `garmin_coach.db`
à côté du projet. On peut donc re-synchroniser tous les jours sans
jamais perdre l'historique déjà téléchargé.
"""

import sqlite3
import json
import hashlib
from pathlib import Path

_BASE_DIR = Path(__file__).parent
DB_PATH = _BASE_DIR / "garmin_coach.db"  # base par défaut (mode local mono-utilisateur avec .env)


def use_db_for_user(email: str):
    """
    Bascule sur une base de données propre à cette personne (utile en mode
    hébergé multi-utilisateurs, où chaque ami a ses propres identifiants
    Garmin). Sans appel à cette fonction, l'appli utilise la base par défaut
    (comportement local historique, inchangé).
    """
    global DB_PATH
    data_dir = _BASE_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    user_hash = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    DB_PATH = data_dir / f"garmin_coach_{user_hash}.db"
    init_db()

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id TEXT PRIMARY KEY,
    date TEXT,
    name TEXT,
    distance_km REAL,
    duration_s REAL,
    avg_pace_s_per_km REAL,
    avg_hr REAL,
    max_hr REAL,
    avg_cadence REAL,
    elevation_gain REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS sleep (
    date TEXT PRIMARY KEY,
    sleep_score INTEGER,
    total_sleep_s REAL,
    deep_sleep_s REAL,
    light_sleep_s REAL,
    rem_sleep_s REAL,
    awake_s REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS wellness (
    date TEXT PRIMARY KEY,
    resting_hr INTEGER,
    hrv_avg REAL,
    body_battery_max INTEGER,
    body_battery_min INTEGER,
    stress_avg INTEGER,
    steps INTEGER
);

CREATE TABLE IF NOT EXISTS laps (
    activity_id TEXT,
    lap_index INTEGER,
    distance_km REAL,
    duration_s REAL,
    avg_pace_s_per_km REAL,
    avg_hr REAL,
    max_hr REAL,
    PRIMARY KEY (activity_id, lap_index)
);

CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    date TEXT,
    distance_km REAL,
    elevation_gain REAL,
    elevation_profile_json TEXT,
    is_active INTEGER DEFAULT 0
);
"""

# Colonnes ajoutées après la création initiale de la base : on les ajoute
# automatiquement si elles manquent, pour que ta base existante se mette à
# jour sans que tu aies à la supprimer.
MIGRATIONS = {
    "activities": [
        ("temp_c", "REAL"),
        ("feels_like_c", "REAL"),
    ],
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()

    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
    conn.commit()
    conn.close()


def upsert_activity(row: dict):
    conn = get_conn()
    conn.execute(
        """INSERT INTO activities
           (activity_id, date, name, distance_km, duration_s, avg_pace_s_per_km,
            avg_hr, max_hr, avg_cadence, elevation_gain, raw_json)
           VALUES (:activity_id, :date, :name, :distance_km, :duration_s, :avg_pace_s_per_km,
                   :avg_hr, :max_hr, :avg_cadence, :elevation_gain, :raw_json)
           ON CONFLICT(activity_id) DO UPDATE SET
             date=excluded.date, name=excluded.name, distance_km=excluded.distance_km,
             duration_s=excluded.duration_s, avg_pace_s_per_km=excluded.avg_pace_s_per_km,
             avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, avg_cadence=excluded.avg_cadence,
             elevation_gain=excluded.elevation_gain, raw_json=excluded.raw_json
        """,
        row,
    )
    conn.commit()
    conn.close()


def update_activity_weather(activity_id: str, temp_c, feels_like_c):
    conn = get_conn()
    conn.execute(
        "UPDATE activities SET temp_c = ?, feels_like_c = ? WHERE activity_id = ?",
        (temp_c, feels_like_c, activity_id),
    )
    conn.commit()
    conn.close()


def upsert_sleep(row: dict):
    conn = get_conn()
    conn.execute(
        """INSERT INTO sleep
           (date, sleep_score, total_sleep_s, deep_sleep_s, light_sleep_s, rem_sleep_s, awake_s, raw_json)
           VALUES (:date, :sleep_score, :total_sleep_s, :deep_sleep_s, :light_sleep_s, :rem_sleep_s, :awake_s, :raw_json)
           ON CONFLICT(date) DO UPDATE SET
             sleep_score=excluded.sleep_score, total_sleep_s=excluded.total_sleep_s,
             deep_sleep_s=excluded.deep_sleep_s, light_sleep_s=excluded.light_sleep_s,
             rem_sleep_s=excluded.rem_sleep_s, awake_s=excluded.awake_s, raw_json=excluded.raw_json
        """,
        row,
    )
    conn.commit()
    conn.close()


def upsert_wellness(row: dict):
    conn = get_conn()
    conn.execute(
        """INSERT INTO wellness
           (date, resting_hr, hrv_avg, body_battery_max, body_battery_min, stress_avg, steps)
           VALUES (:date, :resting_hr, :hrv_avg, :body_battery_max, :body_battery_min, :stress_avg, :steps)
           ON CONFLICT(date) DO UPDATE SET
             resting_hr=excluded.resting_hr, hrv_avg=excluded.hrv_avg,
             body_battery_max=excluded.body_battery_max, body_battery_min=excluded.body_battery_min,
             stress_avg=excluded.stress_avg, steps=excluded.steps
        """,
        row,
    )
    conn.commit()
    conn.close()


def replace_laps(activity_id: str, laps: list[dict]):
    """Remplace tous les tours d'une séance (supprime puis réinsère, plus simple qu'un upsert par tour)."""
    conn = get_conn()
    conn.execute("DELETE FROM laps WHERE activity_id = ?", (activity_id,))
    for lap in laps:
        conn.execute(
            """INSERT INTO laps
               (activity_id, lap_index, distance_km, duration_s, avg_pace_s_per_km, avg_hr, max_hr)
               VALUES (:activity_id, :lap_index, :distance_km, :duration_s, :avg_pace_s_per_km, :avg_hr, :max_hr)
            """,
            lap,
        )
    conn.commit()
    conn.close()


def add_race(row: dict):
    conn = get_conn()
    conn.execute(
        """INSERT INTO races (name, date, distance_km, elevation_gain, elevation_profile_json)
           VALUES (:name, :date, :distance_km, :elevation_gain, :elevation_profile_json)
        """,
        row,
    )
    conn.commit()
    conn.close()


def set_active_race(race_id: int):
    conn = get_conn()
    conn.execute("UPDATE races SET is_active = 0")
    conn.execute("UPDATE races SET is_active = 1 WHERE id = ?", (race_id,))
    conn.commit()
    conn.close()


def delete_race(race_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM races WHERE id = ?", (race_id,))
    conn.commit()
    conn.close()


def read_df(table: str):
    import pandas as pd
    conn = get_conn()
    try:
        df = pd.read_sql(f"SELECT * FROM {table}", conn)
    finally:
        conn.close()
    return df
