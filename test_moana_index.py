#
# partcad-openusd-disney-moana, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Tests of the Moana archive's seek index: reading members through it, and building it.

The archives are gzipped tars made here. The reader is tested with access
points made in pure Python - after a sync flush, which ends a block on a byte -
so it runs everywhere; the builder needs the system's libz, through ctypes, and
is skipped where there is none to load. Nothing here touches the network.
"""

import gzip
import hashlib
import importlib.util
import io
import os
import random
import sys
import tarfile
import zlib

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mi = _load(os.path.join(_HERE, "moana_index.py"), "moana_index_under_test")


def _words(seed, size):
    """Text that compresses as a USD layer does: words, repeated, not a pattern zlib makes one block of."""
    rng = random.Random(seed)
    vocabulary = ["point3f", "normal3f", "faceVertexIndices", "def", "Xform", "Mesh", "over", "(", ")", "[", "]"]
    vocabulary += ["%d.%03d" % (rng.randrange(1000), rng.randrange(1000)) for _ in range(400)]
    out = []
    length = 0
    while length < size:
        word = rng.choice(vocabulary)
        out.append(word)
        length += len(word) + 1
    return (" ".join(out)).encode()[:size]


def _tar(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        offsets = {m.name: (m.offset, m.offset_data, m.size) for m in archive.getmembers()}
    return raw, offsets


def indexed_archive(files, aligned=(), archive_info=None):
    """(gzipped tar of 'files', its Index): an access point before each member 'aligned' names.

    Made with a sync flush, which ends a deflate block on a byte - the only
    place the reader begins (see moana_index.py).
    """
    raw, offsets = _tar(files)
    cuts = sorted(offsets[n][0] for n in aligned)
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    out, done, flushed = bytearray(), 0, []
    for at in cuts:
        out += compressor.compress(raw[done:at])
        done = at
        out += compressor.flush(zlib.Z_SYNC_FLUSH)
        flushed.append((len(out), at))
    out += compressor.compress(raw[done:]) + compressor.flush()
    data = bytes(out)
    points = [(length, 0, at, raw[max(0, at - 32768) : at]) for length, at in flushed]
    members = {}
    for name, (_, start, size) in offsets.items():
        if name not in files:
            continue
        end = next((c for c, _, u, _ in points if u >= start + size), len(data))
        members[name] = mi.Member(start, size, hashlib.sha256(files[name]).hexdigest(), end)
    archive = archive_info or {"url": "https://example.com/island.tgz", "size": len(data), "etag": '"e"'}
    return data, mi.Index(_header(archive, points, members), b"".join(p[3] for p in points))


def _header(archive, points, members):
    return {
        "archive": archive,
        "points": [[c, bits, u, len(window)] for c, bits, u, window in points],
        "members": [[name] + list(m) for name, m in members.items()],
    }


class Sink:
    """What a read hands its members to: {name: bytes}, and what was discarded."""

    def __init__(self):
        self.files, self.discarded = {}, []

    def __call__(self, name):
        sink = self

        class Writer:
            def __init__(self):
                self.parts = []

            def write(self, data):
                self.parts.append(bytes(data))

            def close(self):
                sink.files[name] = b"".join(self.parts)

            def discard(self):
                sink.discarded.append(name)

        return Writer()


def _fetcher(data):
    asked = []

    def fetch(start, end):
        asked.append((start, end))
        return data[start:end]

    fetch.asked = asked
    return fetch


FILES = {
    "island/README.pdf": _words(1, 300000),
    "island/usd/island.usda": b"#usda 1.0\n" + _words(2, 2000),
    "island/usd/elements/isKava/element.usda": b"#usda 1.0\n" + _words(3, 5000),
    "island/usd/elements/isKava/model.usd": _words(4, 400000),
    "island/usd/elements/isBeach/element.usda": b"#usda 1.0\n" + _words(5, 3000),
    "island/usd/elements/isBeach/geometry.usd": _words(6, 600000),
    "island/usd/elements/isBeach/empty.usda": b"",
    "island/textures/isBeach/sand.ptx": _words(7, 900000),
}


ALIGNED = [
    "island/usd/island.usda",
    "island/usd/elements/isKava/element.usda",
    "island/usd/elements/isBeach/element.usda",
    "island/textures/isBeach/sand.ptx",
]


def test_a_member_is_read_from_the_access_point_before_it_and_not_the_archive_up_to_it():
    data, index = indexed_archive(FILES, aligned=ALIGNED)
    for folder in ("isKava", "isBeach"):
        names = [n for n in index.members if "/elements/%s/" % folder in n]
        sink, fetch = Sink(), _fetcher(data)
        fetched = mi.read(fetch, index.points, index.members, names, sink)
        assert sink.files == {n: FILES[n] for n in names}
        # One request, from the folder's own access point: not the archive up to it.
        ((start, end),) = fetch.asked
        point = next(p for p in index.points if p[2] == min(index.members[n].offset for n in names) - 512)
        assert start == point[0] and fetched == end - start < len(data) // 2


def test_members_far_apart_are_read_a_request_each_and_near_ones_in_one():
    data, index = indexed_archive(FILES, aligned=ALIGNED)
    names = [
        "island/usd/island.usda",
        "island/usd/elements/isKava/element.usda",
        "island/usd/elements/isBeach/geometry.usd",
    ]
    plan = mi.plan(index.points, index.members, names)
    assert [len(run[3]) for run in plan] == [1, 1, 1]
    sink = Sink()
    mi.read(_fetcher(data), index.points, index.members, names, sink)
    assert sink.files == {n: FILES[n] for n in names}
    # Two members after one point, with no point between them, are one read.
    both = ["island/usd/elements/isKava/element.usda", "island/usd/elements/isKava/model.usd"]
    assert [run[3] for run in mi.plan(index.points, index.members, both)] == [both]


def test_a_member_deflate_kept_as_it_is_is_read_too():
    """Bytes that do not compress - a crate file's LZ4 - are written as stored blocks, which begin on a byte."""
    stored = dict(FILES)
    stored["island/usd/elements/isKava/model.usd"] = random.Random(11).randbytes(300000)
    data, index = indexed_archive(stored, aligned=ALIGNED)
    names = [n for n in index.members if "/isKava/" in n]
    sink = Sink()
    mi.read(_fetcher(data), index.points, index.members, names, sink)
    assert sink.files == {n: stored[n] for n in names}


def test_an_empty_member_is_made_without_a_read():
    data, index = indexed_archive(FILES, aligned=ALIGNED)
    sink, fetch = Sink(), _fetcher(data)
    assert mi.read(fetch, index.points, index.members, ["island/usd/elements/isBeach/empty.usda"], sink) == 0
    assert sink.files == {"island/usd/elements/isBeach/empty.usda": b""} and fetch.asked == []


def test_a_member_that_is_not_what_the_index_says_is_discarded_and_said_to_be():
    data, index = indexed_archive(FILES, aligned=["island/usd/elements/isKava/element.usda"])
    name = "island/usd/elements/isKava/model.usd"
    index.members[name] = index.members[name]._replace(sha256="0" * 64)
    sink = Sink()
    with pytest.raises(mi.Mismatch, match="not what the index says"):
        mi.read(_fetcher(data), index.points, index.members, [name], sink)
    assert sink.discarded == [name] and name not in sink.files


def test_an_access_point_that_is_not_one_is_said_to_be_a_mismatch():
    data, index = indexed_archive(FILES, aligned=["island/usd/elements/isKava/element.usda"])
    c, bits, u, window = index.points[0]
    # Inside a byte: the reader does not begin there (see moana_index.py).
    index.points[0] = (c, 3, u, window)
    with pytest.raises(mi.Mismatch, match="not on a byte boundary"):
        mi.read(_fetcher(data), index.points, index.members, ["island/usd/elements/isKava/model.usd"], Sink())
    # Not where a block begins at all.
    index.points[0] = (c + 7, 0, u, window)
    with pytest.raises(mi.Mismatch):
        mi.read(_fetcher(data), index.points, index.members, ["island/usd/elements/isKava/model.usd"], Sink())
    # And no point before a member.
    index.points[0] = (c, 0, index.members["island/usd/elements/isKava/model.usd"].offset + 1, window)
    with pytest.raises(mi.Mismatch, match="no access point before"):
        mi.plan(index.points[:1], index.members, ["island/usd/elements/isKava/model.usd"])


def test_an_index_file_reads_back_as_it_was_written(tmp_path):
    data, index = indexed_archive(FILES, aligned=ALIGNED)
    path = tmp_path / "index.xz"
    path.write_bytes(mi.dump(index.archive, index.points, index.members, compressed=len(data)))
    again = mi.load(str(path))
    assert again.archive == index.archive and again.points == index.points and again.members == index.members
    assert mi.load(str(tmp_path / "none.xz")) is None
    (tmp_path / "bad.xz").write_bytes(b"not an index")
    assert mi.load(str(tmp_path / "bad.xz")) is None
    import lzma

    (tmp_path / "other.xz").write_bytes(lzma.compress(b"something else"))
    assert mi.load(str(tmp_path / "other.xz")) is None


# --- the builder --------------------------------------------------------------------


def _builder():
    module = _load(os.path.join(_HERE, "tools", "build_moana_index.py"), "build_moana_index_under_test")
    try:
        module.load_zlib()
    except OSError:
        pytest.skip("no zlib library to load through ctypes here")
    return module


def test_the_builder_indexes_every_folder_and_the_reader_reads_every_member_through_it(tmp_path):
    builder = _builder()
    raw, offsets = _tar(FILES)
    data = gzip.compress(raw, compresslevel=6)
    work = str(tmp_path / "work")
    stream = io.BytesIO(data)
    total_in, total_out, done = builder.scan(stream.read, work, scan_span=64 << 10, log=None)
    # It stops at the first member past the USD layers: the textures are not read.
    assert done and total_out < offsets["island/textures/isBeach/sand.ptx"][1] + 2 * (1 << 18)
    assert total_in < len(data)
    found, windows = builder._load_work(work)
    archive = {"url": "https://example.com/island.tgz", "size": len(data), "etag": '"e"', "complete": True}
    index = mi.loads(builder.pack(found, windows, archive, 128 << 10))
    kept = {n for n in FILES if n.startswith("island/usd/")}
    assert set(index.members) == kept
    # Every point is on a byte: the reader begins nowhere else.
    assert index.points and not any(bits for _, bits, _, _ in index.points)
    # A point at or before each folder's first member's header: the last block that ends on a byte
    # before it, which in an archive this small may be well before it.
    for folder in ("isKava", "isBeach"):
        first = min(offsets[n][0] for n in kept if "/elements/%s/" % folder in n)
        assert any(u <= first for _, _, u, _ in index.points)
    sink = Sink()
    mi.read(_fetcher(data), index.points, index.members, sorted(kept), sink)
    assert sink.files == {n: FILES[n] for n in kept}
    # And one element alone is read without the archive up to it.
    beach = [n for n in kept if "/isBeach/" in n]
    fetch = _fetcher(data)
    mi.read(fetch, index.points, index.members, beach, Sink())
    assert fetch.asked[0][0] > len(data) // 4


def test_a_scan_that_loses_its_connection_starts_again_and_one_that_cannot_says_so(tmp_path, monkeypatch, capsys):
    builder = _builder()
    raw, _ = _tar(FILES)
    data = gzip.compress(raw, compresslevel=6)
    attempts = []

    class Answer(io.BytesIO):
        headers = {"Content-Length": str(len(data)), "ETag": '"e"', "Last-Modified": "then"}

        def __init__(self, cut):
            super().__init__(data[:cut])

    def urlopen(request, timeout=None):
        attempts.append(request.full_url)
        return Answer(len(data) // 3 if len(attempts) == 1 else len(data))

    monkeypatch.setattr(builder.urllib.request, "urlopen", urlopen)
    work = str(tmp_path / "work")
    assert builder.main(["scan", work, "--url", "https://example.com/island.tgz"]) == 0
    assert len(attempts) == 2 and "starting the pass again" in capsys.readouterr().err
    with open(os.path.join(work, "archive.json")) as f:
        assert '"complete": true' in f.read()
    attempts.clear()
    monkeypatch.setattr(builder.urllib.request, "urlopen", lambda request, timeout=None: Answer(len(data) // 3))
    assert builder.main(["scan", work, "--url", "https://example.com/island.tgz", "--attempts", "2"]) == 1
    assert "could not be read through" in capsys.readouterr().err


def test_the_builder_says_what_each_spacing_of_points_costs(tmp_path, capsys):
    builder = _builder()
    raw, _ = _tar(FILES)
    data = gzip.compress(raw, compresslevel=6)
    work = str(tmp_path / "work")
    builder.scan(io.BytesIO(data).read, work, scan_span=64 << 10, log=None)
    with open(os.path.join(work, "archive.json"), "w") as f:
        f.write('{"url": "u", "size": %d, "etag": "e", "complete": false}' % len(data))
    assert builder.main(["pack", work, str(tmp_path / "partial.xz")]) == 1
    with open(os.path.join(work, "archive.json"), "w") as f:
        f.write('{"url": "u", "size": %d, "etag": "e", "complete": true}' % len(data))
    assert builder.main(["measure", work]) == 0
    assert "index" in capsys.readouterr().out
    out = str(tmp_path / "index.xz")
    assert builder.main(["pack", work, out, "--span", "0.1"]) == 0
    assert mi.load(out) is not None


@pytest.mark.skipif(sys.platform == "win32", reason="the shipped index is checked where the builder runs")
def test_the_shipped_index_is_of_the_archive_the_plugin_reads():
    index = mi.load(os.path.join(_HERE, "island-usd-v2.1.index.xz"))
    assert index is not None
    assert index.archive["url"] == "https://datasets.disneyanimation.com/moanaislandscene/island-usd-v2.1.tgz"
    assert index.archive["size"] == 15159731761 and index.archive["etag"]
    names = set(index.members)
    for element in ("isBeach", "isLavaRocks", "osOcean", "isBayCedarA1"):
        assert "island/usd/elements/%s/element.usda" % element in names
    assert "island/usd/island.usda" in names
    assert not any(n.rsplit("/", 1)[-1].startswith("._") for n in names)
    # Every member has an access point before it, and a bound past it.
    first = min(m.offset for m in index.members.values())
    assert index.points[0][2] <= first
    assert all(m.end > index.points[0][0] for m in index.members.values())
    with open(os.path.join(_HERE, "island-usd-v2.1.index.LICENSE.txt")) as f:
        licence = f.read()
    assert "License for Moana Island Scene" in licence and "Disclaimer" in licence
