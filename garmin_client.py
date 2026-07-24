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
    def __init__(self, email: str = None, password: str = None):
        self.email = email or os.getenv("GARMIN_EMAIL")
        self.password = password or os.getenv("GARMIN_PASSWORD")
        # Le mot de passe est optionnel : une session Garmin déjà en cache
        # (token_store) suffit pour se reconnecter. Il ne redevient nécessaire
        # que si cette session a expiré (la librairie lèvera alors une erreur
        # d'authentification explicite, gérée par l'appelant).
        if not self.email:
            raise RuntimeError(
                "Identifiants Garmin manquants."
            )
        # La connexion est faite explicitement via login() (et non dans le
        # constructeur) : c'est ce qui permet de gérer la double
        # authentification (MFA) de façon "reprenable" côté web — voir plus bas.
        self.client = None

    def _token_store(self) -> str:
        # Un fichier de token distinct par personne (basé sur son email), pour
        # que plusieurs utilisateurs sur la même appli ne se marchent pas dessus.
        import hashlib
        user_hash = hashlib.sha256(self.email.strip().lower().encode()).hexdigest()[:16]
        return str(Path.home() / f".garmin_coach_tokens_{user_hash}")

    def login(self, prompt_mfa=None) -> str:
        """
        Établit la connexion Garmin.

        Retourne :
          - "ok"        : connexion réussie (session en cache réutilisée, compte
                          sans MFA, ou code MFA fourni via prompt_mfa en CLI).
          - "needs_mfa" : le compte a la double authentification activée et
                          Garmin vient d'envoyer un code par SMS/email. Il faut
                          alors demander ce code à la personne puis appeler
                          resume_with_mfa(code) sur CE MÊME objet — l'état MFA
                          (session SSO en cours) est porté par l'objet client,
                          donc en recréer un nouveau invaliderait le code.

        `prompt_mfa` : uniquement pour un usage en ligne de commande (ex :
                       input()). En mode web, laisser None : on utilise alors le
                       flux reprenable (return_on_mfa) au lieu d'un callback
                       bloquant qui ne survivrait pas à un rechargement de page.
        """
        token_store = self._token_store()
        self.client = Garmin(
            self.email, self.password,
            prompt_mfa=prompt_mfa,
            return_on_mfa=(prompt_mfa is None),
        )
        # login() tente d'abord de réutiliser la session en cache (token_store) ;
        # sinon il se reconnecte avec les identifiants.
        status, _ = self.client.login(token_store)
        if status == "needs_mfa":
            return "needs_mfa"
        self._save_tokens()
        return "ok"

    def resume_with_mfa(self, mfa_code: str) -> None:
        """
        Termine une connexion MFA initiée par login() sur ce même objet.
        Le premier argument (client_state) est ignoré par cette version de
        garminconnect : l'état est déjà stocké sur l'objet client.
        """
        if self.client is None:
            raise RuntimeError("Aucune connexion à reprendre : appelle login() d'abord.")
        self.client.resume_login(None, mfa_code)
        self._save_tokens()

    def _save_tokens(self) -> None:
        # Persiste la session pour éviter de redemander le MFA à chaque sync.
        # (self.client est le wrapper Garmin ; self.client.client est le client
        # bas-niveau qui expose dump().)
        import contextlib
        with contextlib.suppress(Exception):
            self.client.client.dump(self._token_store())

    # ------------------------------------------------------------------
    # Activités (séances de course)
    # ------------------------------------------------------------------
    def get_activities(self, limit=50):
        """Retourne les dernières activités (toutes disciplines)."""
        return self.client.get_activities(0, limit)

    def get_running_activities(self, limit=50):
        acts = self.get_activities(limit=limit)
        return [a for a in acts if "running" in (a.get("activityType", {}).get("typeKey", "") or "")]

    def _activities_since(self, match, months=6, max_activities=400, page_size=50):
        """
        Récupère les activités correspondant au filtre `match(activity)` en
        remontant page par page jusqu'à atteindre une activité plus vieille que
        `months` mois (au lieu de se limiter à un nombre fixe d'activités, ce
        qui louperait les séances anciennes si tu t'entraînes souvent).
        """
        cutoff = dt.date.today() - dt.timedelta(days=int(months * 30.5))
        results = []
        start = 0
        while len(results) < max_activities:
            batch = self.client.get_activities(start, page_size)
            if not batch:
                break
            results.extend(a for a in batch if match(a))

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

    @staticmethod
    def _type_key(a):
        return (a.get("activityType", {}) or {}).get("typeKey", "") or ""

    def get_running_activities_since(self, months=6, max_activities=400, page_size=50):
        """Séances de course (course à pied) des `months` derniers mois."""
        return self._activities_since(
            lambda a: "running" in self._type_key(a),
            months=months, max_activities=max_activities, page_size=page_size,
        )

    def get_cross_training_since(self, months=6, max_activities=400, page_size=50):
        """
        Séances de renforcement / musculation ET de yoga/étirements des
        `months` derniers mois. Garmin nomme ça `strength_training` (ou
        `functional_strength_training`) et `yoga`.
        """
        return self._activities_since(
            lambda a: any(k in self._type_key(a) for k in ("strength", "yoga")),
            months=months, max_activities=max_activities, page_size=page_size,
        )

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
