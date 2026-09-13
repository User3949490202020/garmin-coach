"""
cloud_backup.py
---------------
Sauvegarde/restauration des bases SQLite sur un stockage externe (Supabase
Storage), pour survivre au système de fichiers ÉPHÉMÈRE de Streamlit Cloud :
sans ça, chaque mise à jour de l'appli efface data/ — et les utilisateurs
doivent re-synchroniser et re-saisir FCmax, objectif, nuits manuelles...

Principe (volontairement simple et robuste) :
  - à la première ouverture d'une base absente localement, on la retélécharge
    depuis le bucket si une copie y existe (restore_if_missing) ;
  - après chaque écriture importante, on renvoie le fichier entier vers le
    bucket (backup, avec anti-rafale : au plus un envoi par fichier toutes
    les N secondes, sauf force=True).
Les fichiers font quelques centaines de Ko : l'upload complet reste léger.

Configuration attendue (Streamlit secrets ou variables d'environnement) :
  SUPABASE_URL          ex: https://xxxx.supabase.co
  SUPABASE_SERVICE_KEY  la clé "service_role" (secrète, jamais dans le code)
  Bucket : "allure-db" (privé), à créer une fois dans Supabase.

Sans ces secrets, tout ce module est neutre (aucun appel réseau, aucune
erreur) : l'appli fonctionne exactement comme avant.
"""

import os
import time
from pathlib import Path

import requests

BUCKET = "allure-db"
_TIMEOUT = 20  # secondes par requête : la sauvegarde ne doit jamais bloquer l'appli

# Mémoire du processus : derniers envois (anti-rafale) et restaurations déjà
# tentées (une seule vérification distante par fichier et par démarrage).
_last_upload: dict[str, float] = {}
_restore_checked: set[str] = set()


def _config():
    """(url, key) si la sauvegarde cloud est configurée, sinon None."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        try:  # secrets Streamlit (hébergé) — absent en CLI/local : pas grave
            import streamlit as st
            url = url or st.secrets.get("SUPABASE_URL")
            key = key or st.secrets.get("SUPABASE_SERVICE_KEY")
        except Exception:
            pass
    if url and key:
        return url.rstrip("/"), key
    return None


def is_active() -> bool:
    return _config() is not None


def _object_url(base_url: str, filename: str) -> str:
    return f"{base_url}/storage/v1/object/{BUCKET}/{filename}"


def backup(db_path, min_interval_s: int = 90, force: bool = False) -> bool:
    """
    Envoie le fichier vers le bucket. Anti-rafale par fichier (min_interval_s)
    sauf force=True. Ne lève JAMAIS : une sauvegarde ratée ne doit pas casser
    l'appli (le fichier local reste la référence jusqu'au prochain essai).
    """
    conf = _config()
    if conf is None:
        return False
    path = Path(db_path)
    if not path.exists():
        return False
    key_name = str(path)
    now = time.time()
    if not force and now - _last_upload.get(key_name, 0) < min_interval_s:
        return False
    url, key = conf
    try:
        with open(path, "rb") as f:
            data = f.read()
        r = requests.post(
            _object_url(url, path.name), data=data,
            # "apikey" + "Authorization" : compatibles anciennes clés (JWT
            # service_role) ET nouvelles clés Supabase (sb_secret_...).
            headers={"Authorization": f"Bearer {key}", "apikey": key,
                     "Content-Type": "application/octet-stream",
                     "x-upsert": "true"},
            timeout=_TIMEOUT,
        )
        if r.status_code in (200, 201):
            _last_upload[key_name] = now
            return True
    except Exception:
        pass
    return False


def restore_if_missing(db_path) -> bool:
    """
    Si le fichier n'existe pas localement (déploiement frais), tente de le
    retélécharger depuis le bucket. Une seule tentative par fichier et par
    démarrage. Ne lève jamais.
    """
    conf = _config()
    path = Path(db_path)
    key_name = str(path)
    if conf is None or path.exists() or key_name in _restore_checked:
        return False
    _restore_checked.add(key_name)
    url, key = conf
    try:
        r = requests.get(_object_url(url, path.name),
                         headers={"Authorization": f"Bearer {key}", "apikey": key},
                         timeout=_TIMEOUT)
        if r.status_code == 200 and r.content:
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(r.content)
            # Le fichier restauré est identique à la copie distante : inutile
            # de le re-sauvegarder dans la foulée.
            _last_upload[key_name] = time.time()
            return True
    except Exception:
        pass
    return False
