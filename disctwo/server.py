#!/usr/bin/env python3
"""
Disc Two's web UI.

Narrow on purpose. It can read a disc, look a film up, and import extras into a
folder Radarr manages. It cannot delete an ISO, move one, or write anywhere
except a film's own extras folders.
"""
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import library
from .library import ISO_DIR

PORT = int(os.environ.get("WEB_PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")
MAX_JSON = 256 * 1024
MAX_PHOTO = 16 * 1024 * 1024


def list_isos():
    """Every ISO under ISO_DIR, with whatever is known about it."""
    out = []
    for root, _dirs, files in os.walk(ISO_DIR):
        for name in files:
            if not name.lower().endswith(".iso"):
                continue
            path = os.path.join(root, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            review = library.load_review(path)
            movie = review.get("movie") or {}
            out.append({
                "name": name,
                "rel": os.path.relpath(path, ISO_DIR).replace(os.sep, "/"),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
                "review": library.review_status(path),
                "movie": movie.get("title"),
                "year": movie.get("year"),
            })
    out.sort(key=lambda i: i["mtime"], reverse=True)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "DiscTwo"

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _iso(self, rel):
        """Resolve a client path to an ISO inside ISO_DIR, or None."""
        rel = unquote(rel or "").strip().lstrip("/")
        if not rel:
            return None
        root = os.path.realpath(ISO_DIR)
        path = os.path.realpath(os.path.join(root, rel))
        if not path.startswith(root + os.sep):
            return None
        return path if os.path.isfile(path) and path.lower().endswith(".iso") else None

    def handle_one_request(self):
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _guard(self, fn):
        try:
            fn()
        except Exception:                                # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": "something went wrong; see the log"})

    def do_GET(self):
        self._guard(self._get)

    def do_POST(self):
        self._guard(self._post)

    # ------------------------------------------------------------------ GET
    def _get(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB, "index.html"), "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html missing", "text/plain")
            return

        if path == "/api/status":
            self._send(200, {
                "isos": list_isos(),
                "iso_dir": ISO_DIR,
                "discdb": library.discdb_status(),
                "vision": library.vision_status(),
                "radarr": library.radarr_status(),
            })
            return

        if path == "/api/inspect":
            target = self._iso((query.get("rel") or [""])[0])
            if not target:
                return self._send(404, {"error": "no such ISO"})
            try:
                result = library.inspect_iso(target)
            except Exception as e:                       # noqa: BLE001
                msg = str(e) if not isinstance(e, SystemExit) else ""
                return self._send(400, {"error": msg or "could not read the disc"})
            result["rel"] = (query.get("rel") or [""])[0]
            result["saved"] = library.load_review(target)
            return self._send(200, result)

        if path == "/api/search":
            term = (query.get("q") or [""])[0].strip()
            if len(term) < 2:
                return self._send(400, {"error": "search for at least two characters"})
            return self._send(200, {"library": library.search_library(term),
                                    "tmdb": library.search_tmdb(term)})

        if path == "/api/job":
            return self._send(200, library.job((query.get("id") or [""])[0]))

        self._send(404, {"error": "not found"})

    # ----------------------------------------------------------------- POST
    def _post(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)

        # A cover photo is raw image bytes and far larger than the JSON limit,
        # so it is handled before the body is parsed as JSON.
        if path == "/api/cover":
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_PHOTO:
                return self._send(413, {"error": "photo must be under 16 MB"})
            target = self._iso((query.get("rel") or [""])[0])
            if not target:
                return self._send(404, {"error": "no such ISO"})
            items, err = library.read_cover_photo(target, self.rfile.read(length))
            return self._send(200, {"ok": True, "names": items, "error": err})

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            return self._send(413, {"error": "too large"})
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})

        if path == "/api/import":
            target = self._iso(payload.get("rel"))
            if not target:
                return self._send(404, {"error": "no such ISO"})
            try:
                tmdb_id = int(payload.get("tmdbId") or 0)
            except (TypeError, ValueError):
                tmdb_id = 0
            if not tmdb_id:
                return self._send(400, {"error": "pick a film first"})
            extras = _clean_extras(payload.get("extras"))
            include_feature = bool(payload.get("includeFeature"))
            if not include_feature and not any(t["include"] for t in extras):
                return self._send(400, {"error": "nothing selected to import"})
            try:
                job_id, movie, created = library.start_import(
                    target, tmdb_id, extras,
                    include_feature=include_feature,
                    feature_ix=payload.get("featureIx"),
                    feature_seconds=payload.get("featureSeconds"),
                    feature_name=(payload.get("featureName") or "").strip()[:160] or None)
            except (ValueError, SystemExit) as e:
                return self._send(400, {"error": str(e) or "could not start"})
            return self._send(200, {"ok": True, "job": job_id,
                                    "created_movie": created,
                                    "movie": {"title": movie.get("title"),
                                              "year": movie.get("year"),
                                              "path": movie.get("path")}})

        if path == "/api/menu-scan":
            target = self._iso(payload.get("rel"))
            if not target:
                return self._send(404, {"error": "no such ISO"})
            try:
                return self._send(200, {"ok": True,
                                        "job": library.start_menu_scan(target)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        if path == "/api/contribute":
            target = self._iso(payload.get("rel"))
            movie = payload.get("movie") or {}
            if not target:
                return self._send(404, {"error": "no such ISO"})
            if not (movie.get("title") and movie.get("tmdbId")):
                return self._send(400, {"error": "pick the film first"})
            try:
                result = library.contribute(
                    target, movie, _clean_extras(payload.get("extras")),
                    feature_ix=payload.get("featureIx"),
                    feature_seconds=payload.get("featureSeconds"),
                    feature_chapters=payload.get("featureChapters"),
                    release_title=(payload.get("releaseTitle") or "").strip()[:120] or None,
                    release_year=payload.get("releaseYear"))
            except Exception as e:                       # noqa: BLE001
                return self._send(400, {"error": str(e) or "could not prepare"})
            return self._send(200, {"ok": True, **result})

        if path == "/api/skip":
            target = self._iso(payload.get("rel"))
            if not target:
                return self._send(404, {"error": "no such ISO"})
            library.save_review(target, {"status": "skipped"})
            return self._send(200, {"ok": True})

        if path == "/api/discdb-sync":
            return self._send(200, {"ok": True, "job": library.start_discdb_sync()})

        self._send(404, {"error": "not found"})


def _clean_extras(rows):
    out = []
    for t in (rows or [])[:64]:
        try:
            out.append({
                "ix": int(t.get("ix")),
                "seconds": float(t.get("seconds") or 0),
                "chapters": int(t.get("chapters") or 0),
                "name": (t.get("name") or "").strip()[:120] or "Featurette",
                "subdir": (t.get("subdir") or "").strip()[:40],
                "include": bool(t.get("include")),
            })
        except (TypeError, ValueError):
            continue
    return out


def main():
    os.makedirs(library.STATE_DIR, exist_ok=True)
    print("Disc Two on :%d — reading ISOs from %s" % (PORT, ISO_DIR), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
