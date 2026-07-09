"""
providers/base.py
-----------------
Interface commune à toutes les sources de données (Garmin, Strava, ...).

Un provider renvoie des dictionnaires NORMALISÉS dont les clés correspondent
directement aux colonnes des tables de `storage.py`. Ainsi `sync.py` et
`dashboard.py` n'ont AUCUNE connaissance du format brut propre à une marque :
tout le parsing spécifique vit dans le provider concerné.
"""

from abc import ABC, abstractmethod


class DataProvider(ABC):
    #: identifiant court de la source ("garmin", "strava", ...)
    name: str = "base"
    #: la source fournit-elle des données de récupération (sommeil, HRV, ...) ?
    supports_wellness: bool = False

    @abstractmethod
    def get_activities(self, months: int = 6) -> list[dict]:
        """
        Séances de course normalisées, de la plus récente à la plus ancienne.

        Chaque dict contient les colonnes de la table `activities`
        (activity_id, date, name, distance_km, duration_s, avg_pace_s_per_km,
        avg_hr, max_hr, avg_cadence, elevation_gain, raw_json) PLUS trois
        champs annexes utilisés pour l'enrichissement météo, qui ne sont pas
        des colonnes de la base : start_lat, start_lon, start_time_local.
        """

    @abstractmethod
    def get_activity_laps(self, activity_id: str) -> list[dict] | None:
        """
        Tours normalisés d'une séance (colonnes de la table `laps`), ou None
        si le détail n'est pas disponible.
        """

    def get_wellness(self, days: int = 30) -> list[dict] | None:
        """
        Données de récupération normalisées, ou None si la source ne les
        fournit pas (ex: Strava). Par défaut : non supporté.

        Chaque entrée est un dict à deux clés :
          - "wellness" : dict pour la table `wellness` (toujours présent)
          - "sleep"    : dict pour la table `sleep`, ou None si pas de sommeil
        """
        return None
