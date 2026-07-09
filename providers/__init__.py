"""
Package `providers`
--------------------
Couche d'abstraction « fournisseur de données ». Chaque source (Garmin, et
plus tard Strava pour Suunto/toutes marques) implémente la même interface
`DataProvider` et renvoie des dictionnaires NORMALISÉS, identiques quelle que
soit la marque. Voir ARCHITECTURE_SUUNTO.md.
"""
