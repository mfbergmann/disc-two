# Disc Two

**Point it at a DVD ISO. It files the special features into your media library, named properly.**

Plex [cannot read an ISO](https://support.plex.tv/articles/200264956-iso-img-and-video-ts-movie-files/) —
not ISO, IMG, VIDEO_TS or BDMV. A shelf of archived discs is invisible to the
library it was archived for, and the featurettes, deleted scenes and
commentaries on them may as well not exist.

Disc Two reads the ISO, works out which titles are extras, finds out what they
are called, and encodes them where Plex looks:

```
Alien³ (1992) {tmdb-8077}/
├── Alien³ (1992) {tmdb-8077} Bluray-1080p.mkv    <- your existing copy, untouched
├── Featurettes/
│   ├── Optical Fury.mkv
│   └── Tales of the Wooden Planet.mkv
└── Deleted Scenes/
    └── Outtakes.mkv
```

TV box sets work the same way, into `Season 02/` via Sonarr.

The ISO is never modified, moved or deleted.

## It complements your ripper, it doesn't replace it

Disc Two takes an ISO that already exists. Whatever made it —
[ISOHungry](https://github.com/JamesDavid/ISOHungry),
[ARM](https://github.com/automatic-ripping-machine/automatic-ripping-machine),
MakeMKV, `dd` — is none of its business.

Ripping a disc is solved several times over. Knowing that title 22 is
*"Who Wants to Cook Aloo Gobi?"* and belongs in `Featurettes/` is not, and the
answer is the same whichever ripper produced the file.

## Names

A DVD title is a number and a duration. The names printed on the box exist
nowhere on the disc in machine-readable form, so any ripper can only call them
`Featurette 01`. Disc Two asks three sources, best first:

1. **[TheDiscDb](https://github.com/TheDiscDb/data)** — a community catalogue.
   Identification is exact: discs are keyed on a hash of their `VIDEO_TS` file
   sizes, so a hit is the same pressing, not a similar title. Brings names *and*
   categories, so deleted scenes land in `Deleted Scenes/`.
2. **The disc's own menus** — every disc with extras has a menu naming them.
   Button rectangles and commands come from each menu's navigation pack, which
   is what says *which title* a name belongs to.
3. **A photo of the case back** — printed card reads better than menu video.
   Take it from your phone in the review screen.

Point `OLLAMA_URL` at an [Ollama](https://ollama.com) with a vision model and
images are read by that instead of tesseract. On one disc: 7 of 7 features off
the case against 5, and 4 of 4 menu labels against 2. Tesseract is the fallback
and needs nothing running.

Details, including which model to use: [docs/NAMING.md](docs/NAMING.md).

## Nothing is written without confirmation

Every disc stops at a review screen: this is the film, these are its extras,
these are their names. Matching refuses rather than guesses — "matrix" and
"master" score 0.67 against each other, close enough to file a disc under the
wrong film, so a match needs a high score *and* a distinctive word in common.

If the film isn't in your library, Disc Two adds it to Radarr first so Radarr
computes the folder name, then encodes the main title in alongside the extras.

## Quick start

```bash
git clone https://github.com/mfbergmann/disc-two
cd disc-two
cp .env.example .env      # then edit it
docker compose up -d
```

Open `http://localhost:8472`.

**Mount your library the same way your *arr mounts it.** If Radarr sees
`/data/media/movies`, Disc Two must too — then the paths Radarr returns are
valid as-is, with no rewriting to drift out of sync. This is the one setting
that will waste your afternoon if you get it wrong.

Everything except the ISOs is optional. No Radarr: you pick the folder. No
Plex: no rescan. No Ollama: tesseract reads the images. No Sonarr: films only.

## Encoding

Extras are encoded to MKV with HandBrake. Defaults are fine; two things are
worth knowing if an import feels slow, both in [docs/ENCODING.md](docs/ENCODING.md):

- **The deinterlacer costs more than the encoder.** The default is 2.6× faster
  than HandBrake's thorough setting *and* more faithful on film-sourced DVDs.
- **A GPU helps less than you'd expect**, because decode and filtering stay on
  the CPU. It is supported and optional.

## Command line

```bash
disc-two scan   /isos/SOME_DISC.iso        # what's on it, and what is it
disc-two names  /isos/SOME_DISC.iso --menus --cover back.jpg
disc-two import /isos/SOME_DISC.iso --tmdb-id 455
disc-two sync                              # refresh the catalogue
```

## Docs

- [Naming](docs/NAMING.md) — the three sources, TV box sets, and how they disagree
- [Encoding](docs/ENCODING.md) — filters, GPUs, and damaged discs
- [Contributing discs](docs/CONTRIBUTING-DISCS.md) — filling gaps in TheDiscDb

## Licence

MIT. Grew out of a fork of [ISOHungry](https://github.com/JamesDavid/ISOHungry);
shares no code with it.
