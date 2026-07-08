"""
gpx_utils.py
------------
Parse un fichier GPX de parcours de course (exporté depuis le site de
l'épreuve, Strava, ou Garmin Connect) pour en extraire le profil
altimétrique et le dénivelé positif total (D+).
"""

import gpxpy


def parse_gpx(file) -> dict:
    """
    file : objet fichier (ex. retourné par st.file_uploader)
    Retourne {"distance_km": ..., "elevation_gain": ..., "profile": [{"distance_km", "elevation_m"}, ...]}
    """
    gpx = gpxpy.parse(file)
    points = []
    for track in gpx.tracks:
        for segment in track.segments:
            points.extend(segment.points)

    # Certains GPX de parcours utilisent des routes plutôt que des tracks
    if not points:
        for route in gpx.routes:
            points.extend(route.points)

    if not points:
        return {"distance_km": 0, "elevation_gain": 0, "profile": []}

    elevation_gain = 0.0
    profile = []
    cum_dist_m = 0.0
    prev = None

    for p in points:
        if prev is not None:
            d = prev.distance_2d(p) or 0
            cum_dist_m += d
            if p.elevation is not None and prev.elevation is not None:
                diff = p.elevation - prev.elevation
                if diff > 0:
                    elevation_gain += diff
        profile.append({"distance_km": round(cum_dist_m / 1000, 3), "elevation_m": p.elevation})
        prev = p

    # Réduit le nombre de points si le tracé est très détaillé, pour un graphique plus léger
    if len(profile) > 300:
        step = len(profile) // 300
        profile = profile[::step]

    return {
        "distance_km": cum_dist_m / 1000,
        "elevation_gain": round(elevation_gain),
        "profile": profile,
    }
