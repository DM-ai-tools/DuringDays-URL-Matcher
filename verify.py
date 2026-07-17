"""Stage 4: JSON-LD verification of confirmed MATCH URLs."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz

from config import HEADERS, VERIFY_DELAY_SEC, VERIFY_FUZZY_MIN, VERIFY_TIMEOUT_SEC
from product_query import ProductQuery

_last_fetch_at = 0.0


@dataclass
class VerifyResult:
    status: str  # VERIFIED | PARTIAL | MATCH (unconfirmed)
    title: str  # retailer product name or "-"
    price: str  # offer price or "-"
    note: str = ""
    brand: str = ""
    sku: str = ""


def _polite_sleep() -> None:
    global _last_fetch_at
    elapsed = time.time() - _last_fetch_at
    if _last_fetch_at and elapsed < VERIFY_DELAY_SEC:
        time.sleep(VERIFY_DELAY_SEC - elapsed)


def _walk_jsonld(obj, found: list) -> None:
    if isinstance(obj, dict):
        t = obj.get("@type")
        types = t if isinstance(t, list) else ([t] if t else [])
        types_l = {str(x).lower() for x in types}
        if "product" in types_l:
            found.append(obj)
        if "@graph" in obj:
            _walk_jsonld(obj["@graph"], found)
        for v in obj.values():
            _walk_jsonld(v, found)
    elif isinstance(obj, list):
        for item in obj:
            _walk_jsonld(item, found)


def _extract_price(product: dict) -> str:
    offers = product.get("offers") or product.get("Offers")
    if isinstance(offers, list) and offers:
        offers = offers[0]
    if not isinstance(offers, dict):
        return "-"
    price = offers.get("price") or offers.get("lowPrice")
    if price is None:
        return "-"
    return str(price)


def _extract_brand(product: dict) -> str:
    brand = product.get("brand")
    if isinstance(brand, dict):
        return str(brand.get("name") or "")
    if isinstance(brand, str):
        return brand
    return ""


def _parse_ldjson_blocks(html: str) -> list[dict]:
    import re

    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    products: list[dict] = []
    for raw in blocks:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        _walk_jsonld(data, products)
    return products


def verify_match(url: str, pq: ProductQuery) -> VerifyResult:
    """
    Fetch product page, parse JSON-LD Product, confirm brand + attrs vs page name.
    Never raises — returns a soft result on failure.
    """
    global _last_fetch_at

    html = ""
    for attempt in range(2):
        try:
            _polite_sleep()
            with httpx.Client(
                headers=HEADERS,
                timeout=VERIFY_TIMEOUT_SEC,
                follow_redirects=True,
            ) as client:
                resp = client.get(url)
                _last_fetch_at = time.time()
                if resp.status_code != 200:
                    if attempt == 0:
                        time.sleep(2)
                        continue
                    return VerifyResult(
                        status="MATCH",
                        title="-",
                        price="-",
                        note=f"verify: unconfirmed (HTTP {resp.status_code})",
                    )
                html = resp.text
                break
        except Exception as exc:
            _last_fetch_at = time.time()
            if attempt == 0:
                time.sleep(2)
                continue
            return VerifyResult(
                status="MATCH",
                title="-",
                price="-",
                note=f"verify: unconfirmed ({exc})",
            )

    products = _parse_ldjson_blocks(html)
    if not products:
        return VerifyResult(
            status="MATCH",
            title="-",
            price="-",
            note="verify: unconfirmed (no structured data)",
        )

    product = products[0]
    name = str(product.get("name") or "")
    brand = _extract_brand(product)
    price = _extract_price(product)
    sku = str(product.get("sku") or "")

    name_l = name.lower()
    fuzzy = fuzz.token_set_ratio(pq.title_norm, name_l) if name else 0

    brand_ok = True
    if pq.brand:
        brand_ok = (
            pq.brand.lower() in name_l
            or pq.brand.lower() in brand.lower()
        )

    missing_attrs = [
        a for a in pq.all_attr_strings() if a not in name_l and a not in name_l.replace(" ", "")
    ]
    # grey/gray alias
    if "grey" in missing_attrs and "gray" in name_l:
        missing_attrs = [a for a in missing_attrs if a != "grey"]
    if "gray" in missing_attrs and "grey" in name_l:
        missing_attrs = [a for a in missing_attrs if a != "gray"]

    if fuzzy >= VERIFY_FUZZY_MIN and brand_ok and not missing_attrs:
        return VerifyResult(
            status="VERIFIED",
            title=name or "-",
            price=price,
            note="verified via JSON-LD",
            brand=brand,
            sku=sku,
        )

    detail_parts = []
    if fuzzy < VERIFY_FUZZY_MIN:
        detail_parts.append(f"fuzzy={fuzzy:.0f}<{VERIFY_FUZZY_MIN}")
    if not brand_ok:
        detail_parts.append(f"brand '{pq.brand}' not in page name/brand")
    if missing_attrs:
        detail_parts.append(f"attrs missing in name: {', '.join(missing_attrs)}")

    return VerifyResult(
        status="PARTIAL",
        title=name or "-",
        price=price if price else "-",
        note=f"verify: page variant mismatch ({'; '.join(detail_parts)})",
        brand=brand,
        sku=sku,
    )
