# 🏃 Coach Running Garmin

Un dashboard perso qui se connecte à ton compte Garmin Connect et analyse
tout ce qui compte pour progresser : allure, FC, cadence, sommeil, HRV,
FC repos, Body Battery, charge d'entraînement et risque de blessure.

Tout tourne **en local sur ton ordinateur**. Tes identifiants Garmin ne sont
jamais envoyés ailleurs qu'à Garmin lui-même.

⚠️ **Point important à savoir** : ce projet utilise `garminconnect`, une
librairie **non-officielle** qui imite l'app mobile Garmin Connect (il n'existe
pas d'API publique gratuite pour un usage personnel). Ça fonctionne très bien
au quotidien, mais Garmin peut occasionnellement changer son système et casser
temporairement la connexion — dans ce cas il suffit généralement de mettre à
jour la librairie (`pip install --upgrade garminconnect`).

---

## Étape 1 — Installer Python

Vérifie que Python 3.10+ est installé :
```bash
python3 --version
```
Si ce n'est pas le cas, installe-le depuis [python.org](https://www.python.org/downloads/).

## Étape 2 — Récupérer le projet

Place tous les fichiers de ce projet dans un dossier, par exemple `garmin_coach/`,
puis ouvre un terminal dans ce dossier.

## Étape 3 — Créer un environnement virtuel

Ça évite d'installer les librairies dans ton Python système :
```bash
python3 -m venv venv
source venv/bin/activate        # Sur Mac/Linux
venv\Scripts\activate           # Sur Windows
```

## Étape 4 — Installer les dépendances

```bash
pip install -r requirements.txt
```

## Étape 5 — Configurer tes identifiants Garmin

```bash
cp .env.example .env
```
Puis ouvre `.env` et remplis ton email et mot de passe Garmin Connect (les mêmes
que dans l'app mobile).

## Étape 6 — Première synchronisation

```bash
python sync.py --days 30
```
Ça télécharge tes 30 derniers jours de séances, sommeil, FC repos, HRV et Body
Battery dans un fichier local `garmin_coach.db`. La première fois ça peut
prendre 1-2 minutes.

Note : si Garmin te demande une vérification en deux étapes lors de la
première connexion, suis les instructions affichées dans le terminal.

## Étape 7 — Lancer le dashboard

```bash
streamlit run dashboard.py
```
Ça ouvre automatiquement ton navigateur sur `http://localhost:8501`.

## Utilisation au quotidien

- Clique sur **"🔄 Synchroniser avec Garmin maintenant"** dans le menu de
  gauche après chaque sortie pour mettre à jour tes données.
- Onglet **Vue d'ensemble** : résumé de ta semaine.
- Onglet **Séances** : historique + courbe d'efficacité (allure/FC) qui
  montre ta vraie progression.
- Onglet **Sommeil & récupération** : score de récupération quotidien,
  qualité de sommeil, FC repos et HRV dans le temps.
- Onglet **Charge d'entraînement** : le fameux ratio **ACWR**
  (Acute:Chronic Workload Ratio), l'indicateur de référence en sciences du
  sport pour anticiper le risque de blessure par surcharge :
  - `< 0.8` → tu pourrais probablement augmenter le volume
  - `0.8 – 1.3` → zone optimale de progression
  - `1.3 – 1.5` → vigilance, pense à une semaine plus légère
  - `> 1.5` → risque élevé, réduis la charge
- Onglet **Recommandations** : conseils texte générés automatiquement à
  partir de ta charge et ta récupération du jour.

## Automatiser la synchronisation (optionnel)

Pour ne plus avoir à cliquer sur "Synchroniser", tu peux programmer
`sync.py` pour qu'il tourne tout seul chaque matin :

**Mac/Linux (cron)** :
```bash
crontab -e
# Ajoute cette ligne (synchro tous les jours à 7h) :
0 7 * * * cd /chemin/vers/garmin_coach && venv/bin/python sync.py --days 2
```

**Windows** : utilise le Planificateur de tâches pour lancer
`venv\Scripts\python.exe sync.py --days 2` chaque matin.

## Utiliser le Coach IA (agent conversationnel)

Le nouvel onglet **💬 Coach IA** te permet de poser des questions en langage
naturel sur tes données ("Comment était ma récupération cette semaine ?",
"Est-ce que je peux enchaîner une sortie longue demain ?"). Il s'appuie sur
tes vraies stats, pas sur des réponses génériques.

Ça utilise **l'API Gemini de Google** (modèle Flash), qui a un vrai palier
gratuit permanent, sans carte bancaire — largement suffisant pour ce type
d'usage personnel. Ta licence Gemini Enterprise ne s'applique pas ici (c'est
un produit différent), mais ce n'est pas grave : la clé gratuite ci-dessous
suffit.

1. Va sur [aistudio.google.com](https://aistudio.google.com) et connecte-toi avec un compte Google
2. Clique sur **Get API key** (en haut à gauche) puis **Create API key**
3. Copie la clé générée
4. Ouvre ton fichier `.env` et colle-la après `GEMINI_API_KEY=`
5. Redémarre le dashboard (`Ctrl+C` dans le terminal puis relance `python -m streamlit run dashboard.py`)

**Limites du palier gratuit** : quelques dizaines de questions par jour dans
la pratique (les limites exactes varient selon Google et peuvent changer).
Pour un usage running perso (quelques questions par jour), tu ne devrais
jamais les atteindre.

Pense à cliquer sur "🗑️ Effacer la conversation" si tu veux repartir de zéro
(par exemple après une nouvelle synchronisation de données) : ça relance une
session avec tes stats à jour.

## Partager l'application avec des amis (mode hébergé)

Par défaut (avec un `.env` rempli), l'appli reste en **mode local mono-utilisateur** : c'est toi, sur ton PC, avec tes identifiants. Pour que des amis puissent l'utiliser avec **leurs propres identifiants Garmin**, il faut l'héberger quelque part d'accessible. Voici comment, gratuitement, via Streamlit Community Cloud :

### Étape 1 — Mettre le code sur GitHub
1. Crée un compte gratuit sur [github.com](https://github.com) si tu n'en as pas
2. Crée un nouveau dépôt (repository), par exemple `garmin-coach`
3. Mets-y tous les fichiers du projet **sauf** `.env`, `garmin_coach.db`, le dossier `venv/` et le dossier `data/` (le `.gitignore` fourni s'en charge automatiquement si tu utilises `git`)

### Étape 2 — Déployer sur Streamlit Community Cloud
1. Va sur [share.streamlit.io](https://share.streamlit.io) et connecte-toi avec ton compte GitHub
2. Clique sur **"New app"**, sélectionne ton dépôt `garmin-coach`
3. Fichier principal : `dashboard.py`
4. Clique sur **Deploy**

### Étape 3 — Configurer ta clé Gemini (secret partagé)
Dans les paramètres de l'appli déployée (⚙️ → **Settings** → **Secrets**), ajoute :
```
GEMINI_API_KEY = "ta_clé_ici"
```
⚠️ **N'ajoute PAS** `GARMIN_EMAIL` ni `GARMIN_PASSWORD` dans les secrets — sans eux, l'appli passe automatiquement en mode multi-utilisateurs, et chacun se connecte avec son propre formulaire de connexion Garmin (identifiants gardés en mémoire le temps de sa session seulement, jamais écrits sur le serveur).

### Étape 4 — Partager le lien
Streamlit te donne une URL du type `https://ton-app.streamlit.app` — c'est ce lien que tu partages à tes amis. Chacun arrive sur un écran de connexion, entre ses propres identifiants Garmin, et voit uniquement ses propres données (bases de données séparées par personne).

### Points importants à connaître
- **Confidentialité** : tes amis entrent leur mot de passe Garmin dans une appli qui tourne sur un serveur cloud (Streamlit). Ce n'est jamais écrit sur disque, mais ça transite par ce serveur le temps de la session — sois transparent avec eux là-dessus avant qu'ils se connectent.
- **Clé Gemini partagée** : par défaut tout le monde utilise ta clé (gratuite, mais avec un quota). Si l'usage devient important, chacun peut renseigner sa propre clé Gemini dans le formulaire de connexion (champ optionnel).
- **Limite Garmin** : plusieurs personnes synchronisant depuis le même serveur peuvent occasionnellement se faire bloquer temporairement par Garmin (429) — la librairie réessaie automatiquement, comme tu l'as déjà vu en local.
- **Coach bénévole avec plusieurs élèves** : chaque élève se connecte avec son propre compte Garmin sur le même lien ; ton coach peut demander à chacun de lui montrer son propre écran, ou tu peux lui donner tes identifiants Garmin si tu veux qu'il voie tes données spécifiquement.

## Prochaines pistes d'évolution

- Détection automatique des zones de FC personnalisées (au lieu de valeurs
  fixes 190/55 dans `analysis.py` — à ajuster avec ta vraie FC max et repos)
- Alertes par email/Slack quand l'ACWR passe en zone rouge
- Import d'un plan d'entraînement pour comparer prévu vs réalisé
- Détection des tendances de VO2max si ta montre les calcule
