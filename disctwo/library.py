#!/usr/bin/env python3
"""
Disc review: confirm what a ripped disc actually is before anything is written
into the film library.

The ripper can only ever guess. A DVD carries a volume label like WB_DVD and a
set of numbered titles; nothing on it names the film, and nothing names the
extras. Guessing wrong means featurettes filed under the wrong movie, which is
tedious to notice and worse to unpick.

So the guess stops here and waits. This module inspects a finished ISO, ranks
it against the films Radarr already manages, and hands the result to the web UI
for a human to confirm. Only after that confirmation does anything get encoded.

Two outcomes:

  in the library    -> extras go into <movie>/Featurettes/, feature untouched
  not in the library-> the film is added to Radarr first, so Radarr computes
                       the folder name; the main title is encoded in alongside
                       the extras and Radarr upgrades it on a later pass

Import runs on a background thread because encoding takes minutes, and the
browser must not be holding a socket open for it.
"""
import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from . import disc as ei

try:
    from . import discdb
except Exception:                                        # noqa: BLE001
    discdb = None

try:
    from . import menus as dvdmenu
except Exception:                                        # noqa: BLE001
    dvdmenu = None

try:
    from . import covers as cover_ocr
except Exception:                                        # noqa: BLE001
    cover_ocr = None

# Where the ISOs live. Read-only as far as this is concerned; it never writes
# to them and never deletes one.
ISO_DIR = os.environ.get("ISO_DIR", "/isos")
# Its own state: review records, cover photos, the catalogue index.
STATE_DIR = os.environ.get("STATE_DIR", "/config")
REVIEW_DIR = os.path.join(STATE_DIR, "review")

# Radarr needs both to add a film: which quality profile to track it against,
# and which root folder to compute the path under. Both are discovered rather
# than configured, so the stack can change without editing this.
ROOT_FOLDER = os.environ.get("RADARR_ROOT_FOLDER", "")
QUALITY_PROFILE = os.environ.get("RADARR_QUALITY_PROFILE", "")

_jobs = {}
_jobs_lock = threading.Lock()


# --------------------------------------------------------------- review state

def _review_path(iso_path):
    os.makedirs(REVIEW_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(iso_path))
    return os.path.join(REVIEW_DIR, safe + ".json")


def load_review(iso_path):
    try:
        with open(_review_path(iso_path)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_review(iso_path, data):
    path = _review_path(iso_path)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chown(path, ei.OWNER_UID, ei.OWNER_GID)
    except (PermissionError, OSError):
        pass
    return data


def review_status(iso_path):
    """One of: new, inspected, scanning, importing, imported, failed, skipped."""
    data = load_review(iso_path)
    status = data.get("status") or "new"
    # Jobs live in memory, so a restart mid-job leaves a review pointing at a
    # job that no longer exists. An import at least said "importing" and could
    # be reported as interrupted; a menu scan said nothing at all, so it just
    # vanished and the disc looked untouched. Every long job now records itself
    # the same way and is recovered the same way.
    if status in ("importing", "scanning"):
        with _jobs_lock:
            alive = data.get("job") in _jobs
        if not alive:
            what = "import" if status == "importing" else "menu scan"
            data["status"] = "failed"
            data["error"] = ("interrupted — the ripper restarted during the %s"
                             % what)
            save_review(iso_path, data)
            return "failed"
    return status


def _clear_job_marker(iso_path, job_id):
    """Release a review from a finished job without losing what it produced."""
    data = load_review(iso_path)
    if data.get("job") == job_id and data.get("status") == "scanning":
        data["status"] = data.get("prior_status") or "inspected"
        data.pop("job", None)
        data.pop("prior_status", None)
        save_review(iso_path, data)


# ------------------------------------------------------------------- inspect

def _movie_brief(m, score=None, in_library=True):
    out = {
        "title": m.get("title"),
        "year": m.get("year"),
        "tmdbId": m.get("tmdbId"),
        "path": m.get("path") or "",
        "in_library": in_library,
        "hasFile": bool(m.get("hasFile")),
        "poster": "",
    }
    for img in m.get("images") or []:
        if img.get("coverType") == "poster":
            out["poster"] = img.get("remoteUrl") or img.get("url") or ""
            break
    if score is not None:
        out["score"] = round(score, 3)
    return out


def lookup_discdb(iso_path, titles):
    """Ask TheDiscDb what this disc actually is. Never fatal.

    Identification is by a hash of the VIDEO_TS file sizes, so a hit is the
    same physical pressing rather than a film with a similar name.
    """
    if not discdb:
        return None
    try:
        entry, how, conf = discdb.match_disc(
            iso_path, [t["seconds"] for t in titles])
        if not entry:
            return {"matched": False, "reason": how}
        return {
            "matched": True, "how": how, "confidence": conf,
            "movie": entry.get("movie"), "year": entry.get("year"),
            "tmdb": entry.get("tmdb"), "release": entry.get("release"),
            "names": discdb.name_titles(entry, titles),
        }
    except Exception as e:                               # noqa: BLE001
        return {"matched": False, "reason": "lookup failed: %s" % e}


def inspect_iso(iso_path):
    """Read the disc's titles and propose a film, without writing anything."""
    titles = ei.lsdvd_titles(iso_path)
    if not titles:
        raise ValueError("no DVD titles found — is this a video DVD?")

    feature, extras, skipped = ei.classify(titles)
    guess = ei.guess_name_from_iso(iso_path)
    disc = lookup_discdb(iso_path, titles)

    suggestion, candidates = None, []

    # An exact disc match names the film outright. A hash hit is the same
    # physical pressing, which beats any amount of string similarity against a
    # squashed volume label, so it wins over the guess-from-filename path.
    if disc and disc.get("matched") and disc.get("tmdb"):
        try:
            want_tmdb = int(disc["tmdb"])
        except (TypeError, ValueError):
            want_tmdb = None
        if want_tmdb:
            for m in ei.radarr_get("movie"):
                if m.get("tmdbId") == want_tmdb:
                    suggestion = _movie_brief(m, 1.0)
                    break
            if not suggestion:
                for m in search_tmdb("%s %s" % (disc.get("movie") or "",
                                                disc.get("year") or "")):
                    if m["tmdbId"] == want_tmdb:
                        suggestion = m
                        break
            if suggestion:
                candidates = [suggestion]

    if guess and not suggestion:
        want, year = ei.parse_query(guess)
        if want:
            ranked = ei.rank_movies(want, ei.radarr_get("movie"), year)
            candidates = [_movie_brief(m, s) for s, shares, m in ranked[:6]
                          if s > 0.4 and (shares or s >= 0.85)]
            top_score, shares_word, top = ranked[0] if ranked else (0, False, None)
            # Same bar the CLI uses. Below it the UI shows candidates and a
            # search box instead of a pre-selected answer, so a weak guess
            # never arrives looking like a decision already made.
            if top and top_score >= ei.MATCH_THRESHOLD and shares_word:
                suggestion = _movie_brief(top, top_score)

        # Nothing in the library looks right, so the disc is probably a film
        # that is not tracked yet. Ask TMDB now rather than making someone
        # retype a title the disc label already spelled out.
        if not suggestion:
            seen = {c["tmdbId"] for c in candidates}
            for m in search_tmdb(guess):
                if m["tmdbId"] not in seen:
                    candidates.append(m)
                    seen.add(m["tmdbId"])

    # Names from the catalogue where it has them, placeholders everywhere else.
    # A catalogued name also carries its category, so deleted scenes land in
    # Deleted Scenes rather than all extras being swept into Featurettes.
    named = (disc or {}).get("names") or {}
    extra_rows = []
    for n, t in enumerate(extras, 1):
        info = named.get(t["ix"])
        row = {
            "ix": t["ix"],
            "duration": ei.human_duration(t["seconds"]),
            "seconds": t["seconds"],
            "chapters": t["chapters"],
            "include": True,
            "name": "Featurette %02d (%s)" % (n, ei.human_duration(t["seconds"])),
            "subdir": ei.EXTRAS_SUBDIR,
            "source": "placeholder",
        }
        if info and not info.get("is_feature"):
            row.update(name=info["name"], subdir=info["subdir"],
                       source="thediscdb", discdb_type=info["type"])
        extra_rows.append(row)

    return {
        "iso": iso_path,
        "guess": guess,
        "suggestion": suggestion,
        "candidates": candidates,
        "discdb": disc,
        "feature": {
            "ix": feature["ix"],
            "duration": ei.human_duration(feature["seconds"]),
            "seconds": feature["seconds"],
            "chapters": feature["chapters"],
        },
        "extras": extra_rows,
        "skipped": [
            {"ix": t["ix"], "duration": ei.human_duration(t["seconds"]),
             "reason": t["reason"]}
            for t in skipped
        ],
    }


# -------------------------------------------------------------------- search

def search_library(term):
    """Rank the films Radarr already manages against a search term.

    Requires a whole word in common, not just a good character ratio. Without
    it a search for "The Matrix" lists The Master, Mata Hari and Matilda above
    the TMDB result for the film actually being searched for - which invites
    exactly the misfiling this whole flow exists to prevent.
    """
    want, year = ei.parse_query(term)
    if not want:
        return []
    ranked = ei.rank_movies(want, ei.radarr_get("movie"), year)
    return [_movie_brief(m, s) for s, shares_word, m in ranked[:8]
            if s > 0.4 and (shares_word or s >= 0.85)]


def search_tmdb(term):
    """Radarr's own TMDB lookup, for films not in the library yet.

    Going through Radarr rather than TMDB directly means the title, year and
    tmdbId are exactly the ones Radarr will use to build the folder name.
    """
    try:
        results = ei.radarr_get(
            "movie/lookup?term=" + urllib.parse.quote(term[:200]))
    except SystemExit:
        return []
    out = []
    for m in results[:8]:
        # Radarr returns library entries here too, flagged by having an id.
        out.append(_movie_brief(m, in_library=bool(m.get("id"))))
    return out


# ------------------------------------------------------- adding to the library

def _radarr_defaults():
    root = ROOT_FOLDER
    if not root:
        folders = ei.radarr_get("rootfolder")
        if not folders:
            raise ValueError("Radarr has no root folder configured")
        # The movies root, not whatever happens to be first.
        pick = next((f for f in folders if "movie" in (f.get("path") or "").lower()),
                    folders[0])
        root = pick["path"]

    profile = QUALITY_PROFILE
    if not profile:
        profiles = ei.radarr_get("qualityprofile")
        if not profiles:
            raise ValueError("Radarr has no quality profile configured")
        # The profile the library overwhelmingly already uses, not whichever
        # Radarr happens to list first. A film added by this flow should be
        # tracked the same way every other film is, so it upgrades on the same
        # terms. RADARR_QUALITY_PROFILE overrides when that is not wanted.
        counts = {}
        for m in ei.radarr_get("movie"):
            pid = m.get("qualityProfileId")
            if pid:
                counts[pid] = counts.get(pid, 0) + 1
        valid = {p["id"] for p in profiles}
        ranked = sorted((n, pid) for pid, n in counts.items() if pid in valid)
        profile = ranked[-1][1] if ranked else profiles[0]["id"]
    return root, int(profile)


def add_to_radarr(tmdb_id):
    """Add a film to Radarr and return its record, folder path included.

    Radarr computes the folder name from its own naming configuration, so the
    directory this creates matches every other folder in the library by
    construction rather than by imitation.
    """
    for m in ei.radarr_get("movie"):
        if m.get("tmdbId") == tmdb_id:
            return m                                    # already there

    lookup = ei.radarr_get("movie/lookup/tmdb?tmdbId=%d" % tmdb_id)
    if isinstance(lookup, list):
        lookup = lookup[0] if lookup else None
    if not lookup:
        raise ValueError("TMDB id %s not found" % tmdb_id)

    root, profile = _radarr_defaults()
    payload = {
        "title": lookup["title"],
        "tmdbId": tmdb_id,
        "year": lookup.get("year"),
        "titleSlug": lookup.get("titleSlug"),
        "images": lookup.get("images", []),
        "qualityProfileId": profile,
        "rootFolderPath": root,
        "monitored": True,
        "minimumAvailability": "released",
        # Do not kick off an indexer search: the disc in hand is the point.
        # Radarr will upgrade on its own schedule once the film is monitored.
        "addOptions": {"searchForMovie": False},
    }
    created = ei.radarr_post("movie", payload)
    if not created or not created.get("path"):
        raise ValueError("Radarr rejected the add for tmdb-%d" % tmdb_id)
    return created


# --------------------------------------------------------------- import jobs

def _set(job_id, **kw):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).update(kw)


def job(job_id):
    with _jobs_lock:
        return dict(_jobs.get(job_id) or {})


def _run_import(job_id, iso_path, movie, extras, include_feature, feature_ix,
                feature_name, feature_seconds=None):
    """Encode the chosen titles into the movie folder. Runs on its own thread."""
    try:
        movie_path = movie["path"]
        os.makedirs(movie_path, exist_ok=True)
        try:
            os.chown(movie_path, ei.OWNER_UID, ei.OWNER_GID)
        except (PermissionError, OSError):
            pass

        encoder = ei.pick_encoder()

        work = []
        if include_feature:
            # The feature belongs beside the extras folder, not inside it.
            # Naming it after the folder is what Radarr's scanner expects; it
            # renames to the configured format when it imports.
            work.append((feature_ix, os.path.join(
                movie_path, ei.safe_filename(feature_name) + ".mkv"), True,
                feature_seconds))
        for t in extras:
            if not t.get("include"):
                continue
            # Each extra carries its own Plex folder: TheDiscDb knows a deleted
            # scene from a featurette, and Plex presents them differently.
            subdir = t.get("subdir") or ei.EXTRAS_SUBDIR
            if subdir not in ei.PLEX_EXTRA_DIRS:
                subdir = ei.EXTRAS_SUBDIR
            work.append((t["ix"], os.path.join(
                movie_path, subdir, ei.safe_filename(t["name"]) + ".mkv"),
                False, t.get("seconds")))

        _set(job_id, status="running", total=len(work), done=0,
             encoder=encoder, movie=movie, log=[])

        ok = failed = 0
        for n, (ix, dest, is_feature, seconds) in enumerate(work, 1):
            label = os.path.basename(dest)
            _set(job_id, current=label, done=n - 1)
            if os.path.exists(dest):
                _append_log(job_id, "skipped (exists): %s" % label)
                continue
            good, note = ei.encode_title(iso_path, ix, dest, encoder,
                                         expect_seconds=seconds)
            if good:
                ok += 1
                _append_log(job_id, "imported: %s%s"
                            % (label, "  (%s)" % note if note else ""))
            else:
                failed += 1
                _append_log(job_id, "FAILED: %s — %s" % (label, (note or "")[:200]))

        _set(job_id, done=len(work), current="")

        if ok:
            ei.notify_plex(movie_path)
            # Radarr only learns about the main feature by rescanning; extras
            # are invisible to it either way.
            if movie.get("id"):
                ei.radarr_post("command", {"name": "RescanMovie",
                                           "movieId": movie["id"]})
                _append_log(job_id, "asked Radarr to rescan")

        _set(job_id, status="failed" if failed and not ok else "done",
             ok=ok, failed=failed, finished=time.time())

        review = load_review(iso_path)
        review.update({
            "status": "imported" if ok and not failed else
                      ("failed" if failed and not ok else "imported"),
            "movie": movie, "imported": ok, "failed": failed,
            "finished": time.time(),
        })
        save_review(iso_path, review)

    except Exception as e:                              # noqa: BLE001
        _set(job_id, status="failed", error=str(e),
             trace=traceback.format_exc()[-800:], finished=time.time())
        review = load_review(iso_path)
        review.update({"status": "failed", "error": str(e)})
        save_review(iso_path, review)


def _append_log(job_id, line):
    with _jobs_lock:
        _jobs.setdefault(job_id, {}).setdefault("log", []).append(line)


def contribute(iso_path, movie, extras, feature_ix=None, feature_seconds=None,
               feature_chapters=None, release_title=None, release_year=None):
    """Prepare a TheDiscDb submission for a disc the catalogue does not have.

    Only worth doing once a human has named the extras: the value being
    contributed *is* the names, and "Featurette 01" helps nobody.
    """
    if not discdb:
        raise ValueError("TheDiscDb support is unavailable")

    named = [t for t in extras if t.get("include") and t.get("name")
             and not re.match(r"^Featurette \d+", t["name"] or "")]
    if not named:
        raise ValueError("name the extras first — a submission of "
                         "'Featurette 01' is worse than no submission")

    titles = [{"ix": t["ix"], "seconds": t["seconds"], "name": t["name"],
               "discdb_type": _plex_to_discdb(t.get("subdir")),
               "chapters": t.get("chapters")}
              for t in named]
    if feature_ix is not None:
        titles.insert(0, {"ix": feature_ix, "seconds": feature_seconds,
                          "name": movie["title"], "discdb_type": "MainMovie",
                          "chapters": feature_chapters})

    out_dir = os.path.join(STATE_DIR, "submissions")
    result = discdb.export_contribution(
        iso_path,
        {"title": movie["title"], "year": movie["year"],
         "tmdbId": movie["tmdbId"]},
        titles, out_dir, release_title, release_year)

    review = load_review(iso_path)
    review["contributed"] = {"at": time.time(), "dir": result["release_dir"],
                             "content_hash": result["content_hash"]}
    save_review(iso_path, review)
    return result


# The reverse of discdb.TYPE_TO_PLEX. Several of their types collapse onto one
# Plex folder, so this picks the type a contributor would most likely mean.
_PLEX_TO_DISCDB = {
    "Featurettes": "Featurette",
    "Behind The Scenes": "Featurette",
    "Deleted Scenes": "DeletedScene",
    "Trailers": "Trailer",
    "Interviews": "Interview",
    "Scenes": "Scene",
    "Shorts": "Short",
    "Other": "Other",
}


def _plex_to_discdb(subdir):
    return _PLEX_TO_DISCDB.get(subdir or "", "Extra")


def discdb_status():
    """What the local catalogue knows, for the UI to show without a lookup."""
    if not discdb:
        return {"available": False}
    index = discdb.load_index()
    if not index:
        return {"available": True, "synced": False}
    return {"available": True, "synced": True,
            "discs": index.get("count", 0), "built": index.get("built")}


def start_menu_scan(iso_path):
    """Read the disc's own menus for extra names, on a background thread.

    Minutes of work — every menu has to be rendered to frames and every button
    region OCRed — so this is never part of inspecting a disc. It is offered
    when TheDiscDb has nothing, which is when it is worth the wait.
    """
    if not dvdmenu:
        raise ValueError("menu reading is unavailable")

    job_id = "menu-%d" % (time.time() * 1000)
    _set(job_id, status="running", total=1, done=0,
         current="reading disc menus", log=[])

    # Recorded before the thread starts, so a restart one second later still
    # leaves evidence that a scan was under way.
    claim = load_review(iso_path)
    claim["prior_status"] = claim.get("status") or "new"
    claim["status"] = "scanning"
    claim["job"] = job_id
    save_review(iso_path, claim)

    def run():
        try:
            names = dvdmenu.menu_names(iso_path, want_frames=2)
            review = load_review(iso_path)
            review["menu_names"] = [
                {"label": n["label"], "kind": n["kind"], "score": n["score"]}
                for n in names]
            review["menu_scanned"] = time.time()
            save_review(iso_path, review)
            review["status"] = review.get("prior_status") or "inspected"
            review.pop("job", None)
            review.pop("prior_status", None)
            save_review(iso_path, review)
            for n in names:
                _append_log(job_id, "%s  [%s]" % (n["label"], n["kind"]))
            _append_log(job_id, "read %d candidate names" % len(names))
            _set(job_id, status="done", done=1, current="",
                 names=review["menu_names"], finished=time.time())
        except Exception as e:                           # noqa: BLE001
            _append_log(job_id, "FAILED: %s" % e)
            _clear_job_marker(iso_path, job_id)
            _set(job_id, status="failed", error=str(e), finished=time.time())

    threading.Thread(target=run, daemon=True).start()
    return job_id


def read_cover_photo(iso_path, image_bytes):
    """Read a photo of the case's back cover for the special-features list.

    Fast enough to answer inline - one image, a few OCR passes - unlike the
    menu scan, which has to render every menu on the disc.
    """
    if not cover_ocr:
        raise ValueError("cover reading is unavailable")

    photo = _review_path(iso_path).replace(".json", ".cover.jpg")
    with open(photo, "wb") as fh:
        fh.write(image_bytes)
    try:
        os.chown(photo, ei.OWNER_UID, ei.OWNER_GID)
    except (PermissionError, OSError):
        pass

    items, err = cover_ocr.read_cover(photo)
    review = load_review(iso_path)
    review["cover_photo"] = photo
    review["cover_names"] = items
    review["cover_read"] = time.time()
    save_review(iso_path, review)
    return items, err


def start_discdb_sync():
    job_id = "discdb-%d" % (time.time() * 1000)
    _set(job_id, status="running", total=1, done=0,
         current="fetching TheDiscDb", log=[])

    def run():
        try:
            index = discdb.sync()
            _append_log(job_id, "indexed %d discs" % index.get("count", 0))
            _set(job_id, status="done", done=1, current="",
                 finished=time.time())
        except Exception as e:                           # noqa: BLE001
            _append_log(job_id, "FAILED: %s" % e)
            _set(job_id, status="failed", error=str(e), finished=time.time())

    threading.Thread(target=run, daemon=True).start()
    return job_id


def start_import(iso_path, tmdb_id, extras, include_feature=False,
                 feature_ix=None, feature_name=None, feature_seconds=None,
                 add_if_missing=True):
    """Confirm the film, then kick off encoding on a background thread."""
    # One import per disc. Two jobs on the same ISO would race for the same
    # .partial paths and interleave two HandBrake runs over one optical image
    # for no gain, and the second would silently "skip (exists)" whatever the
    # first had already finished.
    if review_status(iso_path) == "importing":
        raise ValueError("this disc is already importing — "
                         "let it finish, or wait for it to fail")

    movie = None
    for m in ei.radarr_get("movie"):
        if m.get("tmdbId") == tmdb_id:
            movie = m
            break

    created = False
    if movie is None:
        if not add_if_missing:
            raise ValueError("tmdb-%s is not in Radarr" % tmdb_id)
        movie = add_to_radarr(tmdb_id)
        created = True

    if not feature_name:
        feature_name = os.path.basename(movie["path"].rstrip("/"))

    job_id = "%d" % (time.time() * 1000)
    _set(job_id, status="queued", iso=iso_path, done=0, total=0,
         created_movie=created, log=[])

    save_review(iso_path, {
        "status": "importing", "movie": _movie_brief(movie),
        "job": job_id, "started": time.time(),
        "created_movie": created,
    })

    threading.Thread(
        target=_run_import,
        args=(job_id, iso_path, movie, extras, include_feature, feature_ix,
              feature_name, feature_seconds),
        daemon=True,
    ).start()
    return job_id, movie, created


def vision_status():
    """What the vision model is doing, for the UI to show honestly."""
    try:
        from . import vision
    except Exception:                                    # noqa: BLE001
        return {"configured": False}
    return {
        "configured": bool(vision.OLLAMA_URL and vision.VISION_MODEL),
        "model": vision.VISION_MODEL or None,
        "available": vision.available(),
        "loaded": vision.loaded(),
    }


def radarr_status():
    """Whether Radarr is reachable, and how much it manages."""
    if not ei.RADARR_API_KEY:
        return {"configured": False}
    try:
        movies = ei.radarr_get("movie")
    except SystemExit:
        return {"configured": True, "reachable": False}
    return {"configured": True, "reachable": True, "movies": len(movies)}
