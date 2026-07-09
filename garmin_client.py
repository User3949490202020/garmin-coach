"""
garmin_client.py
----------------
Wrapper autour de la librairie non-officielle `garminconnect`.
Gère la connexion (avec cache de session pour éviter de se reconnecter
à chaque fois) et expose des méthodes simples pour récupérer :
  - la liste des activités (courses)
  - le détail d'une activité (splits, FC, allure, cadence)
  - le sommeil
  - la fréquence cardiaque au repos
  - la variabilité cardiaque (HRV)
  - le Body Battery (niveau d'énergie/récupération Garmin)
"""

import os
import datetime as dt
from pathlib import Path

from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()


class GarminClient:
    def __init__(self, email: str = None, password: str = None, mfa_code: str = None):
        self.email = email or os.getenv("GARMIN_EMAIL")
        self.password = password or os.getenv("GARMIN_PASSWORD")
        if not self.email or not self.password:
            raise RuntimeError(
                "Identifiants Garmin manquants."
            )
        # Si le compte a la double authentification (MFA) activée, Garmin
        # demande un code (SMS/email) au moment de la connexion. On ne peut
        # le transmettre qu'AVANT d'appeler login() : s'il n'est pas fourni,
        # on laisse la librairie lever son erreur "MFA Required...", que
        # l'appelant (sync.py / dashboard.py) attrape pour redemander le code.
        prompt_mfa = (lambda: mfa_code) if mfa_code else None
        self.client = Garmin(self.email, self.password, prompt_mfa=prompt_mfa)
        self._login()

    def _login(self):
        # Un fichier de token distinct par personne (basé sur son email), pour
        # que plusieurs utilisateurs sur la même appli ne se marchent pas dessus.
        import hashlib
        user_hash = hashlib.sha256(self.email.strip().lower().encode()).hexdigest()[:16]
        token_store = Path.home() / f".garmin_coach_tokens_{user_hash}"
        try:
            # Essaie d'abord de réutiliser une session déjà connue
            self.client.login(str(token_store))
        except Exception:
            # Sinon connexion complète + sauvegarde du token
            self.client.login()
            self.client.garth.dump(str(token_store))

    # ------------------------------------------------------------------
    # Activités (séances de course)
    # ------------------------------------------------------------------
    def get_activities(self, limit=50):
        """Retourne les dernières activités (toutes disciplines)."""
        return self.client.get_activities(0, limit)

    def get_running_activities(self, limit=50):
        acts = self.get_activities(limit=limit)
        return [a for a in acts if "running" in (a.get("activityType", {}).get("typeKey", "") or "")]

    def get_running_activities_since(self, months=6, max_activities=400, page_size=50):
        """
        Récupère les activités de course en remontant page par page jusqu'à
        atteindre une activité plus vieille que `months` mois (au lieu de se
        limiter à un nombre fixe d'activités, ce qui loupait les courses
        anciennes si tu cours souvent).
        """
        cutoff = dt.date.today() - dt.timedelta(days=int(months * 30.5))
        results = []
        start = 0
        while len(results) < max_activities:
            batch = self.client.get_activities(start, page_size)
            if not batch:
                break
            results.extend(a for a in batch if "running" in (a.get("activityType", {}).get("typeKey", "") or ""))

            oldest_date_str = (batch[-1].get("startTimeLocal") or "")[:10]
            reached_cutoff = False
            if oldest_date_str:
                try:
                    if dt.date.fromisoformat(oldest_date_str) < cutoff:
                        reached_cutoff = True
                except ValueError:
                    pass

            if reached_cutoff or len(batch) < page_size:
                break
            start += page_size

        return results[:max_activities]

    def get_activity_splits(self, activity_id):
        """Détail des tours/splits d'une séance : allure, FC, cadence par km."""
        try:
            return self.client.get_activity_splits(activity_id)
        except Exception:
            return None

    def get_activity_hr_zones(self, activity_id):
        try:
            return self.client.get_activity_hr_in_timezones(activity_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Sommeil / récupération
    # ------------------------------------------------------------------
    def get_sleep(self, date: dt.date):
        try:
            return self.client.get_sleep_data(date.isoformat())
        except Exception:
            return None

    def get_resting_hr(self, date: dt.date):
        try:
            data = self.client.get_rhr_day(date.isoformat())
            return data
        except Exception:
            return None

    def get_hrv(self, date: dt.date):
        try:
            return self.client.get_hrv_data(date.isoformat())
        except Exception:
            return None

    def get_body_battery(self, date: dt.date):
        try:
            return self.client.get_body_battery(date.isoformat(), date.isoformat())
        except Exception:
            return None

    def get_stats(self, date: dt.date):
        """Stats journalières générales : pas, stress, calories, etc."""
        try:
            return self.client.get_stats(date.isoformat())
        except Exception:
            return None
