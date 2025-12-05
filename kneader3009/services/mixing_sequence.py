
import frappe
from frappe import _
from frappe.utils import today
import re

@frappe.whitelist(allow_guest=True)   # callable from client/REST
def find_mixing_sequence(final_item=None, batch_no=None):
    """
    final_item or batch_no (one required).
    Returns: { final_item, sequence_steps: [...], mixing_sequence_name }
    """
    # 1) If batch_no provided but not final_item -> resolve final_item via Stock Entry -> BOM.
    if not final_item and batch_no:
        mix = _get_final_item_from_batch(batch_no)
        if not mix:
            frappe.throw(_("Could not resolve final_item from batch {0}").format(batch_no))
        final_item = mix.get("final_item")

    if not final_item:
        frappe.throw(_("Provide 'final_item' or 'batch_no'"))

    # 2) Find Mixing Sequence doc that produces final_item (child-table filter)
    seq_name = _find_mixing_sequence_name_by_final_item(final_item)
    if not seq_name:
        frappe.throw(_("No Mixing Sequence produces {0}").format(final_item))

    # 3) Fetch doc and build sequence_steps
    seq_doc = frappe.get_doc("Mixing Sequence", seq_name)

    sequence_steps = []
    # collect mixing_items grouped by sequence
    items_by_seq = {}
    for r in seq_doc.get("mixing_items") or []:
        seq = str(r.get("sequence") or "").strip()
        items_by_seq.setdefault(seq, []).append(r.get("item_code"))

    time_by_seq = { str(r.get("sequence")): r.get("mixing_time") for r in (seq_doc.get("mixing_time") or []) }

    # preserve order by sorting mixing_time by idx (same logic as your app2)
    ordered = sorted(seq_doc.get("mixing_time") or [], key=lambda x: x.get("idx", 0))
    ordered_seq = [ str(r.get("sequence") or "") for r in ordered ]

    for s in ordered_seq:
        sequence_steps.append({
            "sequence": s,
            "items": items_by_seq.get(s, []),
            "mixing_time": time_by_seq.get(s)
        })

    return {
        "final_item": final_item,
        "mixing_sequence": seq_name,
        "sequence_steps": sequence_steps
    }


def _find_mixing_sequence_name_by_final_item(final_item):
    # Use frappe.get_list with child-table filter (same as using frappe.client.get_list on REST)
    filters = [
        ["Mixing Sequence Mapping", "final_item", "=", final_item],
    ]
    res = frappe.get_list("Mixing Sequence", filters=filters, fields=["name"], limit_page_length=1)
    if res:
        return res[0].name
    return None


def _get_final_item_from_batch(batch_no):
    """
    1) find latest Stock Entry that has Stock Entry Detail.batch_no == batch_no (or item_group == 'Batch')
    2) extract items[].item_code (last row), then lookup BOM (is_default=1) to get final item
    """
    # Query Stock Entry via child-table filter
    filters = [
        ["Stock Entry Detail", "batch_no", "=", batch_no],
        ["Stock Entry", "docstatus", "=", 1]
    ]
    # order by posting_date desc and limit 1
    stock_entries = frappe.get_list("Stock Entry", filters=filters,
                                   fields=["name","posting_date"], order_by="posting_date desc", limit_page_length=1)

    if not stock_entries:
        return None

    se_name = stock_entries[0].name
    se_doc = frappe.get_doc("Stock Entry", se_name)

    # extract last item row's item_code (similar to your app2 logic)
    items = se_doc.get("items") or []
    if not items:
        return None

    # choose last row (or logic you prefer)
    last = items[-1]
    item_code_full = last.get("item_code") or last.get("item") or None
    if not item_code_full:
        return None

    # normalize/prefix like in app2 (extract B_123 etc.)
    m = re.search(r"B[_-]?\d+", (item_code_full or ""))
    prefix = m.group(0) if m else item_code_full.split()[0]

    # find BOM where BOM Item.item_code == prefix and is_default = 1
    bom_filters = [
        ["BOM Item", "item_code", "=", prefix],
        ["BOM", "is_default", "=", 1],
        ["BOM", "is_active", "=", 1]
    ]
    b = frappe.get_list("BOM", filters=bom_filters, fields=["name", "item"], limit_page_length=1)
    if not b:
        # fallback: search using use child table filter via get_list same as above but maybe different fields
        return {"batch_no": batch_no, "item_code_full": item_code_full, "item_code_prefix": prefix}

    final_item = b[0].get("item")
    return {"batch_no": batch_no, "item_code_full": item_code_full, "item_code_prefix": prefix, "final_item": final_item}
