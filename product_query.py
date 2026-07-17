"""Parse a DuringDays product Title into brand / model tokens / variant attributes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import COLOURS, KNOWN_BRANDS, SIZES, STOPWORDS


def _norm_volume(num: float, unit: str) -> str:
    """
    Normalise volumes for slug matching.
    Small litre amounts (diffusers etc.) -> ml (0.5l -> 500ml).
    Large litre amounts (storage boxes etc.) stay as Nl — that's how slugs write them.
    """
    unit = unit.lower()
    if unit in {"l", "litre", "liter"}:
        if num >= 10:
            if num == int(num):
                return f"{int(num)}l"
            return f"{num}l".rstrip("0").rstrip(".") + "l" if "." in str(num) else f"{num}l"
        ml = int(round(num * 1000))
        return f"{ml}ml"
    if num == int(num):
        return f"{int(num)}ml"
    return f"{num}ml".replace(".0ml", "ml")


def build_brand_set(titles: list[str] | None = None) -> set[str]:
    """
    Known brands = seed list UNION leading tokens that appear on >= 2 titles.
    """
    brands = {b.lower() for b in KNOWN_BRANDS}
    if not titles:
        return brands
    counts: dict[str, int] = {}
    for title in titles:
        if not title:
            continue
        first = title.strip().split()[0].lower().strip(",.?!")
        if first:
            counts[first] = counts.get(first, 0) + 1
    for tok, n in counts.items():
        if n >= 2:
            brands.add(tok)
    return brands


# Bed sizes longest-first so "super king" wins over bare "king"
_BED_SIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bking\s*single\b|\bkingsingle\b"), "kingsingle"),
    (re.compile(r"\bsuper\s*king\b|\bsuperking\b"), "superking"),
    (re.compile(r"\bqueen\b"), "queen"),
    (re.compile(r"\bdouble\b"), "double"),
    (re.compile(r"\bsingle\b"), "single"),
    # bare king only if not already captured as super/king-single
    (re.compile(r"\bking\b"), "king"),
]


@dataclass
class ProductQuery:
    title: str
    brand: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    model_tokens: list[str] = field(default_factory=list)
    title_norm: str = ""
    brand_set: set[str] = field(default_factory=lambda: set(KNOWN_BRANDS), repr=False)
    # Attributes that must match exactly (conflicts → reject candidate)
    hard_attrs: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_title(cls, title: str, brand_set: set[str] | None = None) -> "ProductQuery":
        pq = cls(title=title or "", brand_set=brand_set or set(KNOWN_BRANDS))
        pq._parse()
        return pq

    def _parse(self) -> None:
        raw = self.title or ""
        self.title_norm = raw.lower().replace("-", " ")
        tokens = re.findall(r"[a-z0-9.]+", self.title_norm)

        # --- brand ---
        self.brand = ""
        if tokens:
            first = tokens[0]
            if first in self.brand_set:
                self.brand = first
            else:
                if len(tokens) >= 2:
                    two = f"{tokens[0]} {tokens[1]}"
                    if two.replace(" ", "") in {b.replace(" ", "") for b in self.brand_set}:
                        self.brand = tokens[0]
                if not self.brand:
                    self.brand = first

        attrs: dict[str, str] = {}
        hard: dict[str, str] = {}
        attr_words: set[str] = set()

        # colour
        for t in tokens:
            if t in COLOURS:
                attrs.setdefault("colour", t)
                attr_words.add(t)
                break

        # bed / product size — longest compound first
        for pat, size_val in _BED_SIZE_PATTERNS:
            if pat.search(self.title_norm):
                attrs["size"] = size_val
                hard["size"] = size_val
                attr_words.update(re.findall(r"[a-z0-9]+", size_val))
                if size_val == "superking":
                    attr_words.update({"super", "king"})
                elif size_val == "kingsingle":
                    attr_words.update({"king", "single"})
                break
        else:
            # non-bed generic sizes from vocab (small/medium/large/xl/cot/…)
            for t in tokens:
                if t in SIZES and t not in {"king", "super", "single", "queen", "double"}:
                    attrs.setdefault("size", t)
                    attr_words.add(t)

        # fill / material type (goose vs duck quilts etc.) — hard discriminator
        fill_pat = re.search(
            r"\b(goose|duck|microfibre|microfiber|wool|silk|bamboo)\b",
            self.title_norm,
        )
        if fill_pat:
            fill = fill_pat.group(1)
            if fill == "microfiber":
                fill = "microfibre"
            attrs["fill"] = fill
            hard["fill"] = fill
            attr_words.add(fill)
            if fill == "microfibre":
                attr_words.add("microfiber")

        # GSM (quilt fill weight) — hard variant (must run before wattage)
        m = re.search(r"(\d+)\s*gsm\b", self.title_norm)
        if m:
            gsm = f"{int(m.group(1))}gsm"
            attrs["gsm"] = gsm
            hard["gsm"] = gsm
            attr_words.add(m.group(1))
            attr_words.add("gsm")
            attr_words.add(gsm)

        # wattage e.g. 1000W / 500W (avoid matching inside Ngsm)
        m = re.search(r"(\d+)\s*w(?:att)?s?\b", self.title_norm)
        if m and "gsm" not in attrs:
            watts = f"{int(m.group(1))}w"
            attrs["watts"] = watts
            hard["watts"] = watts
            attr_words.add(m.group(1))
            attr_words.add("w")
            attr_words.add(watts)
            attr_words.add("watt")
            attr_words.add("watts")

        # volume
        vol_re = re.compile(
            r"(\d+(?:\.\d+)?)\s?(ml|l|litre|liter)\b", re.IGNORECASE
        )
        m = vol_re.search(self.title_norm)
        if m:
            norm = _norm_volume(float(m.group(1)), m.group(2))
            attrs["volume"] = norm
            hard["volume"] = norm
            attr_words.add(m.group(1))
            attr_words.add(m.group(2).lower())
            attr_words.add(norm)

        # dimensions e.g. 120x60x40cm or 120 x 60 cm
        dim_re = re.compile(
            r"(\d+)\s?x\s?(\d+)(?:\s?x\s?(\d+))?\s?cm\b", re.IGNORECASE
        )
        m = dim_re.search(self.title_norm)
        if m:
            parts = [m.group(1), m.group(2)] + ([m.group(3)] if m.group(3) else [])
            dim = "x".join(parts) + "cm"
            attrs["dimensions"] = dim
            hard["dimensions"] = dim
            attr_words.update(parts)
            attr_words.add("cm")
            attr_words.add(dim)

        # pack count
        pack_re = re.compile(
            r"(\d+)\s?(pcs|pack|piece|pieces|pk|set)\b", re.IGNORECASE
        )
        m = pack_re.search(self.title_norm)
        if m:
            n = m.group(1)
            unit = m.group(2).lower()
            if unit in {"piece", "pieces", "pcs", "pk"}:
                slug_forms = [f"{n}pcs", f"{n}pack", f"{n}pk"]
            else:
                slug_forms = [f"{n}{unit}", f"{n}pack"]
            attrs["pack_count"] = slug_forms[0]
            hard["pack_count"] = slug_forms[0]
            attr_words.add(n)
            attr_words.add(unit)
            attr_words.update(slug_forms)

        # weight
        wt_re = re.compile(r"(\d+(?:\.\d+)?)\s?kg\b", re.IGNORECASE)
        m = wt_re.search(self.title_norm)
        if m:
            num = m.group(1)
            if float(num) == int(float(num)):
                norm = f"{int(float(num))}kg"
            else:
                norm = f"{num}kg"
            attrs["weight"] = norm
            hard["weight"] = norm
            attr_words.add(num)
            attr_words.add("kg")
            attr_words.add(norm)

        self.attributes = attrs
        self.hard_attrs = hard

        # --- model tokens ---
        skip = set(STOPWORDS) | attr_words | {self.brand}
        skip |= COLOURS | SIZES | {"gsm", "super", "king"}
        model: list[str] = []
        for t in tokens:
            if t in skip:
                continue
            if t.isdigit():
                continue
            if len(t) < 2:
                continue
            # skip tokens that are pure attr compounds like 700gsm
            if re.fullmatch(r"\d+gsm", t) or re.fullmatch(r"\d+ml", t) or re.fullmatch(r"\d+kg", t):
                continue
            model.append(t)
        self.model_tokens = model

    def all_attr_strings(self) -> set[str]:
        """Normalised attribute values to look for in retailer slugs."""
        out: set[str] = set()
        for key, v in self.attributes.items():
            if not v:
                continue
            v = v.lower()
            out.add(v)
            if v == "grey":
                out.add("gray")
            elif v == "gray":
                out.add("grey")
            # size aliases for slug forms
            if key == "size" and v == "superking":
                out.add("superking")
            if key == "size" and v == "kingsingle":
                out.add("kingsingle")
        return out


if __name__ == "__main__":
    samples = [
        "Giselle Bedding 700GSM Goose Down Feather Quilt King",
        "Giselle Bedding 700GSM Goose Down Feather Quilt Super King",
        "Giselle Bedding 500GSM Goose Down Feather Quilt Super King",
        "Devanti Aroma Diffuser Aromatherapy Humidifier 500ml White",
        "Giantz 1000 Watt Step Down Transformer",
    ]
    brands = build_brand_set(samples)
    for title in samples:
        pq = ProductQuery.from_title(title, brand_set=brands)
        print(f"TITLE: {title}")
        print(f"  brand      : {pq.brand}")
        print(f"  attributes : {pq.attributes}")
        print(f"  hard_attrs : {pq.hard_attrs}")
        print(f"  attr_str   : {pq.all_attr_strings()}")
        print(f"  model      : {pq.model_tokens}")
        print()
