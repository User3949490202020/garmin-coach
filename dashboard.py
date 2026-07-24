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

        # --- Reconnexion automatique via le jeton de session dans l'URL ---
        # Streamlit perd la mémoire de session à chaque coupure (onglet
        # inactif, téléphone verrouillé...). Le jeton aléatoire "s" dans l'URL
        # permet de retrouver QUI était connecté sans redemander les
        # identifiants. Le mot de passe n'est jamais stocké : la session
        # Garmin en cache (token_store) suffit pour resynchroniser.
        if ("garmin_email" not in st.session_state
                and "strava_tokens" not in st.session_state
                and "code" not in st.query_params):
            _sess = storage.read_session_token(st.query_params.get("s"))
            if _sess:
                _src, _ident = _sess
                if _src == "garmin":
                    st.session_state.garmin_email = _ident
                    st.session_state.garmin_password = None  # session Garmin en cache
                    st.session_state.active_source = "garmin"
                else:
                    _db = storage.get_db_path_for_user(f"strava-{_ident}")
                    storage.init_db(db_path=_db)
                    _tok = storage.read_strava_tokens(db_path=_db)
                    if _tok:
                        st.session_state.strava_tokens = _tok
                        st.session_state.active_source = "strava"

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
                    # Jeton de reconnexion automatique (la synchro auto à
                    # l'ouverture prend le relais après le rerun)
                    st.session_state.reconnect_token = storage.create_session_token(
                        "strava", _tok["athlete_id"])
                except Exception as e:
                    # On mémorise l'erreur pour l'afficher après le rerun, et on
                    # purge le `code` de l'URL : un code d'autorisation n'est
                    # utilisable qu'une fois, inutile de retenter en boucle.
                    st.session_state.strava_error = str(e)
            # Toujours nettoyer l'URL puis relancer (succès comme échec).
            st.query_params.clear()
            if st.session_state.get("reconnect_token"):
                st.query_params["s"] = st.session_state.reconnect_token
            st.rerun()

        # Erreur de connexion Strava mémorisée lors d'un précédent essai.
        if st.session_state.get("strava_error"):
            st.error(f"Échec de la connexion Strava : {st.session_state.pop('strava_error')}")

        connected_garmin = "garmin_email" in st.session_state
        connected_strava = "strava_tokens" in st.session_state

        # --- Aucune source connectée : proposer le choix Garmin / Strava ---
        if not connected_garmin and not connected_strava:
            if st.session_state.pop("session_expired_msg", False):
                st.warning("Ta session Garmin a expiré — ressaisis ton mot de passe pour te "
                           "reconnecter. Tes données, ton objectif et ton historique de "
                           "conversation sont conservés.")
            src_g, src_s = st.tabs(["⌚ Garmin", "🔶 Strava (Suunto & autres)"])
            with src_g:
                st.caption("Connecte-toi avec tes identifiants Garmin Connect. Ton mot de passe "
                           "n'est jamais enregistré sur le serveur ; ton email est associé à un "
                           "identifiant de session aléatoire pour te reconnecter automatiquement.")
                with st.form("garmin_login"):
                    email_input = st.text_input("Email Garmin Connect")
                    password_input = st.text_input("Mot de passe Garmin Connect", type="password")
                    submitted = st.form_submit_button("Se connecter")
                if submitted:
                    if email_input and password_input:
                        st.session_state.garmin_email = email_input
                        st.session_state.garmin_password = password_input
                        st.session_state.active_source = "garmin"
                        # Jeton de reconnexion (email seul, jamais le mot de
                        # passe) ; la synchro auto se déclenche après le rerun.
                        tok = storage.create_session_token("garmin", email_input)
                        st.session_state.reconnect_token = tok
                        st.query_params["s"] = tok
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
            # Invalide aussi le jeton de reconnexion automatique
            tok = st.session_state.get("reconnect_token") or st.query_params.get("s")
            if tok:
                storage.delete_session_token(tok)
            for key in ["garmin_email", "garmin_password", "own_gemini_key",
                       "chat_session", "gemini_client", "chat_display_history",
                       "garmin_client", "mfa_pending", "strava_tokens", "active_source",
                       "reconnect_token", "auto_sync_done"]:
                st.session_state.pop(key, None)
            st.query_params.clear()
            st.rerun()
        st.divider()

    st.header("Synchronisation")
    # Fenêtres de synchro fixes : 6 mois de séances, 30 jours de récupération
    # (sommeil/FC repos/HRV). L'ancien curseur "nombre de jours" ne réglait que
    # la récupération, ce qui était trompeur — retiré pour simplifier.
    days = 30

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
                    storage.save_manual_note("last_sync_ts", dt.datetime.now().timestamp(),
                                             db_path=USER_DB_PATH)
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
                        storage.save_manual_note("last_sync_ts", dt.datetime.now().timestamp(),
                                                 db_path=USER_DB_PATH)
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
                        storage.save_manual_note("last_sync_ts", dt.datetime.now().timestamp(),
                                                 db_path=USER_DB_PATH)
                        st.success("Synchronisation réussie !")
                except Exception as e:
                    st.error(f"Erreur pendant la synchronisation : {e}")
            # st.rerun() est hors du try : il lève une exception interne de
            # contrôle que le except ne doit pas intercepter.
            if need_mfa:
                st.rerun()

    # --- Synchronisation automatique à l'ouverture ---
    # Une seule tentative par session de navigation, et uniquement si la
    # dernière synchro date de plus de 6 h : inutile de solliciter Garmin/
    # Strava à chaque rechargement (risque de limitation 429 côté Garmin).
    if not st.session_state.get("auto_sync_done") and not st.session_state.get("mfa_pending"):
        st.session_state.auto_sync_done = True
        _last = storage.read_manual_note("last_sync_ts", db_path=USER_DB_PATH)
        _stale = _last is None or (dt.datetime.now().timestamp() - _last[0]) > 6 * 3600
        if _stale:
            with st.spinner("🔄 Synchronisation automatique en cours..."):
                try:
                    if active_source == "strava":
                        def _save_tokens_auto(t):
                            st.session_state.strava_tokens = t
                            storage.save_strava_tokens(t, db_path=USER_DB_PATH)
                        _prov = strava.StravaProvider(
                            st.session_state.strava_tokens,
                            STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET,
                            on_token_refresh=_save_tokens_auto)
                        sync_module.sync_data(_prov, days=days, db_path=USER_DB_PATH)
                        storage.save_manual_note("last_sync_ts", dt.datetime.now().timestamp(),
                                                 db_path=USER_DB_PATH)
                        st.success("Données à jour !")
                    else:
                        _client = GarminProvider(email=garmin_email, password=garmin_password)
                        _status = _client.login()
                        st.session_state.garmin_client = _client
                        if _status == "needs_mfa":
                            st.session_state.mfa_pending = True
                        else:
                            sync_module.sync_data(_client, days=days, db_path=USER_DB_PATH)
                            storage.save_manual_note("last_sync_ts", dt.datetime.now().timestamp(),
                                                     db_path=USER_DB_PATH)
                            st.success("Données à jour !")
                except Exception as e:
                    if garmin_password is None and active_source == "garmin" and (
                            "password" in str(e).lower() or "authent" in str(e).lower()):
                        # Session restaurée mais cache Garmin expiré : on
                        # redemande le mot de passe (jamais stocké, par choix).
                        # Données, objectif et conversations sont conservés.
                        st.session_state.pop("garmin_email", None)
                        st.session_state.session_expired_msg = True
                        st.rerun()
                    st.warning(f"Synchro automatique impossible pour l'instant — tu peux "
                               f"réessayer avec le bouton. ({e})")
            if st.session_state.get("mfa_pending"):
                st.rerun()

    _last_sync = storage.read_manual_note("last_sync_ts", db_path=USER_DB_PATH)
    if _last_sync:
        _age_h = (dt.datetime.now().timestamp() - _last_sync[0]) / 3600
        st.caption(f"Dernière synchro : il y a {'moins d’1 h' if _age_h < 1 else f'{int(_age_h)} h'} "
                   "— synchro auto à l'ouverture si plus de 6 h.")

    st.divider()
    st.caption("Première utilisation ? Clique sur Synchroniser pour récupérer tes données.")

activities = storage.read_df("activities", db_path=USER_DB_PATH)
wellness = storage.read_df("wellness", db_path=USER_DB_PATH)
sleep = storage.read_df("sleep", db_path=USER_DB_PATH)
laps = storage.read_df("laps", db_path=USER_DB_PATH)
cross_training = storage.read_df("cross_training", db_path=USER_DB_PATH)

if activities.empty and wellness.empty:
    st.info("Aucune donnée pour l'instant. Clique sur "
            "**Synchroniser avec Garmin maintenant** dans le menu de gauche.")
    st.stop()

if not activities.empty:
    activities["date"] = pd.to_datetime(activities["date"])
if not wellness.empty:
    wellness["date"] = pd.to_datetime(wellness["date"])

tab_coach, tab_strava, tab_seances, tab_recup, tab_charge = st.tabs(
    ["💬 Coach IA", "🔥 Stats Strava", "🏃 Séances", "😴 Récupération",
     "📈 Charge d'entraînement"]
)

# ----------------------------------------------------------------------
# Coach IA (en premier, pour un accès direct sans avoir à scroller)
# ----------------------------------------------------------------------
with tab_coach:
    st.subheader("💬 Discute avec ton coach IA")
    st.caption("Pose des questions sur tes séances, ta récupération, ta progression : l'agent répond "
               "en s'appuyant sur tes vraies données, pas sur des généralités.")

    # --- Objectif persistant : le coach le connaît sans que tu le répètes ---
    saved_obj = storage.read_text_note("objectif", db_path=USER_DB_PATH)
    with st.expander("🎯 Mon objectif du moment"
                     + (f" — défini le {pd.to_datetime(saved_obj[1]).strftime('%d/%m/%Y')}" if saved_obj else ""),
                     expanded=not saved_obj):
        st.caption("Écris ton objectif ici une fois : le coach en tiendra compte dans **toutes** ses "
                   "réponses, sans que tu aies à le répéter à chaque message.")
        obj_text = st.text_area(
            "Ton objectif", value=saved_obj[0] if saved_obj else "",
            placeholder="Ex : « Atteindre 70 km/semaine d'ici 3 semaines » — ou « Courir un 10 km "
                        "en moins de 45 min le mois prochain » — ou « Reprendre en douceur après blessure ».",
            key="objectif_field", height=80,
        )
        if st.button("💾 Enregistrer mon objectif"):
            storage.save_text_note("objectif", obj_text.strip(), db_path=USER_DB_PATH)
            # On repart d'une conversation neuve pour que le nouvel objectif soit
            # pris en compte immédiatement (le contexte est figé au démarrage du chat).
            for k in ["chat_session", "gemini_client"]:
                st.session_state.pop(k, None)
            st.success("Objectif enregistré ! Le coach en tient compte dès maintenant.")
            st.rerun()

    if not os.getenv("GEMINI_API_KEY"):
        st.warning("Il manque une clé API Gemini. Ajoute `GEMINI_API_KEY=...` dans ton fichier "
                   "`.env` puis relance l'application. Voir le README pour savoir comment l'obtenir "
                   "(c'est gratuit, aucune carte bancaire requise).")
    else:
        if "chat_display_history" not in st.session_state:
            # Reprend la conversation des 12 dernières heures (persistée en
            # base par utilisateur) : une coupure de session ne l'efface plus.
            st.session_state.chat_display_history = storage.read_chat_history(
                hours=12, db_path=USER_DB_PATH)

        weekly_ctx = analysis.weekly_stats(activities) if not activities.empty else pd.DataFrame()
        records_ctx = analysis.personal_records(activities) if not activities.empty else {}

        recovery_latest_ctx = None
        if not wellness.empty:
            rec_df = analysis.recovery_score(wellness, sleep)
            if not rec_df.empty and rec_df["recovery_score"].notna().any():
                recovery_latest_ctx = rec_df["recovery_score"].dropna().iloc[-1]

        acwr_latest_ctx = None
        if not activities.empty:
            daily_ctx = analysis.daily_training_load(activities, cross_training)
            if not daily_ctx.empty:
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
            cross_training=cross_training,
            objective=storage.read_text_note("objectif", db_path=USER_DB_PATH),
        )

        if "chat_session" not in st.session_state:
            # Si une conversation récente existe (reprise après coupure), on la
            # réinjecte dans le contexte : le coach « se souvient » des échanges
            # des 12 dernières heures.
            if st.session_state.chat_display_history:
                recent = st.session_state.chat_display_history[-12:]
                convo = "\n".join(
                    f"- {'Athlète' if m['role'] == 'user' else 'Coach'} : {m['content'][:500]}"
                    for m in recent)
                context_summary += ("\n\n### Reprise de conversation (échanges des 12 dernières heures)\n"
                                    "Continue naturellement cette conversation déjà entamée :\n" + convo)
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
            storage.append_chat_message("user", question, db_path=USER_DB_PATH)
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
            storage.append_chat_message("assistant", answer, db_path=USER_DB_PATH)

        if st.session_state.chat_display_history and st.button("🗑️ Effacer la conversation"):
            st.session_state.chat_display_history = []
            storage.clear_chat_history(db_path=USER_DB_PATH)
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

        st.subheader("Répartition des séances par intensité")
        st.caption("Chaque semaine : combien de séances faciles (vert), moyennes (orange) et dures (rouge), "
                   "d'après ta FC moyenne rapportée à ta FC max (<75% facile, 75-85% moyen, >85% dur). "
                   "Un bon équilibre = beaucoup de vert, un peu d'orange, du rouge avec parcimonie.")
        intens = analysis.session_intensity(activities)
        intens_recent = intens[intens["date"] >= pd.Timestamp.now() - pd.DateOffset(months=6)].copy()
        if not intens_recent.empty:
            intens_recent["week_start"] = (intens_recent["date"] - pd.to_timedelta(
                intens_recent["date"].dt.weekday, unit="D")).dt.normalize()
            counts = (intens_recent.groupby(["week_start", "intensite"]).size()
                      .reset_index(name="nb_seances"))
            fig_int = px.bar(
                counts, x="week_start", y="nb_seances", color="intensite",
                color_discrete_map={"Facile": "seagreen", "Moyen": "orange",
                                    "Dur": "crimson", "Non classée": "lightgray"},
                category_orders={"intensite": ["Facile", "Moyen", "Dur", "Non classée"]},
            )
            fig_int.update_layout(barmode="stack", yaxis_title="Nb de séances",
                                  xaxis_title="", legend_title_text="")
            st.plotly_chart(mobile_friendly(fig_int), width='stretch', config=PLOTLY_CONFIG)

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

        st.subheader("💪 Renfo & étirements par semaine")
        st.caption("Objectif : **2 séances de musculation** et **2 séances d'étirements** (yoga sur "
                   "ta montre) par semaine — la ligne pointillée marque l'objectif. Données tirées "
                   "directement de ta montre Garmin.")
        if cross_training.empty:
            st.caption("Aucune séance de renfo/étirements synchronisée pour l'instant — resynchronise "
                       "pour récupérer aussi tes séances de yoga.")
        else:
            ct = cross_training.copy()
            ct["date"] = pd.to_datetime(ct["date"])
            ct = ct[ct["date"] >= pd.Timestamp.now() - pd.DateOffset(months=6)]
            if not ct.empty:
                ct["categorie"] = ct["sport"].astype(str).str.contains("yoga", case=False).map(
                    {True: "Étirements", False: "Musculation"})
                ct["week_start"] = (ct["date"] - pd.to_timedelta(ct["date"].dt.weekday, unit="D")).dt.normalize()
                ct_counts = ct.groupby(["week_start", "categorie"]).size().reset_index(name="nb")
                fig_ct = px.bar(
                    ct_counts, x="week_start", y="nb", color="categorie", barmode="group",
                    color_discrete_map={"Musculation": "mediumpurple", "Étirements": "lightseagreen"},
                    category_orders={"categorie": ["Musculation", "Étirements"]},
                )
                fig_ct.add_hline(y=2, line_dash="dash", line_color="gray",
                                 annotation_text="Objectif : 2/sem")
                fig_ct.update_layout(yaxis_title="Nb de séances", xaxis_title="", legend_title_text="",
                                     yaxis=dict(dtick=1))
                st.plotly_chart(mobile_friendly(fig_ct), width='stretch', config=PLOTLY_CONFIG)

# ----------------------------------------------------------------------
# Séances
# ----------------------------------------------------------------------
with tab_seances:
    if activities.empty:
        st.info("Pas encore de séances synchronisées.")
    else:
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
            # 3e courbe : la température de chaque séance, sur un axe séparé (°C à
            # droite) — on visualise d'un coup d'œil quand la chaleur explique
            # l'écart entre allure brute et allure ajustée.
            fig_adj.add_trace(go.Scatter(
                x=adj["date"], y=adj["temp_c"], name="Température (°C)", yaxis="y2",
                mode="lines+markers", line=dict(color="goldenrod", width=2), opacity=0.7,
                hovertemplate="%{x|%d/%m}<br>%{y:.0f}°C<extra></extra>",
            ))
            fig_adj.update_layout(
                yaxis_title="Allure (min/km)", yaxis_autorange="reversed",
                yaxis2=dict(title="°C", overlaying="y", side="right", showgrid=False),
            )
            st.plotly_chart(mobile_friendly(fig_adj), width='stretch', config=PLOTLY_CONFIG)
            st.caption("⚠️ Approximation basée sur une règle empirique (~0.6 %/°C au-dessus de 15°C), "
                       "pas un calcul physiologique individualisé — à lire comme une tendance, pas une "
                       "vérité chiffrée exacte.")

# ----------------------------------------------------------------------
# Récupération
# ----------------------------------------------------------------------
with tab_recup:
    # --- Saisie manuelle d'une nuit (montre non portée) ---
    # La synchro Garmin reste la référence : la saisie manuelle comble les
    # trous nuit par nuit, et si Garmin a une vraie mesure pour cette date,
    # elle reprend le dessus à la prochaine synchro. Les nuits saisies entrent
    # dans le graphique de sommeil et le contexte du coach IA comme les autres.
    with st.expander("📝 Saisir une nuit à la main (montre non portée)"):
        manual_date = st.date_input("Nuit du", value=dt.date.today() - dt.timedelta(days=1),
                                    max_value=dt.date.today(), key="manual_sleep_date")
        manual_score = st.slider("Note sommeil (0-100)", 0, 100, 70, key="manual_sleep_score")
        manual_hours = st.number_input("Durée dormie (heures)", min_value=0.0, max_value=14.0,
                                       value=7.5, step=0.5, key="manual_sleep_hours")
        if st.button("💾 Enregistrer cette nuit"):
            date_iso = manual_date.isoformat()
            already = sleep[sleep["date"].astype(str).str[:10] == date_iso] if not sleep.empty else pd.DataFrame()
            is_garmin_row = (not already.empty
                             and "manual" not in str(already.iloc[0].get("raw_json") or ""))
            if is_garmin_row:
                st.warning("Ta montre a déjà mesuré cette nuit-là — la donnée Garmin est "
                           "conservée (pas besoin de saisie manuelle).")
            else:
                storage.upsert_sleep({
                    "date": date_iso,
                    "sleep_score": float(manual_score),
                    "total_sleep_s": manual_hours * 3600,
                    "deep_sleep_s": None, "light_sleep_s": None,
                    "rem_sleep_s": None, "awake_s": None, "nap_s": None,
                    "raw_json": json.dumps({"manual": True}),
                }, db_path=USER_DB_PATH)
                st.success(f"Nuit du {manual_date.strftime('%d/%m/%Y')} enregistrée !")
                st.rerun()

    if sleep.empty and wellness.empty:
        if active_source == "strava":
            st.info("La récupération (sommeil, HRV, FC repos, Body Battery) n'est pas disponible "
                    "via Strava. Ces données sont propres à l'écosystème de ta montre et ne "
                    "transitent pas par Strava — cet onglet reste donc vide pour les comptes Strava.")
        else:
            st.info("Pas encore de données de sommeil/récupération.")
    else:
        rec = analysis.recovery_score(wellness, sleep) if not wellness.empty else pd.DataFrame()
        if not rec.empty:
            st.subheader("Score de récupération quotidien")
            st.caption("Basé sur ta FC repos, ta HRV, ton Body Battery, ton stress et tes pas comparés "
                       "à ta moyenne perso des 28 derniers jours. Tes siestes comptent aussi : même "
                       "10 minutes ajoutent un petit bonus au score du jour.")
            # 3 zones à couleur fixe (plutôt qu'un dégradé continu par barre,
            # beaucoup moins lisible d'un coup d'œil) : 50 = ta moyenne perso.
            def _recovery_zone(v):
                if pd.isna(v):
                    return "Donnée manquante"
                if v < 35:
                    return "Fatigue"
                if v <= 65:
                    return "Normal"
                return "Bonne récup"
            rec = rec.copy()
            rec["zone"] = rec["recovery_score"].apply(_recovery_zone)
            fig = px.bar(
                rec, x="date", y="recovery_score", color="zone",
                color_discrete_map={"Fatigue": "crimson", "Normal": "orange",
                                    "Bonne récup": "seagreen", "Donnée manquante": "lightgray"},
                category_orders={"zone": ["Fatigue", "Normal", "Bonne récup", "Donnée manquante"]},
            )
            fig.update_layout(legend_title_text="")
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

        daily = analysis.daily_training_load(activities, cross_training)
        full_range = pd.date_range(daily.index.min(), pd.Timestamp.now().normalize(), freq="D")
        daily = daily.reindex(full_range, fill_value=0)
        if not cross_training.empty:
            st.caption(f"ℹ️ Inclut tes **{len(cross_training)} séances de renfo/muscu** : elles ajoutent "
                       "une charge modérée (durée × intensité cardiaque ; intensité modérée supposée si "
                       "la FC n'est pas mesurée).")

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

        # --- Synthèse en clair : que faire de cette charge, là maintenant ? ---
        if last_val is not None:
            acute_now = acwr_df["charge_aigue_7j"].iloc[-1]
            chronic_now = acwr_df["charge_chronique_28j"].iloc[-1]
            trend = "au-dessus" if acute_now > chronic_now else "en dessous"
            if last_val < 0.8:
                synthese = (f"🔵 **Ta charge récente est {trend} de ton habitude (ACWR {last_val:.2f}).** "
                            "Tu es en sous-régime : c'est parfait si tu récupères ou sors de blessure, "
                            "sinon tu peux augmenter progressivement le volume sans risque.")
            elif last_val <= 1.3:
                synthese = (f"🟢 **Charge bien dosée (ACWR {last_val:.2f}), continue comme ça.** "
                            "Ta progression est dans la zone optimale : ni sous-entraînement, "
                            "ni pic de charge dangereux.")
            elif last_val <= 1.5:
                synthese = (f"🟠 **Attention, ta charge grimpe vite (ACWR {last_val:.2f}).** "
                            "Évite d'en rajouter cette semaine : maintiens ou allège légèrement "
                            "pour laisser ton corps absorber.")
            else:
                synthese = (f"🔴 **Ralentis : ta charge récente est nettement trop élevée (ACWR {last_val:.2f}).** "
                            "Le risque de blessure est réel — réduis le volume quelques jours "
                            "et privilégie des sorties faciles.")
            st.markdown(synthese)

        zone_explanations = {
            "Sous-entraînement": "Ta charge récente est plus basse que ton habitude : marge pour progresser sans risque particulier.",
            "Zone optimale": "Ta progression est saine : ta charge récente est cohérente avec ton niveau de fitness accumulé.",
            "Vigilance": "Ta charge a augmenté plus vite que ta charge habituelle : reste attentif aux signaux de fatigue.",
            "Risque élevé de blessure": "Ta charge a augmenté trop vite : c'est dans cette zone que le risque de blessure de surcharge grimpe le plus.",
            "Données insuffisantes": "Pas encore assez d'historique pour calculer ce ratio de façon fiable.",
        }
        st.info(zone_explanations.get(zone, ""))

