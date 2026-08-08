# Contributing a disc to TheDiscDb

Most DVDs are not in [TheDiscDb](https://github.com/TheDiscDb/data) — it is
Blu-ray-first. Every disc you name is a gap someone else will hit too.

When a disc is not in the catalogue, the review panel offers **Contribute to
TheDiscDb**. It writes a submission in the project's own layout under
`$STATE_DIR/submissions/`:

```
Bend It Like Beckham (2002)/
├── metadata.json
└── 2003-dvd/
    ├── release.json
    ├── disc01.json          <- ContentHash plus named, typed titles
    ├── disc01-summary.txt   <- the file their CI validates
    └── disc01.txt           <- HSH lines, so the hash can be recomputed
```

## Why those files

The layout is shaped by what their CI actually checks
(`housekeeping/appliances/check-summaries.ts`):

- chunks separated by blank lines
- `Name` and `Type` mandatory
- `Type` from their fixed list — MainMovie, Episode, Extra, Trailer,
  DeletedScene, Featurette, Interview, Scene, Music, Short, Other
- numeric fields as integers
- **`File name` last in its chunk** — the validator checks that exact position

`Comment` and `Segment map` are MakeMKV artefacts Disc Two cannot produce, and
the validator treats them as optional.

`disc01.txt` carries `HSH:` lines in MakeMKV log format because that is what
their importer reads to derive `ContentHash`. A reviewer can therefore recompute
the hash from the submission rather than trusting the number in the JSON.

## Submitting

Contributions are merged pull requests, credited to the contributor in the
commit message. Fork [TheDiscDb/data](https://github.com/TheDiscDb/data), copy
the `data/` tree in, and open a pull request.

## Check the names first

The export refuses to run while extras are still called *Featurette 01* — the
value being contributed is the names.

Check them against the disc packaging before submitting. A wrong name in a
shared catalogue is worse than a gap: a gap is obvious, and a confident error
propagates to everyone who ripped the same pressing.
