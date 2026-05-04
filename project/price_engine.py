import re
from typing import Any, Dict, Optional


def _extract_discount_amount(offers) -> float:
    total_discount = 0.0
    if not offers:
        return total_discount
    for offer in offers:
        nums = re.findall(r"\d+(?:\.\d+)?", offer or "")
        if nums:
            total_discount += float(nums[0])
    return total_discount


def _to_base_unit(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in {"kg", "l"}:
        return value * 1000
    return value


def _base_unit_name(unit: str) -> str:
    return "g" if unit.lower() in {"g", "gm", "kg"} else "ml"


def parse_quantity_details(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Parse quantity formats:
    - 150g + 150g
    - 150g x 2 / 150g*2
    - 2 x 150g
    - Pack of 4 (150g each)
    - 600g pack of 4
    """
    if not text:
        return None
    raw = text.lower()
    compact = re.sub(r"\s+", "", raw)

    # 150g+150g(+150g...)
    plus = re.findall(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)", compact)
    if "+" in compact and len(plus) >= 2:
        units = len(plus)
        first_val, first_unit = float(plus[0][0]), plus[0][1]
        total = sum(_to_base_unit(float(v), u) for v, u in plus)
        qpu = _to_base_unit(first_val, first_unit)
        return {
            "units": units,
            "quantity_per_unit": qpu,
            "total_quantity": total,
            "unit": _base_unit_name(first_unit),
        }

    # 150gx4
    m = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)[x*](\d+)", compact)
    if m:
        qpu = _to_base_unit(float(m.group(1)), m.group(2))
        units = int(m.group(3))
        return {
            "units": units,
            "quantity_per_unit": qpu,
            "total_quantity": qpu * units,
            "unit": _base_unit_name(m.group(2)),
        }

    # 4x150g
    m = re.search(r"(\d+)[x*](\d+(?:\.\d+)?)(g|gm|kg|ml|l)", compact)
    if m:
        units = int(m.group(1))
        qpu = _to_base_unit(float(m.group(2)), m.group(3))
        return {
            "units": units,
            "quantity_per_unit": qpu,
            "total_quantity": qpu * units,
            "unit": _base_unit_name(m.group(3)),
        }

    # pack of N variants: "pack of 4", "pack-4", "pack4"
    pack = re.search(r"pack(?:of)?[-:]?(\d+)", compact)
    each = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)each", compact)
    if pack and each:
        units = int(pack.group(1))
        qpu = _to_base_unit(float(each.group(1)), each.group(2))
        return {
            "units": units,
            "quantity_per_unit": qpu,
            "total_quantity": qpu * units,
            "unit": _base_unit_name(each.group(2)),
        }

    # pack of N with explicit per-unit quantity, e.g. "150g pack of 4"
    if pack:
        first_qty = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)", compact)
        if first_qty:
            units = int(pack.group(1))
            qpu = _to_base_unit(float(first_qty.group(1)), first_qty.group(2))
            return {
                "units": units,
                "quantity_per_unit": qpu,
                "total_quantity": qpu * units,
                "unit": _base_unit_name(first_qty.group(2)),
            }

    # total qty + pack count: 600g pack of 4
    total_match = re.search(r"(\d+(?:\.\d+)?)(g|gm|kg|ml|l)", compact)
    if total_match:
        total = _to_base_unit(float(total_match.group(1)), total_match.group(2))
        units = int(pack.group(1)) if pack else 1
        qpu = total / units if units > 0 else total
        return {
            "units": units,
            "quantity_per_unit": qpu,
            "total_quantity": total,
            "unit": _base_unit_name(total_match.group(2)),
        }

    return None


def calculate_price_metrics(scraped_data: Dict[str, Any], requested_quantity: Optional[str] = None) -> Dict[str, Any]:
    base_price = float(scraped_data["price"])
    discount = _extract_discount_amount(scraped_data.get("offers"))
    final_price = max(base_price - discount, 0.0)

    title = scraped_data.get("title", "")
    qty_text = scraped_data.get("quantity", "")
    pack_count = scraped_data.get("pack_count")

    details = parse_quantity_details(title) or parse_quantity_details(qty_text)
    if details and pack_count and details["units"] == 1 and pack_count > 1:
        # Apply pack multiplication only when the parsed text itself does not
        # already express a combo/pack. This avoids 600g being multiplied again.
        source_text = f"{title} {qty_text}".lower()
        has_pack_or_combo = bool(
            re.search(r"pack\s*[-:]?\s*(?:of\s*)?\d+", source_text)
            or re.search(r"\d+\s*[x*]\s*\d+(?:\.\d+)?\s*(?:g|gm|kg|ml|l)", source_text)
            or re.search(r"\d+(?:\.\d+)?\s*(?:g|gm|kg|ml|l)\s*[x*]\s*\d+", source_text)
        )
        if not has_pack_or_combo:
            # If quantity says 150g and page says pack of 4 -> 600g
            details["units"] = int(pack_count)
            details["total_quantity"] = float(details["quantity_per_unit"]) * int(pack_count)

    requested_details = parse_quantity_details(requested_quantity) if requested_quantity else None
    requested_qty = requested_details["quantity_per_unit"] if requested_details else None
    if details and requested_qty and pack_count and int(pack_count) > 1:
        # Disambiguate "600g pack of 4" style text:
        # if parsed as 600 each (2400 total) but requested is 150,
        # reinterpret as 600 total (4x150).
        qpu = float(details["quantity_per_unit"])
        alt_qpu = qpu / float(pack_count)
        tol = max(1.0, float(requested_qty) * 0.08)
        if abs(qpu - float(requested_qty)) > tol and abs(alt_qpu - float(requested_qty)) <= tol:
            details["units"] = int(pack_count)
            details["quantity_per_unit"] = alt_qpu
            details["total_quantity"] = qpu

    total_qty = details["total_quantity"] if details else None
    unit = details["unit"] if details else None
    price_per_unit = (final_price / total_qty) if total_qty else None
    effective_price = (price_per_unit * requested_qty) if price_per_unit and requested_qty else None

    return {
        "base_price": base_price,
        "discount": discount,
        "final_price": final_price,
        "quantity_details": details,
        "total_quantity": total_qty,
        "unit": unit,
        "price_per_unit": price_per_unit,
        "requested_quantity": requested_qty,
        "effective_price": effective_price,
    }
