"""Persist and browse Kogan cleaned sitemap data (brands, categories, etc.)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from config import CACHE_DIR, SITES

KOGAN_BRANDS_PATH = CACHE_DIR / "kogan_brands.json"
KOGAN_CLEANED_PATH = CACHE_DIR / "kogan_cleaned.json"

DATA_TYPES = ("products", "brands", "categories", "brand_categories", "collections", "brand_pages")


def _empty_payload() -> dict:
    return {
        "fetched_at": None,
        "brands": [],
        "categories": [],
        "brand_categories": [],
        "collections": [],
        "brand_pages": [],
    }


def _migrate_legacy_brands(data: dict) -> dict:
    """Merge legacy kogan_brands.json into cleaned payload if needed."""
    if data.get("brands"):
        return data
    if not KOGAN_BRANDS_PATH.exists():
        return data
    try:
        legacy = json.loads(KOGAN_BRANDS_PATH.read_text(encoding="utf-8"))
        data["brands"] = list(legacy.get("brands") or [])
    except Exception:
        pass
    return data


def load_kogan_cleaned() -> dict:
    if not KOGAN_CLEANED_PATH.exists():
        return _migrate_legacy_brands(_empty_payload())
    try:
        data = json.loads(KOGAN_CLEANED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _migrate_legacy_brands(_empty_payload())
    for key in ("brands", "categories", "collections", "brand_pages"):
        data.setdefault(key, [])
    data.setdefault("brand_categories", [])
    return _migrate_legacy_brands(data)


def _save_kogan_cleaned(data: dict) -> dict:
    KOGAN_CLEANED_PATH.parent.mkdir(parents=True, exist_ok=True)
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    KOGAN_CLEANED_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Keep legacy brands file in sync for older code paths
    KOGAN_BRANDS_PATH.write_text(
        json.dumps(
            {
                "fetched_at": data["fetched_at"],
                "brands": data.get("brands", []),
                "count": len(data.get("brands", [])),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return data


def merge_kogan_cleaned(incoming: dict, *, merge: bool = True) -> dict:
    """
    Merge cleaned rows from a bulk upload.
    incoming keys: brands, categories, brand_categories, collections, brand_pages
    brand_categories items: {"brand": str, "url": str}
    """
    current = _empty_payload() if not merge else load_kogan_cleaned()

    brand_set = set(current.get("brands") or [])
    brand_set.update(incoming.get("brands") or [])
    current["brands"] = sorted(brand_set)

    for key in ("categories", "collections", "brand_pages"):
        existing = set(current.get(key) or [])
        existing.update(incoming.get(key) or [])
        current[key] = sorted(existing)

    # brand_categories dedupe by url
    bc_map: dict[str, dict] = {}
    for item in current.get("brand_categories") or []:
        if isinstance(item, dict) and item.get("url"):
            bc_map[item["url"]] = item
    for item in incoming.get("brand_categories") or []:
        if isinstance(item, dict) and item.get("url"):
            bc_map[item["url"]] = {"brand": item.get("brand") or "", "url": item["url"]}
    current["brand_categories"] = sorted(bc_map.values(), key=lambda x: x["url"])

    return _save_kogan_cleaned(current)


def load_kogan_brands() -> list[str]:
    return list(load_kogan_cleaned().get("brands") or [])


def save_kogan_brands(brands: list[str], *, merge: bool = True) -> dict:
    result = merge_kogan_cleaned({"brands": brands}, merge=merge)
    return {
        "fetched_at": result["fetched_at"],
        "brands": result["brands"],
        "count": len(result["brands"]),
    }


def _product_count() -> int:
    cache_path = SITES["kogan"]["cache"]
    if not cache_path.exists():
        return 0
    try:
        return len(json.loads(cache_path.read_text(encoding="utf-8")).get("product_urls", []))
    except Exception:
        return 0


def kogan_cleaned_summary() -> dict:
    data = load_kogan_cleaned()
    return {
        "source_id": "kogan",
        "fetched_at": data.get("fetched_at"),
        "counts": {
            "products": _product_count(),
            "brands": len(data.get("brands") or []),
            "categories": len(data.get("categories") or []),
            "brand_categories": len(data.get("brand_categories") or []),
            "collections": len(data.get("collections") or []),
            "brand_pages": len(data.get("brand_pages") or []),
        },
    }


def _load_products() -> list[str]:
    cache_path = SITES["kogan"]["cache"]
    if not cache_path.exists():
        return []
    try:
        return list(json.loads(cache_path.read_text(encoding="utf-8")).get("product_urls", []))
    except Exception:
        return []


def browse_kogan_cleaned(
    data_type: str,
    *,
    offset: int = 0,
    limit: int = 50,
    q: str = "",
) -> dict:
    if data_type not in DATA_TYPES:
        raise ValueError(f"Unknown data type: {data_type}")

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    q = (q or "").strip().lower()

    data = load_kogan_cleaned()

    if data_type == "products":
        items: list[Any] = _load_products()
    elif data_type == "brands":
        items = list(data.get("brands") or [])
    elif data_type == "brand_categories":
        items = list(data.get("brand_categories") or [])
    elif data_type == "categories":
        items = list(data.get("categories") or [])
    elif data_type == "collections":
        items = list(data.get("collections") or [])
    else:  # brand_pages
        items = list(data.get("brand_pages") or [])

    if q:
        if data_type == "brand_categories":
            items = [
                i
                for i in items
                if q in (i.get("url") or "").lower() or q in (i.get("brand") or "").lower()
            ]
        else:
            items = [i for i in items if q in str(i).lower()]

    total = len(items)
    page = items[offset : offset + limit]

    return {
        "type": data_type,
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": page,
        "has_more": offset + limit < total,
    }
