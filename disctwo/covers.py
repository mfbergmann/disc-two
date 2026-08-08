#!/usr/bin/env python3
"""
Read a DVD case's back cover for the list of special features.

The disc's menus garble under OCR because their text sits on moving video. The
back of the case does not: it is flat, printed, high-contrast, and whoever is
holding it controls the framing and the light. It is close to tesseract's best
case where the menu is close to its worst.

What the cover cannot do is say which title each feature is. It lists them as
marketing copy, with no durations and no guaranteed order. So this is the other
half of a pair: the menu knows how many extras there are and which title each
one plays, the cover knows how they are spelled.

    cover_ocr.py <image>            print the features found on a cover photo
"""

import argparse
import os
import re
import subprocess
import sys
from difflib import SequenceMatcher

try:
    from . import vision
except Exception:                                        # noqa: BLE001
    vision = None
import tempfile

# Where the list starts. Studios are not consistent, so this is generous.
HEADINGS = re.compile(
    r"\b(special\s+features?|bonus\s+(features?|materials?|content)|"
    r"extras?|additional\s+(features?|content)|special\s+bonus|"
    r"disc\s+extras?|features?\s+include)\b", re.I)

# Where it stops: the legal and technical block that follows on every case.
ENDINGS = re.compile(
    r"\b(closed\s+caption|subtitl|aspect\s+ratio|dolby|dts|surround|"
    r"running\s+time|rated\s|not\s+rated|all\s+rights\s+reserved|"
    r"©|\(c\)\s*\d{4}|distributed\s+by|manufactured|warner|paramount|"
    r"universal\s+studios|columbia|region\s+[0-9]|anamorphic|widescreen\s+version|"
    r"languages?:|audio:|video:|presented\s+in)\b", re.I)

# A leading bullet in any of the forms print and OCR produce between them.
# Tesseract renders a round bullet as "e", "o", "0", "@" or "©" more often than
# not, so those count too - but only when what follows starts like a title,
# otherwise a sentence beginning "or ..." would be mistaken for an item.
BULLET = re.compile(r"^\s*[•·▪●∙>»\*\-–—o0e@©]\s*(?=[\"\x27A-Z0-9])")

# Studios pad the list with these; they are not extras anyone wants filed.
# The same list, matched as a prefix rather than the whole entry.
FILLER_PREFIX = re.compile(
    r"^\W*(cast\s+and\s+crew|production\s+notes|talent\s+files|"
    r"scene\s+(selection|access)|interactive\s+menus?|digitally\s+(re)?mastered|"
    r"[\d.:]*\s*(anamorphic\s+)?widescreen|full\s*screen|"
    r"[\d.]+\s*(dolby|dts|surround)|dolby\s+digital|dvd-?rom)\b", re.I)

NOT_A_FEATURE = re.compile(
    r"^\W*(and\s+more|much\s+more|plus\s+more|more!?|"
    r"scene\s+(selection|access)|chapter\s+selection|interactive\s+menus?|"
    r"languages?|subtitles?|audio\s+options?|"
    # Presentation and packaging claims, printed in the same list as the real
    # extras but describing the disc rather than anything with a runtime.
    r"[\d.:]*\s*(anamorphic\s+)?widescreen(\s+version)?|"
    r"full\s*screen(\s+version)?|[\d.]+\s*dolby[\w\s.]*|dolby[\w\s.]*|"
    r"[\d.]+\s*(dts|surround)[\w\s.]*|digitally\s+(re)?mastered|"
    r"cast\s+and\s+crew(\s+information)?|production\s+notes|"
    r"talent\s+files|film\s*maker.?s?\s+notes|weblink|dvd-?rom[\w\s]*)"
    r"\W*$", re.I)


# Features panels are overwhelmingly light text on a strong colour. Greyscale
# alone leaves that text swimming in a mid-grey panel; thresholding hard turns
# it into black on white, which is what tesseract wants. Measured on a real
# cover photo: 4 keyword hits with greyscale, 15 with this.
PREPROCESS = ("scale='min(2000,iw*2)':-1:flags=lanczos,format=gray,"
              "lut=y='if(gt(val,%d),0,255)'")


def ocr_image(path, psm="6", extra_vf=None, threshold=180):
    """OCR an image, thresholded so light-on-colour text survives."""
    with tempfile.TemporaryDirectory(prefix="cover-") as work:
        prepared = os.path.join(work, "p.png")
        vf = extra_vf or (PREPROCESS % threshold)
        proc = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vf", vf, prepared],
            capture_output=True, timeout=420)
        if proc.returncode != 0 or not os.path.exists(prepared):
            prepared = path                      # let tesseract try the original
        out = subprocess.run(["tesseract", prepared, "stdout", "--psm", psm],
                             capture_output=True, timeout=420)
        return out.stdout.decode("utf-8", "replace")


def _clean_item(text):
    text = BULLET.sub("", text)
    text = " ".join(text.split())
    text = text.strip(" .;:*-–—•·")
    # OCR turns a bullet into a leading "e" or "©" surprisingly often.
    text = re.sub(r"^[e©@]\s+(?=[A-Z])", "", text)
    return text


def _is_filler(text):
    """Spec claims and DVD-ROM padding, matched on how the entry opens.

    Requiring a whole-string match let any of these through as soon as OCR
    dragged a few words of the adjacent synopsis column onto the end.
    """
    return bool(NOT_A_FEATURE.match(text) or FILLER_PREFIX.match(text))


def _trim_bleed(text):
    """Drop trailing debris picked up from a neighbouring column.

    Cover art puts a synopsis beside the features panel, and OCR reading in
    rows splices the two. A run of shouting capitals after a normal-looking
    title is that other column, not part of the feature.
    """
    text = re.sub(r"\s+[A-Z]{3,}(\s+[A-Z0-9'\".,-]{2,}){1,}\s*$", "", text)
    # Stray single tokens and symbols left at the end by the same effect.
    text = re.sub(r"(\s+[^\w\s]+)+\s*$", "", text)
    text = re.sub(r"\s+[a-z]{1,3}\s*$", "", text)
    return text.strip(" .,;:-–—")


def _plausible(text):
    if len(text) < 3 or len(text) > 90:
        return False
    letters = sum(c.isalpha() for c in text)
    return letters >= 3 and letters >= len(text) * 0.5


def parse_features(text):
    """Pull the special-features list out of OCRed cover text.

    Two shapes have to work. Most covers bullet the list, in which case the
    bullets are the items. Some run it as prose after the heading, in which
    case the separators are the only structure available.
    """
    lines = [l.rstrip() for l in text.splitlines()]

    start = None
    for i, line in enumerate(lines):
        if HEADINGS.search(line):
            start = i
            break
        # Covers set the heading over two lines as often as one:
        # "SPECIAL" / "FEATURES *". Test the pair before giving up on it.
        if i + 1 < len(lines) and HEADINGS.search(line + " " + lines[i + 1]):
            start = i + 1
            break
    if start is None:
        return [], "no special-features heading found"

    # The heading line itself sometimes carries the first item after a colon.
    items, tail = [], []
    head_rest = re.split(r"[:–\-]", lines[start], 1)
    if len(head_rest) > 1 and len(head_rest[1].strip()) > 3:
        tail.append(head_rest[1])

    for line in lines[start + 1:]:
        # Only an unbulleted line can end the list. The legal and technical
        # block is never bulleted; spec claims inside the list often are.
        if ENDINGS.search(line) and not BULLET.match(line):
            break
        if not line.strip():
            # A blank line after we have items usually means the list ended.
            if items or tail:
                blanks = 1
                continue
        tail.append(line)

    # Group into items: a bulleted line starts one, an unbulleted line under it
    # continues it. Covers wrap long features across two or three lines, and
    # treating each line as its own item loses the tail of every one of them.
    grouped, current = [], None
    for line in tail:
        if not line.strip():
            if current:
                grouped.append(current)
                current = None
            continue
        if BULLET.match(line):
            if current:
                grouped.append(current)
            current = _clean_item(line)
        elif current:
            current = (current + " " + line.strip()).strip()
    if current:
        grouped.append(current)

    if len(grouped) >= 2:
        source = grouped                          # trust the printed bullets
    else:
        # Prose: split on the separators studios use between features.
        joined = " ".join(l.strip() for l in tail if l.strip())
        source = re.split(r"\s*[•·▪●>]\s*|\s\|\s|;\s*", joined)
        if len(source) < 2:
            source = re.split(r",\s+(?=[A-Z0-9])", joined)

    for raw in source:
        item = _trim_bleed(_clean_item(raw))
        if _plausible(item) and not _is_filler(item):
            items.append(item)

    # OCR repeats lines when a photo is skewed; keep first occurrences.
    seen, out = set(), []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out, None if out else "heading found but no features parsed under it"


def _same_item(a, b):
    """Whether two reads are the same feature seen through different noise."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    # A pass that clipped an entry leaves a fragment of one another pass read in
    # full. Comparing a fragment against the whole of the longer entry always
    # scores low, so compare it against the matching head instead - but only
    # when it really is a fragment, or two different commentaries that open
    # with the same six words would collapse into one.
    if len(short) < len(long_) * 0.6:
        return SequenceMatcher(None, short, long_[:len(short)]).ratio() >= 0.85
    return SequenceMatcher(None, na, nb).ratio() >= 0.75


def _norm(text):
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _quality(text):
    """Prefer the cleaner reading of the same feature."""
    words = [w for w in text.split() if len(w) > 1]
    real = sum(1 for w in words if re.fullmatch(r"[A-Za-z][A-Za-z'-]*", w))
    return real * 2 - (len(words) - real)


def read_cover(path):
    """The features list, preferring a vision model when one is reachable.

    Tesseract stays as the fallback because it needs nothing running. It is a
    genuine fallback though, not a peer: on the photo these were developed
    against it found 5 of 7 features to the vision model's 6, with more noise
    in the ones it did find.
    """
    if vision and vision.available():
        try:
            items = vision.read_cover(path)
            if items:
                return [_trim_bleed(_clean_item(i)) for i in items
                        if _plausible(i) and not _is_filler(i)], None
        except Exception:                                # noqa: BLE001
            pass                                         # fall through to OCR
    return _read_cover_tesseract(path)


def _read_cover_tesseract(path):
    """Features list, merged across several readings of the same photo.

    No single pass gets a whole panel: thresholds that recover the bright
    heading can blow out the smaller entries below it, and the page-segmentation
    mode that keeps the features column clean sometimes drops its last lines.
    Taking only the best pass threw away entries that another pass had read
    perfectly well, so the passes are merged and near-duplicates collapsed to
    whichever reading looks cleanest.
    """
    merged, err_seen = [], "could not read the image"
    for psm, threshold in (("4", 180), ("6", 180), ("4", 200), ("6", 150),
                           ("3", 180)):
        text = ocr_image(path, psm=psm, threshold=threshold)
        items, err = parse_features(text)
        if err and not merged:
            err_seen = err
        for item in items:
            for i, existing in enumerate(merged):
                if _same_item(existing, item):
                    if _quality(item) > _quality(existing):
                        merged[i] = item
                    break
            else:
                merged.append(item)
    return merged, (None if merged else err_seen)


def main():
    p = argparse.ArgumentParser(description="Read a DVD back cover for extras.")
    p.add_argument("image")
    p.add_argument("--raw", action="store_true", help="dump the raw OCR text too")
    args = p.parse_args()
    if args.raw:
        print(ocr_image(args.image))
        print("-" * 40)
    items, err = read_cover(args.image)
    if err:
        print("error: %s" % err, file=sys.stderr)
        sys.exit(1)
    for n, item in enumerate(items, 1):
        print("%2d. %s" % (n, item))


if __name__ == "__main__":
    main()
