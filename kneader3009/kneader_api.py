

import frappe
from frappe import _
import re
import string
from datetime import datetime, timedelta

# -------------------- Helpers --------------------

def _parse_batch_date(batch_no: str):
    """
    Robust parse:
    - If batch looks like YY<LETTER>DD... (e.g. 25K15...), parse month/year/day.
    - Otherwise return a safe wide range (last 90 days -> today).
    """
    import re
    from datetime import datetime, timedelta

    b = str(batch_no or "").strip()
    # pattern: two digits, letter, two digits (e.g. 25K15)
    m = re.match(r"^(\d{2})([A-Za-z])(\d{2})", b)
    if not m:
        today = datetime.today()
        start = (today - timedelta(days=90)).date()
        end = today.date()
        return start, end

    try:
        year = int("20" + m.group(1))
        month_letter = m.group(2).upper()
        day = int(m.group(3))

        # map letter -> month (A=1, B=2, ...)
        import string
        month = string.ascii_uppercase.index(month_letter) + 1
        # guard month range
        if month < 1 or month > 12:
            raise ValueError("month letter out of range")

        current_date = datetime(year, month, day)

        first_day_current = current_date.replace(day=1)
        prev_month_last_day = first_day_current - timedelta(days=1)
        first_day_prev = prev_month_last_day.replace(day=1)

        return first_day_prev.date(), current_date.date()
    except Exception:
        today = datetime.today()
        start = (today - timedelta(days=90)).date()
        end = today.date()
        return start, end


def _extract_item(row: dict):
    """Return the item value from a produces_items / mapping row."""
    if not row:
        return None
    for key in ("item_code", "item", "final_item", "item_code1"):
        if key in row and row.get(key):
            return str(row.get(key)).strip()
    for k, v in row.items():
        if "item" in k.lower() and v:
            return str(v).strip()
    return None


# -------------------- Step: Batch -> Stock Entry -> BOM -> final_item --------------------

def _get_final_production_item_internal(batch_no: str):
    """
    Resolve batch_no -> Stock Entry -> choose correct item_code row ->
    find BOM -> return final_item and related meta.
    """

    batch_no = str(batch_no or "").strip()
    if not batch_no:
        return None

    start_date, end_date = _parse_batch_date(batch_no)

    entries = frappe.get_all(
        "Stock Entry",
        filters=[
            ["Stock Entry Detail", "batch_no", "=", batch_no],
            ["posting_date", "between", [start_date, end_date]],
        ],
        fields=["name", "posting_date"],
        order_by="posting_date desc",
        limit_page_length=5,
    )

    if not entries:
        return None

    se_name = entries[0]["name"]
    se = frappe.get_doc("Stock Entry", se_name)

    # --------- Robust item_code selection (prefer matching batch row) ---------
    item_code_full = None

    items = getattr(se, "items", []) or []

    # 1) Prefer row whose batch_no equals requested batch
    for row in items:
        r_batch = getattr(row, "batch_no", None)
        if r_batch and str(r_batch).strip() == batch_no:
            item_code_full = getattr(row, "item_code", None)
            break

    # 2) Prefer item_code that looks like production prefix (B_12345)
    if not item_code_full:
        pref_re = re.compile(r"B[_-]?\d+", re.IGNORECASE)
        for row in items:
            ic = getattr(row, "item_code", "") or ""
            if ic and pref_re.search(ic):
                item_code_full = ic
                break

    # 3) Fallback to first row
    if not item_code_full and items:
        item_code_full = getattr(items[0], "item_code", None)

    if not item_code_full:
        return None

    # Extract prefix for BOM
    prefix_match = re.search(r"B[_-]?\d+", item_code_full or "")
    item_code_prefix = prefix_match.group(0) if prefix_match else (item_code_full.split()[0] if item_code_full else "")

    # Try to find BOM by item (exact or prefix) or by BOM Item child table
    # 1) Try BOM where BOM.item like prefix% (normal case)
    boms = []
    if item_code_prefix:
        boms = frappe.get_all(
            "BOM",
            filters={
                "item": ["like", f"{item_code_prefix}%"],
                "is_default": 1,
                "is_active": 1,
            },
            fields=["name", "item"],
            limit_page_length=1,
        )

    # 2) Fallback: BOM by child table BOM Item.item_code
    if not boms:
        boms = frappe.get_all(
            "BOM",
            filters=[
                ["BOM Item", "item_code", "=", item_code_prefix],
                ["BOM", "is_default", "=", 1],
            ],
            fields=["name", "item"],
            limit_page_length=1,
        )

    # 3) Additional tolerant fallback:
    #    If still not found, try transforming prefix like B_60103 -> MB_60103
    #    and also try numeric substring matches to find BOM.item containing the digits.
    if not boms and item_code_prefix:
        # try MB_ prefix (if we have numeric part)
        digits_match = re.search(r"(\d+)", item_code_prefix)
        tried = []
        if digits_match:
            digits = digits_match.group(1)
            candidates = [f"MB_{digits}", f"MB{digits}", f"%{digits}%"]
            for c in candidates:
                if c in tried:
                    continue
                tried.append(c)
                if c.startswith("%"):
                    # use a like filter for substring
                    boms = frappe.get_all(
                        "BOM",
                        filters={
                            "item": ["like", c],
                            "is_default": 1,
                            "is_active": 1,
                        },
                        fields=["name", "item"],
                        limit_page_length=1,
                    )
                else:
                    boms = frappe.get_all(
                        "BOM",
                        filters={
                            "item": ["like", f"{c}%"],
                            "is_default": 1,
                            "is_active": 1,
                        },
                        fields=["name", "item"],
                        limit_page_length=1,
                    )
                if boms:
                    break

    if not boms:
        return None


# -------------------- Step: final_item -> Mixing Sequence --------------------

def _build_mixing_sequence_from_doc(seq_name):
    """Return sequence_steps built from Mixing Sequence doc name."""
    doc = frappe.get_doc("Mixing Sequence", seq_name)

    mixing_items = doc.get("mixing_items") or []
    mixing_times = doc.get("mixing_time") or []

    items_by_seq = {}
    for r in mixing_items:
        seq = str(getattr(r, "sequence", "")).strip()
        itm = getattr(r, "item_code", None)
        if seq:
            items_by_seq.setdefault(seq, []).append(itm)

    time_by_seq = {str(getattr(t, "sequence", "")).strip(): getattr(t, "mixing_time", None) for t in mixing_times}

    # Keep order via idx if available, else natural sort
    ordered_seq_rows = sorted(mixing_times, key=lambda x: getattr(x, "idx", 0))
    ordered_sequences = [getattr(r, "sequence", None) for r in ordered_seq_rows]

    sequence_steps = []
    for seq in ordered_sequences:
        if seq is None:
            continue
        sequence_steps.append({
            "sequence": seq,
            "items": items_by_seq.get(seq, []),
            "mixing_time": time_by_seq.get(seq)
        })

    return {
        "mixing_sequence": seq_name,
        "sequence_steps": sequence_steps,
    }


def _find_mixing_sequence_for_final_item_internal(final_item: str):
    """
    Try to find a Mixing Sequence for final_item using multiple tolerant strategies.
    """

    final_norm = str(final_item or "").strip().lower()
    if not final_norm:
        return None

    # 1) Exact mapping via child table Mixing Sequence Mapping.final_item
    seqs = frappe.get_all(
        "Mixing Sequence",
        filters=[["Mixing Sequence Mapping", "final_item", "=", final_item]],
        fields=["name"],
        limit_page_length=1,
    )
    if seqs:
        return _build_mixing_sequence_from_doc(seqs[0]["name"])

    # 2) Try searching produces_items child table inside each Mixing Sequence (case-insensitive)
    #    We'll fetch a reasonable number (all or up to 200); usually there are few.
    all_seqs = frappe.get_all("Mixing Sequence", fields=["name"], limit_page_length=200)
    for s in all_seqs:
        name = s.get("name")
        doc = frappe.get_doc("Mixing Sequence", name)
        produces = doc.get("produces_items") or []
        found = False
        for p in produces:
            produced = _extract_item(p.as_dict() if hasattr(p, "as_dict") else p)
            if produced and produced.strip().lower() == final_norm:
                found = True
                break
            # allow prefix match (final startswith produced) to handle small naming diffs
            if produced and final_norm.startswith(produced.strip().lower()):
                found = True
                break
        if found:
            return _build_mixing_sequence_from_doc(name)

    # 3) No match found
    return None


# -------------------- Public whitelisted API --------------------

@frappe.whitelist()
def get_kneader_mixing_sequence(batch_no=None, final_item=None):
    """
    Single API:
      - Accepts batch_no or final_item (or both).
      - Resolves final_item from batch if needed.
      - Returns mixing_sequence and sequence_steps.
    """
    if not batch_no and not final_item:
        frappe.throw(_("Provide batch_no or final_item"))

    meta = {}
    if batch_no and not final_item:
        meta = _get_final_production_item_internal(batch_no)
        if not meta or not meta.get("final_item"):
            return {"success": False, "error": f"Could not resolve final item from batch {batch_no}"}
        final_item = meta["final_item"]

    final_item = str(final_item or "").strip()
    seq_info = _find_mixing_sequence_for_final_item_internal(final_item)
    if not seq_info:
        return {"success": False, "error": f"No Mixing Sequence found for '{final_item}'"}

    out = {
        "success": True,
        "batch_no": batch_no,
        "final_item": final_item,
        "mixing_sequence": seq_info["mixing_sequence"],
        "sequence_steps": seq_info["sequence_steps"],
    }

    if meta:
        out.update(meta)

    return out
