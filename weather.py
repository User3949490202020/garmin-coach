"""
weather.py
----------
Récupère la météo historique (température, ressenti) au moment et à
l'endroit d'une séance, via l'API gratuite Open-Meteo (aucune clé requise).

Nécessite une connexion internet au moment de la synchronisation.
Si l'appel échoue (pas de réseau, séance sans coordonnées GPS...), on
retourne simplement (None, None) sans faire planter la synchronisation.
"""

import datetime as dt
import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def get_weather_for_activity(lat, lon, date: dt.date, hour: int):
    """Retourne (température °C, ressenti °C) à l'heure la plus proche de l'activité."""
    if lat is None or lon is None:
        return None, None
    try:
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": date.isoformat(),
            "end_date": date.isoformat(),
            "hourly": "temperature_2m,apparent_temperature",
            "timezone": "auto",
        }
        resp = requests.get(ARCHIVE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        hourly = data.get("hourly", {})
        temps = hourly.get("temperature_2m", [])
        feels = hourly.get("apparent_temperature", [])
        if not temps:
            return None, None
        idx = min(max(hour, 0), len(temps) - 1)
        temp_c = temps[idx]
        feels_like_c = feels[idx] if feels and idx < len(feels) else None
        return temp_c, feels_like_c
    except Exception:
        return None, None
