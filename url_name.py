"""Helpers to derive / fetch a retailer product name from a matched URL."""

from __future__ import annotations

import re
from urllib.parse import urlparse


def slug_from_product_url(url: str) -> str:
    """Extract the product slug from a DuringDays, Kogan, or BigW product URL."""
    if not url or url == "-":
        return ""
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]

    # During Days / Shopify-style: .../products/<slug>
    if "products" in parts:
        try:
            i = parts.index("products")
            if i + 1 < len(parts):
                return parts[i + 1]
        except ValueError:
            pass

    # BigW: .../product/<slug>/p/<id>
    if "product" in parts:
        try:
            i = parts.index("product")
            if i + 1 < len(parts):
                return parts[i + 1]
        except ValueError:
            pass

    # Kogan: .../au/buy/<slug>/
    if "buy" in parts:
        try:
            i = parts.index("buy")
            if i + 1 < len(parts):
                return parts[i + 1]
        except ValueError:
            pass

    return parts[-1] if parts else ""


def name_from_url(url: str) -> str:
    """
    Build a human-readable product name from the URL slug.
    e.g. zenses-massage-table-3-fold-brown → Zenses Massage Table 3 Fold Brown
    """
    slug = slug_from_product_url(url)
    if not slug:
        return "-"
    # keep alphanumerics and hyphens; drop trailing junk / query leftovers
    slug = re.sub(r"[^a-zA-Z0-9\-]+", "-", slug).strip("-")
    words = [w for w in slug.split("-") if w]
    if not words:
        return "-"
    # Title-case words but preserve all-caps tokens like LED, BMW, CM sizes mixed
    out = []
    for w in words:
        if w.isupper() or (w.isdigit()):
            out.append(w)
        elif re.fullmatch(r"\d+[a-zA-Z]+", w) or re.fullmatch(r"[a-zA-Z]+\d+", w):
            out.append(w.upper() if len(w) <= 4 else w.capitalize())
        else:
            out.append(w.capitalize())
    return " ".join(out)


def title_from_product_url(url: str, *, fetch_live: bool = False) -> str:
    """
    Resolve a product Title from a During Days (or other) product URL.
    Prefer slug-derived title; optionally enrich from the live page.
    """
    if not url or not str(url).strip().startswith("http"):
        return ""
    url = str(url).strip()
    if fetch_live:
        live = fetch_name_from_page(url)
        if live:
            # Strip common storefront suffixes
            live = re.split(r"\s*[\|–—-]\s*During\s*Days", live, flags=re.I)[0].strip()
            if live:
                return live
    name = name_from_url(url)
    return "" if name in {"", "-"} else name


def fetch_name_from_page(url: str, timeout: float = 12.0) -> str | None:
    """
    Try to pull the real product name from the live page (JSON-LD or <title>).
    Uses curl_cffi (same stack that works for BigW sitemaps). Returns None on failure.
    """
    if not url or url == "-":
        return None
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None

    try:
        resp = creq.get(
            url,
            impersonate="chrome",
            timeout=timeout,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-AU,en;q=0.9",
            },
        )
        if resp.status_code != 200:
            return None
        html = resp.text
    except Exception:
        return None

    # JSON-LD Product.name
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    import json

    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        name = _find_product_name(data)
        if name:
            return name

    # og:title
    m = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()

    # <title>
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        # strip site suffix e.g. " … | BIG W"
        title = re.split(r"\s*\|\s*", title)[0].strip()
        if title:
            return title
    return None


def _find_product_name(obj) -> str | None:
    if isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else ([t] if t else [])
        types_l = {str(x).lower() for x in types}
        if "product" in types_l and obj.get("name"):
            return str(obj["name"]).strip()
        if "@graph" in obj:
            found = _find_product_name(obj["@graph"])
            if found:
                return found
        for v in obj.values():
            found = _find_product_name(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_product_name(item)
            if found:
                return found
    return None


def resolve_product_name(url: str, fetch_live: bool = False) -> str:
    """Prefer live page name when requested; always fall back to slug-derived name."""
    if fetch_live:
        live = fetch_name_from_page(url)
        if live:
            return live
    return name_from_url(url)
