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


def _norm_pack_count(n: int) -> str:
    """Canonical pack token used in attributes / hard_attrs (e.g. 6pack)."""
    return f"{int(n)}pack"


def pack_count_aliases(n: int) -> list[str]:
    """Slug forms that all mean the same multipack quantity."""
    n = int(n)
    return [
        f"{n}pack",
        f"{n}pk",
        f"{n}pcs",
        f"{n}pc",
        f"{n}set",
        f"{n}bottle",
        f"{n}bottles",
        f"packof{n}",
        f"pack-of-{n}",
        f"{n}-pack",
        f"{n}x",
    ]


def parse_pack_count_value(value: str) -> int | None:
    """Extract integer pack quantity from a normalised pack attr like '6pack'."""
    if not value:
        return None
    m = re.match(r"(\d+)", str(value).lower().strip())
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 240 else None


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

        # dimensions e.g. 120x60x40cm or 120 x 60 cm (before Nxvolume so cm dims win)
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

        # Multipack × volume: 6x750ml / 12 x 750 ml / 6×750mL (wine cases etc.)
        nx_vol = re.search(
            r"\b(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(ml|l|litre|liter)\b",
            self.title_norm,
            re.IGNORECASE,
        )
        if nx_vol and "dimensions" not in attrs:
            pack_n = int(nx_vol.group(1))
            if 2 <= pack_n <= 240:
                pack = _norm_pack_count(pack_n)
                attrs["pack_count"] = pack
                hard["pack_count"] = pack
                attr_words.add(str(pack_n))
                attr_words.update(pack_count_aliases(pack_n))
            vol_norm = _norm_volume(float(nx_vol.group(2)), nx_vol.group(3))
            attrs["volume"] = vol_norm
            hard["volume"] = vol_norm
            attr_words.add(nx_vol.group(2))
            attr_words.add(nx_vol.group(3).lower())
            attr_words.add(vol_norm)

        # volume (single bottle / unit) — skip if already set from Nxvolume
        if "volume" not in attrs:
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

        # pack / bottle / case quantity (hard discriminator for wine & multipacks)
        if "pack_count" not in attrs:
            pack_n: int | None = None
            pack_patterns = [
                re.compile(r"\bpack\s*of\s*(\d+)\b", re.I),
                re.compile(r"\bcase\s*of\s*(\d+)\b", re.I),
                re.compile(r"\b(\d+)\s*[\-]?\s*pack\b", re.I),
                re.compile(r"\b(\d+)\s*(?:pcs|pieces?|pk|set)\b", re.I),
                re.compile(r"\b(\d+)\s*bottles?\b", re.I),
                re.compile(r"\b(\d+)\s*cans?\b", re.I),
            ]
            for pat in pack_patterns:
                m = pat.search(self.title_norm)
                if m:
                    pack_n = int(m.group(1))
                    break
            if pack_n is None and re.search(r"\bdozen\b", self.title_norm):
                pack_n = 12
            if pack_n is not None and 2 <= pack_n <= 240:
                pack = _norm_pack_count(pack_n)
                attrs["pack_count"] = pack
                hard["pack_count"] = pack
                attr_words.add(str(pack_n))
                attr_words.update(pack_count_aliases(pack_n))
                attr_words.update({"pack", "case", "bottle", "bottles", "dozen"})

        # Explicit single unit when volume present but no multipack
        # (stops 750ml singles matching 6x750ml cases)
        if "pack_count" not in attrs and "volume" in attrs:
            if re.search(
                r"\b(bottle|single|each|unit)\b",
                self.title_norm,
            ) or not re.search(
                r"\b(pack|case|dozen|bottles|cans|multipack|multi\s*pack)\b",
                self.title_norm,
            ):
                attrs["pack_count"] = "1pack"
                hard["pack_count"] = "1pack"
                attr_words.update({"1pack", "1pk", "bottle", "single"})

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
            # pack_count aliases are resolved inside matcher._attr_in_candidate
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
