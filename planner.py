"""
planner.py
----------
Génère un programme d'entraînement course à pied sur ~3 mois, personnalisé à
partir des données réelles de l'athlète :
  - volume de départ = sa moyenne glissante 4 semaines (jamais un plan "hors sol")
  - allures cibles dérivées de sa prédiction 10K (formule de Riegel)
  - progression prudente (+6 %/sem), semaine de récupération toutes les 4 semaines
  - si une course objectif est renseignée : affûtage 2 semaines avant + semaine de course

Le plan est recalculé à la volée à chaque affichage à partir des réglages
persistés (nb séances/semaine, jour de la sortie longue, objectif) — on ne
stocke jamais le plan lui-même, donc il s'adapte automatiquement aux nouvelles
synchros (volume qui monte, forme qui change).
"""

import datetime as dt
import pandas as pd

FR_DAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# Jours d'entraînement types selon le nombre de séances/semaine (0=lundi ... 6=dimanche).
# La sortie longue occupe toujours le dernier jour du motif (remplacé par le
# jour choisi par l'utilisateur).
DAY_PATTERNS = {
    1: [6],
    2: [2, 6],
    3: [1, 3, 6],
    4: [1, 3, 5, 6],
    5: [0, 1, 3, 5, 6],
    6: [0, 1, 3, 4, 5, 6],
    7: [0, 1, 2, 3, 4, 5, 6],
}

# Menus de séances de qualité (rotation pour varier les semaines)
VMA_MENU = [
    "10 × 400 m à allure VMA, récup 1 min trot",
    "6 × 800 m à allure 5K, récup 1 min 30 trot",
    "12 × 200 m en côte ou à VMA, récup descente/45 s",
    "2 × (6 × 300 m) vite, récup 45 s / 3 min entre blocs",
]
SEUIL_MENU = [
    "3 × 8 min à allure seuil, récup 2 min trot",
    "2 × 12 min à allure seuil, récup 3 min trot",
    "20 min continues à allure seuil (tempo)",
    "4 × 6 min à allure seuil, récup 90 s trot",
]


def _fmt_pace(sec_per_km):
    if sec_per_km is None or pd.isna(sec_per_km):
        return "aux sensations"
    return f"{int(sec_per_km // 60)}:{int(sec_per_km % 60):02d}/km"


def target_paces(predictions: dict) -> dict:
    """
    Allures cibles dérivées de la prédiction 10K (elle-même issue de la
    meilleure perf récente). Sans prédiction (pas assez de données), tout
    est "aux sensations".
    """
    if not predictions or "10K" not in predictions:
        return {"facile": None, "longue": None, "seuil": None, "vma": None}
    pace10 = predictions["10K"]["temps_s"] / 10  # s/km
    return {
        "facile": pace10 + 65,             # endurance fondamentale, conversation possible
        "longue": pace10 + 50,             # un poil plus soutenu que le footing
        "seuil": pace10 + 12,              # ~allure semi/10K longue
        "vma": pace10 * (5 / 10) ** 0.06,  # ~allure 5K (Riegel)
    }


def _week_sessions(nb_seances: int, longrun_day: int, week_km: float,
                   week_type: str, week_index: int, paces: dict) -> list[dict]:
    """Détail des séances d'une semaine donnée (jour, type, distance, allure, contenu)."""
    days = list(DAY_PATTERNS[min(max(nb_seances, 1), 7)])
    # place la sortie longue le jour choisi par l'utilisateur
    days = [d for d in days[:-1] if d != longrun_day] + [longrun_day]
    days = sorted(set(days))[:nb_seances] if len(set(days)) >= nb_seances else sorted(set(days))
    if longrun_day not in days:
        days[-1] = longrun_day
    days = sorted(days)

    # Cas particulier : une seule séance par semaine = une sortie unique
    n = len(days)
    if n == 1:
        return [{
            "jour": days[0], "type": "Sortie unique", "km": round(week_km, 1),
            "allure": _fmt_pace(paces["longue"]),
            "contenu": "Ta seule séance de la semaine : allure régulière et confortable, "
                       "profites-en pour prendre du plaisir.",
            "couleur": "🟠",
        }]

    # Répartition du volume : la part de la sortie longue grandit quand il y a
    # peu de séances (avec 2 séances/sem, la longue doit rester LA grosse
    # séance), qualité ~18 %, le reste en footings.
    long_share = {2: 0.55, 3: 0.45}.get(n, 0.35)
    long_km = round(min(week_km * long_share, 24), 1)
    quality_slots = 0 if n <= 2 or week_type in ("Course",) else (2 if n >= 5 else 1)
    quality_km = round(week_km * 0.18, 1)
    remaining = max(week_km - long_km - quality_slots * quality_km, 0)
    easy_slots = max(n - 1 - quality_slots, 0)
    easy_km = round(remaining / easy_slots, 1) if easy_slots else 0

    sessions = []
    quality_used = 0
    for i, d in enumerate(days):
        if d == longrun_day and n > 1:
            sessions.append({
                "jour": d, "type": "Sortie longue", "km": long_km,
                "allure": _fmt_pace(paces["longue"]),
                "contenu": "Allure régulière, hydratation ; c'est la séance qui construit l'endurance.",
                "couleur": "🟠",
            })
        elif quality_used < quality_slots:
            # alterne VMA / seuil ; varie le contenu selon la semaine
            if quality_used == 0 and week_type != "Récupération":
                menu = VMA_MENU[week_index % len(VMA_MENU)]
                sessions.append({
                    "jour": d, "type": "Qualité — VMA", "km": quality_km,
                    "allure": _fmt_pace(paces["vma"]),
                    "contenu": f"Échauffement 15 min + {menu} + retour au calme 10 min.",
                    "couleur": "🔴",
                })
            else:
                menu = SEUIL_MENU[week_index % len(SEUIL_MENU)]
                sessions.append({
                    "jour": d, "type": "Qualité — Seuil", "km": quality_km,
                    "allure": _fmt_pace(paces["seuil"]),
                    "contenu": f"Échauffement 15 min + {menu} + retour au calme 10 min.",
                    "couleur": "🔴",
                })
            quality_used += 1
        else:
            sessions.append({
                "jour": d, "type": "Footing facile", "km": easy_km,
                "allure": _fmt_pace(paces["facile"]),
                "contenu": "Allure conversationnelle — si tu ne peux pas parler, ralentis.",
                "couleur": "🟢",
            })

    # Semaine de récupération : tout en facile, pas de VMA
    if week_type == "Récupération":
        for s in sessions:
            if s["type"].startswith("Qualité — VMA"):
                s.update({"type": "Qualité — Seuil léger", "allure": _fmt_pace(paces["seuil"]),
                          "contenu": "Version allégée : 2 × 6 min au seuil max, le reste très facile.",
                          "couleur": "🟠"})
    return sessions


def build_plan(activities: pd.DataFrame, nb_seances: int, longrun_day: int = 6,
               race: dict = None, horizon_weeks: int = 13,
               predictions: dict = None) -> dict:
    """
    Construit le plan semaine par semaine.
    `race` : {"name", "date" (Timestamp), "distance_km"} ou None.
    Retourne {"weeks": [...], "paces": {...}, "base_km": float}.
    """
    paces = target_paces(predictions or {})

    # Volume de départ = moyenne réelle des 4 dernières semaines (plancher 10 km)
    base_km = 10.0
    if activities is not None and not activities.empty:
        df = activities.copy()
        df["date"] = pd.to_datetime(df["date"])
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=28)
        recent_km = df[df["date"] >= cutoff]["distance_km"].sum()
        base_km = max(round(recent_km / 4, 1), 10.0)

    # Garde-fou : le volume hebdo doit rester absorbable avec ce nombre de
    # séances (~13 km max par séance en moyenne). Quelqu'un qui passe de 5 à
    # 2 séances/semaine ne doit pas se voir prescrire le même kilométrage.
    freq_cap = max(nb_seances, 1) * 13.0
    base_km = min(base_km, freq_cap)

    today = pd.Timestamp.now().normalize()
    current_week_start = today - pd.Timedelta(days=today.weekday())

    race_week_start = None
    if race and race.get("date") is not None:
        race_date = pd.Timestamp(race["date"]).normalize()
        if race_date >= current_week_start:
            race_week_start = race_date - pd.Timedelta(days=race_date.weekday())
            horizon_weeks = min(horizon_weeks,
                                int((race_week_start - current_week_start).days / 7) + 1)

    weeks = []
    vol = base_km
    peak_cap = min(base_km * 1.45, freq_cap)  # +45 % max sur 3 mois, borné par la fréquence
    for i in range(horizon_weeks):
        week_start = current_week_start + pd.Timedelta(weeks=i)

        if race_week_start is not None and week_start == race_week_start:
            week_type, week_km = "Course", round(vol * 0.5, 1)
        elif race_week_start is not None and week_start == race_week_start - pd.Timedelta(weeks=1):
            week_type, week_km = "Affûtage", round(vol * 0.7, 1)
        elif i % 4 == 3:
            week_type, week_km = "Récupération", round(vol * 0.7, 1)
        else:
            if i > 0:
                vol = min(vol * 1.06, peak_cap)
            week_type = "Progression" if vol < peak_cap else "Stabilité"
            week_km = round(vol, 1)

        sessions = _week_sessions(nb_seances, longrun_day, week_km, week_type, i, paces)
        if week_type == "Course" and race:
            # remplace la sortie longue par la course elle-même
            for s in sessions:
                if s["type"] == "Sortie longue" or len(sessions) == 1:
                    s.update({"type": f"🏁 COURSE : {race.get('name') or 'Objectif'}",
                              "km": race.get("distance_km") or s["km"],
                              "allure": "objectif !", "couleur": "🏁",
                              "contenu": "Jour J — échauffement court, puis fais-toi plaisir."})
        weeks.append({
            "week_start": week_start,
            "type": week_type,
            "km": week_km,
            "nb_seances": len(sessions),
            "sessions": sessions,
        })

    return {"weeks": weeks, "paces": paces, "base_km": base_km}
