# Encoding

## The GPU is not the bottleneck, the deinterlacer is

Measured on one NTSC DVD title, RTX 3060, `nvenc_h265`:

| Filters | Speed | 112 min feature |
|---|---|---|
| `--comb-detect --decomb` | 15.2 fps | ~3.7 h |
| `--decomb` alone | 18.0 fps | ~3.1 h |
| `--comb-detect` alone | 22.7 fps | ~2.5 h |
| **`--detelecine --deinterlace`** (default) | **39.5 fps** | **~85 min** |
| `--detelecine` alone | 55.8 fps | ~60 min |
| no filters | 94.7 fps | ~35 min |

`nvidia-smi` shows the encoder at 0–2% throughout, which looks like the GPU is
idle. It is — it is just never the constraint. Debian's HandBrake reports
`nvdec: is not compiled into this build`, so MPEG-2 decode and every filter run
on the CPU and only the final encode is offloaded.

Compiling NVDEC in would not help either: HandBrake disables hardware decoding
[whenever any video filter is enabled](https://handbrake.fr/docs/en/latest/technical/video-nvenc.html),
and every DVD needs one. A bigger card changes nothing here.

## Why detelecine rather than decomb

A film shot at 24 fps and pressed to an NTSC DVD carries 3:2 pulldown.
`--detelecine` inverts that exactly, restoring true 23.976p — cheaper *and* more
faithful than `--decomb`, which only smooths the combing it detects.
`--deinterlace` (yadif) then covers extras shot on video, which telecine does
not describe.

Set `EXTRAS_FILTERS="--detelecine"` for a disc you know is entirely film, or
`"--comb-detect --decomb"` to trade 2.6× the time for HandBrake's most thorough
adaptive handling.

## A failed encode is not always a failure

HandBrake reports failure for a damaged source even when it has already written
essentially the whole title. One disc has a title whose last pack is malformed:
HandBrake exits 5, and the file it produced is 373.6s of an expected 375s and
decodes end to end without a single decoder error.

Non-zero exit therefore prompts a look at the output rather than a verdict. A
file that runs to at least 95% of the title's known length is kept, and the log
says so. Genuinely truncated output still fails.

Damaged discs are exactly the ones worth rescuing — they are why someone is
ripping before things get worse.
