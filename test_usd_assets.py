#
# PartCAD OpenUSD asset repository plugins, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""Tests of usd_assets.py, the part every OpenUSD asset plugin shares.

The same file is in each partcad-collection-openusd-* repository, beside the
same module. Nothing here touches the network: a source is a stand-in, and HTTP is
answered from a table. The tests that need Pixar's USD library ('usd-core')
check the packages this module writes the way USD itself reads them, and are
skipped where it is not installed.
"""

import base64
import importlib.util
import io
import os
import re
import struct
import urllib.parse
import zipfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location("usd_assets_under_test", os.path.join(_HERE, "usd_assets.py"))
ua = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ua)

try:
    from pxr import Sdf, Usd, UsdGeom, UsdShade  # noqa: F401

    HAVE_USD = True
except ImportError:
    HAVE_USD = False

needs_usd = pytest.mark.skipif(not HAVE_USD, reason="usd-core is not installed")

# A binary layer, as usd-core 26.11 writes it (crate 0.8.0, its tokens LZ4
# compressed): a prim that references './geom.usda' and two shaders whose
# files are 'textures/wood.png' and the UDIM '../../shared/metal_<UDIM>.png'.
CRATE = base64.b64decode(
    "UFhSLVVTREMACAAAAAAAAMcCAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAgAAACABAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAADwPwAAAAAAAAAAAQAA"
    "AAAAAAAKAAAAAgAAAAAAAAALAAAADAAAAAEAAAAAAAAADgAAABQAAAAAAAAAvQAAAAAAAAC8AAAAAAAAAAD0hzstKQAAQ2hh"
    "aXIAcHJpbUNoaWxkcmVuAGRlZmF1bHRQcmltAHNwZWNpZmllcgBYZm9ybQB0eXBlTmFtZQAuL2dlb20udXNkYQByZWZlcmVu"
    "Y2VzAE1hdAB0ZXgAdGV4MgBTaGFkZXIAaW5wdXRzOmZpbGUAcHJvcGVydGllcwBhc3NldAB0ZXh0dXJlcy93b29kLnBuZ38A"
    "8BAALi4vLi4vc2hhcmVkL21ldGFsXzxVRElNPi5wbmcAAQAAAAAAAAAIAAAADAAAAAAAAAAUAAAAAAAAAADwAgIAAAAVVFUD"
    "AQH6AAQI+AsATAAAAAAAAAAAIFgAAQAwKQACBwAxAAtABwBBACpABgkAMQtAZAgAMSMAjQgAMykAmQgAEQ0IADELQKkIADEp"
    "ABAIADELQBEIAKAMQBMAAAAAAAxAFQAAAAAAAAAcAAAAAAAAAADwCgEAAABRQFVRVAEA/gP6AwT5AwX3CvUKAvQIAAAAAAAA"
    "AAcAAAAAAAAADAAAAAAAAAAAoAIAAABRBAABAf8MAAAAAAAAAACgAQAAABAVCOca5g0AAAAAAAAAALAAAAAAQRX/A/wB/wcA"
    "AAAAAAAACgAAAAAAAAAAgAEAAAAFAAACDAAAAAAAAAAAoAMAAAARBQAFAAQLAAAAAAAAAACQAAAAAAUEB//7BgAAAAAAAABU"
    "T0tFTlMAAAAAAAAAAAAAtQAAAAAAAADUAAAAAAAAAFNUUklOR1MAAAAAAAAAAACJAQAAAAAAAAwAAAAAAAAARklFTERTAAAA"
    "AAAAAAAAAJUBAAAAAAAAeAAAAAAAAABGSUVMRFNFVFMAAAAAAAAADQIAAAAAAAAsAAAAAAAAAFBBVEhTAAAAAAAAAAAAAAA5"
    "AgAAAAAAAE0AAAAAAAAAU1BFQ1MAAAAAAAAAAAAAAIYCAAAAAAAAQQAAAAAAAAA="
)


# --- LZ4 and the token table of a binary layer ---------------------------------


def test_an_lz4_block_with_an_overlapping_match_repeats_what_it_copies():
    # Literal 'ab', then a match 6 long at offset 2: 'ababab' - 'abababab'.
    block = bytes([0x22]) + b"ab" + struct.pack("<H", 2)
    assert ua.lz4_block(block, 100) == b"abababab"


def test_an_lz4_block_that_points_before_its_start_is_refused():
    with pytest.raises(ValueError):
        ua.lz4_block(bytes([0x10]) + b"a" + struct.pack("<H", 5), 100)


def test_a_binary_layers_tokens_are_read_without_usd():
    tokens = ua.crate_tokens(CRATE)
    assert "./geom.usda" in tokens
    assert "textures/wood.png" in tokens
    assert "Chair" in tokens


def test_a_binary_layer_names_its_layers_and_textures():
    assert ua.named_paths("Chair_payload.usdc", CRATE) == [
        "./geom.usda",
        "textures/wood.png",
        "../../shared/metal_<UDIM>.png",
    ]


def test_a_text_layer_names_its_asset_paths():
    text = (
        b'#usda 1.0\n(\n    subLayers = [@./look.usda@]\n)\ndef "A" (\n    references = @@@odd@name.usd@@@</A>\n)\n{}\n'
    )
    assert ua.named_paths("a.usda", text) == ["./look.usda", "odd@name.usd"]


def test_a_package_names_nothing_outside_itself():
    assert ua.named_paths("a.usdz", b"PK\x03\x04rest") == []


# --- following the names ---------------------------------------------------------


def _store(files):
    return ua.MemoryStore(files)


def test_a_relative_path_a_search_path_and_a_udim_are_resolved():
    store = _store({"a/b/x.usda": b"", "a/b/t/w.png": b"", "a/m_1001.png": b"", "a/m_1002.png": b"", "lib/l.usda": b""})
    assert ua.resolve("a/b/root.usda", "./x.usda", store.exists) == ["a/b/x.usda"]
    assert ua.resolve("a/b/root.usda", "t/w.png", store.exists) == ["a/b/t/w.png"]
    assert ua.resolve("a/b/root.usda", "lib/l.usda", store.exists) == ["lib/l.usda"]  # from the top
    assert ua.resolve("a/b/root.usda", "../m_<UDIM>.png", store.exists) == ["a/m_1001.png", "a/m_1002.png"]
    assert ua.resolve("a/b/root.usda", "./absent.usda", store.exists) == []
    for elsewhere in ("/abs/x.usd", "https://x/y.usd", "omniverse://h/x.usd", "${ASSET}/x.usd"):
        assert ua.resolve("a/b/root.usda", elsewhere, store.exists) is None


def test_a_path_out_of_the_tree_is_not_followed():
    store = _store({"x.usda": b""})
    assert ua.resolve("a/root.usda", "../../x.usda", store.exists) == []


def test_an_asset_is_everything_its_root_reaches_and_textures_only_when_asked():
    store = _store(
        {
            "assets/Chair/Chair.usda": b"#usda 1.0\n(\n subLayers = [@../../shared/look.usda@]\n)\n"
            b'def "Chair" ( payload = @./Chair_payload.usdc@ ) {}\n',
            "assets/Chair/Chair_payload.usdc": CRATE,
            "assets/Chair/geom.usda": b"#usda 1.0\n",
            "assets/Chair/textures/wood.png": b"png",
            "shared/look.usda": b"#usda 1.0\n",
            "shared/metal_1001.png": b"png",
            "unrelated.usda": b"#usda 1.0\n",
        }
    )
    files, missing = ua.closure("assets/Chair/Chair.usda", store.read, store.exists, textures=False)
    assert files == [
        "assets/Chair/Chair.usda",
        "shared/look.usda",
        "assets/Chair/Chair_payload.usdc",
        "assets/Chair/geom.usda",
    ]
    assert missing == []
    files, _ = ua.closure("assets/Chair/Chair.usda", store.read, store.exists, textures=True)
    assert files[-2:] == ["assets/Chair/textures/wood.png", "shared/metal_1001.png"]


def test_what_an_asset_names_and_the_source_lacks_is_reported():
    store = _store({"a.usda": b"#usda 1.0\n( subLayers = [@./gone.usda@] )\n"})
    files, missing = ua.closure("a.usda", store.read, store.exists)
    assert files == ["a.usda"]
    assert missing == [("a.usda", "./gone.usda")]


def test_omniverse_s_own_mdl_library_is_no_file_an_asset_lacks():
    store = _store({"a.usda": b"#usda 1.0\ndef Shader \"s\" { uniform asset info:mdl:sourceAsset = @OmniPBR.mdl@ }\n"})
    assert ua.closure("a.usda", store.read, store.exists, textures=ua.is_look)[1] == []


# --- USDZ ------------------------------------------------------------------------


def test_a_package_is_stored_aligned_and_root_first():
    data = ua.write_usdz([("dir/root.usda", b"#usda 1.0\n"), ("dir/a.png", b"x" * 100), ("b.usdc", CRATE)])
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = z.infolist()
        assert [i.filename for i in infos] == ["dir/root.usda", "dir/a.png", "b.usdc"]
        for info in infos:
            assert info.compress_type == zipfile.ZIP_STORED
            start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            assert start % 64 == 0, info.filename
        assert z.read("b.usdc") == CRATE


def test_a_package_is_the_same_bytes_every_time():
    entries = [("r.usda", b"#usda 1.0\n"), ("t.png", b"png")]
    assert ua.write_usdz(entries) == ua.write_usdz(entries)


def test_a_package_keeps_the_paths_below_what_its_files_share():
    assert ua.package_root(["a/b/c/r.usd", "a/b/t/x.png", "a/b/r2.usd"]) == "a/b"
    assert ua.package_root(["a/r.usd", "b/x.usd"]) == ""


@needs_usd
def test_usd_reads_a_package_written_here_through_every_layer(tmp_path):
    store = _store(
        {
            "assets/Chair/Chair.usda": b'#usda 1.0\n( defaultPrim = "Chair" )\n'
            b'def Xform "Chair" ( prepend payload = @./Chair_payload.usdc@ ) {}\n',
            "assets/Chair/Chair_payload.usdc": CRATE,
            "assets/Chair/geom.usda": b'#usda 1.0\n( defaultPrim = "Chair" )\ndef Xform "Chair" {\n'
            b'  def Mesh "seat" {\n    point3f[] points = [(0,0,0),(1,0,0),(1,1,0)]\n'
            b"    int[] faceVertexCounts = [3]\n    int[] faceVertexIndices = [0,1,2]\n  }\n}\n",
        }
    )
    files, _ = ua.closure("assets/Chair/Chair.usda", store.read, store.exists, textures=False)
    path = tmp_path / "chair.usdz"
    path.write_bytes(ua.write_usdz([(f[len("assets/Chair/") :], store.read(f)) for f in files]))
    stage = Usd.Stage.Open(str(path))
    assert stage.GetPrimAtPath("/Chair/seat").IsA(UsdGeom.Mesh)


# --- HTTP, archives and their cache ----------------------------------------------


class Wire:
    """HTTP answered from a table of {url: bytes}, with ranges where 'ranges' says."""

    def __init__(self, table, ranges=True):
        self.table = dict(table)
        self.ranges = ranges
        self.asked = []

    def __call__(self, url, headers=None, method=None, timeout=120):
        headers = dict(headers or {})
        self.asked.append((url, headers.get("Range")))
        if url not in self.table:
            return ua.Response(404, {}, b"", url)
        body = self.table[url]
        wanted = headers.get("Range")
        if wanted and self.ranges:
            start, end = (int(v) for v in re.match(r"bytes=(\d+)-(\d+)", wanted).groups())
            end = min(end, len(body) - 1)
            return ua.Response(
                206, {"Content-Range": "bytes %d-%d/%d" % (start, end, len(body))}, body[start : end + 1], url
            )
        return ua.Response(200, {"Content-Length": str(len(body))}, body, url)


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("PARTCAD_TESTSOURCE_CACHE", str(tmp_path / "cache"))

    def make(table, ranges=True):
        made = ua.Http("testsource", "test")
        made.request = Wire(table, ranges)
        return made

    return make


ARCHIVE = "https://example.com/set.zip"
MEMBERS = {
    "Set/assets/Cup/Cup.usda": b'#usda 1.0\ndef "Cup" ( references = @./geom.usda@ ) {}\n',
    "Set/assets/Cup/geom.usda": b"#usda 1.0\n" + b"#" * 300000,
    "Set/README.txt": b"hello",
}


def test_a_large_archive_that_takes_ranges_is_read_a_member_a_request_and_kept(http):
    made = http({ARCHIVE: _zip(MEMBERS)})
    store = ua.ZipStore(made, ARCHIVE, "the set", prefix="Set", whole_mb=0)
    assert sorted(store.listing()) == ["README.txt", "assets/Cup/Cup.usda", "assets/Cup/geom.usda"]
    made.request.asked.clear()
    assert store.read("assets/Cup/geom.usda") == MEMBERS["Set/assets/Cup/geom.usda"]
    assert len(made.request.asked) == 1 and made.request.asked[0][1] is not None
    # Asked again, by another run: from the cache, not the wire.
    again = ua.ZipStore(made, ARCHIVE, "the set", prefix="Set", whole_mb=0)
    made.request.asked.clear()
    assert again.read("assets/Cup/geom.usda") == MEMBERS["Set/assets/Cup/geom.usda"]
    assert made.request.asked == []
    # A generation of members is fetched together, and each only once.
    again.prefetch(["assets/Cup/Cup.usda", "README.txt", "assets/Cup/geom.usda"])
    assert len(made.request.asked) == 2
    assert again.read("README.txt") == b"hello"


def _downloads(made, monkeypatch):
    downloads = []

    def download(url, target, what, timeout=3600):
        downloads.append(url)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(made.request.table[url])
        return target

    monkeypatch.setattr(made, "download", download)
    return downloads


@pytest.mark.parametrize("ranges", [True, False])
def test_a_small_archive_or_one_without_ranges_is_downloaded_once(http, monkeypatch, ranges):
    made = http({ARCHIVE: _zip(MEMBERS)}, ranges=ranges)
    downloads = _downloads(made, monkeypatch)
    store = ua.ZipStore(made, ARCHIVE, "the set", prefix="Set", whole_mb=0 if not ranges else 64)
    assert store.read("assets/Cup/Cup.usda") == MEMBERS["Set/assets/Cup/Cup.usda"]
    assert store.read("README.txt") == b"hello"
    assert ua.ZipStore(made, ARCHIVE, "the set", prefix="Set").read("README.txt") == b"hello"
    assert downloads == [ARCHIVE]


def test_an_archive_without_ranges_is_not_downloaded_to_be_listed(http, monkeypatch):
    made = http({ARCHIVE: _zip(MEMBERS)}, ranges=False)
    downloads = _downloads(made, monkeypatch)
    monkeypatch.setattr(ua, "LIST_WHOLE_MB", 0)
    with pytest.raises(ua.TooLarge, match="cannot be listed without being downloaded"):
        ua.ZipStore(made, ARCHIVE, "the set", prefix="Set").listing()
    assert downloads == []
    # Building an asset of it downloads it.
    assert ua.ZipStore(made, ARCHIVE, "the set", prefix="Set").read("README.txt") == b"hello"
    assert downloads == [ARCHIVE]


def test_an_archive_too_large_to_download_whole_is_refused_saying_so(http):
    made = http({ARCHIVE: _zip(MEMBERS)}, ranges=False)
    store = ua.ZipStore(made, ARCHIVE, "the set", max_download_mb=0)
    with pytest.raises(ua.TooLarge, match="does not take range requests"):
        store.listing()


def test_a_tar_archive_is_read_once_for_the_members_kept_and_only_when_allowed(http, monkeypatch, tmp_path):
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, data in {"island/usd/a.usda": b"#usda 1.0\n", "island/textures/t.ptx": b"x" * 1000}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    opened = []

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def urlopen(request, timeout=None):
        opened.append(request.full_url)
        return Answer(buf.getvalue())

    monkeypatch.setattr(ua.urllib.request, "urlopen", urlopen)
    made = http({})
    refused = ua.TarStore(made, "https://example.com/island.tgz", "the island", lambda n: "/usd/" in n, "island")
    with pytest.raises(ua.NotAccepted):
        refused.listing()
    assert opened == []
    store = ua.TarStore(made, "https://example.com/island.tgz", "the island", lambda n: "/usd/" in n, "island", True)
    assert store.listing() == {"usd/a.usda": 10}
    assert store.read("usd/a.usda") == b"#usda 1.0\n"
    again = ua.TarStore(made, "https://example.com/island.tgz", "the island", lambda n: "/usd/" in n, "island")
    assert again.read("usd/a.usda") == b"#usda 1.0\n"
    assert opened == ["https://example.com/island.tgz"]


BUCKET = "https://bucket.example.com/"
OBJECTS = {
    "Assets/Props/Chair/Chair.usd": b"#usda 1.0\n( subLayers = [@./Textures/wood_<UDIM>.png@, @../Shared/mat.usda@] )\n",
    "Assets/Props/Chair/Textures/wood_1001.png": b"png",
    "Assets/Props/Chair/Textures/wood_1002.png": b"png",
    "Assets/Props/Shared/mat.usda": b"#usda 1.0\n",
    "Assets/Props/Table/Table.usd": b"#usda 1.0\n",
    "Assets/Props/Table/.thumbs/Table.png": b"png",
}


def _bucket_listing(url):
    """What S3's ListObjectsV2 answers for 'url', a folder at a time when asked for one, a key a page."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    prefix, delimiter = query.get("prefix", [""])[0], query.get("delimiter", [None])[0]
    after = query.get("continuation-token", [None])[0]
    keys = sorted(k for k in OBJECTS if k.startswith(prefix) and (not delimiter or "/" not in k[len(prefix) :]))
    folders = sorted({k[len(prefix) :].split("/")[0] for k in OBJECTS if k.startswith(prefix) and delimiter
                      and "/" in k[len(prefix) :]})  # fmt: skip
    entries = [("k", k) for k in keys] + [("p", prefix + f + "/") for f in folders]
    start = [e[1] for e in entries].index(after) + 1 if after else 0
    page, more = entries[start : start + 2], start + 2 < len(entries)
    body = "<ListBucketResult><IsTruncated>%s</IsTruncated>" % ("true" if more else "false")
    for kind, name in page:
        if kind == "k":
            body += "<Contents><Key>%s</Key><Size>%d</Size></Contents>" % (name, len(OBJECTS[name]))
        else:
            body += "<CommonPrefixes><Prefix>%s</Prefix></CommonPrefixes>" % name
    if more:
        body += "<NextContinuationToken>%s</NextContinuationToken>" % page[-1][1]
    return (body + "</ListBucketResult>").encode()


@pytest.fixture
def bucket(http):
    made = http({})
    listed = []

    def request(url, headers=None, method=None, timeout=120):
        if url.startswith(BUCKET + "?"):
            listed.append(url)
            return ua.Response(200, {}, _bucket_listing(url), url)
        path = urllib.parse.unquote(url[len(BUCKET) :])
        if path in OBJECTS:
            return ua.Response(200, {}, OBJECTS[path], url)
        return ua.Response(404, {}, b"", url)

    made.request = request
    made.listed = listed
    return made


def test_a_bucket_is_listed_a_folder_at_a_time_and_walked_past_what_is_skipped(bucket):
    store = ua.S3Store(bucket, BUCKET)
    files, folders = store.folder("Assets/Props")
    assert files == {} and folders == ["Chair", "Shared", "Table"]
    walked = store.walk("Assets/Props", skip=lambda name: name in ("Textures", ".thumbs"))
    assert sorted(walked) == [
        "Assets/Props/Chair/Chair.usd",
        "Assets/Props/Shared/mat.usda",
        "Assets/Props/Table/Table.usd",
    ]
    # Kept: another store, another run, asks nothing again.
    bucket.listed.clear()
    ua.S3Store(bucket, BUCKET).walk("Assets/Props", skip=lambda name: name in ("Textures", ".thumbs"))
    assert bucket.listed == []


def test_an_asset_in_a_bucket_is_found_a_folder_at_a_time_udims_included(bucket):
    store = ua.S3Store(bucket, BUCKET)
    files, missing = ua.closure("Assets/Props/Chair/Chair.usd", store.read, store.exists, prefetch=store.prefetch,
                                size=store.size)  # fmt: skip
    assert files == [
        "Assets/Props/Chair/Chair.usd",
        "Assets/Props/Chair/Textures/wood_1001.png",
        "Assets/Props/Chair/Textures/wood_1002.png",
        "Assets/Props/Shared/mat.usda",
    ]
    assert missing == []
    assert store.size("Assets/Props/Shared/mat.usda") == len(OBJECTS["Assets/Props/Shared/mat.usda"])
    # Only the folders the asset is in, not the bucket.
    assert not any("prefix=&" in url or url.endswith("prefix=") for url in bucket.listed)


def test_a_fetch_that_failed_is_not_repeated_at_once(http):
    made = http({})
    with pytest.raises(ua.NotFound):
        made.get("https://example.com/none.json", "the catalog")
    made.request.asked.clear()
    with pytest.raises(ua.NotFound):
        made.get("https://example.com/none.json", "the catalog")
    assert made.request.asked == []


def test_a_catalog_that_cannot_be_fetched_is_answered_from_an_old_copy_when_asked(http, capsys):
    made = http({"https://example.com/tree.json": b'{"tree": []}'})
    assert made.json("https://example.com/tree.json", "the tree", ttl=1) == {"tree": []}
    made.request.table.clear()
    os.utime(made.path("body", "GET https://example.com/tree.json {}"), (0, 0))
    assert made.json("https://example.com/tree.json", "the tree", ttl=1, stale=True) == {"tree": []}
    assert "answering from what was kept" in capsys.readouterr().err
    with pytest.raises(ua.NotFound):
        made.json("https://example.com/tree.json", "the tree", ttl=1)


def test_a_credential_is_not_carried_to_another_host_by_a_redirect():
    import urllib.request

    handler = ua._SameHostRedirects()
    request = urllib.request.Request("https://api.example.com/a", headers={"Authorization": "Bearer t"})
    elsewhere = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example.net/a")
    assert not elsewhere.has_header("Authorization")
    same = handler.redirect_request(request, None, 302, "Found", {}, "https://api.example.com/b")
    assert same.has_header("Authorization")


def test_a_bot_check_is_said_to_be_one():
    response = ua.Response(403, {"cf-mitigated": "challenge"}, b"Just a moment...", "u")
    assert ua.bot_check(response) == "a Cloudflare bot check"
    with pytest.raises(ua.Refused, match="does not pretend to be a browser"):
        ua.Http("x", "ua").checked(response, "the catalog")


# --- a source, as PartCAD asks it --------------------------------------------------


class Source(ua.Source):
    NAME = "testsource"
    TITLE = "Test"
    URL = "https://example.com"
    DESC = "A stand-in"
    LICENSE = "CC-BY-4.0"

    def __init__(self, store, parameters=None, collections=True):
        super().__init__({"parameters": parameters or {}})
        self.store = store
        self._collections = collections

    def collections(self):
        return {"set": ua.Collection("set", "The set")} if self._collections else {}

    def assets(self, collection):
        return {
            "Cup": ua.Asset("Cup", self.store, "assets/Cup/Cup.usda", title="Cup", url="https://example.com/cup"),
            "Ball": ua.Asset("Ball", self.store, "assets/Ball.usdz", license="CC0-1.0"),
        }


def _source(parameters=None, collections=True):
    files = {k[len("Set/") :]: v for k, v in MEMBERS.items() if k.startswith("Set/")}
    files["assets/Ball.usdz"] = ua.write_usdz([("Ball.usda", b"#usda 1.0\n")])
    files["assets/Cup/look.png"] = b"png"
    return Source(ua.MemoryStore(files), parameters, collections)


def test_the_top_is_the_collections_and_holds_nothing_itself():
    source = _source()
    assert source.get("deps") == ["set"]
    assert source.get("meta")["objectKinds"] == []
    assert source.get("meta")["searchKinds"] == []
    assert source.get("meta")["manufacturable"] is False
    assert source.get("objects/part") == {}
    assert source.get("search/part?q=cup") == {}


def test_a_collection_lists_its_assets_as_parts_of_partcad_s_usd_type():
    source = _source()
    assert source.get("set/meta")["objectKinds"] == ["part"]
    assert source.get("set/meta")["searchKinds"] == ["part"]
    assert source.get("set/deps") == []
    parts = source.get("set/objects/part")
    assert list(parts) == ["Cup", "Ball"]
    assert parts["Cup"]["type"] == "usd"
    assert parts["Cup"]["path"] == "Cup.usdz"
    assert parts["Cup"]["license"] == "CC-BY-4.0"
    assert parts["Ball"]["license"] == "CC0-1.0"
    assert set(parts["Cup"]["parameters"]) == {"reader", "prim", "purpose", "maxTriangles", "surface", "time"}
    assert parts["Cup"]["parameters"]["reader"]["enum"] == ["usd-core", "cadmpeg"]
    assert source.get("set/objects/part/Cup") == parts["Cup"]
    assert source.get("set/objects/part/Cup;purpose=proxy") == parts["Cup"]
    assert source.get("set/objects/part/Nope") is None
    # PartCAD's own part type builds them: no package serves one.
    assert source.get("set/objects/partType") == {}
    assert source.get("set/files/usd_part.py") is None


def test_a_source_with_no_collections_is_one_package():
    source = _source(collections=False)
    assert source.get("deps") == []
    assert list(source.get("objects/part")) == ["Cup", "Ball"]
    assert "Cup.usdz" == source.get("objects/part/Cup")["path"]
    assert list(source.get("search/part?q=ball")) == ["Ball"]


def test_a_search_is_every_word_and_ranks_a_word_in_the_name_first():
    source = _source()
    assert list(source.get("set/search/part?q=cup")) == ["Cup"]
    # 'example.com' is in Cup's url, which is not searched; 'set' is the package of both.
    assert source.get("set/search/part?q=example") == {}
    assert list(source.get("set/search/part?q=set")) == ["Cup", "Ball"]
    assert list(source.get("set/search/part?q=set+ball")) == ["Ball"]
    assert list(source.get("set/search/part?q=set&limit=1")) == ["Cup"]
    assert source.get("set/search/part?q=") == {}
    assert source.get("set/search/part?q=cup")["Cup"]["type"] == "usd"


def test_listing_and_searching_read_no_file_of_an_asset():
    source = _source()
    read = []
    original = source.store.read
    source.store.read = lambda path: read.append(path) or original(path)
    source.get("set/objects/part")
    source.get("set/search/part?q=cup")
    source.get("set/objects/part/Cup")
    assert read == []


def test_an_assets_file_is_a_package_of_what_it_reaches():
    data = base64.b64decode(_source().get("set/files/Cup.usdz"))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.namelist() == ["Cup.usda", "geom.usda"]


def test_a_package_the_source_has_is_served_as_it_is():
    source = _source()
    assert base64.b64decode(source.get("set/files/Ball.usdz")) == source.store.read("assets/Ball.usdz")


def test_textures_change_the_file_a_part_names():
    source = _source({"textures": "true"})
    assert source.get("set/objects/part/Cup")["path"] == "Cup.textured.usdz"
    assert base64.b64decode(source.get("set/files/Cup.textured.usdz"))


def test_a_look_is_a_shader_or_an_image_of_a_colour_or_a_finish():
    for path in ("tex/wood_base_color.jpg", "T_Pallet_A1_Albedo.png", "textures/global-colors/blue.jpg",
                 "T_Pallet_A1_ORM.png", "bishop_black_roughness.jpg", "materials/Aluminium_Brushed_A.mdl",
                 "Bishop_mat.mtlx", "texture/mug_1_albedo.dds", "jar/decor_jar01_role_preview_texture.jpg",
                 # A word that names something, not what the image is of: ALab's thumb tack, a release's
                 # folder, a folder of masks, an image of a colour with an alpha.
                 "look/surfacing/decor_thumb_tack01/texture/decor_thumb_tack01_role_preview_texture.jpg",
                 "look/surfacing/decor_thumb_tack01/texture/decor_thumb_tack01_proxy_role_preview_texture.jpg",
                 "alpha_release/tex/wood_albedo.png", "normal/blue_albedo.png", "assets/Mask/tex/wood_base_color.png",
                 "alpha_albedo.png", "wood_albedo.1001.png", "1001.png",
                 "AlphaBlendModeTest/0/AlphaBlendModeTest_image0_e93a912e.png"):  # fmt: skip
        assert ua.is_look(path), path
    for path in ("T_Pallet_A1_Normal.png", "tex/bishop_black_normal.jpg", "thumbnails/Bishop.png", "cards/X_XPos.png",
                 "fragment/tex/jar/albedo.exr", "Leaf_Opacity.png", "rock_height.tif", "glow_emissive.png",
                 "jar_preview/cards_textures_X_pos.png", "jar/decor_jar01_opacity_role_preview_texture.jpg",
                 "shaders/look.osl", "PaintMask.png", "wood_AO.png", "tex/.thumbs/256x256/wood.png.auto.png",
                 "rock_normal_gl_2k.png", "wood_normal.<UDIM>.png", "Wood_Normal_Map.png", "chair_thumb.jpg",
                 "thumb_chair.jpg", "Screenshots/chair.png", "rock_disp2.png",
                 "stoat_body01/stoat_body01_opacity_proxy_role_preview_texture.jpg",
                 "NormalsTextureBiasAndScale/r_normal_map_reversed_y.png"):  # fmt: skip
        assert not ua.is_look(path), path


LOOKED = {
    "assets/Cup/Cup.usda": b"#usda 1.0\n"
    b'def "Cup" ( references = @./geom.usda@ ) {\n'
    b"  asset inputs:a = @./tex/cup_albedo.png@\n"
    b"  asset inputs:n = @./tex/cup_normal.png@\n"
    b"  asset inputs:b = @./tex/huge_basecolor.png@\n"
    b"  asset inputs:c = @./tex/T_Glaze_Albedo.png@\n"
    b"  asset info:mdl:sourceAsset = @./materials/Glaze.mdl@\n"
    b"}\n",
    "assets/Cup/geom.usda": b"#usda 1.0\n",
    "assets/Cup/tex/cup_albedo.png": b"albedo",
    "assets/Cup/tex/cup_normal.png": b"normal",
    "assets/Cup/tex/huge_basecolor.png": b"h" * 2000,
    "assets/Cup/tex/T_Glaze_Albedo.png": b"g" * 2000,
    "assets/Cup/tex/.thumbs/256x256/T_Glaze_Albedo.png.auto.png": b"small",
    "assets/Cup/materials/Glaze.mdl": b'mdl 1.4;\nexport material Glaze(*) = OmniPBR(ORM_texture: texture_2d("../tex/cup_orm.png"));\n',
    "assets/Cup/tex/cup_orm.png": b"orm",
    "assets/Ball.usdz": ua.write_usdz([("Ball.usda", b"#usda 1.0\n")]),
}


def test_a_part_carries_what_its_look_is_read_from_a_smaller_copy_where_there_is_one(monkeypatch):
    monkeypatch.setattr(ua, "LOOK_MAX_MB", 1000 / (1 << 20))
    source = Source(ua.MemoryStore(LOOKED))
    assert source.get("set/objects/part/Cup")["path"] == "Cup.usdz"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(source.get("set/files/Cup.usdz")))) as z:
        assert sorted(z.namelist()) == sorted(
            ["Cup.usda", "geom.usda", "tex/cup_albedo.png", "tex/T_Glaze_Albedo.png", "materials/Glaze.mdl",
             "tex/cup_orm.png"]  # fmt: skip
        )
        # The thumbnail the source keeps stands in for the image, under the image's name.
        assert z.read("tex/T_Glaze_Albedo.png") == b"small"


def test_a_part_without_its_look_names_another_file_and_carries_none():
    source = Source(ua.MemoryStore(LOOKED), {"looks": "false"})
    assert source.get("set/objects/part/Cup")["path"] == "Cup.bare.usdz"
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(source.get("set/files/Cup.bare.usdz")))) as z:
        assert z.namelist() == ["Cup.usda", "geom.usda"]
    textured = Source(ua.MemoryStore(LOOKED), {"textures": "true"})
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(textured.get("set/files/Cup.textured.usdz")))) as z:
        # Every texture, each as it is.
        assert z.read("tex/T_Glaze_Albedo.png") == b"g" * 2000 and "tex/cup_normal.png" in z.namelist()


def test_a_missing_look_is_reported_by_the_file_it_would_be_and_no_copy_of_it_is_looked_for(capsys):
    asked = []

    class Store(ua.MemoryStore):
        def stand_in(self, path):
            asked.append(path)
            return super().stand_in(path)

    files = dict(LOOKED)
    files["assets/Cup/Cup.usda"] += b'def "Gone" { asset inputs:a = @./thumbs/../tex/gone_albedo.png@ }\n'
    source = Source(Store(files))
    assert source.get("set/files/Cup.usdz")
    assert "./thumbs/../tex/gone_albedo.png (in assets/Cup/Cup.usda)" in capsys.readouterr().err
    assert asked and not [path for path in asked if "gone" in path]


def test_an_asset_whose_id_ends_like_a_variant_is_still_that_asset():
    class Twins(Source):
        def assets(self, collection):
            return {
                "Cup": ua.Asset("Cup", self.store, "assets/Cup/Cup.usda"),
                "Cup.bare": ua.Asset("Cup.bare", self.store, "assets/Ball.usdz"),
            }

    ball = LOOKED["assets/Ball.usdz"]
    for parameters, name in (({}, "Cup.bare.usdz"), ({"looks": "false"}, "Cup.bare.bare.usdz"),
                             ({"textures": "true"}, "Cup.bare.textured.usdz")):  # fmt: skip
        source = Twins(ua.MemoryStore(LOOKED), parameters)
        assert source.get("set/objects/part/Cup.bare")["path"] == name
        assert base64.b64decode(source.get("set/files/" + name)) == ball, name
    # A name made with other parameters is no asset's here, rather than another file served under it.
    with pytest.raises(ua.NotFound):
        Twins(ua.MemoryStore(LOOKED)).file("set", "Cup.textured.usdz")


def test_an_asset_larger_than_allowed_is_refused_before_its_layers_are_read(capsys):
    source = _source({"max_size_mb": "0.1"})
    read = []
    original = source.store.read
    source.store.read = lambda path: read.append(path) or original(path)
    assert ua.serve(source, "set/files/Cup.usdz") is None
    assert "assets/Cup/geom.usda" not in read
    assert "Raise the plugin's 'max_size_mb'" in capsys.readouterr().err


def test_an_asset_larger_than_allowed_says_how_to_take_it(capsys):
    source = _source({"max_size_mb": "0.1"})
    assert ua.serve(source, "set/files/Cup.usdz") is None
    assert "Raise the plugin's 'max_size_mb'" in capsys.readouterr().err


def test_an_unknown_key_has_no_answer():
    source = _source()
    assert source.get("set/objects/assembly") == {}
    assert source.get("set/files/elsewhere.step") is None
    assert source.get("whatever") is None


def test_the_shared_files_are_the_ones_every_openusd_plugin_has():
    """usd_assets.py and its tests are one copy, in every repository: shared.sha256 says which."""
    import hashlib

    listed = os.path.join(_HERE, "shared.sha256")
    if not os.path.exists(listed):
        pytest.skip("not in a plugin's repository")
    with open(listed) as f:
        expected = dict(reversed(line.split()) for line in f if line.strip())
    for name, digest in expected.items():
        with open(os.path.join(_HERE, name), "rb") as f:
            assert hashlib.sha256(f.read()).hexdigest() == digest, (
                "%s differs from the shared copy: change it in every partcad-collection-openusd-* repository" % name
            )
