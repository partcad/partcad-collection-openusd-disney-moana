#!/usr/bin/env python3
#
# partcad-openusd-disney-moana, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Build the seek index of the Moana Island Scene's USD archive (see moana_index.py).

The archive is one gzip stream, and a gzip stream can be read from the middle
only where its deflate blocks begin, given the 32 KB of uncompressed data
before that place - which is what zlib's own 'zran.c' records, and what this
does, in the same way: it inflates the archive once with 'Z_BLOCK', which
returns at the end of every deflate block, and keeps an access point - the
compressed byte offset, the uncompressed offset and the window - where one is
wanted, at the last block boundary before it that falls on a byte (the plugin
cannot resume from one inside a byte; see moana_index.py):

  * just before every run of the USD layers the archive keeps together - the
    island's root layers, and each element's own folder;
  * every '--span' MB of the compressed stream between them, which is what a
    single file is read from.

and the members of the archive under 'island/usd/': each one's name, where its
bytes begin in the uncompressed stream, its size, its SHA-256, and the first
compressed byte past the block its last byte is in. (Not the archive's own
'island/License.txt', which lies beyond them: the licence is shipped beside the
index instead.)

It reads the archive only as far as the USD layers go. The archive keeps all of
'island/usd/' together near its start - the README, then the USD tree, then
'island/ref' and the textures - and the tree ends 1,892,515,840 bytes into the
compressed stream. That is known from one pass over 12.8 GB of the 15.2 that
counted the members under 'island/usd/' as it went: 460 by 1.8 GB, 471 by
2.9 GB, and 471 still at 12.8 GB. So a scan stops at the first member past the
tree, about 1.9 GB in, rather than reading the textures to be sure.

A deflate stream cannot be resumed in the middle, so a connection that drops
starts the pass again from the beginning; after '--attempts' of those, it fails
saying so.

Python's zlib module cannot do this - it has no 'Z_BLOCK', and does not say
where a block ends - so this asks the system's libz with ctypes. Only this tool
does: the plugin reads the index with Python's own zlib.

    python tools/build_moana_index.py scan  work/          # stream the archive's first 1.9 GB once
    python tools/build_moana_index.py measure work/        # what each spacing of points costs
    python tools/build_moana_index.py pack work/ island-usd-v2.1.index.xz --span 16

'scan' keeps a point every 2 MB in 'work/'; 'pack' ships the subset '--span'
asks for. The windows are pieces of the scene, so the index is shipped under
the scene's own licence, beside it (see the README).
"""

import argparse
import collections
import ctypes
import ctypes.util
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import moana_index  # noqa: E402 - the plugin's own reader, beside this folder

ARCHIVE = "https://datasets.disneyanimation.com/moanaislandscene/island-usd-v2.1.tgz"
WINDOW = 32768

Z_OK, Z_STREAM_END, Z_BUF_ERROR, Z_BLOCK = 0, 1, -5, 5

# A point every this many compressed bytes is kept by 'scan'; 'pack' ships a
# subset of them.
SCAN_SPAN = 2 << 20
# A run of kept members begins wherever the next is more than this many
# uncompressed bytes past the end of the one before, or in a folder of its own.
RUN_GAP = 1 << 20
# Past this many uncompressed bytes beyond the last member kept, no more
# periodic points are kept (the archive's textures are not read).
PAST_KEPT = 256 << 20


def wanted(name):
    """What the index describes: the archive's USD layers.

    Not the '._' files macOS's tar put beside every file and folder: they are
    its metadata, which nothing reads.
    """
    if name.rsplit("/", 1)[-1].startswith("._"):
        return False
    return name.startswith("island/usd/")


def past(name):
    """Whether the member 'name', met after the USD layers began, is past them: the scan stops there."""
    return not name.startswith("island/usd/")


def group_of(name):
    """The folder a member is read with: an element's own, or the island's root layers."""
    parts = name.split("/")
    if parts[:3] == ["island", "usd", "elements"] and len(parts) > 4:
        return "element:" + parts[3]
    return "root"


# --- zlib, through ctypes ---------------------------------------------------------


class ZStream(ctypes.Structure):
    _fields_ = [
        ("next_in", ctypes.c_void_p),
        ("avail_in", ctypes.c_uint),
        ("total_in", ctypes.c_ulong),
        ("next_out", ctypes.c_void_p),
        ("avail_out", ctypes.c_uint),
        ("total_out", ctypes.c_ulong),
        ("msg", ctypes.c_char_p),
        ("state", ctypes.c_void_p),
        ("zalloc", ctypes.c_void_p),
        ("zfree", ctypes.c_void_p),
        ("opaque", ctypes.c_void_p),
        ("data_type", ctypes.c_int),
        ("adler", ctypes.c_ulong),
        ("reserved", ctypes.c_ulong),
    ]


def load_zlib():
    """The system's libz, with what this calls declared; OSError where there is none to load."""
    name = ctypes.util.find_library("z") or ctypes.util.find_library("zlib")
    if not name:
        raise OSError("no zlib library to load")
    lib = ctypes.CDLL(name)
    lib.zlibVersion.restype = ctypes.c_char_p
    lib.inflateInit2_.argtypes = [ctypes.POINTER(ZStream), ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    lib.inflate.argtypes = [ctypes.POINTER(ZStream), ctypes.c_int]
    lib.inflateEnd.argtypes = [ctypes.POINTER(ZStream)]
    lib.inflateGetDictionary.argtypes = [ctypes.POINTER(ZStream), ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    return lib


def inflate_blocks(read, on_output, on_boundary, chunk_in=1 << 20, chunk_out=1 << 18):
    """Inflate the gzip stream 'read(n)' gives, block by block, as zran.c does.

    'on_output(data)' is handed the uncompressed bytes in order, and returns
    True to stop; 'on_boundary(compressed, bits, uncompressed, window)' is
    called at the end of every deflate block but the last. Answers (compressed
    bytes read, uncompressed bytes made).
    """
    lib = load_zlib()
    stream = ZStream()
    ret = lib.inflateInit2_(ctypes.byref(stream), 15 + 32, lib.zlibVersion(), ctypes.sizeof(ZStream))
    if ret != Z_OK:
        raise RuntimeError("inflateInit2 failed: %d" % ret)
    inbuf = ctypes.create_string_buffer(chunk_in)
    outbuf = ctypes.create_string_buffer(chunk_out)
    window = ctypes.create_string_buffer(WINDOW)
    total_in = total_out = 0
    try:
        while True:
            if stream.avail_in == 0:
                data = read(chunk_in)
                if not data:
                    raise RuntimeError("the archive ended before its gzip stream did")
                ctypes.memmove(inbuf, data, len(data))
                stream.next_in = ctypes.addressof(inbuf)
                stream.avail_in = len(data)
            given = stream.avail_in
            stream.next_out = ctypes.addressof(outbuf)
            stream.avail_out = chunk_out
            ret = lib.inflate(ctypes.byref(stream), Z_BLOCK)
            if ret not in (Z_OK, Z_STREAM_END, Z_BUF_ERROR):
                raise RuntimeError("inflate failed: %d %s" % (ret, stream.msg))
            total_in += given - stream.avail_in
            produced = chunk_out - stream.avail_out
            total_out += produced
            if produced and on_output(ctypes.string_at(ctypes.addressof(outbuf), produced)):
                break
            if ret == Z_STREAM_END:
                break
            if stream.data_type & 128 and not stream.data_type & 64:
                length = ctypes.c_uint(WINDOW)
                lib.inflateGetDictionary(ctypes.byref(stream), window, ctypes.byref(length))
                on_boundary(total_in, stream.data_type & 7, total_out, ctypes.string_at(window, length.value))
    finally:
        lib.inflateEnd(ctypes.byref(stream))
    return total_in, total_out


# --- the tar stream, pushed a piece at a time --------------------------------------


def _octal(field):
    if field and field[0] & 0x80:  # base-256, for a size too large for octal
        return int.from_bytes(field[1:], "big")
    text = field.split(b"\0", 1)[0].strip()
    return int(text, 8) if text else 0


def _pax(data):
    found = {}
    while data:
        length = int(data.split(b" ", 1)[0])
        record, data = data[:length], data[length:]
        key, _, value = record.split(b" ", 1)[1].rstrip(b"\n").partition(b"=")
        found[key.decode("utf-8")] = value.decode("utf-8")
    return found


class TarScanner:
    """A tar stream fed in pieces: each member's header offset, name, data offset and size, as it is met.

    'on_member(header, name, data, size, kind)' is called at each member; the
    bytes of the ones 'keep(name)' says are hashed, and 'on_end(name, sha256,
    end)' is called once each has been read.
    """

    def __init__(self, on_member, keep, on_end):
        self.on_member, self.keep, self.on_end = on_member, keep, on_end
        self.position = 0  # uncompressed offset of the start of 'pending'
        self.pending = b""
        self.state = "header"
        self.need = 512
        self.header_at = 0
        self.long_name = None
        self.pax = {}
        self.current = None  # (name, hasher, remaining) of a member being hashed
        self.ended = False

    def feed(self, data):
        self.pending += data
        while not self.ended:
            if self.state == "skip":
                take = min(self.need, len(self.pending))
                if self.current is not None:
                    name, hasher, remaining, end = self.current
                    used = min(take, remaining)
                    hasher.update(self.pending[:used])
                    remaining -= used
                    self.current = (name, hasher, remaining, end)
                    if remaining == 0:
                        self.on_end(name, hasher.hexdigest(), end)
                        self.current = None
                self.pending = self.pending[take:]
                self.position += take
                self.need -= take
                if self.need:
                    return
                self.state, self.need = "header", 512
                continue
            if len(self.pending) < self.need:
                return
            block, self.pending = self.pending[: self.need], self.pending[self.need :]
            start = self.position
            self.position += self.need
            if self.state == "header":
                self._header(block, start)
            elif self.state == "longname":
                self.long_name = block[: self.long_size].split(b"\0", 1)[0].decode("utf-8")
                self.state, self.need = "header", 512
            elif self.state == "pax":
                self.pax.update(_pax(block[: self.long_size]))
                self.state, self.need = "header", 512
            elif self.state == "gpax":
                self.state, self.need = "header", 512

    def _header(self, block, start):
        if block == b"\0" * 512:
            self.state, self.need = "header", 512  # the end of the archive is two of these
            return
        name = block[0:100].split(b"\0", 1)[0].decode("utf-8")
        if block[257:262] == b"ustar":
            prefix = block[345:500].split(b"\0", 1)[0].decode("utf-8")
            if prefix:
                name = prefix + "/" + name
        size = _octal(block[124:136])
        kind = chr(block[156]) if block[156] else "0"
        padded = (size + 511) // 512 * 512
        if kind in "LxXg":
            self.long_size = size
            self.state = {"L": "longname", "x": "pax", "X": "pax", "g": "gpax"}[kind]
            self.need = padded
            if not padded:
                self.state, self.need = "header", 512
            return
        if self.long_name is not None:
            name, self.long_name = self.long_name, None
        if self.pax:
            name = self.pax.get("path", name)
            size = int(self.pax.get("size", size))
            padded = (size + 511) // 512 * 512
            self.pax = {}
        name = name.lstrip("./") if name.startswith("./") else name
        data = start + 512
        self.on_member(start, name, data, size, kind)
        if kind in "07" and self.keep(name):
            self.current = (name, hashlib.sha256(), size, data + size) if size else None
            if not size:
                self.on_end(name, hashlib.sha256().hexdigest(), data)
        self.state, self.need = "skip", padded
        if not padded:
            self.state, self.need = "header", 512


# --- scan ---------------------------------------------------------------------------


def scan(read, work, keep=wanted, scan_span=SCAN_SPAN, log=print, until=None, stop=past):
    """Read the archive 'read(n)' gives as far as its USD layers go, and write the points and the members into 'work'.

    It stops at the first member 'stop(name)' says is past the layers, once
    they have begun (see the module's docstring); 'until' stops after that
    many uncompressed bytes, for a quick look. Answers (compressed bytes read,
    uncompressed bytes made, whether it got past the layers).
    """
    os.makedirs(work, exist_ok=True)
    history = collections.deque(maxlen=512)  # recent boundaries on a byte, (compressed, 0, uncompressed, window)
    points = {}  # compressed offset -> (compressed, bits, uncompressed, window, why)
    members = []  # [name, data offset, size, sha256, end bound]
    pending = []  # indices of members whose end bound is not known yet
    by_name = {}
    state = {"last_end": None, "last_group": None, "last_periodic": None, "out": 0, "first_kept": None}
    state["past"] = False
    started = time.time()

    def point_before(offset, why):
        for boundary in reversed(history):
            if boundary[2] <= offset:
                points.setdefault(boundary[0], boundary + (why,))
                return
        raise RuntimeError("no block boundary is remembered before offset %d (%s)" % (offset, why))

    def on_member(header, name, data, size, kind):
        if state["first_kept"] is not None and stop is not None and stop(name):
            state["past"] = True
            return
        if kind not in "07" or not keep(name):
            return
        group = group_of(name)
        if state["last_end"] is None or header > state["last_end"] + RUN_GAP or group != state["last_group"]:
            point_before(header, group)
        if state["first_kept"] is None:
            state["first_kept"] = header
        state["last_end"] = max(state["last_end"] or 0, data + size)
        state["last_group"] = group
        by_name[name] = len(members)
        members.append([name, data, size, None, None])
        pending.append(len(members) - 1)

    def on_end(name, digest, end):
        members[by_name[name]][3] = digest

    tar = TarScanner(on_member, keep, on_end)

    def on_output(data):
        tar.feed(data)
        state["out"] += len(data)
        return state["past"] or (until is not None and state["out"] >= until)

    def on_boundary(compressed, bits, uncompressed, window):
        # Any boundary bounds the read of what ends before it; only one on a
        # byte is a place to begin one.
        while pending and members[pending[0]][1] + members[pending[0]][2] <= uncompressed:
            members[pending.pop(0)][4] = compressed
        if bits:
            return
        boundary = (compressed, bits, uncompressed, window)
        history.append(boundary)
        if state["first_kept"] is not None and uncompressed - (state["last_end"] or 0) <= PAST_KEPT:
            if state["last_periodic"] is None or compressed - state["last_periodic"] >= scan_span:
                points.setdefault(compressed, boundary + ("periodic",))
                state["last_periodic"] = compressed
        if log and uncompressed >= state.setdefault("next_log", 1 << 30):
            state["next_log"] += 1 << 30
            log(
                "%.1f GB inflated, %.1f GB read, %d members kept, %d points, %.0f s"
                % (uncompressed / 1e9, compressed / 1e9, len(members), len(points), time.time() - started),
                flush=True,
            )

    total_in, total_out = inflate_blocks(read, on_output, on_boundary)
    if pending:
        # Their last bytes were made from no more than what had been read when
        # the scan stopped, or the stream ended: that is a bound past them.
        for index in pending:
            members[index][4] = total_in
    last_end = state["last_end"] or 0
    kept = sorted(p for p in points.values() if p[2] <= last_end)
    with open(os.path.join(work, "windows.bin"), "wb") as f:
        listed = []
        for compressed, bits, uncompressed, window, why in kept:
            listed.append([compressed, bits, uncompressed, f.tell(), len(window), why])
            f.write(window)
    with open(os.path.join(work, "scan.json"), "w") as f:
        json.dump({"compressed": total_in, "uncompressed": total_out, "points": listed, "members": members}, f)
    return total_in, total_out, state["past"]


# --- pack ---------------------------------------------------------------------------


def _load_work(work):
    with open(os.path.join(work, "scan.json")) as f:
        found = json.load(f)
    with open(os.path.join(work, "windows.bin"), "rb") as f:
        windows = f.read()
    return found, windows


def choose(points, span):
    """The points shipped: every run's own, and periodic ones at least 'span' compressed bytes apart."""
    chosen, last = [], None
    for point in points:
        if point[5] != "periodic":
            chosen.append(point)
            last = point[0]
        elif span and (last is None or point[0] - last >= span):
            chosen.append(point)
            last = point[0]
    return chosen


def pack(found, windows, archive, span):
    """The index file's bytes (see moana_index.dump), with the points 'span' asks for."""
    points = [
        (compressed, bits, uncompressed, windows[offset : offset + length])
        for compressed, bits, uncompressed, offset, length, _ in choose(found["points"], span)
    ]
    members = {m[0]: moana_index.Member(*m[1:]) for m in found["members"]}
    shipped = {k: v for k, v in archive.items() if k in ("url", "size", "etag", "last_modified")}
    return moana_index.dump(
        shipped, points, members, compressed=found["compressed"], uncompressed=found["uncompressed"]
    )


def costs(found, span):
    """{group: compressed bytes read to build it}, by the plugin's own plan (see moana_index.plan)."""
    points = [(p[0], p[1], p[2], b"") for p in choose(found["points"], span)]
    members = {m[0]: moana_index.Member(*m[1:]) for m in found["members"]}
    groups = collections.defaultdict(list)
    for name in members:
        groups[group_of(name)].append(name)
    return {
        group: sum(end - start for start, end, _, _ in moana_index.plan(points, members, sorted(names)))
        for group, names in groups.items()
    }


def _open(args):
    """(the archive as a stream, what is known of it): the local copy '--file' names, or the URL's answer."""
    if args.file:
        stat = os.stat(args.file)
        return open(args.file, "rb"), {"url": ARCHIVE, "size": stat.st_size, "etag": None, "last_modified": None}
    if urllib.parse.urlsplit(args.url).scheme not in ("http", "https"):
        raise SystemExit("--url must be an http or https URL; a local copy is read with --file")
    request = urllib.request.Request(args.url, headers={"User-Agent": "partcad-moana-index-builder"})
    source = urllib.request.urlopen(request, timeout=600)  # noqa: S310 - the scheme is checked above
    length = source.headers.get("Content-Length")
    if not length or not str(length).isdigit():
        source.close()
        raise RuntimeError("the server did not say how large the archive is")
    archive = {
        "url": args.url,
        "size": int(length),
        "etag": source.headers.get("ETag"),
        "last_modified": source.headers.get("Last-Modified"),
    }
    return source, archive


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("scan", help="stream the archive once into a work directory")
    s.add_argument("work")
    s.add_argument("--url", default=ARCHIVE)
    s.add_argument("--file", help="read a local copy of the archive instead")
    s.add_argument("--until-gb", type=float, help="stop after this much inflated (a quick look; not to ship)")
    s.add_argument("--attempts", type=int, default=3, help="passes to try when the connection drops")
    m = sub.add_parser("measure", help="what each spacing of points costs")
    m.add_argument("work")
    p = sub.add_parser("pack", help="write the index the plugin ships")
    p.add_argument("work")
    p.add_argument("out")
    p.add_argument("--span", type=float, default=16, help="MB of the compressed stream between periodic points")
    args = parser.parse_args(argv)

    if args.command == "scan":
        until = int(args.until_gb * 1e9) if args.until_gb else None
        for attempt in range(1, args.attempts + 1):
            try:
                # Opened inside the attempt: a connection that cannot be made,
                # or an answer that does not say its size, is tried again too.
                source, archive = _open(args)
                with source:
                    total_in, total_out, done = scan(source.read, args.work, until=until)
                break
            except (OSError, http.client.HTTPException, urllib.error.URLError, RuntimeError) as e:
                # A dropped connection ends the stream early; a deflate stream
                # cannot be picked up again in the middle, so the pass starts over.
                if args.file or attempt == args.attempts:
                    print("the archive could not be read through: %s" % e, file=sys.stderr)
                    return 1
                print("attempt %d: %s; starting the pass again" % (attempt, e), file=sys.stderr)
        archive["complete"] = done
        with open(os.path.join(args.work, "archive.json"), "w") as f:
            json.dump(archive, f, indent=1)
        print(
            "read %d compressed bytes into %d, %s; the archive is %d"
            % (total_in, total_out, "past the USD layers" if done else "not past the USD layers", archive["size"])
        )
        return 0

    found, windows = _load_work(args.work)
    with open(os.path.join(args.work, "archive.json")) as f:
        archive = json.load(f)
    if args.command == "measure":
        for span in (4, 8, 16, 32, 64, 0):
            size = len(pack(found, windows, archive, int(span * (1 << 20))))
            each = costs(found, int(span * (1 << 20)))
            elements = {g: c for g, c in each.items() if g.startswith("element:")}
            print(
                "span %-4s MB: %3d points, index %6.2f MB; reading one element costs %.1f MB at most, %.1f MB in all"
                % (span or "-", len(choose(found["points"], int(span * (1 << 20)))), size / 1e6,
                   max(elements.values()) / 1e6, sum(elements.values()) / 1e6)
            )  # fmt: skip
        return 0
    if not archive.get("complete"):
        print("the work directory holds a scan that stopped before the USD layers ended; scan again", file=sys.stderr)
        return 1
    data = pack(found, windows, archive, int(args.span * (1 << 20)))
    with open(args.out, "wb") as f:
        f.write(data)
    print(
        "%s: %d bytes, %d points, %d members"
        % (args.out, len(data), len(choose(found["points"], int(args.span * (1 << 20)))), len(found["members"]))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
