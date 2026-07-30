# Clone from https://github.com/Hugoved/pywebmdump

import os
from typing import List, Optional, Tuple

DEFAULT_METADATA_SCAN_BYTES = 8 * 1024 * 1024
MASTER_IDS = {
    0x1A45DFA3,  # EBML
    0x18538067,  # Segment
    0x1654AE6B,  # Tracks
    0xAE,  # TrackEntry
    0x6D80,  # ContentEncodings
    0x6240,  # ContentEncoding
    0x5035,  # ContentEncryption
}
CONTENT_ENC_KEY_ID = 0x47E2


class WebMExtractorError(Exception):
    pass


def read_metadata_prefix(path: str, limit: int = DEFAULT_METADATA_SCAN_BYTES) -> bytes:
    # Function to minimize memory usage by reading only the prefix (metadata) of the file
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        return f.read(min(size, max(4096, limit)))


def read_ebml_id(buf: bytes, off: int) -> Tuple[int, int]:
    # Function to read the EBML ID and its length from the buffer
    if off >= len(buf):
        raise WebMExtractorError("unexpected end while reading EBML ID")
    first = buf[off]
    mask = 0x80
    length = 1
    while length <= 4 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 4 or off + length > len(buf):
        raise WebMExtractorError("invalid EBML ID")
    raw = buf[off : off + length]
    value = 0
    for b in raw:
        value = (value << 8) | b
    return value, length


def read_ebml_size(buf: bytes, off: int) -> Tuple[Optional[int], int]:
    # Function to read the EBML data size and its length from the buffer
    if off >= len(buf):
        raise WebMExtractorError("unexpected end while reading EBML size")
    first = buf[off]
    mask = 0x80
    length = 1
    while length <= 8 and (first & mask) == 0:
        mask >>= 1
        length += 1
    if length > 8 or off + length > len(buf):
        raise WebMExtractorError("invalid EBML size")
    raw = buf[off : off + length]
    data_bits = 8 - length
    value = raw[0] & ((1 << data_bits) - 1)
    unknown = raw[0] == ((1 << data_bits) - 1) and all(b == 0xFF for b in raw[1:])
    for b in raw[1:]:
        value = (value << 8) | b
    return (None if unknown else value), length


def extract_webm_kids(input_file: str) -> List[str]:
    data = read_metadata_prefix(input_file)
    kids = []

    def parse_children(start: int, end: int):
        pos = start
        effective_end = min(end, len(data))
        while pos < effective_end:
            try:
                id_value, id_len = read_ebml_id(data, pos)
                size_value, size_len = read_ebml_size(data, pos + id_len)
            except WebMExtractorError:
                break

            data_start = pos + id_len + size_len
            data_end = effective_end if size_value is None else min(data_start + size_value, effective_end)

            if id_value == CONTENT_ENC_KEY_ID:
                kid_hex = data[data_start:data_end].hex()
                if kid_hex and kid_hex not in kids:
                    kids.append(kid_hex)
            elif id_value in MASTER_IDS:
                parse_children(data_start, data_end)

            pos = data_end

    parse_children(0, len(data))
    return kids
