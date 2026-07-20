"""Extract and clean competitor product URLs from messy bulk dumps."""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

# Product URL patterns (site-specific first, then generic)
_KOGAN_BUY = re.compile(
    r"https?://(?:www\.)?kogan\.com/au/buy/[a-zA-Z0-9][a-zA-Z0-9\-._%]*/?",
    re.I,
)
_BIGW_PRODUCT = re.compile(
    r"https?://(?:www\.)?bigw\.com\.au/product/[a-zA-Z0-9][a-zA-Z0-9\-._%]*/p/\d+",
    re.I,
)
# Catch any http(s) URL in concatenated text (fallback)
_ANY_HTTP = re.compile(r"https?://[^\s<>\"'\]]+", re.I)
# XML <loc> tags
_LOC_TAG = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)

_ASSET_HOSTS = (
    "assets.kogan.com",
    "cdn.",
    "images.",
    "img.",
    "static.",
)
_SKIP_PATH_FRAGMENTS = (
    "/sitemap",
    "/image",
    "/images/",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".svg",
    ".css",
    ".js",
)


def _strip_trailing_junk(url: str) -> str:
    """Remove dates / concatenated next-URL stuck after a product path."""
    # Cut at second https:// if present (concatenated dump)
    m = re.search(r"https?://", url[8:], re.I)
    if m:
        url = url[: 8 + m.start()]

    # Strip trailing ISO date jammed after path: .../slug/2026-07-08
    url = re.sub(r"/\d{4}-\d{2}-\d{2}$", "/", url)
    # Strip trailing date without leading slash
    url = re.sub(r"(?<![/\d])\d{4}-\d{2}-\d{2}$", "", url)

    # Trim trailing punctuation / XML leftovers
    url = url.rstrip(".,;)]}>\"'")
    return url


def _normalize_product_url(url: str) -> str | None:
    url = _strip_trailing_junk(url.strip())
    if not url.startswith("http"):
        return None

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    low = (host + path).lower()

    if any(h in host for h in _ASSET_HOSTS):
        return None
    if any(frag in low for frag in _SKIP_PATH_FRAGMENTS):
        # Allow /product/ paths that aren't image files
        if not re.search(r"/product/.+/p/\d+", low) and not re.search(r"/au/buy/", low):
            return None

    # Drop query/fragment for matching stability (product identity is in path)
    clean = urlunparse((parsed.scheme.lower(), host, path.rstrip("/") + "/", "", "", ""))
    # Prefer https
    if clean.startswith("http://"):
        clean = "https://" + clean[len("http://") :]
    return clean


def _is_product_for_target(url: str, target: str, custom_re: re.Pattern | None) -> bool:
    if custom_re is not None:
        return bool(custom_re.search(url))
    t = target.lower()
    u = url.lower()
    if t in ("kogan", "kogan.com"):
        return "/au/buy/" in u and "kogan.com" in u
    if t in ("bigw", "bigw.com.au"):
        return "/product/" in u and "/p/" in u and "bigw.com.au" in u
    # Custom catalogue: common product path heuristics
    return any(
        p in u
        for p in ("/product/", "/products/", "/au/buy/", "/buy/", "/p/", "/dp/")
    )


def extract_product_urls(
    raw: str,
    target: str = "kogan",
    product_pattern: str | None = None,
) -> dict:
    """
    Clean a bulk dump (concatenated text, line lists, or sitemap XML)
    into unique product URLs for the given target catalogue.
    """
    if not raw or not raw.strip():
        return {
            "urls": [],
            "extracted": 0,
            "unique": 0,
            "samples": [],
            "rejected_samples": [],
        }

    custom_re = re.compile(product_pattern, re.I) if product_pattern else None
    found: list[str] = []
    rejected: list[str] = []

    # Always run high-precision extractors first — dumps are often concatenated
    # without whitespace (product URL + date + asset URL stuck together).
    found.extend(_KOGAN_BUY.findall(raw))
    found.extend(_BIGW_PRODUCT.findall(raw))

    # XML <loc> values
    found.extend(_LOC_TAG.findall(raw))

    # Line / whitespace separated pieces
    for piece in re.split(r"[\s,;|]+", raw):
        if piece.startswith("http"):
            found.append(piece)

    # Fallback any-http (may grab concatenated blobs — normalize will trim)
    found.extend(_ANY_HTTP.findall(raw))

    unique: list[str] = []
    seen: set[str] = set()
    for cand in found:
        norm = _normalize_product_url(cand)
        if not norm:
            if len(rejected) < 8 and cand.startswith("http"):
                rejected.append(cand[:120])
            continue
        if not _is_product_for_target(norm, target, custom_re):
            if len(rejected) < 8:
                rejected.append(norm[:120])
            continue
        if norm in seen:
            continue
        seen.add(norm)
        unique.append(norm)

    return {
        "urls": unique,
        "extracted": len(found),
        "unique": len(unique),
        "samples": unique[:12],
        "rejected_samples": rejected[:8],
    }
