"""
sync.py
-------
Récupère les données des N derniers jours depuis Garmin Connect
et les enregistre dans la base locale.

Usage :
    python sync.py --days 30
"""

import argparse
import datetime as dt
import sys

# Force l'UTF-8 en sortie console : évite un plantage sur Windows quand le
# terminal utilise un encodage (cp1252) qui ne sait pas afficher certains
# caractères comme "→" ou "✅".
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from providers.base import DataProvider
from providers.garmin import GarminProvider
import storage
import weather


def sync_activities(provider: DataProvider, months=6, laps_limit=15, weather_limit=80, db_path=None):
    print(f"→ Récupération des activités de course des {months} derniers mois...")
    acts = provider.get_activities(months=months)
    for a in acts:
        # `a` est déjà normalisé ; storage n'utilise que les clés-colonnes,
        # les champs annexes (start_lat/lon/time) sont ignorés à l'insertion.
        storage.upsert_activity(a, db_path=db_path)
    print(f"  {len(acts)} activités de course enregistrées.")

    # Récupère le détail des tours (allure/FC par km) sur les séances les plus
    # récentes, et la météo sur une fenêtre plus large (utile pour l'indice de
    # forme ajusté météo sur plusieurs mois), sans multiplier excessivement
    # les appels API.
    print(f"→ Récupération des tours détaillés ({laps_limit} séances) et de la météo "
          f"({weather_limit} séances)...")
    laps_synced = 0
    weather_synced = 0
    max_index = max(laps_limit, weather_limit)
    for idx, a in enumerate(acts[:max_index]):
        activity_id = a["activity_id"]

        if idx < laps_limit:
            laps_rows = provider.get_activity_laps(activity_id)
            if laps_rows:
                storage.replace_laps(activity_id, laps_rows, db_path=db_path)
                laps_synced += 1

        if idx < weather_limit:
            lat = a.get("start_lat")
            lon = a.get("start_lon")
            start_time_str = a.get("start_time_local") or ""
            if lat is not None and lon is not None and start_time_str:
                try:
                    dt_obj = dt.datetime.fromisoformat(start_time_str)
                    temp_c, feels_like_c = weather.get_weather_for_activity(lat, lon, dt_obj.date(), dt_obj.hour)
                    if temp_c is not None:
                        storage.update_activity_weather(activity_id, temp_c, feels_like_c, db_path=db_path)
                        weather_synced += 1
                except Exception:
                    pass

    print(f"  Tours détaillés enregistrés pour {laps_synced} séances.")
    print(f"  Météo enregistrée pour {weather_synced} séances.")


def sync_wellness(provider: DataProvider, days=30, db_path=None):
    if not provider.supports_wellness:
        return
    print(f"→ Récupération sommeil / FC repos / HRV / Body Battery sur {days} jours...")
    for entry in provider.get_wellness(days=days) or []:
        if entry.get("sleep"):
            storage.upsert_sleep(entry["sleep"], db_path=db_path)
        storage.upsert_wellness(entry["wellness"], db_path=db_path)
    print("  Données de récupération enregistrées.")


def sync_data(provider: DataProvider, days: int = 30,
              activities_months: int = 6, weather_limit: int = 80, db_path=None):
    """
    Récupère et enregistre les données depuis un `provider` DÉJÀ connecté.
    Séparé de la connexion (login/MFA) pour que le dashboard puisse gérer la
    double authentification en deux temps sans relancer une nouvelle session.

    Agnostique de la marque : `provider` est n'importe quel DataProvider
    (Garmin aujourd'hui, Strava demain). Le wellness n'est synchronisé que si
    la source le fournit (`supports_wellness`).

    `db_path` doit être calculé par l'appelant (ex : dashboard.py, via
    storage.get_db_path_for_user) et transmis explicitement — jamais deviné
    ici, pour éviter tout risque de mélange entre utilisateurs en cas
    d'usage simultané de l'appli.
    """
    storage.init_db(db_path=db_path)
    sync_activities(provider, months=activities_months, weather_limit=weather_limit, db_path=db_path)
    sync_wellness(provider, days=days, db_path=db_path)


def run_sync(email: str = None, password: str = None, days: int = 30,
             activities_months: int = 6, weather_limit: int = 80, db_path=None):
    """
    Point d'entrée en ligne de commande (mode local mono-utilisateur, Garmin).
    Si email/password ne sont pas fournis, se rabat sur le fichier .env.
    Si le compte a la double authentification (MFA) activée, le code est
    demandé directement dans le terminal.

    Note : le dashboard n'utilise PAS cette fonction pour le MFA. Il appelle
    provider.login() / provider.resume_with_mfa() puis sync_data(), afin de
    gérer le code de vérification en deux étapes (voir dashboard.py).
    """
    provider = GarminProvider(email=email, password=password)
    provider.login(prompt_mfa=lambda: input("Code de vérification Garmin (SMS/email) : ").strip())
    sync_data(provider, days=days, activities_months=activities_months,
              weather_limit=weather_limit, db_path=db_path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30, help="Nombre de jours d'historique à synchroniser (sommeil/FC/HRV)")
    parser.add_argument("--activities-months", type=int, default=6,
                        help="Nombre de mois d'historique à récupérer pour les activités de course")
    parser.add_argument("--weather-limit", type=int, default=80,
                        help="Nombre de séances récentes pour lesquelles récupérer la météo")
    args = parser.parse_args()

    run_sync(days=args.days, activities_months=args.activities_months, weather_limit=args.weather_limit)
    print("\n✅ Synchronisation terminée. Lance maintenant : streamlit run dashboard.py")


if __name__ == "__main__":
    main()
