# Subtitle Processing Configuration

This document covers subtitle processing and formatting options under the top-level `subtitle:` key in `envied.yaml`.

For the canonical example, see `envied/envied-example.yaml`.

## subtitle (dict)

Control subtitle conversion, SDH (hearing-impaired) stripping, formatting preservation, and output behavior.

- `conversion_method`: How to convert subtitles between formats. Default: `auto`.
  - `auto`: Smart routing - subby for WebVTT/fVTT/SAMI; for SSA/ASS/MicroDVD/MPL2/TMP use SubtitleEdit when available, otherwise pysubs2; standard pycaption/SubtitleEdit pipeline for everything else.
  - `subby`: Always use subby with `CommonIssuesFixer` (falls back to standard if the source codec isn't supported by subby).
  - `subtitleedit`: Prefer SubtitleEdit when available; otherwise fall back to the standard pycaption pipeline.
  - `pycaption`: Use only the pycaption library (no SubtitleEdit, no subby). Limited to SRT, TTML, and WebVTT outputs.
  - `pysubs2`: Use pysubs2 (supports SRT, SSA, ASS, WebVTT, TTML, SAMI, MicroDVD, MPL2, TMP).

- `sdh_method`: How to strip SDH cues. Default: `auto`.
  - `auto`: Try subby for SRT first, then SubtitleEdit (when `conversion_method` is `auto`/`subtitleedit` and the binary is available), then subtitle-filter as the final fallback.
  - `subby`: Use subby's `SDHStripper`. **Only operates on SRT**; for other codecs the call returns without stripping.
  - `subtitleedit`: Use SubtitleEdit's `/RemoveTextForHI` when the binary is available; otherwise falls through to subtitle-filter.
  - `filter-subs`: Use the `subtitle-filter` library directly (`rm_fonts`, `rm_ast`, `rm_music`, `rm_effects`, `rm_names`, `rm_author`).

- `strip_sdh`: Enable/disable automatic SDH stripping for tracks flagged as SDH. Default: `true`.

- `convert_before_strip`: When falling through to the subtitle-filter path, auto-convert non-SRT subtitles to SRT first for better compatibility. Default: `true`. Has no effect when SubtitleEdit handles stripping directly.

- `preserve_formatting`: Keep original subtitle tags and positioning during WebVTT processing. When `true`, sanitized WebVTT is written back without round-tripping through pycaption, preserving tags like `<i>`, `<b>`, and `line:` positioning. Default: `true`.

- `output_mode`: Controls how subtitles are included in the output. Default: `mux`.
  - `mux`: Embed subtitles in the MKV container only.
  - `sidecar`: Save subtitles as separate files only (not muxed).
  - `both`: Embed in the MKV container and save as sidecar files.

- `sidecar_format`: Format for sidecar subtitle files (used when `output_mode` is `sidecar` or `both`). Default: `srt`.
  - `srt`: SubRip.
  - `vtt`: WebVTT.
  - `ass`: Advanced SubStation Alpha.
  - `original`: Keep the subtitle in its current format without conversion.

Example:

```yaml
subtitle:
  conversion_method: auto
  sdh_method: auto
  strip_sdh: true
  convert_before_strip: true
  preserve_formatting: true
  output_mode: mux
  sidecar_format: srt
```

## WebVTT Sanitization (automatic, not configurable)

After download, WebVTT and segmented WebVTT (`fVTT`/`WVTT`) tracks pass through a fixed sanitization pipeline before any conversion or muxing:

1. **Segment merge** — segmented DASH/HLS WebVTT is stitched via `merge_segmented_webvtt` (uses pysubs2 for lenient parsing when `conversion_method` is `auto` or `pysubs2`, otherwise pycaption directly).
2. **Negative timestamps** — `sanitize_webvtt_timestamps` rewrites `-HH:MM:SS.mmm` cues to `00:00:00.000`.
3. **Cue identifiers** — `sanitize_webvtt_cue_identifiers` strips letter+digit IDs (e.g. `Q0`, `S12`) on their own line before a timing line, which otherwise confuse parsers like pysubs2.
4. **Overlapping cues** — `merge_overlapping_webvtt_cues` collapses cues with start times within 50 ms and matching end times into a single multi-line cue, ordered by `line:` percentage (lower % = higher on screen = first line).
5. **Fallback hardening** — when `preserve_formatting` is `false` and the first pycaption parse fails, `sanitize_webvtt` retries with a `WEBVTT` header guard, hour-padded timings, and another negative-timestamp pass; if that still fails, the sanitized text is written as-is.

`sanitize_broken_webvtt` and `space_webvtt_headers` additionally run inside `Subtitle.parse()` to drop malformed `-->` lines and reflow merged-segment headers. `merge_same_cues` and `filter_unwanted_cues` (drops `&nbsp;`/whitespace-only cues) run only on the pycaption path.

These behaviors are intentional and have no config knobs — they apply to every WebVTT track regardless of `conversion_method`.

## Related

- Filename sanitization (e.g. parenthesis handling, unidecode bracket artifacts from PR #105) lives in `envied/core/utilities.py::sanitize_filename` and is governed by `output_template`, not the `subtitle:` config block.
- Subtitle codec support and the conversion matrix are defined in `envied/core/tracks/subtitle.py`.

---
