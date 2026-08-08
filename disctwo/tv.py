#!/usr/bin/env python3
"""
Television box sets: telling an episode disc from a film, and filing it.

A film disc has one title far longer than the rest. An episode disc does not —
it has several of much the same length, and often a "play all" chain longer
than any of them. Run the film classifier over that and it calls the play-all
chain the feature and the episodes extras, which is wrong in every particular.

Detection therefore looks at the shape of the durations rather than at the
longest one. Where TheDiscDb knows the disc it does not have to guess at all:
the catalogue records a season and episode number per title.
"""

import os
import re
import urllib.error
import urllib.parse
import urllib.request
import json

SONARR_URL = os.environ.get("SONARR_URL", "").rstrip("/")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")

# An episode is somewhere between a short-form comedy and a feature-length
# special. Below this it is an extra; above it, it is a film or a play-all.
MIN_EPISODE_SECS = int(os.environ.get("TV_MIN_EPISODE_SECS", "900"))     # 15 min
MAX_EPISODE_SECS = int(os.environ.get("TV_MAX_EPISODE_SECS", "4200"))    # 70 min
# How alike two runtimes must be to count as the same kind of thing. Episodes
# of one season vary by a minute or two of credits, not by half.
EPISODE_SPREAD = float(os.environ.get("TV_EPISODE_SPREAD", "0.25"))
# Two is enough. A disc whose season opener is feature-length leaves only two
# ordinary episodes beside it, and that is a common shape rather than a rare
# one — requiring three missed fourteen real box sets in the catalogue.
MIN_EPISODES = int(os.environ.get("TV_MIN_EPISODES", "2"))


class SonarrError(Exception):
    pass


# ------------------------------------------------------------------ detection

def looks_like_episodes(titles):
    """(is_tv, episodes, reason) from the shape of a disc's runtimes.

    The test is a cluster of similar-length titles, not the presence of a long
    one. A play-all chain is evidence *for* a box set, never against it.
    """
    runs = [t for t in titles
            if MIN_EPISODE_SECS <= t["seconds"] <= MAX_EPISODE_SECS]
    if len(runs) < MIN_EPISODES:
        return False, [], "only %d titles of episode length" % len(runs)

    runs.sort(key=lambda t: t["seconds"])
    median = runs[len(runs) // 2]["seconds"]
    cluster = [t for t in runs
               if abs(t["seconds"] - median) <= median * EPISODE_SPREAD]
    if len(cluster) < MIN_EPISODES:
        return False, [], "no cluster of similar runtimes"

    # A film with several long featurettes could still reach here. What settles
    # it is that a film's feature towers over everything else, and an episode
    # disc's longest title is either one of the cluster or the sum of it.
    longest = max(titles, key=lambda t: t["seconds"])
    in_cluster = any(t["ix"] == longest["ix"] for t in cluster)
    # A play-all chain is the sum of the episodes it chains, which is not
    # necessarily the sum of the cluster: a disc whose runtimes straddle the
    # spread leaves some episodes outside it, and comparing against the cluster
    # alone then makes a perfectly ordinary box set look like a film.
    runs_total = sum(t["seconds"] for t in runs)
    cluster_total = sum(t["seconds"] for t in cluster)
    is_playall = any(abs(longest["seconds"] - total) <= total * 0.2
                     for total in (runs_total, cluster_total) if total)
    if not (in_cluster or is_playall):
        return False, [], ("longest title (%ds) dwarfs the cluster — looks like "
                           "a film with long extras" % longest["seconds"])

    cluster.sort(key=lambda t: t["ix"])
    reason = "%d titles around %dm%s" % (
        len(cluster), int(median // 60),
        ", plus a play-all chain" if is_playall and not in_cluster else "")
    return True, cluster, reason


def playall_titles(titles, episodes):
    """Titles that are just the episodes joined together, to be excluded."""
    total = sum(t["seconds"] for t in episodes)
    return [t for t in titles
            if t["seconds"] > MAX_EPISODE_SECS
            and abs(t["seconds"] - total) <= total * 0.2]


# --------------------------------------------------------------------- sonarr

def _get(path):
    if not (SONARR_URL and SONARR_API_KEY):
        raise SonarrError("Sonarr is not configured")
    req = urllib.request.Request("%s/api/v3/%s" % (SONARR_URL, path),
                                 headers={"X-Api-Key": SONARR_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.URLError as e:
        raise SonarrError("Sonarr unreachable at %s: %s" % (SONARR_URL, e))


def post(path, payload):
    req = urllib.request.Request(
        "%s/api/v3/%s" % (SONARR_URL, path), data=json.dumps(payload).encode(),
        headers={"X-Api-Key": SONARR_API_KEY, "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.URLError:
        return None


def configured():
    return bool(SONARR_URL and SONARR_API_KEY)


def status():
    if not configured():
        return {"configured": False}
    try:
        series = _get("series")
    except SonarrError:
        return {"configured": True, "reachable": False}
    return {"configured": True, "reachable": True, "series": len(series)}


def all_series():
    return _get("series")


def search_series(term):
    """Rank the shows Sonarr manages, reusing the film matcher's caution."""
    from . import disc as ei
    want, year = ei.parse_query(term)
    if not want:
        return []
    ranked = ei.rank_movies(want, all_series(), year)   # same title/year shape
    out = []
    for score, shares, s in ranked[:8]:
        if score <= 0.4 or not (shares or score >= 0.85):
            continue
        out.append(_series_brief(s, score))
    return out


def _series_brief(s, score=None):
    out = {"title": s.get("title"), "year": s.get("year"), "id": s.get("id"),
           "tvdbId": s.get("tvdbId"), "path": s.get("path") or "",
           "seasons": [x.get("seasonNumber") for x in s.get("seasons") or []],
           "poster": ""}
    for img in s.get("images") or []:
        if img.get("coverType") == "poster":
            out["poster"] = img.get("remoteUrl") or img.get("url") or ""
            break
    if score is not None:
        out["score"] = round(score, 3)
    return out


def episodes(series_id, season):
    """Sonarr's episode list for one season, in order."""
    eps = _get("episode?seriesId=%d&seasonNumber=%d" % (int(series_id), int(season)))
    eps.sort(key=lambda e: e.get("episodeNumber") or 0)
    return [{"number": e.get("episodeNumber"), "title": e.get("title"),
             "runtime": e.get("runtime"), "hasFile": bool(e.get("hasFile")),
             "id": e.get("id")} for e in eps]


def rescan(series_id):
    return post("command", {"name": "RescanSeries", "seriesId": int(series_id)})


# ---------------------------------------------------------------------- filing

def season_dir(series_path, season):
    """Sonarr's own convention, which Plex reads without help."""
    return os.path.join(series_path,
                        "Specials" if int(season) == 0 else "Season %02d" % int(season))


def episode_filename(series_title, season, number, title):
    """A name Sonarr's scanner parses, and renames to your format on import."""
    from . import disc as ei
    safe = ei.safe_filename(title or "")
    stem = "%s - S%02dE%02d" % (series_title, int(season), int(number))
    return "%s - %s.mkv" % (stem, safe) if safe else "%s.mkv" % stem


def map_episodes(disc_titles, first_episode=1, catalogue=None):
    """Pair a disc's episode titles with episode numbers.

    A catalogue hit carries the numbers outright. Without one, discs run in
    order and the only unknown is where this disc starts in the season — which
    is one number a human can supply, rather than a mapping they have to build.
    """
    rows = []
    for n, t in enumerate(sorted(disc_titles, key=lambda t: t["ix"])):
        info = (catalogue or {}).get(t["ix"]) or {}
        rows.append({
            "ix": t["ix"],
            "seconds": t["seconds"],
            "chapters": t.get("chapters", 0),
            "season": info.get("season"),
            "number": info.get("number", first_episode + n),
            "title": info.get("name", ""),
            "source": "thediscdb" if info else "sequence",
            "include": True,
        })
    return rows
