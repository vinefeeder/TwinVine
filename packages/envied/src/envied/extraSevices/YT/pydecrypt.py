# Clone from https://github.com/Hugoved/pydecrypt

import argparse
import mmap
import os
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    from Crypto.Cipher import AES as FP_AES
except Exception:
    FP_AES = None

CONTAINER_TYPES = {
    b"moov",
    b"trak",
    b"mdia",
    b"minf",
    b"stbl",
    b"edts",
    b"dinf",
    b"mvex",
    b"moof",
    b"traf",
    b"mfra",
    b"skip",
    b"meta",
    b"ipro",
    b"sinf",
    b"schi",
    b"udta",
    b"ilst",
    b"stsd",
    b"mvhd",
}
FULL_BOX_TYPES = {
    b"mvhd",
    b"tkhd",
    b"mdhd",
    b"hdlr",
    b"vmhd",
    b"smhd",
    b"hmhd",
    b"nmhd",
    b"dref",
    b"stsd",
    b"stts",
    b"ctts",
    b"stsc",
    b"stsz",
    b"stz2",
    b"stco",
    b"co64",
    b"stss",
    b"mvex",
    b"trex",
    b"mfhd",
    b"tfhd",
    b"trun",
    b"tfdt",
    b"sidx",
    b"saiz",
    b"saio",
    b"sbgp",
    b"sgpd",
    b"senc",
    b"mehd",
    b"elst",
    b"url ",
    b"urn ",
    b"schm",
    b"pssh",
}
PROTECTED_SAMPLE_ENTRY_TYPES = {b"enca", b"encv", b"enct", b"encs", b"drms", b"drmi", b"p608"}
VISUAL_SAMPLE_ENTRY_TYPES = {
    b"avc1",
    b"avc2",
    b"avc3",
    b"avc4",
    b"hev1",
    b"hvc1",
    b"dvhe",
    b"dvh1",
    b"encv",
    b"av01",
    b"vp09",
}
AUDIO_SAMPLE_ENTRY_TYPES = {b"mp4a", b"ac-3", b"ec-3", b"ac-4", b"enca", b"alac", b"fLaC", b"opus"}
HINT_SAMPLE_ENTRY_TYPES = {b"rtp ", b"srtp", b"rrtp"}
TEXT_SAMPLE_ENTRY_TYPES = {b"tx3g", b"wvtt", b"stpp", b"sbtt", b"enct", b"c608"}
SUBTITLE_SAMPLE_ENTRY_TYPES = {b"stpp", b"wvtt", b"sbtt", b"tx3g", b"enct"}
TEXT_HANDLER_TYPES = {b"text", b"sbtl", b"subt", b"clcp"}
PIFF_TRACK_ENCRYPTION_UUID = bytes.fromhex("8974dbce7be74c5184f97148f9882554")
PIFF_SAMPLE_ENCRYPTION_UUID = bytes.fromhex("a2394f525a9b4f14a2446c427c648df4")

DEFAULT_COPY_CHUNK = 32 * 1024 * 1024
PROGRESS_UPDATE_INTERVAL = 0.50

WEBM_ID_EBML = 0x1A45DFA3
WEBM_ID_SEGMENT = 0x18538067
WEBM_ID_SEEK_HEAD = 0x114D9B74
WEBM_ID_INFO = 0x1549A966
WEBM_ID_TRACKS = 0x1654AE6B
WEBM_ID_CLUSTER = 0x1F43B675
WEBM_ID_CUES = 0x1C53BB6B
WEBM_ID_TAGS = 0x1254C367
WEBM_ID_CHAPTERS = 0x1043A770
WEBM_ID_ATTACHMENTS = 0x1941A469
WEBM_ID_VOID = 0xEC
WEBM_ID_CRC32 = 0xBF
WEBM_ID_TRACK_ENTRY = 0xAE
WEBM_ID_TRACK_NUMBER = 0xD7
WEBM_ID_TRACK_UID = 0x73C5
WEBM_ID_TRACK_TYPE = 0x83
WEBM_ID_CODEC_ID = 0x86
WEBM_ID_NAME = 0x536E
WEBM_ID_LANGUAGE = 0x22B59C
WEBM_ID_CONTENT_ENCODINGS = 0x6D80
WEBM_ID_CONTENT_ENCODING = 0x6240
WEBM_ID_CONTENT_ENCRYPTION = 0x5035
WEBM_ID_CONTENT_ENC_KEY_ID = 0x47E2
WEBM_ID_SIMPLE_BLOCK = 0xA3
WEBM_ID_BLOCK_GROUP = 0xA0
WEBM_ID_BLOCK = 0xA1
WEBM_TRACK_TYPE_VIDEO = 1
WEBM_TRACK_TYPE_AUDIO = 2
WEBM_TRACK_TYPE_SUBTITLE = 0x11
WEBM_TRACK_TYPE_METADATA = 0x21
WEBM_SIGNAL_BYTE_SIZE = 1
WEBM_IV_SIZE = 8
WEBM_ENCRYPTED_SIGNAL = 0x01
WEBM_PARTITIONED_SIGNAL = 0x02
WEBM_NUM_PARTITIONS_SIZE = 1
WEBM_PARTITION_OFFSET_SIZE = 4


def fail(message: str):
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def u8(data, offset):
    return data[offset]


def u16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def u24(data, offset):
    return (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]


def u32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def u64(data, offset):
    return struct.unpack_from(">Q", data, offset)[0]


def normalize_kid(text: str) -> bytes:
    text = text.strip().lower().replace("0x", "").replace("-", "")
    if len(text) != 32:
        raise ValueError("KID must be 32 hex characters")
    return bytes.fromhex(text)


def normalize_key(text: str) -> bytes:
    text = text.strip().lower().replace("0x", "").replace("-", "")
    if len(text) != 32:
        raise ValueError("Key must be 32 hex characters")
    return bytes.fromhex(text)


@dataclass
class Box:
    start: int
    size: int
    type: bytes
    header_size: int
    end: int
    uuid: Optional[bytes] = None
    children: List["Box"] = field(default_factory=list)


@dataclass
class SampleAuxInfo:
    iv: bytes
    subsamples: List[Tuple[int, int]]


@dataclass
class TencInfo:
    is_encrypted: int
    iv_size: int
    kid: bytes
    constant_iv: bytes
    crypt_byte_block: int
    skip_byte_block: int
    scheme: bytes


@dataclass
class TrackInfo:
    track_id: int
    timescale: int = 0
    handler_type: bytes = b""
    sample_count: int = 0
    sample_sizes: List[int] = field(default_factory=list)
    sample_offsets: List[int] = field(default_factory=list)
    scheme: bytes = b""
    tenc: Optional[TencInfo] = None
    sample_entry_box: Optional[Box] = None
    original_format: Optional[bytes] = None
    aux_info: List[SampleAuxInfo] = field(default_factory=list)
    default_sample_size: int = 0
    codec_format: bytes = b""
    nal_length_size: int = 0
    nal_header_clear_bytes: int = 0


@dataclass
class FragmentRun:
    track_id: int
    trun_box: Box
    tfhd_box: Optional[Box]
    traf_box: Box
    data_offset: int
    sample_sizes: List[int]
    sample_offsets: List[int]
    aux_info: List[SampleAuxInfo]
    scheme: bytes
    tenc: Optional[TencInfo]


@dataclass
class BytePatch:
    start: int
    data: bytes

    @property
    def end(self) -> int:
        return self.start + len(self.data)


@dataclass
class DecryptTask:
    start: int
    size: int
    key: bytes
    info: SampleAuxInfo
    scheme: bytes
    tenc: TencInfo
    codec_format: bytes = b""
    nal_length_size: int = 0
    nal_header_clear_bytes: int = 0

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass
class StreamEvent:
    start: int
    end: int
    kind: str
    payload: object


@dataclass
class EbmlElement:
    id_value: int
    id_bytes: bytes
    size_value: Optional[int]
    size_len: int
    data_start: int
    data_end: int
    header_start: int
    header_end: int
    end: int
    unknown_size: bool


@dataclass
class WebMTrack:
    track_number: int
    track_uid: Optional[int] = None
    track_type: Optional[int] = None
    codec_id: str = ""
    name: str = ""
    language: str = ""
    key_id: bytes = b""
    encrypted: bool = False
    content_encodings_start_rel: Optional[int] = None
    content_encodings_end_rel: Optional[int] = None


class ProgressPrinter:
    def __init__(self, total_size: int):
        self.total_size = max(total_size, 1)
        self.started_at = time.monotonic()
        self.last_update = 0.0
        self.last_line_length = 0
        self.done = 0

    @staticmethod
    def _format_hms(seconds: float) -> str:
        seconds = max(0, int(seconds))
        hours, rem = divmod(seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _line(self) -> str:
        ratio = min(max(self.done / self.total_size, 0.0), 1.0)
        width = 40
        filled = min(width, int(ratio * width))
        bar = "■" * filled + " " * (width - filled)
        percent = ratio * 100.0
        elapsed = time.monotonic() - self.started_at
        speed = self.done / elapsed if elapsed > 0 else 0.0
        remaining = (self.total_size - self.done) / speed if speed > 0 else 0.0
        return (
            f"[{bar}] {percent:6.2f}% (elapsed: {self._format_hms(elapsed)}, remaining: {self._format_hms(remaining)})"
        )

    def update(self, done: int, force: bool = False):
        self.done = max(0, min(done, self.total_size))
        now = time.monotonic()
        if not force and (now - self.last_update) < PROGRESS_UPDATE_INTERVAL and self.done < self.total_size:
            return
        line = self._line()
        clear_pad = " " * max(0, self.last_line_length - len(line))
        sys.stdout.write("\r" + line + clear_pad)
        sys.stdout.flush()
        self.last_line_length = len(line)
        self.last_update = now

    def finish(self):
        self.update(self.total_size, force=True)
        sys.stdout.write("\n")
        sys.stdout.flush()


class Mp4Parser:
    def __init__(self, data: bytes):
        self.data = data
        self.root = self.parse_children(0, len(data), None)

    def parse_children(self, start: int, end: int, parent_type: Optional[bytes]) -> List[Box]:
        boxes = []
        pos = start
        while pos + 8 <= end:
            size = u32(self.data, pos)
            box_type = self.data[pos + 4 : pos + 8]
            header_size = 8
            if size == 1:
                if pos + 16 > end:
                    break
                size = u64(self.data, pos + 8)
                header_size = 16
            elif size == 0:
                size = end - pos
            if size < header_size or pos + size > end:
                break
            box_uuid = None
            if box_type == b"uuid":
                if pos + header_size + 16 > end:
                    break
                box_uuid = self.data[pos + header_size : pos + header_size + 16]
                header_size += 16
            box = Box(pos, size, box_type, header_size, pos + size, box_uuid)
            content_start = pos + header_size
            if box_type == b"meta" and content_start + 4 <= box.end:
                content_start += 4
                box.header_size += 4
            if self.is_container(box, parent_type):
                box.children = self.parse_children(content_start, box.end, box_type)
            boxes.append(box)
            pos += size
        return boxes

    def is_container(self, box: Box, parent_type: Optional[bytes]) -> bool:
        if box.type in {
            b"moov",
            b"trak",
            b"mdia",
            b"minf",
            b"stbl",
            b"edts",
            b"dinf",
            b"mvex",
            b"moof",
            b"traf",
            b"mfra",
            b"skip",
            b"udta",
            b"ipro",
            b"sinf",
            b"schi",
            b"ilst",
        }:
            return True
        if box.type == b"stsd":
            return False
        if box.type == b"meta":
            return True
        return False

    def find_children(self, parent: Box, box_type: bytes) -> List[Box]:
        return [child for child in parent.children if child.type == box_type]

    def find_child(self, parent: Box, box_type: bytes) -> Optional[Box]:
        for child in parent.children:
            if child.type == box_type:
                return child
        return None

    def find_uuid_child(self, parent: Box, uuid_value: bytes) -> Optional[Box]:
        for child in parent.children:
            if child.type == b"uuid" and child.uuid == uuid_value:
                return child
        return None


def parse_tkhd(data: bytes, tkhd: Box) -> int:
    version = u8(data, tkhd.start + tkhd.header_size)
    offset = tkhd.start + tkhd.header_size + 4
    if version == 1:
        return u32(data, offset + 16)
    return u32(data, offset + 8)


def parse_mdhd_timescale(data: bytes, mdhd: Box) -> int:
    version = u8(data, mdhd.start + mdhd.header_size)
    offset = mdhd.start + mdhd.header_size + 4
    if version == 1:
        return u32(data, offset + 16)
    return u32(data, offset + 8)


def parse_hdlr(data: bytes, hdlr: Box) -> bytes:
    return data[hdlr.start + hdlr.header_size + 8 : hdlr.start + hdlr.header_size + 12]


def parse_stsz(data: bytes, box: Box) -> List[int]:
    offset = box.start + box.header_size + 4
    sample_size = u32(data, offset)
    sample_count = u32(data, offset + 4)
    sizes = []
    if sample_size:
        sizes = [sample_size] * sample_count
    else:
        pos = offset + 8
        for _ in range(sample_count):
            sizes.append(u32(data, pos))
            pos += 4
    return sizes


def parse_stz2(data: bytes, box: Box) -> List[int]:
    offset = box.start + box.header_size + 4
    field_size = u8(data, offset + 3)
    sample_count = u32(data, offset + 4)
    pos = offset + 8
    sizes: List[int] = []
    if field_size == 4:
        for _ in range((sample_count + 1) // 2):
            packed = u8(data, pos)
            pos += 1
            sizes.append((packed >> 4) & 0x0F)
            if len(sizes) < sample_count:
                sizes.append(packed & 0x0F)
    elif field_size == 8:
        for _ in range(sample_count):
            sizes.append(u8(data, pos))
            pos += 1
    elif field_size == 16:
        for _ in range(sample_count):
            sizes.append(u16(data, pos))
            pos += 2
    else:
        raise ValueError(f"Unsupported stz2 field size: {field_size}")
    return sizes


def parse_stco(data: bytes, box: Box) -> List[int]:
    offset = box.start + box.header_size + 4
    entry_count = u32(data, offset)
    pos = offset + 4
    values = []
    if box.type == b"stco":
        for _ in range(entry_count):
            values.append(u32(data, pos))
            pos += 4
    else:
        for _ in range(entry_count):
            values.append(u64(data, pos))
            pos += 8
    return values


def parse_stsc(data: bytes, box: Box) -> List[Tuple[int, int, int]]:
    offset = box.start + box.header_size + 4
    entry_count = u32(data, offset)
    pos = offset + 4
    values = []
    for _ in range(entry_count):
        values.append((u32(data, pos), u32(data, pos + 4), u32(data, pos + 8)))
        pos += 12
    return values


def compute_sample_offsets(
    chunk_offsets: List[int], stsc: List[Tuple[int, int, int]], sample_sizes: List[int]
) -> List[int]:
    if not chunk_offsets or not stsc or not sample_sizes:
        return []
    offsets = []
    sample_index = 0
    for i, (first_chunk, samples_per_chunk, _) in enumerate(stsc):
        next_first_chunk = stsc[i + 1][0] if i + 1 < len(stsc) else len(chunk_offsets) + 1
        for chunk_number in range(first_chunk, next_first_chunk):
            if chunk_number - 1 >= len(chunk_offsets):
                break
            current = chunk_offsets[chunk_number - 1]
            for _ in range(samples_per_chunk):
                if sample_index >= len(sample_sizes):
                    break
                offsets.append(current)
                current += sample_sizes[sample_index]
                sample_index += 1
    return offsets


def parse_tenc_from_bytes(data: bytes, start: int, size: int, scheme: bytes) -> TencInfo:
    header_size = 8
    if u32(data, start) == 1:
        header_size = 16
    if data[start + 4 : start + 8] == b"uuid":
        header_size += 16
    version = u8(data, start + header_size)
    pos = start + header_size + 4
    crypt_byte_block = 0
    skip_byte_block = 0

    if version == 0:
        pos += 2
    else:
        reserved_or_pattern = u8(data, pos)
        next_byte = u8(data, pos + 1)

        if reserved_or_pattern == 0 and next_byte != 0:
            block_info = next_byte
            pos += 2
        else:
            block_info = reserved_or_pattern
            pos += 2

        crypt_byte_block = block_info >> 4
        skip_byte_block = block_info & 0x0F

    is_encrypted = u8(data, pos)
    iv_size = u8(data, pos + 1)
    kid = data[pos + 2 : pos + 18]
    pos += 18
    constant_iv = b""
    if is_encrypted and iv_size == 0 and pos < start + size:
        constant_iv_size = u8(data, pos)
        constant_iv = data[pos + 1 : pos + 1 + constant_iv_size]
    return TencInfo(is_encrypted, iv_size, kid, constant_iv, crypt_byte_block, skip_byte_block, scheme)


def parse_avcc_nal_length_size(payload: bytes) -> int:
    if len(payload) < 5:
        return 0
    return (payload[4] & 0x03) + 1


def parse_hvcc_nal_length_size(payload: bytes) -> int:
    if len(payload) < 22:
        return 0
    return (payload[21] & 0x03) + 1


def parse_codec_fallback_info(data: bytes, entry_start: int, entry_end: int) -> Tuple[bytes, int, int]:
    scan = entry_start
    while scan + 8 <= entry_end:
        child_size = u32(data, scan)
        child_type = data[scan + 4 : scan + 8]
        child_header = 8
        if child_size == 1:
            if scan + 16 > entry_end:
                break
            child_size = u64(data, scan + 8)
            child_header = 16
        elif child_size == 0:
            child_size = entry_end - scan
        if child_size < child_header or scan + child_size > entry_end:
            break
        payload = data[scan + child_header : scan + child_size]
        if child_type == b"avcC":
            nal_length_size = parse_avcc_nal_length_size(payload)
            return b"avc1", nal_length_size, 1
        if child_type == b"hvcC":
            nal_length_size = parse_hvcc_nal_length_size(payload)
            return b"hvc1", nal_length_size, 2
        scan += child_size
    return b"", 0, 0


def parse_stsd_sample_entry(
    data: bytes, stsd: Box
) -> Tuple[Optional[Box], Optional[bytes], bytes, Optional[TencInfo], bytes, int, int]:
    pos = stsd.start + stsd.header_size + 4
    entry_count = u32(data, pos)
    pos += 4
    if entry_count < 1 or pos + 8 > stsd.end:
        return None, None, b"", None, b"", 0, 0
    entry_size = u32(data, pos)
    entry_type = data[pos + 4 : pos + 8]
    if entry_size < 8 or pos + entry_size > stsd.end:
        return None, None, b"", None, b"", 0, 0
    entry_box = Box(pos, entry_size, entry_type, 8, pos + entry_size)
    original_format = None
    scheme = b""
    tenc = None
    sinf_pos = None
    scan_start = entry_box.start + entry_box.header_size
    scan_end = entry_box.end - 8
    for candidate in range(scan_start, scan_end + 1):
        child_size = u32(data, candidate)
        child_type = data[candidate + 4 : candidate + 8]
        child_header = 8
        if child_size == 1:
            if candidate + 16 > entry_box.end:
                continue
            child_size = u64(data, candidate + 8)
            child_header = 16
        elif child_size == 0:
            child_size = entry_box.end - candidate
        if child_type == b"uuid":
            if candidate + child_header + 16 > entry_box.end:
                continue
            child_header += 16
        if child_type == b"sinf" and child_size >= child_header and candidate + child_size <= entry_box.end:
            sinf_pos = candidate
            break
    if sinf_pos is None:
        codec_format, nal_length_size, nal_header_clear_bytes = parse_codec_fallback_info(
            data, entry_box.start + entry_box.header_size, entry_box.end
        )
        return entry_box, None, b"", None, codec_format, nal_length_size, nal_header_clear_bytes
    sinf_size = u32(data, sinf_pos)
    sinf_header = 8
    if sinf_size == 1:
        sinf_size = u64(data, sinf_pos + 8)
        sinf_header = 16
    elif sinf_size == 0:
        sinf_size = entry_box.end - sinf_pos
    sinf_end = sinf_pos + sinf_size
    sub = sinf_pos + sinf_header
    while sub + 8 <= sinf_end:
        sub_size = u32(data, sub)
        sub_type = data[sub + 4 : sub + 8]
        sub_header = 8
        sub_uuid = None
        if sub_size == 1:
            sub_size = u64(data, sub + 8)
            sub_header = 16
        elif sub_size == 0:
            sub_size = sinf_end - sub
        if sub_type == b"uuid":
            if sub + sub_header + 16 > sinf_end:
                break
            sub_uuid = data[sub + sub_header : sub + sub_header + 16]
            sub_header += 16
        if sub_size < sub_header or sub + sub_size > sinf_end:
            break
        if sub_type == b"frma":
            original_format = data[sub + sub_header : sub + sub_header + 4]
        elif sub_type == b"schm":
            scheme = data[sub + sub_header + 4 : sub + sub_header + 8]
        elif sub_type == b"schi":
            schi_end = sub + sub_size
            schi_pos = sub + sub_header
            while schi_pos + 8 <= schi_end:
                schi_size = u32(data, schi_pos)
                schi_type = data[schi_pos + 4 : schi_pos + 8]
                schi_header = 8
                schi_uuid = None
                if schi_size == 1:
                    schi_size = u64(data, schi_pos + 8)
                    schi_header = 16
                elif schi_size == 0:
                    schi_size = schi_end - schi_pos
                if schi_type == b"uuid":
                    if schi_pos + schi_header + 16 > schi_end:
                        break
                    schi_uuid = data[schi_pos + schi_header : schi_pos + schi_header + 16]
                    schi_header += 16
                if schi_size < schi_header or schi_pos + schi_size > schi_end:
                    break
                if schi_type == b"tenc" or (schi_type == b"uuid" and schi_uuid == PIFF_TRACK_ENCRYPTION_UUID):
                    tenc = parse_tenc_from_bytes(data, schi_pos, schi_size, scheme)
                schi_pos += schi_size
        sub += sub_size
    codec_format, nal_length_size, nal_header_clear_bytes = parse_codec_fallback_info(
        data, entry_box.start + entry_box.header_size, entry_box.end
    )
    return entry_box, original_format, scheme, tenc, codec_format, nal_length_size, nal_header_clear_bytes


def parse_senc_payload(blob: bytes, iv_size_hint: int, default_constant_iv: bytes) -> List[SampleAuxInfo]:
    if len(blob) < 8:
        return []
    flags = (blob[1] << 16) | (blob[2] << 8) | blob[3]
    sample_count = struct.unpack_from(">I", blob, 4)[0]
    pos = 8
    records = []
    use_subsamples = (flags & 0x000002) != 0
    for _ in range(sample_count):
        if iv_size_hint == 0:
            iv = default_constant_iv
        else:
            if pos + iv_size_hint > len(blob):
                break
            iv = blob[pos : pos + iv_size_hint]
            pos += iv_size_hint
        subsamples = []
        if use_subsamples:
            if pos + 2 > len(blob):
                break
            subsample_count = struct.unpack_from(">H", blob, pos)[0]
            pos += 2
            for _ in range(subsample_count):
                if pos + 6 > len(blob):
                    break
                clear_bytes = struct.unpack_from(">H", blob, pos)[0]
                encrypted_bytes = struct.unpack_from(">I", blob, pos + 2)[0]
                subsamples.append((clear_bytes, encrypted_bytes))
                pos += 6
        records.append(SampleAuxInfo(iv, subsamples))
    return records


def parse_saiz(data: bytes, box: Box) -> List[int]:
    pos = box.start + box.header_size
    flags = u24(data, pos + 1)
    pos += 4
    if flags & 1:
        pos += 8
    default_info_size = u8(data, pos)
    sample_count = u32(data, pos + 1)
    pos += 5
    if default_info_size:
        return [default_info_size] * sample_count
    sizes = []
    for _ in range(sample_count):
        sizes.append(u8(data, pos))
        pos += 1
    return sizes


def parse_saio(data: bytes, box: Box) -> List[int]:
    pos = box.start + box.header_size
    flags = u24(data, pos + 1)
    version = u8(data, pos)
    pos += 4
    if flags & 1:
        pos += 8
    entry_count = u32(data, pos)
    pos += 4
    offsets = []
    for _ in range(entry_count):
        offsets.append(u64(data, pos) if version == 1 else u32(data, pos))
        pos += 8 if version == 1 else 4
    return offsets


def parse_senc_box(data: bytes, box: Box, iv_size_hint: int, default_constant_iv: bytes) -> List[SampleAuxInfo]:
    payload = data[box.start + box.header_size : box.end]
    return parse_senc_payload(payload, iv_size_hint, default_constant_iv)


def parse_aux_info_via_saiz_saio(
    data: bytes,
    sample_info_sizes: List[int],
    offsets: List[int],
    iv_size_hint: int,
    default_constant_iv: bytes,
    offset_base: int = 0,
) -> List[SampleAuxInfo]:
    if not sample_info_sizes or not offsets:
        return []
    base = offset_base + offsets[0]
    total = sum(sample_info_sizes)
    if base + total > len(data):
        total = max(0, len(data) - base)
    blob = data[base : base + total]
    records = []
    pos = 0
    for sample_size in sample_info_sizes:
        if pos + sample_size > len(blob):
            break
        sample_blob = blob[pos : pos + sample_size]
        if iv_size_hint == 0:
            iv = default_constant_iv
            sub_pos = 0
        else:
            iv = sample_blob[:iv_size_hint]
            sub_pos = iv_size_hint
        subsamples = []
        if sub_pos < len(sample_blob) and sub_pos + 2 <= len(sample_blob):
            subsample_count = struct.unpack_from(">H", sample_blob, sub_pos)[0]
            sub_pos += 2
            for _ in range(subsample_count):
                if sub_pos + 6 > len(sample_blob):
                    break
                clear_bytes = struct.unpack_from(">H", sample_blob, sub_pos)[0]
                encrypted_bytes = struct.unpack_from(">I", sample_blob, sub_pos + 2)[0]
                subsamples.append((clear_bytes, encrypted_bytes))
                sub_pos += 6
        records.append(SampleAuxInfo(iv, subsamples))
        pos += sample_size
    return records


def parse_trex(data: bytes, box: Box) -> Tuple[int, int]:
    pos = box.start + box.header_size + 4
    track_id = u32(data, pos)
    default_sample_size = u32(data, pos + 12)
    return track_id, default_sample_size


def parse_tfhd(data: bytes, box: Box) -> Dict[str, int]:
    pos = box.start + box.header_size
    flags = u24(data, pos + 1)
    pos += 4
    values = {"flags": flags, "track_id": u32(data, pos)}
    pos += 4
    if flags & 0x000001:
        values["base_data_offset"] = u64(data, pos)
        pos += 8
    if flags & 0x000002:
        values["sample_description_index"] = u32(data, pos)
        pos += 4
    if flags & 0x000008:
        values["default_sample_duration"] = u32(data, pos)
        pos += 4
    if flags & 0x000010:
        values["default_sample_size"] = u32(data, pos)
        pos += 4
    if flags & 0x000020:
        values["default_sample_flags"] = u32(data, pos)
        pos += 4
    return values


def parse_trun(data: bytes, box: Box) -> Dict[str, object]:
    pos = box.start + box.header_size
    version = u8(data, pos)
    flags = u24(data, pos + 1)
    pos += 4
    sample_count = u32(data, pos)
    pos += 4
    info = {
        "version": version,
        "flags": flags,
        "sample_count": sample_count,
        "data_offset": 0,
        "first_sample_flags": None,
        "samples": [],
    }
    if flags & 0x000001:
        info["data_offset"] = struct.unpack_from(">i", data, pos)[0]
        pos += 4
    if flags & 0x000004:
        info["first_sample_flags"] = u32(data, pos)
        pos += 4
    samples = []
    for _ in range(sample_count):
        sample = {}
        if flags & 0x000100:
            sample["duration"] = u32(data, pos)
            pos += 4
        if flags & 0x000200:
            sample["size"] = u32(data, pos)
            pos += 4
        if flags & 0x000400:
            sample["flags"] = u32(data, pos)
            pos += 4
        if flags & 0x000800:
            sample["cto"] = struct.unpack_from(">i", data, pos)[0] if version == 1 else u32(data, pos)
            pos += 4
        samples.append(sample)
    info["samples"] = samples
    return info


def build_tracks(parser: Mp4Parser) -> Tuple[Dict[int, TrackInfo], Dict[int, int]]:
    tracks: Dict[int, TrackInfo] = {}
    trex_defaults: Dict[int, int] = {}
    for box in parser.root:
        if box.type == b"moov":
            mvex = parser.find_child(box, b"mvex")
            if mvex:
                for trex in parser.find_children(mvex, b"trex"):
                    track_id, default_sample_size = parse_trex(parser.data, trex)
                    trex_defaults[track_id] = default_sample_size
            for trak in parser.find_children(box, b"trak"):
                tkhd = parser.find_child(trak, b"tkhd")
                mdia = parser.find_child(trak, b"mdia")
                if not tkhd or not mdia:
                    continue
                track_id = parse_tkhd(parser.data, tkhd)
                track = TrackInfo(track_id=track_id)
                mdhd = parser.find_child(mdia, b"mdhd")
                if mdhd:
                    track.timescale = parse_mdhd_timescale(parser.data, mdhd)
                hdlr = parser.find_child(mdia, b"hdlr")
                if hdlr:
                    track.handler_type = parse_hdlr(parser.data, hdlr)
                minf = parser.find_child(mdia, b"minf")
                stbl = parser.find_child(minf, b"stbl") if minf else None
                if stbl:
                    stsd = parser.find_child(stbl, b"stsd")
                    if stsd:
                        (
                            entry_box,
                            original_format,
                            scheme,
                            tenc,
                            codec_format,
                            nal_length_size,
                            nal_header_clear_bytes,
                        ) = parse_stsd_sample_entry(parser.data, stsd)
                        track.sample_entry_box = entry_box
                        track.original_format = original_format
                        track.scheme = scheme
                        track.tenc = tenc
                        track.codec_format = codec_format
                        track.nal_length_size = nal_length_size
                        track.nal_header_clear_bytes = nal_header_clear_bytes
                    stsz = parser.find_child(stbl, b"stsz") or parser.find_child(stbl, b"stz2")
                    if stsz:
                        if stsz.type == b"stsz":
                            track.sample_sizes = parse_stsz(parser.data, stsz)
                        else:
                            track.sample_sizes = parse_stz2(parser.data, stsz)
                        track.sample_count = len(track.sample_sizes)
                    stco = parser.find_child(stbl, b"stco") or parser.find_child(stbl, b"co64")
                    stsc = parser.find_child(stbl, b"stsc")
                    if stco and stsc and track.sample_sizes:
                        track.sample_offsets = compute_sample_offsets(
                            parse_stco(parser.data, stco), parse_stsc(parser.data, stsc), track.sample_sizes
                        )
                    senc = parser.find_child(stbl, b"senc") or parser.find_uuid_child(stbl, PIFF_SAMPLE_ENCRYPTION_UUID)
                    saiz = parser.find_child(stbl, b"saiz")
                    saio = parser.find_child(stbl, b"saio")
                    if track.tenc:
                        if senc:
                            track.aux_info = parse_senc_box(
                                parser.data, senc, track.tenc.iv_size, track.tenc.constant_iv
                            )
                        elif saiz and saio:
                            track.aux_info = parse_aux_info_via_saiz_saio(
                                parser.data,
                                parse_saiz(parser.data, saiz),
                                parse_saio(parser.data, saio),
                                track.tenc.iv_size,
                                track.tenc.constant_iv,
                                offset_base=0,
                            )
                tracks[track_id] = track
    return tracks, trex_defaults


def build_fragments(
    parser: Mp4Parser, tracks: Dict[int, TrackInfo], trex_defaults: Dict[int, int]
) -> List[FragmentRun]:
    runs: List[FragmentRun] = []
    for moof in [box for box in parser.root if box.type == b"moof"]:
        next_top = None
        for top in parser.root:
            if top.start == moof.start:
                continue
            if top.start >= moof.end:
                next_top = top
                break
        for traf in parser.find_children(moof, b"traf"):
            tfhd = parser.find_child(traf, b"tfhd")
            if not tfhd:
                continue
            tfhd_info = parse_tfhd(parser.data, tfhd)
            track_id = tfhd_info["track_id"]
            track = tracks.get(track_id)
            tenc = track.tenc if track else None
            scheme = track.scheme if track else b""
            senc = parser.find_child(traf, b"senc") or parser.find_uuid_child(traf, PIFF_SAMPLE_ENCRYPTION_UUID)
            saiz = parser.find_child(traf, b"saiz")
            saio = parser.find_child(traf, b"saio")
            aux_info = []
            if tenc:
                if senc:
                    aux_info = parse_senc_box(parser.data, senc, tenc.iv_size, tenc.constant_iv)
                elif saiz and saio:
                    aux_info = parse_aux_info_via_saiz_saio(
                        parser.data,
                        parse_saiz(parser.data, saiz),
                        parse_saio(parser.data, saio),
                        tenc.iv_size,
                        tenc.constant_iv,
                        offset_base=moof.start,
                    )
            default_size = tfhd_info.get("default_sample_size", trex_defaults.get(track_id, 0))
            for trun in parser.find_children(traf, b"trun"):
                trun_info = parse_trun(parser.data, trun)
                base_data_offset = tfhd_info.get("base_data_offset")
                if base_data_offset is None:
                    base_data_offset = moof.start
                data_offset = base_data_offset + trun_info["data_offset"]
                sample_sizes = []
                for sample in trun_info["samples"]:
                    sample_sizes.append(sample.get("size", default_size))
                sample_offsets = []
                current = data_offset
                for size in sample_sizes:
                    sample_offsets.append(current)
                    current += size
                run_aux_info = aux_info[: len(sample_sizes)] if aux_info else []
                if aux_info:
                    del aux_info[: len(sample_sizes)]
                runs.append(
                    FragmentRun(
                        track_id,
                        trun,
                        tfhd,
                        traf,
                        data_offset,
                        sample_sizes,
                        sample_offsets,
                        run_aux_info,
                        scheme,
                        tenc,
                    )
                )
    return runs


def aes_ecb_decryptor(key: bytes):
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    return cipher.decryptor()


def decrypt_cenc_ctr(sample: bytes, key: bytes, iv: bytes, subsamples: List[Tuple[int, int]]) -> bytes:
    counter_iv = iv + (b"\x00" * (16 - len(iv)))
    cipher = Cipher(algorithms.AES(key), modes.CTR(counter_iv))
    decryptor = cipher.decryptor()
    if not subsamples:
        out = bytearray(len(sample) + 15)
        written = decryptor.update_into(sample, out)
        tail = decryptor.finalize()
        total = written + len(tail)
        if tail:
            out[written:total] = tail
        return bytes(out[:total])
    out = bytearray(sample)
    pos = 0
    for clear_bytes, encrypted_bytes in subsamples:
        pos += clear_bytes
        if encrypted_bytes:
            end = pos + encrypted_bytes
            src = memoryview(out)[pos:end]
            dst = bytearray(encrypted_bytes + 15)
            written = decryptor.update_into(src, dst)
            if written:
                out[pos : pos + written] = dst[:written]
            pos = end
    decryptor.finalize()
    return bytes(out)


def decrypt_cbcs(
    sample: bytes, key: bytes, iv: bytes, subsamples: List[Tuple[int, int]], crypt_blocks: int, skip_blocks: int
) -> bytes:
    if not iv:
        iv = b"\x00" * 16
    if len(iv) < 16:
        iv = iv + (b"\x00" * (16 - len(iv)))
    out = bytearray(sample)

    encrypted_ranges: List[Tuple[int, int]] = []

    def collect_pattern_ranges(start: int, length: int):
        usable = length - (length % 16)
        if usable <= 0:
            return
        if crypt_blocks <= 0 and skip_blocks <= 0:
            encrypted_ranges.append((start, usable))
            return
        if crypt_blocks <= 0:
            return
        if skip_blocks <= 0:
            encrypted_ranges.append((start, usable))
            return
        pos = start
        remaining = usable
        crypt_len = crypt_blocks * 16
        skip_len = skip_blocks * 16
        while remaining >= 16:
            take = min(crypt_len, remaining)
            take -= take % 16
            if take <= 0:
                break
            encrypted_ranges.append((pos, take))
            pos += take
            remaining -= take
            skip = min(skip_len, remaining)
            pos += skip
            remaining -= skip

    if not subsamples:
        collect_pattern_ranges(0, len(out))
    else:
        pos = 0
        for clear_bytes, encrypted_bytes in subsamples:
            pos += clear_bytes
            collect_pattern_ranges(pos, encrypted_bytes)
            pos += encrypted_bytes

    if not encrypted_ranges:
        return bytes(out)

    encrypted_blob = b"".join(bytes(out[start : start + length]) for start, length in encrypted_ranges)
    if not encrypted_blob:
        return bytes(out)

    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    decrypted = decryptor.update(encrypted_blob) + decryptor.finalize()

    cursor = 0
    decrypted_len = len(decrypted)
    for start, length in encrypted_ranges:
        available = min(length, decrypted_len - cursor)
        if available <= 0:
            break
        out[start : start + available] = decrypted[cursor : cursor + available]
        cursor += available

    return bytes(out)


def build_length_prefixed_nal_subsamples(
    sample: bytes, nal_length_size: int, nal_header_clear_bytes: int
) -> List[Tuple[int, int]]:
    if nal_length_size <= 0 or nal_header_clear_bytes < 0:
        return []
    subsamples: List[Tuple[int, int]] = []
    pos = 0
    while pos < len(sample):
        if pos + nal_length_size > len(sample):
            return []
        nal_size = int.from_bytes(sample[pos : pos + nal_length_size], "big")
        pos += nal_length_size
        if nal_size == 0:
            subsamples.append((nal_length_size, 0))
            continue
        if pos + nal_size > len(sample):
            return []
        clear_payload = min(nal_header_clear_bytes, nal_size)
        encrypted_payload = max(0, nal_size - clear_payload)
        subsamples.append((nal_length_size + clear_payload, encrypted_payload))
        pos += nal_size
    return subsamples


def decrypt_sample(
    sample: bytes,
    key: bytes,
    info: SampleAuxInfo,
    scheme: bytes,
    tenc: TencInfo,
    codec_format: bytes = b"",
    nal_length_size: int = 0,
    nal_header_clear_bytes: int = 0,
) -> bytes:
    effective_subsamples = info.subsamples
    if not effective_subsamples and codec_format in {b"avc1", b"hvc1"} and nal_length_size > 0:
        guessed_subsamples = build_length_prefixed_nal_subsamples(sample, nal_length_size, nal_header_clear_bytes)
        if guessed_subsamples:
            effective_subsamples = guessed_subsamples
    if scheme in {b"cenc", b"cens", b"piff", b""}:
        return decrypt_cenc_ctr(sample, key, info.iv, effective_subsamples)
    if scheme in {b"cbcs", b"cbc1"}:
        return decrypt_cbcs(
            sample,
            key,
            info.iv if info.iv else tenc.constant_iv,
            effective_subsamples,
            tenc.crypt_byte_block,
            tenc.skip_byte_block,
        )
    raise ValueError(f"Unsupported protection scheme: {scheme.decode('ascii', 'ignore')}")


def patch_sample_description(data: bytearray, track: TrackInfo):
    if not track.sample_entry_box or not track.original_format:
        return
    box = track.sample_entry_box
    data[box.start + 4 : box.start + 8] = track.original_format


def resolve_track_key(track: TrackInfo, keys_by_track: Dict[int, bytes], keys_by_kid: Dict[bytes, bytes]) -> bytes:
    if track.track_id in keys_by_track:
        return keys_by_track[track.track_id]
    if track.tenc and track.tenc.kid in keys_by_kid:
        return keys_by_kid[track.tenc.kid]
    if len(keys_by_kid) == 1 and track.tenc:
        return next(iter(keys_by_kid.values()))
    if len(keys_by_track) == 1:
        return next(iter(keys_by_track.values()))
    raise KeyError(f"No key found for track {track.track_id}")


def resolve_default_sample_info(tenc: TencInfo) -> Optional[SampleAuxInfo]:
    if tenc.iv_size == 0 and tenc.constant_iv:
        return SampleAuxInfo(tenc.constant_iv, [])
    return None


def resolve_sample_info(aux_info: List[SampleAuxInfo], index: int, tenc: TencInfo) -> Optional[SampleAuxInfo]:
    if aux_info:
        if index >= len(aux_info):
            return None
        return aux_info[index]
    return resolve_default_sample_info(tenc)


def apply_track_decryption(data: bytearray, track: TrackInfo, key: bytes):
    if not track.tenc or not track.sample_offsets or not track.sample_sizes:
        return
    if track.aux_info and len(track.aux_info) != len(track.sample_sizes):
        raise ValueError(f"Track {track.track_id} has mismatched sample encryption info")
    for index, (offset, size) in enumerate(zip(track.sample_offsets, track.sample_sizes)):
        if offset + size > len(data):
            raise ValueError(f"Track {track.track_id} sample {index + 1} exceeds file size")
        info = resolve_sample_info(track.aux_info, index, track.tenc)
        if info is None:
            continue
        sample = bytes(data[offset : offset + size])
        data[offset : offset + size] = decrypt_sample(
            sample,
            key,
            info,
            track.scheme,
            track.tenc,
            track.codec_format,
            track.nal_length_size,
            track.nal_header_clear_bytes,
        )
    patch_sample_description(data, track)


def apply_fragment_decryption(data: bytearray, run: FragmentRun, key: bytes):
    if not run.tenc:
        return
    if run.aux_info and len(run.aux_info) != len(run.sample_sizes):
        raise ValueError(f"Fragment track {run.track_id} has mismatched sample encryption info")
    for index, (offset, size) in enumerate(zip(run.sample_offsets, run.sample_sizes)):
        if offset + size > len(data):
            raise ValueError(f"Fragment track {run.track_id} sample {index + 1} exceeds file size")
        info = resolve_sample_info(run.aux_info, index, run.tenc)
        if info is None:
            continue
        sample = bytes(data[offset : offset + size])
        data[offset : offset + size] = decrypt_sample(sample, key, info, run.scheme, run.tenc)


def patch_senc_flags(data: bytearray, boxes: List[Box]):
    for box in boxes:
        if box.type == b"senc" or (box.type == b"uuid" and box.uuid == PIFF_SAMPLE_ENCRYPTION_UUID):
            pos = box.start + box.header_size
            if pos + 4 <= box.end:
                data[pos + 1 : pos + 4] = b"\x00\x00\x00"
        patch_senc_flags(data, box.children)


def gather_track_ids_to_remove(parser: Mp4Parser) -> List[int]:
    moov = None
    for box in parser.root:
        if box.type == b"moov":
            moov = box
            break
    if moov is None:
        return []
    result: List[int] = []
    for trak in moov.children:
        if trak.type != b"trak":
            continue
        tkhd = parser.find_child(trak, b"tkhd")
        mdia = parser.find_child(trak, b"mdia")
        hdlr = parser.find_child(mdia, b"hdlr") if mdia else None
        if tkhd and hdlr:
            track_id = parse_tkhd(parser.data, tkhd)
            handler_type = parse_hdlr(parser.data, hdlr)
            if handler_type in TEXT_HANDLER_TYPES:
                result.append(track_id)
    return result


def slice_out_intervals(blob: bytes, intervals: List[Tuple[int, int]]) -> bytes:
    if not intervals:
        return blob
    out = bytearray()
    pos = 0
    for start, end in sorted(intervals):
        if pos < start:
            out.extend(blob[pos:start])
        pos = max(pos, end)
    if pos < len(blob):
        out.extend(blob[pos:])
    return bytes(out)


def remove_text_tracks_from_init_segment(source: bytes, parser: Mp4Parser) -> bytes:
    if any(box.type == b"mdat" for box in parser.root):
        return source
    track_ids_to_remove = set(gather_track_ids_to_remove(parser))
    if not track_ids_to_remove:
        return source
    moov = None
    for box in parser.root:
        if box.type == b"moov":
            moov = box
            break
    if moov is None:
        return source
    moov_bytes = bytes(source[moov.start : moov.end])
    moov_intervals: List[Tuple[int, int]] = []
    mvex_replacement = None
    mvex_start_local = None
    mvex_end_local = None
    for child in moov.children:
        if child.type == b"trak":
            tkhd = parser.find_child(child, b"tkhd")
            if tkhd and parse_tkhd(parser.data, tkhd) in track_ids_to_remove:
                moov_intervals.append((child.start - moov.start, child.end - moov.start))
        elif child.type == b"mvex":
            mvex_bytes = bytes(source[child.start : child.end])
            mvex_intervals: List[Tuple[int, int]] = []
            for trex in child.children:
                if trex.type != b"trex":
                    continue
                track_id = u32(parser.data, trex.start + trex.header_size + 4)
                if track_id in track_ids_to_remove:
                    mvex_intervals.append((trex.start - child.start, trex.end - child.start))
            if mvex_intervals:
                mvex_payload = slice_out_intervals(mvex_bytes[8:], [(a - 8, b - 8) for a, b in mvex_intervals])
                mvex_replacement = struct.pack(">I4s", 8 + len(mvex_payload), b"mvex") + mvex_payload
                mvex_start_local = child.start - moov.start
                mvex_end_local = child.end - moov.start
                moov_intervals.append((mvex_start_local, mvex_end_local))
    new_payload = bytearray()
    pos = 8
    for start, end in sorted(moov_intervals):
        if pos < start:
            new_payload.extend(moov_bytes[pos:start])
        if mvex_replacement is not None and start == mvex_start_local and end == mvex_end_local:
            new_payload.extend(mvex_replacement)
        pos = max(pos, end)
    if pos < len(moov_bytes):
        new_payload.extend(moov_bytes[pos:])
    new_moov = struct.pack(">I4s", 8 + len(new_payload), b"moov") + bytes(new_payload)
    out = bytearray()
    for box in parser.root:
        if box.type == b"moov":
            out.extend(new_moov)
        else:
            out.extend(source[box.start : box.end])
    return bytes(out)


def collect_detected_kids(tracks: Dict[int, TrackInfo], fragments: List[FragmentRun]) -> List[bytes]:
    seen = set()
    ordered: List[bytes] = []
    for track in tracks.values():
        if track.tenc and track.tenc.kid and track.tenc.kid not in seen:
            seen.add(track.tenc.kid)
            ordered.append(track.tenc.kid)
    for run in fragments:
        if run.tenc and run.tenc.kid and run.tenc.kid not in seen:
            seen.add(run.tenc.kid)
            ordered.append(run.tenc.kid)
    return ordered


def ensure_supplied_kids_match(detected_kids: List[bytes], keys_by_kid: Dict[bytes, bytes]):
    if not detected_kids or not keys_by_kid:
        return
    zero_kid = bytes(16)
    supplied = {kid for kid in keys_by_kid.keys() if kid != zero_kid}
    if not supplied:
        return
    if set(detected_kids) & supplied:
        return
    unique = []
    seen = set()
    for kid in detected_kids:
        if kid not in seen:
            seen.add(kid)
            unique.append(kid)
    if len(unique) == 1:
        print(f"The supplied KID does not match this file. The correct KID is: {unique[0].hex()}", file=sys.stderr)
        sys.exit(1)
    print(
        "The supplied KID does not match this file. The correct KIDs are: " + ", ".join(k.hex() for k in unique),
        file=sys.stderr,
    )
    sys.exit(1)


def collect_time_patches(data, boxes: List[Box], value: int, patches: List[BytePatch]):
    for box in boxes:
        if box.type in {b"mvhd", b"tkhd", b"mdhd"}:
            pos = box.start + box.header_size
            version = u8(data, pos)
            if version == 1:
                patches.append(BytePatch(pos + 4, struct.pack(">Q", value)))
                patches.append(BytePatch(pos + 12, struct.pack(">Q", value)))
            else:
                patches.append(BytePatch(pos + 4, struct.pack(">I", value & 0xFFFFFFFF)))
                patches.append(BytePatch(pos + 8, struct.pack(">I", value & 0xFFFFFFFF)))
        collect_time_patches(data, box.children, value, patches)


def collect_senc_flag_patches(boxes: List[Box], patches: List[BytePatch]):
    for box in boxes:
        if box.type == b"senc" or (box.type == b"uuid" and box.uuid == PIFF_SAMPLE_ENCRYPTION_UUID):
            pos = box.start + box.header_size
            if pos + 4 <= box.end:
                patches.append(BytePatch(pos + 1, b"\x00\x00\x00"))
        collect_senc_flag_patches(box.children, patches)


def unix_to_mp4_time(timestamp: int) -> int:
    return int(timestamp) + 2082844800


def collect_tfhd_sample_description_index_patches(
    data, parser: Mp4Parser, tracks: Dict[int, TrackInfo], patches: List[BytePatch]
):
    decrypted_track_ids = {track_id for track_id, track in tracks.items() if track.original_format}
    if not decrypted_track_ids:
        return
    for moof in [box for box in parser.root if box.type == b"moof"]:
        for traf in parser.find_children(moof, b"traf"):
            tfhd = parser.find_child(traf, b"tfhd")
            if not tfhd:
                continue
            pos = tfhd.start + tfhd.header_size
            flags = u24(data, pos + 1)
            pos += 4
            track_id = u32(data, pos)
            pos += 4
            if track_id not in decrypted_track_ids:
                continue
            if flags & 0x000001:
                pos += 8
            if flags & 0x000002:
                sample_description_index = u32(data, pos)
                if sample_description_index != 1:
                    patches.append(BytePatch(pos, struct.pack(">I", 1)))


def collect_metadata_patches(data, parser: Mp4Parser, tracks: Dict[int, TrackInfo], input_path: str) -> List[BytePatch]:
    patches: List[BytePatch] = []
    for track in tracks.values():
        if track.sample_entry_box and track.original_format:
            patches.append(BytePatch(track.sample_entry_box.start + 4, track.original_format))
    collect_senc_flag_patches(parser.root, patches)
    collect_tfhd_sample_description_index_patches(data, parser, tracks, patches)
    patches.sort(key=lambda x: (x.start, x.end))
    merged: List[BytePatch] = []
    for patch in patches:
        if not merged:
            merged.append(patch)
            continue
        prev = merged[-1]
        if patch.start < prev.end:
            fail("Overlapping metadata patches were generated")
        merged.append(patch)
    return merged


def collect_decrypt_tasks(
    tracks: Dict[int, TrackInfo],
    fragments: List[FragmentRun],
    keys_by_track: Dict[int, bytes],
    keys_by_kid: Dict[bytes, bytes],
) -> List[DecryptTask]:
    tasks: List[DecryptTask] = []
    skipped_without_iv = 0
    decrypted_any = False
    for track_id in sorted(tracks):
        track = tracks[track_id]
        if not track.tenc or not track.tenc.is_encrypted:
            continue
        if track.aux_info and len(track.aux_info) != len(track.sample_sizes):
            raise ValueError(f"Track {track.track_id} has mismatched sample encryption info")
        key = resolve_track_key(track, keys_by_track, keys_by_kid)
        for index, (offset, size) in enumerate(zip(track.sample_offsets, track.sample_sizes)):
            info = resolve_sample_info(track.aux_info, index, track.tenc)
            if info is None:
                skipped_without_iv += 1
                continue
            tasks.append(
                DecryptTask(
                    offset,
                    size,
                    key,
                    info,
                    track.scheme,
                    track.tenc,
                    track.codec_format,
                    track.nal_length_size,
                    track.nal_header_clear_bytes,
                )
            )
            decrypted_any = True
    for run in fragments:
        if not run.tenc or not run.tenc.is_encrypted:
            continue
        if run.aux_info and len(run.aux_info) != len(run.sample_sizes):
            raise ValueError(f"Fragment track {run.track_id} has mismatched sample encryption info")
        track = tracks.get(run.track_id)
        if not track:
            fail(f"Missing initialization track for fragment track {run.track_id}")
        key = resolve_track_key(track, keys_by_track, keys_by_kid)
        for index, (offset, size) in enumerate(zip(run.sample_offsets, run.sample_sizes)):
            info = resolve_sample_info(run.aux_info, index, run.tenc)
            if info is None:
                skipped_without_iv += 1
                continue
            tasks.append(
                DecryptTask(
                    offset,
                    size,
                    key,
                    info,
                    run.scheme,
                    run.tenc,
                    track.codec_format,
                    track.nal_length_size,
                    track.nal_header_clear_bytes,
                )
            )
            decrypted_any = True
    if skipped_without_iv:
        print(
            f"Skipped {skipped_without_iv} samples without IV/subsample metadata; they were preserved as clear samples"
        )
    if not decrypted_any:
        fail("No encrypted samples were decrypted")
    tasks.sort(key=lambda x: (x.start, x.end))
    previous_end = -1
    for task in tasks:
        if task.start < previous_end:
            fail("Overlapping encrypted sample ranges were detected")
        previous_end = task.end
    return tasks


def build_events(metadata_patches: List[BytePatch], decrypt_tasks: List[DecryptTask]) -> List[StreamEvent]:
    events: List[StreamEvent] = []
    for patch in metadata_patches:
        events.append(StreamEvent(patch.start, patch.end, "patch", patch))
    for task in decrypt_tasks:
        events.append(StreamEvent(task.start, task.end, "decrypt", task))
    events.sort(key=lambda x: (x.start, 0 if x.kind == "patch" else 1, x.end))
    previous_end = -1
    for event in events:
        if event.start < previous_end:
            fail("Overlapping stream events were generated")
        previous_end = event.end
    return events


def stream_copy_range(
    mm, out_file, start: int, end: int, progress: ProgressPrinter, chunk_size: int = DEFAULT_COPY_CHUNK
):
    pos = start
    while pos < end:
        chunk_end = min(end, pos + chunk_size)
        out_file.write(mm[pos:chunk_end])
        pos = chunk_end
        progress.update(pos)


def stream_write_data(out_file, data: bytes, end_offset: int, progress: ProgressPrinter):
    out_file.write(data)
    progress.update(end_offset)


def is_webm_file(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(4) == b"\x1a\x45\xdf\xa3"


def read_ebml_id(buf: bytes, off: int) -> Tuple[int, int, bytes]:
    first = buf[off]
    mask = 0x80
    length = 1
    while length <= 4 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 4 or off + length > len(buf):
        raise ValueError("Invalid EBML ID")
    raw = buf[off : off + length]
    value = 0
    for b in raw:
        value = (value << 8) | b
    return value, length, raw


def read_ebml_size(buf: bytes, off: int) -> Tuple[Optional[int], int, bool]:
    first = buf[off]
    mask = 0x80
    length = 1
    while length <= 8 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 8 or off + length > len(buf):
        raise ValueError("Invalid EBML size")
    raw = bytearray(buf[off : off + length])
    data_bits = 8 - length
    value = raw[0] & ((1 << data_bits) - 1)
    unknown = raw[0] == ((1 << data_bits) - 1) and all(b == 0xFF for b in raw[1:])
    for b in raw[1:]:
        value = (value << 8) | b
    return (None if unknown else value), length, unknown


def encode_ebml_size(value: int, preferred_len: Optional[int] = None, force_unknown: bool = False) -> bytes:
    if force_unknown:
        if preferred_len is None:
            preferred_len = 8
        if not 1 <= preferred_len <= 8:
            raise ValueError("Invalid unknown-size length")
        return bytes([((1 << (8 - preferred_len)) - 1) | (1 << (8 - preferred_len))]) + (b"\xff" * (preferred_len - 1))
    if value < 0:
        raise ValueError("EBML size cannot be negative")
    candidate_lengths = [preferred_len] if preferred_len else list(range(1, 9))
    for length in candidate_lengths:
        if length is None:
            continue
        if not 1 <= length <= 8:
            continue
        max_value = (1 << (7 * length)) - 2
        if value <= max_value:
            encoded = value.to_bytes(length, "big")
            leading = 1 << (8 - length)
            encoded = bytes([encoded[0] | leading]) + encoded[1:]
            return encoded
    raise ValueError(f"Value {value} is too large for EBML size encoding")


def parse_ebml_elements(buf: bytes, start: int, end: int) -> List[EbmlElement]:
    elements: List[EbmlElement] = []
    effective_end = min(end, len(buf))
    pos = start
    while pos < effective_end:
        if pos >= len(buf):
            break
        id_value, id_len, id_bytes = read_ebml_id(buf, pos)
        size_value, size_len, unknown = read_ebml_size(buf, pos + id_len)
        data_start = pos + id_len + size_len
        if data_start > effective_end:
            raise ValueError("EBML header exceeds parent boundary")
        if size_value is None:
            data_end = effective_end
        else:
            raw_end = data_start + size_value
            data_end = raw_end if raw_end <= effective_end else effective_end
        elements.append(
            EbmlElement(
                id_value, id_bytes, size_value, size_len, data_start, data_end, pos, data_start, data_end, unknown
            )
        )
        if data_end <= pos:
            raise ValueError("EBML parser did not advance")
        pos = data_end
    return elements


def parse_ebml_uint(payload: bytes) -> int:
    value = 0
    for b in payload:
        value = (value << 8) | b
    return value


def parse_ebml_string(payload: bytes) -> str:
    return payload.rstrip(b"\x00").decode("utf-8", "replace")


def strip_crc32_elements(payload: bytes) -> bytes:
    out = bytearray()
    cursor = 0
    for child in parse_ebml_elements(payload, 0, len(payload)):
        if cursor < child.header_start:
            out.extend(payload[cursor : child.header_start])
        if child.id_value != WEBM_ID_CRC32:
            out.extend(payload[child.header_start : child.end])
        cursor = child.end
    if cursor < len(payload):
        out.extend(payload[cursor:])
    return bytes(out)


def parse_vint_value(buf: bytes, off: int) -> Tuple[int, int, bytes]:
    first = buf[off]
    mask = 0x80
    length = 1
    while length <= 8 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 8 or off + length > len(buf):
        raise ValueError("Invalid VINT")
    value = first & (mask - 1)
    raw = buf[off : off + length]
    for i in range(1, length):
        value = (value << 8) | buf[off + i]
    return value, length, raw


def build_webm_counter_block(iv8: bytes) -> bytes:
    if len(iv8) != 8:
        raise ValueError("WebM IV must be 8 bytes")
    return iv8 + (b"\x00" * 8)


def parse_webm_signal_frame(frame_payload: bytes) -> Tuple[bytes, int, SampleAuxInfo]:
    if len(frame_payload) < 1:
        raise ValueError("Empty WebM frame payload")
    signal_byte = frame_payload[0]
    header_size = WEBM_SIGNAL_BYTE_SIZE
    if signal_byte & WEBM_ENCRYPTED_SIGNAL:
        header_size += WEBM_IV_SIZE
        if len(frame_payload) < header_size:
            raise ValueError("Encrypted WebM frame is too small to contain the IV")
        iv = frame_payload[1 : 1 + WEBM_IV_SIZE]
        subsamples: List[Tuple[int, int]] = []
        if signal_byte & WEBM_PARTITIONED_SIGNAL:
            header_size += WEBM_NUM_PARTITIONS_SIZE
            if len(frame_payload) < header_size:
                raise ValueError("Encrypted WebM frame is too small to contain partition metadata")
            num_partitions = frame_payload[1 + WEBM_IV_SIZE]
            offsets_start = header_size
            offsets_end = offsets_start + (num_partitions * WEBM_PARTITION_OFFSET_SIZE)
            if offsets_end > len(frame_payload):
                raise ValueError("Encrypted WebM frame is too small to contain partition offsets")
            data_start = offsets_end
            subsample_offset = 0
            encrypted_subsample = False
            clear_size = 0
            encrypted_size = 0
            cursor = offsets_start
            for partition_index in range(num_partitions):
                partition_offset = struct.unpack_from(">I", frame_payload, cursor)[0]
                cursor += 4
                if partition_offset < subsample_offset:
                    raise ValueError("Partition offsets are out of order")
                if encrypted_subsample:
                    encrypted_size = partition_offset - subsample_offset
                    subsamples.append((clear_size, encrypted_size))
                else:
                    clear_size = partition_offset - subsample_offset
                    if partition_index == (num_partitions - 1):
                        encrypted_size = len(frame_payload) - data_start - subsample_offset - clear_size
                        subsamples.append((clear_size, encrypted_size))
                subsample_offset = partition_offset
                encrypted_subsample = not encrypted_subsample
            if (num_partitions % 2) == 0:
                clear_size = len(frame_payload) - data_start - subsample_offset
                subsamples.append((clear_size, 0))
            return signal_byte.to_bytes(1, "big"), data_start, SampleAuxInfo(iv, subsamples)
        return signal_byte.to_bytes(1, "big"), header_size, SampleAuxInfo(iv, [])
    return signal_byte.to_bytes(1, "big"), header_size, SampleAuxInfo(b"", [])


def decrypt_webm_frame(frame_payload: bytes, key: bytes) -> bytes:
    _, data_offset, info = parse_webm_signal_frame(frame_payload)
    encoded_frame = frame_payload[data_offset:]
    if info.iv:
        return decrypt_cenc_ctr(encoded_frame, key, build_webm_counter_block(info.iv), info.subsamples)
    return encoded_frame


def is_webm_text_track(track: WebMTrack) -> bool:
    if track.track_type in {WEBM_TRACK_TYPE_SUBTITLE, WEBM_TRACK_TYPE_METADATA}:
        return True
    codec = track.codec_id.upper()
    return codec.startswith("D_WEBVTT") or codec.startswith("S_TEXT") or codec.startswith("S_VOBSUB")


def parse_webm_track_entry(track_entry_payload: bytes) -> WebMTrack:
    track = WebMTrack(track_number=-1)
    for child in parse_ebml_elements(track_entry_payload, 0, len(track_entry_payload)):
        payload = track_entry_payload[child.data_start : child.data_end]
        if child.id_value == WEBM_ID_TRACK_NUMBER:
            track.track_number = parse_ebml_uint(payload)
        elif child.id_value == WEBM_ID_TRACK_UID:
            track.track_uid = parse_ebml_uint(payload)
        elif child.id_value == WEBM_ID_TRACK_TYPE:
            track.track_type = parse_ebml_uint(payload)
        elif child.id_value == WEBM_ID_CODEC_ID:
            track.codec_id = parse_ebml_string(payload)
        elif child.id_value == WEBM_ID_NAME:
            track.name = parse_ebml_string(payload)
        elif child.id_value == WEBM_ID_LANGUAGE:
            track.language = parse_ebml_string(payload)
        elif child.id_value == WEBM_ID_CONTENT_ENCODINGS:
            track.content_encodings_start_rel = child.header_start
            track.content_encodings_end_rel = child.end
            for enc in parse_ebml_elements(track_entry_payload, child.data_start, child.data_end):
                if enc.id_value != WEBM_ID_CONTENT_ENCODING:
                    continue
                for enc_child in parse_ebml_elements(track_entry_payload, enc.data_start, enc.data_end):
                    if enc_child.id_value != WEBM_ID_CONTENT_ENCRYPTION:
                        continue
                    track.encrypted = True
                    for ce_child in parse_ebml_elements(track_entry_payload, enc_child.data_start, enc_child.data_end):
                        if ce_child.id_value == WEBM_ID_CONTENT_ENC_KEY_ID:
                            track.key_id = bytes(track_entry_payload[ce_child.data_start : ce_child.data_end])
    if track.track_number <= 0:
        raise ValueError("WebM track entry is missing TrackNumber")
    return track


def rewrite_webm_track_entry(
    track_entry_payload: bytes, decrypt_track_numbers: set, drop_text_tracks: bool
) -> Optional[bytes]:
    track_entry_payload = strip_crc32_elements(track_entry_payload)
    track = parse_webm_track_entry(track_entry_payload)
    if drop_text_tracks and is_webm_text_track(track):
        return None
    if track.track_number in decrypt_track_numbers and track.content_encodings_start_rel is not None:
        out = bytearray()
        out.extend(track_entry_payload[: track.content_encodings_start_rel])
        out.extend(track_entry_payload[track.content_encodings_end_rel :])
        return bytes(out)
    return track_entry_payload


def parse_webm_tracks(tracks_payload: bytes) -> Dict[int, WebMTrack]:
    tracks: Dict[int, WebMTrack] = {}
    for child in parse_ebml_elements(tracks_payload, 0, len(tracks_payload)):
        if child.id_value == WEBM_ID_TRACK_ENTRY:
            payload = tracks_payload[child.data_start : child.data_end]
            track = parse_webm_track_entry(payload)
            tracks[track.track_number] = track
    return tracks


def print_webm_tracks(tracks: Dict[int, WebMTrack]):
    print("Detected WebM tracks:")
    for track_number in sorted(tracks):
        track = tracks[track_number]
        kid_text = track.key_id.hex() if track.key_id else "-"
        kind = {
            WEBM_TRACK_TYPE_VIDEO: "video",
            WEBM_TRACK_TYPE_AUDIO: "audio",
            WEBM_TRACK_TYPE_SUBTITLE: "subtitle/caption",
            WEBM_TRACK_TYPE_METADATA: "metadata/description",
        }.get(track.track_type, f"type-{track.track_type}")
        print(
            f"  Track {track.track_number}: type={kind}, codec={track.codec_id or '-'}, "
            f"encrypted={'yes' if track.encrypted else 'no'}, kid={kid_text}, "
            f"name={track.name or '-'}, language={track.language or '-'}"
        )


def ensure_supplied_webm_kids_match(tracks: Dict[int, WebMTrack], keys_by_kid: Dict[bytes, bytes]):
    detected = [track.key_id for track in tracks.values() if track.key_id]
    if not detected or not keys_by_kid:
        return
    zero_kid = bytes(16)
    supplied = {kid for kid in keys_by_kid.keys() if kid != zero_kid}
    if not supplied:
        return
    if set(detected) & supplied:
        return
    unique = []
    seen = set()
    for kid in detected:
        if kid not in seen:
            seen.add(kid)
            unique.append(kid)
    if len(unique) == 1:
        print(f"The supplied KID does not match this file. The correct KID is: {unique[0].hex()}", file=sys.stderr)
        sys.exit(1)
    print(
        "The supplied KID does not match this file. The correct KIDs are: " + ", ".join(k.hex() for k in unique),
        file=sys.stderr,
    )
    sys.exit(1)


def encode_webm_element(
    id_bytes: bytes, payload: bytes, preferred_size_len: Optional[int] = None, force_unknown_size: bool = False
) -> bytes:
    return id_bytes + encode_ebml_size(len(payload), preferred_size_len, force_unknown_size) + payload


def rewrite_webm_block_payload(block_payload: bytes, track_keys: Dict[int, bytes]) -> bytes:
    track_number, vint_len, vint_raw = parse_vint_value(block_payload, 0)
    if len(block_payload) < vint_len + 3:
        raise ValueError("Block payload is too small")
    header_prefix = vint_raw + block_payload[vint_len : vint_len + 3]
    frame_payload = block_payload[vint_len + 3 :]
    key = track_keys.get(track_number)
    if key is None:
        return block_payload
    flags = block_payload[vint_len + 2]
    lacing = (flags >> 1) & 0x03
    if lacing != 0:
        fail(f"Encrypted WebM block uses unsupported lacing mode {lacing}")
    clear_frame = decrypt_webm_frame(frame_payload, key)
    return header_prefix + clear_frame


def rewrite_webm_cluster_payload(cluster_payload: bytes, track_keys: Dict[int, bytes]) -> bytes:
    cluster_payload = strip_crc32_elements(cluster_payload)
    out = bytearray()
    cursor = 0
    for child in parse_ebml_elements(cluster_payload, 0, len(cluster_payload)):
        if cursor < child.header_start:
            out.extend(cluster_payload[cursor : child.header_start])
        child_payload = cluster_payload[child.data_start : child.data_end]
        if child.id_value == WEBM_ID_SIMPLE_BLOCK:
            new_payload = rewrite_webm_block_payload(child_payload, track_keys)
            out.extend(encode_webm_element(child.id_bytes, new_payload, child.size_len))
        elif child.id_value == WEBM_ID_BLOCK_GROUP:
            new_group = bytearray()
            inner_cursor = 0
            for inner in parse_ebml_elements(child_payload, 0, len(child_payload)):
                if inner_cursor < inner.header_start:
                    new_group.extend(child_payload[inner_cursor : inner.header_start])
                inner_payload = child_payload[inner.data_start : inner.data_end]
                if inner.id_value == WEBM_ID_BLOCK:
                    rewritten_block = rewrite_webm_block_payload(inner_payload, track_keys)
                    new_group.extend(encode_webm_element(inner.id_bytes, rewritten_block, inner.size_len))
                else:
                    new_group.extend(child_payload[inner.header_start : inner.end])
                inner_cursor = inner.end
            if inner_cursor < len(child_payload):
                new_group.extend(child_payload[inner_cursor:])
            out.extend(encode_webm_element(child.id_bytes, bytes(new_group), child.size_len))
        else:
            out.extend(cluster_payload[child.header_start : child.end])
        cursor = child.end
    if cursor < len(cluster_payload):
        out.extend(cluster_payload[cursor:])
    return bytes(out)


def rewrite_webm_tracks_payload(
    tracks_payload: bytes, decrypt_track_numbers: set, drop_text_tracks: bool
) -> Tuple[bytes, Dict[int, WebMTrack]]:
    tracks_payload = strip_crc32_elements(tracks_payload)
    original_tracks = parse_webm_tracks(tracks_payload)
    out = bytearray()
    cursor = 0
    for child in parse_ebml_elements(tracks_payload, 0, len(tracks_payload)):
        if cursor < child.header_start:
            out.extend(tracks_payload[cursor : child.header_start])
        if child.id_value == WEBM_ID_TRACK_ENTRY:
            payload = tracks_payload[child.data_start : child.data_end]
            rewritten = rewrite_webm_track_entry(payload, decrypt_track_numbers, drop_text_tracks)
            if rewritten is not None:
                out.extend(encode_webm_element(child.id_bytes, rewritten, child.size_len))
        else:
            out.extend(tracks_payload[child.header_start : child.end])
        cursor = child.end
    if cursor < len(tracks_payload):
        out.extend(tracks_payload[cursor:])
    return bytes(out), original_tracks


def resolve_webm_track_keys(
    tracks: Dict[int, WebMTrack], keys_by_track: Dict[int, bytes], keys_by_kid: Dict[bytes, bytes]
) -> Dict[int, bytes]:
    resolved: Dict[int, bytes] = {}
    for track_number, track in tracks.items():
        if not track.encrypted:
            continue
        if track_number in keys_by_track:
            resolved[track_number] = keys_by_track[track_number]
            continue
        if track.key_id and track.key_id in keys_by_kid:
            resolved[track_number] = keys_by_kid[track.key_id]
            continue
        if len(keys_by_track) == 1:
            resolved[track_number] = next(iter(keys_by_track.values()))
            continue
        if len(keys_by_kid) == 1:
            resolved[track_number] = next(iter(keys_by_kid.values()))
            continue
        fail(f"No key found for WebM track {track_number}")
    if not resolved:
        fail("No encrypted WebM tracks were matched to the supplied keys")
    return resolved


def decrypt_webm_file(
    input_path: str,
    output_path: str,
    keys_by_track: Dict[int, bytes],
    keys_by_kid: Dict[bytes, bytes],
    show_tracks: bool,
    drop_text: bool,
):
    source = Path(input_path).read_bytes()
    top = parse_ebml_elements(source, 0, len(source))
    segment = None
    for element in top:
        if element.id_value == WEBM_ID_SEGMENT:
            segment = element
            break
    if segment is None:
        fail("WebM Segment element was not found")
    tracks_element = None
    for child in parse_ebml_elements(source, segment.data_start, segment.data_end):
        if child.id_value == WEBM_ID_TRACKS:
            tracks_element = child
            break
    if tracks_element is None:
        fail("WebM Tracks element was not found")
    tracks_payload = source[tracks_element.data_start : tracks_element.data_end]
    discovered_tracks = parse_webm_tracks(tracks_payload)
    if show_tracks:
        print_webm_tracks(discovered_tracks)
    ensure_supplied_webm_kids_match(discovered_tracks, keys_by_kid)
    track_keys = resolve_webm_track_keys(discovered_tracks, keys_by_track, keys_by_kid)
    decrypt_track_numbers = set(track_keys)

    output = bytearray()
    cursor = 0
    for element in top:
        if cursor < element.header_start:
            output.extend(source[cursor : element.header_start])
        if element.id_value != WEBM_ID_SEGMENT:
            output.extend(source[element.header_start : element.end])
            cursor = element.end
            continue
        segment_payload_out = bytearray()
        inner_cursor = element.data_start
        for child in parse_ebml_elements(source, element.data_start, element.data_end):
            if inner_cursor < child.header_start:
                segment_payload_out.extend(source[inner_cursor : child.header_start])
            child_payload = source[child.data_start : child.data_end]
            if child.id_value == WEBM_ID_TRACKS:
                rewritten_tracks, _ = rewrite_webm_tracks_payload(child_payload, decrypt_track_numbers, drop_text)
                segment_payload_out.extend(encode_webm_element(child.id_bytes, rewritten_tracks, child.size_len))
            elif child.id_value == WEBM_ID_CLUSTER:
                rewritten_cluster = rewrite_webm_cluster_payload(child_payload, track_keys)
                segment_payload_out.extend(encode_webm_element(child.id_bytes, rewritten_cluster, child.size_len))
            elif child.id_value in {WEBM_ID_SEEK_HEAD, WEBM_ID_CUES, WEBM_ID_CRC32}:
                pass
            else:
                segment_payload_out.extend(source[child.header_start : child.end])
            inner_cursor = child.end
        if inner_cursor < element.data_end:
            segment_payload_out.extend(source[inner_cursor : element.data_end])
        segment_header = element.id_bytes + encode_ebml_size(
            len(segment_payload_out), element.size_len, force_unknown=element.unknown_size
        )
        output.extend(segment_header)
        output.extend(segment_payload_out)
        cursor = element.end
    if cursor < len(source):
        output.extend(source[cursor:])
    Path(output_path).write_bytes(output)


def extract_webm_kids_quick(path: str, max_scan_bytes: int = 16 * 1024 * 1024) -> List[bytes]:
    data = Path(path).read_bytes()[:max_scan_bytes]
    results: List[bytes] = []

    def walk(offset, end):
        while offset < end and offset < len(data):
            eid, id_len, _ = read_ebml_id(data, offset)
            size, size_len, _ = read_ebml_size(data, offset + id_len)
            start = offset + id_len + size_len
            stop = min(start + (size if size is not None else len(data) - start), len(data))
            if eid == WEBM_ID_CONTENT_ENC_KEY_ID:
                kid = data[start:stop]
                if kid and kid not in results:
                    results.append(kid)
            if eid in (
                WEBM_ID_SEGMENT,
                WEBM_ID_TRACKS,
                WEBM_ID_TRACK_ENTRY,
                WEBM_ID_CONTENT_ENCODINGS,
                WEBM_ID_CONTENT_ENCODING,
                WEBM_ID_CONTENT_ENCRYPTION,
            ):
                walk(start, stop)
            offset = stop

    segment_pos = data.find(b"\x18\x53\x80\x67")
    if segment_pos != -1:
        eid, id_len, _ = read_ebml_id(data, segment_pos)
        size, size_len, _ = read_ebml_size(data, segment_pos + id_len)
        seg_start = segment_pos + id_len + size_len
        seg_end = min(seg_start + (size if size is not None else len(data) - seg_start), len(data))
        walk(seg_start, seg_end)
    return results


def describe_tracks(tracks: Dict[int, TrackInfo], sample_entry_types: Dict[int, bytes]) -> List[str]:
    lines = []
    for track_id in sorted(tracks):
        track = tracks[track_id]
        entry = sample_entry_types.get(track_id, b"").decode("ascii", "replace")
        handler = track.handler_type.decode("ascii", "replace")
        scheme = track.scheme.decode("ascii", "replace")
        encrypted = "yes" if track.tenc and track.tenc.is_encrypted else "no"
        lines.append(
            f"track={track_id} handler={handler or '-'} entry={entry or '-'} encrypted={encrypted} scheme={scheme or '-'}"
        )
    return lines


def parse_keys(values: List[str]) -> Tuple[Dict[int, bytes], Dict[bytes, bytes]]:
    keys_by_track: Dict[int, bytes] = {}
    keys_by_kid: Dict[bytes, bytes] = {}
    for item in values:
        if ":" not in item:
            raise ValueError("Each -k value must be in the form ID:KEY")
        left, right = item.split(":", 1)
        key = normalize_key(right)
        left_clean = left.strip().lower().replace("0x", "").replace("-", "")
        if len(left_clean) == 32 and all(c in "0123456789abcdef" for c in left_clean):
            keys_by_kid[bytes.fromhex(left_clean)] = key
        else:
            track_id = int(left.strip(), 10)
            if track_id <= 0:
                raise ValueError("Track ID must be greater than zero")
            keys_by_track[track_id] = key
    return keys_by_track, keys_by_kid


FP_PROGRESS_WIDTH = 38


def fp_be32(data, offset):
    return struct.unpack_from(">I", data, offset)[0]


def fp_be64(data, offset):
    return struct.unpack_from(">Q", data, offset)[0]


def fp_be16(data, offset):
    return struct.unpack_from(">H", data, offset)[0]


def fp_read_box_header(data, offset, limit):
    if offset + 8 > limit:
        return None
    size = fp_be32(data, offset)
    box_type = bytes(data[offset + 4 : offset + 8]).decode("latin1")
    header = 8
    if size == 1:
        if offset + 16 > limit:
            return None
        size = fp_be64(data, offset + 8)
        header = 16
    elif size == 0:
        size = limit - offset
    if size < header or offset + size > limit:
        return None
    return offset, offset + size, header, box_type


def fp_children(data, start, end):
    offset = start
    while offset + 8 <= end:
        header = fp_read_box_header(data, offset, end)
        if header is None:
            break
        box_start, box_end, box_header, box_type = header
        yield box_start, box_end, box_header, box_type
        offset = box_end


def fp_recursive_boxes(data, start, end, wanted):
    stack = [(start, end)]
    container_types = {
        "moov",
        "trak",
        "mdia",
        "minf",
        "stbl",
        "moof",
        "traf",
        "mvex",
        "edts",
        "dinf",
        "sinf",
        "schi",
        "udta",
    }
    while stack:
        current_start, current_end = stack.pop()
        for box_start, box_end, box_header, box_type in fp_children(data, current_start, current_end):
            if box_type in wanted:
                yield box_start, box_end, box_header, box_type
            if box_type in container_types:
                stack.append((box_start + box_header, box_end))
            elif box_type == "meta":
                stack.append((box_start + box_header + 4, box_end))
            elif box_type == "stsd":
                entry_offset = box_start + box_header + 8
                entry_count = fp_be32(data, box_start + box_header + 4) if box_start + box_header + 8 <= box_end else 0
                for _ in range(entry_count):
                    if entry_offset + 8 > box_end:
                        break
                    entry_size = fp_be32(data, entry_offset)
                    if entry_size < 8 or entry_offset + entry_size > box_end:
                        break
                    entry_type = bytes(data[entry_offset + 4 : entry_offset + 8]).decode("latin1")
                    skip = 8
                    if entry_type in {"avc1", "avc3", "encv", "hvc1", "hev1", "dvhe", "dvh1", "av01", "vp09"}:
                        skip = 86
                    elif entry_type in {"mp4a", "enca", "ac-3", "ec-3", "Opus", "fLaC"}:
                        skip = 36
                    stack.append((entry_offset + skip, entry_offset + entry_size))
                    entry_offset += entry_size


def fp_parse_fullbox(data, box_start, box_header):
    value = fp_be32(data, box_start + box_header)
    return value >> 24, value & 0x00FFFFFF


def fp_normalize_hex(value):
    value = value.strip().replace("-", "").replace(" ", "")
    if value.startswith("0x") or value.startswith("0X"):
        value = value[2:]
    return value.lower()


def fp_parse_keys(values):
    result = {}
    for item in values:
        if ":" in item:
            kid, key = item.split(":", 1)
        else:
            kid, key = "00000000000000000000000000000000", item
        kid = fp_normalize_hex(kid)
        key = fp_normalize_hex(key)
        if len(kid) != 32:
            raise ValueError("KID must be 16 bytes as hex.")
        if len(key) != 32:
            raise ValueError("KEY must be 16 bytes as hex.")
        result[kid] = bytes.fromhex(key)
    return result


def fp_make_aes_cbc_decryptor(key, iv):
    if FP_AES is not None:
        cipher = FP_AES.new(key, FP_AES.MODE_CBC, iv)
        return cipher.decrypt
    if Cipher is not None:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()

        def run(data):
            return decryptor.update(data) + decryptor.finalize()

        return run
    fail("Install pycryptodome or cryptography.")


def fp_make_aes_ctr_decryptor(key, iv):
    if FP_AES is not None:
        cipher = FP_AES.new(key, FP_AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(iv, "big"))
        return cipher.decrypt
    if Cipher is not None:
        cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
        decryptor = cipher.decryptor()

        def run(data):
            return decryptor.update(data) + decryptor.finalize()

        return run
    fail("Install pycryptodome or cryptography.")


def fp_decrypt_ctr_range(buffer, start, size, key, iv):
    if size <= 0:
        return
    decrypt = fp_make_aes_ctr_decryptor(key, iv)
    buffer[start : start + size] = decrypt(bytes(buffer[start : start + size]))


def fp_decrypt_cbc_full_range(buffer, start, size, key, iv):
    full = size - (size % 16)
    if full <= 0:
        return
    decrypt = fp_make_aes_cbc_decryptor(key, iv)
    buffer[start : start + full] = decrypt(bytes(buffer[start : start + full]))


def fp_decrypt_cbc_pattern_range(buffer, start, size, key, iv, crypt_blocks, skip_blocks):
    if size < 16 or crypt_blocks <= 0:
        return
    if skip_blocks <= 0:
        fp_decrypt_cbc_full_range(buffer, start, size, key, iv)
        return
    full = size - (size % 16)
    encrypted_positions = []
    chunks = []
    offset = 0
    crypt_bytes = crypt_blocks * 16
    skip_bytes = skip_blocks * 16
    while offset + 16 <= full:
        take = min(crypt_bytes, full - offset)
        if take > 0:
            encrypted_positions.append((start + offset, take))
            chunks.append(bytes(buffer[start + offset : start + offset + take]))
        offset += take + skip_bytes
    if not chunks:
        return
    encrypted_blob = b"".join(chunks)
    if len(encrypted_blob) % 16:
        encrypted_blob = encrypted_blob[: len(encrypted_blob) - (len(encrypted_blob) % 16)]
    if not encrypted_blob:
        return
    decrypt = fp_make_aes_cbc_decryptor(key, iv)
    decrypted_blob = decrypt(encrypted_blob)
    cursor = 0
    remaining = len(decrypted_blob)
    for position, length in encrypted_positions:
        length = min(length, remaining - cursor)
        if length <= 0:
            break
        buffer[position : position + length] = decrypted_blob[cursor : cursor + length]
        cursor += length


def fp_parse_tkhd_track_id(data, trak_start, trak_end):
    for box_start, box_end, box_header, box_type in fp_children(data, trak_start, trak_end):
        if box_type == "tkhd":
            version, flags = fp_parse_fullbox(data, box_start, box_header)
            offset = box_start + box_header + 4
            if version == 1:
                return fp_be32(data, offset + 16)
            return fp_be32(data, offset + 8)
    return None


def fp_parse_hdlr_type(data, trak_start, trak_end):
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(data, trak_start, trak_end, {"hdlr"}):
        return bytes(data[box_start + box_header + 8 : box_start + box_header + 12]).decode("latin1")
    return "----"


def fp_parse_sample_entry_and_protection(data, trak_start, trak_end):
    entry_type = "----"
    scheme = "-"
    default_kid = "00000000000000000000000000000000"
    default_is_protected = 0
    default_iv_size = 0
    default_constant_iv = b""
    crypt_blocks = 0
    skip_blocks = 0
    original_format = ""
    sample_entry_start = None
    tenc_is_protected_offset = None
    tenc_iv_size_offset = None
    for stsd_start, stsd_end, stsd_header, stsd_type in fp_recursive_boxes(data, trak_start, trak_end, {"stsd"}):
        entry_count = fp_be32(data, stsd_start + stsd_header + 4)
        entry_offset = stsd_start + stsd_header + 8
        if entry_count > 0 and entry_offset + 8 <= stsd_end:
            entry_size = fp_be32(data, entry_offset)
            entry_type = bytes(data[entry_offset + 4 : entry_offset + 8]).decode("latin1")
            sample_entry_start = entry_offset
    for frma_start, frma_end, frma_header, frma_type in fp_recursive_boxes(data, trak_start, trak_end, {"frma"}):
        if frma_start + frma_header + 4 <= frma_end:
            original_format = bytes(data[frma_start + frma_header : frma_start + frma_header + 4]).decode("latin1")
    for schm_start, schm_end, schm_header, schm_type in fp_recursive_boxes(data, trak_start, trak_end, {"schm"}):
        scheme = bytes(data[schm_start + schm_header + 4 : schm_start + schm_header + 8]).decode("latin1")
    for tenc_start, tenc_end, tenc_header, tenc_type in fp_recursive_boxes(data, trak_start, trak_end, {"tenc"}):
        version, flags = fp_parse_fullbox(data, tenc_start, tenc_header)
        offset = tenc_start + tenc_header + 4
        if version == 0:
            tenc_is_protected_offset = offset + 2
            tenc_iv_size_offset = offset + 3
            default_is_protected = data[offset + 2]
            default_iv_size = data[offset + 3]
            default_kid = bytes(data[offset + 4 : offset + 20]).hex()
            offset += 20
        else:
            packed = data[offset + 1]
            crypt_blocks = packed >> 4
            skip_blocks = packed & 15
            tenc_is_protected_offset = offset + 2
            tenc_iv_size_offset = offset + 3
            default_is_protected = data[offset + 2]
            default_iv_size = data[offset + 3]
            default_kid = bytes(data[offset + 4 : offset + 20]).hex()
            offset += 20
        if default_is_protected and default_iv_size == 0 and offset < tenc_end:
            iv_length = data[offset]
            offset += 1
            default_constant_iv = bytes(data[offset : offset + iv_length])
    return (
        entry_type,
        scheme,
        default_kid,
        default_is_protected,
        default_iv_size,
        default_constant_iv,
        crypt_blocks,
        skip_blocks,
        original_format,
        sample_entry_start,
        tenc_is_protected_offset,
        tenc_iv_size_offset,
    )


def fp_parse_moov(data, moov_start, moov_end):
    tracks = {}
    trex = {}
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(data, moov_start, moov_end, {"trex"}):
        offset = box_start + box_header + 4
        track_id = fp_be32(data, offset)
        trex[track_id] = {
            "default_sample_description_index": fp_be32(data, offset + 4),
            "default_sample_duration": fp_be32(data, offset + 8),
            "default_sample_size": fp_be32(data, offset + 12),
            "default_sample_flags": fp_be32(data, offset + 16),
        }
    for trak_start, trak_end, trak_header, trak_type in fp_children(data, moov_start + 8, moov_end):
        if trak_type != "trak":
            continue
        track_id = fp_parse_tkhd_track_id(data, trak_start + trak_header, trak_end)
        if track_id is None:
            continue
        handler = fp_parse_hdlr_type(data, trak_start + trak_header, trak_end)
        (
            entry_type,
            scheme,
            default_kid,
            protected,
            iv_size,
            constant_iv,
            crypt_blocks,
            skip_blocks,
            original_format,
            sample_entry_start,
            tenc_is_protected_offset,
            tenc_iv_size_offset,
        ) = fp_parse_sample_entry_and_protection(data, trak_start + trak_header, trak_end)
        tracks[track_id] = {
            "id": track_id,
            "handler": handler,
            "entry_type": entry_type,
            "scheme": scheme,
            "kid": default_kid,
            "encrypted": bool(protected),
            "iv_size": iv_size,
            "constant_iv": constant_iv,
            "crypt_blocks": crypt_blocks,
            "skip_blocks": skip_blocks,
            "trex": trex.get(track_id, {}),
            "original_format": original_format,
            "sample_entry_start": sample_entry_start,
            "tenc_is_protected_offset": tenc_is_protected_offset,
            "tenc_iv_size_offset": tenc_iv_size_offset,
        }
    return tracks


def fp_parse_tfhd(data, box_start, box_end, box_header):
    version, flags = fp_parse_fullbox(data, box_start, box_header)
    offset = box_start + box_header + 4
    track_id = fp_be32(data, offset)
    offset += 4
    result = {"track_id": track_id, "flags": flags}
    if flags & 0x000001:
        result["base_data_offset"] = fp_be64(data, offset)
        offset += 8
    if flags & 0x000002:
        result["sample_description_index"] = fp_be32(data, offset)
        offset += 4
    if flags & 0x000008:
        result["default_sample_duration"] = fp_be32(data, offset)
        offset += 4
    if flags & 0x000010:
        result["default_sample_size"] = fp_be32(data, offset)
        offset += 4
    if flags & 0x000020:
        result["default_sample_flags"] = fp_be32(data, offset)
        offset += 4
    return result


def fp_parse_trun(data, box_start, box_end, box_header):
    version, flags = fp_parse_fullbox(data, box_start, box_header)
    offset = box_start + box_header + 4
    sample_count = fp_be32(data, offset)
    offset += 4
    data_offset = None
    first_sample_flags = None
    if flags & 0x000001:
        data_offset = struct.unpack_from(">i", data, offset)[0]
        offset += 4
    if flags & 0x000004:
        first_sample_flags = fp_be32(data, offset)
        offset += 4
    samples = []
    for index in range(sample_count):
        sample = {}
        if flags & 0x000100:
            sample["duration"] = fp_be32(data, offset)
            offset += 4
        if flags & 0x000200:
            sample["size"] = fp_be32(data, offset)
            offset += 4
        if flags & 0x000400:
            sample["flags"] = fp_be32(data, offset)
            offset += 4
        elif index == 0 and first_sample_flags is not None:
            sample["flags"] = first_sample_flags
        if flags & 0x000800:
            if version == 0:
                sample["composition_time_offset"] = fp_be32(data, offset)
            else:
                sample["composition_time_offset"] = struct.unpack_from(">i", data, offset)[0]
            offset += 4
        samples.append(sample)
    return sample_count, data_offset, samples


def fp_parse_senc(data, box_start, box_end, box_header, iv_size, constant_iv):
    version, flags = fp_parse_fullbox(data, box_start, box_header)
    offset = box_start + box_header + 4
    sample_count = fp_be32(data, offset)
    offset += 4
    entries = []
    for _ in range(sample_count):
        iv = constant_iv if iv_size == 0 else bytes(data[offset : offset + iv_size])
        if iv_size:
            offset += iv_size
        if len(iv) == 8:
            iv = iv + b"\x00" * 8
        subsamples = []
        if flags & 0x000002:
            subsample_count = fp_be16(data, offset)
            offset += 2
            for _ in range(subsample_count):
                clear_size = fp_be16(data, offset)
                encrypted_size = fp_be32(data, offset + 2)
                offset += 6
                subsamples.append((clear_size, encrypted_size))
        entries.append((iv, subsamples))
    return entries


def fp_print_progress(index, total, start_time):
    if total <= 0:
        ratio = 1.0
    else:
        ratio = max(0.0, min(1.0, index / total))
    filled = int(FP_PROGRESS_WIDTH * ratio)
    bar = "■" * filled + " " * (FP_PROGRESS_WIDTH - filled)
    elapsed = max(0.0, time.time() - start_time)
    remaining = 0.0 if ratio <= 0 else max(0.0, elapsed * (1.0 - ratio) / ratio)
    elapsed_s = int(round(elapsed))
    remaining_s = int(round(remaining))
    eh, er = divmod(elapsed_s, 3600)
    em, es = divmod(er, 60)
    rh, rr = divmod(remaining_s, 3600)
    rm, rs = divmod(rr, 60)
    sys.stdout.write(
        f"\r[{bar}] {ratio * 100:6.2f}% (elapsed: {eh:02d}:{em:02d}:{es:02d}, remaining: {rh:02d}:{rm:02d}:{rs:02d})"
    )
    sys.stdout.flush()


def fp_decrypt_sample(buffer, sample_start, sample_size, sample_aux, track, key):
    iv, subsamples = sample_aux
    scheme = track["scheme"].lower()
    crypt_blocks = track["crypt_blocks"]
    skip_blocks = track["skip_blocks"]
    if not subsamples:
        if scheme in ("cenc", "cens"):
            fp_decrypt_ctr_range(buffer, sample_start, sample_size, key, iv)
        else:
            fp_decrypt_cbc_pattern_range(buffer, sample_start, sample_size, key, iv, crypt_blocks, skip_blocks)
        return
    cursor = sample_start
    for clear_size, encrypted_size in subsamples:
        cursor += clear_size
        if encrypted_size > 0:
            if scheme in ("cenc", "cens"):
                fp_decrypt_ctr_range(buffer, cursor, encrypted_size, key, iv)
            else:
                fp_decrypt_cbc_pattern_range(buffer, cursor, encrypted_size, key, iv, crypt_blocks, skip_blocks)
        cursor += encrypted_size


def fp_collect_fragments(data, tracks):
    fragments = []
    total_samples = 0
    file_size = len(data)
    offset = 0
    while offset + 8 <= file_size:
        header = fp_read_box_header(data, offset, file_size)
        if header is None:
            break
        box_start, box_end, box_header, box_type = header
        if box_type == "moof":
            next_header = fp_read_box_header(data, box_end, file_size)
            mdat_start = None
            mdat_data_start = None
            if next_header is not None and next_header[3] == "mdat":
                mdat_start = next_header[0]
                mdat_data_start = next_header[0] + next_header[2]
            trafs = []
            for traf_start, traf_end, traf_header, traf_type in fp_children(data, box_start + box_header, box_end):
                if traf_type != "traf":
                    continue
                tfhd = None
                truns = []
                senc_entries = None
                for child_start, child_end, child_header, child_type in fp_children(
                    data, traf_start + traf_header, traf_end
                ):
                    if child_type == "tfhd":
                        tfhd = fp_parse_tfhd(data, child_start, child_end, child_header)
                    elif child_type == "trun":
                        truns.append(fp_parse_trun(data, child_start, child_end, child_header))
                    elif child_type == "senc":
                        current_track = tracks.get(tfhd["track_id"]) if tfhd else None
                        if current_track:
                            senc_entries = fp_parse_senc(
                                data,
                                child_start,
                                child_end,
                                child_header,
                                current_track["iv_size"],
                                current_track["constant_iv"],
                            )
                if tfhd and truns:
                    trafs.append((tfhd, truns, senc_entries))
            for tfhd, truns, senc_entries in trafs:
                track_id = tfhd["track_id"]
                track = tracks.get(track_id)
                if not track or not track["encrypted"]:
                    continue
                trex = track.get("trex", {})
                default_sample_size = tfhd.get("default_sample_size", trex.get("default_sample_size", 0))
                aux_index = 0
                for sample_count, data_offset, samples in truns:
                    base = tfhd.get("base_data_offset", box_start)
                    sample_offset = base + data_offset if data_offset is not None else mdat_data_start
                    if sample_offset is None:
                        continue
                    fragment_samples = []
                    for sample in samples:
                        sample_size = sample.get("size", default_sample_size)
                        if sample_size <= 0:
                            continue
                        if senc_entries is None or aux_index >= len(senc_entries):
                            sample_offset += sample_size
                            aux_index += 1
                            continue
                        sample_aux = senc_entries[aux_index]
                        fragment_samples.append((sample_offset, sample_size, sample_aux, track_id))
                        sample_offset += sample_size
                        aux_index += 1
                    fragments.extend(fragment_samples)
                    total_samples += len(fragment_samples)
        offset = box_end
    return fragments, total_samples


def fp_is_text_or_caption_track(track):
    handler = str(track.get("handler", "")).lower()
    entry_type = str(track.get("entry_type", "")).lower()
    return handler in {"text", "sbtl", "subt", "clcp"} or entry_type in {"c608", "tx3g", "wvtt", "stpp", "sbtt", "enct"}


def fp_disable_text_tracks_in_place(data):
    moov_start = None
    moov_end = None
    for box_start, box_end, box_header, box_type in fp_children(data, 0, len(data)):
        if box_type == "moov":
            moov_start = box_start
            moov_end = box_end
            break
    if moov_start is None:
        return set()
    text_track_ids = set()
    for trak_start, trak_end, trak_header, trak_type in fp_children(data, moov_start + 8, moov_end):
        if trak_type != "trak":
            continue
        track_id = fp_parse_tkhd_track_id(data, trak_start + trak_header, trak_end)
        handler = fp_parse_hdlr_type(data, trak_start + trak_header, trak_end).lower()
        (
            entry_type,
            scheme,
            default_kid,
            protected,
            iv_size,
            constant_iv,
            crypt_blocks,
            skip_blocks,
            original_format,
            sample_entry_start,
            tenc_is_protected_offset,
            tenc_iv_size_offset,
        ) = fp_parse_sample_entry_and_protection(data, trak_start + trak_header, trak_end)
        if track_id is not None and (
            handler in {"text", "sbtl", "subt", "clcp"}
            or entry_type.lower() in {"c608", "tx3g", "wvtt", "stpp", "sbtt", "enct"}
        ):
            text_track_ids.add(track_id)
            data[trak_start + 4 : trak_start + 8] = b"free"
    if not text_track_ids:
        return text_track_ids
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(data, moov_start, moov_end, {"trex"}):
        if box_start + box_header + 8 <= box_end:
            track_id = fp_be32(data, box_start + box_header + 4)
            if track_id in text_track_ids:
                data[box_start + 4 : box_start + 8] = b"free"
    return text_track_ids


def fp_patch_decrypted_mp4_metadata(data, tracks):
    for track_id in sorted(tracks):
        track = tracks[track_id]
        if fp_is_text_or_caption_track(track):
            continue
        if not track.get("encrypted"):
            continue
        original_format = track.get("original_format") or ""
        sample_entry_start = track.get("sample_entry_start")
        if original_format and sample_entry_start is not None and sample_entry_start + 8 <= len(data):
            data[sample_entry_start + 4 : sample_entry_start + 8] = original_format.encode("latin1")[:4]
        tenc_is_protected_offset = track.get("tenc_is_protected_offset")
        tenc_iv_size_offset = track.get("tenc_iv_size_offset")
        if tenc_is_protected_offset is not None and 0 <= tenc_is_protected_offset < len(data):
            data[tenc_is_protected_offset] = 0
        if tenc_iv_size_offset is not None and 0 <= tenc_iv_size_offset < len(data):
            data[tenc_iv_size_offset] = 0
    protection_boxes = {"sinf", "schm", "schi", "tenc", "senc", "saiz", "saio", "pssh"}
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(data, 0, len(data), protection_boxes):
        if box_start + 8 <= len(data):
            data[box_start + 4 : box_start + 8] = b"free"
        if box_type == "senc":
            fullbox_offset = box_start + box_header
            sample_count_offset = fullbox_offset + 4
            if sample_count_offset + 4 <= box_end:
                data[fullbox_offset + 1 : fullbox_offset + 4] = b"\x00\x00\x00"
                data[sample_count_offset : sample_count_offset + 4] = b"\x00\x00\x00\x00"
        elif box_type in {"saiz", "saio", "tenc", "schm"}:
            fullbox_offset = box_start + box_header
            if fullbox_offset + 4 <= box_end:
                data[fullbox_offset + 1 : fullbox_offset + 4] = b"\x00\x00\x00"


def fp_collect_text_track_patches(data):
    patches = []
    moov_start = None
    moov_end = None
    for box_start, box_end, box_header, box_type in fp_children(data, 0, len(data)):
        if box_type == "moov":
            moov_start = box_start
            moov_end = box_end
            break
    if moov_start is None:
        return patches
    text_track_ids = set()
    for trak_start, trak_end, trak_header, trak_type in fp_children(data, moov_start + 8, moov_end):
        if trak_type != "trak":
            continue
        track_id = fp_parse_tkhd_track_id(data, trak_start + trak_header, trak_end)
        handler = fp_parse_hdlr_type(data, trak_start + trak_header, trak_end).lower()
        (
            entry_type,
            scheme,
            default_kid,
            protected,
            iv_size,
            constant_iv,
            crypt_blocks,
            skip_blocks,
            original_format,
            sample_entry_start,
            tenc_is_protected_offset,
            tenc_iv_size_offset,
        ) = fp_parse_sample_entry_and_protection(data, trak_start + trak_header, trak_end)
        if track_id is not None and (
            handler in {"text", "sbtl", "subt", "clcp"}
            or entry_type.lower() in {"c608", "tx3g", "wvtt", "stpp", "sbtt", "enct"}
        ):
            text_track_ids.add(track_id)
            patches.append((trak_start + 4, b"free"))
    if not text_track_ids:
        return patches
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(data, moov_start, moov_end, {"trex"}):
        if box_start + box_header + 8 <= box_end:
            track_id = fp_be32(data, box_start + box_header + 4)
            if track_id in text_track_ids:
                patches.append((box_start + 4, b"free"))
    return patches


def fp_collect_decrypted_mp4_metadata_patches(data, tracks):
    patches = []
    for track_id in sorted(tracks):
        track = tracks[track_id]
        if fp_is_text_or_caption_track(track):
            continue
        if not track.get("encrypted"):
            continue
        original_format = track.get("original_format") or ""
        sample_entry_start = track.get("sample_entry_start")
        if original_format and sample_entry_start is not None and sample_entry_start + 8 <= len(data):
            patches.append((sample_entry_start + 4, original_format.encode("latin1")[:4]))
        tenc_is_protected_offset = track.get("tenc_is_protected_offset")
        tenc_iv_size_offset = track.get("tenc_iv_size_offset")
        if tenc_is_protected_offset is not None and 0 <= tenc_is_protected_offset < len(data):
            patches.append((tenc_is_protected_offset, b"\x00"))
        if tenc_iv_size_offset is not None and 0 <= tenc_iv_size_offset < len(data):
            patches.append((tenc_iv_size_offset, b"\x00"))
    protection_boxes = {"sinf", "schm", "schi", "tenc", "senc", "saiz", "saio", "pssh"}
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(data, 0, len(data), protection_boxes):
        if box_start + 8 <= len(data):
            patches.append((box_start + 4, b"free"))
        if box_type == "senc":
            fullbox_offset = box_start + box_header
            sample_count_offset = fullbox_offset + 4
            if sample_count_offset + 4 <= box_end:
                patches.append((fullbox_offset + 1, b"\x00\x00\x00"))
                patches.append((sample_count_offset, b"\x00\x00\x00\x00"))
        elif box_type in {"saiz", "saio", "tenc", "schm"}:
            fullbox_offset = box_start + box_header
            if fullbox_offset + 4 <= box_end:
                patches.append((fullbox_offset + 1, b"\x00\x00\x00"))
    return patches


def fp_decrypt_sample_to_bytes(data, sample_start, sample_size, sample_aux, track, key):
    sample = bytearray(data[sample_start : sample_start + sample_size])
    fp_decrypt_sample(sample, 0, sample_size, sample_aux, track, key)
    return bytes(sample)


def fp_prepare_patch_events(patches):
    events = []
    for position, payload in patches:
        if payload:
            events.append((position, position + len(payload), "patch", payload))
    events.sort(key=lambda item: (item[0], item[1]))
    merged = []
    for event in events:
        if merged and event[0] < merged[-1][1]:
            if event[0] == merged[-1][0] and event[3] == merged[-1][3]:
                continue
            fail("Overlapping output metadata patches were generated")
        merged.append(event)
    return merged


def fp_prepare_decrypt_events(fragments, tracks, fast_keys):
    events = []
    for sample_start, sample_size, sample_aux, track_id in fragments:
        track = tracks[track_id]
        key = (
            fast_keys.get(str(track_id))
            or fast_keys.get(track["kid"])
            or fast_keys.get("00000000000000000000000000000000")
        )
        if key is None:
            fail(f"Missing key for KID {track['kid']}")
        events.append(
            (sample_start, sample_start + sample_size, "decrypt", (sample_start, sample_size, sample_aux, track, key))
        )
    events.sort(key=lambda item: (item[0], item[1]))
    previous_end = -1
    for event in events:
        if event[0] < previous_end:
            fail("Overlapping encrypted sample ranges were detected")
        previous_end = event[1]
    return events


def fp_stream_decrypt_to_output(data, output_path, patch_events, decrypt_events):
    events = patch_events + decrypt_events
    events.sort(key=lambda item: (item[0], 0 if item[2] == "patch" else 1, item[1]))
    previous_end = -1
    for event in events:
        if event[0] < previous_end:
            fail("Overlapping stream events were generated")
        previous_end = event[1]
    total_samples = len(decrypt_events)
    start_time = time.time()
    processed = 0
    next_progress = 0.0
    cursor = 0
    fp_print_progress(0, max(total_samples, 1), start_time)
    with open(output_path, "wb") as out_file:
        for event_start, event_end, event_kind, event_payload in events:
            if cursor < event_start:
                position = cursor
                while position < event_start:
                    chunk_end = min(event_start, position + DEFAULT_COPY_CHUNK)
                    out_file.write(data[position:chunk_end])
                    position = chunk_end
            if event_kind == "patch":
                out_file.write(event_payload)
            else:
                sample_start, sample_size, sample_aux, track, key = event_payload
                out_file.write(fp_decrypt_sample_to_bytes(data, sample_start, sample_size, sample_aux, track, key))
                processed += 1
                ratio = processed / total_samples if total_samples else 1.0
                if ratio >= next_progress or processed == total_samples:
                    fp_print_progress(processed, total_samples, start_time)
                    next_progress = ratio + 0.01
            cursor = event_end
        if cursor < len(data):
            position = cursor
            while position < len(data):
                chunk_end = min(len(data), position + DEFAULT_COPY_CHUNK)
                out_file.write(data[position:chunk_end])
                position = chunk_end
    fp_print_progress(total_samples, max(total_samples, 1), start_time)
    print()


def decrypt_mp4_file(
    input_path: str,
    output_path: str,
    keys_by_track: Dict[int, bytes],
    keys_by_kid: Dict[bytes, bytes],
    show_tracks: bool = False,
    drop_text: bool = True,
):
    if not os.path.isfile(input_path):
        fail("Input file does not exist")

    fast_keys: Dict[str, bytes] = {}
    for track_id, key in keys_by_track.items():
        fast_keys[str(track_id)] = key
    for kid, key in keys_by_kid.items():
        fast_keys[kid.hex()] = key
    if len(fast_keys) == 1:
        only_key = next(iter(fast_keys.values()))
        fast_keys.setdefault("00000000000000000000000000000000", only_key)

    with open(input_path, "rb") as file_handle:
        data = mmap.mmap(file_handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            moov_start = None
            moov_end = None
            for box_start, box_end, box_header, box_type in fp_children(data, 0, len(data)):
                if box_type == "moov":
                    moov_start = box_start
                    moov_end = box_end
                    break
            if moov_start is None:
                fail("No moov box found")

            tracks = fp_parse_moov(data, moov_start, moov_end)
            ensure_supplied_kids_match(
                [
                    bytes.fromhex(track["kid"])
                    for track in tracks.values()
                    if track.get("encrypted") and track.get("kid")
                ],
                keys_by_kid,
            )
            if show_tracks:
                print("Detected tracks:")
                for track_id in sorted(tracks):
                    track = tracks[track_id]
                    if fp_is_text_or_caption_track(track):
                        continue
                    encrypted = "yes" if track["encrypted"] else "no"
                    print(
                        f"  track={track_id} handler={track['handler']} entry={track['entry_type']} encrypted={encrypted} scheme={track['scheme']}"
                    )

            fragments, total_samples = fp_collect_fragments(data, tracks)
            if total_samples <= 0:
                with open(output_path, "wb") as out_file:
                    out_file.write(data[:])
                print("No encrypted fragmented samples found")
                return

            patches = []
            if drop_text:
                patches.extend(fp_collect_text_track_patches(data))
            patches.extend(fp_collect_decrypted_mp4_metadata_patches(data, tracks))
            patch_events = fp_prepare_patch_events(patches)
            decrypt_events = fp_prepare_decrypt_events(fragments, tracks, fast_keys)
            fp_stream_decrypt_to_output(data, output_path, patch_events, decrypt_events)
            print("Decrypted successfully")
        finally:
            data.close()


def main():
    parser = argparse.ArgumentParser(prog="pydecrypt.py")
    parser.add_argument("-i", required=True, help="Input MP4, fragmented MP4, WebM, or Matroska file")
    parser.add_argument("-o", required=True, help="Output decrypted file")
    parser.add_argument(
        "-k",
        required=True,
        action="append",
        help="Track ID or 128-bit KID, followed by a 128-bit key, in the form ID:KEY",
    )
    parser.add_argument("--show-tracks", action="store_true", help="Print detected tracks before decryption")
    parser.add_argument(
        "--keep-text",
        action="store_true",
        help="Keep text/caption tracks instead of removing them from init metadata when supported",
    )
    args = parser.parse_args()

    keys_by_track, keys_by_kid = parse_keys(args.k)

    if is_webm_file(args.i):
        decrypt_webm_file(
            input_path=args.i,
            output_path=args.o,
            keys_by_track=keys_by_track,
            keys_by_kid=keys_by_kid,
            show_tracks=args.show_tracks,
            drop_text=not args.keep_text,
        )
        return

    decrypt_mp4_file(
        args.i, args.o, keys_by_track, keys_by_kid, show_tracks=args.show_tracks, drop_text=not args.keep_text
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
