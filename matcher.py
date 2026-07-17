"""Stage 3: score sitemap candidates and classify MATCH / PARTIAL / NOT FOUND."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from config import (
    MATCH_THRESHOLD,
    PARTIAL_THRESHOLD,
    W_FUZZY,
    W_MODEL_TOKEN,
    W_VARIANT_ATTR,
    WRITE_PARTIAL_URL,
)
from product_query import ProductQuery

# Bed-size hierarchy: a bare "king" must not match a "super king" slug.
_SIZE_SLUG_FORMS = {
    "superking": {"superking", "super-king"},
    "kingsingle": {"kingsingle", "king-single"},
    "king": {"king"},
    "queen": {"queen"},
    "double": {"double"},
    "single": {"single"},
}


def _slug_gsm_values(tokens: set[str], slug_l: str) -> set[str]:
    found = {t for t in tokens if re.fullmatch(r"\d+gsm", t)}
    found.update(re.findall(r"\d+gsm", slug_l))
    return found


def _slug_size(tokens: set[str], slug_l: str) -> str | None:
    """Detect the most specific bed size present in a candidate slug."""
    joined = slug_l.replace("_", "-")
    if "superking" in tokens or "super-king" in joined or (
        "super" in tokens and "king" in tokens
    ):
        return "superking"
    if "kingsingle" in tokens or "king-single" in joined or (
        "king" in tokens and "single" in tokens and "super" not in tokens
    ):
        # king+single without super → kingsingle; careful with unrelated "single"
        if "kingsingle" in tokens or "king-single" in joined:
            return "kingsingle"
    if "queen" in tokens or re.search(r"(^|-)queen(-|$)", joined):
        return "queen"
    if "double" in tokens or re.search(r"(^|-)double(-|$)", joined):
        return "double"
    if "single" in tokens or re.search(r"(^|-)single(-|$)", joined):
        # avoid treating kingsingle leftovers
        if "king" not in tokens:
            return "single"
    if "king" in tokens or re.search(r"(^|-)king(-|$)", joined):
        return "king"
    return None


def _attr_in_candidate(attr: str, tokens: set[str], slug_l: str, attr_key: str | None = None) -> bool:
    """
    Check attribute presence without naive substring traps
    (e.g. 'king' must not match inside 'superking' / 'super-king').
    """
    attr = attr.lower()

    if attr_key == "size" or attr in _SIZE_SLUG_FORMS:
        size = attr if attr in _SIZE_SLUG_FORMS else attr
        slug_size = _slug_size(tokens, slug_l)
        return slug_size == size

    if attr.endswith("gsm") or (attr_key == "gsm"):
        return attr in tokens or attr in _slug_gsm_values(tokens, slug_l)

    # token exact match preferred
    if attr in tokens:
        return True

    # whole-segment match in hyphenated slug (not raw substring)
    if re.search(rf"(^|-){re.escape(attr)}(-|$)", slug_l):
        return True

    # compound attrs like 120x60cm may appear without separators
    if attr in slug_l and not attr.isalpha():
        return True

    return False


def _conflicts_hard_attrs(
    pq: ProductQuery, tokens: set[str], slug_l: str
) -> str | None:
    """
    Return a short reason if the candidate contradicts a hard source attribute.
    """
    hard = pq.hard_attrs or {}

    if "gsm" in hard:
        needed = hard["gsm"]
        present = _slug_gsm_values(tokens, slug_l)
        if present and needed not in present:
            return f"gsm conflict (need {needed}, slug has {', '.join(sorted(present))})"
        # also reject if another gsm number appears as bare digits+gsm in slug
        for g in present:
            if g != needed:
                return f"gsm conflict (need {needed}, slug has {g})"

    if "size" in hard:
        needed = hard["size"]
        slug_size = _slug_size(tokens, slug_l)
        if slug_size and slug_size != needed:
            return f"size conflict (need {needed}, slug has {slug_size})"

    if "volume" in hard:
        needed = hard["volume"]
        # conflicting volumes like 400ml vs 500ml
        vols = {t for t in tokens if re.fullmatch(r"\d+ml", t) or re.fullmatch(r"\d+l", t)}
        vols.update(re.findall(r"\d+ml", slug_l))
        if vols and needed not in vols and not any(
            _attr_in_candidate(needed, tokens, slug_l, "volume") for _ in [0]
        ):
            if not _attr_in_candidate(needed, tokens, slug_l, "volume"):
                # only conflict when a *different* volume is clearly present
                other = {v for v in vols if v != needed}
                if other:
                    return f"volume conflict (need {needed}, slug has {', '.join(sorted(other))})"

    if "watts" in hard:
        needed = hard["watts"]
        watts = {t for t in tokens if re.fullmatch(r"\d+w", t)}
        watts.update(re.findall(r"\d+w", slug_l))
        if watts and needed not in watts:
            return f"watts conflict (need {needed}, slug has {', '.join(sorted(watts))})"

    if "fill" in hard:
        needed = hard["fill"]
        rivals = {"goose", "duck", "microfibre", "microfiber", "wool", "silk"}
        present = (tokens | set(re.findall(r"[a-z]+", slug_l))) & rivals
        if "microfiber" in present:
            present.discard("microfiber")
            present.add("microfibre")
        # Another fill type is on the slug → hard reject
        if present and needed not in present:
            return f"fill conflict (need {needed}, slug has {', '.join(sorted(present))})"

    return None


@dataclass
class MatchResult:
    status: str  # MATCH | PARTIAL | NOT FOUND
    url: str
    score: float
    candidates_considered: int
    note: str = ""
    best_url: str = ""  # always the top candidate (for run_log / partial review)


def match_product(
    pq: ProductQuery,
    records: dict,
    inverted_index: dict,
    site: str,
) -> MatchResult:
    brand = (pq.brand or "").lower()
    if not brand or brand not in inverted_index:
        return MatchResult(
            status="NOT FOUND",
            url="-",
            score=0.0,
            candidates_considered=0,
            note=f"no brand match in {site} sitemap",
            best_url="-",
        )

    candidate_urls = inverted_index[brand]
    # Map attr value -> key for special matching rules
    attr_key_by_val: dict[str, str] = {}
    for k, v in (pq.attributes or {}).items():
        if v:
            attr_key_by_val[v.lower()] = k
            if v.lower() == "grey":
                attr_key_by_val["gray"] = k
            if v.lower() == "gray":
                attr_key_by_val["grey"] = k

    attr_strings = pq.all_attr_strings()
    model_tokens = [t.lower() for t in pq.model_tokens]

    scored: list[tuple[float, str, set[str], set[str]]] = []
    considered = 0

    for url in candidate_urls:
        rec = records.get(url)
        if not rec:
            continue
        tokens: set[str] = rec["tokens"]
        slug: str = rec["slug"]
        slug_spaced = slug.replace("-", " ")
        slug_l = slug.lower()

        # Hard reject conflicting GSM / size / volume / watts siblings
        conflict = _conflicts_hard_attrs(pq, tokens, slug_l)
        if conflict:
            continue

        considered += 1
        score = 0.0
        matched_attrs: set[str] = set()

        for attr in attr_strings:
            key = attr_key_by_val.get(attr)
            if _attr_in_candidate(attr, tokens, slug_l, key):
                score += W_VARIANT_ATTR
                matched_attrs.add(attr)
                # size aliases
                if attr == "superking":
                    matched_attrs.add("superking")
                if key == "gsm":
                    matched_attrs.add(attr)

        for tok in model_tokens:
            if tok in tokens:
                score += W_MODEL_TOKEN

        fuzzy = fuzz.token_sort_ratio(pq.title_norm, slug_spaced)
        score += W_FUZZY * fuzzy

        # Required attr values (canonical), accounting for grey/gray
        required = set()
        for k, v in (pq.attributes or {}).items():
            if not v:
                continue
            required.add(v.lower())
        missing = set()
        for req in required:
            ok = False
            variants = {req}
            if req == "grey":
                variants.add("gray")
            if req == "gray":
                variants.add("grey")
            for a in variants:
                key = attr_key_by_val.get(a) or attr_key_by_val.get(req)
                if a in matched_attrs or _attr_in_candidate(a, tokens, slug_l, key):
                    ok = True
                    break
            if not ok:
                missing.add(req)

        scored.append((score, url, matched_attrs, missing))

    if not scored:
        return MatchResult(
            status="NOT FOUND",
            url="-",
            score=0.0,
            candidates_considered=considered,
            note=(
                f"no viable candidates in {site} sitemap "
                f"(brand hits filtered by GSM/size/variant conflicts)"
                if considered == 0 and brand in inverted_index
                else f"no brand match in {site} sitemap"
            ),
            best_url="-",
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_url, matched_attrs, missing = scored[0]
    n = len(scored)

    if best_score >= MATCH_THRESHOLD and not missing:
        return MatchResult(
            status="MATCH",
            url=best_url,
            score=best_score,
            candidates_considered=n,
            note="brand + variant attrs confirmed in URL slug",
            best_url=best_url,
        )

    if best_score >= PARTIAL_THRESHOLD:
        miss_list = ", ".join(sorted(missing)) if missing else "n/a"
        note = (
            f"base product matched, variant unclear "
            f"({miss_list} not found in candidate slug); "
            f"best-guess={best_url}"
        )
        return MatchResult(
            status="PARTIAL",
            url=best_url if WRITE_PARTIAL_URL else "-",
            score=best_score,
            candidates_considered=n,
            note=note,
            best_url=best_url,
        )

    return MatchResult(
        status="NOT FOUND",
        url="-",
        score=best_score,
        candidates_considered=n,
        note=f"score {best_score:.1f} below PARTIAL_THRESHOLD",
        best_url=best_url,
    )
