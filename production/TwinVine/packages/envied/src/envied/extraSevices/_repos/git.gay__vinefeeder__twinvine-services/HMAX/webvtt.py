import re
import sys
import typing
from typing import Optional

import pysubs2
from pycaption import Caption, CaptionList, CaptionNode, CaptionReadError, WebVTTReader, WebVTTWriter

from envied.core.config import config


class CaptionListExt(CaptionList):
    @typing.no_type_check
    def __init__(self, iterable=None, layout_info=None):
        self.first_segment_mpegts = 0
        super().__init__(iterable, layout_info)


class CaptionExt(Caption):
    @typing.no_type_check
    def __init__(self, start, end, nodes, style=None, layout_info=None, segment_index=0, mpegts=0, cue_time=0.0):
        style = style or {}
        self.segment_index: int = segment_index
        self.mpegts: float = mpegts
        self.cue_time: float = cue_time
        super().__init__(start, end, nodes, style, layout_info)


class WebVTTReaderExt(WebVTTReader):
    # HLS extension support <https://datatracker.ietf.org/doc/html/rfc8216#section-3.5>
    RE_TIMESTAMP_MAP = re.compile(r"X-TIMESTAMP-MAP.*")
    RE_MPEGTS = re.compile(r"MPEGTS:(\d+)")
    RE_LOCAL = re.compile(r"LOCAL:((?:(\d{1,}):)?(\d{2}):(\d{2})\.(\d{3}))")

    def _parse(self, lines: list[str]) -> CaptionList:
        captions = CaptionListExt()
        start = None
        end = None
        nodes: list[CaptionNode] = []
        layout_info = None
        found_timing = False
        segment_index = -1
        mpegts = 0
        cue_time = 0.0

        # The first segment MPEGTS is needed to calculate the rest. It is possible that
        # the first segment contains no cue and is ignored by pycaption, this acts as a fallback.
        captions.first_segment_mpegts = 0

        for i, line in enumerate(lines):
            if "-->" in line:
                found_timing = True
                timing_line = i
                last_start_time = captions[-1].start if captions else 0
                try:
                    start, end, layout_info = self._parse_timing_line(line, last_start_time)
                except CaptionReadError as e:
                    new_msg = f"{e.args[0]} (line {timing_line})"
                    tb = sys.exc_info()[2]
                    raise type(e)(new_msg).with_traceback(tb) from None

            elif "" == line:
                if found_timing and nodes:
                    found_timing = False
                    caption = CaptionExt(
                        start,
                        end,
                        nodes,
                        layout_info=layout_info,
                        segment_index=segment_index,
                        mpegts=mpegts,
                        cue_time=cue_time,
                    )
                    captions.append(caption)
                    nodes = []

            elif "WEBVTT" in line:
                # Merged segmented VTT doesn't have index information, track manually.
                segment_index += 1
                mpegts = 0
                cue_time = 0.0
            elif m := self.RE_TIMESTAMP_MAP.match(line):
                if r := self.RE_MPEGTS.search(m.group()):
                    mpegts = int(r.group(1))

                cue_time = self._parse_local(m.group())

                # Early assignment in case the first segment contains no cue.
                if segment_index == 0:
                    captions.first_segment_mpegts = mpegts

            else:
                if found_timing:
                    if nodes:
                        nodes.append(CaptionNode.create_break())
                    nodes.append(CaptionNode.create_text(self._decode(line)))
                else:
                    # it's a comment or some metadata; ignore it
                    pass

        # Add a last caption if there are remaining nodes
        if nodes:
            caption = CaptionExt(start, end, nodes, layout_info=layout_info, segment_index=segment_index, mpegts=mpegts)
            captions.append(caption)

        return captions

    @staticmethod
    def _parse_local(string: str) -> float:
        """
        Parse WebVTT LOCAL time and convert it to seconds.
        """
        m = WebVTTReaderExt.RE_LOCAL.search(string)
        if not m:
            return 0

        parsed = m.groups()
        if not parsed:
            return 0
        hours = int(parsed[1])
        minutes = int(parsed[2])
        seconds = int(parsed[3])
        milliseconds = int(parsed[4])
        return (milliseconds / 1000) + seconds + (minutes * 60) + (hours * 3600)


def _merge_webvtt_text(vtt_raw: str) -> str:
    """
    Pure text-based merge of segmented WebVTT segments.
    Preserves cue settings (line:, position:, align:), formatting tags (<i>, <b>),
    and STYLE blocks. Used for DASH segments with absolute timestamps.
    """
    segments = re.split(r"(?=WEBVTT)", vtt_raw.strip())
    segments = [s.strip() for s in segments if s.strip()]

    TIMING_RE = re.compile(
        r"^(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\d{2}:\d{2}[.,]\d{3})(.*)"
    )

    style_block = None
    all_cues: list = []

    for seg in segments:
        seg_lines = seg.splitlines()
        i = 0
        if seg_lines and seg_lines[i].startswith("WEBVTT"):
            i += 1
        while i < len(seg_lines) and not seg_lines[i].strip():
            i += 1
        # Skip X-TIMESTAMP-MAP line if present
        if i < len(seg_lines) and "X-TIMESTAMP-MAP" in seg_lines[i]:
            i += 1
            while i < len(seg_lines) and not seg_lines[i].strip():
                i += 1
        # Collect STYLE block (keep only first occurrence)
        if i < len(seg_lines) and seg_lines[i].strip() == "STYLE":
            style_lines = ["STYLE"]
            i += 1
            while i < len(seg_lines) and seg_lines[i].strip():
                style_lines.append(seg_lines[i])
                i += 1
            if style_block is None:
                style_block = "\n".join(style_lines)
            while i < len(seg_lines) and not seg_lines[i].strip():
                i += 1
        # Parse cues
        while i < len(seg_lines):
            line = seg_lines[i].strip()
            if not line:
                i += 1
                continue
            if line in ("NOTE", "REGION"):
                i += 1
                while i < len(seg_lines) and seg_lines[i].strip():
                    i += 1
                continue
            m = TIMING_RE.match(line)
            if not m:
                i += 1  # cue identifier or unknown — skip
                continue
            start, end, settings = m.group(1), m.group(2), m.group(3)
            i += 1
            content_lines: list = []
            while i < len(seg_lines) and seg_lines[i].strip() and "-->" not in seg_lines[i]:
                content_lines.append(seg_lines[i].rstrip())
                i += 1
            if content_lines:
                text = "\n".join(content_lines)
                # Filter out ghost cues: those whose visible text is empty after
                # stripping ASS/SSA position tags (e.g. {\an8}) and whitespace.
                # Also filter cues with duration < 100ms — these are sync markers
                # inserted by some providers (HBO Max, Sky) with no real content.
                def _ts_to_ms(ts: str) -> int:
                    ts = ts.replace(",", ".")
                    parts = ts.split(":")
                    if len(parts) == 3:
                        h, m, s = parts
                    else:
                        h, m, s = "0", parts[0], parts[1]
                    sec, ms = s.split(".")
                    return (int(h) * 3600 + int(m) * 60 + int(sec)) * 1000 + int(ms[:3].ljust(3, "0"))
                visible = re.sub(r"\{[^}]*\}", "", text).strip()
                duration_ms = _ts_to_ms(end) - _ts_to_ms(start)
                if visible and duration_ms >= 100:
                    all_cues.append((start, end, settings, text))

    # Deduplicate exact matches
    seen: set = set()
    deduped = []
    for cue in all_cues:
        key = (cue[0], cue[1], cue[3])
        if key not in seen:
            seen.add(key)
            deduped.append(cue)

    out = ["WEBVTT", ""]
    if style_block:
        out.append(style_block)
        out.append("")
    for s, e, settings, text in deduped:
        out.append(f"{s} --> {e}{settings}")
        out.append(text)
        out.append("")
    return "\n".join(out)


def merge_segmented_webvtt(vtt_raw: str, segment_durations: Optional[list[int]] = None, timescale: int = 1) -> str:
    """
    Merge Segmented WebVTT data.

    Parameters:
        vtt_raw: The concatenated WebVTT files to merge. All WebVTT headers must be
            appropriately spaced apart, or it may produce unwanted effects like
            considering headers as captions, timestamp lines, etc.
        segment_durations: A list of each segment's duration. If not provided it will try
            to get it from the X-TIMESTAMP-MAP headers, specifically the MPEGTS number.
        timescale: The number of time units per second.

    This parses the X-TIMESTAMP-MAP data to compute new absolute timestamps, replacing
    the old start and end timestamp values. All X-TIMESTAMP-MAP header information will
    be removed from the output as they are no longer of concern. Consider this function
    the opposite of a WebVTT Segmenter, a WebVTT Joiner of sorts.

    Algorithm borrowed from N_m3u8DL-RE and shaka-player.
    """
    MPEG_TIMESCALE = 90_000

    # Use pure text merge when timestamps are already absolute — this preserves
    # cue settings (line:, position:, align:) and formatting tags (<i>, <b>).
    # Conditions:
    #   1. No X-TIMESTAMP-MAP at all (DASH with absolute timestamps)
    #   2. X-TIMESTAMP-MAP present but MPEGTS=0 and LOCAL=00:00:00.000 (SKY, some HLS)
    has_timestamp_map = 'X-TIMESTAMP-MAP' in vtt_raw
    if not has_timestamp_map:
        return _merge_webvtt_text(vtt_raw)
    # Check if all MPEGTS values are 0 and LOCAL is 00:00:00.000 (already absolute)
    import re as _re
    all_mpegts = _re.findall(r'MPEGTS:(\d+)', vtt_raw)
    all_local = _re.findall(r'LOCAL:([^\s,\n]+)', vtt_raw)
    if all_mpegts and all(m == '0' for m in all_mpegts) and all(l == '00:00:00.000' for l in all_local):
        return _merge_webvtt_text(vtt_raw)

    # Check config for conversion method preference
    conversion_method = config.subtitle.get("conversion_method", "auto")
    use_pysubs2 = conversion_method in ("pysubs2", "auto")

    if use_pysubs2:
        # Try using pysubs2 first for more lenient parsing
        try:
            # Use pysubs2 to parse and normalize the VTT
            subs = pysubs2.SSAFile.from_string(vtt_raw)
            # Convert back to WebVTT string for pycaption processing
            normalized_vtt = subs.to_string("vtt")
            vtt = WebVTTReaderExt().read(normalized_vtt)
        except Exception:
            # Fall back to direct pycaption parsing
            vtt = WebVTTReaderExt().read(vtt_raw)
    else:
        # Use pycaption directly
        vtt = WebVTTReaderExt().read(vtt_raw)
    for lang in vtt.get_languages():
        prev_caption = None
        duplicate_index: list[int] = []
        captions = vtt.get_captions(lang)

        # Some providers can produce "segment_index" values that are
        # outside the provided segment_durations list after normalization/merge.
        # This used to crash with IndexError and abort the entire download.
        if segment_durations and captions:
            max_idx = max(getattr(c, "segment_index", 0) for c in captions)
            if max_idx >= len(segment_durations):
                # Pad with the last known duration (or 0 if empty) so indexing is safe.
                pad_val = segment_durations[-1] if segment_durations else 0
                segment_durations = segment_durations + [pad_val] * (max_idx - len(segment_durations) + 1)

        if captions[0].segment_index == 0:
            first_segment_mpegts = captions[0].mpegts
        else:
            first_segment_mpegts = segment_durations[0] if segment_durations else captions.first_segment_mpegts

        caption: CaptionExt
        for i, caption in enumerate(captions):
            # DASH WebVTT doesn't have MPEGTS timestamp like HLS. Instead,
            # calculate the timestamp from SegmentTemplate/SegmentList duration.
            likely_dash = first_segment_mpegts == 0 and caption.mpegts == 0
            if likely_dash and segment_durations:
                # Defensive: segment_index can still be out of range if captions are malformed.
                if caption.segment_index < 0 or caption.segment_index >= len(segment_durations):
                    continue
                duration = segment_durations[caption.segment_index]
                caption.mpegts = MPEG_TIMESCALE * (duration / timescale)

            if caption.mpegts == 0:
                continue

            # Commeted to fix DSNP subs being out of sync and mistimed.
            # seconds = (caption.mpegts - first_segment_mpegts) / MPEG_TIMESCALE - caption.cue_time
            # offset = seconds * 1_000_000  # pycaption use microseconds

            # if caption.start < offset:
            #    caption.start += offset
            #    caption.end += offset

            # If the difference between current and previous captions is <=1ms
            # and the payload is equal then splice.
            if (
                prev_caption
                and not caption.is_empty()
                and (caption.start - prev_caption.end) <= 1000  # 1ms in microseconds
                and caption.get_text() == prev_caption.get_text()
            ):
                prev_caption.end = caption.end
                duplicate_index.append(i)

            prev_caption = caption

        # Remove duplicate
        captions[:] = [c for c_index, c in enumerate(captions) if c_index not in set(duplicate_index)]

    return WebVTTWriter().write(vtt)
