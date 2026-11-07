#
# partcad-openusd-disney-moana, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Tests of the Moana plugin, with Disney's archive answered from a table.

The archive is a gzipped tar laid out as island-usd-v2.1.tgz is, with made-up
layers in it. Nothing here touches the network: the archive is read whole with
no index, or through an index made here of the archive made here, and every
request either way is answered from a table.
"""

import base64
import hashlib
import importlib.util
import io
import os
import re
import runpy
import tarfile
import zipfile
import zlib

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN = os.path.join(_HERE, "moana_repo.py")
_SPEC = importlib.util.spec_from_file_location("moana_repo_under_test", _PLUGIN)
plugin = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(plugin)
ua = plugin.ua


def _usda(text=""):
    return ("#usda 1.0\n" + text).encode()


MEMBERS = {
    "island/usd/island.usda": _usda("( subLayers = [@./elements/isKava/element.usda@] )\n"),
    "island/usd/elements/isKava/element.usda": _usda('def "k" ( references = @./model.usd@ ) {}\n'),
    "island/usd/elements/isKava/model.usd": _usda(),
    "island/usd/elements/isNew/element.usda": _usda(),
    "island/textures/isKava/Color/kava.ptx": b"x" * 5000,
    "island/License.txt": b"licence",
}


def _archive():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, data in MEMBERS.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def moana(tmp_path, monkeypatch):
    """The plugin with no index of the archive: it reads the archive whole, once."""
    monkeypatch.setenv("PARTCAD_OPENUSD_DISNEY_MOANA_CACHE", str(tmp_path))
    monkeypatch.setattr(plugin, "INDEX", str(tmp_path / "no-index.xz"))
    streamed = []

    def urlopen(request, timeout=None):
        streamed.append(request.full_url)
        assert request.full_url == plugin.ARCHIVE
        return _Answer(_archive())

    monkeypatch.setattr(ua.urllib.request, "urlopen", urlopen)

    def make(**parameters):
        made = plugin.Moana({"parameters": parameters})
        made.streamed = streamed
        return made

    return make


def test_the_elements_are_listed_without_reading_the_archive(moana):
    made = moana()
    parts = made.get("objects/part")
    assert list(parts)[0] == "island"
    assert set(plugin.ELEMENTS) <= set(parts)
    assert parts["isKava"]["path"] == "isKava.usdz"
    assert made.streamed == []


def test_nothing_is_fetched_until_the_plugin_may_read_the_archive(moana, capsys):
    made = moana()
    assert ua.serve(made, "files/isKava.usdz") is None
    said = capsys.readouterr().err
    assert "15.2 GB" in said and "download: true" in said and "research and software development" in said
    assert made.streamed == []


def test_the_archive_is_read_once_for_its_usd_and_not_its_textures(moana):
    made = moana(download=True)
    data = base64.b64decode(made.get("files/isKava.usdz"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == ["element.usda", "model.usd"]
    again = moana()
    assert base64.b64decode(again.get("files/isKava.usdz")) == data
    assert made.streamed == [plugin.ARCHIVE]
    kept = again.store().listing()
    assert "usd/elements/isKava/model.usd" in kept and not any(p.endswith(".ptx") for p in kept)


def test_once_read_an_element_the_archive_holds_is_listed_too(moana):
    moana(download=True).get("files/isKava.usdz")
    assert "isNew" in moana().get("objects/part")


def test_the_plugin_is_named_as_the_package_declares_it():
    with open(os.path.join(_HERE, "partcad.yaml")) as f:
        text = f.read()
    name = re.search(r"^name: (\S+)", text, re.M).group(1)
    assert re.search(r"^  moana_repo:\n    type: basic\n    path: moana_repo.py", text, re.M)
    assert plugin.Moana.PLUGIN == name + ":moana_repo"


def test_partcad_runs_it_and_reads_the_answer_from_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTCAD_OPENUSD_DISNEY_MOANA_CACHE", str(tmp_path))
    result = runpy.run_path(_PLUGIN, init_globals={"request": {"key": "meta", "parameters": {}}}, run_name="get")
    assert result["output"]["result"]["manufacturable"] is False


# --- through the index -------------------------------------------------------------


def _indexed_archive():
    """(the gzipped tar of MEMBERS, its index's points and members): a point before the root and each element."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, data in MEMBERS.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    raw = buf.getvalue()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        offsets = {m.name: (m.offset, m.offset_data, m.size) for m in archive.getmembers()}
    firsts = {}
    for name, (header, _, _) in offsets.items():
        parts = name.split("/")
        group = parts[3] if parts[:3] == ["island", "usd", "elements"] else "root"
        firsts.setdefault(group, header)
    compressor = zlib.compressobj(6, zlib.DEFLATED, 31)
    out, done, points = bytearray(), 0, []
    for at in sorted(firsts.values()):
        out += compressor.compress(raw[done:at])
        done = at
        out += compressor.flush(zlib.Z_SYNC_FLUSH)
        points.append((len(out), 0, at, raw[max(0, at - 32768) : at]))
    out += compressor.compress(raw[done:]) + compressor.flush()
    data = bytes(out)
    members = {}
    for name, (_, start, size) in offsets.items():
        if plugin._kept(name) and name.startswith("island/usd/"):
            end = next((c for c, _, u, _ in points if u >= start + size), len(data))
            members[name] = plugin.mi.Member(start, size, hashlib.sha256(MEMBERS[name]).hexdigest(), end)
    return data, points, members


@pytest.fixture
def indexed(tmp_path, monkeypatch, moana):
    """The plugin with an index of the archive; 'answer' says what the server says of it, 'asked' what was asked."""
    data, points, members = _indexed_archive()
    state = {"etag": '"e"', "asked": [], "heads": 0}

    def make(etag='"e"', corrupt=None, **parameters):
        index_members = dict(members)
        if corrupt:
            index_members[corrupt] = index_members[corrupt]._replace(sha256="0" * 64)
        archive = {"url": plugin.ARCHIVE, "size": len(data), "etag": '"e"', "last_modified": None}
        path = tmp_path / "index.xz"
        path.write_bytes(plugin.mi.dump(archive, points, index_members))
        monkeypatch.setattr(plugin, "INDEX", str(path))
        state["etag"] = etag
        made = moana(**parameters)
        made.asked = state["asked"]
        made.size = len(data)
        return made

    def head(self, url, what):
        assert url == plugin.ARCHIVE
        state["heads"] += 1
        return len(data), True, state["etag"]

    def ranged(self, url, start, end, what):
        assert url == plugin.ARCHIVE
        state["asked"].append((start, end))
        return data[start:end]

    monkeypatch.setattr(ua.Http, "head", head)
    monkeypatch.setattr(ua.Http, "range", ranged)
    make.state = state
    return make


def test_an_element_is_read_through_the_index_its_own_folder_and_no_more(indexed, capsys):
    made = indexed(download=True)
    data = base64.b64decode(made.get("files/isKava.usdz"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == ["element.usda", "model.usd"]
    assert made.streamed == [] and made.asked
    # From the element's own access point: not the archive up to it, nor what comes after it.
    assert sum(end - start for start, end in made.asked) < made.size
    assert "through its index" in capsys.readouterr().err
    # Kept: a second build asks for nothing.
    asked = len(made.asked)
    again = indexed(download=True)
    assert base64.b64decode(again.get("files/isKava.usdz")) == data
    assert len(again.asked) == asked


def test_the_elements_are_listed_from_the_index_without_a_request(indexed):
    made = indexed()
    assert "isNew" in made.get("objects/part")
    assert made.asked == [] and made.streamed == [] and indexed.state["heads"] == 0


def test_nothing_is_read_through_the_index_until_the_plugin_may(indexed, capsys):
    made = indexed()
    assert ua.serve(made, "files/isKava.usdz") is None
    said = capsys.readouterr().err
    assert "through the index this plugin ships" in said and "download: true" in said
    assert made.asked == [] and made.streamed == []


@pytest.mark.parametrize("what", ["another archive", "a member that is not what it says"])
def test_an_archive_the_index_does_not_describe_is_read_whole_instead(indexed, capsys, what):
    if what == "another archive":
        made = indexed(etag='"other"', download=True)
    else:
        made = indexed(corrupt="island/usd/elements/isKava/model.usd", download=True)
    data = base64.b64decode(made.get("files/isKava.usdz"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == ["element.usda", "model.usd"]
    assert made.streamed == [plugin.ARCHIVE]
    assert "reading the whole archive, once, instead" in capsys.readouterr().err


def test_the_shipped_index_is_the_one_the_plugin_reads():
    assert os.path.exists(plugin.INDEX)
    index = plugin.mi.load(plugin.INDEX)
    assert index is not None and index.archive["url"] == plugin.ARCHIVE
    elements = {n.split("/")[3] for n in index.members if n.startswith("island/usd/elements/")}
    assert set(plugin.ELEMENTS) <= elements
