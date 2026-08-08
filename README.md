# Disc Two

**Point it at a DVD ISO. It files the special features into your media library, named properly.**

Plex [cannot read an ISO](https://support.plex.tv/articles/200264956-iso-img-and-video-ts-movie-files/) —
not ISO, IMG, VIDEO_TS or BDMV. So a shelf of archived discs is invisible to the
library it was archived for, and the featurettes, deleted scenes and commentaries
on them may as well not exist.

Disc Two reads the ISO, works out which titles are extras, finds out what they
are actually called, and writes them where Plex looks:

```
Alien³ (1992) {tmdb-8077}/
├── Alien³ (1992) {tmdb-8077} Bluray-1080p.mkv    <- your existing copy, untouched
├── Featurettes/
│   ├── Optical Fury.mkv
│   └── Tales of the Wooden Planet.mkv
└── Deleted Scenes/
    └── Outtakes.mkv
```

The ISO is never modified, moved or deleted.

## It does not rip

Disc Two takes an ISO that already exists. Whatever produced it —
[ISOHungry](https://github.com/JamesDavid/ISOHungry),
[ARM](https://github.com/automatic-ripping-machine/automatic-ripping-machine),
MakeMKV, `dd` — is none of its business.

That is deliberate. Ripping a disc is solved several times over; knowing that
title 22 is *"Who Wants to Cook Aloo Gobi?"* and belongs in `Featurettes/` is
not, and the answer is the same whichever ripper you use.

## The hard part is names

A DVD title is a number and a duration. The names printed on the box exist
nowhere on the disc in machine-readable form, so a ripper can only ever call
them `Featurette 01`. Disc Two asks three sources, best first.

**1. TheDiscDb** — a [community catalogue](https://github.com/TheDiscDb/data) of
disc contents. Identification is *exact*, not fuzzy: every disc is keyed on an
MD5 over the sizes of its `VIDEO_TS` files, which is reproducible from an ISO
with no disc in the drive.

```
md5( int64le(size) for each file in sorted(VIDEO_TS) )
```

Verified against every catalogued disc that ships its file listing — 3639 of
3644 reproduced exactly, including 378 of 378 DVDs — and then against a real
disc, hashing the pressing in the drive and the ISO ripped from it to the same
value. A hit brings names *and* categories, so deleted scenes land in
`Deleted Scenes/` rather than everything being swept into `Featurettes/`.

Coverage is the limit: it is Blu-ray-first, with a few hundred DVDs. Most discs
miss — and a miss is the case for [contributing the disc back](docs/CONTRIBUTING-DISCS.md),
which Disc Two prepares for you in the project's own format.

**2. The disc's own menus** — every disc with extras has a menu naming them.
Buttons are not in the IFO; they live in the highlight information of each menu
VOBU's navigation pack, each carrying a screen rectangle and a VM command. That
structure is what makes the names usable: it says which title a name belongs to,
and which "menu" is really a filmography screen that must not be imported.

**3. A photo of the case back** — printed card is flat, high-contrast and
lit however you like, which is close to the best case for reading text. Take
the photo from your phone; the features list is extracted from it.

## Read it with a vision model, not OCR

Sources 2 and 3 are images. Tesseract recognises characters and nothing else,
so everything around it — finding the heading, spotting bullets, joining wrapped
lines, telling a featurette from *5.1 Dolby Digital Audio* — has to be written
as rules, and those rules break on the next disc laid out differently.

Point `OLLAMA_URL` at any [Ollama](https://ollama.com) with a vision model and
it does recognition and understanding in one step. Measured on the same disc:

| | tesseract, tuned | vision model |
|---|---|---|
| Case back | 5 of 7 features | **7 of 7** |
| Disc menu | 2 of 4 labels clean | **4 of 4 exact** |

Tesseract remains the fallback, because it needs nothing running. It is a
fallback, not a peer.

**The disc's structure still decides what is real.** A vision model reads a
cast list beautifully, and a cast list must never be imported. Twelve labels
behind one button is a text screen, not a menu — and no amount of reading the
words tells you that. Names come from the model; the button table vetoes them.

## Nothing is written without confirmation

Every disc stops at a review screen: this is the film, these are its extras,
these are their names. Matching *refuses* rather than guesses — similarity alone
puts "matrix" and "master" at 0.67, which is close enough to file a disc under
the wrong movie, so a match needs a high score *and* a whole word in common.

If the film is not in your library at all, Disc Two adds it to Radarr first, so
**Radarr** computes the folder name rather than this code imitating it, then
encodes the main title in alongside the extras. Radarr monitors it from then on
and upgrades it when a better release appears.

## Quick start

```bash
git clone https://github.com/mfbergmann/disc-two
cd disc-two
cp .env.example .env      # then edit it
docker compose up -d
```

Open `http://localhost:8080`.

**One thing matters more than the rest:** mount your library the *same way your
Radarr mounts it*. If Radarr sees `/data/media/movies`, Disc Two must too — then
the paths Radarr returns over its API are valid as-is, with no rewriting to
drift out of sync.

Everything except the ISOs is optional. No Radarr: you pick the folder. No Plex:
no rescan is requested. No Ollama: tesseract reads the images.

## Command line

```bash
disc-two scan   /isos/SOME_DISC.iso        # what is on it, and what is it
disc-two names  /isos/SOME_DISC.iso --menus --cover back.jpg
disc-two import /isos/SOME_DISC.iso --tmdb-id 455
disc-two sync                              # refresh the catalogue
```

## Documentation

- [Encoding](docs/ENCODING.md) — why the GPU looks idle, and why the deinterlacer matters more than the encoder
- [Naming](docs/NAMING.md) — the three sources in detail, and how they disagree
- [Contributing discs](docs/CONTRIBUTING-DISCS.md) — filling gaps in TheDiscDb

## Licence

MIT. See [LICENSE](LICENSE).

Disc Two grew out of a fork of [ISOHungry](https://github.com/JamesDavid/ISOHungry)
and still pairs well with it, but shares no code with it.
