#!/usr/bin/env python3
"""Deterministic preflight checks for the static site published from ``docs/``.

The checker uses only the Python standard library and never makes network
requests, so it is fast and repeatable in GitHub Actions and local builds. It
checks the deployable artifact; it does not replace browser, accessibility,
performance, or external-link testing.

Errors identify broken or contradictory artifacts and fail CI. Warnings identify
optional quality improvements and never block deployment.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "docs"
ORIGIN = "https://moosa-r.com"
HOST = urlsplit(ORIGIN).hostname

ERRORS: list[str] = []
WARNINGS: list[str] = []
DOCUMENTS: dict[Path, "Document"] = {}
IMAGE_SIZES: dict[Path, set[tuple[int, int]]] = {}

VOID_ELEMENTS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
ARIA_IDREFS = {
    "aria-activedescendant", "aria-controls", "aria-describedby", "aria-details",
    "aria-errormessage", "aria-labelledby", "aria-owns",
}
RESOURCE_ATTRIBUTES = {
    "audio": ("src",), "embed": ("src",), "iframe": ("src",),
    "img": ("src",), "input": ("src",), "object": ("data",),
    "script": ("src",), "source": ("src",), "track": ("src",),
    "video": ("src", "poster"),
}
IMAGE_SUFFIXES = {".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"}


# ---------------------------------------------------------------------------
# Reporting and URL helpers
# Record diagnostics and resolve local URLs exactly as GitHub Pages will.


def fail(message: str) -> None:
    """Record an integrity error that must stop deployment."""
    ERRORS.append(message)


def warn(message: str) -> None:
    """Record a non-blocking quality recommendation."""
    WARNINGS.append(message)


def label(path: Path, line: int | None = None) -> str:
    """Format a repository-relative source location for diagnostics."""
    relative = path.relative_to(ROOT).as_posix()
    return f"{relative}:{line}" if line else relative


def split_url(value: str):
    """Parse a URL safely, returning None for malformed authorities or ports."""
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return parsed
    except (AttributeError, TypeError, ValueError):
        return None


def normalized_url(value: str) -> str | None:
    """Normalize an absolute URL for reliable equality comparisons."""
    parsed = split_url(value)
    if parsed is None or not parsed.scheme or not parsed.hostname:
        return None
    scheme = parsed.scheme.lower()
    host = parsed.hostname.lower()
    port = parsed.port
    authority = host
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        authority = f"{host}:{port}"
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{scheme}://{authority}{path}{query}"


def plausible_language(value: str) -> bool:
    """Apply a conservative syntax check to an HTML language tag."""
    return bool(re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value))


def is_site_url(parsed) -> bool:
    """Return whether a parsed URL targets this site's standard HTTP(S) origin."""
    if parsed is None or parsed.hostname != HOST:
        return False
    return (
        parsed.scheme == "https" and parsed.port in {None, 443}
    ) or (
        parsed.scheme == "http" and parsed.port in {None, 80}
    )


def parse_srcset(value: str) -> list[str]:
    """Extract URL candidates from a simple HTML srcset value."""
    if value.lstrip().lower().startswith("data:"):
        return []
    return [candidate.strip().split()[0] for candidate in value.split(",") if candidate.strip()]


def exact_case_exists(path: Path) -> bool:
    """Detect paths that only work on a case-insensitive filesystem."""
    try:
        parts = path.relative_to(SITE).parts
    except ValueError:
        return False
    current = SITE
    for part in parts:
        if not current.is_dir():
            return False
        names = {child.name for child in current.iterdir()}
        if part not in names:
            return False
        current /= part
    return current.exists()


def site_target(value: str, source: Path) -> tuple[Path, str] | None:
    """Map a same-origin URL to its deployed file and decoded fragment."""
    parsed = split_url(value)
    if parsed is None:
        return None
    base = f"{ORIGIN}/{source.relative_to(SITE).as_posix()}"
    resolved = split_url(urljoin(base, value))
    if not is_site_url(resolved):
        return None

    decoded_path = unquote(resolved.path)
    if "\x00" in decoded_path or "\\" in decoded_path:
        return None
    candidate = (SITE / decoded_path.lstrip("/")).resolve(strict=False)
    try:
        candidate.relative_to(SITE.resolve())
    except ValueError:
        return None
    if decoded_path.endswith("/") or candidate.is_dir():
        candidate /= "index.html"
    return candidate, unquote(resolved.fragment)


def inspect_reference(value: str, source: Path, line: int, context: str) -> Path | None:
    """Validate one URL and return its local file when it is same-origin."""
    value = value.strip()
    here = label(source, line)
    navigation = context in {"<a> href", "<area> href"}
    if not value:
        if navigation:
            warn(f"{here}: empty {context}")
        else:
            fail(f"{here}: empty {context}")
        return None

    parsed = split_url(value)
    if parsed is None:
        fail(f"{here}: malformed URL in {context}: {value!r}")
        return None
    scheme = parsed.scheme.lower()
    if scheme in {"javascript", "vbscript", "file"}:
        fail(f"{here}: unsafe URL scheme in {context}: {value!r}")
        return None
    if scheme == "http":
        if navigation:
            warn(f"{here}: insecure HTTP URL in {context}: {value!r}")
        else:
            fail(f"{here}: insecure HTTP URL in {context}: {value!r}")
    if scheme in {"mailto", "tel"}:
        if not navigation:
            fail(f"{here}: navigation-only URL scheme in {context}: {value!r}")
        return None
    if scheme in {"data", "blob"}:
        return None
    if scheme and scheme not in {"http", "https"}:
        if not navigation:
            fail(f"{here}: unsupported URL scheme in {context}: {value!r}")
        return None

    target_data = site_target(value, source)
    if target_data is None:
        base = f"{ORIGIN}/{source.relative_to(SITE).as_posix()}"
        resolved = split_url(urljoin(base, value))
        if is_site_url(resolved):
            fail(f"{here}: invalid same-origin path in {context}: {value!r}")
        return None  # External HTTPS URL; availability is intentionally not tested here.
    target, fragment = target_data
    if not target.exists():
        fail(f"{here}: missing local target for {context}: {value!r}")
        return target
    if not exact_case_exists(target):
        fail(f"{here}: local target has incorrect filename case: {value!r}")
    if target.is_symlink():
        fail(f"{here}: local target is a symlink, which Pages artifacts do not support: {value!r}")

    if fragment and target.suffix.lower() in {".html", ".htm"}:
        document = DOCUMENTS.get(target.resolve())
        if document is not None and fragment not in document.ids:
            fail(f"{here}: fragment #{fragment} does not exist in {target.relative_to(SITE).as_posix()}")
    return target


# ---------------------------------------------------------------------------
# HTML collection and accessibility
# Parse documents once, then check structural and accessibility invariants.


class Document(HTMLParser):
    """Collect facts needed for high-signal checks without assuming a template."""

    def __init__(self, source: Path, text: str):
        """Initialize collectors for one HTML document."""
        super().__init__(convert_charrefs=True)
        self.source = source
        self.text = text
        self.doctype_html = False
        self.counts = {name: 0 for name in ("html", "head", "body", "main", "h1", "title")}
        self.languages: list[str] = []
        self.title_parts: list[str] = []
        self.ids: dict[str, int] = {}
        self.duplicate_ids: list[tuple[str, int]] = []
        self.aria_refs: list[tuple[str, str, int]] = []
        self.references: list[tuple[str, int, str]] = []
        self.metas: list[tuple[dict[str, str], int]] = []
        self.links: list[tuple[dict[str, str], int]] = []
        self.images: list[tuple[dict[str, str], int]] = []
        self.controls: list[dict] = []
        self.labels_for: set[str] = set()
        self.navs: list[dict] = []
        self.dialogs: list[dict] = []
        self.frames: list[dict] = []
        self.interactives: list[dict] = []
        self.scripts: list[dict] = []
        self.styles: list[tuple[str, int]] = []
        self.inline_styles: list[tuple[str, int]] = []
        self.stack: list[dict] = []
        self.active_names: list[dict] = []
        self.in_title = False
        self.current_script: dict | None = None
        self.current_style: dict | None = None

    def handle_decl(self, declaration: str) -> None:
        """Record whether the document declares the HTML doctype."""
        if declaration.strip().lower() == "doctype html":
            self.doctype_html = True

    @staticmethod
    def attrs_dict(attrs) -> dict[str, str]:
        """Normalize HTML attributes into a lowercase-key dictionary."""
        return {name.lower(): (value if value is not None else "") for name, value in attrs}

    def handle_starttag(self, tag: str, attrs) -> None:
        """Collect facts and references from each opening tag."""
        tag = tag.lower()
        attributes = self.attrs_dict(attrs)
        line = self.getpos()[0]
        in_head = any(item["tag"] == "head" for item in self.stack)
        hidden = (
            any(item["hidden"] for item in self.stack)
            or "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
        )
        entry = {"tag": tag, "hidden": hidden, "interactive": None}

        if tag in self.counts:
            if tag not in {"main", "h1"} or not hidden:
                self.counts[tag] += 1
        if tag == "html":
            self.languages.append(attributes.get("lang", ""))
        if tag == "title" and in_head:
            self.in_title = True
        if tag == "meta" and in_head:
            self.metas.append((attributes, line))
        if tag == "link" and in_head:
            self.links.append((attributes, line))
        if tag == "label" and attributes.get("for"):
            self.labels_for.add(attributes["for"])

        element_id = attributes.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.append((element_id, line))
            else:
                self.ids[element_id] = line
        for name in ARIA_IDREFS:
            if attributes.get(name):
                self.aria_refs.append((name, attributes[name], line))

        if attributes.get("href") is not None:
            self.references.append((attributes["href"], line, f"<{tag}> href"))
        for name in RESOURCE_ATTRIBUTES.get(tag, ()):
            if attributes.get(name) is not None:
                self.references.append((attributes[name], line, f"<{tag}> {name}"))
        if attributes.get("srcset") is not None:
            for candidate in parse_srcset(attributes["srcset"]):
                self.references.append((candidate, line, f"<{tag}> srcset"))
        if attributes.get("style"):
            self.inline_styles.append((attributes["style"], line))

        if tag == "img":
            self.images.append((attributes, line))
            if attributes.get("alt"):
                for item in self.active_names:
                    item["text"].append(attributes["alt"])
        if tag in {"a", "button"}:
            item = {"tag": tag, "attrs": attributes, "line": line, "text": [], "hidden": hidden}
            self.interactives.append(item)
            self.active_names.append(item)
            entry["interactive"] = item
        if tag in {"input", "select", "textarea"}:
            self.controls.append({
                "tag": tag,
                "attrs": attributes,
                "line": line,
                "hidden": hidden,
                "wrapped": any(item["tag"] == "label" for item in self.stack),
            })
        if tag == "nav" and not hidden:
            self.navs.append({"attrs": attributes, "line": line})
        if tag == "dialog":
            self.dialogs.append({"attrs": attributes, "line": line})
        if tag == "iframe" and not hidden:
            self.frames.append({"attrs": attributes, "line": line})
        if tag == "script":
            self.current_script = {"attrs": attributes, "line": line, "text": []}
            self.scripts.append(self.current_script)
        if tag == "style":
            self.current_style = {"line": line, "text": []}

        if tag not in VOID_ELEMENTS:
            self.stack.append(entry)

    def handle_startendtag(self, tag: str, attrs) -> None:
        """Process an explicitly self-closing HTML element."""
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        """Close active text collectors when an element ends."""
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        if tag == "script":
            self.current_script = None
        if tag == "style" and self.current_style is not None:
            self.styles.append(("".join(self.current_style["text"]), self.current_style["line"]))
            self.current_style = None

        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]["tag"] == tag:
                closed = self.stack[index:]
                self.stack = self.stack[:index]
                for entry in closed:
                    interactive = entry["interactive"]
                    if interactive in self.active_names:
                        self.active_names.remove(interactive)
                break

    def handle_data(self, data: str) -> None:
        """Collect text used by titles, scripts, styles, and accessible names."""
        if self.in_title:
            self.title_parts.append(data)
        if self.current_script is not None:
            self.current_script["text"].append(data)
        if self.current_style is not None:
            self.current_style["text"].append(data)
        for item in self.active_names:
            item["text"].append(data)

    @property
    def title(self) -> str:
        """Return the document title with whitespace normalized."""
        return " ".join("".join(self.title_parts).split())


def has_name(item: dict, ids: dict[str, int]) -> bool:
    """Return whether an interactive element has an accessible name source."""
    attrs = item["attrs"]
    if attrs.get("aria-label", "").strip() or attrs.get("title", "").strip():
        return True
    labelled_by = attrs.get("aria-labelledby", "").split()
    if labelled_by and all(reference in ids for reference in labelled_by):
        return True
    return bool(" ".join(item.get("text", [])).strip())


def meta_values(document: Document, attribute: str, name: str) -> list[tuple[str, int]]:
    """Collect matching metadata content and source line numbers."""
    expected = name.lower()
    return [
        (attrs.get("content", "").strip(), line)
        for attrs, line in document.metas
        if attrs.get(attribute, "").lower() == expected
    ]


def validate_document(document: Document) -> None:
    """Check core HTML structure and static accessibility relationships."""
    source = document.source
    where = label(source)
    if not document.doctype_html:
        fail(f"{where}: missing <!doctype html>")
    for tag in ("html", "head", "body"):
        if document.counts[tag] != 1:
            fail(f"{where}: expected one <{tag}>, found {document.counts[tag]}")
    if document.counts["main"] == 0:
        warn(f"{where}: no visible <main> landmark")
    elif document.counts["main"] > 1:
        fail(f"{where}: expected at most one visible <main>, found {document.counts['main']}")
    if document.counts["h1"] < 1:
        warn(f"{where}: no visible <h1> heading")
    if document.counts["title"] != 1 or not document.title:
        fail(f"{where}: <head> must contain one non-empty <title>")

    language = document.languages[0] if document.languages else ""
    if not plausible_language(language):
        fail(f"{where}: <html> needs a valid BCP 47 lang value")
    charsets = [attrs.get("charset", "").strip().lower() for attrs, _ in document.metas if "charset" in attrs]
    if charsets != ["utf-8"]:
        fail(f"{where}: <head> must contain one UTF-8 charset declaration")
    first_kib = document.text.encode("utf-8")[:1024].decode("utf-8", errors="ignore")
    if not re.search(r"<meta\s+[^>]*charset\s*=", first_kib, re.I):
        fail(f"{where}: UTF-8 charset declaration must appear in the first 1024 bytes")
    viewports = meta_values(document, "name", "viewport")
    if len(viewports) != 1 or "width=device-width" not in viewports[0][0].replace(" ", "").lower():
        fail(f"{where}: add one responsive viewport meta tag")

    for element_id, line in document.duplicate_ids:
        fail(f"{label(source, line)}: duplicate id {element_id!r}")
    for name, value, line in document.aria_refs:
        for reference in value.split():
            if reference not in document.ids:
                warn(f"{label(source, line)}: {name} references an id absent from the static HTML: {reference!r}")

    for attrs, line in document.images:
        if "alt" not in attrs:
            fail(f"{label(source, line)}: <img> is missing alt text (use alt=\"\" when decorative)")
        dimensions = [name for name in ("width", "height") if name in attrs]
        for dimension in dimensions:
            if not attrs[dimension].isdigit():
                fail(f"{label(source, line)}: <img> has an invalid {dimension} attribute")
        if len(dimensions) < 2:
            warn(f"{label(source, line)}: <img> should declare both width and height to reserve layout space")
        allowed = {
            "loading": {"lazy", "eager"},
            "decoding": {"sync", "async", "auto"},
            "fetchpriority": {"high", "low", "auto"},
        }
        for name, values in allowed.items():
            if name in attrs and attrs[name].lower() not in values:
                warn(f"{label(source, line)}: unrecognized {name} value {attrs[name]!r}")

    for item in document.interactives:
        if item["hidden"] or (item["tag"] == "a" and "href" not in item["attrs"]):
            continue
        if not has_name(item, document.ids):
            fail(f"{label(source, item['line'])}: <{item['tag']}> has no accessible name")
    for item in document.controls:
        attrs = item["attrs"]
        if item["hidden"] or attrs.get("type", "").lower() == "hidden":
            continue
        labelled_by = attrs.get("aria-labelledby", "").split()
        input_type = attrs.get("type", "text").lower() if item["tag"] == "input" else ""
        native_name = (
            input_type in {"submit", "reset"}
            or (input_type == "button" and bool(attrs.get("value", "").strip()))
            or (input_type == "image" and bool(attrs.get("alt", "").strip()))
        )
        named = (
            attrs.get("aria-label", "").strip()
            or attrs.get("title", "").strip()
            or item["wrapped"]
            or native_name
            or (attrs.get("id") and attrs["id"] in document.labels_for)
            or (labelled_by and all(reference in document.ids for reference in labelled_by))
        )
        if not named:
            fail(f"{label(source, item['line'])}: <{item['tag']}> has no accessible label")
    if len(document.navs) > 1:
        names = []
        for item in document.navs:
            attrs = item["attrs"]
            name = attrs.get("aria-label", "").strip() or attrs.get("aria-labelledby", "").strip()
            if not name:
                warn(f"{label(source, item['line'])}: multiple <nav> landmarks should have distinct labels")
            names.append(name.casefold())
        if len([name for name in names if name]) != len(set(name for name in names if name)):
            warn(f"{where}: multiple <nav> landmarks should have distinct labels")
    for item in document.dialogs:
        if not has_name(item, document.ids):
            fail(f"{label(source, item['line'])}: <dialog> has no accessible name")
    for item in document.frames:
        if not item["attrs"].get("title", "").strip():
            fail(f"{label(source, item['line'])}: <iframe> needs a non-empty title")


# ---------------------------------------------------------------------------
# Asset inspection
# Verify that browser-facing binary assets are readable and match their formats.


def jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Read intrinsic dimensions from a JPEG frame header."""
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("invalid JPEG signature")
    position = 2
    sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
    while position < len(data):
        while position < len(data) and data[position] != 0xFF:
            position += 1
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0x01, *range(0xD0, 0xDA)}:
            continue
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2 or position + length > len(data):
            raise ValueError("truncated JPEG segment")
        if marker in sof_markers:
            if length < 7:
                raise ValueError("invalid JPEG frame header")
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            return width, height
        position += length
    raise ValueError("JPEG dimensions not found")


def webp_dimensions(data: bytes) -> tuple[int, int]:
    """Read intrinsic dimensions from a supported WebP header."""
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("invalid WebP signature")
    kind = data[12:16]
    payload = data[20:]
    if kind == b"VP8X" and len(payload) >= 10:
        return 1 + int.from_bytes(payload[4:7], "little"), 1 + int.from_bytes(payload[7:10], "little")
    if kind == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width, height = struct.unpack_from("<HH", payload, 6)
        return width & 0x3FFF, height & 0x3FFF
    if kind == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        b1, b2, b3, b4 = payload[1:5]
        width = 1 + b1 + ((b2 & 0x3F) << 8)
        height = 1 + ((b2 & 0xC0) >> 6) + (b3 << 2) + ((b4 & 0x0F) << 10)
        return width, height
    raise ValueError("unsupported or truncated WebP image")


def raster_dimensions(path: Path, data: bytes) -> set[tuple[int, int]]:
    """Validate a raster header and return all declared intrinsic sizes."""
    suffix = path.suffix.lower()
    if suffix == ".png":
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            raise ValueError("invalid PNG header")
        return {struct.unpack(">II", data[16:24])}
    if suffix in {".jpg", ".jpeg"}:
        return {jpeg_dimensions(data)}
    if suffix == ".gif":
        if len(data) < 10 or data[:6] not in {b"GIF87a", b"GIF89a"}:
            raise ValueError("invalid GIF header")
        return {struct.unpack("<HH", data[6:10])}
    if suffix == ".webp":
        return {webp_dimensions(data)}
    if suffix == ".ico":
        if len(data) < 6:
            raise ValueError("truncated ICO header")
        reserved, image_type, count = struct.unpack_from("<HHH", data)
        if reserved != 0 or image_type != 1 or count < 1 or len(data) < 6 + 16 * count:
            raise ValueError("invalid ICO directory")
        sizes = set()
        for index in range(count):
            width, height = data[6 + 16 * index:8 + 16 * index]
            sizes.add((width or 256, height or 256))
        return sizes
    raise ValueError(f"unsupported raster extension {suffix}")


def validate_svg(path: Path) -> None:
    """Check that an SVG is valid XML without executable content."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"invalid SVG XML: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ValueError("SVG root element is not <svg>")
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() == "script":
            raise ValueError("SVG contains a <script> element")
        for name, value in element.attrib.items():
            local_name = name.rsplit("}", 1)[-1].lower()
            if local_name.startswith("on") or value.lstrip().lower().startswith("javascript:"):
                raise ValueError("SVG contains executable content")


def inspect_assets() -> None:
    """Inspect deployed images, fonts, and unsupported artifact symlinks."""
    for path in sorted(SITE.rglob("*")):
        if path.is_symlink():
            fail(f"{label(path)}: symlinks are not supported in GitHub Pages artifacts")
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix == ".svg":
                validate_svg(path)
            elif suffix in IMAGE_SUFFIXES:
                sizes = raster_dimensions(path, path.read_bytes())
                if any(width <= 0 or height <= 0 for width, height in sizes):
                    raise ValueError("image dimensions must be positive")
                IMAGE_SIZES[path.resolve()] = sizes
            elif suffix == ".woff" and path.read_bytes()[:4] != b"wOFF":
                raise ValueError("invalid WOFF signature")
            elif suffix == ".woff2" and path.read_bytes()[:4] != b"wOF2":
                raise ValueError("invalid WOFF2 signature")
        except (OSError, ValueError, struct.error) as exc:
            fail(f"{label(path)}: {exc}")


def check_declared_image_size(attrs: dict[str, str], line: int, source: Path) -> None:
    """Warn when HTML dimensions would distort a local raster image."""
    target_data = site_target(attrs.get("src", ""), source)
    if target_data is None:
        return
    target, _ = target_data
    intrinsic = IMAGE_SIZES.get(target.resolve())
    if not intrinsic or not attrs.get("width", "").isdigit() or not attrs.get("height", "").isdigit():
        return
    declared = int(attrs["width"]), int(attrs["height"])
    if declared[0] == 0 or declared[1] == 0:
        return
    ratio = declared[0] / declared[1]
    if all(abs(ratio - (width / height)) > 0.01 for width, height in intrinsic):
        warn(f"{label(source, line)}: declared dimensions may distort {attrs.get('src')!r}")


# ---------------------------------------------------------------------------
# Metadata and local references
# Check metadata for internal consistency without prescribing page wording.


def single_meta(document: Document, attribute: str, name: str, on_missing=None) -> str | None:
    """Return one metadata value and report missing, duplicate, or empty entries."""
    values = meta_values(document, attribute, name)
    if not values:
        if on_missing:
            on_missing(f"{label(document.source)}: missing {name} metadata")
        return None
    if len(values) > 1:
        fail(f"{label(document.source)}: duplicate {name} metadata")
    if not values[0][0]:
        fail(f"{label(document.source, values[0][1])}: empty {name} metadata")
    return values[0][0]


def link_values(document: Document, relation: str) -> list[tuple[str, dict[str, str], int]]:
    """Collect head links containing a requested rel token."""
    relation = relation.lower()
    return [
        (attrs.get("href", ""), attrs, line)
        for attrs, line in document.links
        if relation in attrs.get("rel", "").lower().split()
    ]


def page_url(path: Path) -> str:
    """Return the public URL GitHub Pages assigns to an HTML file."""
    relative = path.relative_to(SITE).as_posix()
    if relative == "index.html":
        return f"{ORIGIN}/"
    if relative.endswith("/index.html"):
        return f"{ORIGIN}/{relative[:-10]}"
    return f"{ORIGIN}/{relative}"


def is_noindex(document: Document) -> bool:
    """Return whether robots metadata excludes a document from indexing."""
    directives = " ".join(value for value, _ in meta_values(document, "name", "robots"))
    return bool(re.search(r"(?:^|[\s,])(?:noindex|none)(?:$|[\s,])", directives, re.I))


def validate_json_ld(document: Document) -> None:
    """Ensure every JSON-LD block contains valid object or array JSON."""
    for script in document.scripts:
        if script["attrs"].get("type", "").lower() != "application/ld+json":
            continue
        raw = "".join(script["text"]).strip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            fail(f"{label(document.source, script['line'])}: invalid JSON-LD: {exc.msg}")
            continue
        if not isinstance(value, (dict, list)):
            fail(f"{label(document.source, script['line'])}: JSON-LD root must be an object or array")


def validate_metadata() -> None:
    """Validate canonical, search, social-card, and structured metadata."""
    titles: dict[str, Path] = {}
    canonicals: dict[str, Path] = {}

    for document in DOCUMENTS.values():
        title_key = document.title.casefold()
        if title_key in titles:
            warn(f"{label(document.source)}: title duplicates {label(titles[title_key])}")
        elif title_key:
            titles[title_key] = document.source
        validate_json_ld(document)

        if document.source.name == "404.html" and not is_noindex(document):
            warn(f"{label(document.source)}: a custom 404 page should use robots=noindex")

        canonical_links = link_values(document, "canonical")
        canonical = None
        if len(canonical_links) > 1:
            fail(f"{label(document.source)}: multiple canonical links are ambiguous")
        elif canonical_links:
            href, _, line = canonical_links[0]
            parsed = split_url(href)
            canonical = normalized_url(href)
            if canonical is None or parsed is None or parsed.scheme != "https":
                fail(f"{label(document.source, line)}: canonical URL must be absolute HTTPS")
                canonical = None
            elif parsed.fragment:
                fail(f"{label(document.source, line)}: canonical URL must not contain a fragment")
        elif not is_noindex(document):
            warn(f"{label(document.source)}: indexable page has no canonical link")
        if canonical:
            if canonical in canonicals:
                warn(f"{label(document.source)}: canonical URL is shared with {label(canonicals[canonical])}")
            else:
                canonicals[canonical] = document.source

        if is_noindex(document):
            continue

        single_meta(document, "name", "description", warn)
        og_names = ("og:title", "og:type", "og:description", "og:url", "og:image", "og:image:alt")
        twitter_names = ("twitter:card", "twitter:title", "twitter:description", "twitter:image", "twitter:image:alt")
        has_og = any(meta_values(document, "property", name) for name in og_names)
        has_twitter = any(meta_values(document, "name", name) for name in twitter_names)
        if not has_og and not has_twitter:
            warn(f"{label(document.source)}: indexable page has no social preview metadata")
        og = {name: single_meta(document, "property", name, warn) for name in og_names} if has_og else {}
        twitter = {name: single_meta(document, "name", name, warn) for name in twitter_names} if has_twitter else {}
        og_url = og.get("og:url")
        og_image = og.get("og:image")
        twitter_card = twitter.get("twitter:card")
        twitter_image = twitter.get("twitter:image")

        if og_url and canonical and normalized_url(og_url) != canonical:
            fail(f"{label(document.source)}: og:url must match the canonical URL")
        if twitter_card and twitter_card not in {"summary", "summary_large_image", "app", "player"}:
            warn(f"{label(document.source)}: unrecognized twitter:card value {twitter_card!r}")
        for name, value in (("og:image", og_image), ("twitter:image", twitter_image)):
            if not value:
                continue
            target = inspect_reference(value, document.source, 1, name)
            if target and target.resolve() not in IMAGE_SIZES:
                fail(f"{label(document.source)}: {name} does not reference a valid raster image")
        if og_image:
            width = single_meta(document, "property", "og:image:width", warn)
            height = single_meta(document, "property", "og:image:height", warn)
            target_data = site_target(og_image, document.source)
            if target_data and width and height and width.isdigit() and height.isdigit():
                actual = IMAGE_SIZES.get(target_data[0].resolve(), set())
                if actual and (int(width), int(height)) not in actual:
                    fail(f"{label(document.source)}: Open Graph image dimensions do not match the file")
            elif (width and not width.isdigit()) or (height and not height.isdigit()):
                fail(f"{label(document.source)}: Open Graph image dimensions must be integers")


def validate_references() -> None:
    """Validate every collected HTML URL and image aspect ratio."""
    for document in DOCUMENTS.values():
        for value, line, context in document.references:
            inspect_reference(value, document.source, line, context)
        for attrs, line in document.images:
            check_declared_image_size(attrs, line, document.source)


# ---------------------------------------------------------------------------
# CSS, JavaScript, and integrations
# Validate code syntax, referenced assets, and detectable analytics consistency.


CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?(['\"])(.*?)\1", re.I)


def css_without_comments_or_strings(text: str) -> str:
    """Remove CSS comments and strings before balancing delimiters."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(['\"])(?:\\.|(?!\1).)*\1", "", text, flags=re.S)


def validate_css_text(text: str, source: Path, starting_line: int = 1) -> None:
    """Check CSS delimiter balance and resolvable local resources."""
    without_urls = CSS_URL.sub(
        lambda match: "url()" + "\n" * match.group(0).count("\n"), text
    )
    stripped = css_without_comments_or_strings(without_urls)
    pairs = {"}": "{", "]": "[", ")": "("}
    stack: list[tuple[str, int]] = []
    index = 0
    while index < len(stripped):
        char = stripped[index]
        if char == "\\":
            index += 2
            continue
        if char in "{[(":
            stack.append((char, index))
        elif char in pairs:
            if not stack or stack[-1][0] != pairs[char]:
                line = starting_line + stripped.count("\n", 0, index)
                fail(f"{label(source, line)}: unmatched CSS delimiter {char!r}")
                break
            stack.pop()
        index += 1
    if stack:
        char, index = stack[-1]
        line = starting_line + stripped.count("\n", 0, index)
        fail(f"{label(source, line)}: unclosed CSS delimiter {char!r}")

    seen: set[tuple[int, str]] = set()
    for pattern in (CSS_URL, CSS_IMPORT):
        for match in pattern.finditer(text):
            value = match.group(2).strip()
            item = (match.start(), value)
            if item in seen or not value or value.startswith("#"):
                continue
            seen.add(item)
            line = starting_line + text.count("\n", 0, match.start())
            inspect_reference(value, source, line, "CSS resource")


def run_node_check(node: str, source: Path, code: str | None = None, module: bool = False) -> None:
    """Use Node.js to syntax-check a JavaScript file or inline block."""
    command = [node, "--check"]
    if code is None:
        command.append(str(source))
    else:
        if module:
            command.append("--input-type=module")
        command.append("-")
    try:
        result = subprocess.run(command, input=code, text=True, capture_output=True, check=False)
    except OSError as exc:
        fail(f"{label(source)}: could not run Node.js: {exc}")
        return
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        summary = detail[-1] if detail else "syntax error"
        fail(f"{label(source)}: JavaScript syntax check failed: {summary}")


def validate_code() -> None:
    """Validate all deployed CSS and browser JavaScript without executing it."""
    css_files = {
        path for path in SITE.rglob("*")
        if path.is_file() and path.suffix.lower() == ".css"
    }
    for path in sorted(css_files):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"{label(path)}: CSS is not readable UTF-8: {exc}")
            continue
        validate_css_text(text, path)
    for document in DOCUMENTS.values():
        for text, line in document.styles:
            validate_css_text(text, document.source, line)
        for text, line in document.inline_styles:
            validate_css_text(text, document.source, line)

    node = shutil.which("node")
    if node is None:
        message = "Node.js is unavailable; JavaScript syntax was not checked"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            fail(message)
        else:
            warn(message)
        return
    module_files = set()
    for document in DOCUMENTS.values():
        for script in document.scripts:
            if script["attrs"].get("type", "").lower() != "module" or not script["attrs"].get("src"):
                continue
            target_data = site_target(script["attrs"]["src"], document.source)
            if target_data:
                module_files.add(target_data[0].resolve())
    javascript_files = {
        path for path in SITE.rglob("*")
        if path.is_file() and path.suffix.lower() in {".js", ".mjs"}
    }
    for path in sorted(javascript_files):
        if path.resolve() in module_files or path.suffix.lower() == ".mjs":
            try:
                code = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                fail(f"{label(path)}: JavaScript is not readable UTF-8: {exc}")
                continue
            run_node_check(node, path, code, module=True)
        else:
            run_node_check(node, path)
    for document in DOCUMENTS.values():
        for script in document.scripts:
            script_type = script["attrs"].get("type", "").lower().split(";", 1)[0].strip()
            code = "".join(script["text"]).strip()
            if not code or script["attrs"].get("src") or script_type == "application/ld+json":
                continue
            if script_type not in {"", "text/javascript", "application/javascript", "module"}:
                continue
            run_node_check(node, document.source, code, module=script_type == "module")


def document_script_text(document: Document) -> str:
    """Combine inline and local script text in document order for inspection."""
    parts = []
    for script in document.scripts:
        source = script["attrs"].get("src", "")
        target_data = site_target(source, document.source) if source else None
        if target_data and target_data[0].is_file():
            try:
                parts.append(target_data[0].read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                pass  # The referenced file is reported by the code and URL checks.
        elif source:
            parts.append(source)
        parts.append("".join(script["text"]))
    return "\n".join(parts)


def validate_google_tag() -> None:
    """Check statically detectable Google tag IDs."""
    for document in DOCUMENTS.values():
        text = document_script_text(document)
        loader_ids = set(re.findall(r"googletagmanager\.com/gtag/js\?id=(G-[A-Z0-9]+)", text, re.I))
        config_ids = set(re.findall(r"gtag\(\s*['\"]config['\"]\s*,\s*['\"](G-[A-Z0-9]+)['\"]", text, re.I))
        if not loader_ids and not config_ids:
            continue
        if loader_ids and config_ids and loader_ids != config_ids:
            warn(f"{label(document.source)}: verify differing Google tag loader and config IDs")
        elif not loader_ids or not config_ids:
            warn(f"{label(document.source)}: Google tag setup could not be fully verified statically")


# ---------------------------------------------------------------------------
# Icons, manifest, robots, and sitemap
# Validate browser-discovery files and crawler-facing URL declarations.


def declared_sizes(value: str) -> set[tuple[int, int]]:
    """Parse HTML or manifest image-size tokens into integer pairs."""
    sizes = set()
    for token in value.lower().split():
        match = re.fullmatch(r"(\d+)x(\d+)", token)
        if match:
            sizes.add((int(match.group(1)), int(match.group(2))))
        elif token != "any":
            raise ValueError(f"invalid sizes token {token!r}")
    return sizes


def validate_icons_and_manifest() -> None:
    """Validate declared browser icons and any linked web manifests."""
    manifests: set[Path] = set()
    for document in DOCUMENTS.values():
        icons = link_values(document, "icon")
        apple_icons = link_values(document, "apple-touch-icon")
        manifest_links = link_values(document, "manifest")
        if not icons:
            warn(f"{label(document.source)}: missing browser icon declaration")
        if not apple_icons:
            warn(f"{label(document.source)}: missing apple-touch-icon declaration")
        if len(manifest_links) > 1:
            fail(f"{label(document.source)}: multiple web manifest links are ambiguous")
        elif not manifest_links:
            warn(f"{label(document.source)}: missing web manifest link")
        if not any(value for value, _ in meta_values(document, "name", "theme-color")):
            warn(f"{label(document.source)}: missing theme-color metadata")
        if not any(value for value, _ in meta_values(document, "name", "color-scheme")):
            warn(f"{label(document.source)}: missing color-scheme metadata")

        for href, attrs, line in icons + apple_icons + link_values(document, "mask-icon"):
            target = inspect_reference(href, document.source, line, "browser icon")
            if not target:
                continue
            try:
                sizes = declared_sizes(attrs.get("sizes", ""))
            except ValueError as exc:
                fail(f"{label(document.source, line)}: {exc}")
                continue
            actual = IMAGE_SIZES.get(target.resolve())
            if sizes and actual and not sizes.issubset(actual):
                fail(f"{label(document.source, line)}: declared icon sizes do not match {href!r}")
        for href, _, line in manifest_links:
            target = inspect_reference(href, document.source, line, "web manifest")
            if target:
                manifests.add(target.resolve())

    for path in manifests:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            fail(f"{label(path)}: invalid web manifest: {exc}")
            continue
        if not isinstance(manifest, dict):
            fail(f"{label(path)}: web manifest root must be an object")
            continue
        if not any(isinstance(manifest.get(name), str) and manifest[name].strip() for name in ("name", "short_name")):
            warn(f"{label(path)}: web manifest should provide name or short_name")
        for name in ("start_url", "scope"):
            value = manifest.get(name)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                fail(f"{label(path)}: manifest {name} must be a non-empty string when present")
                continue
            target_data = site_target(value, path)
            if target_data is None:
                fail(f"{label(path)}: manifest {name} must stay on {HOST}")
            elif name == "start_url" and not target_data[0].exists():
                warn(f"{label(path)}: manifest start_url does not map to a static file")
            elif name == "start_url":
                inspect_reference(value, path, 1, "manifest start_url")
        display = manifest.get("display")
        if display is not None and (not isinstance(display, str) or not display.strip()):
            fail(f"{label(path)}: manifest display must be a non-empty string when present")
        elif display and display not in {"browser", "minimal-ui", "standalone", "fullscreen"}:
            warn(f"{label(path)}: unrecognized manifest display mode {display!r}")

        icons = manifest.get("icons")
        if icons is None:
            warn(f"{label(path)}: web manifest should provide icons")
            continue
        if not isinstance(icons, list):
            fail(f"{label(path)}: manifest icons must be an array")
            continue
        if not icons:
            warn(f"{label(path)}: web manifest icons array is empty")
        available_sizes: set[tuple[int, int]] = set()
        for index, icon in enumerate(icons):
            if not isinstance(icon, dict) or not isinstance(icon.get("src"), str):
                fail(f"{label(path)}: manifest icon {index + 1} needs a src")
                continue
            target = inspect_reference(icon["src"], path, 1, f"manifest icon {index + 1}")
            try:
                sizes = declared_sizes(str(icon.get("sizes", "")))
            except ValueError as exc:
                fail(f"{label(path)}: manifest icon {index + 1}: {exc}")
                continue
            available_sizes.update(sizes)
            actual = IMAGE_SIZES.get(target.resolve()) if target else None
            if sizes and actual and not sizes.issubset(actual):
                fail(f"{label(path)}: manifest icon {index + 1} sizes do not match the file")
        if not any(width >= 192 and height >= 192 for width, height in available_sizes):
            warn(f"{label(path)}: consider including a manifest icon of at least 192x192")
        if not any(width >= 512 and height >= 512 for width, height in available_sizes):
            warn(f"{label(path)}: consider including a manifest icon of at least 512x512")


def sitemap_urls(path: Path, visited: set[Path] | None = None) -> list[str]:
    """Read URL entries from an XML sitemap or local sitemap index."""
    visited = visited or set()
    resolved_path = path.resolve()
    if resolved_path in visited:
        fail(f"{label(path)}: recursive sitemap reference")
        return []
    visited.add(resolved_path)
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        fail(f"{label(path)}: invalid sitemap XML: {exc}")
        return []
    kind = root.tag.rsplit("}", 1)[-1]
    record_name = "url" if kind == "urlset" else "sitemap"
    locations = []
    for record in root:
        if record.tag.rsplit("}", 1)[-1] != record_name:
            continue
        location = next(
            ((child.text or "").strip() for child in record if child.tag.rsplit("}", 1)[-1] == "loc"),
            "",
        )
        locations.append(location)
    if kind == "urlset":
        return locations
    if kind == "sitemapindex":
        urls: list[str] = []
        for location in locations:
            target = inspect_reference(location, path, 1, "sitemap index")
            if target:
                urls.extend(sitemap_urls(target, visited))
        return urls
    fail(f"{label(path)}: sitemap root must be <urlset> or <sitemapindex>")
    return []


def validate_robots_and_sitemap() -> None:
    """Check crawler access and consistency of local sitemap entries."""
    robots_path = SITE / "robots.txt"
    if not robots_path.is_file():
        fail("docs/robots.txt: missing robots.txt")
        return
    try:
        text = robots_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"docs/robots.txt: robots.txt is not readable UTF-8: {exc}")
        return
    parser = RobotFileParser()
    parser.set_url(f"{ORIGIN}/robots.txt")
    parser.parse(text.splitlines())
    if not parser.can_fetch("*", f"{ORIGIN}/"):
        fail("docs/robots.txt: the site root is blocked for general crawlers")

    directives = [match.group(1).strip() for match in re.finditer(r"^\s*Sitemap\s*:\s*(\S+)\s*$", text, re.I | re.M)]
    if not directives:
        fail("docs/robots.txt: missing Sitemap directive")
        return
    indexed_urls: list[str] = []
    for value in directives:
        target = inspect_reference(value, robots_path, 1, "robots Sitemap directive")
        if target:
            indexed_urls.extend(sitemap_urls(target))

    normalized: set[str] = set()
    for value in indexed_urls:
        url = normalized_url(value)
        parsed = split_url(value)
        if url is None or parsed is None or parsed.scheme != "https" or parsed.hostname != HOST:
            fail(f"docs/sitemap.xml: sitemap URL must be absolute HTTPS on {HOST}: {value!r}")
            continue
        if url in normalized:
            warn(f"docs/sitemap.xml: duplicate sitemap URL {url!r}")
        normalized.add(url)
        target = inspect_reference(value, SITE / "sitemap.xml", 1, "sitemap URL")
        document = DOCUMENTS.get(target.resolve()) if target else None
        if document and is_noindex(document):
            fail(f"docs/sitemap.xml: noindex page must not appear in the sitemap: {url!r}")
        if document:
            canonical_links = link_values(document, "canonical")
            canonical = normalized_url(canonical_links[0][0]) if len(canonical_links) == 1 else None
            if canonical and canonical != url:
                warn(f"docs/sitemap.xml: {url!r} differs from its declared canonical {canonical!r}")

    preferred_urls = set()
    for document in DOCUMENTS.values():
        if is_noindex(document):
            continue
        canonical_links = link_values(document, "canonical")
        canonical = normalized_url(canonical_links[0][0]) if len(canonical_links) == 1 else None
        parsed = split_url(canonical) if canonical else None
        if canonical and parsed and parsed.scheme == "https" and parsed.hostname == HOST:
            preferred_urls.add(canonical)
        else:
            preferred_urls.add(page_url(document.source))
    for value in sorted(preferred_urls - normalized):
        warn(f"docs/sitemap.xml: indexable page is not listed: {value!r}")


def validate_indexnow_key() -> None:
    """Validate the explicitly configured IndexNow ownership file when enabled."""
    key = os.environ.get("INDEXNOW_KEY")
    if not key:
        return
    if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
        fail("INDEXNOW_KEY must contain 8-128 letters, digits, or hyphens")
        return
    path = SITE / f"{key}.txt"
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        fail(f"{label(path)}: IndexNow ownership file is unavailable: {exc}")
        return
    if content != key:
        fail(f"{label(path)}: IndexNow ownership file content must match INDEXNOW_KEY")


# ---------------------------------------------------------------------------
# Entry point
# Discover the artifact, run each independent check, and summarize the result.


def discover_documents() -> None:
    """Load required pages and parse every deployed HTML document."""
    if not SITE.is_dir():
        fail("docs/: publish directory does not exist")
        return
    required = (SITE / "index.html", SITE / "404.html")
    for path in required:
        if not path.is_file():
            fail(f"{label(path)}: required page is missing")
    html_paths = {
        path for path in SITE.rglob("*")
        if path.is_file() and path.suffix.lower() in {".html", ".htm"}
    }
    for path in sorted(html_paths):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            fail(f"{label(path)}: HTML is not readable UTF-8: {exc}")
            continue
        document = Document(path.resolve(), text)
        try:
            document.feed(text)
            document.close()
        except Exception as exc:
            fail(f"{label(path)}: HTML parser failed: {exc}")
            continue
        DOCUMENTS[path.resolve()] = document


def report() -> int:
    """Print stable diagnostics and return a CI-compatible exit status."""
    errors = sorted(set(ERRORS))
    warnings = sorted(set(WARNINGS))
    for message in warnings:
        print(f"WARNING: {message}")
    for message in errors:
        print(f"ERROR: {message}")
    if errors:
        print(f"FAILED: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"PASSED: {len(DOCUMENTS)} HTML document(s), {len(IMAGE_SIZES)} raster image(s), {len(warnings)} warning(s)")
    return 0


def main() -> int:
    """Run the complete production artifact preflight."""
    discover_documents()
    for document in DOCUMENTS.values():
        validate_document(document)
    inspect_assets()
    validate_references()
    validate_metadata()
    validate_code()
    validate_google_tag()
    validate_icons_and_manifest()
    validate_robots_and_sitemap()
    validate_indexnow_key()
    return report()


if __name__ == "__main__":
    sys.exit(main())
