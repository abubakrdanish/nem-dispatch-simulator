"""Read named members out of a large remote zip with HTTP range requests, without downloading it.

Up to 2014 AEMO's archive holds each month only as one zip of about 1.9 GB. The constraint definition
tables inside it are under 1 MB. This reads the end-of-archive record and the central directory from
the end of the file, then fetches just the bytes of each wanted member: a handful of small requests
instead of 1.9 GB.
"""
from __future__ import annotations

import re
import struct
import zlib

from nemsim.http_client import PoliteClient

EOCD_SIG = b"PK\x05\x06"
TAIL = 65_557           # the end-of-central-directory record lies within the last 64 KB + 22 bytes


def central_directory(client: PoliteClient, url: str) -> tuple[int, list[dict]]:
    size = client.size(url)
    tail = client.get_range(url, max(0, size - TAIL), size - 1)
    at = tail.rfind(EOCD_SIG)
    if at < 0:
        raise ValueError(f"{url}: no end-of-central-directory record in the last {TAIL} bytes")
    cd_size, cd_offset = struct.unpack("<II", tail[at + 12:at + 20])
    if cd_offset == 0xFFFFFFFF:
        raise ValueError(f"{url}: zip64 archives are not handled")
    cd = client.get_range(url, cd_offset, cd_offset + cd_size - 1)
    entries, i = [], 0
    while i + 46 <= len(cd) and cd[i:i + 4] == b"PK\x01\x02":
        method, = struct.unpack("<H", cd[i + 10:i + 12])
        csize, usize = struct.unpack("<II", cd[i + 20:i + 28])
        nlen, elen, clen = struct.unpack("<HHH", cd[i + 28:i + 34])
        offset, = struct.unpack("<I", cd[i + 42:i + 46])
        name = cd[i + 46:i + 46 + nlen].decode("utf-8", "replace")
        entries.append({"name": name, "method": method, "csize": csize, "usize": usize, "offset": offset})
        i += 46 + nlen + elen + clen
    return size, entries


def fetch_member(client: PoliteClient, url: str, entry: dict) -> bytes:
    # Local header (30 bytes) + name + extra field, then the data. Fetch a little slack for the
    # header's variable part, then cut the data out.
    raw = client.get_range(url, entry["offset"], entry["offset"] + 30 + 1024 + entry["csize"] - 1)
    if raw[:4] != b"PK\x03\x04":
        raise ValueError(f"{url}: no local header for {entry['name']}")
    nlen, elen = struct.unpack("<HH", raw[26:30])
    data = raw[30 + nlen + elen:30 + nlen + elen + entry["csize"]]
    if entry["method"] == 0:
        return data
    if entry["method"] == 8:
        return zlib.decompressobj(-15).decompress(data)
    raise ValueError(f"{url}: compression method {entry['method']} not handled")


def find(entries: list[dict], table: str) -> list[dict]:
    """Members holding one table: PUBLIC_DVD_<TABLE>_<YYYYMM>010000.zip, in any folder."""
    pat = re.compile(rf"(^|/)PUBLIC_DVD_{table}_\d{{12}}\.(zip|CSV|csv)$")
    return [e for e in entries if pat.search(e["name"])]
