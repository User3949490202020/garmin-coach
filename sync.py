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
import json
import sys

# Force l'UTF-8 en sortie console : évite un plantage sur Windows quand le
# terminal utilise un encodage (cp1252) qui ne sait pas afficher certains
# caractères comme "→" ou "✅".
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from garmin_client import GarminClient
import storage
import weather


def duration_to_pace(duration_s, distance_km):
    if not distance_km:
        return None
    return duration_s / distance_km  # secondes par km


def sync_activities(client: GarminClient, months=6, laps_limit=15, weather_limit=80, db_path=None):
    print(f"→ Récupération des activités de course des {months} derniers mois...")
    acts = client.get_running_activities_since(months=months)
    for a in acts:
        distance_km = (a.get("distance") or 0) / 1000
        duration_s = a.get("duration") or 0
        row = {
            "activity_id": str(a.get("activityId")),
            "date": (a.get("startTimeLocal") or "")[:10],
            "name": a.get("activityName"),
            "distance_km": distance_km,
            "duration_s": duration_s,
            "avg_pace_s_per_km": duration_to_pace(duration_s, distance_km),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "avg_cadence": a.get("averageRunningCadenceInStepsPerMinute"),
            "elevation_gain": a.get("elevationGain"),
            "raw_json": json.dumps(a),
        }
        storage.upsert_activity(row, db_path=db_path)
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
        activity_id = str(a.get("activityId"))

        if idx < laps_limit:
            splits = client.get_activity_splits(activity_id)
            if splits:
                lap_list = splits.get("lapDTOs") or splits.get("laps") or []
                laps_rows = []
                for i, lap in enumerate(lap_list):
                    lap_distance_km = (lap.get("distance") or 0) / 1000
                    lap_duration_s = lap.get("duration") or lap.get("movingDuration") or 0
                    laps_rows.append({
                        "activity_id": activity_id,
                        "lap_index": i + 1,
                        "distance_km": lap_distance_km,
                        "duration_s": lap_duration_s,
                        "avg_pace_s_per_km": duration_to_pace(lap_duration_s, lap_distance_km),
                        "avg_hr": lap.get("averageHR"),
                        "max_hr": lap.get("maxHR"),
                    })
                if laps_rows:
                    storage.replace_laps(activity_id, laps_rows, db_path=db_path)
                    laps_synced += 1

        if idx < weather_limit:
            lat = a.get("startLatitude")
            lon = a.get("startLongitude")
            start_time_str = a.get("startTimeLocal") or ""
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


def sync_wellness(client: GarminClient, days=30, db_path=None):
    print(f"→ Récupération sommeil / FC repos / HRV / Body Battery sur {days} jours...")
    today = dt.date.today()
    for i in range(days):
        date = today - dt.timedelta(days=i)

        # Sommeil
        sleep = client.get_sleep(date)
        if sleep and sleep.get("dailySleepDTO"):
            s = sleep["dailySleepDTO"]

            # Le champ exact du score de sommeil a pu varier selon les versions
            # de l'API Garmin : on essaie plusieurs emplacements connus avant d'abandonner.
            sleep_score = None
            sleep_scores_obj = sleep.get("sleepScores") or s.get("sleepScores") or {}
            if isinstance(sleep_scores_obj, dict):
                overall = sleep_scores_obj.get("overall") or {}
                if isinstance(overall, dict):
                    sleep_score = overall.get("value")
            if sleep_score is None:
                sleep_score = s.get("sleepScore") or sleep.get("overallSleepScore")

            storage.upsert_sleep({
                "date": date.isoformat(),
                "sleep_score": sleep_score,
                "total_sleep_s": s.get("sleepTimeSeconds"),
                "deep_sleep_s": s.get("deepSleepSeconds"),
                "light_sleep_s": s.get("lightSleepSeconds"),
                "rem_sleep_s": s.get("remSleepSeconds"),
                "awake_s": s.get("awakeSleepSeconds"),
                "raw_json": json.dumps(sleep),
            }, db_path=db_path)

        # FC repos
        rhr_data = client.get_resting_hr(date)
        resting_hr = None
        if rhr_data:
            try:
                resting_hr = rhr_data["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"]
            except Exception:
                resting_hr = None

        # HRV
        hrv_data = client.get_hrv(date)
        hrv_avg = None
        if hrv_data:
            hrv_avg = (hrv_data.get("hrvSummary") or {}).get("lastNightAvg")

        # Body battery
        bb_data = client.get_body_battery(date)
        bb_max, bb_min = None, None
        if bb_data and len(bb_data) > 0:
            values = [v[1] for v in bb_data[0].get("bodyBatteryValuesArray", []) if v[1] is not None]
            if values:
                bb_max, bb_min = max(values), min(values)

        # Stats générales (pas, stress)
        stats = client.get_stats(date)
        steps = stats.get("totalSteps") if stats else None
        stress_avg = stats.get("averageStressLevel") if stats else None

        storage.upsert_wellness({
            "date": date.isoformat(),
            "resting_hr": resting_hr,
            "hrv_avg": hrv_avg,
            "body_battery_max": bb_max,
            "body_battery_min": bb_min,
            "stress_avg": stress_avg,
            "steps": steps,
        }, db_path=db_path)
    print("  Données de récupération enregistrées.")


def sync_data(client: GarminClient, days: int = 30,
              activities_months: int = 6, weather_limit: int = 80, db_path=None):
    """
    Récupère et enregistre les données depuis un `client` DÉJÀ connecté.
    Séparé de la connexion (login/MFA) pour que le dashboard puisse gérer la
    double authentification en deux temps sans relancer une nouvelle session.

    `db_path` doit être calculé par l'appelant (ex : dashboard.py, via
    storage.get_db_path_for_user) et transmis explicitement — jamais deviné
    ici, pour éviter tout risque de mélange entre utilisateurs en cas
    d'usage simultané de l'appli.
    """
    storage.init_db(db_path=db_path)
    sync_activities(client, months=activities_months, weather_limit=weather_limit, db_path=db_path)
    sync_wellness(client, days=days, db_path=db_path)


def run_sync(email: str = None, password: str = None, days: int = 30,
             activities_months: int = 6, weather_limit: int = 80, db_path=None):
    """
    Point d'entrée en ligne de commande (mode local mono-utilisateur).
    Si email/password ne sont pas fournis, se rabat sur le fichier .env.
    Si le compte a la double authentification (MFA) activée, le code est
    demandé directement dans le terminal.

    Note : le dashboard n'utilise PAS cette fonction pour le MFA. Il appelle
    client.login() / client.resume_with_mfa() puis sync_data(), afin de gérer
    le code de vérification en deux étapes (voir dashboard.py).
    """
    client = GarminClient(email=email, password=password)
    client.login(prompt_mfa=lambda: input("Code de vérification Garmin (SMS/email) : ").strip())
    sync_data(client, days=days, activities_months=activities_months,
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
