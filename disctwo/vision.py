#!/usr/bin/env python3
"""
Read an image with a local vision model, when one is available.

Tesseract recognises characters. It does not know what it is looking at, so
everything around it — finding the heading, spotting the bullets, joining
wrapped lines, telling a featurette from "5.1 Dolby Digital Audio" — has to be
written as rules, and those rules break on the next disc that is laid out
differently.

A vision model does the recognition and the understanding in one step. On a
real cover photo, tesseract with tuned thresholds found 5 of 7 features and
needed ~80 lines of parsing to exclude the specification lines; ministral-3:3b
found 6 of 7, excluded the specifications because it was asked to, and took
about 7 seconds warm.

Entirely optional. It talks to an Ollama the user already runs; with no Ollama
reachable, callers fall back to tesseract.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("OLLAMA_URL", "").rstrip("/")  # e.g. http://ollama:11434
VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "")
# Ollama loads the model on the first call, which can take minutes from cold;
# once resident it answers in seconds.
LOAD_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))
# Big phone photos gain nothing and cost tokens. This is enough for print.
MAX_WIDTH = int(os.environ.get("OLLAMA_IMAGE_WIDTH", "1400"))


class VisionError(Exception):
    pass


def available():
    """Whether a usable vision model is reachable right now."""
    if not (OLLAMA_URL and VISION_MODEL):
        return False
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=5) as r:
            tags = json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return False
    names = {m.get("name", "") for m in tags.get("models") or []}
    if VISION_MODEL in names:
        return True
    # Ollama reports "name:tag"; accept a bare name the user configured.
    return any(n.split(":")[0] == VISION_MODEL.split(":")[0] for n in names)


def _downscale(path):
    work = tempfile.mkdtemp(prefix="vision-")
    small = os.path.join(work, "s.jpg")
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", path,
         "-vf", "scale='min(%d,iw)':-1" % MAX_WIDTH, "-q:v", "3", small],
        capture_output=True, timeout=120)
    return small if proc.returncode == 0 and os.path.exists(small) else path


def ask(image_path, prompt, timeout=None):
    """Send one image and one question; return the model's raw reply."""
    if not (OLLAMA_URL and VISION_MODEL):
        raise VisionError("no vision model configured")
    small = _downscale(image_path)
    with open(small, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    payload = json.dumps({
        "model": VISION_MODEL,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        # Deterministic: the same photo should not give different names on a
        # second look.
        "options": {"temperature": 0},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL + "/api/generate", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or LOAD_TIMEOUT) as r:
            return (json.load(r).get("response") or "").strip()
    except urllib.error.URLError as e:
        raise VisionError("vision model unreachable: %s" % e)
    except (ValueError, OSError) as e:
        raise VisionError("vision model failed: %s" % e)


def ask_json_list(image_path, prompt, timeout=None):
    """Ask for a JSON array of strings and get one, whatever it wraps it in."""
    reply = ask(image_path, prompt, timeout=timeout)
    # Models fence their JSON, prefix it, or explain it first.
    fenced = re.search(r"```(?:json)?\s*(.+?)```", reply, re.S)
    body = fenced.group(1) if fenced else reply
    match = re.search(r"\[.*\]", body, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(0))
    except ValueError:
        return []
    return [str(i).strip() for i in items
            if isinstance(i, (str, int, float)) and str(i).strip()]


COVER_PROMPT = (
    "This is a photo of the back of a DVD case. List ONLY the video special "
    "features / bonus features it advertises — the things that play as video. "
    "Exclude audio and video specifications (widescreen, aspect ratio, Dolby, "
    "DTS, surround, digitally mastered), and exclude DVD-ROM and navigation "
    "items (scene access, scene selection, interactive menus, cast and crew "
    "information, production notes, talent files). Copy each title exactly as "
    "printed. Reply with a JSON array of strings and nothing else."
)


def read_cover(image_path, timeout=None):
    """The special-features list from a photo of a case back."""
    return ask_json_list(image_path, COVER_PROMPT, timeout=timeout)


MENU_PROMPT = (
    "This is a DVD menu screen. List ONLY the selectable menu items that play "
    "video content. Exclude navigation buttons (Main Menu, Back, More, Next, "
    "Previous, Resume, Play, Setup, Languages, Scene Selection). Copy each "
    "label exactly as shown, in the order they appear top to bottom. "
    "Reply with a JSON array of strings and nothing else."
)


def read_menu(image_path, timeout=None):
    """The playable items on a DVD menu screen, in reading order."""
    return ask_json_list(image_path, MENU_PROMPT, timeout=timeout)


# How long Ollama should hold the model after a preload. A rip runs twenty
# minutes or more and the review follows it, so the useful window is the whole
# of that: loading on demand at review time costs minutes of cold start on a
# screen someone is sitting in front of.
KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "2h")


def loaded():
    """Whether the model is already resident in VRAM."""
    if not (OLLAMA_URL and VISION_MODEL):
        return False
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/ps", timeout=5) as r:
            running = json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return False
    return any(m.get("name") == VISION_MODEL
               for m in running.get("models") or [])


def preload(timeout=900):
    """Warm the model, quietly, without generating anything.

    Ollama loads a model on first use, which is minutes for an 8B. Doing that
    while a disc is ripping means it is already resident by the time anyone
    looks at the review screen. Never fatal: if Ollama is down or the model is
    missing, cover and menu reading fall back to tesseract as they always did.
    """
    if os.environ.get("VISION_PRELOAD", "1") != "1":
        return False, "preload disabled"
    if not (OLLAMA_URL and VISION_MODEL):
        return False, "no vision model configured"
    if loaded():
        return True, "already loaded"
    if not available():
        return False, "ollama unreachable or model not pulled"
    payload = json.dumps({"model": VISION_MODEL, "keep_alive": KEEP_ALIVE}).encode()
    req = urllib.request.Request(
        OLLAMA_URL + "/api/generate", data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, "loaded %s (held for %s)" % (VISION_MODEL, KEEP_ALIVE)
    except (urllib.error.URLError, OSError) as e:
        return False, "could not load: %s" % e


if __name__ == "__main__":
    import sys as _sys
    if "--preload" in _sys.argv:
        ok, msg = preload()
        print(msg)
        _sys.exit(0 if ok else 1)
    if "--status" in _sys.argv:
        print("configured: %s" % bool(OLLAMA_URL and VISION_MODEL))
        print("model     : %s" % (VISION_MODEL or "-"))
        print("available : %s" % available())
        print("loaded    : %s" % loaded())
