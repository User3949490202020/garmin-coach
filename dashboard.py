"""
dashboard.py
------------
Lance avec : streamlit run dashboard.py
"""

import datetime as dt
import json
import os

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

import storage
import analysis
import coach_agent
import gpx_utils
import sync as sync_module
from providers.garmin import GarminProvider
from providers import strava

load_dotenv()

# Sur Streamlit Community Cloud, les secrets sont dans st.secrets (pas dans
# os.environ comme en local avec un .env) : on fait le pont pour que le reste
# du code (qui lit os.getenv) fonctionne pareil dans les deux cas.
try:
    for _key in ("GEMINI_API_KEY", "STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET",
                 "STRAVA_REDIRECT_URI"):
        if _key in st.secrets and not os.getenv(_key):
            os.environ[_key] = str(st.secrets[_key])
except Exception:
    pass

# Config Strava (renseignée par l'administrateur via secrets/.env). Si absente,
# la connexion Strava est simplement masquée — le reste de l'appli fonctionne.
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET")
STRAVA_REDIRECT_URI = os.getenv("STRAVA_REDIRECT_URI")
STRAVA_CONFIGURED = bool(STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET and STRAVA_REDIRECT_URI)

st.set_page_config(page_title="Coach Running Garmin", page_icon="🏃", layout="wide")


def mobile_friendly(fig):
    """
    Passe la légende en haut (horizontale) au lieu de sur le côté droit :
    sur mobile, une légende verticale à droite mange presque la moitié de
    l'écran et donne l'impression que le graphique "s'arrête au milieu".
    À appeler juste avant chaque st.plotly_chart(...).
    """
    fig.update_layout(
        legend=dict(orientation="h", yanchor="bottom", y=1.12, xanchor="center", x=0.5),
        margin=dict(l=10, r=10, t=60, b=10),
        autosize=True,
    )
    return fig


# Barre d'outils Plotly (appareil photo, zoom, pan...) masquée partout : sur
# mobile elle prend de la place et peut chevaucher la légende sans vraiment
# servir (le zoom tactile fonctionne déjà nativement au doigt).
PLOTLY_CONFIG = {"displayModeBar": False}

# Mode local mono-utilisateur (toi, avec ton .env) : si les identifiants sont
# dans le .env, on saute directement le formulaire de connexion, comme avant.
ENV_EMAIL = os.getenv("GARMIN_EMAIL")
ENV_PASSWORD = os.getenv("GARMIN_PASSWORD")
LOCAL_MODE = bool(ENV_EMAIL and ENV_PASSWORD)

st.title("🏃 Ton coach running personnel")
st.caption("Connecté à Garmin — analyse de tes séances, sommeil, FC et charge d'entraînement.")

with st.sidebar:
    if LOCAL_MODE:
        active_source = "garmin"
        garmin_email = ENV_EMAIL
        garmin_password = ENV_PASSWORD
        own_gemini_key = None
        USER_DB_PATH = None  # base par défaut, mode local
        storage.init_db(db_path=USER_DB_PATH)
    else:
        # Mode hébergé multi-utilisateurs : chacun se connecte avec SA source
        # (Garmin directement, ou Strava pour Suunto et les autres marques).
        st.header("Connexion")

        # --- Retour de l'autorisation Strava (Strava renvoie sur ?code=...) ---
        if (STRAVA_CONFIGURED and "code" in st.query_params
                and "strava_tokens" not in st.session_state):
            with st.spinner("Connexion à Strava..."):
                try:
                    _tok = strava.exchange_code(
                        STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, st.query_params["code"])
                    _db = storage.get_db_path_for_user(f"strava-{_tok['athlete_id']}")
                    storage.init_db(db_path=_db)
                    storage.save_strava_tokens(_tok, db_path=_db)
                    st.session_state.strava_tokens = _tok
                    st.session_state.active_source = "strava"
                    st.session_state.pop("strava_error", None)
                except Exception as e:
                    # On mémorise l'erreur pour l'afficher après le rerun, et on
                    # purge le `code` de l'URL : un code d'autorisation n'est
                    # utilisable qu'une fois, inutile de retenter en boucle.
                    st.session_state.strava_error = str(e)
            # Toujours nettoyer l'URL puis relancer (succès comme échec).
            st.query_params.clear()
            st.rerun()

        # Erreur de connexion Strava mémorisée lors d'un précédent essai.
        if st.session_state.get("strava_error"):
            st.error(f"Échec de la connexion Strava : {st.session_state.pop('strava_error')}")

        connected_garmin = "garmin_email" in st.session_state
        connected_strava = "strava_tokens" in st.session_state

        # --- Aucune source connectée : proposer le choix Garmin / Strava ---
        if not connected_garmin and not connected_strava:
            src_g, src_s = st.tabs(["⌚ Garmin", "🔶 Strava (Suunto & autres)"])
            with src_g:
                st.caption("Connecte-toi avec tes identifiants Garmin Connect. Ils restent en "
                           "mémoire le temps de ta session uniquement, jamais écrits sur le serveur.")
                with st.form("garmin_login"):
                    email_input = st.text_input("Email Garmin Connect")
                    password_input = st.text_input("Mot de passe Garmin Connect", type="password")
                    submitted = st.form_submit_button("Se connecter")
                if submitted:
                    if email_input and password_input:
                        st.session_state.garmin_email = email_input
                        st.session_state.garmin_password = password_input
                        st.session_state.active_source = "garmin"
                        st.rerun()
                    else:
                        st.error("Renseigne ton email et ton mot de passe Garmin.")
            with src_s:
                st.caption("Pour les montres **Suunto**, Coros, Polar… synchronisées vers Strava. "
                           "⚠️ La récupération (sommeil, HRV, FC repos) n'est **pas** disponible via Strava.")
                if not STRAVA_CONFIGURED:
                    st.info("La connexion Strava n'est pas encore configurée par l'administrateur de l'appli.")
                else:
                    st.link_button(
                        "🔶 Se connecter avec Strava",
                        strava.build_authorize_url(STRAVA_CLIENT_ID, STRAVA_REDIRECT_URI),
                    )
            st.stop()

        # --- Une source est connectée ---
        active_source = st.session_state.get(
            "active_source", "strava" if connected_strava else "garmin")
        garmin_email = st.session_state.get("garmin_email")
        garmin_password = st.session_state.get("garmin_password")
        own_gemini_key = st.session_state.get("own_gemini_key")

        # Chemin de base calculé explicitement à chaque script run, jamais stocké
        # dans une variable globale partagée : garantit qu'un usage simultané par
        # plusieurs personnes ne mélange jamais leurs données.
        if active_source == "strava":
            _tok = st.session_state.strava_tokens
            USER_DB_PATH = storage.get_db_path_for_user(f"strava-{_tok['athlete_id']}")
            storage.init_db(db_path=USER_DB_PATH)
            st.success(f"Connecté via Strava (athlète {_tok.get('athlete_id')})")
        else:
            USER_DB_PATH = storage.get_db_path_for_user(garmin_email)
            storage.init_db(db_path=USER_DB_PATH)
            st.success(f"Connecté : {garmin_email}")

        if st.button("Se déconnecter"):
            for key in ["garmin_email", "garmin_password", "own_gemini_key",
                       "chat_session", "gemini_client", "chat_display_history",
                       "garmin_client", "mfa_pending", "strava_tokens", "active_source"]:
                st.session_state.pop(key, None)
            st.query_params.clear()
            st.rerun()
        st.divider()

    st.header("Synchronisation")
    days = st.slider("Nombre de jours à synchroniser", 7, 90, 30)

    if active_source == "strava":
        if st.button("🔄 Synchroniser avec Strava maintenant"):
            with st.spinner("Récupération de tes séances depuis Strava..."):
                try:
                    def _save_tokens(t):
                        st.session_state.strava_tokens = t
                        storage.save_strava_tokens(t, db_path=USER_DB_PATH)
                    provider = strava.StravaProvider(
                        st.session_state.strava_tokens,
                        STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
                        on_token_refresh=_save_tokens,
                    )
                    sync_module.sync_data(provider, days=days, db_path=USER_DB_PATH)
                    st.success("Synchronisation réussie !")
                except Exception as e:
                    st.error(f"Erreur pendant la synchronisation Strava : {e}")
        st.caption("Tes séances Suunto apparaissent dans Strava quelques minutes après la "
                   "synchro de ta montre. La récupération (sommeil/HRV) n'est pas fournie par Strava.")

    # Garmin — double authentification (MFA) : Garmin envoie un code par SMS/email
    # lors de la connexion, et ce code est lié à la session de connexion en cours.
    # On garde donc le MÊME objet client (dans st.session_state) entre la
    # demande du code et sa validation — en recréer un invaliderait le code.
    elif st.session_state.get("mfa_pending"):
        st.warning("Ton compte Garmin a la double authentification (MFA) activée. "
                   "Entre le code de vérification à 6 chiffres.")
        st.caption("Selon la méthode configurée sur ton compte Garmin, ce code arrive "
                   "**par SMS**, **par email** (pense à vérifier tes spams), ou s'affiche "
                   "dans ton **appli d'authentification** (Google Authenticator, Authy…). "
                   "Si rien n'arrive au bout d'une minute, vérifie le numéro/email associé "
                   "à ton compte Garmin, puis réessaie dans quelques minutes.")
        mfa_code_input = st.text_input("Code de vérification Garmin", key="mfa_code_field")
        if st.button("✅ Valider le code et synchroniser"):
            with st.spinner("Vérification du code et synchronisation..."):
                try:
                    client = st.session_state.get("garmin_client")
                    if client is None:
                        # Session perdue (rechargement complet de la page) :
                        # on repart de la connexion.
                        st.session_state.mfa_pending = False
                        st.error("Session expirée, relance la synchronisation.")
                    else:
                        client.resume_with_mfa(mfa_code_input.strip())
                        sync_module.sync_data(client, days=days, db_path=USER_DB_PATH)
                        st.session_state.mfa_pending = False
                        st.success("Synchronisation réussie !")
                except Exception as e:
                    st.error(f"Code invalide ou expiré, réessaie. ({e})")
    else:
        if st.button("🔄 Synchroniser avec Garmin maintenant"):
            need_mfa = False
            with st.spinner("Connexion à Garmin Connect..."):
                try:
                    client = GarminProvider(email=garmin_email, password=garmin_password)
                    status = client.login()
                    st.session_state.garmin_client = client
                    if status == "needs_mfa":
                        st.session_state.mfa_pending = True
                        need_mfa = True
                    else:
                        sync_module.sync_data(client, days=days, db_path=USER_DB_PATH)
                        st.success("Synchronisation réussie !")
                except Exception as e:
                    st.error(f"Erreur pendant la synchronisation : {e}")
            # st.rerun() est hors du try : il lève une exception interne de
            # contrôle que le except ne doit pas intercepter.
            if need_mfa:
                st.rerun()
    st.divider()
    st.caption("Première utilisation ? Clique sur Synchroniser pour récupérer tes données.")

activities = storage.read_df("activities", db_path=USER_DB_PATH)
wellness = storage.read_df("wellness", db_path=USER_DB_PATH)
sleep = storage.read_df("sleep", db_path=USER_DB_PATH)
laps = storage.read_df("laps", db_path=USER_DB_PATH)

if activities.empty and wellness.empty:
    st.info("Aucune donnée pour l'instant. Clique sur "
            "**Synchroniser avec Garmin maintenant** dans le menu de gauche.")
    st.stop()

if not activities.empty:
    activities["date"] = pd.to_datetime(activities["date"])
if not wellness.empty:
    wellness["date"] = pd.to_datetime(wellness["date"])

tab_coach, tab_strava, tab_seances, tab_recup, tab_charge, tab_conseils = st.tabs(
    ["💬 Coach IA", "🔥 Stats Strava", "🏃 Séances", "😴 Récupération",
     "📈 Charge d'entraînement", "💡 Recommandations"]
)

# ----------------------------------------------------------------------
# Coach IA (en premier, pour un accès direct sans avoir à scroller)
# ----------------------------------------------------------------------
with tab_coach:
    st.subheader("💬 Discute avec ton coach IA")
    st.caption("Pose des questions sur tes séances, ta récupération, ta progression : l'agent répond "
               "en s'appuyant sur tes vraies données Garmin, pas sur des généralités.")

    if active_source == "strava":
        st.info("Le Coach IA n'est pas disponible avec une connexion **Strava** : les conditions "
                "d'utilisation de Strava ne permettent pas de transmettre tes données à un service "
                "d'IA tiers (Gemini). Il reste pleinement disponible pour les comptes Garmin.")
    elif not os.getenv("GEMINI_API_KEY"):
        st.warning("Il manque une clé API Gemini. Ajoute `GEMINI_API_KEY=...` dans ton fichier "
                   "`.env` puis relance l'application. Voir le README pour savoir comment l'obtenir "
                   "(c'est gratuit, aucune carte bancaire requise).")
    else:
        if "chat_display_history" not in st.session_state:
            st.session_state.chat_display_history = []

        weekly_ctx = analysis.weekly_stats(activities) if not activities.empty else pd.DataFrame()
        records_ctx = analysis.personal_records(activities) if not activities.empty else {}

        recovery_latest_ctx = None
        if not wellness.empty:
            rec_df = analysis.recovery_score(wellness)
            if not rec_df.empty and rec_df["recovery_score"].notna().any():
                recovery_latest_ctx = rec_df["recovery_score"].dropna().iloc[-1]

        acwr_latest_ctx = None
        if not activities.empty:
            loaded_ctx = analysis.training_load(activities)
            daily_ctx = loaded_ctx.groupby(loaded_ctx["date"].dt.date)["load"].sum()
            daily_ctx.index = pd.to_datetime(daily_ctx.index)
            full_range_ctx = pd.date_range(daily_ctx.index.min(), pd.Timestamp.now().normalize(), freq="D")
            daily_ctx = daily_ctx.reindex(full_range_ctx, fill_value=0)
            acwr_df_ctx = analysis.acwr(daily_ctx)
            if acwr_df_ctx["acwr"].notna().any():
                acwr_latest_ctx = acwr_df_ctx["acwr"].dropna().iloc[-1]

        best_efforts_ctx = {
            "5 km": analysis.best_effort_by_distance(activities, 5, months=6) if not activities.empty else None,
            "10 km": analysis.best_effort_by_distance(activities, 10, months=6) if not activities.empty else None,
            "Semi-marathon": analysis.best_effort_by_distance(activities, 21.1, months=6) if not activities.empty else None,
        }
        predictions_ctx = analysis.predict_race_times(activities, months=6) if not activities.empty else {}
        races_ctx = storage.read_df("races", db_path=USER_DB_PATH)

        context_summary = coach_agent.build_context_summary(
            activities, wellness, weekly_ctx, records_ctx, acwr_latest_ctx, recovery_latest_ctx, laps,
            sleep=sleep, races=races_ctx, predictions=predictions_ctx, best_efforts=best_efforts_ctx,
        )

        if "chat_session" not in st.session_state:
            try:
                gemini_client, chat_session = coach_agent.create_chat_session(
                    context_summary, api_key=own_gemini_key
                )
                st.session_state.gemini_client = gemini_client
                st.session_state.chat_session = chat_session
            except Exception as e:
                st.session_state.chat_session = None
                st.error(f"Impossible de démarrer la session avec Gemini : {e}")

        with st.expander("Voir les données transmises à l'agent"):
            st.text(context_summary)

        for msg in st.session_state.chat_display_history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        question = st.chat_input("Pose ta question au coach...")
        if question and st.session_state.chat_session is not None:
            st.session_state.chat_display_history.append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)
            with st.chat_message("assistant"):
                with st.spinner("Le coach réfléchit..."):
                    try:
                        answer = coach_agent.ask_coach(st.session_state.chat_session, question)
                    except Exception as e:
                        err_str = str(e)
                        if "503" in err_str or "UNAVAILABLE" in err_str or "overloaded" in err_str.lower():
                            answer = ("⏳ Les serveurs Gemini sont temporairement surchargés (ça arrive "
                                     "surtout sur le modèle gratuit aux heures de forte affluence). "
                                     "Ce n'est pas un problème avec ta configuration — réessaie simplement "
                                     "dans une minute.")
                        else:
                            answer = (f"Erreur en contactant l'API Gemini : {e}\n\n"
                                     f"Vérifie que ta clé `GEMINI_API_KEY` dans `.env` est valide.")
                st.markdown(answer)
            st.session_state.chat_display_history.append({"role": "assistant", "content": answer})

        if st.session_state.chat_display_history and st.button("🗑️ Effacer la conversation"):
            st.session_state.chat_display_history = []
            del st.session_state.chat_session
            if "gemini_client" in st.session_state:
                del st.session_state.gemini_client
            st.rerun()

# ----------------------------------------------------------------------
# Stats Strava
# ----------------------------------------------------------------------
with tab_strava:
    if activities.empty:
        st.info("Pas encore de séances synchronisées.")
    else:
        weekly = analysis.weekly_stats(activities)

        st.subheader("Kilométrage des dernières semaines")
        n = len(weekly)
        cols = st.columns(4)
        week_labels = ["S-2", "S-1", "Cette semaine"]
        for i, label in enumerate(week_labels):
            idx = n - (3 - i)
            val = weekly["distance_km"].iloc[idx] if 0 <= idx < n else 0.0
            cols[i].metric(label, f"{val:.1f} km")
        avg_4sem = weekly["moyenne_glissante_4sem_km"].iloc[-1] if not weekly.empty else 0
        cols[3].metric("Moyenne 4 semaines", f"{avg_4sem:.1f} km/sem")

        streak = analysis.current_streak_weeks(weekly)
        st.caption(f"🔥 {streak} semaine(s) active(s) d'affilée")

        st.subheader("Distance par semaine")
        st.caption("Les barres montrent chaque semaine, la ligne orange ta moyenne glissante sur 4 semaines.")
        fig = go.Figure()
        fig.add_trace(go.Bar(x=weekly["week_start"], y=weekly["distance_km"], name="Distance semaine",
                              marker_color="royalblue"))
        fig.add_trace(go.Scatter(x=weekly["week_start"], y=weekly["moyenne_glissante_4sem_km"],
                                  name="Moyenne 4 sem.", line=dict(color="orange", width=3)))
        st.plotly_chart(mobile_friendly(fig), width='stretch', config=PLOTLY_CONFIG)

        st.subheader("Dénivelé par semaine")
        st.caption("Même principe pour le dénivelé positif cumulé chaque semaine (6 derniers mois).")
        weekly_recent = weekly[weekly["week_start"] >= pd.Timestamp.now() - pd.DateOffset(months=6)]
        fig_elev = go.Figure()
        fig_elev.add_trace(go.Bar(x=weekly_recent["week_start"], y=weekly_recent["elevation_gain"],
                                   name="Dénivelé semaine", marker_color="firebrick"))
        fig_elev.add_trace(go.Scatter(x=weekly_recent["week_start"], y=weekly_recent["moyenne_glissante_4sem_elevation"],
                                       name="Moyenne 4 sem.", line=dict(color="darkorange", width=3)))
        fig_elev.update_layout(yaxis_title="Dénivelé (m)")
        st.plotly_chart(mobile_friendly(fig_elev), width='stretch', config=PLOTLY_CONFIG)

        st.subheader("🏆 Records personnels (6 derniers mois)")
        records_5k = analysis.best_effort_by_distance(activities, 5, months=6)
        records_10k = analysis.best_effort_by_distance(activities, 10, months=6)
        records_semi = analysis.best_effort_by_distance(activities, 21.1, months=6)
        record_entries = [("5 km", records_5k), ("10 km", records_10k), ("Semi-marathon", records_semi)]
        available = [(label, r) for label, r in record_entries if r is not None]

        if not available:
            st.caption("Pas encore de séance sur ces distances (5K / 10K / semi) au cours des 6 derniers mois.")
        else:
            rcols = st.columns(len(available))
            big_style = "text-align:center; font-size:2.2rem; font-weight:700; margin:0;"
            label_style = "text-align:center; color:gray; font-size:1rem; margin-bottom:0;"
            for col, (label, r) in zip(rcols, available):
                with col:
                    st.markdown(f"<p style='{label_style}'>{label}</p>", unsafe_allow_html=True)
                    st.markdown(f"<p style='{big_style}'>{analysis._format_time(r['temps_s'])}</p>",
                               unsafe_allow_html=True)
                    st.caption(f"{r['distance_km']:.2f} km — {r['date'].strftime('%d/%m/%Y')}")

        records_all = analysis.personal_records(activities)
        col5, col6 = st.columns(2)
        col5.metric("Distance totale enregistrée", f"{records_all.get('distance_totale_km', 0):.0f} km")
        col6.metric(f"Distance en {pd.Timestamp.now().year}", f"{records_all.get('distance_annee_courante_km', 0):.0f} km")

        st.subheader("🎯 Prédictions de temps de course")
        st.caption("Basées sur ta meilleure performance des 6 derniers mois (formule de Riegel) : "
                   "un ordre de grandeur, pas une science exacte.")
        preds = analysis.predict_race_times(activities, months=6)
        if not preds:
            st.caption("Pas encore assez de données récentes pour estimer tes temps de course.")
        else:
            ref = preds["reference"]
            st.caption(f"Référence : {ref['nom']} — {ref['distance_km']:.1f} km le "
                       f"{pd.to_datetime(ref['date']).strftime('%d/%m/%Y')}")
            pcols = st.columns(3)
            for col, key in zip(pcols, ["10K", "Semi", "Marathon"]):
                col.metric(key, preds[key]["temps_str"])

        st.subheader("Calendrier d'activité")
        st.caption("Chaque colonne = une semaine, chaque case = un jour. Plus c'est foncé, plus tu as couru loin ce jour-là.")
        cal = analysis.activity_calendar(activities, weeks=26)
        if not cal.empty:
            cal["weekday"] = cal["date"].dt.weekday
            cal["week_num"] = ((cal["date"] - cal["date"].min()).dt.days // 7)
            pivot = cal.pivot_table(index="weekday", columns="week_num", values="distance_km", fill_value=0)
            fig_cal = go.Figure(data=go.Heatmap(
                z=pivot.values, colorscale="Greens", showscale=False,
                hovertemplate="Distance: %{z:.1f} km<extra></extra>",
            ))
            fig_cal.update_layout(
                yaxis=dict(tickmode="array", tickvals=list(range(7)),
                           ticktext=["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]),
                xaxis=dict(showticklabels=False),
                height=250, margin=dict(t=10, b=10),
            )
            st.plotly_chart(mobile_friendly(fig_cal), width='stretch', config=PLOTLY_CONFIG)

# ----------------------------------------------------------------------
# Séances
# ----------------------------------------------------------------------
with tab_seances:
    if activities.empty:
        st.info("Pas encore de séances synchronisées.")
    else:
        eff = analysis.pace_efficiency(activities)
        st.subheader("Tendance d'efficacité allure / FC")
        st.caption("Courbe lissée : plus elle descend, plus tu vas vite pour le même effort cardiaque, "
                   "c'est un vrai signe de progrès.")
        if not eff.empty:
            fig = px.line(eff, x="date", y="efficacite", line_shape="spline", markers=True)
            fig.update_traces(line=dict(width=3, color="royalblue"))
            st.plotly_chart(mobile_friendly(fig), width='stretch', config=PLOTLY_CONFIG)

        st.subheader("Historique des séances")
        display_df = activities.sort_values("date", ascending=False).copy()
        display_df["allure_min_km"] = display_df["avg_pace_s_per_km"].apply(
            lambda s: f"{int(s // 60)}:{int(s % 60):02d}/km" if pd.notna(s) else "N/A"
        )
        display_df["jour"] = display_df["date"].apply(
            lambda d: coach_agent.FR_WEEKDAYS[d.weekday()].capitalize()
        )
        if "temp_c" in display_df.columns:
            display_df["météo"] = display_df.apply(
                lambda r: (f"{r['temp_c']:.0f}°C (ressenti {r['feels_like_c']:.0f}°C)"
                          if pd.notna(r.get("temp_c")) and pd.notna(r.get("feels_like_c"))
                          else (f"{r['temp_c']:.0f}°C" if pd.notna(r.get("temp_c")) else "N/A")),
                axis=1,
            )
        else:
            display_df["météo"] = "N/A"

        cols_to_show = ["date", "jour", "name", "distance_km", "allure_min_km",
                        "avg_hr", "avg_cadence", "elevation_gain", "météo"]
        st.dataframe(display_df[[c for c in cols_to_show if c in display_df.columns]], width='stretch')
        st.caption("La météo n'est récupérée automatiquement que pour les 15 séances les plus récentes "
                   "à chaque synchronisation (pour ne pas ralentir la sync).")

        st.subheader("🌡️ Indice de forme ajusté à la météo")
        adj = analysis.weather_adjusted_pace(activities)
        if adj.empty:
            st.caption("Pas encore assez de séances avec météo enregistrée pour calculer cet indice "
                       "(synchronise à nouveau pour en récupérer davantage).")
        else:
            st.caption(
                "La chaleur augmente le coût cardiovasculaire de la course : à effort égal (même FC), "
                "tu cours plus lentement quand il fait chaud. Cette courbe ramène toutes tes séances à "
                "une température neutre (~15°C) pour comparer ta vraie forme indépendamment de la météo. "
                "**Allure ajustée qui descend dans le temps = tu progresses réellement**, même si l'allure "
                "brute ne le montre pas forcément à cause de séances par forte chaleur."
            )
            fig_adj = go.Figure()
            fig_adj.add_trace(go.Scatter(
                x=adj["date"], y=adj["avg_pace_s_per_km"] / 60, name="Allure brute (min/km)",
                mode="lines+markers", line=dict(color="lightgray", width=2, dash="dot"),
            ))
            fig_adj.add_trace(go.Scatter(
                x=adj["date"], y=adj["allure_ajustee_s_per_km"] / 60, name="Allure ajustée météo (min/km)",
                mode="lines+markers", line=dict(color="crimson", width=3), line_shape="spline",
                customdata=adj["temp_c"],
                hovertemplate="%{x|%d/%m}<br>Ajustée: %{y:.2f} min/km<br>Température: %{customdata:.0f}°C<extra></extra>",
            ))
            fig_adj.update_layout(yaxis_title="Allure (min/km)", yaxis_autorange="reversed")
            st.plotly_chart(mobile_friendly(fig_adj), width='stretch', config=PLOTLY_CONFIG)
            st.caption("⚠️ Approximation basée sur une règle empirique (~0.6 %/°C au-dessus de 15°C), "
                       "pas un calcul physiologique individualisé — à lire comme une tendance, pas une "
                       "vérité chiffrée exacte.")

# ----------------------------------------------------------------------
# Récupération
# ----------------------------------------------------------------------
with tab_recup:
    if sleep.empty and wellness.empty:
        if active_source == "strava":
            st.info("La récupération (sommeil, HRV, FC repos, Body Battery) n'est pas disponible "
                    "via Strava. Ces données sont propres à l'écosystème de ta montre et ne "
                    "transitent pas par Strava — cet onglet reste donc vide pour les comptes Strava.")
        else:
            st.info("Pas encore de données de sommeil/récupération.")
    else:
        rec = analysis.recovery_score(wellness) if not wellness.empty else pd.DataFrame()
        if not rec.empty:
            st.subheader("Score de récupération quotidien")
            st.caption("Basé sur ta FC repos, ta HRV et ton Body Battery comparés à ta moyenne perso des 28 derniers jours.")
            fig = px.bar(rec, x="date", y="recovery_score", color="recovery_score",
                         color_continuous_scale=["red", "orange", "green"], range_color=[0, 100])
            st.plotly_chart(mobile_friendly(fig), width='stretch', config=PLOTLY_CONFIG)

        if not sleep.empty:
            st.subheader("Score de sommeil Garmin (6 derniers mois)")
            sleep_sorted = sleep.copy()
            sleep_sorted["date"] = pd.to_datetime(sleep_sorted["date"])
            sleep_sorted = sleep_sorted.sort_values("date")
            cutoff = pd.Timestamp.now() - pd.DateOffset(months=6)
            sleep_recent = sleep_sorted[sleep_sorted["date"] >= cutoff]
            sleep_with_score = sleep_recent.dropna(subset=["sleep_score"])
            if not sleep_with_score.empty:
                fig2 = px.line(sleep_with_score, x="date", y="sleep_score", line_shape="spline", markers=True)
                fig2.update_traces(line=dict(width=3, color="mediumpurple"))
                # Axe fixe 40-100 (au lieu d'un zoom automatique sur le min/max des
                # données) : ça évite de faire passer une petite variation pour
                # un effondrement, en gardant l'échelle réaliste du score Garmin.
                fig2.update_layout(yaxis=dict(range=[40, 100]))
                st.plotly_chart(mobile_friendly(fig2), width='stretch', config=PLOTLY_CONFIG)
            else:
                st.caption("Aucun score de sommeil exploitable sur les 6 derniers mois.")
                with st.expander("🔧 Debug : voir les données brutes de la dernière nuit synchronisée"):
                    if not sleep_sorted.empty:
                        last_raw = sleep_sorted.iloc[-1]
                        st.write(f"Date : {last_raw['date']}")
                        st.json(json.loads(last_raw["raw_json"]) if pd.notna(last_raw.get("raw_json")) else {})
                    else:
                        st.caption("Aucune donnée de sommeil du tout en base.")

        hrv_df = analysis.hrv_trend(wellness) if not wellness.empty else pd.DataFrame()
        if not hrv_df.empty:
            st.subheader("Variabilité de la fréquence cardiaque (HRV)")
            st.caption(
                "La HRV reflète l'état de ton système nerveux. Ce qui compte, c'est son évolution par "
                "rapport à **TA** moyenne perso (ligne pointillée), pas une valeur absolue universelle : "
                "zone verte = normal pour toi, orange = vigilance, rouge = fatigue/stress probable."
            )
            fig3 = go.Figure()
            # Zones colorées calculées par rapport à la moyenne glissante perso (28j)
            fig3.add_trace(go.Scatter(x=hrv_df["date"], y=hrv_df["baseline_28j"] * 1.15,
                                       line=dict(width=0), showlegend=False, hoverinfo="skip"))
            fig3.add_trace(go.Scatter(x=hrv_df["date"], y=hrv_df["seuil_orange"], fill="tonexty",
                                       fillcolor="rgba(0,180,0,0.12)", line=dict(width=0),
                                       name="Zone normale", hoverinfo="skip"))
            fig3.add_trace(go.Scatter(x=hrv_df["date"], y=hrv_df["seuil_rouge"], fill="tonexty",
                                       fillcolor="rgba(255,165,0,0.15)", line=dict(width=0),
                                       name="Vigilance", hoverinfo="skip"))
            fig3.add_trace(go.Scatter(x=hrv_df["date"], y=hrv_df["seuil_rouge"] * 0.7, fill="tonexty",
                                       fillcolor="rgba(255,0,0,0.15)", line=dict(width=0),
                                       name="Fatigue probable", hoverinfo="skip"))
            fig3.add_trace(go.Scatter(x=hrv_df["date"], y=hrv_df["baseline_28j"], name="Ta moyenne (28j)",
                                       line=dict(color="gray", width=1, dash="dot")))
            fig3.add_trace(go.Scatter(x=hrv_df["date"], y=hrv_df["hrv_lisse"], name="HRV (lissée 3j)",
                                       line=dict(color="teal", width=3)))
            fig3.update_layout(yaxis_title="HRV (ms)")
            st.plotly_chart(mobile_friendly(fig3), width='stretch', config=PLOTLY_CONFIG)

# ----------------------------------------------------------------------
# Charge d'entraînement / ACWR
# ----------------------------------------------------------------------
with tab_charge:
    if activities.empty:
        st.info("Pas encore de séances synchronisées.")
    else:
        st.markdown(
            "**Comment lire cette page ?** Ton corps s'adapte à l'entraînement seulement si la charge "
            "augmente progressivement. Si elle grimpe trop vite par rapport à ce à quoi tu es habitué, "
            "le risque de blessure (tendinite, périostite...) augmente nettement — même si tu te sens "
            "bien sur le moment, car la fatigue tissulaire est souvent invisible avant de devenir une "
            "vraie blessure.\n\n"
            "- **Charge aiguë (7 jours)** : ce que tu as fait récemment, ta charge \"sur le vif\".\n"
            "- **Charge chronique (28 jours)** : ta charge habituelle du dernier mois, ton niveau de "
            "fitness accumulé.\n"
            "- **ACWR** = charge aiguë ÷ charge chronique. C'est le *ratio* qui compte, pas les valeurs brutes."
        )

        loaded = analysis.training_load(activities)
        daily = loaded.groupby(loaded["date"].dt.date)["load"].sum()
        daily.index = pd.to_datetime(daily.index)
        full_range = pd.date_range(daily.index.min(), pd.Timestamp.now().normalize(), freq="D")
        daily = daily.reindex(full_range, fill_value=0)

        acwr_df = analysis.acwr(daily)
        st.subheader("Charge aiguë (7j) vs charge chronique (28j)")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=acwr_df.index, y=acwr_df["charge_aigue_7j"], name="Charge aiguë (7j)"))
        fig.add_trace(go.Scatter(x=acwr_df.index, y=acwr_df["charge_chronique_28j"], name="Charge chronique (28j)"))
        st.plotly_chart(mobile_friendly(fig), width='stretch', config=PLOTLY_CONFIG)

        fig2 = go.Figure()
        fig2.add_trace(go.Scatter(x=acwr_df.index, y=acwr_df["acwr"], name="ACWR"))
        fig2.add_hrect(y0=0.8, y1=1.3, fillcolor="green", opacity=0.1, line_width=0)
        fig2.add_hrect(y0=1.3, y1=1.5, fillcolor="orange", opacity=0.1, line_width=0)
        max_y = acwr_df["acwr"].max() if acwr_df["acwr"].notna().any() else 2
        fig2.add_hrect(y0=1.5, y1=max_y, fillcolor="red", opacity=0.1, line_width=0)
        fig2.update_layout(title="Ratio ACWR (zone verte = optimale)")
        st.plotly_chart(mobile_friendly(fig2), width='stretch', config=PLOTLY_CONFIG)

        last_val = acwr_df["acwr"].dropna().iloc[-1] if acwr_df["acwr"].notna().any() else None
        zone = analysis.acwr_zone(last_val)
        st.metric("ACWR actuel", f"{last_val:.2f}" if last_val is not None else "N/A", help=zone)

        zone_explanations = {
            "Sous-entraînement": "Ta charge récente est plus basse que ton habitude : marge pour progresser sans risque particulier.",
            "Zone optimale": "Ta progression est saine : ta charge récente est cohérente avec ton niveau de fitness accumulé.",
            "Vigilance": "Ta charge a augmenté plus vite que ta charge habituelle : reste attentif aux signaux de fatigue.",
            "Risque élevé de blessure": "Ta charge a augmenté trop vite : c'est dans cette zone que le risque de blessure de surcharge grimpe le plus.",
            "Données insuffisantes": "Pas encore assez d'historique pour calculer ce ratio de façon fiable.",
        }
        st.info(zone_explanations.get(zone, ""))

# ----------------------------------------------------------------------
# Recommandations (+ gestion de course et phase d'entraînement)
# ----------------------------------------------------------------------
with tab_conseils:
    st.subheader("🏁 Course préparée")

    with st.expander("➕ Ajouter une course"):
        with st.form("add_race_form"):
            rname = st.text_input("Nom de la course")
            rdate = st.date_input("Date de la course", min_value=dt.date.today())
            rdist = st.number_input("Distance (km)", min_value=1.0, value=10.0, step=0.1)
            rgpx = st.file_uploader("Parcours GPX (optionnel, pour le profil de dénivelé)", type=["gpx"])
            submitted = st.form_submit_button("Enregistrer cette course")
            if submitted and rname:
                elevation_gain, profile_json = None, None
                if rgpx is not None:
                    try:
                        parsed = gpx_utils.parse_gpx(rgpx)
                        elevation_gain = parsed["elevation_gain"]
                        profile_json = json.dumps(parsed["profile"])
                    except Exception as e:
                        st.warning(f"Impossible de lire le fichier GPX : {e}")
                storage.add_race({
                    "name": rname,
                    "date": rdate.isoformat(),
                    "distance_km": rdist,
                    "elevation_gain": elevation_gain,
                    "elevation_profile_json": profile_json,
                }, db_path=USER_DB_PATH)
                st.success(f"Course « {rname} » ajoutée !")
                st.rerun()

    races_df = storage.read_df("races", db_path=USER_DB_PATH)
    active_race = None
    if not races_df.empty:
        options = races_df["id"].tolist()
        labels = {row["id"]: f"{row['name']} — {row['date']}" for _, row in races_df.iterrows()}
        default_idx = 0
        active_rows = races_df[races_df["is_active"] == 1]
        if not active_rows.empty:
            default_idx = options.index(active_rows.iloc[0]["id"])
        selected = st.selectbox("Sélectionne ta course de préparation", options=options,
                                format_func=lambda i: labels[i], index=default_idx)
        if selected is not None:
            storage.set_active_race(int(selected), db_path=USER_DB_PATH)
            active_race = races_df[races_df["id"] == selected].iloc[0]
    else:
        st.caption("Aucune course enregistrée pour l'instant. Ajoute-en une ci-dessus pour un plan "
                   "personnalisé par phase (VMA → Seuil → Affûtage).")

    if active_race is not None:
        race_date = pd.to_datetime(active_race["date"]).date()

        focus_labels = {
            "vma": "Fractionné / VMA",
            "volume": "Volume / endurance de fond",
            "mixte": "Préparation générale (mixte)",
        }
        focus_choice = st.selectbox(
            "Ton focus pour la phase de fond (loin de la course)",
            options=list(focus_labels.keys()),
            format_func=lambda k: focus_labels[k],
            index=0,
            help="Chacun a ses propres objectifs : choisis ce qui correspond à TON plan, "
                 "ce n'est pas figé pour tout le monde.",
        )
        phase = analysis.training_phase(race_date, focus_style=focus_choice)
        days_left = (race_date - dt.date.today()).days

        col1, col2 = st.columns([1, 2])
        col1.metric(f"Jours avant {active_race['name']}", days_left)
        with col2:
            st.markdown(f"#### Phase actuelle : {phase['phase']}")
            st.write(phase["description"])

        if pd.notna(active_race.get("elevation_profile_json")) and active_race.get("elevation_profile_json"):
            profile = json.loads(active_race["elevation_profile_json"])
            if profile:
                st.subheader("Profil du parcours")
                prof_df = pd.DataFrame(profile)
                fig_elev = px.area(prof_df, x="distance_km", y="elevation_m")
                fig_elev.update_layout(yaxis_title="Altitude (m)", xaxis_title="Distance (km)")
                st.plotly_chart(mobile_friendly(fig_elev), width='stretch', config=PLOTLY_CONFIG)
                if pd.notna(active_race.get("elevation_gain")):
                    st.metric("Dénivelé positif total (D+)", f"{active_race['elevation_gain']:.0f} m")

    st.divider()
    st.subheader("💡 Conseils du jour")
    acwr_val = None
    recovery_val = None
    days_since_rest = None
    resting_today = False
    last_activity_date = None
    last_wellness_date = None

    if not activities.empty:
        last_activity_date = activities["date"].max()
        loaded = analysis.training_load(activities)
        daily = loaded.groupby(loaded["date"].dt.date)["load"].sum()
        daily.index = pd.to_datetime(daily.index)
        full_range = pd.date_range(daily.index.min(), pd.Timestamp.now().normalize(), freq="D")
        daily = daily.reindex(full_range, fill_value=0)
        acwr_df = analysis.acwr(daily)
        if acwr_df["acwr"].notna().any():
            acwr_val = acwr_df["acwr"].dropna().iloc[-1]

        today_normalized = pd.Timestamp.now().normalize()
        resting_today = daily.get(today_normalized, 0) == 0

        rest_days = (daily.tail(14) == 0)
        days_since_rest = 0
        for val in rest_days[::-1]:
            if val:
                break
            days_since_rest += 1

    if not wellness.empty:
        last_wellness_date = wellness["date"].max()
        rec = analysis.recovery_score(wellness)
        if not rec.empty and rec["recovery_score"].notna().any():
            recovery_val = rec["recovery_score"].dropna().iloc[-1]

    freshness_bits = []
    if last_activity_date is not None:
        freshness_bits.append(f"dernière séance connue : {last_activity_date.strftime('%d/%m/%Y')}")
    if last_wellness_date is not None:
        freshness_bits.append(f"dernières données de récupération : {last_wellness_date.strftime('%d/%m/%Y')}")
    if freshness_bits:
        st.caption("📅 " + " · ".join(freshness_bits) +
                   " — si ce n'est pas aujourd'hui, resynchronise avant de lire les conseils ci-dessous.")

    manual_rest = st.checkbox(
        "☑️ Je suis en repos aujourd'hui (coche si l'appli ne l'a pas encore détecté automatiquement)",
        value=False,
    )
    if manual_rest:
        resting_today = True
        days_since_rest = 0

    tips = analysis.generate_recommendations(acwr_val, recovery_val, days_since_rest, resting_today)
    for tip in tips:
        st.write("- " + tip)

    if not tips:
        st.info("Synchronise davantage de données pour obtenir des recommandations personnalisées.")
