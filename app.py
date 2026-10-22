from __future__ import annotations

import ipaddress
import json
import random
import re
import socket
import time
import threading
from collections import OrderedDict
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024

USER_AGENT = (
    "Mozilla/5.0 (compatible; RandomPhotoFinder/2.0; "
    "+https://example.invalid/bot)"
)
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 5
MAX_SCAN_SECONDS = 20
MAX_PAGES_TO_SCAN = 10  # Includes the URL entered by the user.
MAX_IMAGES_TO_KEEP = 750
SCAN_CACHE_TTL_SECONDS = 15 * 60  # Reuse a website scan for 15 minutes.
MAX_CACHED_SCANS = 20

# Cache the extracted image list so "Another photo" doesn't crawl the site again.
_SCAN_CACHE: OrderedDict[str, tuple[float, str, list[dict[str, str]], int]] = OrderedDict()
_SCAN_CACHE_LOCK = threading.Lock()

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".webp", ".gif", ".avif",
    ".bmp", ".tif", ".tiff", ".svg", ".jfif", ".pjpeg", ".pjp",
}
NON_PAGE_EXTENSIONS = {
    ".css", ".js", ".mjs", ".json", ".xml", ".pdf", ".zip", ".gz",
    ".rar", ".7z", ".mp4", ".webm", ".mp3", ".woff", ".woff2",
    ".ttf", ".eot", ".ico", ".exe", ".dmg", ".apk", ".csv",
}
PAGE_EXTENSIONS = {".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".jspx"}

CSS_URL_RE = re.compile(r"url\(\s*(?:['\"])?(.*?)(?:['\"])?\s*\)", re.I)


def normalize_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a website URL first.")
    if len(value) > 2048:
        raise ValueError("That URL is too long.")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http:// and https:// URLs are supported.")
    if not parsed.hostname or not parsed.netloc:
        raise ValueError("Enter a valid website URL, such as https://example.com.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing usernames or passwords are not supported.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("That URL contains an invalid port number.") from exc
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))


def ensure_public_host(url: str) -> None:
    """Block localhost and non-public IP destinations, including redirect targets."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError("The website URL does not contain a hostname.")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local and private-network addresses are not supported.")

    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ValueError("Could not resolve that website. Check the URL and try again.") from exc
        addresses = {ipaddress.ip_address(info[4][0]) for info in infos}

    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("This website resolves to a private or restricted address and cannot be scanned.")


def read_limited(response: requests.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=16_384):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise ValueError("The page or image is too large to scan safely.")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_page(url: str, timeout_seconds: float = TIMEOUT_SECONDS) -> tuple[str, str, bytes]:
    """Fetch one public URL, checking every redirect target before requesting it."""
    current = url
    session = requests.Session()
    session.trust_env = False
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            ensure_public_host(current)
            try:
                response = session.get(
                    current,
                    headers=headers,
                    timeout=timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise ValueError("Could not open that website. It may be blocking requests or taking too long.") from exc

            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise ValueError("The website sent an invalid redirect.")
                if redirect_count >= MAX_REDIRECTS:
                    raise ValueError("This website redirected too many times.")
                current = normalize_url(urljoin(current, location))
                continue

            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise ValueError(f"The website responded with HTTP {status}.")

            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type.startswith("image/"):
                # We only need the URL and content type for direct images, not their bytes.
                response.close()
                return current, content_type, b""
            try:
                body = read_limited(response, MAX_PAGE_BYTES)
            except requests.RequestException as exc:
                raise ValueError("The website took too long while sending its page. Try again or use a gallery URL.") from exc
            finally:
                response.close()
            return current, content_type, body

        raise ValueError("Could not follow the website redirect.")
    finally:
        session.close()


def parse_srcset(value: str) -> list[str]:
    """Extract candidate URLs from a typical srcset string."""
    candidates: list[str] = []
    if not value:
        return candidates
    # srcset descriptors are whitespace-separated; data URLs are intentionally ignored later.
    for item in value.split(","):
        parts = item.strip().split()
        if parts:
            candidates.append(parts[0])
    return candidates


def is_image_file_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in IMAGE_EXTENSIONS)


def extract_images(page_url: str, html: bytes) -> list[dict[str, str]]:
    """Extract image candidates from HTML attributes, metadata, JSON-LD, and CSS."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict[str, str]] = {}

    def add(raw_url: object, alt: str = "", image_hint: bool = False) -> None:
        if raw_url is None or isinstance(raw_url, (dict, list)):
            return
        raw = str(raw_url).strip().strip("\"'")
        if not raw or raw.startswith(("data:", "blob:", "javascript:", "#")):
            return
        if raw.startswith("//"):
            raw = urlparse(page_url).scheme + ":" + raw
        absolute = urljoin(page_url, raw)
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return
        # For links or loose JSON strings, avoid treating arbitrary pages as photos.
        if image_hint and not is_image_file_url(absolute):
            return
        if absolute not in found:
            caption = (alt or "Photo from website").strip()[:160]
            found[absolute] = {"url": absolute, "alt": caption}

    # img tags: normal, responsive, and common lazy-load attributes.
    image_attrs = {
        "src", "data-src", "data-lazy-src", "data-original", "data-original-src",
        "data-image", "data-image-src", "data-full", "data-full-src", "data-zoom-image",
        "data-hi-res-src", "data-large-file", "data-orig-file", "data-thumb",
        "data-thumbnail", "data-fallback-src", "data-lazy", "data-img-src",
        "data-actualsrc", "data-cfsrc", "data-defer-src",
    }
    srcset_attrs = {"srcset", "data-srcset", "data-lazy-srcset", "data-original-srcset"}

    for tag in soup.find_all(["img", "source", "input"]):
        if tag.name == "input" and str(tag.get("type", "")).lower() != "image":
            continue
        alt = tag.get("alt") or tag.get("title") or tag.get("aria-label") or ""
        for attr in image_attrs:
            if tag.get(attr):
                add(tag.get(attr), alt)
        for attr in srcset_attrs:
            for candidate in parse_srcset(str(tag.get(attr) or "")):
                add(candidate, alt)
        # Several CMSs use differently named data-* attributes; accept only values with image-like file paths.
        for attr, value in tag.attrs.items():
            attr_name = str(attr).lower()
            if attr_name.startswith("data-") and any(term in attr_name for term in ("image", "photo", "picture", "original", "full", "large", "lazy", "thumb", "src")):
                if isinstance(value, str) and value not in image_attrs and value not in srcset_attrs:
                    if attr_name.endswith("srcset"):
                        for candidate in parse_srcset(value):
                            add(candidate, alt)
                    else:
                        add(value, alt, image_hint=True)

    # Video posters can be visual content too.
    for tag in soup.find_all(["video"]):
        add(tag.get("poster"), tag.get("title") or tag.get("aria-label") or "Video poster")

    # Image links and anchors to downloadable image files.
    for tag in soup.find_all("a", href=True):
        href = str(tag.get("href", "")).strip()
        if href:
            absolute = urljoin(page_url, href)
            if is_image_file_url(absolute):
                add(href, tag.get("title") or tag.get_text(" ", strip=True), image_hint=True)

    # OpenGraph, Twitter, standard itemprop metadata, and other image hints.
    meta_selectors = [
        ("property", "og:image"), ("property", "og:image:url"), ("property", "og:image:secure_url"),
        ("name", "twitter:image"), ("name", "twitter:image:src"),
        ("itemprop", "image"), ("name", "image"),
    ]
    for attr, value in meta_selectors:
        for tag in soup.find_all("meta", attrs={attr: value}):
            add(tag.get("content"), tag.get("alt") or tag.get("title") or "Featured image")
    for tag in soup.find_all("link", href=True):
        rels = {str(item).lower() for item in (tag.get("rel") or [])}
        if "image_src" in rels or ("preload" in rels and str(tag.get("as", "")).lower() == "image"):
            add(tag.get("href"), tag.get("title") or "Featured image")

    # Any element may declare an itemprop image or a CSS background.
    for tag in soup.find_all(True):
        itemprop = str(tag.get("itemprop", "")).lower().split()
        if "image" in itemprop:
            for attr in ("content", "src", "href", "data-src"):
                if tag.get(attr):
                    add(tag.get(attr), tag.get("alt") or tag.get("title") or "Page image")
        style = tag.get("style")
        if isinstance(style, str):
            for match in CSS_URL_RE.finditer(style):
                add(match.group(1).strip().strip("\"'"), tag.get("aria-label") or tag.get("title") or "Background image")

    # CSS background images in style blocks.
    for style_tag in soup.find_all("style"):
        css = style_tag.string or style_tag.get_text(" ", strip=True)
        for match in CSS_URL_RE.finditer(css):
            add(match.group(1).strip().strip("\"'"), "Background image")

    # Structured data often stores image arrays or ImageObject records.
    def walk_json(value: object, key_hint: str = "") -> None:
        image_keys = {"image", "images", "thumbnailurl", "contenturl", "primaryimageofpage", "logo"}
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key).lower()
                if name in image_keys or "image" in name or "thumbnail" in name:
                    if isinstance(child, str):
                        add(child, "Structured-data image")
                    else:
                        walk_json(child, name)
                elif key_hint in image_keys and name in {"url", "contenturl", "@id", "src"} and isinstance(child, str):
                    add(child, "Structured-data image")
                else:
                    walk_json(child, name)
        elif isinstance(value, list):
            for child in value:
                walk_json(child, key_hint)
        elif isinstance(value, str) and key_hint in image_keys:
            add(value, "Structured-data image")

    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw_json = script.string or script.get_text()
        try:
            walk_json(json.loads(raw_json))
        except (json.JSONDecodeError, TypeError):
            continue

    return list(found.values())[:MAX_IMAGES_TO_KEEP]


def get_cached_scan(start_url: str) -> tuple[str, list[dict[str, str]], int] | None:
    """Return an unexpired scan result for this URL, if one is available."""
    now = time.monotonic()
    with _SCAN_CACHE_LOCK:
        expired = [
            key for key, entry in _SCAN_CACHE.items()
            if now - entry[0] >= SCAN_CACHE_TTL_SECONDS
        ]
        for key in expired:
            _SCAN_CACHE.pop(key, None)

        entry = _SCAN_CACHE.get(start_url)
        if entry is None:
            return None
        _SCAN_CACHE.move_to_end(start_url)
        # Return a shallow copy of the list so callers cannot mutate the cache.
        return entry[1], list(entry[2]), entry[3]


def store_cached_scan(
    start_url: str,
    page_url: str,
    images: list[dict[str, str]],
    pages_scanned: int,
) -> None:
    """Store a successful scan in a bounded, process-local cache."""
    now = time.monotonic()
    with _SCAN_CACHE_LOCK:
        _SCAN_CACHE[start_url] = (now, page_url, list(images), pages_scanned)
        _SCAN_CACHE.move_to_end(start_url)
        while len(_SCAN_CACHE) > MAX_CACHED_SCANS:
            _SCAN_CACHE.popitem(last=False)


def same_site_host(left: str, right: str) -> bool:
    def host_key(url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        return host[4:] if host.startswith("www.") else host
    return bool(host_key(left)) and host_key(left) == host_key(right)


def extract_page_links(page_url: str, html: bytes) -> list[tuple[int, str]]:
    """Return a relevance-ranked list of same-site HTML-looking links to crawl."""
    soup = BeautifulSoup(html, "html.parser")
    candidates: dict[str, int] = {}
    priority_terms = ("gallery", "galleries", "photo", "photos", "image", "images", "portfolio", "media", "album", "product", "collection", "artwork", "work")
    ignored_terms = ("logout", "signout", "javascript:", "mailto:", "tel:")

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", "")).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(page_url, href)
        try:
            absolute = normalize_url(absolute)
        except ValueError:
            continue
        parsed = urlparse(absolute)
        if parsed.scheme not in {"http", "https"} or not same_site_host(page_url, absolute):
            continue
        path = parsed.path.lower()
        suffix = next((ext for ext in NON_PAGE_EXTENSIONS if path.endswith(ext)), "")
        if suffix or is_image_file_url(absolute):
            continue
        # If a path has a file extension, only crawl common server-rendered page extensions.
        if "." in path.rsplit("/", 1)[-1]:
            extension = "." + path.rsplit(".", 1)[-1]
            if extension not in PAGE_EXTENSIONS:
                continue
        description = " ".join([
            href,
            str(anchor.get_text(" ", strip=True)),
            str(anchor.get("title") or ""),
            str(anchor.get("aria-label") or ""),
        ]).lower()
        if any(term in description for term in ignored_terms):
            continue
        priority = sum(4 for term in priority_terms if term in description)
        if parsed.path.rstrip("/") == urlparse(page_url).path.rstrip("/") and parsed.query == urlparse(page_url).query:
            continue
        candidates[absolute] = max(candidates.get(absolute, -1), priority)

    return sorted(((score, url) for url, score in candidates.items()), key=lambda item: (-item[0], item[1]))


def scan_site(start_url: str) -> tuple[str, list[dict[str, str]], int]:
    """Scan the entered page plus a capped, relevance-ranked selection of same-site pages."""
    start = time.monotonic()
    frontier: list[tuple[int, str]] = [(10_000, start_url)]
    queued_or_seen = {start_url}
    visited: set[str] = set()
    images: dict[str, dict[str, str]] = {}
    first_final_url = start_url
    max_frontier = 60

    while frontier and len(visited) < MAX_PAGES_TO_SCAN and time.monotonic() - start < MAX_SCAN_SECONDS:
        frontier.sort(key=lambda item: (-item[0], item[1]))
        _priority, current = frontier.pop(0)
        if current in visited:
            continue
        visited.add(current)
        remaining_seconds = MAX_SCAN_SECONDS - (time.monotonic() - start)
        if remaining_seconds <= 0:
            break
        try:
            final_url, content_type, body = fetch_page(current, timeout_seconds=max(0.5, min(TIMEOUT_SECONDS, remaining_seconds)))
        except ValueError:
            if current == start_url:
                raise
            continue

        if len(visited) == 1:
            first_final_url = final_url

        if content_type.startswith("image/"):
            images.setdefault(final_url, {"url": final_url, "alt": "Image from the URL"})
            break

        if content_type and "html" not in content_type and "xhtml" not in content_type:
            continue

        for candidate in extract_images(final_url, body):
            images.setdefault(candidate["url"], candidate)
            if len(images) >= MAX_IMAGES_TO_KEEP:
                break

        # Crawl likely gallery/product pages before generic links. Keep a bounded frontier.
        for score, linked_url in extract_page_links(final_url, body)[:25]:
            if linked_url in queued_or_seen or linked_url in visited:
                continue
            queued_or_seen.add(linked_url)
            frontier.append((score, linked_url))
        if len(frontier) > max_frontier:
            frontier.sort(key=lambda item: (-item[0], item[1]))
            frontier = frontier[:max_frontier]

    if not images:
        raise ValueError(
            "No usable photos were found on the scanned pages. Try a gallery page or a different URL. "
            "Some sites only load images in JavaScript or behind a login."
        )
    return first_final_url, list(images.values()), len(visited)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/random-image")
def random_image():
    payload = request.get_json(silent=True) or {}
    try:
        start_url = normalize_url(str(payload.get("url", "")))
        cached_scan = get_cached_scan(start_url)
        if cached_scan is not None:
            # Re-check DNS/IP safety even on cache hits, in case the host changed.
            ensure_public_host(start_url)
            page_url, images, pages_scanned = cached_scan
            cache_hit = True
        else:
            page_url, images, pages_scanned = scan_site(start_url)
            store_cached_scan(start_url, page_url, images, pages_scanned)
            cache_hit = False

        exclude_value = payload.get("exclude", [])
        if not isinstance(exclude_value, list):
            exclude_value = []
        excluded = {str(item) for item in exclude_value[:750] if isinstance(item, str)}
        choices = [item for item in images if item["url"] not in excluded]
        if not choices:
            choices = images
        picked = random.choice(choices)
        return jsonify({
            "ok": True,
            "site": page_url,
            "image": picked,
            "image_count": len(images),
            "pages_scanned": pages_scanned,
            "cache_hit": cache_hit,
            "message": "Picked from cached scan results." if cache_hit else "Found a random image from the scanned pages.",
        })
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception:
        app.logger.exception("Unexpected error while scanning a website")
        return jsonify({"ok": False, "error": "Something went wrong while scanning that website. Try another page."}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
