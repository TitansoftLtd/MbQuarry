import os
import frappe
from frappe import _
from pypdf import PdfWriter
from frappe.model.document import Document

class NetworkPrinterSettings(Document):
    @frappe.whitelist()
    def get_printers_list(self, ip="127.0.0.1", port=631):
        printer_list = []
        try:
            import cups
        except ImportError:
            frappe.throw(
                _("This feature cannot be used as dependencies are missing. "
                  "Please contact your system manager to install pycups.")
            )
            return
        
        try:
            cups.setServer(self.server_ip)
            cups.setPort(self.port)
            conn = cups.Connection()
            printers = conn.getPrinters()
            printer_list.extend(
                {"value": printer_id, "label": printer["printer-info"]}
                for printer_id, printer in printers.items()
            )
        except (RuntimeError, frappe.ValidationError):
            frappe.throw(_("Failed to connect to the printer server."))
        
        return printer_list

no_cache = 1
base_template_path = "www/printview.html"
standard_format = "templates/print_formats/standard.html"


@frappe.whitelist()
def print_by_server(
    doctype, name, printer_setting, print_format=None, doc=None, no_letterhead=0, file_path=None
):
    print_settings = frappe.get_doc("Network Printer Settings", printer_setting)
    try:
        import cups
    except ImportError:
        frappe.throw(_("You need to install pycups to use this feature!"))

    try:
        cups.setServer(print_settings.server_ip)
        cups.setPort(print_settings.port)
        conn = cups.Connection()
        
        pf = frappe.get_doc("Print Format", print_format or frappe.get_meta(doctype).default_print_format or "Standard")
        
        if pf.raw_printing:
            doc = doc or frappe.get_doc(doctype, name)
            doc_dict = doc.as_dict()
            raw_commands = frappe.render_template(pf.raw_commands, {'doc': doc_dict})
            temp_file = os.path.join("/tmp", f"frappe-raw-{frappe.generate_hash()}.txt")
            with open(temp_file, "wb") as f:
                f.write(raw_commands.encode('utf-8'))
            conn.printFile(print_settings.printer_name, temp_file, name, {"raw": "true"})
            os.remove(temp_file)
        else:
            output = PdfWriter()
            output = frappe.get_print(
                doctype, name, print_format, doc=doc, no_letterhead=no_letterhead, as_pdf=True, output=output
            )
            if not file_path:
                file_path = os.path.join("/tmp", f"frappe-pdf-{frappe.generate_hash()}.pdf")
            output.write(open(file_path, "wb"))
            conn.printFile(print_settings.printer_name, file_path, name, {})
            os.remove(file_path)
            
    except OSError as e:
        if (
            "ContentNotFoundError" in e.message
            or "ContentOperationNotPermittedError" in e.message
            or "UnknownContentError" in e.message
            or "RemoteHostClosedError" in e.message
        ):
            frappe.throw(_("PDF generation failed"))
    except cups.IPPError:
        frappe.throw(_("Printing failed"))