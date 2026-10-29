import argparse
import mmap
import os
import struct
import sys
import time
from dataclasses import dataclass, field
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


def stream_copy_range_without_progress(source, out_file, start: int, end: int, chunk_size: int = DEFAULT_COPY_CHUNK):
    pos = start
    while pos < end:
        chunk_end = min(end, pos + chunk_size)
        out_file.write(source[pos:chunk_end])
        pos = chunk_end


def read_source_range(source, start: int, end: int) -> bytes:
    if end < start:
        raise ValueError("Invalid source range")
    return bytes(source[start:end])


def iter_ebml_elements(buf, start: int, end: int):
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
        yield EbmlElement(
            id_value, id_bytes, size_value, size_len, data_start, data_end, pos, data_start, data_end, unknown
        )
        if data_end <= pos:
            raise ValueError("EBML parser did not advance")
        pos = data_end


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


def rewrite_webm_cluster_payload(
    cluster_payload: bytes, track_keys: Dict[int, bytes], progress: ProgressPrinter, processed: List[int]
) -> bytes:
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
        processed[0] += child.end - child.header_start
        progress.update(processed[0])
    if cursor < len(cluster_payload):
        out.extend(cluster_payload[cursor:])
        processed[0] += len(cluster_payload) - cursor
        progress.update(processed[0])
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
    file_size = os.path.getsize(input_path)
    if file_size <= 0:
        fail("Input file is empty")

    with open(input_path, "rb") as in_file:
        with mmap.mmap(in_file.fileno(), 0, access=mmap.ACCESS_READ) as source:
            top = list(iter_ebml_elements(source, 0, file_size))
            segment = None
            for element in top:
                if element.id_value == WEBM_ID_SEGMENT:
                    segment = element
                    break
            if segment is None:
                fail("WebM Segment element was not found")

            tracks_element = None
            for child in iter_ebml_elements(source, segment.data_start, segment.data_end):
                if child.id_value == WEBM_ID_TRACKS:
                    tracks_element = child
                    break
            if tracks_element is None:
                fail("WebM Tracks element was not found")

            tracks_payload = read_source_range(source, tracks_element.data_start, tracks_element.data_end)
            discovered_tracks = parse_webm_tracks(tracks_payload)
            if show_tracks:
                print_webm_tracks(discovered_tracks)
            ensure_supplied_webm_kids_match(discovered_tracks, keys_by_kid)
            track_keys = resolve_webm_track_keys(discovered_tracks, keys_by_track, keys_by_kid)
            decrypt_track_numbers = set(track_keys)

            progress = ProgressPrinter(file_size)
            processed = [0]
            segment_size_len = segment.size_len if segment.size_len else 8
            if segment_size_len < 1 or segment_size_len > 8:
                segment_size_len = 8

            with open(output_path, "wb") as out_file:
                cursor = 0
                for element in top:
                    if cursor < element.header_start:
                        stream_copy_range_without_progress(source, out_file, cursor, element.header_start)
                        processed[0] += element.header_start - cursor
                        progress.update(processed[0])

                    if element.id_value != WEBM_ID_SEGMENT:
                        stream_copy_range_without_progress(source, out_file, element.header_start, element.end)
                        processed[0] += element.end - element.header_start
                        progress.update(processed[0])
                        cursor = element.end
                        continue

                    out_file.write(element.id_bytes)
                    out_file.write(encode_ebml_size(0, segment_size_len, force_unknown=True))
                    processed[0] += element.header_end - element.header_start
                    progress.update(processed[0])

                    inner_cursor = element.data_start
                    for child in iter_ebml_elements(source, element.data_start, element.data_end):
                        if inner_cursor < child.header_start:
                            stream_copy_range_without_progress(source, out_file, inner_cursor, child.header_start)
                            processed[0] += child.header_start - inner_cursor
                            progress.update(processed[0])

                        if child.id_value == WEBM_ID_TRACKS:
                            child_payload = read_source_range(source, child.data_start, child.data_end)
                            rewritten_tracks, _ = rewrite_webm_tracks_payload(
                                child_payload, decrypt_track_numbers, drop_text
                            )
                            out_file.write(encode_webm_element(child.id_bytes, rewritten_tracks, child.size_len))
                            processed[0] += child.end - child.header_start
                            progress.update(processed[0])
                        elif child.id_value == WEBM_ID_CLUSTER:
                            child_payload = read_source_range(source, child.data_start, child.data_end)
                            rewritten_cluster = rewrite_webm_cluster_payload(
                                child_payload, track_keys, progress, processed
                            )
                            out_file.write(encode_webm_element(child.id_bytes, rewritten_cluster, child.size_len))
                        elif child.id_value in {WEBM_ID_SEEK_HEAD, WEBM_ID_CUES, WEBM_ID_CRC32}:
                            processed[0] += child.end - child.header_start
                            progress.update(processed[0])
                        else:
                            stream_copy_range_without_progress(source, out_file, child.header_start, child.end)
                            processed[0] += child.end - child.header_start
                            progress.update(processed[0])

                        inner_cursor = child.end

                    if inner_cursor < element.data_end:
                        stream_copy_range_without_progress(source, out_file, inner_cursor, element.data_end)
                        processed[0] += element.data_end - inner_cursor
                        progress.update(processed[0])

                    cursor = element.end

                if cursor < file_size:
                    stream_copy_range_without_progress(source, out_file, cursor, file_size)
                    processed[0] += file_size - cursor
                    progress.update(processed[0])

            progress.finish()
    print("Decrypted successfully")
    if output_path.lower().endswith(".mp4"):
        print(
            "WARNING: The output container is still WebM. Use a .webm or .mkv extension unless you remux it afterward."
        )


def extract_webm_kids_quick(path: str, max_scan_bytes: int = 16 * 1024 * 1024) -> List[bytes]:
    with open(path, "rb") as f:
        data = f.read(max_scan_bytes)
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


FP_PROGRESS_WIDTH = 40
FP_PIFF_SAMPLE_ENCRYPTION_UUID = "a2394f525a9b4f14a2446c427c648df4"
FP_PIFF_TRACK_ENCRYPTION_UUID = "8974dbce7be74c5184f97148f9882554"


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
    if box_type == "uuid":
        if offset + header + 16 > limit:
            return None
        header += 16
    if size < header or offset + size > limit:
        return None
    return offset, offset + size, header, box_type


def fp_box_uuid(data, box_start, box_header, box_type):
    if box_type != "uuid":
        return ""
    uuid_start = box_start + box_header - 16
    uuid_end = box_start + box_header
    if uuid_start < box_start + 8 or uuid_end > len(data):
        return ""
    return bytes(data[uuid_start:uuid_end]).hex()


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
            box_uuid = fp_box_uuid(data, box_start, box_header, box_type)
            if box_type in wanted or (
                box_type == "uuid"
                and (
                    ("senc" in wanted and box_uuid == FP_PIFF_SAMPLE_ENCRYPTION_UUID)
                    or ("tenc" in wanted and box_uuid == FP_PIFF_TRACK_ENCRYPTION_UUID)
                )
            ):
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
    sample_entry_end = None
    codec_format = ""
    nal_length_size = 0
    nal_header_clear_bytes = 0
    tenc_is_protected_offset = None
    tenc_iv_size_offset = None
    for stsd_start, stsd_end, stsd_header, stsd_type in fp_recursive_boxes(data, trak_start, trak_end, {"stsd"}):
        entry_count = fp_be32(data, stsd_start + stsd_header + 4)
        entry_offset = stsd_start + stsd_header + 8
        if entry_count > 0 and entry_offset + 8 <= stsd_end:
            entry_size = fp_be32(data, entry_offset)
            entry_type = bytes(data[entry_offset + 4 : entry_offset + 8]).decode("latin1")
            sample_entry_start = entry_offset
            sample_entry_end = entry_offset + entry_size
            scan_skip = 8
            if entry_type in {"avc1", "avc3", "encv", "hvc1", "hev1", "dvhe", "dvh1"}:
                scan_skip = 86
            for cfg_start, cfg_end, cfg_header, cfg_type in fp_recursive_boxes(
                data, entry_offset + scan_skip, entry_offset + entry_size, {"avcC", "hvcC"}
            ):
                if cfg_type == "avcC" and cfg_start + cfg_header + 5 <= cfg_end:
                    codec_format = "avc1"
                    nal_length_size = (data[cfg_start + cfg_header + 4] & 3) + 1
                    nal_header_clear_bytes = 1
                elif cfg_type == "hvcC" and cfg_start + cfg_header + 22 <= cfg_end:
                    codec_format = "hvc1"
                    nal_length_size = (data[cfg_start + cfg_header + 21] & 3) + 1
                    nal_header_clear_bytes = 2
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
        codec_format,
        nal_length_size,
        nal_header_clear_bytes,
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
            codec_format,
            nal_length_size,
            nal_header_clear_bytes,
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
            "codec_format": codec_format,
            "nal_length_size": nal_length_size,
            "nal_header_clear_bytes": nal_header_clear_bytes,
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
            sample["duration_offset"] = offset
            offset += 4
        if flags & 0x000200:
            sample["size"] = fp_be32(data, offset)
            sample["size_offset"] = offset
            offset += 4
        if flags & 0x000400:
            sample["flags"] = fp_be32(data, offset)
            sample["flags_offset"] = offset
            offset += 4
        elif index == 0 and first_sample_flags is not None:
            sample["flags"] = first_sample_flags
        if flags & 0x000800:
            sample["composition_time_offset_offset"] = offset
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


def fp_parse_saiz_fast(data, box_start, box_end, box_header):
    if box_start + box_header + 9 > box_end:
        return []
    version, flags = fp_parse_fullbox(data, box_start, box_header)
    offset = box_start + box_header + 4
    if flags & 0x000001:
        if offset + 8 > box_end:
            return []
        offset += 8
    if offset + 5 > box_end:
        return []
    default_info_size = data[offset]
    sample_count = fp_be32(data, offset + 1)
    offset += 5
    if default_info_size:
        return [default_info_size] * sample_count
    if offset + sample_count > box_end:
        sample_count = max(0, box_end - offset)
    return [data[offset + index] for index in range(sample_count)]


def fp_parse_saio_fast(data, box_start, box_end, box_header):
    if box_start + box_header + 8 > box_end:
        return []
    version, flags = fp_parse_fullbox(data, box_start, box_header)
    offset = box_start + box_header + 4
    if flags & 0x000001:
        if offset + 8 > box_end:
            return []
        offset += 8
    if offset + 4 > box_end:
        return []
    entry_count = fp_be32(data, offset)
    offset += 4
    offsets = []
    for _ in range(entry_count):
        item_size = 8 if version == 1 else 4
        if offset + item_size > box_end:
            break
        offsets.append(fp_be64(data, offset) if version == 1 else fp_be32(data, offset))
        offset += item_size
    return offsets


def fp_expand_cenc_iv(iv):
    if len(iv) == 8:
        return iv + b"\x00" * 8
    return iv


def fp_parse_aux_info_from_saiz_saio_fast(data, sample_info_sizes, offsets, iv_size, constant_iv, offset_base=0):
    if not sample_info_sizes or not offsets:
        return []
    base = offset_base + offsets[0]
    if base < 0 or base >= len(data):
        return []
    total = sum(sample_info_sizes)
    if base + total > len(data):
        total = max(0, len(data) - base)
    blob = data[base : base + total]
    entries = []
    cursor = 0
    for info_size in sample_info_sizes:
        if info_size < 0 or cursor + info_size > len(blob):
            break
        sample_blob = blob[cursor : cursor + info_size]
        if iv_size == 0:
            iv = constant_iv
            sub_offset = 0
        else:
            if len(sample_blob) < iv_size:
                break
            iv = bytes(sample_blob[:iv_size])
            sub_offset = iv_size
        iv = fp_expand_cenc_iv(iv)
        subsamples = []
        if sub_offset + 2 <= len(sample_blob):
            subsample_count = fp_be16(sample_blob, sub_offset)
            sub_offset += 2
            for _ in range(subsample_count):
                if sub_offset + 6 > len(sample_blob):
                    break
                clear_size = fp_be16(sample_blob, sub_offset)
                encrypted_size = fp_be32(sample_blob, sub_offset + 2)
                subsamples.append((clear_size, encrypted_size))
                sub_offset += 6
        entries.append((iv, subsamples))
        cursor += info_size
    return entries


def fp_count_top_level_boxes(data, wanted_type):
    count = 0
    for _, _, _, box_type in fp_children(data, 0, len(data)):
        if box_type == wanted_type:
            count += 1
    return count


def fp_expected_encrypted_fragment_samples(data, tracks):
    samples_by_track = fp_collect_all_fragment_samples(data)
    expected = 0
    for track_id, samples in samples_by_track.items():
        track = tracks.get(track_id)
        if track and track.get("encrypted"):
            expected += len(samples)
    return expected


def fp_fragment_collection_is_suspicious(data, tracks, collected_count):
    expected = fp_expected_encrypted_fragment_samples(data, tracks)
    moof_count = fp_count_top_level_boxes(data, "moof")
    if moof_count <= 1 or expected <= 0:
        return False, expected, moof_count
    missing = expected - collected_count
    if missing <= 0:
        return False, expected, moof_count
    suspicious = collected_count <= max(25, int(expected * 0.75)) and missing >= 25
    return suspicious, expected, moof_count


def fp_collect_fragments_aux_fallback(data, tracks):
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
            mdat_size_offset = None
            mdat_data_start = None
            if next_header is not None and next_header[3] == "mdat":
                mdat_size_offset = next_header[0]
                mdat_data_start = next_header[0] + next_header[2]

            for traf_start, traf_end, traf_header, traf_type in fp_children(data, box_start + box_header, box_end):
                if traf_type != "traf":
                    continue
                tfhd = None
                trun_boxes = []
                senc_box = None
                saiz_box = None
                saio_box = None
                for child_start, child_end, child_header, child_type in fp_children(
                    data, traf_start + traf_header, traf_end
                ):
                    child_uuid = fp_box_uuid(data, child_start, child_header, child_type)
                    if child_type == "tfhd":
                        tfhd = fp_parse_tfhd(data, child_start, child_end, child_header)
                    elif child_type == "trun":
                        trun_boxes.append((child_start, child_end, child_header))
                    elif child_type == "senc" or (
                        child_type == "uuid" and child_uuid == FP_PIFF_SAMPLE_ENCRYPTION_UUID
                    ):
                        senc_box = (child_start, child_end, child_header)
                    elif child_type == "saiz":
                        saiz_box = (child_start, child_end, child_header)
                    elif child_type == "saio":
                        saio_box = (child_start, child_end, child_header)

                if not tfhd or not trun_boxes:
                    continue
                track_id = tfhd["track_id"]
                track = tracks.get(track_id)
                if not track or not track.get("encrypted"):
                    continue

                senc_entries = None
                if senc_box is not None:
                    senc_entries = fp_parse_senc(
                        data, senc_box[0], senc_box[1], senc_box[2], track["iv_size"], track["constant_iv"]
                    )
                elif saiz_box is not None and saio_box is not None:
                    sample_info_sizes = fp_parse_saiz_fast(data, saiz_box[0], saiz_box[1], saiz_box[2])
                    aux_offsets = fp_parse_saio_fast(data, saio_box[0], saio_box[1], saio_box[2])
                    senc_entries = fp_parse_aux_info_from_saiz_saio_fast(
                        data,
                        sample_info_sizes,
                        aux_offsets,
                        track["iv_size"],
                        track["constant_iv"],
                        offset_base=box_start,
                    )
                if not senc_entries:
                    continue

                trex = track.get("trex", {})
                default_sample_size = tfhd.get("default_sample_size", trex.get("default_sample_size", 0))
                base = tfhd.get("base_data_offset", box_start)
                aux_index = 0
                previous_sample_end = None
                for trun_start, trun_end, trun_header in trun_boxes:
                    sample_count, data_offset, samples = fp_parse_trun(data, trun_start, trun_end, trun_header)
                    if data_offset is not None:
                        sample_offset = base + data_offset
                    elif previous_sample_end is not None:
                        sample_offset = previous_sample_end
                    elif mdat_data_start is not None:
                        sample_offset = mdat_data_start
                    else:
                        sample_offset = base
                    for sample in samples:
                        sample_size = sample.get("size", default_sample_size)
                        if sample_size <= 0:
                            aux_index += 1
                            continue
                        if aux_index >= len(senc_entries):
                            sample_offset += sample_size
                            aux_index += 1
                            continue
                        sample_aux = senc_entries[aux_index]
                        sample_size_offset = sample.get("size_offset")
                        if 0 <= sample_offset and sample_offset + sample_size <= file_size:
                            fragments.append(
                                (sample_offset, sample_size, sample_aux, track_id, sample_size_offset, mdat_size_offset)
                            )
                        sample_offset += sample_size
                        previous_sample_end = sample_offset
                        aux_index += 1
                total_samples += sum(1 for item in fragments if item[3] == track_id)
        offset = box_end
    return fragments, len(fragments)


def fp_collect_fragments_with_fallback(data, tracks):
    fragments, total_samples = fp_collect_fragments(data, tracks)
    suspicious, expected, moof_count = fp_fragment_collection_is_suspicious(data, tracks, total_samples)
    if not suspicious:
        return fragments, total_samples
    fallback_fragments, fallback_total = fp_collect_fragments_aux_fallback(data, tracks)
    if fallback_total > total_samples:
        print(
            "Detected fragmented MP4 aux-info layout that the primary parser only partially collected; "
            f"using compatibility fallback ({fallback_total}/{expected} encrypted samples across {moof_count} moof boxes)."
        )
        return fallback_fragments, fallback_total
    return fragments, total_samples


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


def fp_decrypt_ctr_subsamples(buffer, sample_start, subsamples, key, iv):
    encrypted_ranges = []
    cursor = sample_start
    for clear_size, encrypted_size in subsamples:
        cursor += clear_size
        if encrypted_size > 0:
            encrypted_ranges.append((cursor, encrypted_size))
        cursor += encrypted_size
    if not encrypted_ranges:
        return
    encrypted_blob = b"".join(bytes(buffer[start : start + size]) for start, size in encrypted_ranges)
    decrypt = fp_make_aes_ctr_decryptor(key, iv)
    decrypted_blob = decrypt(encrypted_blob)
    offset = 0
    for start, size in encrypted_ranges:
        buffer[start : start + size] = decrypted_blob[offset : offset + size]
        offset += size


def fp_decrypt_cbc_pattern_subsamples(buffer, sample_start, subsamples, key, iv, crypt_blocks, skip_blocks):
    encrypted_ranges = []

    def collect_pattern_ranges(start, size):
        full = size - (size % 16)
        if full <= 0:
            return
        if crypt_blocks <= 0 and skip_blocks <= 0:
            encrypted_ranges.append((start, full))
            return
        if crypt_blocks <= 0:
            return
        if skip_blocks <= 0:
            encrypted_ranges.append((start, full))
            return
        cursor = start
        remaining = full
        crypt_bytes = crypt_blocks * 16
        skip_bytes = skip_blocks * 16
        while remaining >= 16:
            take = min(crypt_bytes, remaining)
            take -= take % 16
            if take <= 0:
                break
            encrypted_ranges.append((cursor, take))
            cursor += take
            remaining -= take
            skip = min(skip_bytes, remaining)
            cursor += skip
            remaining -= skip

    cursor = sample_start
    for clear_size, encrypted_size in subsamples:
        cursor += clear_size
        collect_pattern_ranges(cursor, encrypted_size)
        cursor += encrypted_size

    if not encrypted_ranges:
        return
    encrypted_blob = b"".join(bytes(buffer[start : start + size]) for start, size in encrypted_ranges)
    encrypted_blob = encrypted_blob[: len(encrypted_blob) - (len(encrypted_blob) % 16)]
    if not encrypted_blob:
        return
    decrypt = fp_make_aes_cbc_decryptor(key, iv)
    decrypted_blob = decrypt(encrypted_blob)
    offset = 0
    remaining = len(decrypted_blob)
    for start, size in encrypted_ranges:
        take = min(size, remaining - offset)
        if take <= 0:
            break
        buffer[start : start + take] = decrypted_blob[offset : offset + take]
        offset += take


def fp_build_length_prefixed_nal_subsamples(buffer, sample_start, sample_size, nal_length_size, nal_header_clear_bytes):
    if nal_length_size <= 0 or nal_header_clear_bytes < 0 or sample_size <= 0:
        return []
    end = sample_start + sample_size
    cursor = sample_start
    subsamples = []
    while cursor < end:
        if cursor + nal_length_size > end:
            return []
        nal_size = int.from_bytes(buffer[cursor : cursor + nal_length_size], "big")
        cursor += nal_length_size
        if nal_size == 0:
            subsamples.append((nal_length_size, 0))
            continue
        if cursor + nal_size > end:
            return []
        clear_payload = min(nal_header_clear_bytes, nal_size)
        encrypted_payload = max(0, nal_size - clear_payload)
        subsamples.append((nal_length_size + clear_payload, encrypted_payload))
        cursor += nal_size
    return subsamples


def fp_decrypt_sample(buffer, sample_start, sample_size, sample_aux, track, key):
    iv, subsamples = sample_aux
    scheme = track["scheme"].lower()
    crypt_blocks = track["crypt_blocks"]
    skip_blocks = track["skip_blocks"]
    if not subsamples:
        codec_format = str(track.get("codec_format", "")).lower()
        nal_length_size = int(track.get("nal_length_size", 0) or 0)
        nal_header_clear_bytes = int(track.get("nal_header_clear_bytes", 0) or 0)
        if codec_format in {"avc1", "hvc1"} and nal_length_size > 0:
            guessed = fp_build_length_prefixed_nal_subsamples(
                buffer, sample_start, sample_size, nal_length_size, nal_header_clear_bytes
            )
            if guessed:
                subsamples = guessed
            else:
                if scheme in ("cenc", "cens"):
                    fp_decrypt_ctr_range(buffer, sample_start, sample_size, key, iv)
                else:
                    fp_decrypt_cbc_pattern_range(buffer, sample_start, sample_size, key, iv, crypt_blocks, skip_blocks)
                return
        else:
            if scheme in ("cenc", "cens"):
                fp_decrypt_ctr_range(buffer, sample_start, sample_size, key, iv)
            else:
                fp_decrypt_cbc_pattern_range(buffer, sample_start, sample_size, key, iv, crypt_blocks, skip_blocks)
            return
    if scheme in ("cenc", "cens"):
        fp_decrypt_ctr_subsamples(buffer, sample_start, subsamples, key, iv)
    else:
        fp_decrypt_cbc_pattern_subsamples(buffer, sample_start, subsamples, key, iv, crypt_blocks, skip_blocks)


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
            mdat_size_offset = None
            mdat_data_start = None
            if next_header is not None and next_header[3] == "mdat":
                mdat_start = next_header[0]
                mdat_size_offset = next_header[0]
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
                    elif child_type == "senc" or (
                        child_type == "uuid"
                        and fp_box_uuid(data, child_start, child_header, child_type) == FP_PIFF_SAMPLE_ENCRYPTION_UUID
                    ):
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
                        sample_size_offset = sample.get("size_offset")
                        fragment_samples.append(
                            (sample_offset, sample_size, sample_aux, track_id, sample_size_offset, mdat_size_offset)
                        )
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
            codec_format,
            nal_length_size,
            nal_header_clear_bytes,
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
            codec_format,
            nal_length_size,
            nal_header_clear_bytes,
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
        is_sample_encryption_box = box_type == "senc" or (
            box_type == "uuid" and fp_box_uuid(data, box_start, box_header, box_type) == FP_PIFF_SAMPLE_ENCRYPTION_UUID
        )
        if is_sample_encryption_box:
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


def fp_strip_hevc_emulation_prevention_with_map(payload):
    rbsp = bytearray()
    rbsp_to_ebsp = []
    zero_count = 0
    for index, value in enumerate(payload):
        if zero_count >= 2 and value == 0x03:
            zero_count = 0
            continue
        rbsp_to_ebsp.append(index)
        rbsp.append(value)
        if value == 0:
            zero_count += 1
        else:
            zero_count = 0
    return bytes(rbsp), rbsp_to_ebsp


def fp_repair_hevc_sei_rbsp_stop(sample):
    if len(sample) < 6:
        return sample

    nal_length_size = 4
    cursor = 0
    repaired = bytearray()
    changed = False

    while cursor < len(sample):
        if cursor + nal_length_size > len(sample):
            return sample

        nal_size = int.from_bytes(sample[cursor : cursor + nal_length_size], "big")
        nal_start = cursor + nal_length_size
        nal_end = nal_start + nal_size

        if nal_size <= 0 or nal_end > len(sample):
            return sample

        nal = bytearray(sample[nal_start:nal_end])

        if len(nal) >= 4:
            nal_type = (nal[0] >> 1) & 0x3F

            if nal_type in (39, 40):
                rbsp, rbsp_to_ebsp = fp_strip_hevc_emulation_prevention_with_map(nal[2:])
                position = 0

                while position + 2 <= len(rbsp):
                    payload_type = 0
                    while position < len(rbsp) and rbsp[position] == 0xFF:
                        payload_type += 255
                        position += 1

                    if position >= len(rbsp):
                        break

                    payload_type += rbsp[position]
                    position += 1

                    payload_size = 0
                    size_uses_ff = False
                    payload_size_byte_position = position

                    while position < len(rbsp) and rbsp[position] == 0xFF:
                        payload_size += 255
                        position += 1
                        size_uses_ff = True

                    if position >= len(rbsp):
                        break

                    payload_size_byte_position = position
                    payload_size += rbsp[position]
                    position += 1

                    available_after_size = len(rbsp) - position
                    payload_end = position + payload_size

                    if payload_type == 4 and not size_uses_ff and 0 <= payload_size <= available_after_size:
                        if payload_size == available_after_size:
                            nal.append(0x80)
                            nal_size += 1
                            changed = True
                            break

                        trailing = rbsp[payload_end:]

                        if len(trailing) == 1 and trailing[0] != 0x80:
                            new_payload_size = payload_size + 1
                            if new_payload_size <= 0xFE:
                                size_ebsp_offset = rbsp_to_ebsp[payload_size_byte_position]
                                nal[2 + size_ebsp_offset] = new_payload_size
                                nal.append(0x80)
                                nal_size += 1
                                changed = True
                                break

                    if payload_end >= len(rbsp):
                        break

                    position = payload_end

        repaired.extend(nal_size.to_bytes(nal_length_size, "big"))
        repaired.extend(nal)
        cursor = nal_end

    if cursor != len(sample):
        return sample

    return bytes(repaired) if changed else sample


def fp_track_uses_hevc_bitstream(track):
    original_format = str(track.get("original_format") or track.get("entry_type") or "").lower()
    codec_format = str(track.get("codec_format") or "").lower()
    return original_format in {"hev1", "hvc1", "dvhe", "dvh1"} or codec_format in {"hev1", "hvc1", "dvhe", "dvh1"}


def fp_decrypt_sample_to_bytes(data, sample_start, sample_size, sample_aux, track, key, fix_sei=False):
    sample = bytearray(data[sample_start : sample_start + sample_size])
    fp_decrypt_sample(sample, 0, sample_size, sample_aux, track, key)
    result = bytes(sample)
    if fix_sei and fp_track_uses_hevc_bitstream(track):
        result = fp_repair_hevc_sei_rbsp_stop(result)
    return result


def fp_collect_growth_patches(data, decrypt_events, fix_sei=False):
    patches = []
    trun_size_deltas = {}
    mdat_size_deltas = {}
    growth_count = 0

    if not fix_sei:
        return patches, growth_count

    hevc_events = []
    for event_start, event_end, event_kind, event_payload in decrypt_events:
        sample_start, sample_size, sample_aux, track, key, sample_size_offset, mdat_size_offset = event_payload
        if fp_track_uses_hevc_bitstream(track):
            hevc_events.append((event_start, event_end, event_kind, event_payload))

    if not hevc_events:
        return patches, growth_count

    for event_start, event_end, event_kind, event_payload in hevc_events:
        sample_start, sample_size, sample_aux, track, key, sample_size_offset, mdat_size_offset = event_payload
        decrypted = fp_decrypt_sample_to_bytes(data, sample_start, sample_size, sample_aux, track, key, fix_sei=True)
        delta = len(decrypted) - sample_size
        if delta <= 0:
            continue
        growth_count += 1
        if sample_size_offset is not None:
            trun_size_deltas[sample_size_offset] = trun_size_deltas.get(sample_size_offset, 0) + delta
        if mdat_size_offset is not None:
            mdat_size_deltas[mdat_size_offset] = mdat_size_deltas.get(mdat_size_offset, 0) + delta

    for offset, delta in sorted(trun_size_deltas.items()):
        old_value = fp_be32(data, offset)
        new_value = old_value + delta
        if new_value > 0xFFFFFFFF:
            fail("Expanded HEVC sample size exceeds 32-bit trun storage")
        patches.append((offset, struct.pack(">I", new_value)))

    for offset, delta in sorted(mdat_size_deltas.items()):
        old_value = fp_be32(data, offset)
        if old_value == 1:
            continue
        new_value = old_value + delta
        if new_value > 0xFFFFFFFF:
            fail("Expanded mdat size exceeds 32-bit box storage")
        patches.append((offset, struct.pack(">I", new_value)))

    return patches, growth_count


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
    for fragment in fragments:
        sample_start, sample_size, sample_aux, track_id, sample_size_offset, mdat_size_offset = fragment
        track = tracks[track_id]
        key = (
            fast_keys.get(str(track_id))
            or fast_keys.get(track["kid"])
            or fast_keys.get("00000000000000000000000000000000")
        )
        if key is None:
            fail(f"Missing key for KID {track['kid']}")
        payload = (sample_start, sample_size, sample_aux, track, key, sample_size_offset, mdat_size_offset)
        events.append((sample_start, sample_start + sample_size, "decrypt", payload))
    events.sort(key=lambda item: (item[0], item[1]))
    previous_end = -1
    for event in events:
        if event[0] < previous_end:
            fail("Overlapping encrypted sample ranges were detected")
        previous_end = event[1]
    return events


def fp_stream_decrypt_to_output(data, output_path, patch_events, decrypt_events, fix_sei=False):
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
                sample_start, sample_size, sample_aux, track, key, sample_size_offset, mdat_size_offset = event_payload
                out_file.write(
                    fp_decrypt_sample_to_bytes(data, sample_start, sample_size, sample_aux, track, key, fix_sei=fix_sei)
                )
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


def fp_make_box(box_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def fp_make_full_box(box_type: bytes, version: int, flags: int, payload: bytes = b"") -> bytes:
    return fp_make_box(box_type, bytes([version & 0xFF]) + (flags & 0xFFFFFF).to_bytes(3, "big") + payload)


def fp_compress_table_values(values: List[int]) -> List[Tuple[int, int]]:
    entries: List[Tuple[int, int]] = []
    for value in values:
        if entries and entries[-1][1] == value:
            entries[-1] = (entries[-1][0] + 1, value)
        else:
            entries.append((1, value))
    return entries


def fp_collect_all_fragment_samples(data) -> Dict[int, List[Tuple[int, int, int, int, int]]]:
    moov_start = None
    moov_end = None
    for box_start, box_end, box_header, box_type in fp_children(data, 0, len(data)):
        if box_type == "moov":
            moov_start = box_start
            moov_end = box_end
            break
    if moov_start is None:
        return {}

    tracks = fp_parse_moov(data, moov_start, moov_end)
    samples_by_track: Dict[int, List[Tuple[int, int, int, int, int]]] = {}

    offset = 0
    file_size = len(data)
    while offset < file_size:
        header = fp_read_box_header(data, offset, file_size)
        if header is None:
            break
        box_start, box_end, box_header, box_type = header
        if box_type != "moof":
            offset = box_end
            continue

        next_header = fp_read_box_header(data, box_end, file_size)
        mdat_data_start = None
        if next_header is not None and next_header[3] == "mdat":
            mdat_data_start = next_header[0] + next_header[2]

        for traf_start, traf_end, traf_header, traf_type in fp_children(data, box_start + box_header, box_end):
            if traf_type != "traf":
                continue
            tfhd = None
            truns = []
            for child_start, child_end, child_header, child_type in fp_children(
                data, traf_start + traf_header, traf_end
            ):
                if child_type == "tfhd":
                    tfhd = fp_parse_tfhd(data, child_start, child_end, child_header)
                elif child_type == "trun":
                    truns.append(fp_parse_trun(data, child_start, child_end, child_header))
            if not tfhd or not truns:
                continue
            track_id = tfhd["track_id"]
            track = tracks.get(track_id, {})
            trex = track.get("trex", {})
            default_duration = tfhd.get("default_sample_duration", trex.get("default_sample_duration", 0))
            default_size = tfhd.get("default_sample_size", trex.get("default_sample_size", 0))
            default_flags = tfhd.get("default_sample_flags", trex.get("default_sample_flags", 0))
            base = tfhd.get("base_data_offset", box_start)
            samples_by_track.setdefault(track_id, [])
            for sample_count, data_offset, samples in truns:
                sample_offset = base + data_offset if data_offset is not None else mdat_data_start
                if sample_offset is None:
                    continue
                first_flags = None
                for index, sample in enumerate(samples):
                    sample_size = sample.get("size", default_size)
                    sample_duration = sample.get("duration", default_duration)
                    sample_flags = sample.get(
                        "flags", first_flags if index == 0 and first_flags is not None else default_flags
                    )
                    sample_cto = sample.get("composition_time_offset", 0)
                    if sample_size > 0:
                        samples_by_track[track_id].append(
                            (sample_offset, sample_size, sample_duration, sample_cto, sample_flags)
                        )
                    sample_offset += sample_size
        offset = box_end

    return samples_by_track


def fp_patch_duration_in_box(raw: bytes, box_type: bytes, duration: int) -> bytes:
    out = bytearray(raw)
    if len(out) < 16:
        return bytes(out)
    version = out[8]
    if version == 1:
        duration_offset = 36 if box_type == b"tkhd" else 32
        if duration_offset + 8 <= len(out):
            struct.pack_into(">Q", out, duration_offset, duration & 0xFFFFFFFFFFFFFFFF)
    else:
        duration_offset = 28 if box_type == b"tkhd" else 24
        if duration_offset + 4 <= len(out):
            struct.pack_into(">I", out, duration_offset, duration & 0xFFFFFFFF)
    return bytes(out)


def fp_pack_ctts_payload_with_fallback(ctts_entries):
    try:
        return struct.pack(">I", len(ctts_entries)) + b"".join(
            struct.pack(">II", count, value) for count, value in ctts_entries
        )
    except struct.error as exc:
        if "'I' format requires 0 <= number <= 4294967295" not in str(exc):
            raise
        normalized = []
        for count, value in ctts_entries:
            if value > 0x7FFFFFFF:
                value -= 0x100000000
            normalized.append((count, value))
        return (
            bytes([1, 0, 0, 0])
            + struct.pack(">I", len(normalized))
            + b"".join(struct.pack(">Ii", count, value) for count, value in normalized)
        )


def fp_build_flat_sample_table(
    original_stsd: bytes, samples: List[Tuple[int, int, int, int, int]], chunk_offset: int
) -> bytes:
    durations = [sample[2] for sample in samples]
    composition_offsets = [sample[3] for sample in samples]
    sizes = [sample[1] for sample in samples]
    flags = [sample[4] for sample in samples]

    stts_entries = fp_compress_table_values(durations)
    stts_payload = struct.pack(">I", len(stts_entries)) + b"".join(
        struct.pack(">II", count, value) for count, value in stts_entries
    )
    stts = fp_make_full_box(b"stts", 0, 0, stts_payload)

    ctts_entries = fp_compress_table_values(composition_offsets)
    ctts_payload = fp_pack_ctts_payload_with_fallback(ctts_entries)
    ctts = fp_make_full_box(b"ctts", 0, 0, ctts_payload)

    stsc = fp_make_full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(samples), 1))
    stsz = fp_make_full_box(
        b"stsz", 0, 0, struct.pack(">II", 0, len(samples)) + b"".join(struct.pack(">I", size) for size in sizes)
    )
    stco = fp_make_full_box(b"stco", 0, 0, struct.pack(">II", 1, chunk_offset & 0xFFFFFFFF))

    sync_samples = [index + 1 for index, sample_flags in enumerate(flags) if not (sample_flags & 0x00010000)]
    stss = fp_make_full_box(
        b"stss",
        0,
        0,
        struct.pack(">I", len(sync_samples)) + b"".join(struct.pack(">I", index) for index in sync_samples),
    )

    return fp_make_box(b"stbl", original_stsd + stts + ctts + stsc + stsz + stco + stss)


def fp_collect_all_fragment_sample_chunks(data) -> Dict[int, List[List[Tuple[int, int, int, int, int]]]]:
    moov_start = None
    moov_end = None
    for box_start, box_end, box_header, box_type in fp_children(data, 0, len(data)):
        if box_type == "moov":
            moov_start = box_start
            moov_end = box_end
            break
    if moov_start is None:
        return {}

    tracks = fp_parse_moov(data, moov_start, moov_end)
    chunks_by_track: Dict[int, List[List[Tuple[int, int, int, int, int]]]] = {}

    offset = 0
    file_size = len(data)
    while offset < file_size:
        header = fp_read_box_header(data, offset, file_size)
        if header is None:
            break
        box_start, box_end, box_header, box_type = header
        if box_type != "moof":
            offset = box_end
            continue

        next_header = fp_read_box_header(data, box_end, file_size)
        mdat_data_start = None
        if next_header is not None and next_header[3] == "mdat":
            mdat_data_start = next_header[0] + next_header[2]

        for traf_start, traf_end, traf_header, traf_type in fp_children(data, box_start + box_header, box_end):
            if traf_type != "traf":
                continue
            tfhd = None
            truns = []
            for child_start, child_end, child_header, child_type in fp_children(
                data, traf_start + traf_header, traf_end
            ):
                if child_type == "tfhd":
                    tfhd = fp_parse_tfhd(data, child_start, child_end, child_header)
                elif child_type == "trun":
                    truns.append(fp_parse_trun(data, child_start, child_end, child_header))
            if not tfhd or not truns:
                continue
            track_id = tfhd["track_id"]
            track = tracks.get(track_id, {})
            trex = track.get("trex", {})
            default_duration = tfhd.get("default_sample_duration", trex.get("default_sample_duration", 0))
            default_size = tfhd.get("default_sample_size", trex.get("default_sample_size", 0))
            default_flags = tfhd.get("default_sample_flags", trex.get("default_sample_flags", 0))
            base = tfhd.get("base_data_offset", box_start)
            previous_sample_end = None
            chunks_by_track.setdefault(track_id, [])
            for sample_count, data_offset, samples in truns:
                if data_offset is not None:
                    sample_offset = base + data_offset
                elif previous_sample_end is not None:
                    sample_offset = previous_sample_end
                elif mdat_data_start is not None:
                    sample_offset = mdat_data_start
                else:
                    continue
                chunk = []
                first_flags = None
                for index, sample in enumerate(samples):
                    sample_size = sample.get("size", default_size)
                    sample_duration = sample.get("duration", default_duration)
                    sample_flags = sample.get(
                        "flags", first_flags if index == 0 and first_flags is not None else default_flags
                    )
                    sample_cto = sample.get("composition_time_offset", 0)
                    if sample_size > 0:
                        if 0 <= sample_offset and sample_offset + sample_size <= file_size:
                            chunk.append((sample_offset, sample_size, sample_duration, sample_cto, sample_flags))
                    sample_offset += sample_size
                previous_sample_end = sample_offset
                if chunk:
                    chunks_by_track[track_id].append(chunk)
        offset = box_end

    return chunks_by_track


def fp_flatten_chunks_to_samples(
    chunks: List[List[Tuple[int, int, int, int, int]]],
) -> List[Tuple[int, int, int, int, int]]:
    samples: List[Tuple[int, int, int, int, int]] = []
    for chunk in chunks:
        samples.extend(chunk)
    return samples


def fp_build_chunked_sample_table(
    original_stsd: bytes,
    chunks: List[List[Tuple[int, int, int, int, int]]],
    chunk_offsets: List[int],
    target_duration: Optional[int] = None,
) -> bytes:
    samples = fp_flatten_chunks_to_samples(chunks)
    durations = [sample[2] for sample in samples]
    if target_duration is not None and durations:
        duration_delta = target_duration - sum(durations)
        if 0 < duration_delta <= max(durations) * 4:
            durations[-1] += duration_delta
    composition_offsets = [sample[3] for sample in samples]
    sizes = [sample[1] for sample in samples]
    flags = [sample[4] for sample in samples]

    stts_entries = fp_compress_table_values(durations)
    stts_payload = struct.pack(">I", len(stts_entries)) + b"".join(
        struct.pack(">II", count, value) for count, value in stts_entries
    )
    stts = fp_make_full_box(b"stts", 0, 0, stts_payload)

    ctts_entries = fp_compress_table_values(composition_offsets)
    ctts_payload = fp_pack_ctts_payload_with_fallback(ctts_entries)
    ctts = fp_make_full_box(b"ctts", 0, 0, ctts_payload)

    stsc_entries: List[Tuple[int, int, int]] = []
    previous_samples_per_chunk = None
    for chunk_index, chunk in enumerate(chunks, 1):
        samples_per_chunk = len(chunk)
        if samples_per_chunk <= 0:
            continue
        if samples_per_chunk != previous_samples_per_chunk:
            stsc_entries.append((chunk_index, samples_per_chunk, 1))
            previous_samples_per_chunk = samples_per_chunk
    stsc_payload = struct.pack(">I", len(stsc_entries)) + b"".join(
        struct.pack(">III", *entry) for entry in stsc_entries
    )
    stsc = fp_make_full_box(b"stsc", 0, 0, stsc_payload)

    stsz = fp_make_full_box(
        b"stsz", 0, 0, struct.pack(">II", 0, len(samples)) + b"".join(struct.pack(">I", size) for size in sizes)
    )

    if chunk_offsets and max(chunk_offsets) > 0xFFFFFFFF:
        chunk_table = fp_make_full_box(
            b"co64",
            0,
            0,
            struct.pack(">I", len(chunk_offsets)) + b"".join(struct.pack(">Q", offset) for offset in chunk_offsets),
        )
    else:
        chunk_table = fp_make_full_box(
            b"stco",
            0,
            0,
            struct.pack(">I", len(chunk_offsets))
            + b"".join(struct.pack(">I", offset & 0xFFFFFFFF) for offset in chunk_offsets),
        )

    sync_samples = [index + 1 for index, sample_flags in enumerate(flags) if not (sample_flags & 0x00010000)]
    stss = fp_make_full_box(
        b"stss",
        0,
        0,
        struct.pack(">I", len(sync_samples)) + b"".join(struct.pack(">I", index) for index in sync_samples),
    )

    sdtp_payload = bytearray()
    for sample_flags in flags:
        sample_depends_on = (sample_flags >> 24) & 0x03
        sample_is_depended_on = (sample_flags >> 22) & 0x03
        sample_has_redundancy = (sample_flags >> 20) & 0x03
        sdtp_payload.append((sample_depends_on << 4) | (sample_is_depended_on << 2) | sample_has_redundancy)
    sdtp = fp_make_full_box(b"sdtp", 0, 0, bytes(sdtp_payload))

    return fp_make_box(b"stbl", original_stsd + stts + ctts + stsc + stsz + chunk_table + stss + sdtp)


def fp_detect_hevc_parameter_sets_in_sync_samples(
    source: bytes, samples: List[Tuple[int, int, int, int, int]], limit: int = 12
) -> bool:
    checked = 0
    for sample_offset, sample_size, sample_duration, sample_cto, sample_flags in samples:
        if sample_flags & 0x00010000:
            continue
        sample = source[sample_offset : sample_offset + sample_size]
        cursor = 0
        has_parameter_set = False
        has_idr = False
        while cursor + 4 <= len(sample):
            nal_size = int.from_bytes(sample[cursor : cursor + 4], "big")
            cursor += 4
            if nal_size <= 0 or cursor + nal_size > len(sample):
                break
            if nal_size >= 2:
                nal_type = (sample[cursor] >> 1) & 0x3F
                if nal_type in {32, 33, 34}:
                    has_parameter_set = True
                if nal_type in {19, 20}:
                    has_idr = True
            cursor += nal_size
        checked += 1
        if not (has_parameter_set and has_idr):
            return False
        if checked >= limit:
            return True
    return checked > 0


def fp_get_track_duration_from_moov(data: bytes, moov: Box) -> int:
    for trak in moov.children:
        if trak.type != b"trak":
            continue
        mdia = None
        for child in trak.children:
            if child.type == b"mdia":
                mdia = child
                break
        if not mdia:
            continue
        for child in mdia.children:
            if child.type != b"mdhd":
                continue
            raw = data[child.start : child.end]
            if len(raw) < 16:
                continue
            version = raw[8]
            if version == 1 and len(raw) >= 40:
                return struct.unpack_from(">Q", raw, 32)[0]
            if version == 0 and len(raw) >= 32:
                return struct.unpack_from(">I", raw, 24)[0]
    return 0


def fp_get_timescale_from_box(data: bytes, box: Box) -> int:
    raw = data[box.start : box.end]
    if len(raw) < 32:
        return 0
    version = raw[8]
    if version == 1 and len(raw) >= 44:
        return struct.unpack_from(">I", raw, 28)[0]
    return struct.unpack_from(">I", raw, 16)[0]


def fp_patch_chunked_flatten_durations(raw: bytes, box_type: bytes, duration: int, timescale: int = 0) -> bytes:
    if box_type == b"mdhd":
        return fp_patch_duration_in_box(raw, box_type, duration)
    return raw


def fp_should_use_chunked_compatibility_flatten(
    source: bytes,
    parser: Mp4Parser,
    moov: Box,
    primary_track_id: int,
    primary_samples: List[Tuple[int, int, int, int, int]],
    primary_chunks: List[List[Tuple[int, int, int, int, int]]],
) -> bool:
    moof_count = sum(1 for _, _, _, box_type in fp_children(source, 0, len(source)) if box_type == "moof")
    if moof_count <= 1 or len(primary_chunks) <= 1 or not primary_samples:
        return False
    tracks = fp_parse_moov(source, moov.start, moov.end)
    track = tracks.get(primary_track_id)
    if not track or not fp_track_uses_hevc_bitstream(track):
        return False
    if len(primary_chunks) < max(8, moof_count // 2):
        return False
    if not fp_detect_hevc_parameter_sets_in_sync_samples(source, primary_samples):
        return False
    return True


def fp_flatten_fragmented_mp4_chunked_compat(
    file_path: str,
    source: bytes,
    parser: Mp4Parser,
    ftyp: Box,
    moov: Box,
    primary_track_id: int,
    primary_chunks: List[List[Tuple[int, int, int, int, int]]],
) -> bool:
    primary_samples = fp_flatten_chunks_to_samples(primary_chunks)
    if not primary_samples:
        return False

    def rebuild(current: Box, chunk_offsets: List[int]) -> bytes:
        if current.type == b"mvex":
            return b""
        if current.type == b"stbl":
            stsd = None
            for child in current.children:
                if child.type == b"stsd":
                    stsd = child
                    break
            if stsd is None:
                return source[current.start : current.end]
            target_duration = fp_get_track_duration_from_moov(source, moov)
            if target_duration <= 0:
                target_duration = None
            return fp_build_chunked_sample_table(
                source[stsd.start : stsd.end], primary_chunks, chunk_offsets, target_duration=target_duration
            )
        if current.type == b"mdhd":
            return fp_patch_chunked_flatten_durations(
                source[current.start : current.end], current.type, sum(sample[2] for sample in primary_samples)
            )
        if current.children:
            payload_start = current.start + current.header_size
            payload = bytearray()
            position = payload_start
            for child in current.children:
                if position < child.start:
                    payload.extend(source[position : child.start])
                payload.extend(rebuild(child, chunk_offsets))
                position = child.end
            if position < current.end:
                payload.extend(source[position : current.end])
            return fp_make_box(current.type, bytes(payload))
        return source[current.start : current.end]

    new_moov = b""
    chunk_offsets: List[int] = []
    for _ in range(6):
        mdat_data_start = (ftyp.end - ftyp.start) + len(new_moov) + 8
        chunk_offsets = []
        cursor = mdat_data_start
        for chunk in primary_chunks:
            chunk_offsets.append(cursor)
            cursor += sum(sample_size for _, sample_size, _, _, _ in chunk)
        rebuilt = rebuild(moov, chunk_offsets)
        if rebuilt == new_moov:
            new_moov = rebuilt
            break
        new_moov = rebuilt

    mdat_payload = bytearray()
    for chunk in primary_chunks:
        for sample_offset, sample_size, _, _, _ in chunk:
            mdat_payload.extend(source[sample_offset : sample_offset + sample_size])

    expected_sample_count = len(primary_samples)
    actual_sample_count = sum(len(chunk) for chunk in primary_chunks)
    if expected_sample_count != actual_sample_count:
        fail("Chunked compatibility flatten generated an inconsistent sample count")

    flattened = source[ftyp.start : ftyp.end] + new_moov + fp_make_box(b"mdat", bytes(mdat_payload))
    with open(file_path, "wb") as file_handle:
        file_handle.write(flattened)
    return True


def fp_flatten_fragmented_mp4_in_place(file_path: str):
    with open(file_path, "rb") as file_handle:
        source = file_handle.read()

    parser = Mp4Parser(source)
    moov = None
    ftyp = None
    for top in parser.root:
        if top.type == b"ftyp":
            ftyp = top
        elif top.type == b"moov":
            moov = top
    if ftyp is None or moov is None:
        return

    samples_by_track = fp_collect_all_fragment_samples(source)
    if not samples_by_track:
        return

    primary_track_id = sorted(samples_by_track, key=lambda track_id: len(samples_by_track[track_id]), reverse=True)[0]
    primary_samples = samples_by_track[primary_track_id]
    if not primary_samples:
        return

    chunk_map = fp_collect_all_fragment_sample_chunks(source)
    primary_chunks = chunk_map.get(primary_track_id, [])
    if fp_should_use_chunked_compatibility_flatten(
        source, parser, moov, primary_track_id, primary_samples, primary_chunks
    ):
        if fp_flatten_fragmented_mp4_chunked_compat(
            file_path, source, parser, ftyp, moov, primary_track_id, primary_chunks
        ):
            return
    moof_count = sum(1 for _, _, _, box_type in fp_children(source, 0, len(source)) if box_type == "moof")
    last_sample_end = max(sample_offset + sample_size for sample_offset, sample_size, _, _, _ in primary_samples)
    if moof_count > 1 and last_sample_end < int(len(source) * 0.75):
        print(
            "Skipping fragmented-to-flat rewrite because only an early prefix of media samples "
            f"was visible to the flattening pass ({len(primary_samples)} samples, last sample ends at "
            f"{last_sample_end}/{len(source)} bytes across {moof_count} moof boxes)."
        )
        return

    total_duration = sum(sample[2] for sample in primary_samples)

    def rebuild(current: Box, stco_offset: int) -> bytes:
        if current.type == b"mvex":
            return b""
        if current.type == b"stbl":
            stsd = None
            for child in current.children:
                if child.type == b"stsd":
                    stsd = child
                    break
            if stsd is None:
                return source[current.start : current.end]
            return fp_build_flat_sample_table(source[stsd.start : stsd.end], primary_samples, stco_offset)
        if current.children:
            payload_start = current.start + current.header_size
            payload = bytearray()
            position = payload_start
            for child in current.children:
                if position < child.start:
                    payload.extend(source[position : child.start])
                payload.extend(rebuild(child, stco_offset))
                position = child.end
            if position < current.end:
                payload.extend(source[position : current.end])
            return fp_make_box(current.type, bytes(payload))
        raw = source[current.start : current.end]
        if current.type in {b"mvhd", b"tkhd", b"mdhd"}:
            return fp_patch_duration_in_box(raw, current.type, total_duration)
        return raw

    stco_offset = 0
    new_moov = b""
    for _ in range(4):
        new_moov = rebuild(moov, stco_offset)
        stco_offset = (ftyp.end - ftyp.start) + len(new_moov) + 8

    mdat_payload = bytearray()
    for sample_offset, sample_size, _, _, _ in primary_samples:
        mdat_payload.extend(source[sample_offset : sample_offset + sample_size])

    flattened = source[ftyp.start : ftyp.end] + new_moov + fp_make_box(b"mdat", bytes(mdat_payload))
    with open(file_path, "wb") as file_handle:
        file_handle.write(flattened)


FP_STREAM_FILE_SIZE_THRESHOLD = 1024 * 1024 * 1024
FP_STREAM_MOOF_THRESHOLD = 4096
FP_STREAM_DURATION_THRESHOLD_SECONDS = 2 * 60 * 60
FP_STREAM_MAX_PATCHED_BOX_BYTES = 128 * 1024 * 1024
FP_STREAM_COPY_CHUNK = 4 * 1024 * 1024


def fp_stream_copy_file_range(
    file_handle,
    out_file,
    start: int,
    end: int,
    progress: Optional[ProgressPrinter] = None,
    chunk_size: int = FP_STREAM_COPY_CHUNK,
):
    if end < start:
        raise ValueError("Invalid copy range")
    file_handle.seek(start)
    remaining = end - start
    position = start
    while remaining > 0:
        take = min(chunk_size, remaining)
        chunk = file_handle.read(take)
        if not chunk:
            raise IOError("Unexpected EOF while copying input range")
        out_file.write(chunk)
        position += len(chunk)
        remaining -= len(chunk)
        if progress:
            progress.update(position)


def fp_read_exact_range(file_handle, start: int, size: int) -> bytes:
    if size < 0:
        raise ValueError("Invalid read size")
    file_handle.seek(start)
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = file_handle.read(min(FP_STREAM_COPY_CHUNK, remaining))
        if not chunk:
            raise IOError("Unexpected EOF while reading encrypted sample")
        chunks.append(chunk)
        remaining -= len(chunk)
    if len(chunks) == 1:
        return chunks[0]
    return b"".join(chunks)


def fp_decrypt_sample_to_bytes_from_file(file_handle, sample_start, sample_size, sample_aux, track, key, fix_sei=False):
    sample = bytearray(fp_read_exact_range(file_handle, sample_start, sample_size))
    fp_decrypt_sample(sample, 0, sample_size, sample_aux, track, key)
    result = bytes(sample)
    if fix_sei and fp_track_uses_hevc_bitstream(track):
        result = fp_repair_hevc_sei_rbsp_stop(result)
    return result


def fp_madvise_dontneed(data, start: int, end: int):
    madvise = getattr(data, "madvise", None)
    dontneed = getattr(mmap, "MADV_DONTNEED", None)
    if madvise is None or dontneed is None or end <= start:
        return
    try:
        madvise(dontneed, start, end - start)
    except (OSError, ValueError):
        pass


def fp_apply_absolute_patches_to_blob(blob: bytearray, blob_start: int, patches):
    for patch in patches:
        if len(patch) == 4:
            position, payload = patch[0], patch[3]
        else:
            position, payload = patch
        if not payload:
            continue
        local = position - blob_start
        if local < 0 or local + len(payload) > len(blob):
            continue
        blob[local : local + len(payload)] = payload


def fp_write_patched_box_range(
    data, out_file, start: int, end: int, patches: List[Tuple[int, bytes]], progress: Optional[ProgressPrinter] = None
):
    if end < start:
        raise ValueError("Invalid patched range")
    size = end - start
    if size > FP_STREAM_MAX_PATCHED_BOX_BYTES:
        fail(f"Refusing to buffer an unexpectedly large metadata box of {size} bytes in streaming path")
    blob = bytearray(data[start:end])
    fp_apply_absolute_patches_to_blob(blob, start, patches)
    out_file.write(blob)
    if progress:
        progress.update(end)


def fp_collect_decrypted_mp4_metadata_patches_for_moov(
    data, tracks, moov_start: int, moov_end: int
) -> List[Tuple[int, bytes]]:
    patches: List[Tuple[int, bytes]] = []
    for track_id in sorted(tracks):
        track = tracks[track_id]
        if fp_is_text_or_caption_track(track):
            continue
        if not track.get("encrypted"):
            continue
        original_format = track.get("original_format") or ""
        sample_entry_start = track.get("sample_entry_start")
        if (
            original_format
            and sample_entry_start is not None
            and moov_start <= sample_entry_start
            and sample_entry_start + 8 <= moov_end
        ):
            patches.append((sample_entry_start + 4, original_format.encode("latin1")[:4]))
        tenc_is_protected_offset = track.get("tenc_is_protected_offset")
        tenc_iv_size_offset = track.get("tenc_iv_size_offset")
        if tenc_is_protected_offset is not None and moov_start <= tenc_is_protected_offset < moov_end:
            patches.append((tenc_is_protected_offset, b"\x00"))
        if tenc_iv_size_offset is not None and moov_start <= tenc_iv_size_offset < moov_end:
            patches.append((tenc_iv_size_offset, b"\x00"))

    protection_boxes = {"sinf", "schm", "schi", "tenc", "senc", "saiz", "saio", "pssh"}
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(
        data, moov_start + 8, moov_end, protection_boxes
    ):
        if box_start + 8 <= moov_end:
            patches.append((box_start + 4, b"free"))
        is_sample_encryption_box = box_type == "senc" or (
            box_type == "uuid" and fp_box_uuid(data, box_start, box_header, box_type) == FP_PIFF_SAMPLE_ENCRYPTION_UUID
        )
        if is_sample_encryption_box:
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


def fp_collect_fragment_metadata_patches_for_range(data, range_start: int, range_end: int) -> List[Tuple[int, bytes]]:
    patches: List[Tuple[int, bytes]] = []
    protection_boxes = {"senc", "saiz", "saio", "pssh"}
    for box_start, box_end, box_header, box_type in fp_recursive_boxes(data, range_start, range_end, protection_boxes):
        if box_start + 8 <= range_end:
            patches.append((box_start + 4, b"free"))
        is_sample_encryption_box = box_type == "senc" or (
            box_type == "uuid" and fp_box_uuid(data, box_start, box_header, box_type) == FP_PIFF_SAMPLE_ENCRYPTION_UUID
        )
        if is_sample_encryption_box:
            fullbox_offset = box_start + box_header
            sample_count_offset = fullbox_offset + 4
            if sample_count_offset + 4 <= box_end:
                patches.append((fullbox_offset + 1, b"\x00\x00\x00"))
                patches.append((sample_count_offset, b"\x00\x00\x00\x00"))
        elif box_type in {"saiz", "saio"}:
            fullbox_offset = box_start + box_header
            if fullbox_offset + 4 <= box_end:
                patches.append((fullbox_offset + 1, b"\x00\x00\x00"))
    return patches


def fp_collect_top_level_box_metadata_patches(
    data, box_start: int, box_end: int, box_type: str
) -> List[Tuple[int, bytes]]:
    if box_type == "pssh" and box_start + 8 <= box_end:
        return [(box_start + 4, b"free")]
    return []


def fp_get_mvhd_duration_seconds(data, moov_start: int, moov_end: int) -> float:
    for box_start, box_end, box_header, box_type in fp_children(data, moov_start + 8, moov_end):
        if box_type != "mvhd":
            continue
        if box_start + box_header + 24 > box_end:
            return 0.0
        version, flags = fp_parse_fullbox(data, box_start, box_header)
        offset = box_start + box_header + 4
        if version == 1:
            if offset + 28 > box_end:
                return 0.0
            timescale = fp_be32(data, offset + 16)
            duration = fp_be64(data, offset + 20)
        else:
            if offset + 16 > box_end:
                return 0.0
            timescale = fp_be32(data, offset + 8)
            duration = fp_be32(data, offset + 12)
        if timescale <= 0:
            return 0.0
        return duration / timescale
    return 0.0


def fp_should_use_large_streaming(data, moov_start: int, moov_end: int) -> Tuple[bool, str]:
    file_size = len(data)
    if file_size >= FP_STREAM_FILE_SIZE_THRESHOLD:
        return True, f"file size {file_size} bytes"
    moof_count = fp_count_top_level_boxes(data, "moof")
    if moof_count >= FP_STREAM_MOOF_THRESHOLD:
        return True, f"{moof_count} moof fragments"
    duration_seconds = fp_get_mvhd_duration_seconds(data, moov_start, moov_end)
    if duration_seconds >= FP_STREAM_DURATION_THRESHOLD_SECONDS and moof_count > 1:
        return True, f"duration {duration_seconds:.3f}s across {moof_count} moof fragments"
    return False, ""


def fp_collect_fragments_for_single_moof(data, tracks, moof_start: int, moof_end: int, moof_header: int, next_header):
    fragments = []
    file_size = len(data)
    mdat_size_offset = None
    mdat_data_start = None
    if next_header is not None and next_header[3] == "mdat":
        mdat_size_offset = next_header[0]
        mdat_data_start = next_header[0] + next_header[2]

    for traf_start, traf_end, traf_header, traf_type in fp_children(data, moof_start + moof_header, moof_end):
        if traf_type != "traf":
            continue
        tfhd = None
        trun_boxes = []
        senc_box = None
        saiz_box = None
        saio_box = None
        for child_start, child_end, child_header, child_type in fp_children(data, traf_start + traf_header, traf_end):
            child_uuid = fp_box_uuid(data, child_start, child_header, child_type)
            if child_type == "tfhd":
                tfhd = fp_parse_tfhd(data, child_start, child_end, child_header)
            elif child_type == "trun":
                trun_boxes.append((child_start, child_end, child_header))
            elif child_type == "senc" or (child_type == "uuid" and child_uuid == FP_PIFF_SAMPLE_ENCRYPTION_UUID):
                senc_box = (child_start, child_end, child_header)
            elif child_type == "saiz":
                saiz_box = (child_start, child_end, child_header)
            elif child_type == "saio":
                saio_box = (child_start, child_end, child_header)

        if not tfhd or not trun_boxes:
            continue
        track_id = tfhd["track_id"]
        track = tracks.get(track_id)
        if not track or not track.get("encrypted"):
            continue

        senc_entries = None
        if senc_box is not None:
            senc_entries = fp_parse_senc(
                data, senc_box[0], senc_box[1], senc_box[2], track["iv_size"], track["constant_iv"]
            )
        elif saiz_box is not None and saio_box is not None:
            sample_info_sizes = fp_parse_saiz_fast(data, saiz_box[0], saiz_box[1], saiz_box[2])
            aux_offsets = fp_parse_saio_fast(data, saio_box[0], saio_box[1], saio_box[2])
            senc_entries = fp_parse_aux_info_from_saiz_saio_fast(
                data,
                sample_info_sizes,
                aux_offsets,
                track["iv_size"],
                track["constant_iv"],
                offset_base=moof_start,
            )
        if not senc_entries:
            continue

        trex = track.get("trex", {})
        default_sample_size = tfhd.get("default_sample_size", trex.get("default_sample_size", 0))
        base = tfhd.get("base_data_offset", moof_start)
        aux_index = 0
        previous_sample_end = None
        for trun_start, trun_end, trun_header in trun_boxes:
            sample_count, data_offset, samples = fp_parse_trun(data, trun_start, trun_end, trun_header)
            if data_offset is not None:
                sample_offset = base + data_offset
            elif previous_sample_end is not None:
                sample_offset = previous_sample_end
            elif mdat_data_start is not None:
                sample_offset = mdat_data_start
            else:
                sample_offset = base
            for sample in samples:
                sample_size = sample.get("size", default_sample_size)
                if sample_size <= 0:
                    aux_index += 1
                    continue
                if aux_index >= len(senc_entries):
                    sample_offset += sample_size
                    aux_index += 1
                    continue
                sample_aux = senc_entries[aux_index]
                sample_size_offset = sample.get("size_offset")
                if 0 <= sample_offset and sample_offset + sample_size <= file_size:
                    fragments.append(
                        (sample_offset, sample_size, sample_aux, track_id, sample_size_offset, mdat_size_offset)
                    )
                sample_offset += sample_size
                previous_sample_end = sample_offset
                aux_index += 1
    fragments.sort(key=lambda item: (item[0], item[1]))
    return fragments


def fp_stream_write_range_with_events(
    file_handle,
    data,
    out_file,
    start: int,
    end: int,
    events,
    progress: Optional[ProgressPrinter] = None,
    fix_sei: bool = False,
) -> int:
    events = [event for event in events if start <= event[0] and event[1] <= end]
    events.sort(key=lambda item: (item[0], 0 if item[2] == "patch" else 1, item[1]))
    previous_end = start
    for event in events:
        if event[0] < previous_end:
            fail("Overlapping stream events were generated in streaming path")
        previous_end = event[1]

    cursor = start
    processed = 0
    for event_start, event_end, event_kind, event_payload in events:
        if cursor < event_start:
            fp_stream_copy_file_range(file_handle, out_file, cursor, event_start, progress)
        if event_kind == "patch":
            out_file.write(event_payload)
            if progress:
                progress.update(event_end)
        else:
            sample_start, sample_size, sample_aux, track, key, sample_size_offset, mdat_size_offset = event_payload
            out_file.write(
                fp_decrypt_sample_to_bytes_from_file(
                    file_handle, sample_start, sample_size, sample_aux, track, key, fix_sei=fix_sei
                )
            )
            processed += 1
            if progress:
                progress.update(event_end)
        cursor = event_end
    if cursor < end:
        fp_stream_copy_file_range(file_handle, out_file, cursor, end, progress)
    return processed


def fp_make_box_header(box_type: bytes, payload_size: int) -> bytes:
    total_size = payload_size + 8
    if total_size <= 0xFFFFFFFF:
        return struct.pack(">I4s", total_size, box_type)
    return struct.pack(">I4sQ", 1, box_type, payload_size + 16)


def fp_write_u32(file_handle, value: int):
    file_handle.write(struct.pack(">I", value & 0xFFFFFFFF))


def fp_iter_u32_file(path: str, count: int):
    with open(path, "rb") as file_handle:
        for _ in range(count):
            raw = file_handle.read(4)
            if len(raw) != 4:
                raise IOError("Unexpected EOF while reading sample table")
            yield struct.unpack(">I", raw)[0]


def fp_compress_u32_file(path: str, count: int, last_delta: int = 0) -> List[Tuple[int, int]]:
    entries: List[Tuple[int, int]] = []
    for index, value in enumerate(fp_iter_u32_file(path, count)):
        if index == count - 1 and last_delta:
            value = (value + last_delta) & 0xFFFFFFFF
        if entries and entries[-1][1] == value:
            entries[-1] = (entries[-1][0] + 1, value)
        else:
            entries.append((1, value))
    return entries


def fp_choose_stream_primary_track(tracks) -> Optional[int]:
    for track_id in sorted(tracks):
        track = tracks[track_id]
        if (
            track.get("encrypted")
            and str(track.get("handler", "")).lower() == "vide"
            and not fp_is_text_or_caption_track(track)
        ):
            return track_id
    for track_id in sorted(tracks):
        track = tracks[track_id]
        if track.get("encrypted") and not fp_is_text_or_caption_track(track):
            return track_id
    return None


def fp_resolve_stream_fast_key(track_id: int, track, fast_keys):
    key = (
        fast_keys.get(str(track_id))
        or fast_keys.get(track.get("kid", ""))
        or fast_keys.get("00000000000000000000000000000000")
    )
    if key is None:
        fail(f"Missing key for KID {track.get('kid', '-')}")
    return key


def fp_collect_sample_records_for_single_moof(
    data, tracks, primary_track_id: int, moof_start: int, moof_end: int, moof_header: int, next_header
):
    records = []
    file_size = len(data)
    mdat_data_start = None
    if next_header is not None and next_header[3] == "mdat":
        mdat_data_start = next_header[0] + next_header[2]

    for traf_start, traf_end, traf_header, traf_type in fp_children(data, moof_start + moof_header, moof_end):
        if traf_type != "traf":
            continue
        tfhd = None
        trun_boxes = []
        senc_box = None
        saiz_box = None
        saio_box = None
        for child_start, child_end, child_header, child_type in fp_children(data, traf_start + traf_header, traf_end):
            child_uuid = fp_box_uuid(data, child_start, child_header, child_type)
            if child_type == "tfhd":
                tfhd = fp_parse_tfhd(data, child_start, child_end, child_header)
            elif child_type == "trun":
                trun_boxes.append((child_start, child_end, child_header))
            elif child_type == "senc" or (child_type == "uuid" and child_uuid == FP_PIFF_SAMPLE_ENCRYPTION_UUID):
                senc_box = (child_start, child_end, child_header)
            elif child_type == "saiz":
                saiz_box = (child_start, child_end, child_header)
            elif child_type == "saio":
                saio_box = (child_start, child_end, child_header)
        if not tfhd or not trun_boxes or tfhd["track_id"] != primary_track_id:
            continue
        track = tracks.get(primary_track_id)
        if not track or not track.get("encrypted"):
            continue
        if senc_box is not None:
            senc_entries = fp_parse_senc(
                data, senc_box[0], senc_box[1], senc_box[2], track["iv_size"], track["constant_iv"]
            )
        elif saiz_box is not None and saio_box is not None:
            sample_info_sizes = fp_parse_saiz_fast(data, saiz_box[0], saiz_box[1], saiz_box[2])
            aux_offsets = fp_parse_saio_fast(data, saio_box[0], saio_box[1], saio_box[2])
            senc_entries = fp_parse_aux_info_from_saiz_saio_fast(
                data,
                sample_info_sizes,
                aux_offsets,
                track["iv_size"],
                track["constant_iv"],
                offset_base=moof_start,
            )
        else:
            senc_entries = []
        if not senc_entries:
            continue

        trex = track.get("trex", {})
        default_sample_size = tfhd.get("default_sample_size", trex.get("default_sample_size", 0))
        default_sample_duration = tfhd.get("default_sample_duration", trex.get("default_sample_duration", 0))
        default_sample_flags = tfhd.get("default_sample_flags", trex.get("default_sample_flags", 0))
        base = tfhd.get("base_data_offset", moof_start)
        previous_sample_end = None
        aux_index = 0
        for trun_start, trun_end, trun_header in trun_boxes:
            sample_count, data_offset, samples = fp_parse_trun(data, trun_start, trun_end, trun_header)
            if data_offset is not None:
                sample_offset = base + data_offset
            elif previous_sample_end is not None:
                sample_offset = previous_sample_end
            elif mdat_data_start is not None:
                sample_offset = mdat_data_start
            else:
                sample_offset = base
            chunk_records = []
            for sample in samples:
                sample_size = sample.get("size", default_sample_size)
                sample_duration = sample.get("duration", default_sample_duration)
                sample_cto = sample.get("composition_time_offset", 0)
                sample_flags = sample.get("flags", default_sample_flags)
                if sample_size <= 0:
                    aux_index += 1
                    continue
                if aux_index >= len(senc_entries):
                    sample_offset += sample_size
                    aux_index += 1
                    continue
                sample_aux = senc_entries[aux_index]
                if 0 <= sample_offset and sample_offset + sample_size <= file_size:
                    chunk_records.append(
                        (
                            sample_offset,
                            sample_size,
                            sample_aux,
                            primary_track_id,
                            sample_duration,
                            sample_cto,
                            sample_flags,
                        )
                    )
                sample_offset += sample_size
                previous_sample_end = sample_offset
                aux_index += 1
            if chunk_records:
                records.append(chunk_records)
    return records


def fp_build_chunked_sample_table_from_file(
    original_stsd: bytes,
    table_paths: Dict[str, str],
    sample_count: int,
    chunk_sample_counts: List[int],
    chunk_offsets: List[int],
    target_duration: Optional[int],
    total_duration: int,
) -> bytes:
    duration_delta = 0
    if target_duration is not None and sample_count > 0:
        candidate_delta = target_duration - total_duration
        if 0 < candidate_delta <= 1000000000:
            duration_delta = candidate_delta

    stts_entries = fp_compress_u32_file(table_paths["durations"], sample_count, last_delta=duration_delta)
    stts_payload = struct.pack(">I", len(stts_entries)) + b"".join(
        struct.pack(">II", count, value) for count, value in stts_entries
    )
    stts = fp_make_full_box(b"stts", 0, 0, stts_payload)

    ctts_entries = fp_compress_u32_file(table_paths["ctos"], sample_count)
    ctts_payload = fp_pack_ctts_payload_with_fallback(ctts_entries)
    ctts = fp_make_full_box(b"ctts", 0, 0, ctts_payload)

    stsc_entries: List[Tuple[int, int, int]] = []
    previous_samples_per_chunk = None
    for chunk_index, samples_per_chunk in enumerate(chunk_sample_counts, 1):
        if samples_per_chunk <= 0:
            continue
        if samples_per_chunk != previous_samples_per_chunk:
            stsc_entries.append((chunk_index, samples_per_chunk, 1))
            previous_samples_per_chunk = samples_per_chunk
    stsc_payload = struct.pack(">I", len(stsc_entries)) + b"".join(
        struct.pack(">III", *entry) for entry in stsc_entries
    )
    stsc = fp_make_full_box(b"stsc", 0, 0, stsc_payload)

    stsz_payload = bytearray(struct.pack(">II", 0, sample_count))
    for size in fp_iter_u32_file(table_paths["sizes"], sample_count):
        stsz_payload.extend(struct.pack(">I", size))
    stsz = fp_make_full_box(b"stsz", 0, 0, bytes(stsz_payload))

    if chunk_offsets and max(chunk_offsets) > 0xFFFFFFFF:
        chunk_table_payload = struct.pack(">I", len(chunk_offsets)) + b"".join(
            struct.pack(">Q", offset) for offset in chunk_offsets
        )
        chunk_table = fp_make_full_box(b"co64", 0, 0, chunk_table_payload)
    else:
        chunk_table_payload = struct.pack(">I", len(chunk_offsets)) + b"".join(
            struct.pack(">I", offset & 0xFFFFFFFF) for offset in chunk_offsets
        )
        chunk_table = fp_make_full_box(b"stco", 0, 0, chunk_table_payload)

    sync_samples = []
    sdtp_payload = bytearray()
    for index, sample_flags in enumerate(fp_iter_u32_file(table_paths["flags"], sample_count), 1):
        if not (sample_flags & 0x00010000):
            sync_samples.append(index)
        sample_depends_on = (sample_flags >> 24) & 0x03
        sample_is_depended_on = (sample_flags >> 22) & 0x03
        sample_has_redundancy = (sample_flags >> 20) & 0x03
        sdtp_payload.append((sample_depends_on << 4) | (sample_is_depended_on << 2) | sample_has_redundancy)
    stss = fp_make_full_box(
        b"stss",
        0,
        0,
        struct.pack(">I", len(sync_samples)) + b"".join(struct.pack(">I", index) for index in sync_samples),
    )
    sdtp = fp_make_full_box(b"sdtp", 0, 0, bytes(sdtp_payload))

    return fp_make_box(b"stbl", original_stsd + stts + ctts + stsc + stsz + chunk_table + stss + sdtp)


def fp_rebuild_moov_for_stream_flatten(
    moov_blob: bytes,
    table_paths: Dict[str, str],
    sample_count: int,
    chunk_sample_counts: List[int],
    chunk_offsets: List[int],
    target_duration: Optional[int],
    total_duration: int,
) -> bytes:
    moov_parser = Mp4Parser(moov_blob)
    moov_box = None
    for top in moov_parser.root:
        if top.type == b"moov":
            moov_box = top
            break
    if moov_box is None:
        fail("No moov box found while rebuilding streaming output")

    def rebuild(current: Box) -> bytes:
        if current.type == b"mvex":
            return b""
        if current.type == b"stbl":
            stsd = None
            for child in current.children:
                if child.type == b"stsd":
                    stsd = child
                    break
            if stsd is None:
                return moov_blob[current.start : current.end]
            return fp_build_chunked_sample_table_from_file(
                moov_blob[stsd.start : stsd.end],
                table_paths,
                sample_count,
                chunk_sample_counts,
                chunk_offsets,
                target_duration,
                total_duration,
            )
        if current.children:
            payload_start = current.start + current.header_size
            payload = bytearray()
            position = payload_start
            for child in current.children:
                if position < child.start:
                    payload.extend(moov_blob[position : child.start])
                payload.extend(rebuild(child))
                position = child.end
            if position < current.end:
                payload.extend(moov_blob[position : current.end])
            return fp_make_box(current.type, bytes(payload))
        return moov_blob[current.start : current.end]

    return rebuild(moov_box)


def fp_copy_fileobj_range(in_file, out_file, size: int, chunk_size: int = FP_STREAM_COPY_CHUNK):
    remaining = size
    while remaining > 0:
        chunk = in_file.read(min(chunk_size, remaining))
        if not chunk:
            raise IOError("Unexpected EOF while copying media payload")
        out_file.write(chunk)
        remaining -= len(chunk)


def fp_iter_primary_stream_chunks(data, tracks, primary_track_id: int):
    file_size = len(data)
    offset = 0
    while offset + 8 <= file_size:
        header = fp_read_box_header(data, offset, file_size)
        if header is None:
            break
        box_start, box_end, box_header, box_type = header
        if box_type != "moof":
            offset = box_end
            continue
        next_header = fp_read_box_header(data, box_end, file_size)
        grouped_records = fp_collect_sample_records_for_single_moof(
            data, tracks, primary_track_id, box_start, box_end, box_header, next_header
        )
        for chunk_records in grouped_records:
            if chunk_records:
                yield chunk_records
        if next_header is not None and next_header[3] == "mdat":
            offset = next_header[1]
        else:
            offset = box_end


def fp_stream_decrypted_sample_size(
    file_handle, sample_offset: int, sample_size: int, sample_aux, track, key, fix_sei: bool
) -> int:
    if fix_sei:
        return len(
            fp_decrypt_sample_to_bytes_from_file(
                file_handle, sample_offset, sample_size, sample_aux, track, key, fix_sei=True
            )
        )
    return sample_size


def fp_collect_direct_flatten_stats(
    data, file_handle, tracks, primary_track_id: int, primary_track, primary_key, fix_sei: bool
) -> Dict[str, int]:
    sample_count = 0
    chunk_count = 0
    media_payload_size = 0
    total_duration = 0
    for chunk_records in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
        chunk_samples = 0
        chunk_payload_size = 0
        for (
            sample_offset,
            sample_size,
            sample_aux,
            track_id,
            sample_duration,
            sample_cto,
            sample_flags,
        ) in chunk_records:
            out_size = fp_stream_decrypted_sample_size(
                file_handle, sample_offset, sample_size, sample_aux, primary_track, primary_key, fix_sei
            )
            sample_count += 1
            chunk_samples += 1
            chunk_payload_size += out_size
            total_duration += int(sample_duration or 0)
        if chunk_samples > 0:
            chunk_count += 1
            media_payload_size += chunk_payload_size
    return {
        "sample_count": sample_count,
        "chunk_count": chunk_count,
        "media_payload_size": media_payload_size,
        "total_duration": total_duration,
    }


def fp_iter_direct_flatten_sample_values(
    data, file_handle, tracks, primary_track_id: int, primary_track, primary_key, fix_sei: bool, duration_delta: int = 0
):
    produced = 0
    total_samples = None
    if duration_delta:
        total_samples = 0
        for chunk_records in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
            total_samples += len(chunk_records)
    for chunk_records in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
        for (
            sample_offset,
            sample_size,
            sample_aux,
            track_id,
            sample_duration,
            sample_cto,
            sample_flags,
        ) in chunk_records:
            produced += 1
            out_size = fp_stream_decrypted_sample_size(
                file_handle, sample_offset, sample_size, sample_aux, primary_track, primary_key, fix_sei
            )
            duration = int(sample_duration or 0)
            if duration_delta and total_samples is not None and produced == total_samples:
                duration = (duration + duration_delta) & 0xFFFFFFFF
            yield {
                "duration": duration,
                "cto": int(sample_cto or 0),
                "size": out_size,
                "flags": int(sample_flags or 0),
            }


def fp_count_compressed_entries_from_values(values) -> int:
    count = 0
    previous = None
    for value in values:
        if previous is None or value != previous:
            count += 1
            previous = value
    return count


def fp_count_stsc_entries(data, tracks, primary_track_id: int) -> int:
    count = 0
    previous = None
    for chunk_records in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
        samples_per_chunk = len(chunk_records)
        if samples_per_chunk <= 0:
            continue
        if previous is None or samples_per_chunk != previous:
            count += 1
            previous = samples_per_chunk
    return count


def fp_count_sync_samples(
    data, file_handle, tracks, primary_track_id: int, primary_track, primary_key, fix_sei: bool, duration_delta: int = 0
) -> int:
    count = 0
    for item in fp_iter_direct_flatten_sample_values(
        data, file_handle, tracks, primary_track_id, primary_track, primary_key, fix_sei, duration_delta=duration_delta
    ):
        if not (item["flags"] & 0x00010000):
            count += 1
    return count


def fp_get_stsd_from_first_stbl(moov_blob: bytes, moov_box: Box) -> Optional[bytes]:
    for trak in moov_box.children:
        if trak.type != b"trak":
            continue
        for mdia in trak.children:
            if mdia.type != b"mdia":
                continue
            for minf in mdia.children:
                if minf.type != b"minf":
                    continue
                for stbl in minf.children:
                    if stbl.type != b"stbl":
                        continue
                    for child in stbl.children:
                        if child.type == b"stsd":
                            return moov_blob[child.start : child.end]
    return None


def fp_direct_flatten_table_sizes(
    stsd_size: int,
    sample_count: int,
    chunk_count: int,
    stts_count: int,
    ctts_count: int,
    stsc_count: int,
    sync_count: int,
    use_co64: bool,
) -> Dict[str, int]:
    chunk_offset_entry_size = 8 if use_co64 else 4
    return {
        "stsd": stsd_size,
        "stts": 16 + 8 * stts_count,
        "ctts": 16 + 8 * ctts_count,
        "stsc": 16 + 12 * stsc_count,
        "stsz": 20 + 4 * sample_count,
        "chunk_table": 16 + chunk_offset_entry_size * chunk_count,
        "stss": 16 + 4 * sync_count,
        "sdtp": 12 + sample_count,
    }


def fp_direct_flatten_stbl_size(table_sizes: Dict[str, int]) -> int:
    return 8 + sum(table_sizes.values())


def fp_compute_rebuilt_box_size(current: Box, replacement_stbl_size: int) -> int:
    if current.type == b"mvex":
        return 0
    if current.type == b"stbl":
        return replacement_stbl_size
    if current.children:
        payload_size = 0
        payload_start = current.start + current.header_size
        position = payload_start
        for child in current.children:
            if position < child.start:
                payload_size += child.start - position
            payload_size += fp_compute_rebuilt_box_size(child, replacement_stbl_size)
            position = child.end
        if position < current.end:
            payload_size += current.end - position
        return 8 + payload_size
    return current.size


def fp_write_progress(out_file, progress: Optional[ProgressPrinter] = None):
    if progress:
        progress.update(out_file.tell())


FP_STREAM_TABLE_BUFFER = 1024 * 1024


def fp_flush_table_buffer(out_file, buffer: bytearray, progress: Optional[ProgressPrinter] = None):
    if buffer:
        out_file.write(buffer)
        buffer.clear()
        fp_write_progress(out_file, progress)


def fp_buffer_table_bytes(out_file, buffer: bytearray, payload: bytes, progress: Optional[ProgressPrinter] = None):
    if not payload:
        return
    if len(payload) > FP_STREAM_TABLE_BUFFER:
        fp_flush_table_buffer(out_file, buffer, progress)
        out_file.write(payload)
        fp_write_progress(out_file, progress)
        return
    if len(buffer) + len(payload) > FP_STREAM_TABLE_BUFFER:
        fp_flush_table_buffer(out_file, buffer, progress)
    buffer.extend(payload)


def fp_write_box_header_known(out_file, box_type: bytes, total_size: int, progress: Optional[ProgressPrinter] = None):
    if total_size <= 0xFFFFFFFF:
        out_file.write(struct.pack(">I4s", total_size, box_type))
    else:
        out_file.write(struct.pack(">I4sQ", 1, box_type, total_size))
    fp_write_progress(out_file, progress)


def fp_write_full_box_header_known(
    out_file,
    box_type: bytes,
    total_size: int,
    version: int = 0,
    flags: int = 0,
    progress: Optional[ProgressPrinter] = None,
):
    fp_write_box_header_known(out_file, box_type, total_size, progress=None)
    out_file.write(bytes([version & 0xFF]) + (flags & 0xFFFFFF).to_bytes(3, "big"))
    fp_write_progress(out_file, progress)


def fp_stream_copy_moov_blob_range(
    moov_blob: bytes, out_file, start: int, end: int, progress: Optional[ProgressPrinter] = None
):
    position = start
    while position < end:
        chunk_end = min(end, position + FP_STREAM_COPY_CHUNK)
        out_file.write(moov_blob[position:chunk_end])
        position = chunk_end
        fp_write_progress(out_file, progress)


def fp_write_compressed_time_table(
    out_file, box_type: bytes, total_size: int, entry_count: int, values, progress: Optional[ProgressPrinter] = None
):
    fp_write_full_box_header_known(out_file, box_type, total_size, 0, 0, progress=None)
    out_file.write(struct.pack(">I", entry_count))
    previous = None
    run_count = 0
    buffer = bytearray()
    for value in values:
        if previous is None:
            previous = value
            run_count = 1
        elif value == previous:
            run_count += 1
        else:
            fp_buffer_table_bytes(out_file, buffer, struct.pack(">II", run_count, previous & 0xFFFFFFFF), progress)
            previous = value
            run_count = 1
    if previous is not None:
        fp_buffer_table_bytes(out_file, buffer, struct.pack(">II", run_count, previous & 0xFFFFFFFF), progress)
    fp_flush_table_buffer(out_file, buffer, progress)


def fp_write_stsc_direct(
    out_file,
    total_size: int,
    entry_count: int,
    data,
    tracks,
    primary_track_id: int,
    progress: Optional[ProgressPrinter] = None,
):
    fp_write_full_box_header_known(out_file, b"stsc", total_size, 0, 0, progress=None)
    out_file.write(struct.pack(">I", entry_count))
    previous = None
    chunk_index = 0
    buffer = bytearray()
    for chunk_records in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
        samples_per_chunk = len(chunk_records)
        if samples_per_chunk <= 0:
            continue
        chunk_index += 1
        if previous is None or samples_per_chunk != previous:
            fp_buffer_table_bytes(out_file, buffer, struct.pack(">III", chunk_index, samples_per_chunk, 1), progress)
            previous = samples_per_chunk
    fp_flush_table_buffer(out_file, buffer, progress)


def fp_write_stsz_direct(
    out_file,
    total_size: int,
    sample_count: int,
    data,
    file_handle,
    tracks,
    primary_track_id: int,
    primary_track,
    primary_key,
    fix_sei: bool,
    duration_delta: int,
    progress: Optional[ProgressPrinter] = None,
):
    fp_write_full_box_header_known(out_file, b"stsz", total_size, 0, 0, progress=None)
    out_file.write(struct.pack(">II", 0, sample_count))
    buffer = bytearray()
    for item in fp_iter_direct_flatten_sample_values(
        data, file_handle, tracks, primary_track_id, primary_track, primary_key, fix_sei, duration_delta=duration_delta
    ):
        fp_buffer_table_bytes(out_file, buffer, struct.pack(">I", item["size"] & 0xFFFFFFFF), progress)
    fp_flush_table_buffer(out_file, buffer, progress)


def fp_write_chunk_offsets_direct(
    out_file,
    total_size: int,
    mdat_data_start: int,
    use_co64: bool,
    data,
    file_handle,
    tracks,
    primary_track_id: int,
    primary_track,
    primary_key,
    fix_sei: bool,
    progress: Optional[ProgressPrinter] = None,
):
    box_type = b"co64" if use_co64 else b"stco"
    fp_write_full_box_header_known(out_file, box_type, total_size, 0, 0, progress=None)
    chunk_count = 0
    for _ in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
        chunk_count += 1
    out_file.write(struct.pack(">I", chunk_count))
    cursor = mdat_data_start
    buffer = bytearray()
    for chunk_records in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
        if use_co64:
            fp_buffer_table_bytes(out_file, buffer, struct.pack(">Q", cursor), progress)
        else:
            fp_buffer_table_bytes(out_file, buffer, struct.pack(">I", cursor & 0xFFFFFFFF), progress)
        chunk_payload_size = 0
        for (
            sample_offset,
            sample_size,
            sample_aux,
            track_id,
            sample_duration,
            sample_cto,
            sample_flags,
        ) in chunk_records:
            chunk_payload_size += fp_stream_decrypted_sample_size(
                file_handle, sample_offset, sample_size, sample_aux, primary_track, primary_key, fix_sei
            )
        cursor += chunk_payload_size
    fp_flush_table_buffer(out_file, buffer, progress)


def fp_write_stss_direct(
    out_file,
    total_size: int,
    sync_count: int,
    data,
    file_handle,
    tracks,
    primary_track_id: int,
    primary_track,
    primary_key,
    fix_sei: bool,
    duration_delta: int,
    progress: Optional[ProgressPrinter] = None,
):
    fp_write_full_box_header_known(out_file, b"stss", total_size, 0, 0, progress=None)
    out_file.write(struct.pack(">I", sync_count))
    index = 0
    buffer = bytearray()
    for item in fp_iter_direct_flatten_sample_values(
        data, file_handle, tracks, primary_track_id, primary_track, primary_key, fix_sei, duration_delta=duration_delta
    ):
        index += 1
        if not (item["flags"] & 0x00010000):
            fp_buffer_table_bytes(out_file, buffer, struct.pack(">I", index), progress)
    fp_flush_table_buffer(out_file, buffer, progress)


def fp_write_sdtp_direct(
    out_file,
    total_size: int,
    data,
    file_handle,
    tracks,
    primary_track_id: int,
    primary_track,
    primary_key,
    fix_sei: bool,
    duration_delta: int,
    progress: Optional[ProgressPrinter] = None,
):
    fp_write_full_box_header_known(out_file, b"sdtp", total_size, 0, 0, progress=None)
    buffer = bytearray()
    for item in fp_iter_direct_flatten_sample_values(
        data, file_handle, tracks, primary_track_id, primary_track, primary_key, fix_sei, duration_delta=duration_delta
    ):
        sample_flags = item["flags"]
        sample_depends_on = (sample_flags >> 24) & 0x03
        sample_is_depended_on = (sample_flags >> 22) & 0x03
        sample_has_redundancy = (sample_flags >> 20) & 0x03
        fp_buffer_table_bytes(
            out_file,
            buffer,
            bytes([(sample_depends_on << 4) | (sample_is_depended_on << 2) | sample_has_redundancy]),
            progress,
        )
    fp_flush_table_buffer(out_file, buffer, progress)


def fp_write_stbl_direct(
    out_file,
    moov_blob: bytes,
    stsd_bytes: bytes,
    table_sizes: Dict[str, int],
    sample_count: int,
    chunk_count: int,
    stts_count: int,
    ctts_count: int,
    stsc_count: int,
    sync_count: int,
    mdat_data_start: int,
    use_co64: bool,
    data,
    file_handle,
    tracks,
    primary_track_id: int,
    primary_track,
    primary_key,
    fix_sei: bool,
    duration_delta: int,
    progress: Optional[ProgressPrinter] = None,
):
    fp_write_box_header_known(out_file, b"stbl", fp_direct_flatten_stbl_size(table_sizes), progress=None)
    out_file.write(stsd_bytes)
    fp_write_progress(out_file, progress)
    print("Writing media tables: stsd done", file=sys.stderr, flush=True)
    fp_write_compressed_time_table(
        out_file,
        b"stts",
        table_sizes["stts"],
        stts_count,
        (
            item["duration"]
            for item in fp_iter_direct_flatten_sample_values(
                data,
                file_handle,
                tracks,
                primary_track_id,
                primary_track,
                primary_key,
                fix_sei,
                duration_delta=duration_delta,
            )
        ),
        progress,
    )
    print("Writing media tables: stts done", file=sys.stderr, flush=True)
    fp_write_compressed_time_table(
        out_file,
        b"ctts",
        table_sizes["ctts"],
        ctts_count,
        (
            item["cto"]
            for item in fp_iter_direct_flatten_sample_values(
                data,
                file_handle,
                tracks,
                primary_track_id,
                primary_track,
                primary_key,
                fix_sei,
                duration_delta=duration_delta,
            )
        ),
        progress,
    )
    print("Writing media tables: ctts done", file=sys.stderr, flush=True)
    fp_write_stsc_direct(out_file, table_sizes["stsc"], stsc_count, data, tracks, primary_track_id, progress)
    print("Writing media tables: stsc done", file=sys.stderr, flush=True)
    fp_write_stsz_direct(
        out_file,
        table_sizes["stsz"],
        sample_count,
        data,
        file_handle,
        tracks,
        primary_track_id,
        primary_track,
        primary_key,
        fix_sei,
        duration_delta,
        progress,
    )
    print("Writing media tables: stsz done", file=sys.stderr, flush=True)
    fp_write_chunk_offsets_direct(
        out_file,
        table_sizes["chunk_table"],
        mdat_data_start,
        use_co64,
        data,
        file_handle,
        tracks,
        primary_track_id,
        primary_track,
        primary_key,
        fix_sei,
        progress,
    )
    print("Writing media tables: chunk offsets done", file=sys.stderr, flush=True)
    fp_write_stss_direct(
        out_file,
        table_sizes["stss"],
        sync_count,
        data,
        file_handle,
        tracks,
        primary_track_id,
        primary_track,
        primary_key,
        fix_sei,
        duration_delta,
        progress,
    )
    print("Writing media tables: stss done", file=sys.stderr, flush=True)
    fp_write_sdtp_direct(
        out_file,
        table_sizes["sdtp"],
        data,
        file_handle,
        tracks,
        primary_track_id,
        primary_track,
        primary_key,
        fix_sei,
        duration_delta,
        progress,
    )


def fp_write_rebuilt_moov_direct(
    out_file,
    current: Box,
    moov_blob: bytes,
    replacement_stbl_size: int,
    stsd_bytes: bytes,
    table_sizes: Dict[str, int],
    sample_count: int,
    chunk_count: int,
    stts_count: int,
    ctts_count: int,
    stsc_count: int,
    sync_count: int,
    mdat_data_start: int,
    use_co64: bool,
    data,
    file_handle,
    tracks,
    primary_track_id: int,
    primary_track,
    primary_key,
    fix_sei: bool,
    duration_delta: int,
    progress: Optional[ProgressPrinter] = None,
):
    if current.type == b"mvex":
        return
    if current.type == b"stbl":
        fp_write_stbl_direct(
            out_file,
            moov_blob,
            stsd_bytes,
            table_sizes,
            sample_count,
            chunk_count,
            stts_count,
            ctts_count,
            stsc_count,
            sync_count,
            mdat_data_start,
            use_co64,
            data,
            file_handle,
            tracks,
            primary_track_id,
            primary_track,
            primary_key,
            fix_sei,
            duration_delta,
            progress,
        )
        return
    if current.children:
        size = fp_compute_rebuilt_box_size(current, replacement_stbl_size)
        fp_write_box_header_known(out_file, current.type, size, progress=None)
        payload_start = current.start + current.header_size
        position = payload_start
        for child in current.children:
            if position < child.start:
                fp_stream_copy_moov_blob_range(moov_blob, out_file, position, child.start, progress)
            fp_write_rebuilt_moov_direct(
                out_file,
                child,
                moov_blob,
                replacement_stbl_size,
                stsd_bytes,
                table_sizes,
                sample_count,
                chunk_count,
                stts_count,
                ctts_count,
                stsc_count,
                sync_count,
                mdat_data_start,
                use_co64,
                data,
                file_handle,
                tracks,
                primary_track_id,
                primary_track,
                primary_key,
                fix_sei,
                duration_delta,
                progress,
            )
            position = child.end
        if position < current.end:
            fp_stream_copy_moov_blob_range(moov_blob, out_file, position, current.end, progress)
        fp_write_progress(out_file, progress)
        return
    fp_stream_copy_moov_blob_range(moov_blob, out_file, current.start, current.end, progress)


def fp_decrypt_mp4_large_streaming_flatten(
    input_path: str, output_path: str, data, file_handle, tracks, fast_keys, drop_text: bool, fix_sei: bool, reason: str
):
    file_size = len(data)
    primary_track_id = fp_choose_stream_primary_track(tracks)
    if primary_track_id is None:
        with open(output_path, "wb") as out_file:
            progress = ProgressPrinter(file_size)
            fp_stream_copy_file_range(file_handle, out_file, 0, file_size, progress)
            progress.finish()
        print("No encrypted fragmented samples found")
        return
    primary_track = tracks[primary_track_id]
    primary_key = fp_resolve_stream_fast_key(primary_track_id, primary_track, fast_keys)

    ftyp_start = ftyp_end = moov_start = moov_end = None
    for box_start, box_end, box_header, box_type in fp_children(data, 0, file_size):
        if box_type == "ftyp" and ftyp_start is None:
            ftyp_start, ftyp_end = box_start, box_end
        elif box_type == "moov" and moov_start is None:
            moov_start, moov_end = box_start, box_end
    if ftyp_start is None or moov_start is None:
        fail("Missing ftyp/moov boxes in streaming flatten path")

    print(f"Detected large/long fragmented MP4 ({reason}); using direct streaming decrypt + flat rewrite.")
    stats = fp_collect_direct_flatten_stats(
        data, file_handle, tracks, primary_track_id, primary_track, primary_key, fix_sei
    )
    sample_count = stats["sample_count"]
    chunk_count = stats["chunk_count"]
    media_payload_size = stats["media_payload_size"]
    total_duration = stats["total_duration"]
    if sample_count <= 0:
        with open(output_path, "wb") as out_file:
            progress = ProgressPrinter(file_size)
            fp_stream_copy_file_range(file_handle, out_file, 0, file_size, progress)
            progress.finish()
        print("No encrypted fragmented samples found")
        return

    moov_blob = bytearray(data[moov_start:moov_end])
    moov_patches: List[Tuple[int, bytes]] = []
    if drop_text:
        moov_patches.extend(
            (pos, payload) for pos, payload in fp_collect_text_track_patches(data) if moov_start <= pos < moov_end
        )
    moov_patches.extend(fp_collect_decrypted_mp4_metadata_patches_for_moov(data, tracks, moov_start, moov_end))
    fp_apply_absolute_patches_to_blob(moov_blob, moov_start, fp_prepare_patch_events(moov_patches))
    moov_bytes = bytes(moov_blob)
    moov_parser = Mp4Parser(moov_bytes)
    moov_box = None
    for top in moov_parser.root:
        if top.type == b"moov":
            moov_box = top
            break
    if moov_box is None:
        fail("No moov box found while preparing streaming output")
    stsd_bytes = fp_get_stsd_from_first_stbl(moov_bytes, moov_box)
    if not stsd_bytes:
        fail("No stsd box found while preparing streaming output")
    target_duration = fp_get_track_duration_from_moov(moov_bytes, moov_box)
    duration_delta = 0
    if target_duration > 0 and sample_count > 0:
        candidate_delta = target_duration - total_duration
        if 0 < candidate_delta <= 1000000000:
            duration_delta = candidate_delta

    stts_count = fp_count_compressed_entries_from_values(
        item["duration"]
        for item in fp_iter_direct_flatten_sample_values(
            data,
            file_handle,
            tracks,
            primary_track_id,
            primary_track,
            primary_key,
            fix_sei,
            duration_delta=duration_delta,
        )
    )
    ctts_count = fp_count_compressed_entries_from_values(
        item["cto"]
        for item in fp_iter_direct_flatten_sample_values(
            data,
            file_handle,
            tracks,
            primary_track_id,
            primary_track,
            primary_key,
            fix_sei,
            duration_delta=duration_delta,
        )
    )
    stsc_count = fp_count_stsc_entries(data, tracks, primary_track_id)
    sync_count = fp_count_sync_samples(
        data, file_handle, tracks, primary_track_id, primary_track, primary_key, fix_sei, duration_delta=duration_delta
    )

    ftyp_size = ftyp_end - ftyp_start
    mdat_header_size = 16 if media_payload_size + 8 > 0xFFFFFFFF else 8
    mdat_data_start = ftyp_size + mdat_header_size
    use_co64 = (mdat_data_start + media_payload_size) > 0xFFFFFFFF
    table_sizes = fp_direct_flatten_table_sizes(
        len(stsd_bytes), sample_count, chunk_count, stts_count, ctts_count, stsc_count, sync_count, use_co64
    )
    replacement_stbl_size = fp_direct_flatten_stbl_size(table_sizes)
    rebuilt_moov_size = fp_compute_rebuilt_box_size(moov_box, replacement_stbl_size)
    final_output_size = ftyp_size + mdat_header_size + media_payload_size + rebuilt_moov_size

    progress = ProgressPrinter(final_output_size)
    with open(output_path, "wb") as out_file:
        fp_stream_copy_file_range(file_handle, out_file, ftyp_start, ftyp_end, None)
        fp_write_progress(out_file, progress)
        out_file.write(fp_make_box_header(b"mdat", media_payload_size))
        fp_write_progress(out_file, progress)
        for chunk_records in fp_iter_primary_stream_chunks(data, tracks, primary_track_id):
            for (
                sample_offset,
                sample_size,
                sample_aux,
                track_id,
                sample_duration,
                sample_cto,
                sample_flags,
            ) in chunk_records:
                decrypted = fp_decrypt_sample_to_bytes_from_file(
                    file_handle, sample_offset, sample_size, sample_aux, primary_track, primary_key, fix_sei=fix_sei
                )
                out_file.write(decrypted)
                fp_write_progress(out_file, progress)
        print("Writing final metadata tables", file=sys.stderr, flush=True)
        fp_write_rebuilt_moov_direct(
            out_file,
            moov_box,
            moov_bytes,
            replacement_stbl_size,
            stsd_bytes,
            table_sizes,
            sample_count,
            chunk_count,
            stts_count,
            ctts_count,
            stsc_count,
            sync_count,
            mdat_data_start,
            use_co64,
            data,
            file_handle,
            tracks,
            primary_track_id,
            primary_track,
            primary_key,
            fix_sei,
            duration_delta,
            progress,
        )
    progress.finish()
    print(
        "Streaming path decrypted and flattened "
        f"{sample_count} samples into {chunk_count} chunks without sidecar files."
    )


def fp_decrypt_mp4_large_streaming(
    input_path: str, output_path: str, data, file_handle, tracks, fast_keys, drop_text: bool, fix_sei: bool, reason: str
):
    file_size = len(data)
    progress = ProgressPrinter(file_size)
    processed_samples = 0
    print(f"Detected large/long fragmented MP4 ({reason}); using bounded-memory streaming path.")
    with open(output_path, "wb") as out_file:
        offset = 0
        while offset + 8 <= file_size:
            header = fp_read_box_header(data, offset, file_size)
            if header is None:
                break
            box_start, box_end, box_header, box_type = header
            if box_type == "moov":
                patches: List[Tuple[int, bytes]] = []
                if drop_text:
                    patches.extend(
                        (pos, payload)
                        for pos, payload in fp_collect_text_track_patches(data)
                        if box_start <= pos < box_end
                    )
                patches.extend(fp_collect_decrypted_mp4_metadata_patches_for_moov(data, tracks, box_start, box_end))
                fp_write_patched_box_range(
                    data, out_file, box_start, box_end, fp_prepare_patch_events(patches), progress
                )
                fp_madvise_dontneed(data, box_start, box_end)
                offset = box_end
                continue

            if box_type == "moof":
                next_header = fp_read_box_header(data, box_end, file_size)
                fragments = fp_collect_fragments_for_single_moof(
                    data, tracks, box_start, box_end, box_header, next_header
                )
                decrypt_events = fp_prepare_decrypt_events(fragments, tracks, fast_keys) if fragments else []
                growth_patches, repaired_sei_count = (
                    fp_collect_growth_patches(data, decrypt_events, fix_sei=fix_sei) if decrypt_events else ([], 0)
                )
                moof_patches = fp_collect_fragment_metadata_patches_for_range(data, box_start, box_end)
                moof_patches.extend((pos, payload) for pos, payload in growth_patches if box_start <= pos < box_end)
                fp_write_patched_box_range(
                    data, out_file, box_start, box_end, fp_prepare_patch_events(moof_patches), progress
                )
                fp_madvise_dontneed(data, box_start, box_end)

                if next_header is not None and next_header[3] == "mdat":
                    mdat_start, mdat_end, mdat_header, mdat_type = next_header
                    mdat_patch_events = [
                        (pos, pos + len(payload), "patch", payload)
                        for pos, payload in growth_patches
                        if mdat_start <= pos < mdat_end and payload
                    ]
                    processed_samples += fp_stream_write_range_with_events(
                        file_handle,
                        data,
                        out_file,
                        mdat_start,
                        mdat_end,
                        mdat_patch_events + decrypt_events,
                        progress,
                        fix_sei=fix_sei,
                    )
                    fp_madvise_dontneed(data, mdat_start, mdat_end)
                    offset = mdat_end
                else:
                    processed_samples += len(decrypt_events)
                    offset = box_end
                continue

            local_patches = fp_collect_top_level_box_metadata_patches(data, box_start, box_end, box_type)
            if local_patches:
                fp_write_patched_box_range(
                    data, out_file, box_start, box_end, fp_prepare_patch_events(local_patches), progress
                )
            else:
                fp_stream_copy_file_range(file_handle, out_file, box_start, box_end, progress)
            fp_madvise_dontneed(data, box_start, box_end)
            offset = box_end

        if offset < file_size:
            fp_stream_copy_file_range(file_handle, out_file, offset, file_size, progress)
    progress.finish()
    if processed_samples <= 0:
        print("No encrypted fragmented samples found")
    else:
        print(f"Streaming path decrypted {processed_samples} samples without flattening or full-file buffering.")


def decrypt_mp4_file(
    input_path: str,
    output_path: str,
    keys_by_track: Dict[int, bytes],
    keys_by_kid: Dict[bytes, bytes],
    show_tracks: bool = False,
    drop_text: bool = True,
    fix_sei: bool = False,
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

            use_streaming, streaming_reason = fp_should_use_large_streaming(data, moov_start, moov_end)
            if use_streaming:
                fp_decrypt_mp4_large_streaming_flatten(
                    input_path,
                    output_path,
                    data,
                    file_handle,
                    tracks,
                    fast_keys,
                    drop_text=drop_text,
                    fix_sei=fix_sei,
                    reason=streaming_reason,
                )
                print("Decrypted successfully")
                return

            fragments, total_samples = fp_collect_fragments_with_fallback(data, tracks)
            if total_samples <= 0:
                with open(output_path, "wb") as out_file:
                    fp_stream_copy_file_range(file_handle, out_file, 0, len(data))
                print("No encrypted fragmented samples found")
                return

            patches = []
            if drop_text:
                patches.extend(fp_collect_text_track_patches(data))
            patches.extend(fp_collect_decrypted_mp4_metadata_patches(data, tracks))
            decrypt_events = fp_prepare_decrypt_events(fragments, tracks, fast_keys)
            growth_patches, repaired_sei_count = fp_collect_growth_patches(data, decrypt_events, fix_sei=fix_sei)
            patches.extend(growth_patches)
            patch_events = fp_prepare_patch_events(patches)
            fp_stream_decrypt_to_output(data, output_path, patch_events, decrypt_events, fix_sei=fix_sei)
            fp_flatten_fragmented_mp4_in_place(output_path)
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
    parser.add_argument(
        "-S", "--fix-sei", action="store_true", help="Fix missing rbsp_stop_one_bit in HEVC SEI when present"
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
        args.i,
        args.o,
        keys_by_track,
        keys_by_kid,
        show_tracks=args.show_tracks,
        drop_text=not args.keep_text,
        fix_sei=args.fix_sei,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
