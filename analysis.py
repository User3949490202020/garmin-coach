"""
analysis.py
-----------
Toute la logique "coach" : transformer les données brutes en indicateurs
de performance et de récupération que tu peux vraiment utiliser.

Indicateurs calculés :
- Charge d'entraînement journalière (approximation type TRIMP simplifié :
  durée x intensité relative via la FC moyenne)
- ACWR (Acute:Chronic Workload Ratio) : ratio charge des 7 derniers jours
  / charge moyenne des 28 derniers jours. C'est LA métrique utilisée en
  sciences du sport pour estimer le risque de blessure par surcharge :
    < 0.8  : sous-entraînement (tu pourrais probablement pousser plus)
    0.8-1.3 : zone optimale
    > 1.5  : zone à risque de blessure (charge trop soudaine)
- Tendance d'allure à FC égale (progression réelle de la forme)
- Score de récupération quotidien (sommeil + FC repos + HRV + body battery)
"""

import pandas as pd
import numpy as np
import datetime as dt


def training_load(activities_df: pd.DataFrame, hr_max=190, hr_rest=55) -> pd.DataFrame:
    """Calcule une charge d'entraînement simplifiée par séance (proche du TRIMP)."""
    df = activities_df.copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    hrr = (df["avg_hr"].fillna(hr_rest) - hr_rest) / max(hr_max - hr_rest, 1)
    hrr = hrr.clip(lower=0, upper=1.3)
    df["load"] = (df["duration_s"] / 60) * hrr * 1.5
    return df


def training_load_cross(cross_df: pd.DataFrame, hr_max=190, hr_rest=55, default_hr=105) -> pd.DataFrame:
    """
    Charge d'une séance de renfo/muscu : même logique TRIMP que la course
    (durée × intensité cardiaque), MAIS si la FC moyenne est absente (fréquent
    en muscu, sans capteur), on suppose une intensité modérée-basse
    (`default_hr`) au lieu de 0 — une séance de renfo fatigue même sans FC.
    Résultat volontairement modeste par rapport à une séance de course.
    """
    df = cross_df.copy()
    if df.empty:
        return df
    # Le yoga/étirements ne fatigue pas : il ne compte pas dans la charge
    # (il est suivi à part, comme pratique de récupération).
    if "sport" in df.columns:
        df = df[~df["sport"].astype(str).str.contains("yoga", case=False, na=False)]
        if df.empty:
            return df
    df["date"] = pd.to_datetime(df["date"])
    hr = df["avg_hr"].fillna(default_hr)
    hrr = ((hr - hr_rest) / max(hr_max - hr_rest, 1)).clip(lower=0, upper=1.3)
    df["load"] = (df["duration_s"] / 60) * hrr * 1.5
    return df


def daily_training_load(activities_df: pd.DataFrame, cross_df: pd.DataFrame = None) -> pd.Series:
    """
    Charge quotidienne totale (course + renfo/muscu), indexée par jour.
    Centralise le calcul pour que l'onglet Charge et le contexte du coach IA
    restent cohérents.
    """
    parts = []
    run = training_load(activities_df)
    if not run.empty:
        parts.append(run[["date", "load"]])
    if cross_df is not None and not cross_df.empty:
        cross = training_load_cross(cross_df)
        if not cross.empty:
            parts.append(cross[["date", "load"]])
    if not parts:
        return pd.Series(dtype=float)
    allp = pd.concat(parts, ignore_index=True)
    daily = allp.groupby(allp["date"].dt.date)["load"].sum()
    daily.index = pd.to_datetime(daily.index)
    return daily.sort_index()


def acwr(daily_load: pd.Series) -> pd.DataFrame:
    """
    daily_load : série indexée par date (une valeur par jour, 0 si repos)
    Retourne un DataFrame avec charge aiguë (7j), chronique (28j) et le ratio.
    """
    s = daily_load.sort_index()
    acute = s.rolling(7, min_periods=1).mean()
    chronic = s.rolling(28, min_periods=7).mean()
    ratio = (acute / chronic).replace([np.inf, -np.inf], np.nan)
    return pd.DataFrame({
        "charge_aigue_7j": acute,
        "charge_chronique_28j": chronic,
        "acwr": ratio,
    })


def acwr_zone(value):
    if pd.isna(value):
        return "Données insuffisantes"
    if value < 0.8:
        return "Sous-entraînement"
    if value <= 1.3:
        return "Zone optimale"
    if value <= 1.5:
        return "Vigilance"
    return "Risque élevé de blessure"


def pace_efficiency(activities_df: pd.DataFrame) -> pd.DataFrame:
    """
    Calcule un ratio allure/FC : plus il baisse dans le temps, plus tu es
    efficace (tu vas plus vite au même effort cardiaque = progrès réel).
    """
    df = activities_df.copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["avg_pace_s_per_km", "avg_hr"])
    df["efficacite"] = df["avg_pace_s_per_km"] / df["avg_hr"]
    return df.sort_values("date")


def recovery_score(wellness_df: pd.DataFrame, sleep_df: pd.DataFrame = None) -> pd.DataFrame:
    """
    Score de récupération 0-100 basé sur z-scores de FC repos (inversé),
    HRV, et body battery matin, comparés à la moyenne perso des 28 derniers jours.
    Les siestes (sleep_df.nap_s) ajoutent un petit bonus : même 10 minutes
    aident à récupérer (+1.5 pt), plafonné à +5 pts.
    """
    df = wellness_df.copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")

    # Bonus sieste du jour (fusionné par date depuis les données de sommeil)
    nap_bonus = pd.Series(0.0, index=df.index)
    if sleep_df is not None and not sleep_df.empty and "nap_s" in sleep_df.columns:
        naps = sleep_df.copy()
        naps["date"] = pd.to_datetime(naps["date"])
        nap_map = naps.set_index("date")["nap_s"]
        nap_min = df["date"].map(nap_map).fillna(0) / 60
        nap_bonus = (nap_min * 0.15).clip(0, 5)  # 10 min → +1.5, 30 min → +4.5

    def zscore(col):
        if col not in df.columns:
            return pd.Series(np.nan, index=df.index)
        mean = df[col].rolling(28, min_periods=5).mean()
        std = df[col].rolling(28, min_periods=5).std().replace(0, np.nan)
        return (df[col] - mean) / std

    # Signaux principaux (poids fort) + signaux d'appoint (poids réduit) :
    # le stress Garmin et le volume de pas affinent l'état de forme sans
    # écraser les marqueurs physiologiques (FC repos, HRV, Body Battery).
    signals = [
        (-zscore("resting_hr"), 1.0),       # FC repos plus basse = mieux
        (zscore("hrv_avg"), 1.0),           # HRV plus haut = mieux
        (zscore("body_battery_max"), 1.0),  # Body battery plus haut = mieux
        (-zscore("stress_avg"), 0.7),       # Stress plus bas = mieux
        (-zscore("steps"), 0.4),            # Beaucoup plus de pas que d'habitude = fatigue en plus
    ]
    values = pd.concat([s for s, _ in signals], axis=1)
    weights = np.array([w for _, w in signals])
    # Moyenne pondérée en ignorant les signaux manquants jour par jour
    mask = values.notna()
    weighted_sum = (values.fillna(0) * weights).sum(axis=1)
    weight_total = (mask * weights).sum(axis=1).replace(0, np.nan)
    combined = weighted_sum / weight_total
    # Transforme le z-score combiné en score 0-100 (50 = dans la moyenne perso),
    # puis ajoute le bonus sieste du jour.
    df["recovery_score"] = (50 + combined * 15 + nap_bonus).clip(0, 100)
    return df


def tag_warmups(activities_df: pd.DataFrame) -> pd.DataFrame:
    """
    Repère les échauffements : jours à plusieurs courses où une sortie courte
    (≤ 6 km) est suivie de près (≤ 60 min après sa fin) par une sortie plus
    intense (FC plus haute ou allure nettement plus rapide) — le schéma
    classique « footing d'échauffement puis séance de VMA dans la foulée ».
    Ajoute une colonne booléenne `is_warmup` ; l'heure de départ est extraite
    des données brutes (raw_json) déjà synchronisées.
    """
    import json as _json
    df = activities_df.copy()
    df["is_warmup"] = False
    if df.empty or len(df) < 2:
        return df
    df["date"] = pd.to_datetime(df["date"])

    def _start(row):
        try:
            raw = _json.loads(row.get("raw_json") or "{}")
            s = raw.get("startTimeLocal") or raw.get("start_date_local") or ""
            return pd.to_datetime(s.replace("Z", "")) if s else pd.NaT
        except Exception:
            return pd.NaT

    df["_start"] = df.apply(_start, axis=1)
    for _, grp in df.groupby(df["date"].dt.date):
        if len(grp) < 2:
            continue
        g = grp.dropna(subset=["_start"]).sort_values("_start")
        for i in range(len(g) - 1):
            cur, nxt = g.iloc[i], g.iloc[i + 1]
            cur_end = cur["_start"] + pd.Timedelta(seconds=float(cur["duration_s"] or 0))
            gap_min = (nxt["_start"] - cur_end).total_seconds() / 60
            if gap_min > 60 or (cur["distance_km"] or 0) > 6:
                continue
            harder = (
                (pd.notna(nxt["avg_hr"]) and pd.notna(cur["avg_hr"])
                 and nxt["avg_hr"] > cur["avg_hr"] + 5)
                or (pd.notna(nxt["avg_pace_s_per_km"]) and pd.notna(cur["avg_pace_s_per_km"])
                    and nxt["avg_pace_s_per_km"] < cur["avg_pace_s_per_km"] - 10)
            )
            if harder:
                df.loc[cur.name, "is_warmup"] = True
    return df.drop(columns=["_start"])


def session_intensity(activities_df: pd.DataFrame, hr_max=190) -> pd.DataFrame:
    """
    Classe chaque séance en Facile / Moyen / Dur d'après la FC moyenne
    rapportée à la FC max (%FCmax) : <75% = facile, 75-85% = moyen, >85% = dur.
    Sans FC mesurée, la séance est classée "Non classée".
    """
    df = activities_df.copy()
    if df.empty:
        return df
    # Les échauffements ne comptent pas comme des séances à part entière :
    # ils sont fusionnés avec la séance de qualité qui les suit.
    if "is_warmup" in df.columns:
        df = df[~df["is_warmup"]]
        if df.empty:
            return df
    df["date"] = pd.to_datetime(df["date"])
    pct = df["avg_hr"] / hr_max

    def label(p):
        if pd.isna(p):
            return "Non classée"
        if p < 0.75:
            return "Facile"
        if p <= 0.85:
            return "Moyen"
        return "Dur"

    df["intensite"] = pct.apply(label)
    return df


def weekly_stats(activities_df: pd.DataFrame) -> pd.DataFrame:
    """
    Regroupe les séances par semaine (lundi-dimanche, comme Strava) :
    distance totale, durée totale, nombre de séances, FC moyenne, dénivelé.
    Les semaines sans séance sont incluses avec une distance de 0, pour que
    les moyennes glissantes et le calcul de streak restent corrects.
    """
    df = activities_df.copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["week_start"] = (df["date"] - pd.to_timedelta(df["date"].dt.weekday, unit="D")).dt.normalize()

    # Un échauffement + sa séance de VMA = UNE seule séance (les km comptent
    # tous, mais on ne gonfle pas le nombre de séances de la semaine).
    if "is_warmup" in df.columns:
        df["_seance_unit"] = (~df["is_warmup"]).astype(int)
    else:
        df["_seance_unit"] = 1

    grouped = df.groupby("week_start").agg(
        distance_km=("distance_km", "sum"),
        duration_s=("duration_s", "sum"),
        nb_seances=("_seance_unit", "sum"),
        avg_hr=("avg_hr", "mean"),
        elevation_gain=("elevation_gain", "sum"),
    )

    today = pd.Timestamp.now().normalize()
    current_week_start = today - pd.to_timedelta(today.weekday(), unit="D")
    full_weeks = pd.date_range(grouped.index.min(), current_week_start, freq="7D")
    grouped = grouped.reindex(full_weeks, fill_value=0).rename_axis("week_start").reset_index()

    grouped["moyenne_glissante_4sem_km"] = grouped["distance_km"].rolling(4, min_periods=1).mean()
    grouped["moyenne_glissante_12sem_km"] = grouped["distance_km"].rolling(12, min_periods=1).mean()
    grouped["moyenne_glissante_4sem_elevation"] = grouped["elevation_gain"].rolling(4, min_periods=1).mean()
    return grouped


def monthly_stats(activities_df: pd.DataFrame) -> pd.DataFrame:
    """Regroupe les séances par mois : distance totale, dénivelé, nb séances."""
    df = activities_df.copy()
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df["month"] = df["date"].dt.to_period("M").dt.to_timestamp()
    grouped = df.groupby("month").agg(
        distance_km=("distance_km", "sum"),
        duration_s=("duration_s", "sum"),
        nb_seances=("activity_id", "count"),
        elevation_gain=("elevation_gain", "sum"),
    ).reset_index()
    return grouped


def personal_records(activities_df: pd.DataFrame) -> dict:
    """Records personnels façon Strava : plus longue sortie, meilleure allure, plus de dénivelé."""
    df = activities_df.copy()
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"])

    records = {}
    longest = df.loc[df["distance_km"].idxmax()]
    records["plus_longue_sortie"] = {
        "distance_km": longest["distance_km"],
        "date": longest["date"],
        "nom": longest["name"],
    }

    # Meilleure allure sur les sorties de plus de 3 km (évite les faux positifs sur sprints très courts)
    valid_pace = df[(df["distance_km"] >= 3) & (df["avg_pace_s_per_km"].notna())]
    if not valid_pace.empty:
        best = valid_pace.loc[valid_pace["avg_pace_s_per_km"].idxmin()]
        records["meilleure_allure"] = {
            "pace_s_per_km": best["avg_pace_s_per_km"],
            "date": best["date"],
            "nom": best["name"],
            "distance_km": best["distance_km"],
        }

    if df["elevation_gain"].notna().any():
        hilliest = df.loc[df["elevation_gain"].idxmax()]
        records["plus_de_denivele"] = {
            "elevation_gain": hilliest["elevation_gain"],
            "date": hilliest["date"],
            "nom": hilliest["name"],
        }

    records["distance_totale_km"] = df["distance_km"].sum()
    records["nb_total_seances"] = len(df)
    records["distance_annee_courante_km"] = df[df["date"].dt.year == pd.Timestamp.now().year]["distance_km"].sum()

    return records


def current_streak_weeks(weekly_df: pd.DataFrame) -> int:
    """Nombre de semaines consécutives (jusqu'à aujourd'hui) avec au moins une séance."""
    if weekly_df.empty:
        return 0
    streak = 0
    for val in weekly_df["distance_km"].iloc[::-1]:
        if val > 0:
            streak += 1
        else:
            break
    return streak


def activity_calendar(activities_df: pd.DataFrame, weeks=52) -> pd.DataFrame:
    """
    Construit une grille jour x distance sur les N dernières semaines,
    pour un calendrier d'activité façon Strava/GitHub (heatmap).
    """
    df = activities_df.copy()
    if df.empty:
        return pd.DataFrame(columns=["date", "distance_km"])
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    daily = df.groupby("date")["distance_km"].sum()

    end = pd.Timestamp.now().normalize()
    start = end - pd.Timedelta(weeks=weeks)
    full_range = pd.date_range(start, end, freq="D")
    daily = daily.reindex(full_range, fill_value=0)
    return daily.rename_axis("date").reset_index(name="distance_km")


def generate_recommendations(acwr_latest: float, recovery_latest: float, days_since_rest: int,
                              resting_today: bool = False) -> list[str]:
    """Génère des conseils texte façon coach, basés sur les indicateurs du jour."""
    tips = []

    zone = acwr_zone(acwr_latest)
    if zone == "Risque élevé de blessure":
        if resting_today:
            tips.append("⚠️ Ta charge a augmenté vite ces 7 derniers jours. Bien vu de prendre du repos "
                        "aujourd'hui — continue sur cette lancée avec une ou deux séances faciles "
                        "avant de remonter en intensité.")
        else:
            tips.append("⚠️ Ta charge a augmenté trop vite ces 7 derniers jours par rapport à ton habitude. "
                        "Réduis le volume ou l'intensité cette semaine pour éviter la blessure.")
    elif zone == "Vigilance":
        if resting_today:
            tips.append("Ta charge monte fort, mais tu es déjà en repos aujourd'hui : c'est exactement "
                        "ce qu'il faut faire. Reste à l'écoute de tes sensations pour la reprise.")
        else:
            tips.append("Ta charge monte fort. Ajoute une séance facile ou un jour de repos supplémentaire.")
    elif zone == "Sous-entraînement":
        tips.append("Ta charge récente est basse par rapport à ta charge habituelle : tu peux probablement "
                     "augmenter progressivement le volume si tu te sens bien.")
    else:
        tips.append("Ta charge d'entraînement est dans une zone saine et progressive. Continue ainsi.")

    if recovery_latest is not None and not pd.isna(recovery_latest):
        if recovery_latest < 35:
            tips.append("🔴 Ta récupération (sommeil / FC repos / HRV) est nettement en dessous de ta normale. "
                         "Privilégie une séance facile ou du repos aujourd'hui.")
        elif recovery_latest < 50:
            tips.append("🟠 Récupération un peu en dessous de ta moyenne : reste à l'écoute de tes sensations.")
        else:
            tips.append("🟢 Bonne récupération : c'est le bon moment pour une séance de qualité (fractionné, tempo).")

    if days_since_rest is not None and days_since_rest >= 7:
        tips.append(f"Tu n'as pas eu de jour de repos complet depuis {days_since_rest} jours. "
                     "Un jour de coupure aide à mieux assimiler la charge.")

    return tips


def training_phase(race_date: dt.date, today: dt.date = None, focus_style: str = "vma") -> dict:
    """
    Détermine la phase d'entraînement en fonction du nombre de semaines
    restantes avant la course. Découpage volontairement simple, en 3 blocs :
    phase de fond (loin de l'échéance) -> Seuil (phase intermédiaire) ->
    Affûtage (dernières semaines).

    `focus_style` personnalise le contenu de la phase de fond, car tout le
    monde n'a pas les mêmes objectifs (certains veulent de la VMA, d'autres
    juste du volume, etc.) : "vma", "volume", ou "mixte".
    """
    today = today or dt.date.today()
    if race_date is None:
        return {
            "phase": "Aucune course sélectionnée",
            "description": "Ajoute une course ci-dessus pour obtenir un plan personnalisé par phase.",
        }

    weeks_to_race = (race_date - today).days / 7

    if weeks_to_race < 0:
        return {"phase": "Course passée", "description": "La date de cette course est déjà passée."}
    if weeks_to_race <= 2:
        return {
            "phase": "Affûtage",
            "description": (f"Plus que {weeks_to_race:.1f} semaine(s) avant la course : réduis le volume "
                            "de 30 à 50 % tout en gardant un peu d'intensité courte (quelques accélérations), "
                            "pour arriver frais le jour J sans perdre le rythme."),
        }
    if weeks_to_race <= 6:
        return {
            "phase": "Seuil",
            "description": ("Phase de travail au seuil : privilégie des sorties tempo et seuil lactique "
                            "(effort soutenu mais tenable 20-40 min) pour développer ta capacité à "
                            "maintenir une allure élevée sur la durée de course visée."),
        }

    focus_descriptions = {
        "vma": {
            "phase": "VMA",
            "description": ("Phase de développement de la VMA : vise environ 2 séances de fractionné VMA "
                            "par semaine (ex : 30/30, 400m rapides) pour augmenter ta vitesse maximale "
                            "aérobie, la base sur laquelle tu construiras la vitesse spécifique course ensuite."),
        },
        "volume": {
            "phase": "Volume / Endurance de fond",
            "description": ("Phase de développement du volume : privilégie les sorties longues à allure "
                            "modérée pour construire ta base aérobie, en augmentant progressivement le "
                            "kilométrage hebdomadaire."),
        },
        "mixte": {
            "phase": "Préparation générale",
            "description": ("Phase de préparation générale : alterne sorties longues, un peu de fractionné "
                            "et renforcement musculaire, selon tes sensations et tes objectifs propres."),
        },
    }
    return focus_descriptions.get(focus_style, focus_descriptions["vma"])


def _format_time(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}min{s:02d}" if h else f"{m}min{s:02d}"


def predict_race_times(activities_df: pd.DataFrame, months: int = 6) -> dict:
    """
    Estime les temps sur 10K, semi-marathon et marathon à partir de ta
    meilleure performance récente (sur les `months` derniers mois), via la
    formule de Riegel (référence standard en course à pied) :
    T2 = T1 * (D2/D1)^1.06.
    Ce n'est qu'une estimation basée sur ta forme actuelle, pas une science exacte.
    """
    df = activities_df.copy()
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"])
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    df = df[df["date"] >= cutoff]
    df = df.dropna(subset=["avg_pace_s_per_km", "distance_km"])
    df = df[df["distance_km"] >= 3]
    if df.empty:
        return {}

    df["total_time_s"] = df["avg_pace_s_per_km"] * df["distance_km"]

    # On préfère une référence sur au moins 5 km (plus fiable pour extrapoler
    # sur 10K/semi/marathon qu'un sprint très court)
    ref_candidates = df[df["distance_km"] >= 5]
    if ref_candidates.empty:
        ref_candidates = df
    ref = ref_candidates.loc[ref_candidates["avg_pace_s_per_km"].idxmin()]

    d1 = ref["distance_km"]
    t1 = ref["total_time_s"]

    def riegel(d2):
        return t1 * (d2 / d1) ** 1.06

    predictions = {
        "reference": {"distance_km": d1, "temps_s": t1, "date": ref["date"], "nom": ref["name"]},
    }
    for label, dist in [("10K", 10), ("Semi", 21.1), ("Marathon", 42.195)]:
        t = riegel(dist)
        predictions[label] = {"temps_s": t, "temps_str": _format_time(t)}
    return predictions


def best_effort_by_distance(activities_df: pd.DataFrame, target_km: float, tolerance: float = 0.15,
                             months: int = 6) -> dict:
    """
    Cherche ta meilleure performance (allure la plus rapide) sur une distance
    donnée (à +/- tolérance près, ex : 10K = entre 8.5 et 11.5 km), sur les
    `months` derniers mois. Retourne None si aucune séance ne correspond
    (plutôt que d'inventer une donnée).
    """
    df = activities_df.copy()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    df = df[df["date"] >= cutoff]

    lower, upper = target_km * (1 - tolerance), target_km * (1 + tolerance)
    candidates = df[(df["distance_km"] >= lower) & (df["distance_km"] <= upper) & df["avg_pace_s_per_km"].notna()]
    if candidates.empty:
        return None

    best = candidates.loc[candidates["avg_pace_s_per_km"].idxmin()]
    return {
        "distance_km": best["distance_km"],
        "temps_s": best["avg_pace_s_per_km"] * best["distance_km"],
        "pace_s_per_km": best["avg_pace_s_per_km"],
        "date": best["date"],
        "nom": best["name"],
    }


def hrv_trend(wellness_df: pd.DataFrame) -> pd.DataFrame:
    """
    Prépare la courbe de HRV pour l'affichage :
    - complète les jours manquants (axe de dates continu, sans trous bizarres)
    - comble les petits trous isolés par interpolation (pas plus de 3 jours d'affilée)
    - lisse sur 3 jours pour une courbe plus lisible qu'un zigzag brut
    - calcule une ligne de base perso (moyenne 28j) et des seuils orange/rouge
      relatifs à CETTE moyenne perso (la HRV se lit toujours par rapport à soi-même,
      jamais par rapport à une valeur absolue universelle)
    """
    df = wellness_df.copy()
    if df.empty or "hrv_avg" not in df.columns or df["hrv_avg"].dropna().empty:
        return pd.DataFrame()

    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    full_range = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    df = df.set_index("date").reindex(full_range).rename_axis("date").reset_index()

    df["hrv_avg"] = df["hrv_avg"].interpolate(limit=3)
    df["hrv_lisse"] = df["hrv_avg"].rolling(3, min_periods=1, center=True).mean()
    df["baseline_28j"] = df["hrv_avg"].rolling(28, min_periods=5).mean()
    df["seuil_orange"] = df["baseline_28j"] * 0.90
    df["seuil_rouge"] = df["baseline_28j"] * 0.80
    return df


def weather_adjusted_pace(activities_df: pd.DataFrame, reference_temp: float = 15,
                           pct_per_degree: float = 0.006, months: int = 6) -> pd.DataFrame:
    """
    Calcule une allure "ajustée météo" : ce que serait ton allure dans des
    conditions neutres (~15°C) pour le même effort. La chaleur augmente le
    coût cardiovasculaire de la course, donc à FC égale, une séance courue
    plus lentement par forte chaleur peut en réalité représenter un meilleur
    niveau de forme qu'une séance plus rapide par temps frais.

    ⚠️ C'est une approximation basée sur une règle empirique courante en
    course à pied (~0.6%/°C au-dessus de 15°C), pas un calcul physiologique
    individualisé. À prendre comme tendance indicative, pas comme vérité absolue.
    """
    df = activities_df.copy()
    if df.empty or "temp_c" not in df.columns:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    df = df[df["date"] >= cutoff]
    df = df.dropna(subset=["temp_c", "avg_pace_s_per_km"])
    if df.empty:
        return df
    df = df.sort_values("date")
    penalty = (df["temp_c"] - reference_temp).clip(lower=0) * pct_per_degree
    df["allure_ajustee_s_per_km"] = df["avg_pace_s_per_km"] / (1 + penalty)
    return df


# ======================================================================
# Indicateurs "Bonus" — lecture avancée de la progression
# ======================================================================

def polarization(activities_df: pd.DataFrame, hr_max=190, days=28) -> dict:
    """
    Répartition 80/20 sur les `days` derniers jours, pondérée par le TEMPS
    passé (référence en sciences du sport) : % facile vs % intense
    (moyen + dur). La règle d'or : ~80 % du temps en facile. L'erreur
    classique de l'amateur est le "toujours moyennement dur".
    Les échauffements sont fusionnés avec leur séance (déjà exclus par
    session_intensity).
    """
    df = session_intensity(activities_df, hr_max=hr_max)
    if df.empty:
        return {}
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=days)
    df = df[(df["date"] >= cutoff) & df["duration_s"].notna()]
    df = df[df["intensite"] != "Non classée"]
    total = df["duration_s"].sum()
    if not total or len(df) < 3:
        return {}
    pct = df.groupby("intensite")["duration_s"].sum() / total * 100
    return {
        "facile": float(pct.get("Facile", 0)),
        "moyen": float(pct.get("Moyen", 0)),
        "dur": float(pct.get("Dur", 0)),
        "intense": float(pct.get("Moyen", 0) + pct.get("Dur", 0)),
        "nb_seances": int(len(df)),
    }


def cardiac_drift(activities_df: pd.DataFrame, laps_df: pd.DataFrame,
                  min_km=8, months=6) -> pd.DataFrame:
    """
    Dérive cardiaque sur les sorties longues à allure stable : FC de la
    2e moitié vs 1re moitié (en %), uniquement quand l'allure des deux
    moitiés est comparable (écart < 5 %), sinon la comparaison n'a pas de
    sens. Dérive < 5 % = bonne endurance de base ; au-delà = fatigue,
    chaleur ou déshydratation.
    """
    if activities_df.empty or laps_df is None or laps_df.empty:
        return pd.DataFrame()
    df = activities_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    df = df[(df["date"] >= cutoff) & (df["distance_km"] >= min_km)]
    if "is_warmup" in df.columns:
        df = df[~df["is_warmup"]]

    rows = []
    for _, a in df.iterrows():
        lp = laps_df[laps_df["activity_id"] == a["activity_id"]].sort_values("lap_index")
        lp = lp.dropna(subset=["avg_hr", "avg_pace_s_per_km"])
        # On ignore le dernier tour s'il est partiel (< 500 m)
        if not lp.empty and (lp.iloc[-1]["distance_km"] or 0) < 0.5:
            lp = lp.iloc[:-1]
        if len(lp) < 6:
            continue
        half = len(lp) // 2
        h1, h2 = lp.iloc[:half], lp.iloc[half:]
        pace1, pace2 = h1["avg_pace_s_per_km"].mean(), h2["avg_pace_s_per_km"].mean()
        if not pace1 or abs(pace2 - pace1) / pace1 > 0.05:
            continue  # allure trop variable : séance non comparable
        hr1, hr2 = h1["avg_hr"].mean(), h2["avg_hr"].mean()
        if not hr1:
            continue
        rows.append({
            "date": a["date"], "name": a["name"], "distance_km": a["distance_km"],
            "drift_pct": (hr2 / hr1 - 1) * 100,
        })
    return pd.DataFrame(rows).sort_values("date") if rows else pd.DataFrame()


def fitness_curve(activities_df: pd.DataFrame, months=6, window_weeks=8) -> pd.DataFrame:
    """
    Courbe de forme : le temps 10K théorique (Riegel, sur la meilleure perf
    des `window_weeks` semaines précédentes), recalculé toutes les 2 semaines
    sur les `months` derniers mois. Une courbe qui DESCEND = tu progresses.
    """
    df = activities_df.copy()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["avg_pace_s_per_km", "distance_km"])
    df = df[df["distance_km"] >= 5]
    if df.empty:
        return pd.DataFrame()

    points = []
    end = pd.Timestamp.now().normalize()
    start = end - pd.DateOffset(months=months)
    for ts in pd.date_range(start, end, freq="14D"):
        window = df[(df["date"] <= ts) & (df["date"] > ts - pd.Timedelta(weeks=window_weeks))]
        if window.empty:
            continue
        best = window.loc[window["avg_pace_s_per_km"].idxmin()]
        t10 = (best["avg_pace_s_per_km"] * best["distance_km"]) * (10 / best["distance_km"]) ** 1.06
        points.append({"date": ts, "t10_min": t10 / 60})
    return pd.DataFrame(points)


def monotony(activities_df: pd.DataFrame, cross_df: pd.DataFrame = None) -> pd.DataFrame:
    """
    Monotonie de charge (Foster) : moyenne / écart-type de la charge
    quotidienne sur 7 jours glissants. Au-dessus de 2, l'entraînement est
    trop uniforme (même dose tous les jours) — un facteur de risque de
    blessure indépendant du volume : il faut de l'alternance dur/facile.
    """
    daily = daily_training_load(activities_df, cross_df)
    if daily.empty:
        return pd.DataFrame()
    full = pd.date_range(daily.index.min(), pd.Timestamp.now().normalize(), freq="D")
    daily = daily.reindex(full, fill_value=0)
    mean7 = daily.rolling(7, min_periods=7).mean()
    std7 = daily.rolling(7, min_periods=7).std().replace(0, np.nan)
    return pd.DataFrame({"monotonie": (mean7 / std7)}).dropna()


def cadence_trend(activities_df: pd.DataFrame, months=6) -> pd.DataFrame:
    """Cadence moyenne par séance + moyenne glissante sur 10 séances."""
    df = activities_df.copy()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    df = df[(df["date"] >= cutoff)].dropna(subset=["avg_cadence"]).sort_values("date")
    if "is_warmup" in df.columns:
        df = df[~df["is_warmup"]]
    if df.empty:
        return df
    df["cadence_lissee"] = df["avg_cadence"].rolling(10, min_periods=3).mean()
    return df
