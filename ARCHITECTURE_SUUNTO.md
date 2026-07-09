# 🏗️ Architecture multi-marques (Suunto via Strava)

> Document de conception. But : supporter les montres **Suunto** (et par
> ricochet toutes les marques) **sans casser** le support Garmin existant.
> Décision retenue : passer par **Strava comme hub** (voir `IDEES.md`).

---

## 1. Le constat de départ

Il n'existe **aucune** librairie type `garminconnect` pour Suunto. L'API
officielle Suunto est réservée au **programme partenaire** (relation business,
OAuth2, approbation incertaine pour une appli perso). En revanche, une montre
Suunto — comme une Garmin — peut se **synchroniser automatiquement vers
Strava**, qui expose une **API OAuth2 propre et gratuite**.

**Conséquence assumée** : Strava ne contient **que les activités** (séances de
course), **pas** le sommeil / HRV / FC repos / Body Battery / stress. Un
utilisateur Suunto aura donc l'analyse de ses séances, mais **pas l'onglet
récupération**. C'est une limite de Strava, pas de notre code.

---

## 2. Principe directeur : une couche « fournisseur » (provider)

Aujourd'hui `sync.py` fouille directement dans le JSON **brut de Garmin**
(`a.get("averageHR")`, `sleep["dailySleepDTO"]`…). C'est ce couplage qu'il faut
casser. On introduit :

- un **contrat de données normalisé** (mêmes dictionnaires quelle que soit la
  marque), qui correspond déjà aux colonnes de `storage.py` ;
- une **interface `DataProvider`** que chaque source implémente ;
- le **parsing spécifique** à chaque marque déplacé **dans** son provider.

```
                      ┌──────────────────────────────┐
   sync.py  ────────► │   DataProvider (interface)    │  ← ne voit QUE du
   dashboard.py       └──────────────────────────────┘     normalisé
   (normalisé)               ▲                 ▲
                 ┌───────────┘                 └────────────┐
        ┌──────────────────┐              ┌──────────────────────┐
        │  GarminProvider  │              │    StravaProvider     │
        │  (garminconnect) │              │   (OAuth2 Strava)     │
        │  activités +     │              │   activités seules,   │
        │  wellness        │              │   wellness → None     │
        └──────────────────┘              └──────────────────────┘
```

Bénéfice clé : le jour où on branche Strava (ou une autre source), on **ne
touche ni `sync.py`, ni `storage.py`, ni `dashboard.py`**.

---

## 3. Le contrat normalisé (ce que tout provider doit renvoyer)

Interface (fichier `providers/base.py`) :

```python
class DataProvider:
    name: str                       # "garmin" | "strava"
    supports_wellness: bool         # True pour Garmin, False pour Strava

    def get_activities(self, months: int) -> list[dict]: ...
    def get_activity_laps(self, activity_id: str) -> list[dict] | None: ...
    def get_wellness(self, days: int) -> list[dict] | None: ...   # None si non supporté
```

Formes normalisées (identiques aux colonnes `storage.py` actuelles) :

- **Activité** : `activity_id, date (YYYY-MM-DD), name, distance_km, duration_s,
  avg_pace_s_per_km, avg_hr, max_hr, avg_cadence, elevation_gain, raw_json`
- **Lap** : `activity_id, lap_index, distance_km, duration_s, avg_pace_s_per_km,
  avg_hr, max_hr`
- **Wellness** : `date, resting_hr, hrv_avg, body_battery_max, body_battery_min,
  stress_avg, steps`

`sync.py` ne fait plus que : appeler `provider.get_*()` → écrire tel quel via
`storage.upsert_*()`. Plus aucun `a.get("...")` spécifique Garmin dans `sync.py`.

---

## 4. Découpage des fichiers

```
providers/
  base.py            # DataProvider (interface) + structures normalisées
  garmin.py          # GarminProvider  (déplace l'actuel garmin_client.py +
                     #                   le parsing qui est aujourd'hui dans sync.py)
  strava.py          # StravaProvider  (OAuth2 + appels API + mapping normalisé)
sync.py              # orchestration, agnostique de la marque
storage.py           # inchangé (contrat déjà aligné)
dashboard.py         # choix de la source + UX "wellness indisponible"
```

`garmin_client.py` actuel devient le cœur de `providers/garmin.py`.

---

## 5. Spécificités Strava (le vrai travail technique)

### 5.1 Enregistrement de l'app Strava (une seule fois)
- Créer une application sur https://www.strava.com/settings/api → on obtient
  `client_id` + `client_secret`.
- Les stocker dans **`st.secrets`** (Streamlit Cloud), jamais dans le code.
- `Authorization Callback Domain` = le domaine de l'appli déployée
  (ex : `xxxx.streamlit.app`).

### 5.2 Flux de connexion (OAuth2, par utilisateur)
1. L'utilisateur clique **« Se connecter avec Strava »**.
2. Redirection vers `https://www.strava.com/oauth/authorize` avec
   `scope=activity:read_all` (nécessaire pour lire aussi les activités privées).
3. Strava renvoie sur l'URL de l'appli avec un `?code=...`
   (lu via `st.query_params`).
4. Échange `code` → `access_token` + `refresh_token` (POST `/oauth/token`).

### 5.3 Jetons & rafraîchissement
- Le `access_token` **expire toutes les ~6 h** → il faut le rafraîchir avec le
  `refresh_token` (grant `refresh_token`). À gérer **automatiquement** avant
  chaque sync.
- Stockage des jetons : table `strava_tokens` dans la **base par utilisateur**
  (`get_db_path_for_user`), pour survivre au-delà de la session. ⚠️ sécurité :
  ce sont des identifiants → jamais commités, jamais loggés.

### 5.4 Endpoints utilisés
- Liste des séances : `GET /api/v3/athlete/activities?per_page=&page=`
  (paginer, filtrer `type == "Run"`).
- Détail + laps : `GET /api/v3/activities/{id}` (champs `laps`,
  `splits_metric`).

### 5.5 Mapping Strava → normalisé (pièges)
| Normalisé | Champ Strava | Piège |
|---|---|---|
| `distance_km` | `distance` (mètres) | ÷ 1000 |
| `duration_s` | `moving_time` | `elapsed_time` inclut les pauses |
| `avg_hr`/`max_hr` | `average_heartrate`/`max_heartrate` | absents si pas de ceinture/optique |
| `avg_cadence` | `average_cadence` | **RPM** en course = **×2** pour des pas/min |
| `elevation_gain` | `total_elevation_gain` | — |
| `date` | `start_date_local[:10]` | garder l'heure locale, pas UTC |

---

## 6. Points d'attention (« ne pas rencontrer de problème »)

1. **Redirect URI exacte** : l'URL de callback OAuth doit correspondre au pixel
   près à l'URL déployée (et à `localhost` en dev). Source n°1 d'échecs OAuth.
2. **Expiration 6 h des jetons** : toujours rafraîchir avant un appel, sinon
   401 aléatoires.
3. **Limites de débit Strava** : ~100 req/15 min et ~1000/jour (variable selon
   l'app). Récupérer le détail des laps séance par séance peut vite consommer →
   limiter aux N séances récentes (comme on le fait déjà pour la météo Garmin).
4. **Cadence en RPM** : ×2 sinon la cadence affichée sera divisée par deux.
5. **Config côté utilisateur** : la synchro **Suunto → Strava** doit être
   activée par la personne dans l'app Suunto (réglage unique, de son côté).
6. **Latence** : une séance apparaît dans Strava quelques minutes après la
   synchro de la montre.
7. **Conditions d'utilisation Strava (2024)** ⚠️ **à vérifier** : l'accord
   développeur Strava restreint le partage des données à des tiers / l'usage
   pour entraîner des modèles d'IA. Or l'onglet **Coach IA envoie les données à
   Gemini (Google)**. À clarifier avant d'exposer les données Strava au coach IA
   (option possible : pour les utilisateurs Strava, restreindre ce qui est
   envoyé, ou désactiver le coach IA). **Ne pas ignorer ce point.**

---

## 7. Plan de mise en œuvre (incrémental, sans casser Garmin)

- **✅ Phase 1 — Refactor sûr (FAITE).** Créé `providers/base.py` +
  `providers/garmin.py` ; le parsing Garmin est sorti de `sync.py` vers
  `GarminProvider`. `sync.py`/`dashboard.py` sont désormais agnostiques de la
  marque. Comportement Garmin inchangé (chemin `sync_data` testé de bout en
  bout hors-ligne). `garmin_client.py` (login/MFA) reste intact.
- **🚧 Phase 2 — StravaProvider (code fait, branche `strava-provider`).**
  `providers/strava.py` : OAuth (build_authorize_url / exchange_code /
  refresh), `get_activities` + `get_activity_laps`, mapping normalisé (cadence
  ×2, GPS, etc.), rafraîchissement auto du jeton. Stockage des jetons ajouté à
  `storage.py` (table `strava_tokens` + save/read). Testé hors-ligne (HTTP
  simulé). **Reste à faire côté propriétaire** : enregistrer l'app Strava pour
  obtenir `client_id`/`client_secret`. **Reste à coder** : Phase 3 (branchement
  dashboard) avant de pouvoir tester en réel.
- **🚧 Phase 3 — UX (code fait, branche `strava-provider`).** Sélecteur de
  source Garmin/Strava dans la sidebar, retour OAuth Strava géré via
  `st.query_params`, base par utilisateur dérivée de l'`athlete_id` Strava,
  bouton de synchro Strava, message « récupération indisponible » (onglet
  Récupération) et **Coach IA désactivé pour Strava** (point 6.7 : conditions
  Strava vs envoi à Gemini). Validé hors-ligne via `AppTest` (rendu Garmin réel
  + écran de choix Strava, sans erreur). **Reste** : app Strava enregistrée +
  3 secrets, puis test réel de bout en bout (Phase 4).
- **Phase 4 — Test réel + déploiement.** Un pote avec Suunto→Strava valide de
  bout en bout, puis merge sur `main` → redéploiement.

---

## 8. Décisions ouvertes (à trancher au fil de l'eau)

- Un utilisateur peut-il connecter **Garmin ET Strava** en même temps (fusion
  des sources) ? Pour l'instant : **une source par utilisateur**, plus simple.
- Faut-il garder `garminconnect` pour les utilisateurs Garmin (données plus
  riches : wellness) plutôt que de tout faire passer par Strava ? **Oui** :
  Garmin direct = wellness complet ; Strava = repli universel.
