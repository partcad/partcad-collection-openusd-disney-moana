#
# partcad-openusd-disney-moana, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Tests of the Moana plugin, with Disney's archive answered from a table.

The archive is a gzipped tar laid out as island-usd-v2.1.tgz is, with made-up
layers in it. Nothing here touches the network.
"""

import base64
import importlib.util
import io
import os
import re
import runpy
import tarfile
import zipfile

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
    monkeypatch.setenv("PARTCAD_OPENUSD_DISNEY_MOANA_CACHE", str(tmp_path))
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
