import frappe
from kneader3009.services.mixing_sequence import find_mixing_sequence


@frappe.whitelist()
def get_mixing_sequence(batch_no=None, final_item=None):
    """
    API endpoint to fetch mixing sequence using batch_no or final_item.
    """
    return find_mixing_sequence(batch_no=batch_no, final_item=final_item)
