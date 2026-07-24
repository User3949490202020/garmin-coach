"""
providers/strava.py
-------------------
Implémentation `DataProvider` pour Strava (OAuth2).

Sert de « hub universel » : une montre Suunto (comme Garmin, Coros, Polar…)
peut se synchroniser automatiquement vers Strava, et on lit ensuite les
activités via l'API Strava. Voir ARCHITECTURE_SUUNTO.md.

⚠️ Strava ne fournit QUE les activités — pas de sommeil / HRV / FC repos /
Body Battery. `supports_wellness = False` : l'onglet récupération sera donc
vide pour les utilisateurs Strava (dégradation propre, gérée par le dashboard).

Pré-requis (à faire une fois par le propriétaire de l'appli) :
  - Créer une application sur https://www.strava.com/settings/api
  - Récupérer client_id + client_secret → les mettre dans st.secrets / .env
    (STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET)
  - Renseigner "Authorization Callback Domain" = le domaine de l'appli déployée
"""

import json
import time
import datetime as dt

import requests

from providers.base import DataProvider

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"

# Types d'activités Strava considérés comme « course à pied ».
RUN_TYPES = {"Run", "TrailRun", "VirtualRun"}

# Types Strava considérés comme renforcement / musculation ou étirements
# (comptent dans la charge, hors course).
STRENGTH_TYPES = {"WeightTraining", "Workout", "Crossfit",
                  "HighIntensityIntervalTraining", "Yoga"}


# ----------------------------------------------------------------------
# OAuth2 (fonctions utilitaires réutilisées par le dashboard)
# ----------------------------------------------------------------------
def build_authorize_url(client_id: str, redirect_uri: str) -> str:
    """
    URL vers laquelle envoyer l'utilisateur pour autoriser l'accès à ses
    activités. `activity:read_all` est nécessaire pour lire aussi les
    activités privées.
    """
    from urllib.parse import urlencode
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "activity:read_all",
        "approval_prompt": "auto",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    """
    Échange le `code` reçu après autorisation contre des jetons.
    Retourne un dict : access_token, refresh_token, expires_at, athlete_id.
    """
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
        "athlete_id": str((data.get("athlete") or {}).get("id") or ""),
    }


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Rafraîchit un access_token expiré via le refresh_token."""
    r = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    }


def _duration_to_pace(duration_s, distance_km):
    if not distance_km:
        return None
    return duration_s / distance_km


# ----------------------------------------------------------------------
# Provider
# ----------------------------------------------------------------------
class StravaProvider(DataProvider):
    name = "strava"
    supports_wellness = False  # Strava ne fournit pas les données de récupération

    def __init__(self, tokens: dict, client_id: str, client_secret: str,
                 on_token_refresh=None):
        """
        `tokens` : {access_token, refresh_token, expires_at} déjà obtenus.
        `on_token_refresh(tokens)` : callback appelé quand les jetons sont
        rafraîchis, pour que l'appelant les re-persiste (voir dashboard.py).
        """
        self.tokens = dict(tokens)
        self.client_id = client_id
        self.client_secret = client_secret
        self.on_token_refresh = on_token_refresh

    # --- gestion du jeton (rafraîchi automatiquement avant expiration) ---
    def _access_token(self) -> str:
        # Marge de 60 s pour éviter d'utiliser un jeton qui expire pendant l'appel.
        if (self.tokens.get("expires_at", 0) - 60) <= time.time():
            new = refresh_access_token(self.client_id, self.client_secret,
                                       self.tokens["refresh_token"])
            self.tokens.update(new)
            if self.on_token_refresh:
                self.on_token_refresh(self.tokens)
        return self.tokens["access_token"]

    def _get(self, path: str, params: dict = None):
        r = requests.get(f"{API_BASE}{path}",
                         headers={"Authorization": f"Bearer {self._access_token()}"},
                         params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    # --- données normalisées ---
    def get_activities(self, months: int = 6) -> list[dict]:
        cutoff = dt.date.today() - dt.timedelta(days=int(months * 30.5))
        rows = []
        page = 1
        while True:
            batch = self._get("/athlete/activities",
                              {"per_page": 100, "page": page})
            if not batch:
                break
            reached_cutoff = False
            for a in batch:
                start_local = a.get("start_date_local") or ""
                date_str = start_local[:10]
                if date_str:
                    try:
                        if dt.date.fromisoformat(date_str) < cutoff:
                            reached_cutoff = True
                            continue
                    except ValueError:
                        pass
                if a.get("type") not in RUN_TYPES and a.get("sport_type") not in RUN_TYPES:
                    continue
                rows.append(self._normalize_activity(a))
            if reached_cutoff or len(batch) < 100:
                break
            page += 1
        return rows

    def _normalize_activity(self, a: dict) -> dict:
        distance_km = (a.get("distance") or 0) / 1000
        duration_s = a.get("moving_time") or 0
        # Cadence Strava en course = tours/min d'UNE jambe (RPM) → ×2 pour
        # obtenir des pas/min, cohérent avec Garmin.
        cadence = a.get("average_cadence")
        avg_cadence = cadence * 2 if cadence is not None else None
        latlng = a.get("start_latlng") or []
        start_lat = latlng[0] if len(latlng) == 2 else None
        start_lon = latlng[1] if len(latlng) == 2 else None
        return {
            "activity_id": str(a.get("id")),
            "date": (a.get("start_date_local") or "")[:10],
            "name": a.get("name"),
            "distance_km": distance_km,
            "duration_s": duration_s,
            "avg_pace_s_per_km": _duration_to_pace(duration_s, distance_km),
            "avg_hr": a.get("average_heartrate"),
            "max_hr": a.get("max_heartrate"),
            "avg_cadence": avg_cadence,
            "elevation_gain": a.get("total_elevation_gain"),
            "raw_json": json.dumps(a),
            # Champs annexes pour la météo :
            "start_lat": start_lat,
            "start_lon": start_lon,
            "start_time_local": a.get("start_date_local") or "",
        }

    def get_activity_laps(self, activity_id: str) -> list[dict] | None:
        try:
            detail = self._get(f"/activities/{activity_id}")
        except Exception:
            return None
        lap_list = detail.get("laps") or []
        laps_rows = []
        for i, lap in enumerate(lap_list):
            lap_distance_km = (lap.get("distance") or 0) / 1000
            lap_duration_s = lap.get("moving_time") or lap.get("elapsed_time") or 0
            laps_rows.append({
                "activity_id": activity_id,
                "lap_index": i + 1,
                "distance_km": lap_distance_km,
                "duration_s": lap_duration_s,
                "avg_pace_s_per_km": _duration_to_pace(lap_duration_s, lap_distance_km),
                "avg_hr": lap.get("average_heartrate"),
                "max_hr": lap.get("max_heartrate"),
            })
        return laps_rows or None

    def get_cross_training(self, months: int = 6) -> list[dict]:
        cutoff = dt.date.today() - dt.timedelta(days=int(months * 30.5))
        rows = []
        page = 1
        while True:
            batch = self._get("/athlete/activities",
                              {"per_page": 100, "page": page})
            if not batch:
                break
            reached_cutoff = False
            for a in batch:
                date_str = (a.get("start_date_local") or "")[:10]
                if date_str:
                    try:
                        if dt.date.fromisoformat(date_str) < cutoff:
                            reached_cutoff = True
                            continue
                    except ValueError:
                        pass
                t = a.get("sport_type") or a.get("type")
                if t not in STRENGTH_TYPES:
                    continue
                rows.append({
                    "activity_id": str(a.get("id")),
                    "date": date_str,
                    "sport": t,
                    "name": a.get("name"),
                    "duration_s": a.get("moving_time") or a.get("elapsed_time") or 0,
                    "avg_hr": a.get("average_heartrate"),
                    "raw_json": json.dumps(a),
                })
            if reached_cutoff or len(batch) < 100:
                break
            page += 1
        return rows

    # get_wellness : hérite du défaut (retourne None) — Strava n'en fournit pas.
