#
# partcad-openusd-disney-moana, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Members of the Moana Island Scene's archive, read through a seek index rather than the whole archive.

Disney publishes the scene's USD as one gzipped tar archive of 15 GB, and a
gzip stream cannot be read from the middle as it is: a deflate block can be
decoded only from where it begins, and only given the 32 KB of uncompressed
data before it, which it may refer back to. The index this plugin ships
(``island-usd-v2.1.index.xz``, built once by ``tools/build_moana_index.py``)
records such places - access points, as zlib's ``zran.c`` calls them - just
before every folder of USD layers the archive keeps together, and every few MB
between them: each one's compressed byte offset, its uncompressed offset, and
that window. And the members of the archive under ``island/usd/``: where each
one's bytes begin in the uncompressed stream, its size, its SHA-256, and the
first compressed byte past the block it ends in.

So a member is read by asking the server for the compressed bytes from the
access point before it to the end of the block it ends in, with an HTTP range
request, and inflating them as a raw deflate stream primed with the window:
that element's own bytes, not the archive up to it.

Every access point is where a block ends on a byte boundary. A block can end
anywhere in a byte, and zran.c resumes from such a place with
``inflatePrime``, which Python's zlib does not have. Shifting the bytes onto a
byte boundary instead is not the same: a stored block - which deflate writes
where data does not compress, as the LZ4-packed parts of a USD crate file do
not - begins on a byte boundary of the stream as it was written, and a shifted
stream puts that boundary somewhere else. So only the block boundaries that
fall on a byte are kept, about one in eight, which costs a read a few blocks
before the place it wants. ``bits`` is kept in the format, as zran.c's, and
is always 0.

Every member read is checked against its SHA-256. Anything that does not
match - the archive's size or ETag, a digest - raises ``Mismatch``, and the
plugin reads the whole archive instead, as it did before there was an index.

Pure Python: the index is read with ``lzma``, the archive with ``zlib``.
"""

import bisect
import collections
import hashlib
import json
import lzma
import struct
import zlib

MAGIC = b"PCMOANA1"
FORMAT = 1

# How much of the archive is asked for in one range request, at most.
CHUNK = 16 << 20

# One member of the archive: where its bytes begin in the uncompressed stream,
# how many there are, their SHA-256, and the first compressed byte past the
# deflate block the last of them is in.
Member = collections.namedtuple("Member", "offset size sha256 end")


class Mismatch(Exception):
    """The index does not describe the archive the server has, or a member read is not what it says."""


class Index:
    """A seek index: the archive it is of, its access points, and its members."""

    def __init__(self, header, windows):
        self.archive = header["archive"]
        self.points = []  # (compressed offset, bits, uncompressed offset, window), in order
        start = 0
        for compressed, bits, uncompressed, length in header["points"]:
            self.points.append((compressed, bits, uncompressed, windows[start : start + length]))
            start += length
        self.members = {name: Member(*fields) for name, *fields in header["members"]}


def dump(archive, points, members, **extra):
    """An index file's bytes: an xz stream of MAGIC, a JSON header and the points' windows, one after another.

    'points' is [(compressed, bits, uncompressed, window)], 'members' is
    {name: Member}; 'archive' says what it is of - its 'url', 'size', 'etag'
    and 'last_modified'.
    """
    header = dict(extra)
    header.update(
        {
            "format": FORMAT,
            "archive": archive,
            "points": [[c, bits, u, len(window)] for c, bits, u, window in points],
            "members": [[name] + list(member) for name, member in sorted(members.items(), key=lambda m: m[1].offset)],
        }
    )
    text = json.dumps(header, separators=(",", ":")).encode("utf-8")
    raw = MAGIC + struct.pack("<I", len(text)) + text + b"".join(window for _, _, _, window in points)
    return lzma.compress(raw, preset=9 | lzma.PRESET_EXTREME)


def loads(data):
    """The Index in the bytes of an index file; None where they are not one this reads."""
    try:
        raw = lzma.decompress(data)
    except lzma.LZMAError:
        return None
    if not raw.startswith(MAGIC):
        return None
    (length,) = struct.unpack_from("<I", raw, len(MAGIC))
    start = len(MAGIC) + 4
    header = json.loads(raw[start : start + length].decode("utf-8"))
    if header.get("format") != FORMAT:
        return None
    return Index(header, raw[start + length :])


def load(path):
    """The Index at 'path'; None where there is none, or none this reads."""
    try:
        with open(path, "rb") as f:
            return loads(f.read())
    except OSError:
        return None


def plan(points, members, names):
    """[(start, end, point, names)]: the ranges of the compressed archive to read for 'names', a request each.

    Each member is read from the last access point at or before it, and the
    members after it from the same point for as long as no other point lies
    between the end of one and the start of the next: a read goes on past a
    gap no point could have skipped, and starts again at a point past one.
    """
    starts = [point[2] for point in points]
    runs = []
    for name in sorted(names, key=lambda n: (members[n].offset, n)):
        member = members[name]
        at = bisect.bisect_right(starts, member.offset) - 1
        if at < 0:
            raise Mismatch("the index has no access point before %s" % name)
        point = points[at]
        if point[1]:
            raise Mismatch("the index's access point at byte %d is not on a byte boundary" % point[0])
        if runs and point[2] <= runs[-1][4]:
            run = runs[-1]
        else:
            run = [point, [], 0, 0, 0]  # point, names, end, (unused), uncompressed end
            runs.append(run)
        run[1].append(name)
        run[2] = max(run[2], member.end)
        run[4] = max(run[4], member.offset + member.size)
    return [(point[0], end, point, chosen) for point, chosen, end, _, _ in runs]


def read(fetch, points, members, names, sink):
    """Read the members 'names' of the archive, handing each one's bytes to what 'sink(name)' answers.

    'fetch(start, end)' is the compressed bytes from 'start' to 'end'
    (exclusive) - a range request. What 'sink(name)' answers has 'write(data)',
    'close()' and 'discard()': a member is closed once all of it has been
    written and found to be what the index says, and discarded otherwise,
    before Mismatch is raised. Answers how many compressed bytes were fetched.
    """
    fetched = 0
    sized = [n for n in names if members[n].size]
    for name in names:
        if not members[name].size:
            if members[name].sha256 != hashlib.sha256().hexdigest():
                raise Mismatch("%s is empty, but its digest says otherwise" % name)
            sink(name).close()
    for start, end, point, run in plan(points, members, sized):
        _, _, position, window = point
        inflater = zlib.decompressobj(-15, zdict=window) if window else zlib.decompressobj(-15)
        reader = _Members(members, run, sink, position)
        try:
            done = False
            for at in range(start, end, CHUNK):
                data = fetch(at, min(at + CHUNK, end))
                fetched += len(data)
                if reader.take(inflater.decompress(data)):
                    done = True
                    break
            if not done and not reader.take(inflater.flush()):
                raise Mismatch("the archive ended before %s did" % reader.waiting())
        except zlib.error as e:
            reader.discard()
            raise Mismatch("the archive could not be inflated from the index's access point: %s" % e)
        except Exception:
            reader.discard()
            raise
    return fetched


class _Members:
    """The members of one run, taken out of the uncompressed bytes as they come."""

    def __init__(self, members, names, sink, position):
        self.members, self.queue, self.sink, self.position = members, list(names), sink, position
        self.current = None  # [name, member, writer, hasher, remaining]

    def waiting(self):
        return self.current[0] if self.current else self.queue[0]

    def take(self, data):
        """Hand on what of 'data' belongs to a member; True once every member is whole."""
        data = memoryview(data)
        while data and (self.current is not None or self.queue):
            if self.current is None:
                name = self.queue[0]
                member = self.members[name]
                if member.offset < self.position:
                    raise Mismatch("%s begins before the stream read to reach it" % name)
                if self.position + len(data) <= member.offset:
                    self.position += len(data)
                    return False
                data = data[member.offset - self.position :]
                self.position = member.offset
                self.queue.pop(0)
                self.current = [name, member, self.sink(name), hashlib.sha256(), member.size]
            name, member, writer, hasher, remaining = self.current
            piece = data[:remaining]
            hasher.update(piece)
            writer.write(piece)
            data = data[len(piece) :]
            self.position += len(piece)
            self.current[4] = remaining = remaining - len(piece)
            if remaining == 0:
                self.current = None
                if hasher.hexdigest() != member.sha256:
                    writer.discard()
                    raise Mismatch("%s is not what the index says it is" % name)
                writer.close()
        return self.current is None and not self.queue

    def discard(self):
        if self.current is not None:
            self.current[2].discard()
            self.current = None
