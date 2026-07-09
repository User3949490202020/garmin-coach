"""
providers/garmin.py
-------------------
Implémentation `DataProvider` pour Garmin Connect (via la librairie
non-officielle `garminconnect`, encapsulée dans `garmin_client.GarminClient`).

Tout le parsing spécifique au format Garmin — auparavant dans `sync.py` — vit
désormais ici, pour que `sync.py` reste agnostique de la marque.
"""

import datetime as dt
import json

from garmin_client import GarminClient
from providers.base import DataProvider


def _duration_to_pace(duration_s, distance_km):
    if not distance_km:
        return None
    return duration_s / distance_km  # secondes par km


class GarminProvider(DataProvider):
    name = "garmin"
    supports_wellness = True

    def __init__(self, email: str = None, password: str = None):
        self.client = GarminClient(email=email, password=password)

    # ------------------------------------------------------------------
    # Connexion (délègue au client bas-niveau, qui gère la MFA reprenable)
    # ------------------------------------------------------------------
    def login(self, prompt_mfa=None) -> str:
        return self.client.login(prompt_mfa=prompt_mfa)

    def resume_with_mfa(self, mfa_code: str) -> None:
        self.client.resume_with_mfa(mfa_code)

    # ------------------------------------------------------------------
    # Données normalisées
    # ------------------------------------------------------------------
    def get_activities(self, months: int = 6) -> list[dict]:
        acts = self.client.get_running_activities_since(months=months)
        rows = []
        for a in acts:
            distance_km = (a.get("distance") or 0) / 1000
            duration_s = a.get("duration") or 0
            rows.append({
                "activity_id": str(a.get("activityId")),
                "date": (a.get("startTimeLocal") or "")[:10],
                "name": a.get("activityName"),
                "distance_km": distance_km,
                "duration_s": duration_s,
                "avg_pace_s_per_km": _duration_to_pace(duration_s, distance_km),
                "avg_hr": a.get("averageHR"),
                "max_hr": a.get("maxHR"),
                "avg_cadence": a.get("averageRunningCadenceInStepsPerMinute"),
                "elevation_gain": a.get("elevationGain"),
                "raw_json": json.dumps(a),
                # Champs annexes (pas des colonnes storage) pour la météo :
                "start_lat": a.get("startLatitude"),
                "start_lon": a.get("startLongitude"),
                "start_time_local": a.get("startTimeLocal") or "",
            })
        return rows

    def get_activity_laps(self, activity_id: str) -> list[dict] | None:
        splits = self.client.get_activity_splits(activity_id)
        if not splits:
            return None
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
                "avg_pace_s_per_km": _duration_to_pace(lap_duration_s, lap_distance_km),
                "avg_hr": lap.get("averageHR"),
                "max_hr": lap.get("maxHR"),
            })
        return laps_rows or None

    def get_cross_training(self, months: int = 6) -> list[dict]:
        acts = self.client.get_cross_training_since(months=months)
        rows = []
        for a in acts:
            rows.append({
                "activity_id": str(a.get("activityId")),
                "date": (a.get("startTimeLocal") or "")[:10],
                "sport": (a.get("activityType", {}) or {}).get("typeKey") or "strength_training",
                "name": a.get("activityName"),
                "duration_s": a.get("duration") or 0,
                "avg_hr": a.get("averageHR"),
                "raw_json": json.dumps(a),
            })
        return rows

    def get_wellness(self, days: int = 30) -> list[dict]:
        today = dt.date.today()
        entries = []
        for i in range(days):
            date = today - dt.timedelta(days=i)

            # Sommeil
            sleep = self.client.get_sleep(date)
            sleep_row = None
            if sleep and sleep.get("dailySleepDTO"):
                s = sleep["dailySleepDTO"]
                # Le champ exact du score de sommeil a pu varier selon les
                # versions de l'API Garmin : on essaie plusieurs emplacements.
                sleep_score = None
                sleep_scores_obj = sleep.get("sleepScores") or s.get("sleepScores") or {}
                if isinstance(sleep_scores_obj, dict):
                    overall = sleep_scores_obj.get("overall") or {}
                    if isinstance(overall, dict):
                        sleep_score = overall.get("value")
                if sleep_score is None:
                    sleep_score = s.get("sleepScore") or sleep.get("overallSleepScore")
                sleep_row = {
                    "date": date.isoformat(),
                    "sleep_score": sleep_score,
                    "total_sleep_s": s.get("sleepTimeSeconds"),
                    "deep_sleep_s": s.get("deepSleepSeconds"),
                    "light_sleep_s": s.get("lightSleepSeconds"),
                    "rem_sleep_s": s.get("remSleepSeconds"),
                    "awake_s": s.get("awakeSleepSeconds"),
                    "raw_json": json.dumps(sleep),
                }

            # FC repos
            rhr_data = self.client.get_resting_hr(date)
            resting_hr = None
            if rhr_data:
                try:
                    resting_hr = rhr_data["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"]
                except Exception:
                    resting_hr = None

            # HRV
            hrv_data = self.client.get_hrv(date)
            hrv_avg = None
            if hrv_data:
                hrv_avg = (hrv_data.get("hrvSummary") or {}).get("lastNightAvg")

            # Body battery
            bb_data = self.client.get_body_battery(date)
            bb_max, bb_min = None, None
            if bb_data and len(bb_data) > 0:
                values = [v[1] for v in bb_data[0].get("bodyBatteryValuesArray", []) if v[1] is not None]
                if values:
                    bb_max, bb_min = max(values), min(values)

            # Stats générales (pas, stress)
            stats = self.client.get_stats(date)
            steps = stats.get("totalSteps") if stats else None
            stress_avg = stats.get("averageStressLevel") if stats else None

            entries.append({
                "wellness": {
                    "date": date.isoformat(),
                    "resting_hr": resting_hr,
                    "hrv_avg": hrv_avg,
                    "body_battery_max": bb_max,
                    "body_battery_min": bb_min,
                    "stress_avg": stress_avg,
                    "steps": steps,
                },
                "sleep": sleep_row,
            })
        return entries
