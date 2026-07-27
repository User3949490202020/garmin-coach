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
    pct_avg = df["avg_hr"] / hr_max
    pct_max = (df["max_hr"] / hr_max) if "max_hr" in df.columns else pd.Series(np.nan, index=df.index)

    # Une séance de VMA a une FC MOYENNE diluée par les récupérations entre
    # fractions : la moyenne seule la classerait "Moyen". On regarde donc
    # aussi le PIC : des pointes ≥ 93 % de la FCmax avec une moyenne déjà
    # soutenue (≥ 76 %) = séance dure, quelle que soit la moyenne.
    def label(i):
        pa, pm = pct_avg.loc[i], pct_max.loc[i]
        if pd.isna(pa):
            return "Non classée"
        if pa > 0.85 or (pd.notna(pm) and pm >= 0.93 and pa >= 0.76):
            return "Dur"
        if pa >= 0.75:
            return "Moyen"
        return "Facile"

    df["intensite"] = [label(i) for i in df.index]
    return df


def observed_hr_max(activities_df: pd.DataFrame, months=6, default=190) -> int:
    """
    FC max observée sur les séances des `months` derniers mois (le plus haut
    pic enregistré par la montre). Sert de valeur par défaut personnalisée,
    que l'athlète peut corriger à la main s'il connaît sa vraie FCmax.
    """
    if activities_df is None or activities_df.empty or "max_hr" not in activities_df.columns:
        return default
    df = activities_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)
    observed = df[df["date"] >= cutoff]["max_hr"].max()
    if pd.isna(observed):
        return default
    return int(min(max(observed, 150), 220))  # garde-fou contre les valeurs aberrantes


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


# ======================================================================
# Zones cardiaques en %FCR (Karvonen) et analyse VMA
# ======================================================================

# Bornes des 5 zones en % de la réserve cardiaque (FCmax - FCrepos),
# le découpage de référence chez les coureurs confirmés.
# Dégradé de bleus : clair = facile, foncé = difficile (Z5 = le plus foncé).
HR_ZONES_DEF = [
    ("Z1", "Récupération", 0.50, 0.60, "#A8CCEC"),
    ("Z2", "Endurance fondamentale", 0.60, 0.70, "#6FA8DC"),
    ("Z3", "Tempo", 0.70, 0.80, "#3D85C6"),
    ("Z4", "Seuil", 0.80, 0.90, "#1C5A99"),
    ("Z5", "VMA", 0.90, 1.00, "#0B3866"),
]


def current_rest_hr(wellness_df: pd.DataFrame, default: int = 55) -> int:
    """FC repos actuelle : moyenne des 28 derniers jours mesurés (sinon défaut)."""
    if wellness_df is None or wellness_df.empty or "resting_hr" not in wellness_df.columns:
        return default
    vals = wellness_df.sort_values("date")["resting_hr"].dropna().tail(28)
    return int(round(vals.mean())) if len(vals) else default


def hr_zones(hr_max: int, hr_rest: int) -> pd.DataFrame:
    """Les 5 zones Karvonen traduites en bpm PERSONNALISÉS."""
    reserve = max(hr_max - hr_rest, 1)
    rows = []
    for code, label, lo, hi, color in HR_ZONES_DEF:
        rows.append({
            "zone": code, "label": label,
            "bpm_min": int(round(hr_rest + lo * reserve)),
            "bpm_max": int(round(hr_rest + hi * reserve)),
            "color": color,
        })
    return pd.DataFrame(rows)


def session_zone(avg_hr, hr_max: int, hr_rest: int):
    """Zone Karvonen (Z1-Z5) d'une séance d'après sa FC moyenne, ou None."""
    if pd.isna(avg_hr):
        return None
    pct = (avg_hr - hr_rest) / max(hr_max - hr_rest, 1)
    if pct < 0.50:
        return "Z1"
    for code, _, lo, hi, _ in HR_ZONES_DEF:
        if lo <= pct < hi:
            return code
    return "Z5"


def vma_estimate_curve(activities_df: pd.DataFrame, laps_df: pd.DataFrame = None,
                       hr_max: int = 190, months: int = 6) -> pd.DataFrame:
    """
    vVMA ESTIMÉE (km/h) toutes les 2 semaines, à partir du MEILLEUR des deux
    signaux disponibles sur les 8 semaines précédentes :

    1. La meilleure séance CONTINUE (>= 3 km), ramenée en vitesse équivalente
       3 000 m (Riegel) — pertinent quand on fait des sorties rapides/courses.
    2. La vitesse des FRACTIONS des séances de VMA (x0.97, les fractions
       courtes se courant légèrement au-dessus de la vVMA) — indispensable :
       une séance de fractionné a une allure MOYENNE lente (récupérations),
       et sans ce signal, s'entraîner en fractionné faisait BAISSER
       l'estimation, un contresens.

    Reste une approximation d'entraînement — un test de terrain est plus précis.
    """
    df = activities_df.copy()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["avg_pace_s_per_km", "distance_km"])
    if "is_warmup" in df.columns:
        df = df[~df["is_warmup"]]
    continuous = df[df["distance_km"] >= 3]

    # Signal fractions : une vitesse de VMA par séance de fractionné analysable
    frac = pd.DataFrame()
    if laps_df is not None and not laps_df.empty:
        vs = vma_sessions(activities_df, laps_df, hr_max=hr_max, months=months + 3)
        if not vs.empty:
            frac = pd.DataFrame({
                "date": pd.to_datetime(vs["date"]),
                "vma_kmh": (3600 / vs["allure_moy_s"]) * 0.97,
            })

    points = []
    end = pd.Timestamp.now().normalize()
    for ts in pd.date_range(end - pd.DateOffset(months=months), end, freq="14D"):
        candidates = []
        window = continuous[(continuous["date"] <= ts)
                            & (continuous["date"] > ts - pd.Timedelta(weeks=8))]
        if not window.empty:
            best = window.loc[window["avg_pace_s_per_km"].idxmin()]
            pace_3k = best["avg_pace_s_per_km"] * (3 / best["distance_km"]) ** 0.06
            # La vitesse sur 3 000 m vaut ~94 % de la vVMA : on remonte de 6 %
            # pour comparer le même objet que le signal "fractions".
            candidates.append((3600 / pace_3k) * 1.06)
        if not frac.empty:
            fwin = frac[(frac["date"] <= ts) & (frac["date"] > ts - pd.Timedelta(weeks=8))]
            if not fwin.empty:
                candidates.append(fwin["vma_kmh"].max())
        if candidates:
            points.append({"date": ts, "vma_kmh": max(candidates)})

    # --- Garde-fou physiologique : la VMA évolue de ~0.3 km/h par MOIS, pas
    # par bonds. Chaque estimation étant une borne basse (les semaines sans
    # course ni fractionné sous-estiment), chaque point "relève" ses voisins
    # dans un cône à ±0.15 km/h par quinzaine : si tu vaux 17 aujourd'hui, tu
    # ne valais pas 13.5 il y a six semaines — la donnée manquait, c'est tout.
    # La pente de la courbe est ainsi mathématiquement bornée au plausible.
    if len(points) >= 2:
        raw = [p["vma_kmh"] for p in points]
        rate = 0.15  # km/h par pas de 14 jours
        for t in range(len(raw)):
            points[t]["vma_kmh"] = max(raw[s] - rate * abs(t - s) for s in range(len(raw)))
    return pd.DataFrame(points)


def vma_sessions(activities_df: pd.DataFrame, laps_df: pd.DataFrame,
                 hr_max: int = 190, months: int = 6) -> pd.DataFrame:
    """
    Analyse des séances de VMA/fractionné : pour chaque séance classée "Dur"
    dont on a le détail des tours, isole les FRACTIONS RAPIDES (tours courts
    nettement plus vite que le rythme médian de la séance) et calcule :
    nb de fractions, allure moyenne, meilleure fraction, régularité (écart
    entre fractions). Retourne une ligne par séance + les allures des
    fractions (colonne `laps_paces`) pour le graphique de détail.
    """
    if activities_df.empty or laps_df is None or laps_df.empty:
        return pd.DataFrame()
    intens = session_intensity(activities_df, hr_max=hr_max)
    hard = intens[intens["intensite"] == "Dur"].copy()
    if hard.empty:
        return pd.DataFrame()
    hard["date"] = pd.to_datetime(hard["date"])
    hard = hard[hard["date"] >= pd.Timestamp.now() - pd.DateOffset(months=months)]

    rows = []
    for _, a in hard.iterrows():
        lp = laps_df[laps_df["activity_id"] == a["activity_id"]].sort_values("lap_index")
        lp = lp.dropna(subset=["avg_pace_s_per_km"])
        lp = lp[lp["distance_km"] > 0.05]
        if len(lp) < 4:
            continue
        median_pace = lp["avg_pace_s_per_km"].median()
        fast = lp[(lp["avg_pace_s_per_km"] < median_pace - 20) & (lp["distance_km"] <= 1.6)]
        if len(fast) < 3:
            continue
        paces = fast["avg_pace_s_per_km"].tolist()

        # Récupération entre fractions : les tours lents INTERCALÉS entre la
        # première et la dernière fraction (durée médiane, en secondes).
        recup_s = None
        first_fast, last_fast = fast["lap_index"].min(), fast["lap_index"].max()
        between = lp[(lp["lap_index"] > first_fast) & (lp["lap_index"] < last_fast)
                     & (~lp.index.isin(fast.index))]
        if not between.empty and between["duration_s"].notna().any():
            recup_s = float(between["duration_s"].median())
        rows.append({
            "date": a["date"], "name": a["name"], "activity_id": a["activity_id"],
            "nb_fractions": len(fast),
            "dist_fraction_km": fast["distance_km"].mean(),
            "allure_moy_s": fast["avg_pace_s_per_km"].mean(),
            "meilleure_s": fast["avg_pace_s_per_km"].min(),
            "regularite_s": fast["avg_pace_s_per_km"].std(),
            "laps_paces": paces,
            "temp_c": a.get("temp_c") if "temp_c" in a.index else None,
            "recup_s": recup_s,
        })
    return pd.DataFrame(rows).sort_values("date", ascending=False) if rows else pd.DataFrame()


def vma_fraction_habits(vs_df: pd.DataFrame, n_sessions: int = 4, threshold_s: float = 8) -> dict | None:
    """
    Cherche les mauvaises habitudes récurrentes sur les `n_sessions` dernières
    séances de VMA : pour chaque position de fraction (F1, F2, ...), l'écart
    moyen à l'allure moyenne de la séance. Une position systématiquement
    au-dessus du seuil = trop lente ; en dessous = trop rapide. La dernière
    fraction est analysée à part (les séances n'ont pas toutes le même nombre
    de fractions). Retourne None si pas assez de données.
    """
    if vs_df is None or vs_df.empty or len(vs_df) < 2:
        return None
    recent = vs_df.sort_values("date", ascending=False).head(n_sessions)
    min_n = min(len(p) for p in recent["laps_paces"])
    if min_n < 3:
        return None

    deltas = {}
    last_deltas = []
    for paces in recent["laps_paces"]:
        m = sum(paces) / len(paces)
        for i in range(min_n - 1):  # positions alignées (la dernière traitée à part)
            deltas.setdefault(i, []).append(paces[i] - m)
        last_deltas.append(paces[-1] - m)

    avg = {i: sum(v) / len(v) for i, v in deltas.items()}
    slow = [(i + 1, d) for i, d in avg.items() if d >= threshold_s]
    fast = [(i + 1, d) for i, d in avg.items() if d <= -threshold_s]
    avg_last = sum(last_deltas) / len(last_deltas)
    return {
        "nb_sessions": len(recent),
        "slow": slow, "fast": fast,
        "last_delta": avg_last,
        "last_slow": avg_last >= threshold_s,
        "last_fast": avg_last <= -threshold_s,
    }


# ======================================================================
# Santé : phases de sommeil, stress, conseils personnalisés
# ======================================================================

def sleep_phases(sleep_df: pd.DataFrame, days: int = 30) -> pd.DataFrame:
    """Heures par phase de sommeil (profond / léger / paradoxal / éveil) par nuit."""
    if sleep_df is None or sleep_df.empty:
        return pd.DataFrame()
    df = sleep_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] >= pd.Timestamp.now() - pd.Timedelta(days=days)].sort_values("date")
    df = df.dropna(subset=["total_sleep_s"])
    if df.empty:
        return df
    out = pd.DataFrame({"date": df["date"]})
    for col, label in [("deep_sleep_s", "Profond"), ("light_sleep_s", "Léger"),
                       ("rem_sleep_s", "Paradoxal (REM)"), ("awake_s", "Éveillé")]:
        out[label] = (df[col].fillna(0) / 3600).round(2) if col in df.columns else 0.0
    out["total_h"] = (df["total_sleep_s"] / 3600).round(2)
    return out


def health_insights(wellness_df: pd.DataFrame, sleep_df: pd.DataFrame,
                    activities_df: pd.DataFrame, cross_df: pd.DataFrame,
                    hr_max: int = 190) -> list[dict]:
    """
    Conseils personnalisés dérivés des données : chaque constat croise une
    dérive observée (HRV, sommeil, stress) avec une cause plausible visible
    dans l'entraînement (intensité, sports croisés, monotonie). Retourne une
    liste de {niveau: 'ok'|'info'|'alerte', texte}.
    """
    tips = []
    now = pd.Timestamp.now().normalize()

    w = wellness_df.copy() if wellness_df is not None else pd.DataFrame()
    if not w.empty:
        w["date"] = pd.to_datetime(w["date"])

    # --- HRV : 15 derniers jours vs référence 60 jours ---
    if not w.empty and "hrv_avg" in w.columns and w["hrv_avg"].notna().sum() >= 10:
        recent = w[w["date"] >= now - pd.Timedelta(days=15)]["hrv_avg"].dropna()
        base = w[w["date"] >= now - pd.Timedelta(days=60)]["hrv_avg"].dropna()
        if len(recent) >= 4 and len(base) >= 10:
            delta_pct = (recent.mean() / base.mean() - 1) * 100
            if delta_pct <= -7:
                causes = []
                # Cause plausible n°1 : plus de séances croisées intenses qu'avant
                if cross_df is not None and not cross_df.empty:
                    c = cross_df.copy()
                    c["date"] = pd.to_datetime(c["date"])
                    c = c[~c["sport"].astype(str).str.contains("yoga|stretch", case=False, na=False)]
                    n_recent = len(c[c["date"] >= now - pd.Timedelta(days=15)])
                    n_before = len(c[(c["date"] < now - pd.Timedelta(days=15))
                                     & (c["date"] >= now - pd.Timedelta(days=30))])
                    if n_recent > n_before:
                        causes.append(f"tes séances croisées (crossfit/renfo/vélo...) sont passées de "
                                      f"{n_before} à {n_recent} sur les 15 derniers jours")
                # Cause plausible n°2 : plus de course en intensité
                if activities_df is not None and not activities_df.empty:
                    it = session_intensity(activities_df, hr_max=hr_max)
                    if not it.empty:
                        h_recent = len(it[(it["date"] >= now - pd.Timedelta(days=15))
                                          & (it["intensite"] == "Dur")])
                        h_before = len(it[(it["date"] < now - pd.Timedelta(days=15))
                                          & (it["date"] >= now - pd.Timedelta(days=30))
                                          & (it["intensite"] == "Dur")])
                        if h_recent > h_before:
                            causes.append(f"tes séances de course dures sont passées de {h_before} "
                                          f"à {h_recent}")
                txt = (f"Ta HRV a baissé de {abs(delta_pct):.0f} % sur les 15 derniers jours par "
                       f"rapport à ta référence")
                if causes:
                    txt += (" — et sur la même période, " + " et ".join(causes)
                            + ". Allège l'intensité la semaine prochaine pour laisser ton "
                            "système nerveux récupérer.")
                else:
                    txt += (". L'entraînement n'a pas visiblement changé : regarde côté sommeil, "
                            "stress ou hygiène de vie (alcool, écrans tardifs, gros stress pro).")
                tips.append({"niveau": "alerte", "texte": txt})
            elif delta_pct >= 5:
                tips.append({"niveau": "ok",
                             "texte": f"Ta HRV est {delta_pct:.0f} % au-dessus de ta référence des "
                                      "2 derniers mois : ton corps encaisse bien la période actuelle."})

    # --- Sommeil : durée moyenne sur 14 jours ---
    if sleep_df is not None and not sleep_df.empty:
        s = sleep_df.copy()
        s["date"] = pd.to_datetime(s["date"])
        recent_sleep = s[s["date"] >= now - pd.Timedelta(days=14)]["total_sleep_s"].dropna()
        if len(recent_sleep) >= 5:
            avg_h = recent_sleep.mean() / 3600
            if avg_h < 6.8:
                deficit = (7.5 - avg_h) * 7
                tips.append({"niveau": "alerte",
                             "texte": f"Tu dors en moyenne {avg_h:.1f} h par nuit sur 2 semaines — "
                                      f"soit ~{deficit:.0f} h de dette hebdomadaire vs les 7 h 30 "
                                      "recommandées pour un sportif. C'est LE levier n°1 pour ta HRV "
                                      "et ta récup : couche-toi 30 min plus tôt et coupe les écrans "
                                      "30 min avant de dormir."})
            elif avg_h >= 7.3:
                tips.append({"niveau": "ok",
                             "texte": f"{avg_h:.1f} h de sommeil moyen sur 2 semaines : solide — "
                                      "c'est la meilleure séance de récupération qui existe."})
        # Sieste : renfort positif
        if "nap_s" in s.columns:
            naps = s[(s["date"] >= now - pd.Timedelta(days=14)) & (s["nap_s"].fillna(0) > 0)]
            if len(naps) >= 2:
                tips.append({"niveau": "ok",
                             "texte": f"{len(naps)} sieste(s) sur les 2 dernières semaines : "
                                      "excellent réflexe, même 10 minutes comptent."})

    # --- Stress : 7 derniers jours vs habitude ---
    if not w.empty and "stress_avg" in w.columns and w["stress_avg"].notna().sum() >= 10:
        s7 = w[w["date"] >= now - pd.Timedelta(days=7)]["stress_avg"].dropna()
        s28 = w[w["date"] >= now - pd.Timedelta(days=28)]["stress_avg"].dropna()
        if len(s7) >= 3 and len(s28) >= 10 and s7.mean() > s28.mean() + 7:
            tips.append({"niveau": "info",
                         "texte": f"Ton stress moyen de la semaine ({s7.mean():.0f}) est nettement "
                                  f"au-dessus de ton habitude ({s28.mean():.0f}). Les jours chargés, "
                                  "remplace l'intensité par du footing Z1-Z2 ou des étirements : "
                                  "l'entraînement doit décharger le stress, pas l'empiler."})

    # --- Monotonie d'entraînement ---
    mono = monotony(activities_df, cross_df)
    if not mono.empty and mono["monotonie"].iloc[-1] > 2:
        tips.append({"niveau": "info",
                     "texte": f"Ta charge est très uniforme (monotonie {mono['monotonie'].iloc[-1]:.1f}) : "
                              "alterne franchement jours durs et jours faciles — c'est l'alternance "
                              "qui fait progresser la HRV et la forme, pas la régularité de la dose."})

    if not tips:
        tips.append({"niveau": "info",
                     "texte": "Pas assez de données récentes pour des conseils personnalisés — "
                              "synchronise régulièrement et reviens dans quelques jours."})
    return tips


def stride_by_intensity(activities_df: pd.DataFrame, hr_max: int = 190,
                        months: int = 6) -> pd.DataFrame:
    """
    Longueur de foulée moyenne (en mètres) par type de séance (Facile / Moyen /
    Dur), calculée à partir de la vitesse et de la cadence : foulée = vitesse
    (m/min) ÷ cadence (pas/min). Une foulée globale ne veut rien dire (elle
    varie avec l'allure) ; ventilée par intensité, elle révèle la sur-foulée
    (pas trop grands à allure facile).
    """
    it = session_intensity(activities_df, hr_max=hr_max)
    if it.empty:
        return pd.DataFrame()
    df = it[it["date"] >= pd.Timestamp.now() - pd.DateOffset(months=months)].copy()
    df = df.dropna(subset=["avg_cadence", "avg_pace_s_per_km"])
    df = df[(df["avg_cadence"] > 120) & (df["avg_pace_s_per_km"] > 120)]  # garde-fous
    if df.empty:
        return pd.DataFrame()
    df["stride_m"] = 60000 / (df["avg_pace_s_per_km"] * df["avg_cadence"])
    out = (df.groupby("intensite")
           .agg(foulee_m=("stride_m", "mean"), cadence=("avg_cadence", "mean"),
                nb=("stride_m", "size"))
           .reset_index())
    return out[out["intensite"].isin(["Facile", "Moyen", "Dur"])]


SPORT_LABELS = [
    ("strength", "💪 Renfo / Muscu"),
    ("yoga", "🧘 Yoga (étirements)"),
    ("stretch", "🧘 Étirements"),
    ("cycling", "🚴 Vélo"),
    ("biking", "🚴 Vélo"),
    ("swim", "🏊 Natation"),
    ("hiking", "🥾 Rando"),
    ("walking", "🚶 Marche"),
    ("hiit", "🔥 HIIT"),
    ("cardio", "🔥 Cardio"),
    ("crossfit", "🔥 CrossFit"),
    ("ski", "⛷️ Ski"),
    ("row", "🚣 Rameur"),
]


def sport_label(type_key: str) -> str:
    """Libellé lisible pour un typeKey Garmin/Strava de sport croisé."""
    tk = str(type_key or "").lower()
    for needle, label in SPORT_LABELS:
        if needle in tk:
            return label
    return "🤸 " + tk.replace("_", " ").capitalize() if tk else "🤸 Autre"
