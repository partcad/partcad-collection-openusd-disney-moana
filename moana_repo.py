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

A tar archive cannot be read at random. So the first part built streams the
whole archive, once, keeping its USD layers - 3.2 GB of its 17 - and its
licence in the plugin's cache, and everything after that is read from there.
That is not done unless the user says so, with the plugin's 'download'
parameter; until then, the elements are listed and nothing is fetched.

The elements are listed from what the archive is known to hold (ELEMENTS), so
that listing them costs nothing; once it has been read, from what it held.

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
CACHE_VERSION = 2


def _shared():
    """usd_assets.py, beside this script, under a name of this plugin's own."""
    name = "_partcad_openusd_disney_moana_usd_assets"
    if name not in sys.modules:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "usd_assets.py")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


ua = _shared()

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
    """What of the archive is kept: its USD layers and its licence, not its 14 GB of Ptex textures."""
    return name.startswith("island/usd/") or name == "island/License.txt"


class Moana(ua.Source):
    NAME = "openusd-disney-moana"
    TITLE = "Walt Disney Animation Studios"
    URL = PAGE
    DESC = (
        "Walt Disney Animation Studios' Moana Island Scene: the island, and each of its elements, as parts. "
        "Building one reads the 15 GB archive once, when you allow it (see the README)."
    )
    LICENSE = LICENSE
    VERSION = CACHE_VERSION
    REPOSITORY = "partcad-collection-openusd-disney-moana"
    PLUGIN = "//pub/collection/openusd/disney-moana:moana_repo"

    def store(self):
        return ua.TarStore(
            self.http,
            ARCHIVE,
            "the Moana Island Scene's USD archive",
            _kept,
            prefix="island",
            allowed=self.flag("download"),
            refusal=(
                "%s: the Moana Island Scene is one tar archive of %.1f GB, which cannot be read in pieces: building "
                "a part of it reads it all, once, and keeps its USD layers (%.1f GB) in %s. It is licensed for "
                "research and software development only (%s). To let the plugin do that, set its 'download' "
                'parameter, in ~/.partcad/config.yaml:\n\n    parameters:\n      "%s":\n        download: true\n'
                % (self.NAME, ARCHIVE_GB, KEPT_GB, ua.cache_dir(self.NAME), TERMS, self.PLUGIN)
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
