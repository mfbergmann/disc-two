#!/usr/bin/env python3
"""
Disc Two on the command line, for when a web UI is the wrong shape.

    disc-two scan   <iso>            what is on this disc, and what is it?
    disc-two names  <iso>            every name source, side by side
    disc-two import <iso> --tmdb-id N   encode the extras into the library
    disc-two sync                    refresh the disc catalogue
"""
import argparse
import json
import os
import sys

from . import disc, discdb, library, menus, vision


def cmd_scan(args):
    titles = disc.lsdvd_titles(args.iso)
    feature, extras, skipped = disc.classify(titles)
    print("%s" % os.path.basename(args.iso))
    print("  feature : title %s, %s" % (feature["ix"], disc.human_duration(feature["seconds"])))
    print("  extras  : %d" % len(extras))
    for t in extras:
        print("     title %2s  %9s  %2s ch" % (t["ix"], disc.human_duration(t["seconds"]), t["chapters"]))
    if skipped:
        print("  skipped : %d" % len(skipped))
        for t in skipped:
            print("     title %2s  %9s  %s" % (t["ix"], disc.human_duration(t["seconds"]), t["reason"]))
    entry, how, conf = discdb.match_disc(args.iso, [t["seconds"] for t in titles])
    print("  catalogue: %s" % (("%s (%s) via %s" % (entry["movie"], entry["year"], how))
                               if entry else how))


def cmd_names(args):
    """Every source at once, so their disagreements are visible."""
    titles = disc.lsdvd_titles(args.iso)
    entry, how, _conf = discdb.match_disc(args.iso, [t["seconds"] for t in titles])
    print("TheDiscDb (%s):" % how)
    if entry:
        for ix, info in sorted(discdb.name_titles(entry, titles).items()):
            print("   title %2s  %-16s %s" % (ix, info["subdir"], info["name"]))
    if args.menus:
        print("\nDisc menus:")
        for n, item in enumerate(menus.menu_names(args.iso), 1):
            print("  %2d. %s" % (n, item["label"]))
    if args.cover:
        from . import covers
        items, err = covers.read_cover(args.cover)
        print("\nBox art (%s):" % (err or "read"))
        for n, i in enumerate(items, 1):
            print("  %2d. %s" % (n, i))


def cmd_import(args):
    titles = disc.lsdvd_titles(args.iso)
    _feature, extras, _skipped = disc.classify(titles)
    rows = [{"ix": t["ix"], "seconds": t["seconds"], "chapters": t["chapters"],
             "include": True, "subdir": args.subdir,
             "name": "Featurette %02d" % n} for n, t in enumerate(extras, 1)]
    job_id, movie, created = library.start_import(
        args.iso, args.tmdb_id, rows, include_feature=args.feature)
    print("importing into %s%s" % (movie["path"], " (added to Radarr)" if created else ""))
    print("job %s — watch it with: disc-two job %s" % (job_id, job_id))


def cmd_job(args):
    print(json.dumps(library.job(args.id), indent=2, default=str))


def cmd_sync(args):
    discdb.sync(rebuild_only=args.rebuild_only)


def cmd_status(args):
    print("catalogue :", library.discdb_status())
    print("vision    : configured=%s available=%s loaded=%s"
          % (bool(vision.OLLAMA_URL and vision.VISION_MODEL),
             vision.available(), vision.loaded()))


def main(argv=None):
    p = argparse.ArgumentParser(prog="disc-two",
                                description="File a DVD's special features into your media library.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="what is on this disc")
    s.add_argument("iso"); s.set_defaults(func=cmd_scan)

    n = sub.add_parser("names", help="every name source, side by side")
    n.add_argument("iso")
    n.add_argument("--menus", action="store_true", help="also read the disc menus (slow)")
    n.add_argument("--cover", help="also read a photo of the case back")
    n.set_defaults(func=cmd_names)

    i = sub.add_parser("import", help="encode the extras into the library")
    i.add_argument("iso")
    i.add_argument("--tmdb-id", type=int, required=True)
    i.add_argument("--subdir", default=os.environ.get("EXTRAS_SUBDIR", "Featurettes"))
    i.add_argument("--feature", action="store_true", help="import the main feature too")
    i.set_defaults(func=cmd_import)

    j = sub.add_parser("job", help="progress of a running import")
    j.add_argument("id"); j.set_defaults(func=cmd_job)

    y = sub.add_parser("sync", help="refresh the disc catalogue")
    y.add_argument("--rebuild-only", action="store_true")
    y.set_defaults(func=cmd_sync)

    t = sub.add_parser("status", help="what is configured and reachable")
    t.set_defaults(func=cmd_status)

    args = p.parse_args(argv)
    try:
        args.func(args)
    except (disc.LsdvdError, discdb.DiscDbError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
