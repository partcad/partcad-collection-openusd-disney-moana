#
# partcad-openusd-disney-moana, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""A PartCAD 'basic' repository plugin that serves Walt Disney Animation Studios' Moana Island Scene as parts.

Published as ``//pub/collection/openusd/disney-moana/assets``. The Moana Island
Scene (https://www.disneyanimation.com/resources/moana-island-scene/), which
openusd.org's Downloads page lists among its "Additional Assets", is published
in USD as one gzipped tar archive of 15 GB. Its elements - the beach, the
mountains, the palms, the coral, the ocean - are a part each, named as the
scene names them (``isBeach``, ``isMountainB``, ``osOcean``), and the whole
island is one more (``island``). Each is ``type: usd``, built by PartCAD's
own part type of that name from a USDZ the plugin packs of the element's own layers (see
usd_assets.py).

A gzipped tar archive cannot be read at random as it is. So the plugin ships a
seek index of it (``island-usd-v2.1.index.xz``, see moana_index.py and
tools/build_moana_index.py): building an element asks the server for that
element's own part of the archive - from under a MB to 550 MB of its 15 GB -
and keeps the element's layers in the plugin's cache. Should the archive no
longer be the one the index describes, the first part built streams the whole
archive, once, as the plugin did before it had an index. Neither is done
unless the user says so, with the plugin's 'download' parameter; until then,
the elements are listed and nothing is fetched.

The elements are listed from the index, and without it from what the archive
is known to hold (ELEMENTS), so that listing them costs nothing.

PartCAD runs this script once per key (via runpy) with the key in
``request["key"]``; the answer goes in ``output``.
"""

import importlib.util
import os
import sys

# When this plugin's answers stopped meaning what they meant before. PartCAD
# keys everything it caches of this package on it, so raise it with any change
# to what is answered - here, or in usd_assets.py.
#
# v1: the island's elements, from island-usd-v2.1, as PartCAD 'usd' parts.
# v2: with the files a part's look is read from - its shaders, and its colour and finish
#     images, a smaller copy where the source keeps one ('looks').
# v3: an element is read through the archive's seek index, its own bytes only.
CACHE_VERSION = 3

_HERE = os.path.dirname(os.path.abspath(__file__))


def _beside(script, name):
    """The module 'script', beside this one, under a name of this plugin's own."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, script))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


ua = _beside("usd_assets.py", "_partcad_openusd_disney_moana_usd_assets")
mi = _beside("moana_index.py", "_partcad_openusd_disney_moana_index")

# The archive's seek index, and the licence it is shipped under: its access
# points carry 32 KB of the scene each.
INDEX = os.path.join(_HERE, "island-usd-v2.1.index.xz")

PAGE = "https://www.disneyanimation.com/resources/moana-island-scene/"
ARCHIVE = "https://datasets.disneyanimation.com/moanaislandscene/island-usd-v2.1.tgz"
ARCHIVE_GB = 15.2
KEPT_GB = 3.2
TERMS = "https://media.disneyanimation.com/uploads/production/data_set_asset/4/asset/License.txt"
LICENSE = "LicenseRef-Moana-Island-Scene"

# The elements of island-usd-v2.1, as it was read in November 2026: the
# folders under island/usd/elements, each opened by its 'element.usda'; and
# what each is, in the words of the scene's own README where it has some.
ELEMENTS = {
    "isBayCedarA1": "Bay cedars.",
    "isBeach": "The beach, with its ground cover, pebbles, shells and seaweed.",
    "isCoastline": "The coastline.",
    "isCoral": "The coral.",
    "isDunesA": "Dunes.",
    "isDunesB": "More dunes.",
    "isGardeniaA": "Gardenias.",
    "isHibiscus": "Hibiscus.",
    "isHibiscusYoung": "Young hibiscus.",
    "isIronwoodA1": "Ironwood trees.",
    "isIronwoodB": "More ironwood trees.",
    "isKava": "Kava.",
    "isLavaRocks": "Lava rocks.",
    "isMountainA": "A mountain.",
    "isMountainB": "The great mountain.",
    "isNaupakaA": "Naupaka.",
    "isPalmDead": "Dead palms.",
    "isPalmRig": "Palms.",
    "isPandanusA": "Pandanus.",
    "osOcean": "The ocean.",
}


def _kept(name):
    """What of the archive is kept: its USD layers and its licence, not its 14 GB of Ptex textures.

    Nor the '._' files macOS's tar put beside every file and folder, which are its metadata.
    """
    if name.rsplit("/", 1)[-1].startswith("._"):
        return False
    return name.startswith("island/usd/") or name == "island/License.txt"


class IndexedStore(ua.Store):
    """The archive's USD layers, read a folder at a time through its seek index; else the whole archive, once.

    Its listing is the index's, so it is known without a request. A file asked
    for is fetched with its element's whole folder - the folder is one stretch
    of the archive, so it costs one read from the access point before it - and
    any other file by itself; each into the same tree of the plugin's cache as
    the whole archive read once would leave. What the index says of the
    archive is checked against the server before anything is read from it -
    its size and ETag - and every file read against its SHA-256: anything that
    does not match is said, and the whole archive is read instead ('tar').
    """

    def __init__(self, http, index, tar, allowed, refusal, prefix="island/"):
        self.http, self.index, self.tar, self.allowed, self.refusal = http, index, tar, allowed, refusal
        self.prefix = prefix
        self.paths = {
            name[len(prefix) :]: member.size for name, member in index.members.items() if name.startswith(prefix)
        }
        self.checked = False
        self.whole = False  # the index does not describe the archive: it is read whole, once
        self.fetched = 0  # compressed bytes asked for, for whoever wants to know

    def ready(self):
        return True

    def _tar(self):
        return self.whole or self.tar.ready()

    def listing(self):
        return self.tar.listing() if self._tar() else self.paths

    def _local(self, path):
        return os.path.join(self.tar.directory(), *self.prefix.strip("/").split("/"), *path.split("/"))

    def _have(self, path):
        local = self._local(path)
        return os.path.exists(local) and os.path.getsize(local) == self.paths[path]

    def read(self, path):
        if not self._tar():
            if path not in self.paths:
                raise ua.NotFound("%s is not in %s" % (path, self.tar.what))
            if not self._have(path):
                self.prefetch([path])
        if self._tar():
            return self.tar.read(path)
        with open(self._local(path), "rb") as f:
            return f.read()

    def prefetch(self, paths):
        if self._tar():
            return
        missing = [path for path in paths if path in self.paths and not self._have(path)]
        if not missing:
            return
        if not self.allowed:
            raise ua.NotAccepted(self.refusal)
        try:
            self._check()
            wanted = self._with_folders(missing)
            ua.warn(
                "%s: reading %d file%s of %s through its index"
                % (self.http.name, len(wanted), "" if len(wanted) == 1 else "s", self.tar.what)
            )
            self.fetched += mi.read(
                lambda start, end: self.http.range(self.tar.url, start, end, self.tar.what),
                self.index.points,
                self.index.members,
                [self.prefix + path for path in wanted],
                self._sink,
            )
        except mi.Mismatch as e:
            ua.warn("%s: %s; reading the whole archive, once, instead" % (self.http.name, e))
            self.whole = True

    def _with_folders(self, paths):
        """'paths', each with the rest of its element's folder."""
        wanted = set()
        for path in paths:
            parts = path.split("/")
            if parts[:2] == ["usd", "elements"] and len(parts) > 3:
                folder = "/".join(parts[:3]) + "/"
                wanted.update(p for p in self.paths if p.startswith(folder) and not self._have(p))
            else:
                wanted.add(path)
        return sorted(wanted)

    def _check(self):
        if self.checked:
            return
        size, ranges, validator = self.http.head(self.tar.url, self.tar.what)
        archive = self.index.archive
        if not ranges:
            raise mi.Mismatch("the server does not answer range requests for %s" % self.tar.what)
        if size != archive["size"] or validator not in (archive.get("etag"), archive.get("last_modified")):
            raise mi.Mismatch(
                "%s is %d bytes (%s) on the server, and its index is of one of %d bytes (%s)"
                % (self.tar.what, size, validator or "no ETag", archive["size"], archive.get("etag"))
            )
        self.checked = True

    def _sink(self, name):
        return _File(self._local(name[len(self.prefix) :]))


class _File:
    """A file of the cache, written beside itself and put in place once all of it is there."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.temporary = "%s.%d.partial" % (path, os.getpid())
        self.file = open(self.temporary, "wb")

    def write(self, data):
        self.file.write(data)

    def close(self):
        self.file.close()
        os.replace(self.temporary, self.path)

    def discard(self):
        self.file.close()
        if os.path.exists(self.temporary):
            os.remove(self.temporary)


class Moana(ua.Source):
    NAME = "openusd-disney-moana"
    TITLE = "Walt Disney Animation Studios"
    URL = PAGE
    DESC = (
        "Walt Disney Animation Studios' Moana Island Scene: the island, and each of its elements, as parts. "
        "Building one reads that element's own part of the 15 GB archive, when you allow it (see the README)."
    )
    LICENSE = LICENSE
    VERSION = CACHE_VERSION
    REPOSITORY = "partcad-collection-openusd-disney-moana"
    PLUGIN = "//pub/collection/openusd/disney-moana:moana_repo"

    def store(self):
        allowed = self.flag("download")
        tar = ua.TarStore(
            self.http,
            ARCHIVE,
            "the Moana Island Scene's USD archive",
            _kept,
            prefix="island",
            allowed=allowed,
            refusal=(
                "%s: the Moana Island Scene is one tar archive of %.1f GB, which cannot be read in pieces without "
                "its index: building a part of it reads it all, once, and keeps its USD layers (%.1f GB) in %s. It "
                "is licensed for research and software development only (%s). To let the plugin do that, set its "
                "'download' parameter, in ~/.partcad/config.yaml:\n\n"
                '    parameters:\n      "%s":\n        download: true\n'
                % (self.NAME, ARCHIVE_GB, KEPT_GB, ua.cache_dir(self.NAME), TERMS, self.PLUGIN)
            ),
        )
        index = mi.load(INDEX)
        if index is None:
            return tar
        return IndexedStore(
            self.http,
            index,
            tar,
            allowed,
            refusal=(
                "%s: building a part of the Moana Island Scene reads that element's own part of Disney's %.1f GB "
                "archive - from under a MB to 550 MB of it - through the index this plugin ships, and keeps its USD "
                "layers in %s. The scene is licensed for research and software development only (%s). To let the "
                "plugin do that, set its 'download' parameter, in ~/.partcad/config.yaml:\n\n"
                '    parameters:\n      "%s":\n        download: true\n'
                % (self.NAME, ARCHIVE_GB, ua.cache_dir(self.NAME), TERMS, self.PLUGIN)
            ),
        )

    def assets(self, collection):
        store = self.store()
        elements = dict(ELEMENTS)
        if store.ready():
            # What the archive held, now that it has been read.
            for path in store.listing():
                parts = path.split("/")
                if len(parts) == 4 and parts[:2] == ["usd", "elements"] and parts[3] == "element.usda":
                    elements.setdefault(parts[2], "An element of the island.")
        found = {
            "island": ua.Asset("island", store, "usd/island.usda", desc="The whole island, every element in place.",
                               url=PAGE, author="Walt Disney Animation Studios")
        }  # fmt: skip
        for name, desc in sorted(elements.items()):
            found[name] = ua.Asset(
                name, store, "usd/elements/%s/element.usda" % name, desc=desc, url=PAGE,
                author="Walt Disney Animation Studios",
            )  # fmt: skip
        return found


def get(key):
    return ua.serve(Moana(globals().get("request")), key)


# Last, so that everything above is defined when PartCAD runs this.
if __name__ == "get":
    output = {"result": get(request["key"])}  # noqa: F821 - injected by the runtime
else:
    output = {}
