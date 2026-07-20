"""Extract and clean competitor product URLs from messy bulk dumps."""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse, urlunparse

from lxml import etree

# --- Kogan high-precision patterns ---
_KOGAN_BUY = re.compile(
    r"https?://(?:www\.)?kogan\.com/au/buy/[a-zA-Z0-9][a-zA-Z0-9\-._%]*/?",
    re.I,
)
_KOGAN_ANY = re.compile(
    r"https?://(?:www\.)?kogan\.com/au/[^\s<>\"'\]]+",
    re.I,
)
_KOGAN_BRAND_PAGE = re.compile(
    r"https?://(?:www\.)?kogan\.com/au/([a-z0-9][a-z0-9\-]*)/?(?:\d{4}-\d{2}-\d{2})?",
    re.I,
)
_KOGAN_BRAND_CATEGORY = re.compile(
    r"https?://(?:www\.)?kogan\.com/au/([a-z0-9][a-z0-9\-]*)/shop/category/",
    re.I,
)

_BIGW_PRODUCT = re.compile(
    r"https?://(?:www\.)?bigw\.com\.au/product/[a-zA-Z0-9][a-zA-Z0-9\-._%]*/p/\d+",
    re.I,
)

_ANY_HTTP = re.compile(r"https?://[^\s<>\"'\]]+", re.I)
_LOC_TAG = re.compile(
    r"<(?:[\w-]+:)?loc[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</(?:[\w-]+:)?loc>",
    re.I | re.S,
)
# Split concatenated sitemap rows: .../slug/2026-07-18https://...
_DATE_THEN_URL = re.compile(r"(?<=\d{4}-\d{2}-\d{2})(?=https?://)", re.I)

_ASSET_HOSTS = (
    "assets.kogan.com",
    "cdn.",
    "images.",
    "img.",
    "static.",
    "captcha-delivery.com",
)

_KOGAN_RESERVED_SEGMENTS = frozenset({
    "shop", "buy", "help", "account", "cart", "checkout", "search", "blog",
    "about", "contact", "privacy", "terms", "api", "marketplace", "deals",
    "brands", "brand", "category", "categories", "collection", "collections",
    "sitemap", "plus", "login", "register", "orders", "wishlist", "seller",
    "sell", "support", "faq", "mobile", "app", "news", "press", "careers",
    "affiliate", "rewards", "gift", "giftcards", "services", "trade",
})

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


def _local_tag(tag: str) -> str:
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    if ":" in tag:
        return tag.rsplit(":", 1)[-1]
    return tag


def _prepare_raw(raw: str) -> str:
    """Normalise pasted sitemap dumps: HTML escapes, BOM, wrappers, concatenated rows."""
    raw = raw.strip()
    if raw.startswith("\ufeff"):
        raw = raw[1:]

    # Browser "View Source" saved as HTML, or copied escaped markup
    if "&lt;" in raw and any(k in raw.lower() for k in ("loc", "urlset", "url>", "sitemap")):
        raw = html.unescape(raw)

    if re.search(r"<html[\s>]", raw, re.I):
        pre = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.I | re.S)
        if pre:
            raw = html.unescape(pre.group(1).strip())

    return _preprocess_raw(raw)


def _extract_xml_locs(raw: str) -> list[str]:
    """
    Parse structured XML sitemaps (<urlset>, <loc>, namespaces, CDATA, multiline).
    """
    if not any(k in raw.lower() for k in ("<urlset", "<url", "<sitemapindex", "<?xml", "<loc")):
        return []

    locs: list[str] = []
    try:
        content = raw.encode("utf-8")
        root = etree.fromstring(content, parser=etree.XMLParser(recover=True, huge_tree=True))
        for el in root.iter():
            if _local_tag(el.tag) == "loc" and el.text:
                url = el.text.strip()
                if url.startswith("http"):
                    locs.append(url)
    except Exception:
        pass

    if locs:
        return locs

    # Regex fallback when XML is malformed but still has loc blocks
    for match in _LOC_TAG.finditer(raw):
        url = html.unescape(match.group(1).strip())
        url = re.sub(r"\s+", "", url)
        if url.startswith("http"):
            locs.append(url)
    return locs


def _preprocess_raw(raw: str) -> str:
    """Insert breaks before URLs jammed after ISO dates in sitemap dumps."""
    return _DATE_THEN_URL.sub("\n", raw)


def _strip_trailing_junk(url: str) -> str:
    """Remove dates / concatenated next-URL stuck after a path."""
    m = re.search(r"https?://", url[8:], re.I)
    if m:
        url = url[: 8 + m.start()]

    url = re.sub(r"/\d{4}-\d{2}-\d{2}$", "/", url)
    url = re.sub(r"(?<![/\d])\d{4}-\d{2}-\d{2}$", "", url)
    url = url.rstrip(".,;)]}>\"'")
    return url


def _normalize_url(url: str, *, trailing_slash: bool = True) -> str | None:
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
        if not re.search(r"/product/.+/p/\d+", low) and not re.search(r"/au/buy/", low):
            return None

    path = path.rstrip("/")
    if trailing_slash and path:
        path = path + "/"
    clean = urlunparse((parsed.scheme.lower(), host, path, "", "", ""))
    if clean.startswith("http://"):
        clean = "https://" + clean[len("http://") :]
    return clean


def _kogan_path_segments(path: str) -> list[str]:
    return [p for p in path.strip("/").split("/") if p]


def _kogan_brand_slug(path: str) -> str | None:
    """
    Extract brand slug from:
      /au/{brand}/
      /au/{brand}/shop/category/...
    """
    parts = _kogan_path_segments(path)
    if len(parts) < 2 or parts[0].lower() != "au":
        return None
    brand = parts[1].lower()
    if brand in _KOGAN_RESERVED_SEGMENTS:
        return None
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]*", brand):
        return None
    return brand


def _classify_kogan_url(url: str) -> tuple[str, str | None]:
    """
    Returns (kind, brand_slug).
    kind: product | brand | brand_category | category | collection | other
    """
    path = urlparse(url).path.lower()
    if "/au/buy/" in path:
        return "product", None
    if "/shop/collection/" in path or "/collections/" in path:
        return "collection", None
    if re.search(r"/au/[^/]+/shop/category/", path):
        return "brand_category", _kogan_brand_slug(path)
    if "/shop/category/" in path:
        return "category", None
    brand = _kogan_brand_slug(path)
    if brand and len(_kogan_path_segments(path)) == 2:
        return "brand", brand
    return "other", brand


def _is_product_for_target(url: str, target: str, custom_re: re.Pattern | None) -> bool:
    if custom_re is not None:
        return bool(custom_re.search(url))
    t = target.lower()
    u = url.lower()
    if t in ("kogan", "kogan.com"):
        return "/au/buy/" in u and "kogan.com" in u
    if t in ("bigw", "bigw.com.au"):
        return "/product/" in u and "/p/" in u and "bigw.com.au" in u
    return any(
        p in u
        for p in ("/product/", "/products/", "/au/buy/", "/buy/", "/p/", "/dp/")
    )


def _collect_candidates(raw: str) -> list[str]:
    raw = _prepare_raw(raw)
    found: list[str] = []

    # Structured XML sitemaps first — most accurate for <loc> blocks
    xml_locs = _extract_xml_locs(raw)
    if xml_locs:
        found.extend(xml_locs)

    found.extend(_KOGAN_BUY.findall(raw))
    found.extend(_KOGAN_ANY.findall(raw))
    found.extend(_BIGW_PRODUCT.findall(raw))

    if not xml_locs:
        found.extend(_LOC_TAG.findall(raw))

    for piece in re.split(r"[\s,;|]+", raw):
        if piece.startswith("http"):
            found.append(piece)
    found.extend(_ANY_HTTP.findall(raw))
    return found


def extract_bulk_catalogue(
    raw: str,
    target: str = "kogan",
    product_pattern: str | None = None,
) -> dict:
    """
    Clean Kogan/BigW bulk dumps including:
      - structured XML sitemaps (<urlset> / <loc> tags, namespaces, CDATA)
      - concatenated browser sitemap dumps (URL + date + next URL)
      - marketplace product sitemaps (/au/buy/…)
      - brand sitemaps (/au/{brand}/…)
      - brand-category sitemaps (/au/{brand}/shop/category/…)
      - category / collection sitemaps (stored separately; brands extracted)

    Returns product URLs for matching plus optional Kogan brand slugs.
    """
    if not raw or not raw.strip():
        return {
            "urls": [],
            "brands": [],
            "categories": [],
            "brand_categories": [],
            "collections": [],
            "brand_pages": [],
            "extracted": 0,
            "unique": 0,
            "brand_count": 0,
            "filtered": {},
            "samples": [],
            "brand_samples": [],
            "rejected_samples": [],
        }

    custom_re = re.compile(product_pattern, re.I) if product_pattern else None
    is_kogan = target.lower() in ("kogan", "kogan.com", "kogan au")

    candidates = _collect_candidates(raw)
    products: list[str] = []
    brands: set[str] = set()
    categories: list[str] = []
    brand_categories: list[dict] = []
    collections: list[str] = []
    brand_pages: list[str] = []
    seen_products: set[str] = set()
    seen_norm: set[str] = set()
    seen_categories: set[str] = set()
    seen_brand_categories: set[str] = set()
    seen_collections: set[str] = set()
    seen_brand_pages: set[str] = set()
    rejected: list[str] = []
    filtered: dict[str, int] = {
        "categories": 0,
        "brand_categories": 0,
        "collections": 0,
        "brand_pages": 0,
        "other": 0,
    }

    for cand in candidates:
        norm = _normalize_url(cand)
        if not norm:
            if len(rejected) < 8 and cand.startswith("http"):
                rejected.append(cand[:120])
            continue

        if norm in seen_norm:
            continue
        seen_norm.add(norm)

        if is_kogan and "kogan.com" in norm.lower():
            kind, brand_slug = _classify_kogan_url(norm)
            if brand_slug:
                brands.add(brand_slug)
            if kind == "product":
                if norm not in seen_products and _is_product_for_target(norm, target, custom_re):
                    seen_products.add(norm)
                    products.append(norm)
                continue
            if kind == "category":
                filtered["categories"] += 1
                if norm not in seen_categories:
                    seen_categories.add(norm)
                    categories.append(norm)
            elif kind == "brand_category":
                filtered["brand_categories"] += 1
                if norm not in seen_brand_categories:
                    seen_brand_categories.add(norm)
                    brand_categories.append({"brand": brand_slug or "", "url": norm})
            elif kind == "collection":
                filtered["collections"] += 1
                if norm not in seen_collections:
                    seen_collections.add(norm)
                    collections.append(norm)
            elif kind == "brand":
                filtered["brand_pages"] += 1
                if norm not in seen_brand_pages:
                    seen_brand_pages.add(norm)
                    brand_pages.append(norm)
            else:
                filtered["other"] += 1
            continue

        # BigW / custom / non-kogan
        if _is_product_for_target(norm, target, custom_re):
            if norm not in seen_products:
                seen_products.add(norm)
                products.append(norm)
        elif len(rejected) < 8:
            rejected.append(norm[:120])

    brand_list = sorted(brands)
    return {
        "urls": products,
        "brands": brand_list,
        "categories": categories,
        "brand_categories": brand_categories,
        "collections": collections,
        "brand_pages": brand_pages,
        "extracted": len(seen_norm),
        "unique": len(products),
        "brand_count": len(brand_list),
        "filtered": filtered,
        "samples": products[:12],
        "brand_samples": brand_list[:12],
        "rejected_samples": rejected[:8],
    }


def extract_product_urls(
    raw: str,
    target: str = "kogan",
    product_pattern: str | None = None,
) -> dict:
    """Backward-compatible wrapper — product URLs only in 'urls'."""
    result = extract_bulk_catalogue(raw, target=target, product_pattern=product_pattern)
    return {
        "urls": result["urls"],
        "brands": result.get("brands", []),
        "categories": result.get("categories", []),
        "brand_categories": result.get("brand_categories", []),
        "collections": result.get("collections", []),
        "brand_pages": result.get("brand_pages", []),
        "extracted": result["extracted"],
        "unique": result["unique"],
        "brand_count": result.get("brand_count", 0),
        "filtered": result.get("filtered", {}),
        "samples": result["samples"],
        "brand_samples": result.get("brand_samples", []),
        "rejected_samples": result["rejected_samples"],
    }
