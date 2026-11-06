#
# PartCAD OpenUSD asset repository plugins, 2026
#
# Licensed under Apache License, Version 2.0.
#
"""What the PartCAD repository plugins for OpenUSD asset libraries have in common.

This file is in every partcad-collection-openusd-* repository, byte for byte;
change it in all of them, and raise each plugin's CACHE_VERSION when what it
answers changes. Each plugin's own script (``<source>_repo.py``) says where its
source keeps its assets; this says what PartCAD is answered.

A part of one of these packages is ``type: usd``: the part type PartCAD itself
ships in ``//builtin/import``, which reads a USD stage with Pixar's usd-core -
or with cadmpeg, when the part's ``reader`` says so - and builds the meshes it
shows. What this module hands it is the part's file, a USDZ package:

- a ``.usdz`` the source publishes is served as it is: a package carries what
  it needs;
- a ``.usd``, ``.usda`` or ``.usdc`` asset is a root layer that names other
  files - sublayers, references, payloads, textures - by relative path. The
  plugin follows those names (in a text layer, its ``@asset paths@``; in a
  binary one, its token table, read here with the standard library alone),
  and packs the root layer and everything it reaches into a USDZ, root first,
  stored and 64-byte aligned as the format requires. What the asset names that
  the source does not have is reported, and the asset is served without it.

Textures are left out unless the plugin's ``textures`` parameter says
otherwise: PartCAD builds a shape from the geometry, and on most assets the
textures are most of the bytes.

**Listing and searching download nothing big.** A package lists its parts
(``objects/part``) and answers a search (``search/part``, ``searchKinds``) from
what its source says is there - a zip archive's directory, read with a range
request or two; a bucket's listing, a folder at a time; a repository's tree; a
gallery's page - or from a catalog the plugin carries, for an archive that
cannot be listed without reading it. An asset's files are fetched only when
the part is built (``files/<id>.usdz``), and only the files it is made of: the
members of an archive it needs, a request each, never the archive.

Nothing here is manufacturable: these are scenes and props made to be looked
at, and the packages and their parts say so.
"""

import base64
import hashlib
import io
import json
import os
import posixpath
import re
import struct
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib

# --- what an asset is made of ---------------------------------------------------

# The layers of a USD asset, by extension. '.usd' is either of the other two,
# told apart by what it starts with.
LAYER_EXTENSIONS = ("usd", "usda", "usdc", "usdz")
# What a layer may name besides other layers. A file of one of these kinds is a
# texture, a shader or a volume: worth carrying for a complete asset, and of no
# use to the geometry PartCAD builds.
TEXTURE_EXTENSIONS = (
    "png", "jpg", "jpeg", "exr", "hdr", "tx", "tex", "tif", "tiff", "tga", "bmp", "ktx", "ktx2", "webp",
    "avif", "rat", "ptex", "ptx", "dds", "mtlx", "mdl", "osl", "oso", "vdb", "ies", "gif",
)  # fmt: skip
# Files a USD layer can reference as geometry through a file format plugin.
GEOMETRY_EXTENSIONS = ("abc", "obj")
DEPENDENCY_EXTENSIONS = LAYER_EXTENSIONS + GEOMETRY_EXTENSIONS + TEXTURE_EXTENSIONS
_DEPENDENCY = re.compile(r"\.(%s)$" % "|".join(DEPENDENCY_EXTENSIONS), re.IGNORECASE)

DAY = 24 * 3600
# What a source's catalog is kept for. Sample assets move rarely, and a stale
# catalog costs a "not found" that 'pc update' clears.
CATALOG_TTL = 7 * DAY

# The most a part's file may be, in MB, unless the plugin's 'max_size_mb' says
# otherwise: the file crosses to PartCAD base64-encoded, in one answer.
DEFAULT_MAX_SIZE_MB = 512


def extension_of(path):
    _, dot, ext = (path or "").rpartition(".")
    return ext.lower() if dot else ""


def is_layer(path):
    return extension_of(path) in LAYER_EXTENSIONS


def is_texture(path):
    return extension_of(path) in TEXTURE_EXTENSIONS


# The files a part's look is read from: the shaders a layer names (an MDL preset
# or a MaterialX document says which textures, and how), and the images a colour, a roughness or a
# metalness is read off - every image Pillow reads, but those whose name says
# they are of something else: the surface's normals or height, its occlusion,
# what it emits, its cut-outs, or a thumbnail of the asset. PartCAD's 'usd'
# part type reads each face's colour off them (see the README).
LOOK_SHADERS = ("mdl", "mtlx")
LOOK_IMAGES = ("png", "jpg", "jpeg", "bmp", "tga", "tif", "tiff", "dds")
_NOT_LOOK = re.compile(
    r"(?:^|[^a-z])(?:normals?|nrm|bump|heights?|disp|displacement|ao|cavity|curvature|emissive|emission|opacity|"
    r"alpha|mask|thumbs?|thumbnails?|cards?|screenshots?|icons?)(?:[^a-z]|$)",
    re.IGNORECASE,
)
# The most a look image may be, in MB, where the source keeps no smaller copy of
# it (see 'Store.stand_in'): a texture is read for a colour per face, and an
# image of tens of megabytes says no more of that than a small one.
LOOK_MAX_MB = 8


# The MDL modules of Omniverse's own library, which a renderer has and no
# source serves: a layer naming one is naming no file of the asset's.
_CORE_MDL = re.compile(r"^(?:.*/)?(?:Omni\w*|nvidia/\w+|core_definitions)\.mdl$")


def is_look(path):
    """Whether the file at 'path' is one a part's look is read from: a shader, or an image of its colour or finish."""
    ext = extension_of(path)
    if ext in LOOK_SHADERS:
        return True
    return ext in LOOK_IMAGES and not _NOT_LOOK.search(re.sub(r"([a-z])([A-Z])", r"\1_\2", path))


# --- LZ4, the way a binary USD layer compresses its tokens --------------------


def lz4_block(src, limit):
    """One LZ4 block, decompressed; 'limit' bounds the output, so a corrupt block cannot run on."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        length = token >> 4
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        dst += src[i : i + length]
        i += length
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(dst):
            raise ValueError("a corrupt LZ4 block")
        length = token & 15
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        start = len(dst) - offset
        if offset >= length:
            dst += dst[start : start + length]
        else:
            # An overlapping match repeats the last 'offset' bytes.
            pattern = bytes(dst[start:])
            dst += (pattern * (length // offset + 1))[:length]
        if len(dst) > limit:
            raise ValueError("an LZ4 block larger than declared")
    return bytes(dst)


def tf_decompress(src, size):
    """What TfFastCompression::DecompressFromBuffer reads: a chunk count, then LZ4 blocks."""
    chunks = src[0]
    if chunks == 0:
        return lz4_block(src[1:], size)
    out, i = bytearray(), 1
    for _ in range(chunks):
        (length,) = struct.unpack_from("<i", src, i)
        i += 4
        out += lz4_block(src[i : i + length], size)
        i += length
    return bytes(out)


def crate_tokens(data):
    """Every token of a binary ('PXR-USDC') layer.

    Its prim and field names, and every string it holds, asset paths included,
    are tokens. Read from the layer's table of contents and its TOKENS section,
    compressed since version 0.4.
    """
    if data[:8] != b"PXR-USDC":
        raise ValueError("not a binary USD layer")
    major, minor = data[8], data[9]
    (toc,) = struct.unpack_from("<q", data, 16)
    (count,) = struct.unpack_from("<Q", data, toc)
    sections = {}
    for k in range(min(count, 64)):
        name, start, size = struct.unpack_from("<16sqq", data, toc + 8 + k * 32)
        sections[name.rstrip(b"\0").decode("ascii", "replace")] = (start, size)
    if "TOKENS" not in sections:
        raise ValueError("a binary USD layer without tokens")
    start, _ = sections["TOKENS"]
    (num,) = struct.unpack_from("<Q", data, start)
    if (major, minor) < (0, 4):
        (size,) = struct.unpack_from("<Q", data, start + 8)
        blob = data[start + 16 : start + 16 + size]
    else:
        size, compressed = struct.unpack_from("<QQ", data, start + 8)
        blob = tf_decompress(data[start + 24 : start + 24 + compressed], size)
    return [t.decode("utf-8", "replace") for t in blob.split(b"\0")[:num]]


# --- what a file names -----------------------------------------------------------

_USDA_ASSET = re.compile(r"@@@(.*?)@@@|@([^@\n]+)@", re.S)
_QUOTED = re.compile(r"\"([^\"\n]{1,512})\"|'([^'\n]{1,512})'")


def layer_kind(data):
    """'usda', 'usdc' or 'usdz' by what a file starts with; None for anything else."""
    if data[:8] == b"PXR-USDC":
        return "usdc"
    if data[:4] == b"PK\x03\x04":
        return "usdz"
    if data[:5] == b"#usda":
        return "usda"
    return None


def named_paths(path, data):
    """The asset paths a file names, as it writes them.

    A text layer's are its '@...@'; a binary layer's are those of its tokens
    that end in an extension an asset is made of. A USDZ names nothing outside
    itself. MaterialX and MDL files name their textures in quotes.
    """
    kind = layer_kind(data)
    if kind == "usdc":
        return [t for t in crate_tokens(data) if "\n" not in t and len(t) < 1024 and _DEPENDENCY.search(t)]
    if kind == "usda":
        text = data.decode("utf-8", "replace")
        return [a or b for a, b in _USDA_ASSET.findall(text)]
    if kind == "usdz":
        return []
    if extension_of(path) in ("mtlx", "mdl", "osl"):
        text = data.decode("utf-8", "replace")
        return [a or b for a, b in _QUOTED.findall(text) if _DEPENDENCY.search(a or b)]
    return []


_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_TILE = re.compile(r"<[^>]*>")


class Tiles:
    """The files a UDIM path names ('textures/body_<UDIM>.png'): a pattern, and the directory it is in.

    The directory is what a store that lists a folder at a time lists; 'match'
    is asked of each path there.
    """

    def __init__(self, path):
        self.directory = posixpath.dirname(path)
        self.regex = re.compile("^" + _TILE.sub(".+?", re.escape(path)) + "$", re.IGNORECASE)

    def match(self, path):
        return self.regex.match(path)


def resolve(anchor, written, exists):
    """The files 'written', named in the file 'anchor', is, in the source's tree.

    A list - a UDIM texture ('<UDIM>') names one file per tile - and empty when
    it names nothing there; None when it is not a path in the tree at all (a
    URL, an absolute path, a variable expression). A path that is neither
    './' nor '../' is a search path, which USD tries beside the layer first;
    so is it tried here, and then from the top of the tree.
    """
    written = written.strip()
    if not written or "${" in written or written.startswith(("/", "`")) or _SCHEME.match(written):
        return None
    written = written.split("[", 1)[0]  # 'package.usdz[inner.usd]' is the package
    candidates = [posixpath.normpath(posixpath.join(posixpath.dirname(anchor), written))]
    if not written.startswith(("./", "../")):
        candidates.append(posixpath.normpath(written))
    for candidate in candidates:
        if candidate.startswith("../") or candidate == "..":
            continue
        if _TILE.search(candidate):
            found = exists(Tiles(candidate))
        else:
            found = [candidate] if exists(candidate) else []
        if found:
            return found
    return []


def _names_others(path):
    return is_layer(path) or extension_of(path) in ("mtlx", "mdl", "osl")


def closure(root, read, exists, textures=True, prefetch=None, size=None, limit=None):
    """(files, missing): what the asset whose root layer is 'root' is made of, root first.

    'read(path)' is a file's bytes; 'exists(path or pattern)' says whether the
    tree has it (for a pattern, the paths that match). 'missing' is
    [(file, written)] for every path an asset names that is not in the tree.

    'textures' says which of the files that are not layers - textures, shaders
    - are carried: every one (True), none (False), or those a function of
    their path says (a part's look, see 'is_look').

    The files are found a generation at a time - what the root names, then what
    those name - and 'prefetch(paths)', when given, is handed each generation's
    layers before they are read, so that a remote store can fetch them at once.

    'size(path)' and 'limit', when given, bound what is read: once the files
    found add up to more than 'limit' bytes, TooLarge is raised before
    anything more is fetched.
    """
    files, missing, seen, generation = [], [], {root}, [root]
    total = 0
    while generation:
        files.extend(generation)
        if size is not None and limit is not None:
            total += sum(size(path) for path in generation)
            if total > limit:
                raise TooLarge(total)
        layers = [path for path in generation if _names_others(path)]
        if prefetch is not None and len(layers) > 1:
            prefetch(layers)
        following = []

        def wanted(f):
            return not is_texture(f) or (textures(f) if callable(textures) else bool(textures))

        for path in layers:
            for written in named_paths(path, read(path)):
                found = resolve(path, written, exists)
                if found is None:
                    continue
                if not found:
                    if wanted(written.split("[", 1)[0]) and not _CORE_MDL.match(written.strip()):
                        missing.append((path, written))
                    continue
                for f in found:
                    if f in seen or not wanted(f):
                        continue
                    seen.add(f)
                    following.append(f)
        generation = following
    return files, missing


# --- USDZ ------------------------------------------------------------------------

_PAD_ID = 0x1986  # the extra field Pixar's own writer pads with


def write_usdz(entries):
    """A USDZ package of [(name, bytes)], the first its root layer: stored, 64-byte aligned.

    Deterministic: one date for every member, members in the order given.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            # The data starts after the local header (30 bytes), the name and
            # the extra field, whose own header is 4 bytes.
            start = buf.tell() + 30 + len(name.encode("utf-8")) + 4
            if len(data) >= 0xFFFFFFFF:
                start += 20  # zip64 sizes, which zipfile writes into the extra field first
            pad = (-start) % 64
            info.extra = struct.pack("<HH", _PAD_ID, pad) + b"\0" * pad
            z.writestr(info, data)
    return buf.getvalue()


def package_root(files):
    """The directory every file of an asset is under, so the package holds them at the paths they had there."""
    dirs = [posixpath.dirname(f).split("/") if posixpath.dirname(f) else [] for f in files]
    common = []
    for parts in zip(*dirs):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    return "/".join(common)


# --- what can go wrong, said the way the user can act on it -------------------


class SourceError(Exception):
    """Something the source said or did, in words that say what to do about it."""


class NotFound(SourceError):
    pass


class Refused(SourceError):
    """The source would not answer an automated client, or not this one."""


class TooLarge(SourceError):
    pass


class NotAccepted(SourceError):
    """The source's licence asks to be accepted before its assets are used, and has not been."""


def warn(message):
    """Say something to the user. PartCAD reports a plugin's stderr, line by line."""
    print(message, file=sys.stderr)


# --- HTTP, and what it is remembered by ------------------------------------------

_RETRIES = 3
_NEGATIVE_TTL = {"missing": DAY, "unavailable": 300, "refused": 300}


def cache_dir(name):
    """$PARTCAD_<NAME>_CACHE, else partcad-<name> under the XDG cache directory."""
    base = os.environ.get("PARTCAD_%s_CACHE" % re.sub(r"\W", "_", name).upper())
    if not base:
        xdg = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        base = os.path.join(xdg, "partcad-" + name)
    return base


class Response:
    def __init__(self, status, headers, body, url):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body or b""
        self.url = url

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body.decode("utf-8"))


def bot_check(response):
    """What turned a request away, if it was a bot check rather than the source; else None.

    These plugins say what they are and do not pretend to be a browser, so a
    host that admits only browsers turns them away. That is reported as what it
    is, with nothing tried to get past it.
    """
    if response.status not in (401, 403, 429, 503):
        return None
    text = response.body[:8192].decode("latin-1", "replace")
    if response.headers.get("cf-mitigated") or "Just a moment..." in text or "/cdn-cgi/challenge-platform/" in text:
        return "a Cloudflare bot check"
    if "Request blocked" in text and "cloudfront" in text.lower():
        return "CloudFront, which blocked the request"
    if "Access Denied" in text and "akamai" in (response.headers.get("server", "") + text).lower():
        return "Akamai, which denied access"
    return None


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but keep credentials on the host they were given for.

    urllib hands every header of a request on to wherever a redirect points,
    and a download is commonly a redirect to a CDN.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for header in ("Authorization", "Cookie"):
                new.remove_header(header)
        return new


_OPENER = urllib.request.build_opener(_SameHostRedirects())


class Http:
    """Requests to a source, with an on-disk cache of what it answered."""

    def __init__(self, name, user_agent):
        self.name = name
        self.user_agent = user_agent
        self.root = cache_dir(name)

    # -- the wire --

    def request(self, url, headers=None, method=None, timeout=120):
        """One request, retried on what retrying can help; the Response whatever its status."""
        all_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        all_headers.update(headers or {})
        last = None
        for attempt in range(_RETRIES):
            delay = 1.0 * (attempt + 1)
            try:
                req = urllib.request.Request(url, headers=all_headers, method=method)
                with _OPENER.open(req, timeout=timeout) as answer:
                    return Response(answer.status, dict(answer.headers), answer.read(), answer.geturl())
            except urllib.error.HTTPError as e:
                last = Response(e.code, dict(e.headers or {}), e.read() if e.fp else b"", url)
                if e.code == 429:
                    delay = max(delay, _retry_after(e.headers, delay))
                elif e.code < 500 or bot_check(last):
                    return last
            except Exception as e:  # a timeout, a reset, a name that did not resolve
                last = Response(0, {}, str(e).encode("utf-8"), url)
            time.sleep(delay)
        return last

    def checked(self, response, what):
        """'response' if it is a 2xx; otherwise the SourceError that says why not."""
        if 200 <= response.status < 300:
            return response
        refused_by = bot_check(response)
        if refused_by:
            raise Refused(
                "%s: %s was turned away by %s. This plugin does not pretend to be a browser; "
                "try again later or from another network." % (self.name, what, refused_by)
            )
        if response.status == 404:
            raise NotFound("%s: %s: not found (%s)" % (self.name, what, response.url))
        if response.status == 0:
            raise SourceError("%s: %s could not be fetched: %s" % (self.name, what, response.text()[:200]))
        raise SourceError("%s: %s: HTTP %d (%s)" % (self.name, what, response.status, response.url))

    # -- the cache --

    def path(self, kind, key):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return os.path.join(self.root, kind, digest[:2], digest)

    def remember(self, kind, key, content):
        path = self.path(kind, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = path + ".%d.tmp" % os.getpid()
        with open(temporary, "wb") as f:
            f.write(content)
        os.replace(temporary, path)
        return path

    def recall(self, kind, key, ttl):
        """The bytes kept under 'key' if younger than 'ttl' seconds (None: forever); else None."""
        path = self.path(kind, key)
        try:
            if ttl is None or time.time() - os.path.getmtime(path) < ttl:
                with open(path, "rb") as f:
                    return f.read()
        except OSError:
            pass
        return None

    def _negative(self, key):
        path = self.path("missing", key)
        try:
            with open(path, "r", encoding="utf-8") as f:
                reason, message = f.read().split("\n", 1)
            if time.time() - os.path.getmtime(path) < _NEGATIVE_TTL.get(reason, 0):
                return reason, message
        except (OSError, ValueError):
            pass
        return None

    def get(self, url, what, ttl=CATALOG_TTL, headers=None, stale=False):
        """The bytes at 'url', from the cache when younger than 'ttl' (None: forever; 0: never cached).

        A fetch that came to nothing is remembered too, for a while, so that a
        run that was refused or throttled is not the same burst again. With
        'stale', a fetch that came to nothing is answered from the cache
        however old, when there is anything there, and said to be: a catalog
        a week old is better than none while an API is rate limiting.
        """
        key = "GET %s %s" % (
            url,
            json.dumps({k: v for k, v in (headers or {}).items() if k != "Authorization"}, sort_keys=True),
        )
        if ttl != 0:
            kept = self.recall("body", key, ttl)
            if kept is not None:
                return kept
        try:
            negative = self._negative(key)
            if negative is not None:
                reason, message = negative
                raise {"missing": NotFound, "refused": Refused}.get(reason, SourceError)(message)
            response = self.request(url, headers=headers)
            try:
                self.checked(response, what)
            except SourceError as e:
                reason = {NotFound: "missing", Refused: "refused"}.get(type(e), "unavailable")
                self.remember("missing", key, (reason + "\n" + str(e)).encode("utf-8"))
                raise
        except SourceError as e:
            kept = self.recall("body", key, None) if stale else None
            if kept is None:
                raise
            warn("%s: %s; answering from what was kept from an earlier run" % (self.name, e))
            return kept
        if ttl != 0:
            self.remember("body", key, response.body)
        return response.body

    def json(self, url, what, ttl=CATALOG_TTL, headers=None, stale=False):
        data = self.get(url, what, ttl=ttl, headers=headers, stale=stale)
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:
            raise SourceError("%s: %s did not answer with JSON" % (self.name, what))

    def head(self, url, what):
        """(size, accepts ranges, validator) of 'url', asked with a one-byte range.

        A range request rather than HEAD: some hosts answer HEAD differently
        from GET, and a 206 is the proof that ranges work.
        """
        response = self.request(url, headers={"Range": "bytes=0-0"})
        self.checked(response, what)
        validator = response.headers.get("etag") or response.headers.get("last-modified") or ""
        if response.status == 206:
            total = response.headers.get("content-range", "").rpartition("/")[2]
            if total.isdigit():
                return int(total), True, validator
        size = response.headers.get("content-length")
        return (int(size) if size and size.isdigit() else len(response.body)), False, validator

    def range(self, url, start, end, what):
        """Bytes 'start' to 'end' (exclusive) of 'url'."""
        response = self.request(url, headers={"Range": "bytes=%d-%d" % (start, end - 1)})
        self.checked(response, what)
        if response.status != 206:
            raise SourceError("%s: %s: the server did not answer a range request with a range" % (self.name, what))
        return response.body

    def download(self, url, target, what, timeout=3600):
        """'url' streamed into the file 'target', for an archive too large to hold in memory."""
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temporary = target + ".%d.tmp" % os.getpid()
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as answer, open(temporary, "wb") as f:
                while True:
                    chunk = answer.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(temporary, target)
        except urllib.error.HTTPError as e:
            self.checked(Response(e.code, dict(e.headers or {}), e.read() if e.fp else b"", url), what)
            raise
        except OSError as e:
            raise SourceError("%s: %s could not be fetched: %s" % (self.name, what, e))
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)
        return target


def _retry_after(headers, default):
    value = headers.get("Retry-After") if headers else None
    try:
        return min(float(value), 60.0) if value else default
    except (TypeError, ValueError):
        return default


def credential(request, parameter, environment):
    """A credential: the plugin's parameter (which the user's config may set), else the environment.

    Never written into anything a plugin answers: PartCAD caches the answers,
    and a cache can be shared.
    """
    value = ((request or {}).get("parameters") or {}).get(parameter) or os.environ.get(environment) or ""
    return str(value).strip()


# --- where an asset's files are ----------------------------------------------------


class Store:
    """A tree of files: what is in it, and each one's bytes."""

    def listing(self):
        """{path: size}, every file in the tree."""
        raise NotImplementedError

    def read(self, path):
        raise NotImplementedError

    def size(self, path):
        """The size of one file, in bytes; 0 when the store does not say."""
        return self.listing().get(path, 0)

    def exists(self, path_or_pattern):
        """Whether the tree has 'path_or_pattern'; for a pattern ('Tiles'), the paths that match it."""
        listing = self.listing()
        if isinstance(path_or_pattern, str):
            return path_or_pattern in listing
        return sorted(p for p in listing if path_or_pattern.match(p))

    def stand_in(self, path):
        """A smaller copy of the image at 'path' the tree keeps beside it, or None.

        NVIDIA's Omniverse keeps a 256-pixel picture of every file it serves,
        textures included, at '.thumbs/256x256/<name>.png' (or '.auto.png')
        beside it: a texture of 28 MB is one of 90 KB there, which says as much
        of the colour of each face as the whole of it.
        """
        if extension_of(path) not in LOOK_IMAGES:
            return None
        folder, name = posixpath.split(path)
        for candidate in ("%s.png" % name, "%s.auto.png" % name):
            smaller = posixpath.join(folder, ".thumbs", "256x256", candidate)
            if self.exists(smaller):
                return smaller
        return None


class MemoryStore(Store):
    def __init__(self, files):
        self.files = dict(files)

    def listing(self):
        return {path: len(data) for path, data in self.files.items()}

    def read(self, path):
        return self.files[path]


# How many requests a store has in flight at once when it fetches a
# generation of an asset's files.
WORKERS = 8


def _parallel(function, items):
    """'function' of every item, a few at a time; the first exception raised, after all have run."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for future in [pool.submit(function, item) for item in items]:
            future.result()


class HttpStore(Store):
    """Files at '<base><path>', each fetched when it is first needed and kept.

    'listing' is what the source says is there - a GitHub tree, a bucket's
    listing - since a web server lists nothing by itself.
    """

    def __init__(self, http, base, listing, quote=True):
        self.http = http
        self.base = base
        self._listing = dict(listing)
        self.quote = quote

    def listing(self):
        return self._listing

    def url(self, path):
        return self.base + (urllib.parse.quote(path) if self.quote else path)

    def read(self, path):
        if path not in self._listing:
            raise NotFound("%s: %s is not in the source" % (self.http.name, path))
        return self.http.get(self.url(path), path, ttl=None)

    def prefetch(self, paths):
        _parallel(self.read, [p for p in paths if p in self._listing])


class _RangeFile(io.RawIOBase):
    """A remote file read with range requests, for zipfile to seek about in."""

    def __init__(self, http, url, size, what, block=1 << 20):
        self.http, self.url, self.size, self.what, self.block = http, url, size, what, block
        self.pos = 0
        self._cache = {}

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=io.SEEK_SET):
        self.pos = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence] + offset
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if n == 0:
            return b""
        out = bytearray()
        while n > 0:
            index = self.pos // self.block
            chunk = self._cache.get(index)
            if chunk is None:
                start = index * self.block
                chunk = self.http.range(self.url, start, min(start + self.block, self.size), self.what)
                self._cache = {index: chunk}  # one block: a directory is read front to back
            at = self.pos - index * self.block
            piece = chunk[at : at + n]
            if not piece:
                raise SourceError("%s: %s ended early" % (self.http.name, self.what))
            out += piece
            self.pos += len(piece)
            n -= len(piece)
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


# The largest archive whose server takes no range requests that is downloaded
# to be listed: listing a larger one would mean downloading it, and a listing
# downloads nothing big. Building an asset of it downloads it whole, once.
LIST_WHOLE_MB = 8
# The slack fetched after a member's name, for its local header's extra field,
# which need not be the length its central directory entry says.
_EXTRA_SLACK = 1024


class ZipStore(Store):
    """The members of a zip archive at 'url', under 'prefix' (a folder of the archive, or '').

    Where its server takes range requests - every archive these plugins read
    - its directory is read with a range request or two and kept, and each
    member an asset needs is fetched with one request when the asset is built,
    and kept from then on: the archive itself is never downloaded. Where it
    does not, the archive is downloaded whole, once, into the plugin's cache,
    when an asset of it is built; it is listed that way only when it is no
    larger than LIST_WHOLE_MB. 'whole_mb' downloads an archive that small whole
    even when ranges work. Either way it is read again only when the server
    says it changed.
    """

    def __init__(self, http, url, what, prefix="", max_download_mb=4096, whole_mb=0):
        self.http, self.url, self.what = http, url, what
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.max_download_mb = max_download_mb
        self.whole_mb = whole_mb
        self._state = None
        self._listing = None

    def _probe(self, listing=False):
        if self._state is None:
            key = "ZIP %s" % self.url
            kept = self.http.recall("zip", key, CATALOG_TTL)
            if kept is not None:
                self._state = json.loads(kept.decode("utf-8"))
            else:
                size, ranges, validator = self.http.head(self.url, self.what)
                if listing and not ranges and size > max(LIST_WHOLE_MB, self.whole_mb) * (1 << 20):
                    raise TooLarge(
                        "%s: %s is %d MB and its server takes no range requests, so it cannot be listed without "
                        "being downloaded; build one of its assets to download it" % (self.http.name, self.what,
                                                                                      size >> 20)
                    )  # fmt: skip
                state = {"size": size, "ranges": ranges, "validator": validator}
                state["whole"] = not ranges or size <= self.whole_mb * (1 << 20)
                self._state = state
                if state["whole"]:
                    source = self._local()
                else:
                    source = _RangeFile(self.http, self.url, size, self.what, block=1 << 16)
                with zipfile.ZipFile(source) as z:
                    state["members"] = {
                        i.filename: [i.file_size, i.compress_size, i.header_offset, i.compress_type, i.CRC]
                        for i in z.infolist()
                        if not i.is_dir()
                    }
                self.http.remember("zip", key, json.dumps(state).encode("utf-8"))
        return self._state

    def _local(self):
        """The archive, downloaded whole into the cache."""
        state = self._state
        target = self.http.path("archive", "%s %s" % (self.url, state.get("validator", "")))
        if not os.path.exists(target):
            if state["size"] > self.max_download_mb * (1 << 20):
                raise TooLarge(
                    "%s: %s is %d MB, and its server does not take range requests: more than the %d MB "
                    "this plugin downloads whole"
                    % (self.http.name, self.what, state["size"] >> 20, self.max_download_mb)
                )
            if state["size"] > max(LIST_WHOLE_MB, self.whole_mb) * (1 << 20):
                warn("%s: downloading %s (%d MB), once" % (self.http.name, self.what, state["size"] >> 20))
            self.http.download(self.url, target, self.what)
        return target

    def listing(self):
        if self._listing is None:
            members = self._probe(listing=True)["members"]
            self._listing = {
                name[len(self.prefix) :]: entry[0] for name, entry in members.items() if name.startswith(self.prefix)
            }
        return self._listing

    def _key(self, name):
        return "MEMBER %s %s %s" % (self.url, self._state.get("validator", ""), name)

    def read(self, path):
        state = self._probe()
        name = self.prefix + path
        if name not in state["members"]:
            raise NotFound("%s: %s has no %s" % (self.http.name, self.what, name))
        if state["whole"]:
            with zipfile.ZipFile(self._local()) as z:
                return z.read(name)
        kept = self.http.recall("member", self._key(name), None)
        if kept is not None:
            return kept
        data = self._fetch(name)
        self.http.remember("member", self._key(name), data)
        return data

    def _fetch(self, name):
        """One member, with one range request: its local header, its name, and its data."""
        size, compressed, offset, method, crc = self._state["members"][name]
        encoded = name.encode("utf-8")
        what = "%s of %s" % (name, self.what)
        end = min(offset + 30 + len(encoded) + _EXTRA_SLACK + compressed, self._state["size"])
        chunk = self.http.range(self.url, offset, end, what)
        if chunk[:4] != b"PK\x03\x04":
            raise SourceError("%s: %s is not where the archive's directory says" % (self.http.name, what))
        name_length, extra_length = struct.unpack_from("<HH", chunk, 26)
        start = 30 + name_length + extra_length
        if start + compressed > len(chunk):
            chunk = self.http.range(self.url, offset, offset + start + compressed, what)
        raw = chunk[start : start + compressed]
        if method == zipfile.ZIP_STORED:
            data = raw
        elif method == zipfile.ZIP_DEFLATED:
            data = zlib.decompressobj(-15).decompress(raw)
        else:
            # Anything else zipfile knows how to read, it reads, from the range.
            with zipfile.ZipFile(_RangeFile(self.http, self.url, self._state["size"], what)) as z:
                return z.read(name)
        if len(data) != size or zlib.crc32(data) != crc:
            raise SourceError("%s: %s did not arrive whole" % (self.http.name, what))
        return data

    def prefetch(self, paths):
        state = self._probe()
        if state["whole"]:
            self._local()
            return
        wanted = [
            path
            for path in paths
            if self.prefix + path in state["members"]
            and not os.path.exists(self.http.path("member", self._key(self.prefix + path)))
        ]
        _parallel(self.read, wanted)


class DirStore(Store):
    """A tree of files on this machine."""

    def __init__(self, root):
        self.root = root
        self._listing = None

    def listing(self):
        if self._listing is None:
            self._listing = {}
            for folder, _, names in os.walk(self.root):
                for name in names:
                    full = os.path.join(folder, name)
                    path = os.path.relpath(full, self.root).replace(os.sep, "/")
                    if not path.startswith("."):
                        self._listing[path] = os.path.getsize(full)
        return self._listing

    def read(self, path):
        if path not in self.listing():
            raise NotFound("%s is not in %s" % (path, self.root))
        with open(os.path.join(self.root, *path.split("/")), "rb") as f:
            return f.read()


class TarStore(Store):
    """The members of a gzipped tar archive at 'url' that 'keep(name)' wants, under 'prefix'.

    A tar archive cannot be read at random, so it is streamed once, whole, and
    the members kept are written into the plugin's cache as it goes; nothing
    else of it is kept. That is the archive's whole size downloaded, once, and
    is only ever done when 'allowed' says so - which a plugin asks its user for.
    """

    def __init__(self, http, url, what, keep, prefix="", allowed=False, refusal=None):
        self.http, self.url, self.what, self.keep = http, url, what, keep
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.allowed = allowed
        self.refusal = refusal or "%s: %s is a tar archive, read whole" % (http.name, what)
        self._store = None

    def directory(self):
        return self.http.path("tar", "%s %s" % (self.url, self.prefix))

    def ready(self):
        return os.path.exists(os.path.join(self.directory(), ".complete"))

    def _unpacked(self):
        if self._store is None:
            root = self.directory()
            if not self.ready():
                if not self.allowed:
                    raise NotAccepted(self.refusal)
                self._unpack(root)
            self._store = DirStore(os.path.join(root, *self.prefix.strip("/").split("/")) if self.prefix else root)
        return self._store

    def _unpack(self, root):
        import shutil
        import tarfile

        partial = root + ".%d.partial" % os.getpid()
        shutil.rmtree(partial, ignore_errors=True)
        warn("%s: reading %s, once, for the files it keeps; this is the whole archive" % (self.http.name, self.what))
        request = urllib.request.Request(self.url, headers={"User-Agent": self.http.user_agent, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(request, timeout=600) as answer:
                with tarfile.open(fileobj=answer, mode="r|gz") as archive:
                    for member in archive:
                        name = member.name.lstrip("./")
                        parts = name.split("/")
                        if not member.isfile() or ".." in parts or not self.keep(name):
                            continue
                        target = os.path.join(partial, *parts)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        source = archive.extractfile(member)
                        with open(target, "wb") as f:
                            shutil.copyfileobj(source, f, 1 << 20)
        except urllib.error.HTTPError as e:
            self.http.checked(Response(e.code, dict(e.headers or {}), b"", self.url), self.what)
            raise
        except (OSError, tarfile.TarError) as e:
            shutil.rmtree(partial, ignore_errors=True)
            raise SourceError("%s: %s could not be read: %s" % (self.http.name, self.what, e))
        with open(os.path.join(partial, ".complete"), "w") as f:
            f.write(self.url)
        if os.path.exists(root):
            shutil.rmtree(root, ignore_errors=True)
        os.replace(partial, root)

    def listing(self):
        return self._unpacked().listing()

    def read(self, path):
        return self._unpacked().read(path)


def s3_listing(http, bucket, prefix, what, ttl=CATALOG_TTL):
    """{key: size} of every object under 'prefix' in a publicly listable S3 bucket at 'bucket' (its URL, ending '/').

    Read a thousand keys a request, as S3 pages them, and kept as one answer for
    'ttl'; a listing that cannot be refreshed is answered from an older one.
    """
    key = "S3 %s %s" % (bucket, prefix)
    kept = http.recall("listing", key, ttl)
    if kept is not None:
        return json.loads(kept.decode("utf-8"))
    listing, token = {}, None
    try:
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            page = http.get(bucket + "?" + urllib.parse.urlencode(query), what, ttl=0).decode("utf-8", "replace")
            for name, size in re.findall(r"<Key>([^<]*)</Key>.*?<Size>(\d+)</Size>", page, re.S):
                listing[_xml_text(name)] = int(size)
            found = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", page)
            if not found or "<IsTruncated>true</IsTruncated>" not in page:
                break
            token = _xml_text(found.group(1))
    except SourceError as e:
        kept = http.recall("listing", key, None)
        if kept is None:
            raise
        warn("%s: %s; answering from the listing kept from an earlier run" % (http.name, e))
        return json.loads(kept.decode("utf-8"))
    http.remember("listing", key, json.dumps(listing).encode("utf-8"))
    return listing


def _xml_text(text):
    return (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
    )


class S3Store(Store):
    """A publicly listable S3 bucket at 'bucket' (its URL, ending '/'), read a folder at a time.

    The tree is the whole bucket, so a layer that names a file in another folder
    finds it where it is. Nothing lists the bucket whole: a folder's files and
    subfolders are one listing ('delimiter=/'), asked for when something in it
    is, and kept for a week - which is what lets a package of a thousand assets
    list them without reading the tens of thousands of textures beside them.
    """

    def __init__(self, http, bucket):
        self.http = http
        self.bucket = bucket
        self._folders = {}

    def folder(self, directory):
        """({path: size} of the files in 'directory', [its subfolders' names]): one listing, a page at a time."""
        prefix = directory.strip("/") + "/" if directory.strip("/") else ""
        if prefix not in self._folders:
            key = "S3 %s %s /" % (self.bucket, prefix)
            kept = self.http.recall("listing", key, CATALOG_TTL)
            if kept is None:
                try:
                    files, folders = self._list(prefix)
                    self.http.remember("listing", key, json.dumps([files, folders]).encode("utf-8"))
                except SourceError as e:
                    kept = self.http.recall("listing", key, None)
                    if kept is None:
                        raise
                    warn("%s: %s; answering from the listing kept from an earlier run" % (self.http.name, e))
            if kept is not None:
                files, folders = json.loads(kept.decode("utf-8"))
            self._folders[prefix] = (files, folders)
        return self._folders[prefix]

    def _list(self, prefix):
        files, folders, token = {}, [], None
        while True:
            query = {"list-type": "2", "prefix": prefix, "delimiter": "/"}
            if token:
                query["continuation-token"] = token
            page = self.http.get(self.bucket + "?" + urllib.parse.urlencode(query), "the listing of %s" % (prefix or
                                 "the bucket"), ttl=0).decode("utf-8", "replace")  # fmt: skip
            for name, size in re.findall(r"<Key>([^<]*)</Key>.*?<Size>(\d+)</Size>", page, re.S):
                name = _xml_text(name)
                if name != prefix:
                    files[name] = int(size)
            for sub in re.findall(r"<CommonPrefixes>\s*<Prefix>([^<]*)</Prefix>", page):
                folders.append(_xml_text(sub)[len(prefix) :].rstrip("/"))
            found = re.search(r"<NextContinuationToken>([^<]*)</NextContinuationToken>", page)
            if not found or "<IsTruncated>true</IsTruncated>" not in page:
                return files, folders
            token = _xml_text(found.group(1))

    def walk(self, directory, skip=lambda name: False):
        """{path: size} of every file under 'directory', not descending into a folder 'skip(name)' is true of."""
        found, pending = {}, [directory.strip("/")]
        while pending:
            listed = {}

            def one(folder):
                listed[folder] = self.folder(folder)

            _parallel(one, pending)
            following = []
            for folder in pending:
                files, folders = listed[folder]
                found.update(files)
                following += [(folder + "/" if folder else "") + sub for sub in folders if not skip(sub)]
            pending = following
        return found

    def listing(self):
        raise SourceError("%s: a bucket is listed a folder at a time, not whole" % self.http.name)

    def size(self, path):
        return self.folder(posixpath.dirname(path))[0].get(path, 0)

    def exists(self, path_or_pattern):
        if isinstance(path_or_pattern, str):
            return path_or_pattern in self.folder(posixpath.dirname(path_or_pattern))[0]
        files = self.folder(path_or_pattern.directory)[0]
        return sorted(p for p in files if path_or_pattern.match(p))

    def read(self, path):
        if not self.exists(path):
            raise NotFound("%s: %s is not in the bucket" % (self.http.name, path))
        return self.http.get(self.bucket + urllib.parse.quote(path), path, ttl=None)

    def prefetch(self, paths):
        _parallel(self.read, [p for p in paths if self.exists(p)])


# --- the catalog --------------------------------------------------------------------

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_id(text):
    """A part name from anything: what PartCAD and a path both take."""
    return _SAFE_ID.sub("_", text or "").strip("_.") or "asset"


class Asset:
    """One asset of a source: a root layer in a store, and what to say about it."""

    def __init__(self, id, store, root, title=None, desc=None, url=None, license=None, author=None, tags=None):
        self.id = safe_id(id)
        self.store = store
        self.root = root
        self.title = title
        self.desc = desc
        self.url = url
        self.license = license
        self.author = author
        self.tags = list(tags or [])

    def __repr__(self):
        return "Asset(%r, %r)" % (self.id, self.root)


class Collection:
    """A child package of the source's: a set of assets, published together."""

    def __init__(self, name, desc=None, url=None):
        self.name = name
        self.desc = desc
        self.url = url


# The parameters of PartCAD's 'usd' part type, which every part here declares
# so that they can be set - in its name, or in the 'with' of an enrich of it -
# and so that 'pc info' shows them. The defaults are the part type's own.
READER_PARAMETERS = {
    "reader": {
        "type": "string",
        "default": "usd-core",
        "enum": ["usd-core", "cadmpeg"],
        "desc": "What reads the stage: Pixar's usd-core ('usd-core'), or cadmpeg ('cadmpeg'), which reads what a "
        "render shows at the stage's static values, and no point instancer.",
    },
    "prim": {
        "type": "string",
        "default": "",
        "desc": "The prim to build, by its path ('/Kitchen_set/Props_grp'): only what is under it. Empty is the "
        "stage's default prim - what a reference to the asset brings in - or the whole stage when it names none; "
        "'/' is the whole stage.",
    },
    "purpose": {
        "type": "string",
        "default": "render",
        "enum": ["render", "proxy", "all"],
        "desc": "Which geometry: what is drawn in a final render ('render', with geometry of no purpose), "
        "the stand-ins an interactive viewer draws ('proxy', likewise), or both, guides included ('all').",
    },
    "maxTriangles": {
        "type": "int",
        "default": 5000000,
        "desc": "The most triangles the part may be built of: a scene of more is refused, saying so, rather "
        "than building something PartCAD cannot draw. Raise it, or build a 'prim' of the scene.",
    },
    "surface": {
        "type": "string",
        "default": "auto",
        "enum": ["auto", "faces", "mesh"],
        "desc": "How the triangles become a shape: a face per triangle, sharing its edges ('faces': drawn as lines, "
        "written to STEP, made a solid where closed - and slow past tens of thousands), or a mesh per colour "
        "('mesh': fast at any size, drawn shaded - 'pc render --style shaded' - and written as STL, 3MF or glTF). "
        "'auto' is 'faces' up to 20000 triangles.",
    },
    "time": {
        "type": "string",
        "default": "default",
        "desc": "The time code to read animated geometry at, posing a skinned character to it; 'default' is "
        "what a viewer opens the stage at (its start time code, else its earliest sample), unposed.",
    },
}


# How many hits a search answers with when it is not told, and the most it
# answers with when it is.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


# --- a source, as a plugin serves it ------------------------------------------------


class Source:
    """An OpenUSD asset library, as a plugin serves it. Each plugin's script makes one."""

    NAME = None  # 'openusd-pixar': its cache directory, its User-Agent, its messages
    TITLE = None  # 'Pixar'
    URL = None
    DESC = None
    LICENSE = None  # what applies when an asset says nothing of its own
    VERSION = 1  # the plugin's CACHE_VERSION, which its User-Agent carries
    REPOSITORY = None  # 'partcad-collection-openusd-pixar'
    # A licence the source puts behind an "OK" button, which has to be accepted
    # before an asset is fetched: {"name": ..., "url": ..., "terms": ...}. The
    # user accepts it by setting the plugin's 'accept_license' parameter, as
    # their own: never in a package.
    LICENSE_GATE = None
    # The plugin's name, as the user sets its parameters: '//pub/...:<plugin>'.
    PLUGIN = None

    def __init__(self, request=None):
        self.request = request or {}
        self.parameters = (self.request.get("parameters") or {}) if isinstance(self.request, dict) else {}
        self.http = Http(
            self.NAME,
            "Mozilla/5.0 (compatible; PartCAD-%s/%s; +https://github.com/partcad/%s)"
            % (self.NAME, self.VERSION, self.REPOSITORY or "partcad-collection-" + self.NAME),
        )

    # -- what a source implements --

    def collections(self):
        """{name: Collection}: the child packages. Empty when the source is one flat set of assets."""
        return {}

    def assets(self, collection):
        """{id: Asset} of a collection ('' when there are none), in the order to list them."""
        raise NotImplementedError

    def unavailable(self, collection):
        """Why a collection cannot be served from here, when that is so (too large, behind a sign-in); else None."""
        return None

    # -- the plugin's own parameters --

    def flag(self, name, default=False):
        value = self.parameters.get(name, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def number(self, name, default):
        try:
            return float(self.parameters.get(name, default))
        except (TypeError, ValueError):
            raise SourceError("%s: '%s' is a number, not '%s'" % (self.NAME, name, self.parameters.get(name)))

    # -- what PartCAD is answered --

    def meta(self, collection=None):
        if collection is None:
            desc, url = self.DESC, self.URL
            kinds = [] if self.collections() else ["part"]
        else:
            found = self.collections().get(collection)
            desc = (found.desc if found else None) or "%s: %s" % (self.TITLE, collection)
            url = (found.url if found else None) or self.URL
            kinds = ["part"]
        return {
            "desc": desc,
            "url": url,
            # Scenes and props made to be looked at: nothing in them says they
            # are made to be manufactured, or what of.
            "manufacturable": False,
            "objectKinds": kinds,
            # The package searches its parts itself, from the same listing it
            # lists them from: nothing is fetched to answer a search.
            "searchKinds": kinds,
        }

    def file_name(self, asset):
        """The name of an asset's USDZ: another for each set of files it can be made of, so none is taken for another."""
        if self.flag("textures"):
            return "%s.textured.usdz" % asset.id
        return "%s%s.usdz" % (asset.id, "" if self.flag("looks", True) else ".bare")

    def part_config(self, asset):
        config = {
            "type": "usd",
            "path": self.file_name(asset),
            "desc": asset.desc or asset.title or "%s asset %s" % (self.TITLE, asset.id),
            "parameters": {name: dict(spec) for name, spec in READER_PARAMETERS.items()},
        }
        if asset.url:
            config["url"] = asset.url
        if asset.license or self.LICENSE:
            config["license"] = asset.license or self.LICENSE
        if asset.author:
            config["author"] = asset.author
        if asset.tags:
            config["tags"] = list(asset.tags)
        return config

    def _assets(self, collection):
        reason = self.unavailable(collection)
        if reason:
            raise SourceError("%s: %s" % (self.NAME, reason))
        return self.assets(collection)

    def package(self, asset):
        """The bytes of an asset's USDZ: itself if it is one, else its root layer and everything it reaches."""
        limit = int(self.number("max_size_mb", DEFAULT_MAX_SIZE_MB) * (1 << 20))
        if extension_of(asset.root) == "usdz":
            size = asset.store.size(asset.root)
            if size > limit:
                raise TooLarge(self._too_large(asset, size, limit))
            return asset.store.read(asset.root)
        textures = self.flag("textures")
        # The files a part's look is read from are carried unless 'looks' says
        # not: each image as a smaller copy where the source keeps one, and
        # otherwise as it is where it is no larger than LOOK_MAX_MB.
        stand_ins = {}

        def look(path):
            if not is_look(path):
                return False
            smaller = asset.store.stand_in(path)
            if smaller is not None:
                stand_ins[path] = smaller
                return True
            return extension_of(path) in LOOK_SHADERS or asset.store.size(path) <= LOOK_MAX_MB * (1 << 20)

        carried = True if textures else (look if self.flag("looks", True) else False)

        def size(path):
            return asset.store.size(stand_ins.get(path, path))

        # Refused before it is fetched, by the sizes the source lists: a layer
        # of a gigabyte is not downloaded to be told it is too large.
        try:
            files, missing = closure(
                asset.root,
                asset.store.read,
                asset.store.exists,
                textures=carried,
                prefetch=getattr(asset.store, "prefetch", None),
                size=size,
                limit=limit,
            )
        except TooLarge as e:
            raise TooLarge(self._too_large(asset, e.args[0] if e.args and isinstance(e.args[0], int) else 0, limit))
        if missing:
            shown = ", ".join("%s (in %s)" % (written, path) for path, written in missing[:5])
            warn(
                "%s: %s names %d file%s the source does not have, and is served without: %s%s"
                % (self.NAME, asset.id, len(missing), "" if len(missing) == 1 else "s", shown,
                   "" if len(missing) <= 5 else ", ...")
            )  # fmt: skip
        prefetch = getattr(asset.store, "prefetch", None)
        if prefetch is not None:
            prefetch([stand_ins.get(f, f) for f in files])
        base = package_root(files)
        # A stand-in is carried under the name of the image it stands in for, so
        # that the layers that name the image find it.
        inner = [(f[len(base) + 1 :] if base else f, asset.store.read(stand_ins.get(f, f))) for f in files]
        return write_usdz(inner)

    def _too_large(self, asset, size, limit):
        return (
            "%s: %s is %s MB%s, more than the %d MB a part's file may be here. Raise the plugin's 'max_size_mb' "
            "to take it (see the README)"
            % (
                self.NAME,
                asset.id,
                ("%d" % (size >> 20)) if size else "more than %d" % (limit >> 20),
                " with its textures" if self.flag("textures") else "",
                limit >> 20,
            )
        )

    def check_license(self):
        gate = self.LICENSE_GATE
        if gate and not self.flag("accept_license"):
            raise NotAccepted(
                "%s: %s's assets are licensed under the %s: %s. Read it at %s; if you accept it, say so as "
                "your own parameter of the plugin, in ~/.partcad/config.yaml:\n\n"
                '    parameters:\n      "%s":\n        accept_license: true\n'
                % (self.NAME, self.TITLE, gate["name"], gate["terms"], gate["url"], self.PLUGIN or self.NAME)
            )

    def search(self, collection, query):
        """'search/part?<query>' answered: {name: config}, best first, from the listing alone.

        Every word asked for has to be in a part's name, description, author,
        tags or package; a word in its name ranks it first.
        """
        fields = urllib.parse.parse_qs(query)
        words = [w for w in re.split(r"[^0-9a-z]+", " ".join(fields.get("q", [])).lower()) if w]
        try:
            limit = int((fields.get("limit") or ["0"])[0] or 0)
        except ValueError:
            limit = 0
        limit = min(limit or DEFAULT_LIMIT, MAX_LIMIT)
        if not words:
            return {}
        ranked = []
        for order, asset in enumerate(self._assets(collection).values()):
            name = re.sub(r"[^0-9a-z]+", " ", asset.id.lower()) + " " + asset.id.lower()
            text = " ".join(
                [name, (asset.desc or "").lower(), (asset.title or "").lower(), (asset.author or "").lower(),
                 " ".join(asset.tags).lower(), collection or ""]
            )  # fmt: skip
            if all(w in text for w in words):
                ranked.append((-sum(w in name for w in words), order, asset))
        ranked.sort(key=lambda entry: entry[:2])
        return {asset.id: self.part_config(asset) for _, _, asset in ranked[:limit]}

    def file(self, collection, path):
        stem = path[: -len(".usdz")] if path.endswith(".usdz") else None
        if stem is None:
            return None
        for variant in (".textured", ".bare"):
            stem = stem[: -len(variant)] if stem.endswith(variant) else stem
        asset = self._assets(collection or "").get(stem)
        if asset is None:
            raise NotFound("%s: no asset %s" % (self.NAME, stem))
        self.check_license()
        return base64.b64encode(self.package(asset)).decode("ascii")

    def get(self, key):
        """One key of the repository protocol; None for a key there is no answer to."""
        collections = self.collections()
        first, _, rest = key.partition("/")
        if collections and first in collections:
            collection, key = first, rest
        elif collections:
            collection = None  # the top: its children are the collections
        else:
            collection = ""
        if key == "meta":
            return self.meta(collection or None)
        if key == "deps":
            return sorted(collections) if collection is None else []
        if key.startswith("search/"):
            kind, _, query = key[len("search/") :].partition("?")
            return self.search(collection, query) if kind == "part" and collection is not None else {}
        if key.startswith("objects/"):
            kind, _, name = key[len("objects/") :].partition("/")
            if kind != "part" or collection is None:
                return {} if not name else None
            assets = self._assets(collection)
            if not name:
                return {asset.id: self.part_config(asset) for asset in assets.values()}
            base = name.split(";", 1)[0]
            return self.part_config(assets[base]) if base in assets else None
        if key.startswith("files/"):
            return self.file(collection, key[len("files/") :])
        return None


def serve(source, key):
    """'source.get(key)', with what went wrong said on stderr rather than raised."""
    try:
        return source.get(key)
    except SourceError as e:
        warn(str(e))
    except Exception as e:  # a source changed under us: say what, and where
        frame = traceback.extract_tb(e.__traceback__)[-1]
        warn(
            "%s: unexpected %s answering '%s': %s (%s:%d)"
            % (source.NAME, type(e).__name__, key, e, os.path.basename(frame.filename), frame.lineno)
        )
    return None
