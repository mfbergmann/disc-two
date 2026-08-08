#!/usr/bin/env python3
"""
TheDiscDb lookup: recover the real names of a disc's extras.

A DVD title is a number and a duration. The names printed on the box exist
nowhere on the disc in machine-readable form, so a ripper can only ever call
them "Featurette 01". TheDiscDb (https://thediscdb.com, MIT-licensed data at
github.com/TheDiscDb/data) is a community catalogue of exactly that missing
information: which title is the main feature, which are extras, and what each
one is actually called.

Identification is exact, not fuzzy. TheDiscDb keys every disc on a ContentHash
that is just an MD5 over the sizes of the files in VIDEO_TS, sorted by name:

    md5( int64le(size) for each file in sorted(VIDEO_TS) )

That is reproducible from a ripped ISO with no disc in the drive, and it was
verified byte-for-byte against a real catalogued DVD before this was written
(The White Lotus 2025 Season 3 DVD, disc 2 -> 6C28F8D0CDB1DD836E1B13174D083B6C).
Sizes come from the disc's own filesystem, so two rips of the same pressing
agree and two different pressings do not.

A duration-fingerprint fallback exists for discs whose files were altered in
transit, but the hash is the primary key and the only one trusted outright.

Coverage is the real limit: TheDiscDb is Blu-ray-first and holds far fewer
DVDs, so most discs will miss. A miss is not a failure — it is the case for
contributing the disc back, which `export` prepares.

    discdb.py sync                  refresh the local index
    discdb.py lookup <iso>          identify a disc and print its extras
    discdb.py export <iso> --plan p prepare a contribution for an unknown disc
"""

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
import unicodedata

DISCDB_DIR = os.environ.get("DISCDB_DIR", "/config/discdb")
REPO_DIR = os.path.join(DISCDB_DIR, "repo")
INDEX_PATH = os.path.join(DISCDB_DIR, "index.json")
REPO_URL = os.environ.get("DISCDB_REPO", "https://github.com/TheDiscDb/data.git")

# Only the JSON is needed. The full repository is 2.1 GB, most of it cover art
# and MakeMKV logs; a blobless sparse checkout of just these is ~290 MB.
SPARSE_PATTERNS = ["/data/**/disc*.json", "/data/**/metadata.json",
                   "/data/**/release.json"]

OWNER_UID = int(os.environ.get("EXTRAS_UID", "99"))
OWNER_GID = int(os.environ.get("EXTRAS_GID", "100"))

# TheDiscDb's item types onto the folder names Plex recognises for extras.
# "Extra" is their catch-all and by far the most common; Featurettes is the
# closest Plex bucket and the one this library already uses.
# https://support.plex.tv/articles/local-files-for-trailers-and-extras/
TYPE_TO_PLEX = {
    "Featurette": "Featurettes",
    "Extra": "Featurettes",
    "DeletedScene": "Deleted Scenes",
    "Trailer": "Trailers",
    "Interview": "Interviews",
    "Scene": "Scenes",
    "Short": "Shorts",
    "Music": "Other",
    "Other": "Other",
}


class DiscDbError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------- content hash

def iso_video_ts_files(iso_path):
    """(name, size) for every file in the ISO's VIDEO_TS directory."""
    try:
        out = subprocess.run(["isoinfo", "-l", "-i", iso_path],
                             capture_output=True, timeout=300)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        raise DiscDbError("could not read %s: %s" % (os.path.basename(iso_path), e))

    text = out.stdout.decode("utf-8", "replace")
    files, in_video_ts = [], False
    for line in text.splitlines():
        line = line.rstrip()
        if line.startswith("Directory listing of "):
            in_video_ts = line.strip().upper().endswith("/VIDEO_TS/")
            continue
        if not in_video_ts or not line or line.startswith("d"):
            continue
        # ----------   0 0 0   26624 Aug  7 2026 [   1192 00]  VIDEO_TS.BUP;1
        m = re.match(r"^-\S*\s+\d+\s+\d+\s+\d+\s+(\d+)\s+.*\]\s+(\S+)\s*$", line)
        if not m:
            continue
        size, name = int(m.group(1)), m.group(2)
        name = name.split(";")[0]          # strip the ISO9660 version suffix
        files.append((name, size))
    return files


def content_hash(iso_path):
    """TheDiscDb's disc identity for a ripped ISO.

    MD5 over the little-endian int64 size of each VIDEO_TS file, in name order.
    Names, timestamps and contents are deliberately not part of it, which is
    why a re-rip of the same pressing still matches.
    """
    files = iso_video_ts_files(iso_path)
    if not files:
        raise DiscDbError("no VIDEO_TS files in %s — not a video DVD?"
                          % os.path.basename(iso_path))
    h = hashlib.md5()
    for _name, size in sorted(files, key=lambda f: f[0]):
        h.update(struct.pack("<q", size))
    return h.hexdigest().upper(), files


# ---------------------------------------------------------------------- index

def parse_duration(text):
    """'1:28:42' or '0:06:33' -> seconds."""
    if not text:
        return 0
    parts = [p for p in str(text).split(":") if p != ""]
    try:
        parts = [int(float(p)) for p in parts]
    except ValueError:
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


def _iter_disc_files(repo_dir):
    data_root = os.path.join(repo_dir, "data")
    for root, dirs, files in os.walk(data_root):
        for name in files:
            if re.fullmatch(r"disc\d+\.json", name):
                yield os.path.join(root, name)


def build_index(repo_dir=REPO_DIR, index_path=INDEX_PATH, dvd_only=True):
    """Condense the repository into the few fields a lookup needs.

    The checkout is ~290 MB of JSON, most of it per-title audio and subtitle
    track listings. The index is a fraction of that and is what ships around.
    """
    discs, seen_movies = [], {}
    scanned = kept = 0

    for path in _iter_disc_files(repo_dir):
        scanned += 1
        try:
            with open(path, encoding="utf-8") as fh:
                disc = json.load(fh)
        except (OSError, ValueError):
            continue

        fmt = (disc.get("Format") or "").upper()
        if dvd_only and fmt != "DVD":
            continue

        release_dir = os.path.dirname(path)
        movie_dir = os.path.dirname(release_dir)
        if movie_dir not in seen_movies:
            meta = {}
            try:
                with open(os.path.join(movie_dir, "metadata.json"),
                          encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                pass
            seen_movies[movie_dir] = {
                "title": meta.get("Title") or os.path.basename(movie_dir),
                "year": meta.get("Year"),
                "tmdb": (meta.get("ExternalIds") or {}).get("Tmdb"),
                "slug": meta.get("Slug"),
                "kind": (meta.get("Type") or "Movie"),
            }
        movie = seen_movies[movie_dir]

        titles = []
        for t in disc.get("Titles") or []:
            item = t.get("Item") or {}
            name = (item.get("Title") or "").strip()
            if not name:
                continue                    # unnamed title tells us nothing
            titles.append({
                "src": str(t.get("SourceFile") or ""),
                "s": parse_duration(t.get("Duration")),
                "n": name,
                "t": item.get("Type") or "Extra",
            })
        if not titles:
            continue

        kept += 1
        discs.append({
            "h": (disc.get("ContentHash") or "").upper(),
            "movie": movie["title"], "year": movie["year"],
            "tmdb": movie["tmdb"], "kind": movie["kind"],
            "release": os.path.basename(release_dir),
            "disc": disc.get("Index"), "fmt": fmt,
            "titles": titles,
        })

    index = {"built": int(time.time()), "count": len(discs),
             "dvd_only": dvd_only, "discs": discs}
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    tmp = index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, index_path)
    try:
        os.chown(index_path, OWNER_UID, OWNER_GID)
    except (PermissionError, OSError):
        pass
    log("indexed %d discs with named titles (from %d disc files) -> %s"
        % (kept, scanned, index_path))
    return index


def sync(rebuild_only=False):
    """Fetch or refresh the catalogue, then rebuild the index."""
    if not rebuild_only:
        os.makedirs(DISCDB_DIR, exist_ok=True)
        if os.path.isdir(os.path.join(REPO_DIR, ".git")):
            log("updating TheDiscDb checkout ...")
            _git(["fetch", "--depth", "1", "origin"], cwd=REPO_DIR)
            _git(["checkout", "-f", "FETCH_HEAD"], cwd=REPO_DIR)
        else:
            log("fetching TheDiscDb (blobless sparse clone, ~290 MB) ...")
            _git(["clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
                  REPO_URL, REPO_DIR])
            _git(["sparse-checkout", "init", "--no-cone"], cwd=REPO_DIR)
            _git(["sparse-checkout", "set", "--no-cone"] + SPARSE_PATTERNS,
                 cwd=REPO_DIR)
            _git(["checkout"], cwd=REPO_DIR)
    return build_index()


def _git(args, cwd=None):
    proc = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, timeout=1800)
    if proc.returncode != 0:
        raise DiscDbError("git %s failed: %s"
                          % (" ".join(args[:2]), (proc.stderr or "")[-300:]))
    return proc.stdout


_index_cache = {"mtime": None, "data": None}


def load_index(index_path=INDEX_PATH):
    try:
        mtime = os.path.getmtime(index_path)
    except OSError:
        return None
    if _index_cache["mtime"] == mtime:
        return _index_cache["data"]
    try:
        with open(index_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    _index_cache.update(mtime=mtime, data=data)
    return data


# --------------------------------------------------------------------- lookup

def _fingerprint(seconds_list, tol=2):
    return sorted(int(round(s / float(tol))) for s in seconds_list if s)


def match_disc(iso_path=None, durations=None, index=None):
    """Identify a disc. Returns (entry, how, confidence) or (None, reason, 0).

    `how` is "contenthash" for an exact filesystem-level identification, or
    "duration" for the fallback, which is a guess and labelled as one.
    """
    index = index or load_index()
    if not index:
        return None, "no local index — run: discdb.py sync", 0.0
    discs = index.get("discs") or []

    if iso_path:
        try:
            h, _files = content_hash(iso_path)
        except DiscDbError:
            h = None
        if h:
            for entry in discs:
                if entry.get("h") == h:
                    return entry, "contenthash", 1.0

    if not durations:
        return None, "not in TheDiscDb", 0.0

    # Fallback: same set of runtimes. Two different pressings of the same film
    # usually differ somewhere in their extras, so this is decent evidence -
    # but it is evidence, not identity, and the caller must not treat it as
    # settled without a human looking.
    want = set(_fingerprint(durations))
    if not want:
        return None, "not in TheDiscDb", 0.0
    best, best_score = None, 0.0
    for entry in discs:
        have = set(_fingerprint([t["s"] for t in entry["titles"]]))
        if not have:
            continue
        overlap = len(want & have)
        score = overlap / float(max(len(want), len(have)))
        if score > best_score:
            best, best_score = entry, score
    if best and best_score >= 0.6:
        return best, "duration", round(best_score, 3)
    return None, "not in TheDiscDb", 0.0


def name_titles(entry, titles, tol=2):
    """Map our scanned titles onto the catalogue's names.

    Matched by runtime rather than title number: our numbering comes from
    lsdvd and theirs from MakeMKV, and the two do not always agree, but a
    runtime is a runtime.
    """
    # Assign globally best-fit first rather than walking the titles in order.
    # Discs are full of near-duplicate runtimes - a 60s menu loop sits right
    # next to a 61s featurette - and first-come matching lets whichever title
    # happens to be scanned first take a name that belongs to the other.
    pairs = []
    for t in titles:
        secs = t["seconds"] if isinstance(t, dict) else t[1]
        ix = t["ix"] if isinstance(t, dict) else t[0]
        for ci, cand in enumerate(entry["titles"]):
            delta = abs(cand["s"] - secs)
            if delta <= tol:
                pairs.append((delta, ix, ci))
    pairs.sort(key=lambda p: (p[0], p[1]))

    out, used_titles, used_cands = {}, set(), set()
    for _delta, ix, ci in pairs:
        if ix in used_titles or ci in used_cands:
            continue
        used_titles.add(ix)
        used_cands.add(ci)
        cand = entry["titles"][ci]
        out[ix] = {
            "name": cand["n"],
            "type": cand["t"],
            "subdir": TYPE_TO_PLEX.get(cand["t"], "Featurettes"),
            "is_feature": cand["t"] == "MainMovie",
        }
    return out


# ---------------------------------------------------------------- contribution

def slugify(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "unknown"


def safe_component(name):
    """A filename a reviewer could actually use, without path separators."""
    return re.sub(r'[/\\\\:*?"<>|]+', " ", str(name or "")).strip() or "Untitled"


def seconds_to_duration(secs):
    secs = int(round(secs))
    return "%d:%02d:%02d" % (secs // 3600, (secs % 3600) // 60, secs % 60)


def export_contribution(iso_path, movie, titles, out_dir, release_title=None,
                        release_year=None):
    """Write a TheDiscDb-shaped submission for a disc it does not have.

    Every disc that misses is a gap someone else will hit too. This produces
    the exact folder layout the project's data repository uses, so submitting
    is a fork, a copy and a pull request rather than a transcription job.

    `titles` are the reviewed titles - names supplied by a human, which is the
    whole point; an export of "Featurette 01" would be worse than no export.
    """
    try:
        h, files = content_hash(iso_path)
    except DiscDbError as e:
        raise DiscDbError("cannot export without a content hash: %s" % e)

    movie_dir_name = "%s (%s)" % (movie["title"], movie["year"])
    slug = "%s-%s" % (slugify(movie["title"]), movie["year"])
    release_slug = slugify(release_title or ("%s-dvd" % (release_year or "")))
    if not release_slug.endswith("dvd"):
        release_slug += "-dvd"

    base = os.path.join(out_dir, "data", "movie", movie_dir_name)
    rel_dir = os.path.join(base, release_slug)
    os.makedirs(rel_dir, exist_ok=True)

    metadata = {
        "Title": movie["title"],
        "FullTitle": movie["title"],
        "SortTitle": movie["title"],
        "Slug": slug,
        "Type": "Movie",
        "Year": movie["year"],
        "ExternalIds": {"Tmdb": str(movie["tmdbId"])},
        "Groups": [],
        "DateAdded": time.strftime("%Y-%m-%dT00:00:00+00:00"),
    }
    release = {
        "Slug": release_slug,
        "Locale": "en-us",
        "Title": release_title or ("%s DVD" % (release_year or movie["year"])),
        "SortTitle": release_title or ("%s DVD" % (release_year or movie["year"])),
        "Year": release_year or movie["year"],
        "ReleaseDate": None,
        "DateAdded": time.strftime("%Y-%m-%dT00:00:00+00:00"),
        "Contributors": [],
        "Groups": [],
    }

    disc_titles = []
    for n, t in enumerate(titles):
        item = None
        if t.get("name"):
            item = {"Title": t["name"], "Type": t.get("discdb_type") or "Extra",
                    "Chapters": []}
        disc_titles.append({
            "Index": n,
            "SourceFile": "%02d" % int(t["ix"]),
            "Duration": seconds_to_duration(t["seconds"]),
            "Item": item,
        })

    disc = {
        "Index": 1, "Slug": "dvd", "Name": "DVD", "Format": "DVD",
        "ContentHash": h,
        "Titles": disc_titles,
    }

    def _write(path, payload):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)

    _write(os.path.join(base, "metadata.json"), metadata)
    _write(os.path.join(rel_dir, "release.json"), release)
    _write(os.path.join(rel_dir, "disc01.json"), disc)

    # disc01-summary.txt is what their CI actually validates (housekeeping/
    # appliances/check-summaries.ts): chunks separated by blank lines, Name and
    # Type mandatory, Type drawn from a fixed list, numeric fields integers, and
    # "File name" last in its chunk. Segment map and Comment are MakeMKV
    # artefacts we cannot produce and the validator treats as optional.
    with open(os.path.join(rel_dir, "disc01-summary.txt"), "w",
              encoding="utf-8") as fh:
        for n, t in enumerate(titles):
            if not t.get("name"):
                continue
            if n:
                fh.write("\n")
            fh.write("Name: %s\n" % t["name"])
            fh.write("Type: %s\n" % (t.get("discdb_type") or "Extra"))
            if t.get("discdb_type") == "MainMovie" and movie.get("year"):
                fh.write("Year: %s\n" % movie["year"])
            fh.write("Source title ID: %02d\n" % int(t["ix"]))
            fh.write("Duration: %s\n" % seconds_to_duration(t["seconds"]))
            if t.get("chapters"):
                fh.write("Chapters count: %d\n" % int(t["chapters"]))
            # Must be last in the chunk; the validator checks exactly that.
            fh.write("File name: %s.mkv\n" % safe_component(t["name"]))

    # A MakeMKV-shaped log carrying only the HSH lines. Their importer derives
    # ContentHash from exactly these (TheDiscDb.Core HashLogFile), so a reviewer
    # can recompute the hash from the submission instead of taking it on trust.
    with open(os.path.join(rel_dir, "disc01.txt"), "w", encoding="utf-8") as fh:
        for i, (name, size) in enumerate(sorted(files, key=lambda f: f[0])):
            fh.write("HSH:%d,%s,,%d\n" % (i, name, size))

    return {"dir": base, "release_dir": rel_dir, "content_hash": h,
            "titles": len(disc_titles),
            "named": sum(1 for t in disc_titles if t["Item"])}


# ------------------------------------------------------------------------ CLI

def _load_scan(iso_path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, "/opt/isohungry")
    import extras_import as ei
    return ei.lsdvd_titles(iso_path)


def cmd_sync(args):
    sync(rebuild_only=args.rebuild_only)


def cmd_lookup(args):
    iso = os.path.abspath(args.iso)
    titles = _load_scan(iso)
    h, files = content_hash(iso)
    log("content hash: %s  (%d VIDEO_TS files)" % (h, len(files)))

    entry, how, conf = match_disc(iso, [t["seconds"] for t in titles])
    if not entry:
        log("no match: %s" % how)
        log("This disc is a gap in the catalogue — 'discdb.py export' prepares "
            "a submission once the extras have been named.")
        return
    log("matched by %s (confidence %s): %s (%s) — %s disc %s"
        % (how, conf, entry["movie"], entry["year"], entry["release"], entry["disc"]))
    named = name_titles(entry, titles)
    for t in sorted(titles, key=lambda t: t["ix"]):
        info = named.get(t["ix"])
        if not info:
            continue
        log("   title %2s  %8s  %-16s %s"
            % (t["ix"], seconds_to_duration(t["seconds"]),
               info["subdir"] if not info["is_feature"] else "(main feature)",
               info["name"]))


def cmd_export(args):
    iso = os.path.abspath(args.iso)
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    movie = {"title": plan["movie"]["title"], "year": plan["movie"]["year"],
             "tmdbId": plan["movie"]["tmdbId"]}
    titles = [{"ix": t["ix"], "seconds": t["seconds"], "name": t.get("name")}
              for t in plan.get("titles", []) if t.get("include")]
    feat = plan.get("feature_title") or {}
    if feat.get("ix"):
        titles.insert(0, {"ix": feat["ix"],
                          "seconds": feat.get("seconds")
                          or parse_duration(feat.get("duration")),
                          "name": movie["title"], "discdb_type": "MainMovie"})
    result = export_contribution(iso, movie, titles, args.out,
                                 args.release_title, args.release_year)
    log("wrote submission for %s (%s titles, %s named)"
        % (result["content_hash"], result["titles"], result["named"]))
    log("  %s" % result["release_dir"])
    log("")
    log("To contribute: fork github.com/TheDiscDb/data, copy the data/ tree in,")
    log("and open a pull request. Check the names against the disc packaging")
    log("first — a wrong name in a shared catalogue is worse than a gap.")


def main():
    p = argparse.ArgumentParser(description="TheDiscDb lookup for ripped DVDs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="fetch/refresh the catalogue and reindex")
    s.add_argument("--rebuild-only", action="store_true",
                   help="reindex the existing checkout without fetching")
    s.set_defaults(func=cmd_sync)

    l = sub.add_parser("lookup", help="identify a ripped ISO")
    l.add_argument("iso")
    l.set_defaults(func=cmd_lookup)

    e = sub.add_parser("export", help="prepare a contribution for an unknown disc")
    e.add_argument("iso")
    e.add_argument("--plan", required=True, help="reviewed plan JSON with names")
    e.add_argument("--out", default="/output/.discdb/submissions")
    e.add_argument("--release-title")
    e.add_argument("--release-year", type=int)
    e.set_defaults(func=cmd_export)

    args = p.parse_args()
    try:
        args.func(args)
    except DiscDbError as exc:
        print("error: %s" % exc, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
