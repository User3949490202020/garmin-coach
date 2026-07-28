"""
storage.py
----------
Petite base SQLite locale pour stocker l'historique.

IMPORTANT (multi-utilisateurs) : chaque fonction accepte un paramètre
optionnel `db_path`. Sans lui, on utilise la base par défaut (mode local
mono-utilisateur avec .env). En mode hébergé multi-utilisateurs, le
dashboard calcule le chemin propre à chaque personne connectée et le
transmet explicitement à chaque appel — jamais via une variable globale
partagée, qui serait dangereuse si plusieurs personnes utilisent l'appli
en même temps (risque de mélanger les données de deux utilisateurs).
"""

import sqlite3
import json
import hashlib
from pathlib import Path

_BASE_DIR = Path(__file__).parent
DEFAULT_DB_PATH = _BASE_DIR / "garmin_coach.db"

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

CREATE TABLE IF NOT EXISTS cross_training (
    activity_id TEXT PRIMARY KEY,
    date TEXT,
    sport TEXT,
    name TEXT,
    duration_s REAL,
    avg_hr REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS strava_tokens (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    access_token TEXT,
    refresh_token TEXT,
    expires_at INTEGER,
    athlete_id TEXT
);

CREATE TABLE IF NOT EXISTS manual_notes (
    key TEXT PRIMARY KEY,
    value REAL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS text_notes (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_history (
    ts TEXT,
    role TEXT,
    content TEXT
);

CREATE TABLE IF NOT EXISTS shoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    brand TEXT,
    km_target REAL DEFAULT 700,
    retired INTEGER DEFAULT 0,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS activity_shoes (
    activity_id TEXT PRIMARY KEY,
    shoe_id INTEGER
);
"""

MIGRATIONS = {
    "activities": [
        ("temp_c", "REAL"),
        ("feels_like_c", "REAL"),
    ],
    "sleep": [
        ("nap_s", "REAL"),
    ],
}


def get_db_path_for_user(email: str) -> Path:
    """Calcule (sans rien modifier globalement) le chemin de la base propre à cette personne."""
    data_dir = _BASE_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    user_hash = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]
    return data_dir / f"garmin_coach_{user_hash}.db"


def get_conn(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path=None):
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()

    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for col_name, col_type in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
    conn.commit()
    conn.close()


def upsert_activity(row: dict, db_path=None):
    conn = get_conn(db_path)
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


def update_activity_weather(activity_id: str, temp_c, feels_like_c, db_path=None):
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE activities SET temp_c = ?, feels_like_c = ? WHERE activity_id = ?",
        (temp_c, feels_like_c, activity_id),
    )
    conn.commit()
    conn.close()


def upsert_sleep(row: dict, db_path=None):
    row = {"nap_s": None, **row}  # tolère les anciennes sources sans sieste
    conn = get_conn(db_path)
    # Une nuit corrigée À LA MAIN (montre déchargée, mesure aberrante...) est
    # marquée "override" : la synchro Garmin ne doit plus l'écraser avec sa
    # donnée incomplète. Seule une nouvelle saisie manuelle peut la remplacer.
    incoming_manual = "manual" in str(row.get("raw_json") or "")
    if not incoming_manual:
        existing = conn.execute(
            "SELECT raw_json FROM sleep WHERE date = ?", (row["date"],)
        ).fetchone()
        if existing and "override" in str(existing[0] or ""):
            conn.close()
            return
    conn.execute(
        """INSERT INTO sleep
           (date, sleep_score, total_sleep_s, deep_sleep_s, light_sleep_s, rem_sleep_s, awake_s, nap_s, raw_json)
           VALUES (:date, :sleep_score, :total_sleep_s, :deep_sleep_s, :light_sleep_s, :rem_sleep_s, :awake_s, :nap_s, :raw_json)
           ON CONFLICT(date) DO UPDATE SET
             sleep_score=excluded.sleep_score, total_sleep_s=excluded.total_sleep_s,
             deep_sleep_s=excluded.deep_sleep_s, light_sleep_s=excluded.light_sleep_s,
             rem_sleep_s=excluded.rem_sleep_s, awake_s=excluded.awake_s,
             nap_s=excluded.nap_s, raw_json=excluded.raw_json
        """,
        row,
    )
    conn.commit()
    conn.close()


def save_manual_note(key: str, value: float, db_path=None):
    """Enregistre une note saisie à la main (ex: note sommeil 0-100), datée du jour."""
    import datetime as _dt
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO manual_notes (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, _dt.date.today().isoformat()),
    )
    conn.commit()
    conn.close()


def read_manual_note(key: str, db_path=None):
    """Retourne (valeur, date_de_saisie) ou None si jamais saisie."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM manual_notes WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


def save_text_note(key: str, value: str, db_path=None):
    """Enregistre un texte libre persistant (ex: objectif de l'athlète), daté du jour."""
    import datetime as _dt
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO text_notes (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
        (key, value, _dt.date.today().isoformat()),
    )
    conn.commit()
    conn.close()


def read_text_note(key: str, db_path=None):
    """Retourne (texte, date_de_saisie) ou None si jamais saisi."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT value, updated_at FROM text_notes WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


# ----------------------------------------------------------------------
# Historique de conversation avec le coach IA (par utilisateur)
# ----------------------------------------------------------------------
def append_chat_message(role: str, content: str, db_path=None):
    import datetime as _dt
    conn = get_conn(db_path)
    conn.execute("INSERT INTO chat_history (ts, role, content) VALUES (?, ?, ?)",
                 (_dt.datetime.now().isoformat(), role, content))
    # Garde la table légère : on ne conserve que les 200 derniers messages
    conn.execute("""DELETE FROM chat_history WHERE rowid NOT IN
                    (SELECT rowid FROM chat_history ORDER BY ts DESC LIMIT 200)""")
    conn.commit()
    conn.close()


def read_chat_history(hours: int = 12, db_path=None) -> list[dict]:
    """Messages des `hours` dernières heures, du plus ancien au plus récent."""
    import datetime as _dt
    cutoff = (_dt.datetime.now() - _dt.timedelta(hours=hours)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT role, content FROM chat_history WHERE ts >= ? ORDER BY ts ASC",
            (cutoff,)).fetchall()
    finally:
        conn.close()
    return [{"role": r[0], "content": r[1]} for r in rows]


def clear_chat_history(db_path=None):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM chat_history")
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# Parc de chaussures (par utilisateur)
# ----------------------------------------------------------------------
def add_shoe(name: str, brand: str = "", km_target: float = 700, db_path=None):
    import datetime as _dt
    conn = get_conn(db_path)
    conn.execute("INSERT INTO shoes (name, brand, km_target, created_at) VALUES (?, ?, ?, ?)",
                 (name, brand, km_target, _dt.date.today().isoformat()))
    conn.commit()
    conn.close()


def set_shoe_retired(shoe_id: int, retired: bool, db_path=None):
    conn = get_conn(db_path)
    conn.execute("UPDATE shoes SET retired = ? WHERE id = ?", (1 if retired else 0, shoe_id))
    conn.commit()
    conn.close()


def assign_shoe(activity_id: str, shoe_id, db_path=None):
    """Assigne (ou retire si shoe_id None) une paire à une séance."""
    conn = get_conn(db_path)
    if shoe_id is None:
        conn.execute("DELETE FROM activity_shoes WHERE activity_id = ?", (str(activity_id),))
    else:
        conn.execute("""INSERT INTO activity_shoes (activity_id, shoe_id) VALUES (?, ?)
                        ON CONFLICT(activity_id) DO UPDATE SET shoe_id=excluded.shoe_id""",
                     (str(activity_id), int(shoe_id)))
    conn.commit()
    conn.close()


# ----------------------------------------------------------------------
# Sessions de reconnexion automatique (base PARTAGÉE, un jeton aléatoire
# par personne connectée — le mot de passe n'y est JAMAIS stocké)
# ----------------------------------------------------------------------
def _sessions_conn():
    data_dir = _BASE_DIR / "data"
    data_dir.mkdir(exist_ok=True)
    conn = sqlite3.connect(data_dir / "sessions.db")
    conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, source TEXT, ident TEXT, created_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS usage_sessions (
        session_id TEXT PRIMARY KEY, ident TEXT, source TEXT,
        started_at TEXT, last_seen TEXT)""")
    return conn


def touch_usage(session_id: str, ident: str, source: str):
    """
    Trace d'usage anonyme-légère (identifiant + horodatages uniquement) :
    appelée à chaque affichage, elle crée la session de visite au premier
    passage puis met à jour "vu pour la dernière fois". La durée d'une visite
    = last_seen - started_at.
    """
    import datetime as _dt
    now = _dt.datetime.now().isoformat(timespec="seconds")
    conn = _sessions_conn()
    conn.execute(
        """INSERT INTO usage_sessions (session_id, ident, source, started_at, last_seen)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(session_id) DO UPDATE SET last_seen=excluded.last_seen""",
        (session_id, ident, source, now, now),
    )
    conn.commit()
    conn.close()


def read_usage():
    """Toutes les visites enregistrées (pour le panneau admin)."""
    import pandas as pd
    conn = _sessions_conn()
    try:
        df = pd.read_sql("SELECT * FROM usage_sessions", conn)
    finally:
        conn.close()
    return df


def create_session_token(source: str, ident: str) -> str:
    """Crée un jeton de reconnexion (source: 'garmin'/'strava', ident: email/athlete_id)."""
    import uuid
    import datetime as _dt
    token = uuid.uuid4().hex
    conn = _sessions_conn()
    conn.execute("INSERT INTO sessions (token, source, ident, created_at) VALUES (?, ?, ?, ?)",
                 (token, source, ident, _dt.date.today().isoformat()))
    conn.commit()
    conn.close()
    return token


def read_session_token(token: str):
    """Retourne (source, ident) ou None si jeton inconnu."""
    if not token:
        return None
    conn = _sessions_conn()
    try:
        row = conn.execute("SELECT source, ident FROM sessions WHERE token = ?",
                           (token,)).fetchone()
    finally:
        conn.close()
    return (row[0], row[1]) if row else None


def delete_session_token(token: str):
    conn = _sessions_conn()
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def upsert_wellness(row: dict, db_path=None):
    conn = get_conn(db_path)
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


def upsert_cross_training(row: dict, db_path=None):
    """Enregistre une séance de renfo/muscu (discipline hors course à pied)."""
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO cross_training
           (activity_id, date, sport, name, duration_s, avg_hr, raw_json)
           VALUES (:activity_id, :date, :sport, :name, :duration_s, :avg_hr, :raw_json)
           ON CONFLICT(activity_id) DO UPDATE SET
             date=excluded.date, sport=excluded.sport, name=excluded.name,
             duration_s=excluded.duration_s, avg_hr=excluded.avg_hr, raw_json=excluded.raw_json
        """,
        row,
    )
    conn.commit()
    conn.close()


def replace_laps(activity_id: str, laps: list[dict], db_path=None):
    """Remplace tous les tours d'une séance (supprime puis réinsère, plus simple qu'un upsert par tour)."""
    conn = get_conn(db_path)
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


def add_race(row: dict, db_path=None):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO races (name, date, distance_km, elevation_gain, elevation_profile_json)
           VALUES (:name, :date, :distance_km, :elevation_gain, :elevation_profile_json)
        """,
        row,
    )
    conn.commit()
    conn.close()


def set_active_race(race_id: int, db_path=None):
    conn = get_conn(db_path)
    conn.execute("UPDATE races SET is_active = 0")
    conn.execute("UPDATE races SET is_active = 1 WHERE id = ?", (race_id,))
    conn.commit()
    conn.close()


def delete_race(race_id: int, db_path=None):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM races WHERE id = ?", (race_id,))
    conn.commit()
    conn.close()


def save_strava_tokens(tokens: dict, db_path=None):
    """Enregistre (ou met à jour) les jetons Strava pour cette base utilisateur."""
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO strava_tokens (id, access_token, refresh_token, expires_at, athlete_id)
           VALUES (1, :access_token, :refresh_token, :expires_at, :athlete_id)
           ON CONFLICT(id) DO UPDATE SET
             access_token=excluded.access_token, refresh_token=excluded.refresh_token,
             expires_at=excluded.expires_at, athlete_id=excluded.athlete_id
        """,
        {
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": tokens.get("expires_at"),
            "athlete_id": tokens.get("athlete_id"),
        },
    )
    conn.commit()
    conn.close()


def read_strava_tokens(db_path=None):
    """Retourne les jetons Strava enregistrés (dict) ou None si absent."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT access_token, refresh_token, expires_at, athlete_id FROM strava_tokens WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "access_token": row[0],
        "refresh_token": row[1],
        "expires_at": row[2],
        "athlete_id": row[3],
    }


def read_df(table: str, db_path=None):
    import pandas as pd
    conn = get_conn(db_path)
    try:
        df = pd.read_sql(f"SELECT * FROM {table}", conn)
    finally:
        conn.close()
    # Filet de sécurité pour les bases anciennes ou abîmées : si une table
    # contient une colonne en double (ex : deux colonnes "date", héritées d'un
    # schéma antérieur), df["date"] renverrait un DataFrame au lieu d'une Series
    # et ferait planter les traitements en aval — typiquement
    # `wellness["date"] = pd.to_datetime(...)` avec
    # "ValueError: Columns must be same length as key". On ne conserve que la
    # première occurrence de chaque colonne (sans effet sur une base saine).
    df = df.loc[:, ~df.columns.duplicated()]
    return df
