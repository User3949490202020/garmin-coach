"""
dashboard.py
------------
Lance avec : streamlit run dashboard.py
"""

import datetime as dt
import json
import os
import uuid

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
                 "STRAVA_REDIRECT_URI", "ADMIN_EMAIL"):
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

# Sur téléphone, la barre d'onglets déborde et oblige à scroller vers la
# droite : on l'autorise à passer sur plusieurs lignes (2 lignes sur mobile,
# 1 seule sur ordinateur — ça s'adapte tout seul à la largeur de l'écran).
st.markdown("""
    <style>
    /* Streamlit >= 1.5x : la barre d'onglets est un div[role="tablist"]
       (sélecteur standard d'accessibilité, stable entre versions). */
    div[role="tablist"] {
        flex-wrap: wrap !important;
        overflow-x: visible !important;
        row-gap: 0.25rem;
    }
    div[data-testid="stTab"] {
        flex-shrink: 0 !important;
    }
    /* Anciennes versions de Streamlit (structure baseweb) — sans effet sinon. */
    div[data-baseweb="tab-list"] {
        flex-wrap: wrap !important;
        overflow-x: visible !important;
    }
    </style>
""", unsafe_allow_html=True)


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

st.title("🏃 Ton Coach Running")
st.caption("**L'entraînement au cordeau.** Tes séances, ton sommeil, ta charge — analysés "
           "finement, conseillés sur mesure. Directement depuis ta montre.")

# ----------------------------------------------------------------------
# Connexion — affichée en PLEINE PAGE tant qu'on n'est pas connecté :
# sur smartphone, la barre latérale est repliée derrière une petite flèche
# que personne ne voit. Une fois connecté, elle n'accueille plus que le
# statut et la synchronisation.
# ----------------------------------------------------------------------
if not LOCAL_MODE:
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

    # --- Aucune source connectée : écran de connexion en pleine page ---
    if "garmin_email" not in st.session_state and "strava_tokens" not in st.session_state:
        st.header("Connexion")
        if st.session_state.pop("session_expired_msg", False):
            st.warning("Ta session Garmin a expiré — ressaisis ton mot de passe pour te "
                       "reconnecter. Tes données, ton objectif et ton historique de "
                       "conversation sont conservés.")
        login_col, _ = st.columns([2, 1])
        with login_col:
            src_g, src_s = st.tabs(["⌚ Garmin", "🔶 Strava (Suunto & autres)"])
            with src_g:
                st.caption("Connecte-toi avec tes identifiants Garmin Connect. Ton mot de passe "
                           "n'est jamais enregistré sur le serveur ; ton email est associé à un "
                           "identifiant de session aléatoire pour te reconnecter automatiquement.")
                with st.form("garmin_login"):
                    email_input = st.text_input("Email Garmin Connect")
                    password_input = st.text_input("Mot de passe Garmin Connect", type="password")
                    submitted = st.form_submit_button("Se connecter", width='stretch')
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

with st.sidebar:
    if LOCAL_MODE:
        active_source = "garmin"
        garmin_email = ENV_EMAIL
        garmin_password = ENV_PASSWORD
        own_gemini_key = None
        USER_DB_PATH = None  # base par défaut, mode local
        storage.init_db(db_path=USER_DB_PATH)
    else:
        # Arrivé ici, une source est forcément connectée (sinon st.stop()
        # ci-dessus). La barre latérale n'affiche plus que statut + synchro.
        st.header("Connexion")
        active_source = st.session_state.get(
            "active_source", "strava" if "strava_tokens" in st.session_state else "garmin")
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

    # --- Trace d'usage : une ligne par visite (qui, quand, combien de temps).
    # Uniquement identifiant + horodatages, rien d'autre. Sert au panneau
    # "Fréquentation" ci-dessous, visible par l'administrateur seul.
    if "visit_id" not in st.session_state:
        st.session_state.visit_id = uuid.uuid4().hex
    _usage_ident = (garmin_email if active_source == "garmin"
                    else f"strava-{st.session_state.strava_tokens.get('athlete_id')}")
    try:
        storage.touch_usage(st.session_state.visit_id, _usage_ident or "inconnu", active_source)
    except Exception:
        pass

    _admin_email = os.getenv("ADMIN_EMAIL", "")
    if LOCAL_MODE or (_admin_email and (garmin_email or "").lower() == _admin_email.lower()):
        with st.expander("📊 Fréquentation (admin)"):
            usage = storage.read_usage()
            if usage.empty:
                st.caption("Aucune visite enregistrée pour l'instant.")
            else:
                usage["started_at"] = pd.to_datetime(usage["started_at"])
                usage["last_seen"] = pd.to_datetime(usage["last_seen"])
                usage["duree_min"] = ((usage["last_seen"] - usage["started_at"])
                                      .dt.total_seconds() / 60).round(1)
                uc1, uc2, uc3 = st.columns(3)
                uc1.metric("Utilisateurs", usage["ident"].nunique())
                uc2.metric("Visites", len(usage))
                uc3.metric("Durée moy.", f"{usage['duree_min'].mean():.0f} min")
                par_user = (usage.groupby("ident")
                            .agg(visites=("session_id", "count"),
                                 minutes=("duree_min", "sum"),
                                 derniere_visite=("last_seen", "max"))
                            .sort_values("derniere_visite", ascending=False)
                            .reset_index())
                par_user["derniere_visite"] = par_user["derniere_visite"].dt.strftime("%d/%m %H:%M")
                st.dataframe(par_user, hide_index=True, width='stretch')
                st.bar_chart(usage.groupby(usage["started_at"].dt.date).size(),
                             height=160)
            st.caption("⚠️ Ces compteurs repartent de zéro à chaque mise à jour de "
                       "l'appli (stockage éphémère de l'hébergement gratuit).")

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
    # Détecte les échauffements (footing doux juste avant une séance intense
    # le même jour) : ils sont fusionnés avec leur séance dans les décomptes.
    activities = analysis.tag_warmups(activities)
if not wellness.empty:
    wellness["date"] = pd.to_datetime(wellness["date"])

# FC max de l'athlète : valeur corrigée à la main si renseignée (onglet VMA),
# sinon le plus haut pic observé sur ses 6 derniers mois. Utilisée partout où
# on classe l'intensité des séances.
_hr_saved = storage.read_manual_note("hr_max", db_path=USER_DB_PATH)
HR_MAX_DETECTED = analysis.observed_hr_max(activities)
HR_MAX = int(_hr_saved[0]) if _hr_saved else HR_MAX_DETECTED
# FC repos actuelle (moyenne 28 j) : avec la FCmax, elle définit les zones
# Karvonen (Z1-Z5 en % de réserve cardiaque). Sans données (Strava), défaut 55.
HR_REST = analysis.current_rest_hr(wellness)

tab_coach, tab_strava, tab_seances, tab_recup, tab_charge, tab_vma, tab_sante = st.tabs(
    ["💬 Le Coach", "📊 Momentum", "🏃 Séances", "😴 Récupération",
     "📈 Charge & Risque", "🚀 VMA", "🩺 Santé"]
)

# ----------------------------------------------------------------------
# Coach IA (en premier, pour un accès direct sans avoir à scroller)
# ----------------------------------------------------------------------
with tab_coach:
    st.subheader("💬 Discute avec ton coach IA")
    st.caption("Pose des questions sur tes séances, ta récupération, ta progression : l'agent répond "
               "en s'appuyant sur tes vraies données, pas sur des généralités.")

    # --- Objectif persistant : le coach le connaît sans que tu le répètes ---
    # Conservé 45 jours (1,5 mois) : au-delà il reste actif mais l'appli — et
    # le coach — suggèrent de le rafraîchir. Modifiable à tout moment.
    OBJECTIF_VALIDITE_JOURS = 45
    saved_obj = storage.read_text_note("objectif", db_path=USER_DB_PATH)
    obj_age_days = None
    if saved_obj:
        obj_age_days = (pd.Timestamp.now().normalize()
                        - pd.to_datetime(saved_obj[1])).days
    with st.expander("🎯 Mon objectif du moment"
                     + (f" — défini le {pd.to_datetime(saved_obj[1]).strftime('%d/%m/%Y')}" if saved_obj else ""),
                     expanded=not saved_obj):
        st.caption("Écris ton objectif ici une fois : le coach en tiendra compte dans **toutes** ses "
                   "réponses, sans que tu aies à le répéter à chaque message.")
        if saved_obj and obj_age_days is not None:
            fin_validite = (pd.to_datetime(saved_obj[1])
                            + pd.Timedelta(days=OBJECTIF_VALIDITE_JOURS))
            if obj_age_days > OBJECTIF_VALIDITE_JOURS:
                st.warning(f"⏳ Ton objectif date de plus de 1,5 mois ({obj_age_days} jours). "
                           "Toujours d'actualité ? Mets-le à jour — ou réenregistre-le tel quel "
                           "pour repartir pour 1,5 mois.")
            else:
                st.caption(f"✅ Objectif actif — conservé jusqu'au "
                           f"{fin_validite.strftime('%d/%m/%Y')} (et modifiable à tout moment).")
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
            # Reprend la conversation des 48 dernières heures (persistée en
            # base par utilisateur) : une coupure de session ne l'efface plus.
            # Le bouton "Effacer la conversation" reste le seul moyen de purger.
            st.session_state.chat_display_history = storage.read_chat_history(
                hours=48, db_path=USER_DB_PATH)

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

        st.subheader("Ta semaine, jour par jour")
        st.caption("Du lundi au dimanche : les kilomètres de chaque séance, colorés selon ta zone "
                   "cardiaque Z1-Z5 (Karvonen — les mêmes couleurs que « Tes 5 zones », à régler "
                   "dans l'onglet VMA).")
        _today = pd.Timestamp.now().normalize()
        _week_start = _today - pd.Timedelta(days=_today.weekday())
        week_days = [_week_start + pd.Timedelta(days=i) for i in range(7)]
        day_labels = [f"{['Lun','Mar','Mer','Jeu','Ven','Sam','Dim'][i]} {d.strftime('%d/%m')}"
                      for i, d in enumerate(week_days)]
        week_acts = activities[(activities["date"] >= _week_start)
                               & (activities["date"] < _week_start + pd.Timedelta(days=7))]
        zone_colors = {z[0]: z[4] for z in analysis.HR_ZONES_DEF}
        zone_colors["sans FC"] = "lightgray"
        rows_week = []
        for _, a in week_acts.iterrows():
            z = analysis.session_zone(a["avg_hr"], HR_MAX, HR_REST) or "sans FC"
            rows_week.append({"Jour": day_labels[int(a["date"].weekday())],
                              "km": a["distance_km"], "Zone": z,
                              "seance": a["name"]})
        if not rows_week:
            st.caption("Pas encore de séance cette semaine — les barres apparaîtront ici au fil "
                       "de tes sorties.")
        else:
            # Jours sans séance : barre à zéro pour que les 7 jours s'affichent
            _jours_courus = {r["Jour"] for r in rows_week}
            for lbl in day_labels:
                if lbl not in _jours_courus:
                    rows_week.append({"Jour": lbl, "km": 0, "Zone": "Z1", "seance": ""})
            # Zones absentes cette semaine : ligne fantôme (0 km) pour que la
            # légende affiche TOUJOURS les 5 zones, de Z1 à Z5.
            _zones_presentes = {r["Zone"] for r in rows_week}
            for _z in ["Z1", "Z2", "Z3", "Z4", "Z5"]:
                if _z not in _zones_presentes:
                    rows_week.append({"Jour": day_labels[0], "km": 0, "Zone": _z, "seance": ""})
            week_df = pd.DataFrame(rows_week)
            fig_wk = px.bar(
                week_df, x="Jour", y="km", color="Zone",
                color_discrete_map=zone_colors,
                category_orders={"Jour": day_labels, "Zone": ["Z1", "Z2", "Z3", "Z4", "Z5", "sans FC"]},
                hover_data={"seance": True, "km": ":.1f"},
            )
            fig_wk.update_xaxes(categoryorder="array", categoryarray=day_labels)
            fig_wk.update_layout(barmode="stack", yaxis_title="Kilomètres",
                                 xaxis_title="", legend_title_text="")
            st.plotly_chart(mobile_friendly(fig_wk), width='stretch', config=PLOTLY_CONFIG)

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
                # La table contient désormais TOUS les sports croisés (vélo,
                # natation, crossfit...) : ce graphique ne suit que le duo
                # renfo/étirements — les autres comptent dans la charge.
                sport = ct["sport"].astype(str)
                ct["categorie"] = None
                ct.loc[sport.str.contains("yoga|stretch", case=False, na=False),
                       "categorie"] = "Étirements"
                ct.loc[sport.str.contains("strength|crossfit|hiit|training", case=False, na=False)
                       & ct["categorie"].isna(), "categorie"] = "Musculation"
                ct = ct.dropna(subset=["categorie"])
            if not ct.empty:
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
        if "is_warmup" in display_df.columns:
            display_df["rôle"] = display_df["is_warmup"].map(
                {True: "🔥 échauffement", False: ""})
        # Zone cardiaque Karvonen (Z1-Z5), en plus des catégories facile/moyen/dur
        display_df["zone"] = display_df["avg_hr"].apply(
            lambda h: analysis.session_zone(h, HR_MAX, HR_REST) or "—")
        if "temp_c" in display_df.columns:
            display_df["météo"] = display_df.apply(
                lambda r: (f"{r['temp_c']:.0f}°C (ressenti {r['feels_like_c']:.0f}°C)"
                          if pd.notna(r.get("temp_c")) and pd.notna(r.get("feels_like_c"))
                          else (f"{r['temp_c']:.0f}°C" if pd.notna(r.get("temp_c")) else "N/A")),
                axis=1,
            )
        else:
            display_df["météo"] = "N/A"

        cols_to_show = ["date", "jour", "name", "rôle", "zone", "distance_km", "allure_min_km",
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

        # --- 3. Dérive cardiaque ---
        st.subheader("💓 Dérive cardiaque (sorties longues)")
        st.caption("Sur une sortie longue à allure stable, si ta FC grimpe entre la 1ʳᵉ et la 2ᵉ "
                   "moitié, c'est le signe que l'effort te coûte : **< 5 % = excellente endurance de "
                   "base**, au-delà = fatigue, chaleur ou déshydratation. Seules les sorties ≥ 8 km "
                   "à allure régulière sont analysées.")
        drift = analysis.cardiac_drift(activities, laps)
        if drift.empty:
            st.caption("Aucune sortie longue analysable pour l'instant (il faut ≥ 8 km, une allure "
                       "stable et le détail des tours synchronisé).")
        else:
            fig_dr = go.Figure()
            drift_colors = ["seagreen" if d < 5 else ("orange" if d < 8 else "crimson")
                            for d in drift["drift_pct"]]
            fig_dr.add_trace(go.Bar(x=drift["date"], y=drift["drift_pct"], marker_color=drift_colors,
                                     hovertemplate="%{x|%d/%m}<br>Dérive : %{y:.1f} %<extra></extra>"))
            fig_dr.add_hline(y=5, line_dash="dash", line_color="gray",
                             annotation_text="seuil 5 %")
            fig_dr.update_layout(yaxis_title="Dérive FC (%)", xaxis_title="")
            st.plotly_chart(mobile_friendly(fig_dr), width='stretch', config=PLOTLY_CONFIG)
            if len(drift) >= 3 and (drift["drift_pct"].tail(3) < 5).mean() >= 0.67:
                st.success("🟢 Tes dernières sorties longues montrent une FC stable : ton endurance "
                           "fondamentale est solide.")


        # --- 5. Cadence ---
        st.subheader("👣 Ta cadence dans le temps")
        st.caption("La cadence (pas/minute) est un marqueur de technique : une dérive vers le bas "
                   "accompagne souvent la fatigue ou une foulée qui se dégrade. On ne vise pas "
                   "180 à tout prix — on surveille **ta** tendance.")
        cad = analysis.cadence_trend(activities, months=12)
        if cad.empty:
            st.caption("Pas de données de cadence sur la période.")
        else:
            fig_cad = go.Figure()
            fig_cad.add_trace(go.Scatter(x=cad["date"], y=cad["avg_cadence"], mode="markers",
                                          name="Séances", marker=dict(color="lightgray", size=7)))
            fig_cad.add_trace(go.Scatter(x=cad["date"], y=cad["cadence_lissee"], mode="lines",
                                          name="Tendance (10 séances)",
                                          line=dict(color="#B08D57", width=3)))
            fig_cad.update_layout(yaxis_title="Cadence (pas/min)", xaxis_title="")
            st.plotly_chart(mobile_friendly(fig_cad), width='stretch', config=PLOTLY_CONFIG)

            # --- Fourchette de cadence personnalisée selon le gabarit ---
            st.markdown("**🎯 Ta fourchette de cadence indicative**")
            saved_taille = storage.read_manual_note("taille_cm", db_path=USER_DB_PATH)
            saved_poids = storage.read_manual_note("poids_kg", db_path=USER_DB_PATH)
            gc1, gc2 = st.columns(2)
            taille_in = gc1.number_input("Ta taille (cm)", min_value=140, max_value=210,
                                         value=int(saved_taille[0]) if saved_taille else None,
                                         key="taille_cm_input")
            poids_in = gc2.number_input("Ton poids (kg)", min_value=40, max_value=150,
                                        value=int(saved_poids[0]) if saved_poids else None,
                                        key="poids_kg_input")
            if taille_in and (not saved_taille or int(saved_taille[0]) != taille_in):
                storage.save_manual_note("taille_cm", float(taille_in), db_path=USER_DB_PATH)
            if poids_in and (not saved_poids or int(saved_poids[0]) != poids_in):
                storage.save_manual_note("poids_kg", float(poids_in), db_path=USER_DB_PATH)

            if not taille_in or not poids_in:
                st.caption("Renseigne ta taille et ton poids (mémorisés) pour obtenir une "
                           "fourchette adaptée à ton gabarit.")
            else:
                # Heuristique de terrain : ~180 pas/min pour 1,70 m, ajustée de
                # ±0,5 pas/min par cm (un grand gabarit a une foulée plus ample,
                # donc une cadence naturellement plus basse).
                centre = 180 - (taille_in - 170) * 0.5
                lo, hi = centre - 4, centre + 4
                imc = poids_in / (taille_in / 100) ** 2
                reco = (f"Pour ton gabarit ({taille_in} cm, {poids_in} kg) : vise **{lo:.0f}-"
                        f"{hi:.0f} pas/min**.")
                if imc >= 25:
                    reco += (" Privilégie plutôt le **haut de la fourchette** : à gabarit plus "
                             "costaud, une cadence plus élevée raccourcit la foulée et réduit "
                             "l'impact à chaque appui (moins de contrainte pour les articulations).")
                cad_now = cad["cadence_lissee"].dropna()
                if not cad_now.empty:
                    actuelle = cad_now.iloc[-1]
                    if actuelle < lo - 2:
                        reco += (f" Ta tendance actuelle (**{actuelle:.0f}**) est en dessous : si tu "
                                 "veux la remonter, fais-le très progressivement (+2-3 pas/min à la "
                                 "fois, sur plusieurs semaines) — jamais d'un coup.")
                    elif actuelle > hi + 2:
                        reco += (f" Ta tendance actuelle (**{actuelle:.0f}**) est au-dessus — ce "
                                 "n'est pas un problème en soi si tu ne ressens ni gêne ni fatigue "
                                 "inhabituelle.")
                    else:
                        reco += f" Ta tendance actuelle (**{actuelle:.0f}**) est pile dedans. 👌"
                st.info(reco)
                st.caption("⚠️ **À prendre avec des pincettes** : c'est une heuristique de terrain "
                           "basée sur ton seul gabarit. Ta biomécanique individuelle (longueur de "
                           "jambes, historique, terrain) prime toujours — une cadence hors "
                           "fourchette qui ne cause ni blessure ni inconfort n'a pas besoin "
                           "d'être « corrigée ».")



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

        st.divider()

        # --- 1. Équilibre 80/20 ---
        st.subheader("⚖️ L'équilibre 80/20")
        st.caption("La règle d'or de l'entraînement d'endurance : **~80 % du temps en allure facile**, "
                   "~20 % en intensité. L'erreur classique : courir trop souvent « moyennement dur », "
                   "ce qui fatigue sans faire progresser. Calculé sur tes 4 dernières semaines, avec "
                   "ta FCmax personnalisée : tes VMA comptent bien comme du dur (pics de FC), tes "
                   "footings comme du facile.")
        pol = analysis.polarization(activities, hr_max=HR_MAX)
        if not pol:
            st.caption("Pas encore assez de séances avec FC sur les 4 dernières semaines.")
        else:
            pcols = st.columns(3)
            pcols[0].metric("🟢 Facile", f"{pol['facile']:.0f} %")
            pcols[1].metric("🟠🔴 Intensité", f"{pol['intense']:.0f} %")
            pcols[2].metric("Séances analysées", pol["nb_seances"])
            fig_pol = go.Figure()
            for label, val, color in [("Facile", pol["facile"], "seagreen"),
                                      ("Moyen", pol["moyen"], "orange"),
                                      ("Dur", pol["dur"], "crimson")]:
                fig_pol.add_trace(go.Bar(y=["Répartition"], x=[val], name=label,
                                          orientation="h", marker_color=color))
            fig_pol.add_vline(x=80, line_dash="dash", line_color="#22303F",
                              annotation_text="cible 80 %")
            fig_pol.update_layout(barmode="stack", height=140, xaxis=dict(range=[0, 100], title="%"),
                                  margin=dict(t=10, b=10))
            st.plotly_chart(mobile_friendly(fig_pol), width='stretch', config=PLOTLY_CONFIG)
            if pol["intense"] > 35:
                st.error(f"🔴 {pol['intense']:.0f} % d'intensité : beaucoup trop. Reprends du footing "
                         "facile — c'est lui qui construit le moteur.")
            elif pol["intense"] > 25:
                st.warning(f"🟠 {pol['intense']:.0f} % d'intensité : un peu élevé. Vérifie que tes "
                           "footings sont vraiment faciles (conversation possible).")
            else:
                st.success(f"🟢 {pol['intense']:.0f} % d'intensité : équilibre idéal, continue.")


        # --- 4. Monotonie de charge ---
        st.subheader("🎢 Monotonie de charge (Foster)")
        st.caption("Un entraînement efficace **alterne** jours durs et jours faciles. Cet indice "
                   "mesure l'uniformité de ta charge sur 7 jours : **au-dessus de 2, c'est trop "
                   "monotone** — un facteur de risque de blessure indépendant du volume, même si "
                   "ton ACWR est bon.")
        mono = analysis.monotony(activities, cross_training)
        if mono.empty:
            st.caption("Pas encore assez d'historique quotidien.")
        else:
            mono_recent = mono.tail(90)
            fig_mono = px.line(mono_recent, y="monotonie")
            fig_mono.update_traces(line=dict(width=3, color="#22303F"))
            fig_mono.add_hrect(y0=0, y1=2, fillcolor="green", opacity=0.08, line_width=0)
            fig_mono.add_hline(y=2, line_dash="dash", line_color="crimson",
                               annotation_text="seuil de vigilance")
            fig_mono.update_layout(yaxis_title="Monotonie (7 j)", xaxis_title="")
            st.plotly_chart(mobile_friendly(fig_mono), width='stretch', config=PLOTLY_CONFIG)
            m_now = mono["monotonie"].iloc[-1]
            if m_now > 2:
                st.warning(f"🟠 Monotonie actuelle : {m_now:.1f}. Casse la routine : un vrai jour "
                           "dur, un vrai jour off.")
            else:
                st.success(f"🟢 Monotonie actuelle : {m_now:.1f} — bonne alternance dur/facile.")



# ----------------------------------------------------------------------
# VMA — vitesse maximale aérobie : estimation + analyse des séances
# ----------------------------------------------------------------------
with tab_vma:
    st.subheader("🚀 Ta VMA")

    # --- 0. FC max de référence (détectée, corrigeable) ---
    st.markdown("**❤️ Ta FC max de référence**")
    st.caption(f"Détectée automatiquement : **{HR_MAX_DETECTED} bpm** (ton plus haut pic des "
               "6 derniers mois). C'est elle qui calibre le classement facile/moyen/dur de "
               "toutes tes séances — corrige-la si tu connais ta vraie FCmax.")
    hr_input = st.number_input("FC max (bpm)", min_value=150, max_value=220,
                               value=HR_MAX, step=1, key="hr_max_input")
    if hr_input != HR_MAX:
        storage.save_manual_note("hr_max", float(hr_input), db_path=USER_DB_PATH)
        st.rerun()

    # --- Tes 5 zones cardiaques en %FCR (Karvonen) ---
    st.markdown("**🎯 Tes 5 zones cardiaques (Karvonen, % de réserve)**")
    st.caption(f"Calculées sur TA réserve cardiaque : FCmax {HR_MAX} − FC repos {HR_REST} bpm "
               "(moyenne 28 j). Plus juste que le simple % de FCmax : c'est la référence des "
               "coureurs confirmés. Chaque séance de ton historique est taguée Z1-Z5.")
    zones_df = analysis.hr_zones(HR_MAX, HR_REST)
    fig_z = go.Figure()
    for _, z in zones_df.iterrows():
        fig_z.add_trace(go.Bar(
            y=["Zones"], x=[z["bpm_max"] - z["bpm_min"]], base=[z["bpm_min"]],
            orientation="h", name=f"{z['zone']} {z['label']}",
            marker_color=z["color"],
            hovertemplate=f"{z['zone']} — {z['label']}<br>{z['bpm_min']}–{z['bpm_max']} bpm<extra></extra>",
            text=f"{z['zone']}<br>{z['bpm_min']}-{z['bpm_max']}",
            textposition="inside",
        ))
    fig_z.update_layout(barmode="overlay", height=170, showlegend=False,
                        xaxis=dict(title="bpm", range=[HR_REST, HR_MAX + 5]),
                        margin=dict(t=10, b=10))
    st.plotly_chart(mobile_friendly(fig_z), width='stretch', config=PLOTLY_CONFIG)
    st.divider()

    if activities.empty:
        st.info("Synchronise d'abord tes séances.")
    else:
        # --- vVMA estimée + évolution ---
        st.markdown("**Ta vVMA estimée**")
        st.caption("Estimation d'entraînement dérivée de tes meilleures perfs (séances continues "
                   "ET fractions de VMA), **lissée par une contrainte physiologique** : la VMA "
                   "évolue de quelques dixièmes de km/h par mois, jamais par bonds — les trous de "
                   "données sont corrigés en conséquence. Un test de terrain (demi-Cooper : 6 min "
                   "à fond) reste la référence.")
        vma_curve = analysis.vma_estimate_curve(activities, laps, hr_max=HR_MAX, months=12)
        if vma_curve.empty:
            st.caption("Pas encore assez de séances ≥ 3 km pour estimer ta vVMA.")
        else:
            vma_now = vma_curve["vma_kmh"].iloc[-1]
            vcols = st.columns(3)
            vcols[0].metric("vVMA estimée", f"{vma_now:.1f} km/h")
            pace_vma = 3600 / vma_now
            vcols[1].metric("Allure VMA", f"{int(pace_vma // 60)}:{int(pace_vma % 60):02d}/km")
            pace_105 = 3600 / (vma_now * 0.95)
            vcols[2].metric("Allure fractions (95 %)",
                            f"{int(pace_105 // 60)}:{int(pace_105 % 60):02d}/km")
            if len(vma_curve) >= 2:
                fig_vc = px.line(vma_curve, x="date", y="vma_kmh", markers=True)
                fig_vc.update_traces(line=dict(width=3, color="#C00000"))
                fig_vc.update_layout(yaxis_title="vVMA estimée (km/h)", xaxis_title="")
                st.plotly_chart(mobile_friendly(fig_vc), width='stretch', config=PLOTLY_CONFIG)
                st.caption("ℹ️ L'estimation prend le meilleur des deux signaux : ta meilleure "
                           "séance continue **et** la vitesse de tes fractions de VMA. Note : le "
                           "détail des tours n'existe que sur les ~30 dernières séances, donc les "
                           "points anciens s'appuient surtout sur les séances continues.")

        st.divider()

        # --- Analyse des séances de fractionné ---
        st.markdown("**Tes séances de VMA passées au crible**")
        st.caption("Pour chaque séance dure avec le détail des tours : les fractions rapides "
                   "isolées, leur allure moyenne, la meilleure, et la régularité (écart entre "
                   "fractions — un bon fractionné est RÉGULIER, pas explosif puis à l'agonie).")
        vs = analysis.vma_sessions(activities, laps, hr_max=HR_MAX)
        if vs.empty:
            st.caption("Aucune séance de fractionné analysable pour l'instant : il faut des séances "
                       "dures avec le détail des tours (synchronisé sur les 30 dernières séances — "
                       "resynchronise pour élargir l'historique).")
        else:
            def _p(s):
                return f"{int(s // 60)}:{int(s % 60):02d}"
            table = pd.DataFrame({
                "Date": vs["date"].dt.strftime("%d/%m"),
                "Séance": vs["name"],
                "Fractions": vs["nb_fractions"].astype(int),
                "Dist. moy.": (vs["dist_fraction_km"] * 1000).round(0).astype(int).astype(str) + " m",
                "Allure moy.": vs["allure_moy_s"].apply(_p) + "/km",
                "Meilleure": vs["meilleure_s"].apply(_p) + "/km",
                "Régularité (±s)": vs["regularite_s"].round(1),
            })
            st.dataframe(table, hide_index=True, width='stretch')

            # --- Évolution séance par séance : plus haut = plus rapide ---
            st.markdown("**Évolution de tes séances de VMA**")
            vs_chron = vs.sort_values("date")
            speed_kmh = 3600 / vs_chron["allure_moy_s"]
            # La moyenne est affichée ICI (hors graphique) : sur le graphique,
            # l'étiquette se perdait sur les barres.
            st.caption(f"Une barre par séance, en km/h (plus haut = plus rapide). Au-dessus de "
                       f"chaque barre : température · nombre de répétitions · récupération. "
                       f"La ligne pointillée = ta moyenne : "
                       f"**{speed_kmh.mean():.1f} km/h** ({_p(3600 / speed_kmh.mean())}/km).")
            bar_labels = []
            for (_, r) in vs_chron.iterrows():
                parts = []
                if pd.notna(r["temp_c"]):
                    parts.append(f"{r['temp_c']:.0f}°C")
                parts.append(f"{int(r['nb_fractions'])}Rep")
                if pd.notna(r.get("recup_s")) and r.get("recup_s"):
                    parts.append(f"{int(round(r['recup_s']))}s")
                bar_labels.append("<br>".join([parts[0], " · ".join(parts[1:])])
                                  if len(parts) > 1 else parts[0])
            fig_ev = go.Figure()
            fig_ev.add_trace(go.Bar(
                x=vs_chron["date"].dt.strftime("%d/%m"), y=speed_kmh,
                marker_color="#E06666",
                text=bar_labels, textposition="outside",
                customdata=[_p(p) + "/km" for p in vs_chron["allure_moy_s"]],
                hovertemplate="%{x} : %{y:.1f} km/h (%{customdata})<extra></extra>",
            ))
            fig_ev.add_hline(y=speed_kmh.mean(), line_dash="dash", line_color="#22303F")
            ymin = max(speed_kmh.min() - 1.5, 0)
            fig_ev.update_layout(yaxis_title="Vitesse fractions (km/h)", xaxis_title="",
                                 yaxis=dict(range=[ymin, speed_kmh.max() + 2.6]))
            st.plotly_chart(mobile_friendly(fig_ev), width='stretch', config=PLOTLY_CONFIG)

            choix = st.selectbox(
                "Voir le détail d'une séance (fraction par fraction)",
                vs.index,
                format_func=lambda i: f"{vs.loc[i, 'date'].strftime('%d/%m')} — {vs.loc[i, 'name']}",
            )
            sel = vs.loc[choix]
            paces = sel["laps_paces"]
            frac_speed = [3600 / p for p in paces]
            mean_speed = 3600 / sel["allure_moy_s"]
            # Moyenne affichée hors graphique (l'étiquette se lisait mal sur les barres)
            st.caption(f"Moyenne de la séance : **{mean_speed:.1f} km/h** "
                       f"({_p(sel['allure_moy_s'])}/km) — la ligne pointillée sur le graphique.")
            fig_fr = go.Figure()
            fig_fr.add_trace(go.Bar(
                x=[f"F{i+1}" for i in range(len(paces))],
                y=frac_speed, marker_color="#E06666",
                hovertemplate="%{x} : %{y:.1f} km/h (%{customdata})<extra></extra>",
                customdata=[_p(p) + "/km" for p in paces],
            ))
            fig_fr.add_hline(y=mean_speed, line_dash="dash", line_color="#22303F")
            fmin = max(min(frac_speed) - 1.5, 0)
            fig_fr.update_layout(yaxis_title="Vitesse (km/h) — plus haut = plus rapide",
                                 xaxis_title=f"Fractions du {sel['date'].strftime('%d/%m')}",
                                 yaxis=dict(range=[fmin, max(frac_speed) + 1.5]))
            st.plotly_chart(mobile_friendly(fig_fr), width='stretch', config=PLOTLY_CONFIG)
            if pd.notna(sel["regularite_s"]) and sel["regularite_s"] <= 8:
                st.success(f"🟢 Régularité excellente (± {sel['regularite_s']:.0f} s) : "
                           "allure maîtrisée du début à la fin.")
            elif pd.notna(sel["regularite_s"]) and sel["regularite_s"] > 15:
                st.warning(f"🟠 Fractions irrégulières (± {sel['regularite_s']:.0f} s) : pars "
                           "moins vite sur les premières, l'objectif est de tenir la MÊME allure.")

            # --- Tes habitudes sur les dernières séances de VMA ---
            habits = analysis.vma_fraction_habits(vs)
            if habits:
                constats = []
                for pos, d in sorted(habits["fast"]):
                    constats.append(f"la **F{pos}** part trop vite ({abs(d):.0f} s plus rapide "
                                    "que ta moyenne de séance)")
                for pos, d in sorted(habits["slow"]):
                    constats.append(f"la **F{pos}** est souvent trop lente (+{d:.0f} s)")
                if habits["last_fast"]:
                    constats.append(f"la **dernière** est trop rapide "
                                    f"({abs(habits['last_delta']):.0f} s de mieux — signe qu'il "
                                    "te restait trop de jus)")
                elif habits["last_slow"]:
                    constats.append(f"la **dernière** s'écroule (+{habits['last_delta']:.0f} s)")
                if constats:
                    st.info(f"🔍 **Ton habitude sur les {habits['nb_sessions']} dernières VMA** : "
                            + ", ".join(constats) + ". La prochaine fois, vise la même allure de la "
                            "première à la dernière fraction : retiens-toi quand ça part vite, et "
                            "garde de la lucidité sur celles que tu laisses filer — c'est "
                            "l'homogénéité qui fait progresser, pas la meilleure fraction.")
                else:
                    st.success(f"🟢 **Sur tes {habits['nb_sessions']} dernières VMA**, aucune "
                               "mauvaise habitude récurrente : tes fractions sont homogènes d'une "
                               "séance à l'autre. Exactement ce qu'on veut.")

# ----------------------------------------------------------------------
# Santé — sommeil par phases, stress, conseils personnalisés
# ----------------------------------------------------------------------
with tab_sante:
    st.subheader("🩺 Ta santé de sportif")

    # --- Conseils personnalisés (croisement données ↔ entraînement) ---
    st.markdown("**💡 Tes conseils du moment**")
    st.caption("Générés en croisant tes marqueurs (HRV, sommeil, stress) avec ton entraînement "
               "réel — course ET sports croisés (crossfit, vélo, renfo...).")
    tips = analysis.health_insights(wellness, sleep, activities, cross_training, hr_max=HR_MAX)
    for t in tips:
        {"ok": st.success, "info": st.info, "alerte": st.warning}[t["niveau"]](t["texte"])

    if os.getenv("GEMINI_API_KEY"):
        if st.button("🧠 Analyse approfondie par le coach IA (HRV, sommeil, récupération)"):
            with st.spinner("Le coach analyse tes 60 derniers jours..."):
                try:
                    ctx_sante = coach_agent.build_context_summary(
                        activities, wellness, pd.DataFrame(), {}, None, None, laps,
                        sleep=sleep, cross_training=cross_training,
                    )
                    st.session_state.sante_advice = coach_agent.one_shot_advice(
                        ctx_sante,
                        focus="L'athlète te demande une analyse personnalisée de sa récupération : "
                              "comment améliorer sa HRV, son sommeil et son état de forme, en "
                              "identifiant ce qui, dans SON entraînement récent et SON hygiène de "
                              "vie, explique les tendances observées.",
                        api_key=own_gemini_key,
                    )
                except Exception as e:
                    st.session_state.sante_advice = f"Erreur pendant l'analyse : {e}"
        if st.session_state.get("sante_advice"):
            st.markdown(st.session_state.sante_advice)
    st.divider()

    # --- Sommeil par phases ---
    st.markdown("**🛌 Ton sommeil par phases (30 dernières nuits)**")
    st.caption("Le sommeil **profond** répare le corps (muscles, hormones), le **paradoxal (REM)** "
               "répare la tête (mémoire, système nerveux — clé pour la HRV). Repères d'une bonne "
               "nuit : ~15-20 % de profond, ~20-25 % de REM.")
    phases = analysis.sleep_phases(sleep)
    if phases.empty:
        st.caption("Pas encore de données de sommeil détaillées (synchronise, ou vérifie que ta "
                   "montre enregistre le sommeil).")
    else:
        fig_ph = go.Figure()
        for label, color in [("Profond", "#0B3866"), ("Paradoxal (REM)", "#3D85C6"),
                             ("Léger", "#A8CCEC"), ("Éveillé", "#D9D9D9")]:
            fig_ph.add_trace(go.Bar(x=phases["date"], y=phases[label], name=label,
                                     marker_color=color))
        fig_ph.update_layout(barmode="stack", yaxis_title="Heures", xaxis_title="")
        st.plotly_chart(mobile_friendly(fig_ph), width='stretch', config=PLOTLY_CONFIG)
        tot = phases[["Profond", "Léger", "Paradoxal (REM)", "Éveillé"]].sum().sum()
        if tot > 0:
            pd_pct = phases["Profond"].sum() / tot * 100
            rem_pct = phases["Paradoxal (REM)"].sum() / tot * 100
            pcol1, pcol2, pcol3 = st.columns(3)
            pcol1.metric("Nuit moyenne", f"{phases['total_h'].mean():.1f} h")
            pcol2.metric("Profond", f"{pd_pct:.0f} %",
                         help="Repère : 15-20 % d'une nuit")
            pcol3.metric("Paradoxal (REM)", f"{rem_pct:.0f} %",
                         help="Repère : 20-25 % d'une nuit")
    st.divider()

    # --- Stress ---
    st.markdown("**😮‍💨 Ton stress (60 derniers jours)**")
    st.caption("Le stress Garmin (0-100) agrège la variabilité cardiaque tout au long de la "
               "journée : c'est le stress GLOBAL (boulot, vie, entraînement). Ce qui compte : "
               "les périodes durablement au-dessus de TA moyenne.")
    if wellness.empty or "stress_avg" not in wellness.columns or wellness["stress_avg"].notna().sum() < 5:
        st.caption("Pas encore assez de données de stress.")
    else:
        stress_df = wellness.dropna(subset=["stress_avg"]).sort_values("date").copy()
        stress_df = stress_df[stress_df["date"] >= pd.Timestamp.now() - pd.Timedelta(days=60)]
        stress_df["stress_lisse"] = stress_df["stress_avg"].rolling(7, min_periods=3).mean()
        fig_st = go.Figure()
        fig_st.add_trace(go.Bar(x=stress_df["date"], y=stress_df["stress_avg"],
                                 name="Stress du jour", marker_color="#A8CCEC", opacity=0.6))
        fig_st.add_trace(go.Scatter(x=stress_df["date"], y=stress_df["stress_lisse"],
                                     name="Tendance 7 j", line=dict(color="#22303F", width=3)))
        fig_st.add_hline(y=stress_df["stress_avg"].mean(), line_dash="dash", line_color="gray")
        fig_st.update_layout(yaxis_title="Stress (0-100)", xaxis_title="")
        st.plotly_chart(mobile_friendly(fig_st), width='stretch', config=PLOTLY_CONFIG)
        st.caption(f"Ta moyenne sur la période : **{stress_df['stress_avg'].mean():.0f}** "
                   "(la ligne pointillée).")

    st.caption("⚠️ Ces analyses sont des repères d'entraînement, pas un avis médical : si un "
               "symptôme persiste (fatigue anormale, sommeil durablement dégradé), parles-en à "
               "un professionnel de santé.")
