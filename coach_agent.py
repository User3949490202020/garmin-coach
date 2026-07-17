"""
coach_agent.py
--------------
Agent conversationnel qui répond à tes questions sur tes données running,
en s'appuyant sur tes vraies stats (séances, charge, récupération).

Utilise l'API Gemini de Google (modèle Flash, gratuit pour un usage perso
comme ici) — nécessite une clé API sur aistudio.google.com, gratuite et
sans carte bancaire. Voir le README pour les étapes.
"""

import os
import datetime as dt
import pandas as pd
from google import genai
from google.genai import types

MODEL = "gemini-2.5-flash"

FR_WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]


def format_date_fr(date: dt.date) -> str:
    return f"{FR_WEEKDAYS[date.weekday()]} {date.day} {FR_MONTHS[date.month - 1]} {date.year}"


def get_client(api_key: str = None) -> genai.Client:
    api_key = api_key or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Clé API Gemini manquante : ajoute GEMINI_API_KEY dans ton fichier .env "
            "(voir le README pour savoir comment l'obtenir, c'est gratuit)."
        )
    return genai.Client(api_key=api_key)


def build_context_summary(activities: pd.DataFrame, wellness: pd.DataFrame, weekly: pd.DataFrame,
                           records: dict, acwr_latest, recovery_latest, laps: pd.DataFrame = None,
                           sleep: pd.DataFrame = None, races: pd.DataFrame = None,
                           predictions: dict = None, best_efforts: dict = None,
                           cross_training: pd.DataFrame = None,
                           manual_sleep_note: tuple = None, months: int = 6) -> str:
    """Condense 6 mois de données en un résumé texte détaillé pour une analyse fine."""
    lines = []
    cutoff = pd.Timestamp.now() - pd.DateOffset(months=months)

    # --- Course préparée et phase actuelle ---
    if races is not None and not races.empty:
        active = races[races["is_active"] == 1]
        if not active.empty:
            r = active.iloc[0]
            lines.append(f"### Course préparée : {r['name']} le {r['date']} ({r['distance_km']:.1f} km)")

    # --- Toutes les séances de la fenêtre, avec météo et détail des tours ---
    if not activities.empty:
        acts = activities[activities["date"] >= cutoff].sort_values("date", ascending=False)
        lines.append(f"\n### Toutes les séances des {months} derniers mois ({len(acts)} au total)")
        for _, a in acts.iterrows():
            pace = a["avg_pace_s_per_km"]
            pace_str = f"{int(pace // 60)}:{int(pace % 60):02d}/km" if pd.notna(pace) else "N/A"
            hr = f"{a['avg_hr']:.0f}" if pd.notna(a["avg_hr"]) else "N/A"
            elev = f"{a['elevation_gain']:.0f}" if pd.notna(a["elevation_gain"]) else "N/A"
            temp = ""
            if "temp_c" in a.index and pd.notna(a.get("temp_c")):
                temp = f", {a['temp_c']:.0f}°C"
                if pd.notna(a.get("feels_like_c")):
                    temp += f" (ressenti {a['feels_like_c']:.0f}°C)"
            lines.append(f"- {a['date'].strftime('%d/%m/%Y')} ({FR_WEEKDAYS[a['date'].weekday()]}) : "
                         f"{a['name']} — {a['distance_km']:.2f} km @ {pace_str}, FC moy {hr}, "
                         f"D+ {elev} m{temp}")

            if laps is not None and not laps.empty:
                activity_laps = laps[laps["activity_id"] == a["activity_id"]].sort_values("lap_index")
                if len(activity_laps) > 1:
                    lap_strs = []
                    for _, lap in activity_laps.iterrows():
                        lp = lap["avg_pace_s_per_km"]
                        lp_str = f"{int(lp // 60)}:{int(lp % 60):02d}" if pd.notna(lp) else "N/A"
                        lh = f"{lap['avg_hr']:.0f}" if pd.notna(lap["avg_hr"]) else "N/A"
                        lap_strs.append(f"T{int(lap['lap_index'])}:{lap['distance_km']:.2f}km@{lp_str}(FC{lh})")
                    lines.append(f"  Tours : {' | '.join(lap_strs)}")

    # --- Séances de renfo / musculation (comptent dans la charge/fatigue) ---
    if cross_training is not None and not cross_training.empty:
        ct = cross_training.copy()
        ct["date"] = pd.to_datetime(ct["date"])
        ct = ct[ct["date"] >= cutoff].sort_values("date", ascending=False)
        if not ct.empty:
            lines.append(f"\n### Séances de renfo/musculation ({len(ct)} au total, {months} derniers mois)")
            lines.append("(comptent dans la charge d'entraînement et la fatigue, pas dans le volume de course)")
            for _, c in ct.iterrows():
                dur = f"{c['duration_s'] / 60:.0f} min" if pd.notna(c.get("duration_s")) else "N/A"
                hr = f", FC moy {c['avg_hr']:.0f}" if pd.notna(c.get("avg_hr")) else ""
                lines.append(f"- {c['date'].strftime('%d/%m/%Y')} : {c['name'] or 'Renfo'} — {dur}{hr}")

    # --- Records par distance et prédictions ---
    if best_efforts:
        lines.append("\n### Records personnels par distance (6 derniers mois)")
        for label, r in best_efforts.items():
            if r:
                lines.append(f"- {label} : {r['distance_km']:.2f} km en {_format_time_str(r['temps_s'])} "
                             f"le {r['date'].strftime('%d/%m/%Y')}")

    if predictions:
        lines.append("\n### Prédictions de temps de course (formule de Riegel)")
        for label in ["10K", "Semi", "Marathon"]:
            if label in predictions:
                lines.append(f"- {label} : {predictions[label]['temps_str']}")

    if records:
        lines.append(f"\n- Distance totale enregistrée : {records.get('distance_totale_km', 0):.0f} km")
        lines.append(f"- Distance cette année : {records.get('distance_annee_courante_km', 0):.0f} km")

    # --- Volume hebdomadaire sur toute la fenêtre ---
    if weekly is not None and not weekly.empty:
        weekly_recent = weekly[weekly["week_start"] >= cutoff]
        lines.append(f"\n### Volume hebdomadaire ({months} derniers mois)")
        for _, w in weekly_recent.iterrows():
            lines.append(f"- Semaine du {w['week_start'].strftime('%d/%m')} : {w['distance_km']:.1f} km, "
                         f"{int(w['nb_seances'])} séance(s), D+ {w['elevation_gain']:.0f} m")
        lines.append(f"- Moyenne glissante 4 semaines actuelle : {weekly['moyenne_glissante_4sem_km'].iloc[-1]:.1f} km/semaine")

    if acwr_latest is not None and pd.notna(acwr_latest):
        lines.append(f"\n### Charge d'entraînement (ACWR)")
        lines.append(f"- Ratio actuel : {acwr_latest:.2f} "
                     f"(zone optimale : 0.8-1.3, >1.5 = risque de blessure)")

    if recovery_latest is not None and pd.notna(recovery_latest):
        lines.append(f"\n### Récupération")
        lines.append(f"- Score de récupération actuel : {recovery_latest:.0f}/100")

    # --- Récupération quotidienne détaillée (FC repos, HRV, Body Battery) ---
    if wellness is not None and not wellness.empty:
        w_recent = wellness[wellness["date"] >= pd.Timestamp.now() - pd.Timedelta(days=60)].sort_values("date")
        if not w_recent.empty:
            lines.append("\n### Récupération quotidienne détaillée (60 derniers jours)")
            for _, w in w_recent.iterrows():
                rhr = f"{w['resting_hr']:.0f}" if pd.notna(w.get("resting_hr")) else "N/A"
                hrv = f"{w['hrv_avg']:.0f}" if pd.notna(w.get("hrv_avg")) else "N/A"
                bb = f"{w['body_battery_max']:.0f}" if pd.notna(w.get("body_battery_max")) else "N/A"
                stress = f"{w['stress_avg']:.0f}" if pd.notna(w.get("stress_avg")) else "N/A"
                lines.append(f"- {w['date'].strftime('%d/%m')} : FC repos {rhr}, HRV {hrv}, "
                             f"Body Battery max {bb}, stress moy {stress}")

    # --- Sommeil détaillé ---
    if sleep is not None and not sleep.empty:
        sleep_copy = sleep.copy()
        sleep_copy["date"] = pd.to_datetime(sleep_copy["date"])
        s_recent = sleep_copy[sleep_copy["date"] >= pd.Timestamp.now() - pd.Timedelta(days=60)].sort_values("date")
        if not s_recent.empty:
            lines.append("\n### Sommeil détaillé (60 derniers jours)")
            for _, s in s_recent.iterrows():
                score = f"{s['sleep_score']:.0f}" if pd.notna(s.get("sleep_score")) else "N/A"
                total_h = f"{s['total_sleep_s']/3600:.1f}h" if pd.notna(s.get("total_sleep_s")) else "N/A"
                nap = ""
                if "nap_s" in s.index and pd.notna(s.get("nap_s")) and s["nap_s"] > 0:
                    nap = f", sieste {s['nap_s']/60:.0f} min"
                lines.append(f"- {s['date'].strftime('%d/%m')} : score {score}, durée {total_h}{nap}")

    # --- Note sommeil saisie manuellement par l'athlète ---
    if manual_sleep_note:
        note_val, note_date = manual_sleep_note
        lines.append(f"\n### Ressenti sommeil déclaré par l'athlète")
        lines.append(f"- Note auto-évaluée : {note_val:.0f}/100 (saisie le {note_date}). "
                     "À prendre en compte dans les conseils, surtout si les données "
                     "automatiques de sommeil manquent ou la contredisent.")

    return "\n".join(lines) if lines else "Aucune donnée disponible pour l'instant."


def _format_time_str(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}min{s:02d}" if h else f"{m}min{s:02d}"

    if not wellness.empty:
        latest_w = wellness.sort_values("date").iloc[-1]
        rhr = latest_w.get("resting_hr")
        hrv = latest_w.get("hrv_avg")
        lines.append(f"- FC repos : {rhr:.0f} bpm" if pd.notna(rhr) else "- FC repos : N/A")
        lines.append(f"- HRV : {hrv:.0f} ms" if pd.notna(hrv) else "- HRV : N/A")

    return "\n".join(lines) if lines else "Aucune donnée disponible pour l'instant."


SYSTEM_PROMPT_TEMPLATE = """Tu es un coach de course à pied de haut niveau, bienveillant, direct et concret.
Nous sommes aujourd'hui le {today}. Base-toi STRICTEMENT sur cette date pour tout calcul relatif
(nombre de jours depuis la dernière séance, "cette semaine", "hier", etc.) — ne devine jamais la date.
Tu réponds aux questions de ton athlète en t'appuyant STRICTEMENT sur les données ci-dessous.
Si une information demandée n'est pas dans les données, dis-le clairement plutôt que d'inventer un chiffre.
Donne des conseils actionnables et personnalisés, pas de généralités vagues qu'on trouverait dans
n'importe quel article générique. Réponds en français, de façon concise, sauf si on te demande
explicitement un plan détaillé ou une explication approfondie.

Voici les données actuelles de l'athlète :

{context}
"""


def create_chat_session(context: str, api_key: str = None):
    """
    Crée une session de chat Gemini avec le contexte des données et la date du
    jour injectés comme instruction système. Retourne (client, chat) : il faut
    garder une référence au client tant que la conversation dure, sinon Python
    peut le fermer tout seul entre deux questions (garbage collection) et
    provoquer une erreur "the client has been closed".
    """
    client = get_client(api_key)
    today_str = format_date_fr(dt.date.today())
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(context=context, today=today_str)
    chat = client.chats.create(
        model=MODEL,
        config=types.GenerateContentConfig(system_instruction=system_prompt),
    )
    return client, chat


def ask_coach(chat, question: str) -> str:
    """Envoie une question à la session de chat existante et retourne la réponse texte."""
    response = chat.send_message(question)
    return response.text
