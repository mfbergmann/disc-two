#!/usr/bin/env python3
"""
Pull the special features off a ripped DVD ISO and file them where Plex looks
for extras.

ISOHungry archives a disc as one ISO, which Plex cannot read at all. This turns
that archive into per-extra MKVs inside the matching Radarr movie folder:

    /data/media/movies/Alien³ (1992) {tmdb-8077}/Featurettes/Optical Fury.mkv

The main feature is deliberately NOT imported by default. The common case here
is already owning a better copy of the film (a 4K remux) and wanting only the
featurettes the disc carries.

Two-step by design:

    extras-import.py scan  disc.iso     -> writes disc.iso.extras.json
    <edit the plan: names, include flags>
    extras-import.py apply disc.iso.extras.json

DVD titles carry no names, only numbers and durations, so no tool can label
them correctly on its own. The plan file is the place a human supplies the
names, once, before anything is encoded. `auto` runs both steps with generated
placeholder names for when that does not matter.

Everything is stdlib plus lsdvd and HandBrakeCLI.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

# A title shorter than this is a logo sting, a menu loop, or a copyright card.
MIN_EXTRA_SECS = int(os.environ.get("EXTRAS_MIN_SECS", "60"))
# Longer than this and it is a second feature or a documentary-length disc, not
# a featurette. Raised via env when a disc really is all long-form extras.
MAX_EXTRA_SECS = int(os.environ.get("EXTRAS_MAX_SECS", "3600"))
# Anything within this fraction of the longest title is another cut of the
# feature (director's cut, open matte, a "play all" chain), not an extra.
FEATURE_RATIO = float(os.environ.get("EXTRAS_FEATURE_RATIO", "0.6"))
# Similarity below which a Radarr match is refused rather than guessed at.
MATCH_THRESHOLD = float(os.environ.get("EXTRAS_MATCH_THRESHOLD", "0.78"))

RADARR_URL = os.environ.get("RADARR_URL", "http://radarr:7878").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
PLEX_URL = os.environ.get("PLEX_URL", "").rstrip("/")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

# Plex reads the folder name to decide the extra's category. "Featurettes" is
# what this library already uses; the others are here so EXTRAS_SUBDIR can be
# pointed at them without guessing at spelling.
# https://support.plex.tv/articles/local-files-for-trailers-and-extras/
PLEX_EXTRA_DIRS = {
    "Featurettes", "Behind The Scenes", "Deleted Scenes", "Interviews",
    "Scenes", "Shorts", "Trailers", "Other",
}
EXTRAS_SUBDIR = os.environ.get("EXTRAS_SUBDIR", "Featurettes")

OWNER_UID = int(os.environ.get("EXTRAS_UID", "99"))
OWNER_GID = int(os.environ.get("EXTRAS_GID", "100"))

# Deinterlacing dominates the runtime, and HandBrake's comb-detect/decomb pair
# is startlingly expensive: measured on one NTSC DVD title, RTX 3060, nvenc_h265
#
#   --comb-detect --decomb        15.2 fps    (~3.7 h for a 112 min feature)
#   --decomb alone                18.0 fps
#   --comb-detect alone           22.7 fps
#   --detelecine --deinterlace    39.5 fps    (~85 min)
#   --detelecine alone            55.8 fps
#   no filters                    94.7 fps
#
# The default below is the fourth line. A film shot at 24 fps and pressed to an
# NTSC DVD is 3:2 pulldown, and detelecine inverts that exactly - it is both
# cheaper AND more correct than decomb, which only papers over the combing.
# yadif then cleans up extras shot on video, which telecine does not describe.
#
# The GPU is not the constraint either way: Debian's HandBrake reports
# "nvdec: is not compiled into this build", so MPEG-2 decode and every filter
# run on CPU and only the encode is offloaded. NVENC sits near-idle at 0-2%.
FILTER_ARGS = (os.environ.get("EXTRAS_FILTERS")
               or "--detelecine --deinterlace").split()


class LsdvdError(Exception):
    """Reading the disc failed.

    A real exception rather than die(): this module is imported by the web UI,
    where sys.exit() would surface to the browser as the string "1".
    """


def log(msg):
    print(msg, flush=True)


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


# ------------------------------------------------------------------ disc scan

def lsdvd_titles(iso_path):
    """Every title on the disc, as {ix, seconds, chapters, vts}.

    lsdvd reads an ISO file directly through libdvdread, so the disc does not
    need to be mounted or even still be in the drive.
    """
    if not shutil.which("lsdvd"):
        raise LsdvdError("lsdvd not installed in this image")
    try:
        # -x adds the per-chapter detail; safe now that the ampersand escaping
        # below handles the unescaped fields it also brings in.
        out = subprocess.run(
            ["lsdvd", "-Ox", "-x", iso_path],
            capture_output=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        raise LsdvdError("lsdvd timed out reading %s" % os.path.basename(iso_path))

    # lsdvd writes libdvdread chatter to stdout ahead of the XML, and emits
    # raw high bytes in the disc title that break a strict UTF-8 parse.
    text = out.stdout.decode("utf-8", "replace")
    start = text.find("<lsdvd>")
    if start == -1:
        raise LsdvdError("no DVD structure found in %s — is it a video DVD? %s"
                         % (os.path.basename(iso_path),
                            out.stderr.decode("utf-8", "replace")[:300]))
    # Strip control characters that libdvdread copies verbatim out of the disc
    # header; ElementTree rejects them outright.
    xml = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text[start:])
    # lsdvd does not escape its own output: a disc whose aspect field reads
    # "Pan&Scan" emits a bare ampersand and the whole document fails to parse.
    # Escape any & that is not already the start of an entity reference.
    xml = re.sub(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#[0-9]+|#x[0-9A-Fa-f]+);)", "&amp;", xml)

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise LsdvdError("could not parse lsdvd output: %s" % e)

    titles = []
    for track in root.findall("track"):
        def field(name, cast, default=0):
            node = track.find(name)
            if node is None or node.text is None:
                return default
            try:
                return cast(node.text)
            except (TypeError, ValueError):
                return default

        ix = field("ix", int)
        if not ix:
            continue
        titles.append({
            "ix": ix,
            "seconds": round(field("length", float), 1),
            "chapters": len(track.findall("chapter")),
            "vts": field("vts", int),
        })
    return titles


def human_duration(seconds):
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m{s:02d}s" if h else f"{m}m{s:02d}s"


def classify(titles):
    """Split titles into the feature, plausible extras, and junk.

    Returns (feature, extras, skipped) with a `reason` on everything skipped so
    the plan file explains itself rather than silently dropping titles.
    """
    if not titles:
        return None, [], []

    feature = max(titles, key=lambda t: t["seconds"])
    feature_secs = feature["seconds"]

    extras, skipped, seen = [], [], set()
    for t in sorted(titles, key=lambda t: t["ix"]):
        if t["ix"] == feature["ix"]:
            continue

        # DVDs routinely repeat a title across VTS boundaries, and "play all"
        # chains re-list every featurette as one more title. Same rounded
        # length and same VTS is the reliable signal for a genuine duplicate.
        key = (t["vts"], int(t["seconds"]))
        if key in seen:
            skipped.append({**t, "reason": "duplicate of an earlier title"})
            continue

        # Feature-length first: a title that is another cut of the film is
        # excluded *because* of that, and saying so is more use than "too
        # long" when someone is reading the plan to decide what went missing.
        if t["seconds"] < MIN_EXTRA_SECS:
            skipped.append({**t, "reason": f"under {MIN_EXTRA_SECS}s (logo or menu loop)"})
        elif feature_secs and t["seconds"] >= feature_secs * FEATURE_RATIO:
            skipped.append({**t, "reason": "close to feature length (alternate cut or play-all)"})
        elif t["seconds"] > MAX_EXTRA_SECS:
            skipped.append({**t, "reason": f"over {MAX_EXTRA_SECS}s (too long for a featurette)"})
        else:
            seen.add(key)
            extras.append(t)

    return feature, extras, skipped


# ---------------------------------------------------------------- radarr

def radarr_get(path):
    if not RADARR_API_KEY:
        die("RADARR_API_KEY is not set")
    req = urllib.request.Request(
        f"{RADARR_URL}/api/v3/{path}",
        headers={"X-Api-Key": RADARR_API_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.URLError as e:
        die(f"Radarr unreachable at {RADARR_URL}: {e}")


def radarr_post(path, payload):
    req = urllib.request.Request(
        f"{RADARR_URL}/api/v3/{path}",
        data=json.dumps(payload).encode(),
        headers={"X-Api-Key": RADARR_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.URLError as e:
        log(f"  ! Radarr rescan request failed: {e}")
        return None


def normalize(s):
    """Fold a title down to something two spellings of it can both reach."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", "and")
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def guess_name_from_iso(iso_path):
    """Recover a searchable film title from the ISO filename.

    Disc volume labels arrive as THE_MATRIX_DISC1, and ISOHungry appends a
    timestamp when the label was too generic to trust.
    """
    name = os.path.splitext(os.path.basename(iso_path))[0]
    name = re.sub(r"_\d{4}-\d{2}-\d{2}_\d{6}$", "", name)   # ISOHungry timestamp
    name = re.sub(r"[_.]+", " ", name)
    name = re.sub(r"\b(disc|disk|side|d)\s*\d+\b", " ", name, flags=re.I)
    name = re.sub(r"\b(dvd|ntsc|pal|widescreen|ws|fs|r1|se|collectors?|edition)\b",
                  " ", name, flags=re.I)
    # ISOHungry timestamps discs whose volume label was too generic to trust.
    # Stripping those words leaves nothing, which is the honest answer: the
    # disc never carried a title, so the caller has to supply one.
    name = re.sub(r"\b(video|video ts|untitled|unnamed|unknown|movie|feature|new volume)\b",
                  " ", name, flags=re.I)
    return " ".join(name.split())


def parse_query(query):
    """Split a search string into (normalized title, year or None).

    A trailing year is a strong signal on its own, so it is scored separately
    rather than being allowed to dilute the title similarity.
    """
    year = None
    ym = re.search(r"\b(19\d{2}|20\d{2})\b", query or "")
    if ym:
        year = int(ym.group(1))
        query = query.replace(ym.group(1), " ")
    return normalize(query or ""), year


def rank_movies(want, movies, year=None):
    """Score every movie against a normalized query.

    Returns [(score, shares_word, movie)] best first. `shares_word` records
    whether a whole word is common to both titles - character similarity alone
    puts "matrix" and "master" at 0.67, close enough to misfile a disc.
    """
    want_tokens = {w for w in want.split() if len(w) >= 3}
    # ISO9660 volume labels cannot contain spaces, so discs arrive squashed:
    # BENDITLIKEBECKHAM_4X3. Word-for-word comparison finds nothing in common
    # with "Bend It Like Beckham", so compare the space-stripped forms too and
    # treat a candidate's word appearing inside the squashed run as shared.
    want_flat = want.replace(" ", "")
    scored = []
    for m in movies:
        candidates = [m.get("title", "")] + [
            a.get("title", "") for a in m.get("alternateTitles", []) or []
        ]
        score, shares_word = 0.0, False
        for c in candidates:
            if not c:
                continue
            norm = normalize(c)
            score = max(score, SequenceMatcher(None, want, norm).ratio())
            norm_words = {w for w in norm.split() if len(w) >= 3}
            if want_tokens & norm_words:
                shares_word = True
            norm_flat = norm.replace(" ", "")
            if norm_flat and len(norm_flat) >= 6:
                score = max(score, SequenceMatcher(None, want_flat, norm_flat).ratio())
                # A squashed label counts as sharing a word when the title's
                # own words are actually present inside it.
                if len(norm_words) > 1 and all(w in want_flat for w in norm_words):
                    shares_word = True
        if year and m.get("year") == year:
            score = min(1.0, score + 0.15)
        scored.append((score, shares_word, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def match_movie(query=None, tmdb_id=None, iso_path=None):
    movies = radarr_get("movie")
    if tmdb_id:
        for m in movies:
            if m.get("tmdbId") == tmdb_id:
                return m, 1.0
        die(f"no movie in Radarr with tmdbId {tmdb_id}")

    if not query:
        query = guess_name_from_iso(iso_path)
    if not query:
        die("could not derive a title from the ISO name; pass --movie or --tmdb-id")

    want, year = parse_query(query)
    if not want:
        die(f"nothing searchable in '{query}'")

    want_tokens = {w for w in want.split() if len(w) >= 3}
    scored = rank_movies(want, movies, year)
    best_score, shares_word, best = scored[0] if scored else (0.0, False, None)

    # Character similarity alone is not enough to separate real titles:
    # "matrix" and "master" score 0.67 against each other, which is high enough
    # to have imported a disc into the wrong film's folder. Requiring a whole
    # word in common as well as a high ratio rejects that pairing outright,
    # and misfiling extras is much worse than being asked to name the film.
    if not best or best_score < MATCH_THRESHOLD or (want_tokens and not shares_word):
        lines = [f"no confident Radarr match for '{query}'."]
        lines.append("Closest candidates:")
        for sc, sw, m in scored[:5]:
            flag = "" if sw else "  (no word in common)"
            lines.append(f"  {sc:.2f}  {m.get('title')} ({m.get('year')}) "
                         f"[tmdb-{m.get('tmdbId')}]{flag}")
        lines.append('Re-run with --movie "Exact Title" or --tmdb-id N.')
        die("\n".join(lines))
    return best, best_score


# ------------------------------------------------------------------ encoding

def pick_encoder():
    """nvenc when the GPU is really usable, x265 otherwise.

    HandBrake probes the card at startup and only lists the nvenc encoders when
    it can actually reach it - with the driver libraries missing the names
    disappear from --help entirely (verified: 0 matches without
    NVIDIA_DRIVER_CAPABILITIES, 3 with). That makes the help text an honest
    capability check, and a far cheaper one than a throwaway encode.
    """
    forced = os.environ.get("EXTRAS_ENCODER")
    if forced:
        return forced
    try:
        out = subprocess.run(["HandBrakeCLI", "--help"],
                             capture_output=True, text=True, timeout=60)
        if re.search(r"^\s*nvenc_h265\s*$", out.stdout + out.stderr, re.M):
            return "nvenc_h265"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    log("  nvenc unavailable in this container, falling back to x265")
    return "x265"


def safe_filename(name):
    name = re.sub(r'[/\\:*?"<>|]', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or "Untitled"


def _looks_complete(path, expect_seconds, tolerance=0.95):
    """Duration of `path` if it is close enough to the title's length.

    Returns the measured duration, or None if the file is missing, unreadable
    or short enough to be a genuine truncation rather than a ragged tail.
    """
    if not expect_seconds or not os.path.exists(path):
        return None
    if os.path.getsize(path) == 0:
        return None
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=120)
        seconds = float((probe.stdout or "0").strip())
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None
    return seconds if seconds >= expect_seconds * tolerance else None


def encode_title(iso_path, title_ix, dest, encoder, expect_seconds=None):
    target_dir = os.path.dirname(dest)
    existed = os.path.isdir(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    if not existed:
        # Match the ownership the files get. A root-owned Featurettes/ beside
        # 99:100 files works, but it is the sort of inconsistency that trips up
        # the next tool to touch the library.
        try:
            os.chown(target_dir, OWNER_UID, OWNER_GID)
            os.chmod(target_dir, 0o775)
        except (PermissionError, OSError):
            pass
    partial = dest + ".partial"

    cmd = [
        "HandBrakeCLI",
        "-i", iso_path,
        "-t", str(title_ix),
        "-o", partial,
        "-f", "av_mkv",
        "-e", encoder,
        "-q", os.environ.get("EXTRAS_QUALITY", "22"),
    ] + FILTER_ARGS + [
        "--all-audio", "--aencoder", "copy", "--audio-fallback", "av_aac",
        "--subtitle", "scan", "--subtitle-forced",
        "--markers",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)

    note = None
    if proc.returncode != 0:
        # HandBrake reports failure for a damaged source even when it has
        # already written essentially the whole title. One disc here has a
        # title whose last pack is malformed: HandBrake exits 5, and the file
        # it produced is 373.6s of an expected 375s and decodes end to end.
        # Throwing that away over an exit code loses a perfectly good extra,
        # so the output gets a look before the verdict.
        salvaged = _looks_complete(partial, expect_seconds)
        if not salvaged:
            if os.path.exists(partial):
                os.remove(partial)
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
            return False, "\n      ".join(tail)
        note = ("HandBrake reported an error but wrote %s of an expected %s "
                "— keeping it; the source is probably damaged near the end"
                % (human_duration(salvaged), human_duration(expect_seconds)))
    elif not os.path.exists(partial) or os.path.getsize(partial) == 0:
        return False, "HandBrake exited cleanly but produced nothing"

    # Rename only once the encode succeeded, so an interrupted run can never
    # leave a truncated file that a later scan treats as already imported.
    os.replace(partial, dest)
    try:
        os.chown(dest, OWNER_UID, OWNER_GID)
        os.chmod(dest, 0o664)
    except PermissionError:
        pass
    # On success `note` is None; when the encode was salvaged it carries the
    # warning, so callers can report a kept-but-imperfect extra as such.
    return True, note


# ------------------------------------------------------------- notification

def notify_plex(movie_path):
    """Ask Plex to rescan just this movie's folder.

    Extras are invisible to Radarr, so a Radarr rescan will not surface them;
    only Plex needs to look again, and only at one directory.
    """
    if not (PLEX_URL and PLEX_TOKEN):
        log("  (PLEX_URL/PLEX_TOKEN unset - skipping Plex scan)")
        return
    try:
        req = urllib.request.Request(
            f"{PLEX_URL}/library/sections/all/refresh"
            f"?path={urllib.parse.quote(movie_path)}&X-Plex-Token={PLEX_TOKEN}"
        )
        urllib.request.urlopen(req, timeout=30).read()
        log(f"  Plex rescan requested for {movie_path}")
    except urllib.error.URLError as e:
        log(f"  ! Plex rescan failed: {e}")


# ------------------------------------------------------------------ commands

def plan_path_for(iso_path):
    return iso_path + ".extras.json"


def cmd_scan(args):
    iso = os.path.abspath(args.iso)
    if not os.path.exists(iso):
        die(f"no such ISO: {iso}")

    log(f"Reading titles from {os.path.basename(iso)} ...")
    try:
        titles = lsdvd_titles(iso)
    except LsdvdError as e:
        die(str(e))
    if not titles:
        die("no titles found - is this a video DVD?")
    feature, extras, skipped = classify(titles)
    log(f"  {len(titles)} titles: feature {human_duration(feature['seconds'])}, "
        f"{len(extras)} candidate extras, {len(skipped)} skipped")

    movie, score = match_movie(args.movie, args.tmdb_id, iso)
    log(f"  Radarr match: {movie['title']} ({movie['year']}) "
        f"[tmdb-{movie['tmdbId']}] confidence {score:.2f}")
    log(f"  -> {movie['path']}/{EXTRAS_SUBDIR}/")

    if EXTRAS_SUBDIR not in PLEX_EXTRA_DIRS:
        log(f"  ! warning: '{EXTRAS_SUBDIR}' is not a folder name Plex recognises "
            f"for extras: {', '.join(sorted(PLEX_EXTRA_DIRS))}")

    plan = {
        "iso": iso,
        "movie": {
            "title": movie["title"], "year": movie["year"],
            "tmdbId": movie["tmdbId"], "path": movie["path"],
            "match_confidence": round(score, 3),
        },
        "extras_subdir": EXTRAS_SUBDIR,
        "encoder": args.encoder or "auto",
        "_help": "Edit 'name' for each entry, set include=false to drop one, "
                 "then: extras-import.py apply <this file>",
        "titles": [
            {
                "ix": t["ix"],
                "duration": human_duration(t["seconds"]),
                "seconds": t["seconds"],
                "chapters": t["chapters"],
                "include": True,
                "name": f"Featurette {n:02d} ({human_duration(t['seconds'])})",
            }
            for n, t in enumerate(extras, 1)
        ],
        "feature_title": {
            "ix": feature["ix"], "duration": human_duration(feature["seconds"]),
            "include": False,
            "name": f"{movie['title']} ({movie['year']}) - DVD",
            "_note": "The main feature. include=false by default; a better copy "
                     "usually already exists in the library.",
        },
        "skipped": [
            {"ix": t["ix"], "duration": human_duration(t["seconds"]), "reason": t["reason"]}
            for t in skipped
        ],
    }

    out = args.out or plan_path_for(iso)
    with open(out, "w") as f:
        json.dump(plan, f, indent=2, ensure_ascii=False)
    try:
        os.chown(out, OWNER_UID, OWNER_GID)
    except PermissionError:
        pass

    log(f"\nPlan written: {out}")
    for t in plan["titles"]:
        log(f"   title {t['ix']:>2}  {t['duration']:>9}  {t['chapters']:>2} ch  -> {t['name']}")
    if skipped:
        log("  skipped:")
        for t in plan["skipped"]:
            log(f"   title {t['ix']:>2}  {t['duration']:>9}  {t['reason']}")
    log("\nEdit the names, then: extras-import.py apply " + out)
    return plan, out


def cmd_apply(args):
    with open(args.plan) as f:
        plan = json.load(f)

    iso = plan["iso"]
    if not os.path.exists(iso):
        die(f"ISO referenced by the plan is gone: {iso}")

    movie_path = plan["movie"]["path"]
    if not os.path.isdir(movie_path):
        die(f"movie folder does not exist: {movie_path}\n"
            f"Is /data mounted the same way Radarr mounts it?")

    extras_dir = os.path.join(movie_path, plan.get("extras_subdir", EXTRAS_SUBDIR))
    wanted = [t for t in plan["titles"] if t.get("include")]
    if plan.get("feature_title", {}).get("include"):
        feat = dict(plan["feature_title"])
        feat["_target_dir"] = movie_path      # the feature belongs beside the extras dir, not in it
        wanted.append(feat)

    if not wanted:
        log("Nothing marked include=true in the plan. Nothing to do.")
        return

    encoder = args.encoder or (plan.get("encoder") if plan.get("encoder") != "auto" else None) \
        or pick_encoder()
    log(f"Encoder: {encoder}")
    log(f"Target:  {extras_dir}")

    ok = failed = 0
    for t in wanted:
        target_dir = t.get("_target_dir", extras_dir)
        dest = os.path.join(target_dir, safe_filename(t["name"]) + ".mkv")

        if os.path.exists(dest) and not args.force:
            log(f"  = title {t['ix']:>2} -> {os.path.basename(dest)} (exists, skipping)")
            continue
        if args.dry_run:
            log(f"  . title {t['ix']:>2} -> {dest}")
            continue

        log(f"  + title {t['ix']:>2} ({t.get('duration', '?')}) -> {os.path.basename(dest)}")
        success, err = encode_title(iso, t["ix"], dest, encoder,
                                    expect_seconds=t.get("seconds"))
        if success:
            ok += 1
        else:
            failed += 1
            log(f"    ! encode failed:\n      {err}")

    if args.dry_run:
        log("\nDry run - nothing written.")
        return

    log(f"\n{ok} imported, {failed} failed.")
    if ok:
        notify_plex(movie_path)
        if RADARR_API_KEY:
            radarr_post("command", {"name": "RescanMovie", "movieId": _radarr_id(plan)})
    if failed:
        sys.exit(1)


def _radarr_id(plan):
    for m in radarr_get("movie"):
        if m.get("tmdbId") == plan["movie"]["tmdbId"]:
            return m["id"]
    return 0


def cmd_auto(args):
    plan, out = cmd_scan(args)
    args.plan = out
    cmd_apply(args)


def main():
    p = argparse.ArgumentParser(
        description="Import DVD special features into a Plex extras folder.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--movie", help='Override the Radarr match, e.g. "Alien 3 1992"')
        sp.add_argument("--tmdb-id", type=int, help="Override the Radarr match by TMDB id")
        sp.add_argument("--encoder", help="HandBrake encoder (default: nvenc_h265, else x265)")

    s = sub.add_parser("scan", help="inspect an ISO and write an editable plan")
    s.add_argument("iso")
    s.add_argument("--out", help="plan file path (default: <iso>.extras.json)")
    common(s)
    s.set_defaults(func=lambda a: cmd_scan(a) and None)

    a = sub.add_parser("apply", help="encode the extras described by a plan")
    a.add_argument("plan")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--force", action="store_true", help="re-encode over existing files")
    a.add_argument("--encoder")
    a.set_defaults(func=cmd_apply)

    u = sub.add_parser("auto", help="scan and apply in one pass, generated names")
    u.add_argument("iso")
    u.add_argument("--out")
    u.add_argument("--dry-run", action="store_true")
    u.add_argument("--force", action="store_true")
    common(u)
    u.set_defaults(func=cmd_auto)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
