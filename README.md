# The Moana Island Scene for PartCAD

Serves Walt Disney Animation Studios' [Moana Island
Scene](https://www.disneyanimation.com/resources/moana-island-scene/) - one of the "Additional Assets"
[openusd.org's Downloads page](https://openusd.org/release/dl_downloads.html) lists - as PartCAD
parts, published as `//pub/collection/openusd/disney-moana/assets`: the island, and each of its 20
elements, as the scene names them.

| Parts |
| --- |
| `island`, `isBayCedarA1`, `isBeach`, `isCoastline`, `isCoral`, `isDunesA`, `isDunesB`, `isGardeniaA`, `isHibiscus`, `isHibiscusYoung`, `isIronwoodA1`, `isIronwoodB`, `isKava`, `isLavaRocks`, `isMountainA`, `isMountainB`, `isNaupakaA`, `isPalmDead`, `isPalmRig`, `isPandanusA`, `osOcean` |

```shell
pc list parts //pub/collection/openusd/disney-moana/assets
pc search parts -P //pub/collection/openusd/disney-moana/assets -k palm
pc inspect //pub/collection/openusd/disney-moana/assets:isLavaRocks
```

**It is one 15 GB archive, read whole, once - when you say so.** Disney publishes the scene's USD as a
single gzipped tar archive (`island-usd-v2.1.tgz`), and a tar archive cannot be read in pieces. So the
first part built streams all 15 GB of it, keeping its USD layers (3.2 GB) and its licence, and leaving
its 14 GB of Ptex textures; every part after that is read from what was kept. The plugin does not do
that until you allow it, as your own parameter of the plugin, in `~/.partcad/config.yaml`:

```yaml
parameters:
  "//pub/collection/openusd/disney-moana:moana_repo":
    download: true
```

Until then, the elements are listed - from what the archive is known to hold - and nothing is fetched.

**Disney's licence.** The scene is under its own [licence](https://media.disneyanimation.com/uploads/production/data_set_asset/4/asset/License.txt):
for research and software development, benchmarking included, and nothing else; its notice is kept,
and the scene is called the "Moana Island Scene" and nothing else. Read it before you set `download`.

**No geometry is vendored.** Nothing of the scene is in this repository.

**Nothing here is manufacturable.** It is a scene made to be rendered; the package says
`manufacturable: false`, and so does the plugin of what it serves.

Requires PartCAD 0.9.11 or newer: the first release whose `//builtin` has the `usd` part type.
Until it is on PyPI, which has 0.9.2, that is PartCAD's `devel` branch:
`python -m pip install --upgrade git+https://github.com/partcad/partcad.git@devel`, as
[Installing a development version](https://partcad.readthedocs.io/en/latest/contributing.html#installing-a-development-version)
says. An older PartCAD refuses this package, saying which version it needs.

## Listing, searching, building

**Listing and searching download no asset.** `pc list parts` and `pc search parts` are answered from
what the plugin itself knows the scene holds, without a request of any kind. What a part is made of is
fetched when the part is built - by `pc inspect`, `pc render`, `pc export`, or an assembly that places
it - and then only that asset's own files, and kept.

`pc search parts -k <words>` is answered by the plugin, from the same catalog: a part matches when
every word is in its name, its description, its author, its tags or its package's name, and the ones
whose name matches come first. Each package answers with at most 20, or what `--limit` says (up to
100); search a package and every one below it with `-P '<package>...'`.

## What a part is

Every part here is `type: usd`: PartCAD's own part type for OpenUSD, in its `//builtin`, which reads a
stage with Pixar's USD library (`usd-core`), installed into the sandbox that builds the part and
nowhere else. The plugin's part is the file, and the part type makes the shape of it.

- **The file.** A part's file is a USDZ package of the asset, which the plugin makes when the part is
  built. An asset published as a `.usdz` is served as it is. One published as `.usd`, `.usda` or
  `.usdc` layers is a root layer that names others by relative path - sublayers, references,
  payloads - and the plugin follows those names (in a text layer its `@asset paths@`, in a binary one
  its token table) and packs the root and everything it reaches into a USDZ, root first, as the format
  wants it. Of the files that are not layers, it carries what the part's look is read from (the
  plugin's `looks`): the shaders the layers name, and the images of the colour, roughness and
  metalness - a smaller copy of each where the source keeps one, else each of up to 8 MB - and not
  the normal, height or occlusion maps. `textures` carries every one, as it is. A file an asset names
  that the source does not have is reported, and the asset is served without it.
- **The shape.** What is built is the stage's default prim - what a reference to the asset brings
  in - and every mesh it shows: through native instancing and point instancers, of the purpose asked
  for, visible ones only, in millimetres (by the stage's `metersPerUnit`) with Z up. A cube, sphere,
  cylinder, cone or capsule is meshed; curves, points and volumes are not surfaces, and are left out
  and counted. Up to 20 000 triangles, each triangle is a face, and two that share an edge share it:
  the part is drawn as lines, written to STEP, and made the solid it bounds where it closes. Past
  that, the triangles of each look are one mesh: fast at any size, drawn shaded (`pc render --style
  shaded`) and written as STL, 3MF or glTF, but with no edges for a line drawing. The part is a
  surface as USD describes it - what a renderer draws - and PartCAD says so when it is not a solid.
- **The look.** Each face is in the colour, roughness and metalness its material says - the material
  bound to its mesh, or to the subset of its faces - and as see-through: a colour a texture gives is
  read off the image where the face lies on it. `UsdPreviewSurface`, MaterialX and NVIDIA's OmniPBR,
  OmniGlass and OmniSurface are read, an MDL preset calling one included; a face with no material, or
  none of those, is in its `displayColor`. Each is stated under PartCAD's `usd` convention by the
  material's name, so a package of yours can claim one with a colour, material or finish of its own
  (`ids: {usd: Ceramic}`); one nothing claims is drawn as the stage says, and is display-only. A shaded
  `pc render` draws the roughness and metalness too.
- **The size.** It is what the asset says it is, by its `metersPerUnit`: an asset authored in the
  wrong units comes out as large, or as small, as it says.
- **The other reader.** `reader=cadmpeg` reads the same file with cadmpeg instead, PartCAD's
  converter for CAD formats, which reads USD from its 0.6.8 release on: what a render shows, at the
  stage's static values. It does no point instancer (the City Set's buildings, the chess set's pawns
  are left out), no `purpose` but `render` and no `time` but `default`, and a prim it reads as an
  exact surface (a cube, a sphere) is counted and left out rather than meshed. It is there as a
  second opinion and a fallback; `usd-core` is the reader to use.

Every part takes the part type's parameters, in its name (`King;purpose=proxy`) or in the `with` of
an enrich of it:

| Parameter | Default | What it is |
| --- | --- | --- |
| `reader` | `usd-core` | What reads the stage: `usd-core`, or `cadmpeg` (see above). |
| `prim` | the default prim | Build only what is under this prim; `/` is the whole stage. |
| `purpose` | `render` | `render`: what a final render draws; `proxy`: the stand-ins a viewer draws; `all`: both, and guides. |
| `surface` | `auto` | `faces`: a face per triangle; `mesh`: a mesh per colour; `auto`: `faces` up to 20 000 triangles. |
| `time` | `default` | The time code to read animated geometry at, skinning a character to it. `default` is what a viewer opens the stage at (its start time code, else its earliest sample), unposed. |
| `maxTriangles` | 5000000 | The most triangles the part may be: a scene of more is refused, saying so. |

PartCAD's own documentation of the part type is
[the `usd` part type](https://partcad.readthedocs.io/en/latest/config_parts.html#usd-files).

The elements are large, and the island is not something to build into one part: it is billions of
triangles once its instances are placed. An element is built within the default `maxTriangles` where
it can be; `isBeach`, `isMountainB` and `isCoral` are hundreds of MB of geometry, past the default
`max_size_mb`, which the plugin says, and which a `prim` of them, or a larger `max_size_mb` and
`maxTriangles`, gets past.

## Parameters of the plugin

| Parameter | Default | What it is |
| --- | --- | --- |
| `download` | `false` | Whether the plugin may stream the 15 GB archive, once, keeping its USD layers. Nothing is fetched until it may. Yours alone: set it in `~/.partcad/config.yaml`. |
| `textures` | `false` | Whether a part's USDZ carries the textures its layers name. The archive's Ptex textures are not kept, so this changes nothing here. |
| `max_size_mb` | `512` | The most a part's USDZ may be, in MB. |

## Caching

The plugin keeps the island's USD layers under `~/.cache/partcad-openusd-disney-moana/` (or
`$PARTCAD_OPENUSD_DISNEY_MOANA_CACHE`): 3.2 GB, for good. Delete the directory to give the space back;
the next part built reads the archive again. PartCAD keeps every answer the plugin gives, files
included.

Every request says what it is: `User-Agent: Mozilla/5.0 (compatible; PartCAD-openusd-disney-moana/<version>;
+https://github.com/partcad/partcad-collection-openusd-disney-moana)`.

## Status

Run against datasets.disneyanimation.com: without `download`, the elements are listed and searched
with no request at all, and a part is refused, saying why. With it, the archive was streamed once
(15.2 GB, in ten minutes) and its USD layers kept (3.2 GB); `isLavaRocks` was packed from them (115
thousand triangles in 10 colours, 70 by 20 by 4 metres) and read back by the part type with usd-core
26.11, and `isKava` after it in a tenth of a second, without the archive; `isBeach` (711 MB) was
refused for its size, as it should be. The archive's layout - the elements, each opened by its
`element.usda` - was read from the archive itself in November 2026.

Through PartCAD's own `usd` part type, run from the change that adds it to `//builtin` before its
release, with all six `partcad-collection-openusd-*` packages in one workspace: `pc list packages -r`
listed their 17 packages, and `pc search parts -P '<workspace>...'` answered across all of them in
about two seconds, each collection searching its own catalog; `pc info` of `isPalmRig` without
`download` was refused, saying how to allow it.

## Layout

| Path | Purpose |
| --- | --- |
| `partcad.yaml` | `//pub/collection/openusd/disney-moana`; the `assets` external dependency and the `moana_repo` repository that serves it. |
| `moana_repo.py` | The repository plugin: where Disney keeps the island. |
| `usd_assets.py` | What every OpenUSD collection plugin shares, byte for byte: following a USD asset's layers, packing a USDZ, reading remote archives, buckets and listings a piece at a time, searching a catalog, the keys PartCAD asks. |
| `shared.sha256` | The digests of the shared files and their tests. They are the same in every `partcad-collection-openusd-*` repository - pixar, usd-wg, apple, nvidia, aswf-dpel and disney-moana - and a test fails when one here differs: change them in all of them. |
| `test_*.py` | The tests. Nothing in them touches the network. |

```shell
python -m pytest
```

When what the plugin answers changes - here or in `usd_assets.py` - raise `CACHE_VERSION` in
`moana_repo.py`: PartCAD keys everything it caches of this package on it, and an unchanged version
is answered from what it cached before.

## Licensing

The code here is Apache-2.0 (`LICENSE`). The Moana Island Scene is © Disney Enterprises, Inc., under
its own licence (above). Nothing of it is in this repository.
