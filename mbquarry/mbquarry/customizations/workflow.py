import frappe
from frappe import _
from frappe.model.workflow import apply_workflow

@frappe.whitelist()
def apply_doc_workflow(doctype, docname, action, comment=None):
    """
    Apply Workflow using document type and document name along with action and comment
    POST Method call <Site URL>/api/method/mbquarry.mbquarry.customizations.workflow.apply_doc_workflow
    Payload:
        {
            "doctype": "Item",
            "docname": "Ago Test for JPL",
            "action": "Confirm",
            "comment": "Administrator rejected the document",
        }
    """
    doc = frappe.get_doc(doctype, docname)
    if comment:
        frappe.enqueue_doc(doc.doctype, doc.name, "add_comment", text=comment)
    apply_workflow(doc, action)
    return doc