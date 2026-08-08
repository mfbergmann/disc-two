# Where the names come from

A DVD title is a number and a duration. The names printed on the box exist
nowhere on the disc in machine-readable form, so left alone any ripper can only
call them `Featurette 01`.

Disc Two asks three sources, best first, and shows you which one answered.

---

## 1. TheDiscDb — exact, when it has the disc

[TheDiscDb](https://thediscdb.com) is a community catalogue of disc contents,
MIT-licensed at [github.com/TheDiscDb/data](https://github.com/TheDiscDb/data).
Download it from the **Catalogue** panel or `disc-two sync`. The checkout is
blobless and sparse — around 290 MB rather than the full 2.1 GB — and is
condensed to a small local index.

### Identification is exact, not fuzzy

Every disc is keyed on a `ContentHash`: an MD5 over the sizes of the files in
`VIDEO_TS`, sorted by name.

```
md5( int64le(size) for each file in sorted(VIDEO_TS) )
```

That is the disc's own filesystem talking. Two rips of the same pressing agree;
two different pressings do not. It is reproducible from an ISO with no disc in
the drive.

Before this was relied on it was checked twice. Against their data: every
catalogued disc that ships its file listing was recomputed, and **3639 of 3644
reproduced exactly, including 378 of 378 DVDs** — the five misses are all
Blu-ray/UHD, and two are Game of Thrones discs 29 and 30 whose stored hashes are
swapped, a data-entry slip rather than an algorithm one. Then against a physical
disc: the same disc hashed as the pressing in the drive and as the ISO ripped
from it produced the same value, confirming that `dvdbackup` and `genisoimage`
preserve file sizes and a rip hashes as its pressing does.

The corollary matters when a lookup misses. **A hash that does not match means a
different pressing, not a broken hash.** A Requiem for a Dream special edition
missed its catalogued entry, and the entry's own data agreed: 6054 seconds of
feature against 6064, four trailers against eleven extras. Different disc,
correctly refused.

A duration-fingerprint fallback exists for discs whose files were altered in
transit. It is labelled a guess in the UI, because it is one.

### What a hit brings

Names, and **categories**. TheDiscDb records a type per title, which maps onto
the folders Plex recognises, so deleted scenes land in `Deleted Scenes/` and
trailers in `Trailers/` rather than everything being swept into `Featurettes/`.
A hit also identifies the film outright by its TMDB id, which beats any amount
of string similarity against a squashed volume label.

Names are mapped onto titles by **runtime, assigned globally best-fit first** —
not by title number, since different tools number discs differently, and not in
scan order, since a 60-second menu loop sitting beside a 61-second featurette
will otherwise steal its name.

### Coverage

TheDiscDb is Blu-ray-first. Most DVDs miss, and a miss is not a failure — it is
the case for [contributing the disc back](CONTRIBUTING-DISCS.md).

---

## 2. The disc's own menus

Every disc with extras has a menu naming them — that is what a menu is for.

**Buttons are not in the IFO.** They live in the highlight information (HLI) of
each menu VOBU's navigation pack, and every button carries a screen rectangle
and an 8-byte VM command. That structure is what makes the names usable: it says
which title a label belongs to, and in what order the disc lists them.

Two details cost real debugging time and are worth knowing if you touch this:

- **`btn_ns` is at HL_GI offset 17, not 16.** Offset 16 is `btn_ofn`, the index
  the group starts at. On a paged menu that reads as 4, 8, 12, 16 — a
  plausible-looking button count that silently truncates every menu and hides
  the main menu entirely.
- **Scene-selection pages are dropped before rendering.** They are chapter jumps
  into the feature, they name chapters rather than extras, and a paged one
  carries fifteen buttons — the most expensive thing on a disc to read for no
  benefit.

### Extras are told from setup menus by register

An extras menu rarely jumps straight to a title. A button stashes *which* extra
was chosen in a general-purpose register and links to a dispatcher that reads
it. That register is what separates content from furniture: a disc uses one
register for its extras and different ones for audio, subtitle and setup.

On one disc: register 2 was the language menu, register 10 audio, and
**register 7 the special features**, carrying values 11, 12, 21–24, 31–33,
51–54. Those values are the disc's own index for each extra, so sorting by them
gives the disc's intended order rather than the order its menus were scanned in.

This matters more than it sounds. Setup menus are the worst possible
contamination: their options — *YES*, *STOP*, *Spanish* — are short confident
words that survive OCR beautifully and shove the real names out of order.

---

## 3. A photo of the case back

Printed card is flat, high-contrast, and lit however you like — close to the
best case for reading text, where menu video is close to the worst. Photograph
the case from your phone in the review panel.

What the cover **cannot** do is say which title each feature is. It has no
durations and no guaranteed order. So it is one half of a pair: the menu knows
how many extras exist and which title each plays, the cover knows how they are
spelled.

They also disagree usefully. On one disc the cover listed 7 features and the
menus found 9; the menus had three infomercial extras the back cover never
mentions, and the cover had an interview the menus buried in a submenu. Six were
confirmed by both, which is a real confidence signal since they were read from
different media by different means.

---

## Read images with a vision model

Sources 2 and 3 are pictures. Tesseract recognises characters and nothing else,
so everything around it — finding the heading, spotting bullets, joining wrapped
lines, telling a featurette from *5.1 Dolby Digital Audio* — has to be written
as rules, and those rules break on the next disc laid out differently.

Point `OLLAMA_URL` at any [Ollama](https://ollama.com) with a vision model and
it does recognition and understanding in one step. Measured on the same disc:

| | tesseract, tuned | vision model |
|---|---|---|
| Case back | 5 of 7 features | **7 of 7** |
| Disc menu | 2 of 4 labels clean | **4 of 4 exact** |

Model choice, measured on the same cover photo:

| | `ministral-3:3b` | `qwen3-vl:8b` |
|---|---|---|
| Real features | 7 of 7 | 7 of 7 |
| False positives | *16:9 Widescreen Version* | none |
| Difficult name | Ellen Burst**i**n | Ellen Burst**y**n |
| Cold load | 73 s | 292 s |
| VRAM | 3.5 GB | 11.1 GB |

`qwen3-vl:8b` is the better reader. Note the VRAM: on a 12 GB card it leaves
nothing for GPU encoding, so do not run an import and a cold model load at once.
`VISION_PRELOAD=1` warms the model while you are reviewing rather than at the
moment you need it.

Tesseract remains the fallback, because it needs nothing running. It is a
fallback, not a peer.

### The structure still decides what is real

A vision model reads a cast list beautifully. A cast list must never be
imported. On the first full-disc run one scan scraped a **filmography** — twelve
films — and offered them as extras.

Nothing about the words distinguishes that from a real menu. The button table
does: twelve labels behind one button is a text screen, not a set of playable
items, and a label with no button has no title to play however perfect the name.

**The model supplies names; the disc's structure vetoes them.** Neither half is
safe alone.

---

## Television box sets

A film disc has one title far longer than the rest. An episode disc does not:
it has several of much the same length, and often a "play all" chain longer
than any of them. Run the film classifier over that and it calls the play-all
chain the feature and the episodes extras — wrong in every particular.

Detection therefore looks at the **shape** of the runtimes rather than the
longest one: a cluster of similar-length titles between 15 and 70 minutes, with
the longest title either inside that cluster or roughly the sum of it. A
play-all chain is evidence *for* a box set, never against it.

Measured over every DVD in TheDiscDb with a disc listing — 258 episode discs and
115 film discs — that finds **229 of 258 box sets while correctly rejecting 110
of 115 films**. Two episodes is enough to call it: a season opener that runs
feature-length leaves only two ordinary episodes beside it, which is a common
shape rather than a rare one, and requiring three missed fourteen real box sets.

Detection is a suggestion. The review screen says why it decided what it did and
offers *"It's a film, not a box set"* if it got the shape wrong.

### Which episode is which

A catalogue hit answers outright: TheDiscDb records a season and episode number
per title. Without one, discs run in order, and the only unknown is where this
disc starts in the season — one number a human supplies, rather than a mapping
they have to build.

Episode titles come from **Sonarr**, not from the disc. Sonarr's title is the
one the library already uses, so the file matches its siblings. Files are named
`Show - S02E01 - Title.mkv` in `Season 02/`, which Sonarr's scanner parses and
then renames to whatever format you have configured.
