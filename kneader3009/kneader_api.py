import frappe
from frappe import _
import re
import string
from datetime import datetime, timedelta


# ---------- Helpers ----------

def _parse_batch_date(batch_no: str):
    year = int("20" + batch_no[:2])
    month_letter = batch_no[2].upper()
    day = int(batch_no[3:5])

    month = string.ascii_uppercase.index(month_letter) + 1
    current_date = datetime(year, month, day)

    first_day_current = current_date.replace(day=1)
    prev_month_last_day = first_day_current - timedelta(days=1)
    first_day_prev = prev_month_last_day.replace(day=1)

    return first_day_prev.date(), current_date.date()


def _extract_item(row: dict):
    for key in ("item_code", "item_code1", "final_item", "item"):
        if row.get(key):
            return str(row[key]).strip()
    for k, v in row.items():
        if "item" in k.lower() and v:
            return str(v).strip()
    return None


# ---------- Step 1: Batch → Stock Entry → BOM → Final Item ----------

def _get_final_production_item_internal(batch_no: str):
    batch_no = batch_no.strip()
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

    item_code_full = None
    if se.items:
        item_code_full = se.items[0].item_code

    if not item_code_full:
        return None

    prefix_match = re.search(r"B[_-]?\d+", item_code_full)
    item_code_prefix = (
        prefix_match.group(0) if prefix_match else item_code_full.split()[0]
    )

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

    if not boms:
        return None

    bom = boms[0]

    return {
        "stock_entry": se_name,
        "batch_no": batch_no,
        "item_code_full": item_code_full,
        "item_code_prefix": item_code_prefix,
        "bom_no": bom["name"],
        "final_item": bom["item"],
    }


# ---------- Step 2: Final Item → Mixing Sequence ----------

def _find_mixing_sequence_for_final_item_internal(final_item: str):
    final_item_norm = final_item.strip().lower()

    seqs = frappe.get_all(
        "Mixing Sequence",
        filters=[
            ["Mixing Sequence Mapping", "final_item", "=", final_item],
        ],
        fields=["name"],
        limit_page_length=1,
    )

    if not seqs:
        return None

    seq_name = seqs[0]["name"]
    doc = frappe.get_doc("Mixing Sequence", seq_name)

    produces = doc.get("produces_items") or []
    found_match = False

    for row in produces:
        produced = _extract_item(row.as_dict())
        if produced and produced.strip().lower() == final_item_norm:
            found_match = True
            break

    if not found_match:
        return None

    mixing_items = doc.get("mixing_items", [])
    mixing_times = doc.get("mixing_time", [])

    items_by_seq = {}
    for r in mixing_items:
        seq = str(r.sequence).strip()
        items_by_seq.setdefault(seq, []).append(r.item_code)

    time_by_seq = {str(t.sequence).strip(): t.mixing_time for t in mixing_times}

    sequence_steps = []
    for seq in sorted(time_by_seq.keys()):
        sequence_steps.append({
            "sequence": seq,
            "items": items_by_seq.get(seq, []),
            "mixing_time": time_by_seq.get(seq),
        })

    return {
        "mixing_sequence": seq_name,
        "sequence_steps": sequence_steps,
    }


# ---------- FINAL PUBLIC API ----------

@frappe.whitelist()
def get_kneader_mixing_sequence(batch_no=None, final_item=None):
    """
    Single entry point:
    Batch → Stock Entry → BOM → Final Item → Mixing Sequence → Steps
    """

    if not batch_no and not final_item:
        frappe.throw("Provide batch_no or final_item")

    meta = {}
    if batch_no and not final_item:
        meta = _get_final_production_item_internal(batch_no)
        if not meta or not meta.get("final_item"):
            return {"success": False, "error": "Could not resolve final item"}

        final_item = meta["final_item"]

    seq_info = _find_mixing_sequence_for_final_item_internal(final_item)
    if not seq_info:
        return {"success": False, "error": "No Mixing Sequence found"}

    response = {
        "success": True,
        "batch_no": batch_no,
        "final_item": final_item,
        "mixing_sequence": seq_info["mixing_sequence"],
        "sequence_steps": seq_info["sequence_steps"],
    }

    if meta:
        response.update(meta)

    return response
