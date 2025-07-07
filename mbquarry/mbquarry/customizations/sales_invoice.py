import frappe
from frappe import _
from mbquarry.mbquarry.customizations.workflow import apply_doc_workflow
from tax_integration.customization.sales_invoice import invoice_event_call
from frappe.utils import add_days, nowdate, add_to_date, now_datetime


frappe.utils.logger.set_log_level("DEBUG")
logger = frappe.logger("submission_logs", allow_site=True, file_count=50)
logger_withdrawal = frappe.logger("withdrawal_logs", allow_site=True, file_count=50)

def validate(doc, method):
    if doc.workflow_state == "Credit Approved":
        doc.custom_is_credit_sales = 1
    if doc.docstatus == 0:
        doc.posting_date = frappe.utils.nowdate()
    create_and_update_requisition(doc, method)
    update_custom_qty_requested(doc, method)
    update_cr_workflow(doc, method)

def before_submit(doc, _method=None):
    # Only apply checks if not credit sales
    if doc.custom_credit_limit == 0:
        if doc.is_return == 0:

            # Add function
            validate_mpesa_payments(doc)

            if doc.paid_amount < doc.grand_total:
                frappe.throw("Paid Amount cannot be less than the Invoice Amount.")
            elif doc.paid_amount > doc.grand_total:
                frappe.throw("Paid Amount cannot be greater than the Invoice Amount.")
        else:
            if doc.paid_amount < doc.grand_total:
                frappe.throw("Paid Amount cannot be less than the Return Amount.")
            elif doc.paid_amount > doc.outstanding_amount:
                frappe.throw("Paid Amount cannot be greater than the Outstanding Amount for Returns. Kindly make return payment.")

def on_submit(doc, method):
    logger.info('Submitting Sales Invoice ===================================================================')
    
    # Allocate M-Pesa payments before processing other submission logic
    allocate_mpesa_payments_on_submit(doc)
    
    # Call the KRA Signing Functionality
    signing_details = invoice_event_call(doc.name)
    logger.info('Signing Details: {}'.format(signing_details))
    verify_url = None
    if signing_details.get('success',None):
        verify_url = signing_details.get('verify_url')
        doc.custom_verify_url = verify_url
    logger.info('Verify URL: {}'.format(verify_url))

    # Print Invoice Automatically
    print_invoice_automatically(doc, method, verify_url)
    logger.info('Print Invoice Automatically ================================================================')

def on_cancel(doc, method):
    """
    Handle Sales Invoice cancellation - deallocate M-Pesa payments
    """
    logger.info(f'Cancelling Sales Invoice {doc.name} ===================================================================')
    deallocate_mpesa_payments_on_cancel(doc)
    
def print_invoice_automatically(doc, method, verify_url = None):
    """
    Automatically print Sales Invoice with appropriate print format and printer.
    Also prints Gate Pass for non-return part collection sales.
    """

    prints = frappe.get_single("MbQuarry Settings")

    # Required printer settings
    printer_settings = {
        "cashier": prints.cashier_printer,
        "gate_pass": prints.gate_pass_printer
    }

    # Ensure all printers are configured individually
    missing_printers = {k: v for k, v in printer_settings.items() if not v}
    if missing_printers:
        for printer_label, value in missing_printers.items():
            frappe.log_error(
                f"Missing printer configuration for '{printer_label}_printer'. Value: {value}",
                "Printer Setup Error"
            )
        return

    try:
        customer = frappe.get_doc("Customer", doc.customer)
    except frappe.DoesNotExistError:
        frappe.log_error(f"Customer not found for Sales Invoice: {doc.name}", "Sales Invoice Print Error")
        return

    logger.info(f"Aboutt to print invoice {verify_url}")
    # Decide main print format and printer
    if doc.is_return == 1:
        print_format = prints.credit_note_print_format
    elif verify_url:
        print_format = prints.invoice_print_format
    elif customer.tax_id:
        print_format = prints.receipt_print_format
    elif doc.custom_is_credit_sales == 1:
        print_format = prints.part_collection_note_credit_sale
        # Also print credit sales gate pass
        _print_gate_pass(doc, printer_settings["gate_pass"], prints.gate_pass_credit_sales)
    else:
        print_format = prints.part_collection_print_format

    # Also print standard gate pass
    _print_gate_pass(doc, printer_settings["gate_pass"], prints.gate_pass_print_format)

    # Main print job
    _print_by_server(doc, printer_settings["cashier"], print_format)

@frappe.whitelist()
def print_mpesa_agent_withdrawal_details(*args, **kwargs):
    """
    Print MPESA Agent Withdrawal Details from Sales Invoice.
    """
    document_name = kwargs.get("invoice_name")
    document_type = "Sales Invoice"

    # Define required printer settings
    prints = frappe.get_single("MbQuarry Settings")
    printer_name = prints.withdraw_agent_printer
    print_format = prints.withdrawal_agent_print_format

    # Print the document
    frappe.call(
        "mbquarry.mbquarry.customizations.print_format.print_by_server",
        doctype=document_type,
        name=document_name,
        printer_setting=printer_name,
        print_format=print_format,
        no_letterhead=1
    )
    return {'success': True, 'message': 'MPESA Agent Withdrawal Details printed successfully'}

def _print_by_server(doc, printer_name, print_format):
    frappe.call(
        "mbquarry.mbquarry.customizations.print_format.print_by_server",
        doctype=doc.doctype,
        name=doc.name,
        printer_setting=printer_name,
        print_format=print_format,
        no_letterhead=1
    )

def _print_gate_pass(doc, printer_name, print_format):
    """Helper to print gate pass"""
    _print_by_server(doc, printer_name, print_format)

def create_and_update_requisition(doc, method):
    """Creates or updates Customer Requisition(s) when workflow_state is 'Requisition Sent'"""
    if doc.workflow_state != "Requisition Sent":
        return

    warehouse_requisitions = {}
    submitted_warehouses = []
    updated_requisitions = []

    for item in doc.items:
        if not item.warehouse:
            continue
        
        warehouse = item.warehouse

        if warehouse not in warehouse_requisitions:
            # Check if a requisition already exists for this Sales Invoice & warehouse
            existing_req = frappe.get_value(
                "Customer Requisition", 
                {"sales_invoice": doc.name, "warehouse": warehouse}, 
                "name"
            )

            if existing_req:
                # Fetch and update existing requisition
                customer_requisition = frappe.get_doc("Customer Requisition", existing_req)
                customer_requisition.set("items", [])
                updated_requisitions.append(existing_req)
            else:
                # Create a new requisition
                customer_requisition = frappe.get_doc({
                    "doctype": "Customer Requisition",
                    "customer": doc.customer,
                    "warehouse": warehouse,
                    "company": doc.company,
                    "requested_by": doc.custom_prepared_by,
                    "sales_invoice": doc.name,
                    "is_return": doc.is_return,
                })
                submitted_warehouses.append(warehouse)  # Track newly created requisition

            warehouse_requisitions[warehouse] = customer_requisition

        # Append item to the requisition (new or existing)
        warehouse_requisitions[warehouse].append("items", {
            "item_code": item.item_code,
            "item_name": item.item_name,
            "item_group": item.item_group,
            "qty": item.qty,
            "warehouse": item.warehouse
        })

    # Insert or save requisitions
    for warehouse, req in warehouse_requisitions.items():
        if req.get("name"):  # If updating an existing requisition
            req.save()
        else:
            req.insert()
            # req.submit()  # Uncomment if auto-submission is required

    # Show messages based on action taken
    if submitted_warehouses:
        frappe.msgprint(
            _("Requisition(s) processed for:<br><ul>{0}</ul>").format(
                "".join(f"<li>{wh}</li>" for wh in submitted_warehouses)
            ),
            title=_("Requisition Processed"),
            indicator="green"
        )
    elif updated_requisitions:
        frappe.msgprint(
            _("Updated existing Customer Requisition(s):<br><ul>{0}</ul>").format(
                "".join(f"<li>{name}</li>" for name in updated_requisitions)
            ),
            title=_("Requisition Updated"),
            indicator="blue"
        )


def update_custom_qty_requested(doc, method):
    """
    Update custom_qty_requested based on qty when workflow_state is "Requisition Sent".
    """
    if doc.workflow_state == "Requisition Sent":
        for item in doc.items:
            item.custom_qty_requested = item.qty

        # Recalculate total requested quantity
        doc.custom_total_requested = sum(item.custom_qty_requested or 0 for item in doc.items)

def update_cr_workflow(doc, method):
    """Update Customer Requisition when Sales Invoice changes."""
    customer_requisition = frappe.get_all(
        "Customer Requisition",
        filters={"sales_invoice": doc.name},
        fields=["name", "workflow_state"]
    )

    for requisition in customer_requisition:
        if doc.workflow_state and requisition["workflow_state"] and \
           doc.workflow_state == "Draft" and requisition["workflow_state"] == "Requisition Recalled":
            # Apply workflow for each linked Customer Requisition
            apply_doc_workflow("Customer Requisition", requisition["name"], "Recall Requisition", "Recalled for Adjustment")
            frappe.db.set_value("Customer Requisition", requisition["name"], "printed", 0)

            frappe.msgprint(f"Customer Requisition {requisition['name']} updated due to Sales Invoice changes.")

# Function to Fetch Customer Balance
@frappe.whitelist()
def get_customer_balance(customer):
    balance = 0
    if not customer:
        return balance
    from erpnext.accounts.utils import get_balance_on

    balance = get_balance_on(party_type="Customer", party=customer)
    return balance


@frappe.whitelist()
def print_quotation(doc):
    """Print Quotation from Sales Invoice."""
    _print_document(doc, format_field="quotation_print_format", setting_field="quotation_printer", label="Quotation Print Format", error_title="Quotation Print Error")

@frappe.whitelist()
def print_receipt(doc):
    """Reprint Receipt from Sales Invoice."""
    if isinstance(doc, str):
        doc = frappe.parse_json(doc)
    if isinstance(doc, dict):
        doc = frappe.get_doc(doc)

    try:
        customer = frappe.get_doc("Customer", doc.customer)
    except frappe.DoesNotExistError:
        frappe.log_error(f"Customer not found for Sales Invoice: {doc.name}", "Sales Invoice Print Error")
        return "Customer not found."

    # Determine the appropriate print format
    if doc.custom_verify_url:
        format_field = "invoice_print_format"
    elif customer.tax_id:
        format_field = "receipt_print_format"
    else:
        format_field = "part_collection_print_format"

    _print_document(doc, setting_field="cashier_printer", format_field=format_field, label="Receipt Reprint Format", error_title="Receipt Print Error")


def _print_document(doc, setting_field, format_field, label, error_title):
    """
    Common helper to print a document using printer and format from MbQuarry Settings").
    """
    try:
        if isinstance(doc, str):
            doc = frappe.parse_json(doc)
        if isinstance(doc, dict):
            doc = frappe.get_doc(doc)

        if not hasattr(doc, "doctype") or not hasattr(doc, "name"):
            frappe.throw("Invalid document format.")

        prints = frappe.get_single("MbQuarry Settings")
        printer_name = getattr(prints, setting_field, None)
        print_format = getattr(prints, format_field, None)

        if not printer_name:
            frappe.throw(f"No printer configured in MbQuarry Settings for {label}")
        if not print_format:
            frappe.throw(f"No print format configured in MbQuarry Settings for {label}")

        frappe.call(
            "mbquarry.mbquarry.customizations.print_format.print_by_server",
            doctype=doc.doctype,
            name=doc.name,
            printer_setting=printer_name,
            print_format=print_format,
            no_letterhead=1
        )
        return "success"
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), error_title)


# Cron Job to delete old draft Sales Invoices
def delete_old_draft_sales_invoices():
    # Calculate the date 7 days ago
    cutoff_date = add_days(nowdate(), -7)

    # Get all draft Sales Invoices with posting_date <= cutoff_date
    invoices = frappe.get_all(
        "Sales Invoice",
        filters={
            "docstatus": 0,
            "posting_date": ("<=", cutoff_date)
        },
        pluck="name"
    )
    for name in invoices:
        try:
            frappe.delete_doc("Sales Invoice", name, force=1)
            frappe.logger().info(f"Deleted draft Sales Invoice: {name}")
        except Exception as e:
            frappe.log_error(f"Error deleting Sales Invoice {name}: {e}")

@frappe.whitelist()
def get_matching_mpesa_payments(invoice_amount, minutes_back=5):
    """
    Legacy method - use get_filtered_mpesa_payments instead
    """
    return get_filtered_mpesa_payments(
        amount=invoice_amount,
        amount_tolerance=0.01,
        minutes_back=minutes_back,
        transaction_code=None
    )

@frappe.whitelist()
def get_filtered_mpesa_payments(amount=None, amount_tolerance=0.01, minutes_back=10, transaction_code=None):
    """
    Get M-Pesa payments based on dynamic filter criteria
    """
    # Calculate the time threshold
    time_threshold = add_to_date(now_datetime(), minutes=-int(minutes_back))
    
    # Base query
    query = """
        SELECT 
            name,
            transaction_id,
            CAST(transaction_amount AS DECIMAL(10,2)) as amount,
            CAST(COALESCE(allocated_amount, 0) AS DECIMAL(10,2)) as allocated_amount,
            CAST(transaction_amount - COALESCE(allocated_amount, 0) AS DECIMAL(10,2)) as available_balance,
            first_name as customer_name,
            status,
            creation
        FROM `tabMpesa Payment`
        WHERE creation >= %s
    """
    
    params = [time_threshold]
    
    # Add amount filter if provided
    if amount is not None:
        amount_float = float(amount)
        tolerance = float(amount_tolerance) if amount_tolerance else 0.01
        query += """
            AND (
                CAST(transaction_amount AS DECIMAL(10,2)) = %s 
                OR ABS(CAST(transaction_amount AS DECIMAL(10,2)) - %s) <= %s
            )
        """
        params.extend([amount_float, amount_float, tolerance])
    
    # Add transaction code filter if provided
    if transaction_code:
        # Sanitize transaction code to prevent SQL injection
        sanitized_code = str(transaction_code).replace("%", "\\%").replace("_", "\\_")
        query += " AND transaction_id LIKE %s"
        params.append(f"%{sanitized_code}%")
    
    # Add status filter to exclude already applied payments and fully allocated payments
    query += " AND (status IS NULL OR status != 'Applied')"
    query += " AND (allocated_amount IS NULL OR allocated_amount < transaction_amount OR allocated_amount = 0)"
    
    query += " ORDER BY creation DESC LIMIT 50"
    
    payments = frappe.db.sql(query, params, as_dict=True)
    
    return payments

@frappe.whitelist()
def apply_mpesa_payment(doc, payment_name):
    """
    Apply selected M-Pesa payment to the Sales Invoice
    """
    # Get the payment record
    payment = frappe.get_doc("Mpesa Payment", payment_name)
    
    # Parse doc if it's a string
    if isinstance(doc, str):
        import json
        doc = json.loads(doc)
    
    # Get the actual Sales Invoice document
    sales_invoice = frappe.get_doc("Sales Invoice", doc.get("name"))
    
    # Create a Payment Entry
    payment_entry = frappe.get_doc({
        "doctype": "Payment Entry",
        "payment_type": "Receive",
        "party_type": "Customer",
        "party": sales_invoice.customer,
        "paid_amount": payment.transaction_amount,
        "received_amount": payment.transaction_amount,
        "reference_no": payment.transaction_id,
        "reference_date": payment.creation.date(),
        "mode_of_payment": "Mpesa Express",  # Adjust as needed
        "company": sales_invoice.company,
        "references": [{
            "reference_doctype": "Sales Invoice",
            "reference_name": sales_invoice.name,
            "allocated_amount": payment.transaction_amount
        }]
    })
    
    # Insert and submit the payment entry
    payment_entry.insert()
    payment_entry.submit()
    
    # Update the M-Pesa payment record to mark it as applied
    payment.db_set("applied_to_invoice", sales_invoice.name)
    payment.db_set("status", "Applied")
    
    # Update the Sales Invoice payments table
    sales_invoice.append("payments", {
        "mode_of_payment": "Mpesa Express",
        "amount": payment.transaction_amount,
        "account": frappe.get_value("Mode of Payment Account", 
                                  {"parent": "Mpesa Express", "company": sales_invoice.company}, 
                                  "default_account")
    })
    
    sales_invoice.save()
    
    return {"success": True, "message": "M-Pesa payment applied successfully"}

@frappe.whitelist()
def apply_multiple_mpesa_payments(doc, payment_names):
    """
    Legacy method - kept for backward compatibility
    Use the new frontend approach instead
    """
    return {"success": True, "message": "Please use the new M-Pesa payment dialog with frontend processing"}

@frappe.whitelist()
def get_mpesa_payment_details(payment_names):
    """
    Get M-Pesa payment details for the selected payments
    """
    if isinstance(payment_names, str):
        import json
        payment_names = json.loads(payment_names)
    
    payment_details = []
    
    for payment_name in payment_names:
        try:
            payment = frappe.get_doc("Mpesa Payment", payment_name)
            # Add null safety and default values
            payment_details.append({
                "name": payment.name,
                "transaction_id": payment.transaction_id or "",
                "transaction_amount": float(payment.transaction_amount or 0),
                "available_balance": payment.get_available_balance() if hasattr(payment, 'get_available_balance') else float(payment.transaction_amount or 0),
                "allocated_amount": float(payment.allocated_amount or 0),
                "first_name": payment.first_name or "",
                "status": payment.status or "Draft",
                "creation": payment.creation
            })
        except Exception as e:
            frappe.log_error(f"Error getting M-Pesa payment details for {payment_name}: {str(e)}")
            continue
    
    return payment_details

@frappe.whitelist()
def mark_mpesa_payments_applied(payment_names, invoice_name):
    """
    Mark M-Pesa payments as applied to an invoice
    """
    if isinstance(payment_names, str):
        import json
        payment_names = json.loads(payment_names)
    
    applied_count = 0
    
    for payment_name in payment_names:
        try:
            payment = frappe.get_doc("Mpesa Payment", payment_name)
            payment.db_set("applied_to_invoice", invoice_name)
            payment.db_set("status", "Applied")
            applied_count += 1
        except Exception as e:
            frappe.log_error(f"Error marking M-Pesa payment {payment_name} as applied: {str(e)}")
            continue
    
    return {"success": True, "applied_count": applied_count}

def allocate_mpesa_payments_on_submit(doc):
    """
    Allocate M-Pesa payments when Sales Invoice is submitted
    """
    try:
        for payment_row in doc.payments:
            if payment_row.mode_of_payment == "Mpesa Paybill" and payment_row.reference_no:
                # Validate payment row amount
                if not payment_row.amount or float(payment_row.amount) <= 0:
                    logger.warning(f"Invalid payment amount in row for Sales Invoice {doc.name}")
                    continue
                    
                # Extract transaction IDs from reference_no field
                transaction_ids = [tid.strip() for tid in payment_row.reference_no.split(',') if tid.strip()]
                
                # Sequential allocation strategy - allocate fully from each payment in order
                total_amount_needed = round(float(payment_row.amount), 2)
                if not transaction_ids or len(transaction_ids) == 0:
                    logger.warning(f"No valid transaction IDs found in payment row for Sales Invoice {doc.name}")
                    continue
                
                successful_allocations = []
                failed_allocations = []
                remaining_amount = total_amount_needed
                
                for transaction_id in transaction_ids:
                    if not transaction_id:  # Skip empty transaction IDs
                        continue
                        
                    # Break if we've allocated all the needed amount
                    if remaining_amount <= 0:
                        break
                        
                    try:
                        # Find the M-Pesa payment record
                        mpesa_payment_name = frappe.db.get_value("Mpesa Payment", {"transaction_id": transaction_id}, "name")
                        if not mpesa_payment_name:
                            failed_allocations.append({
                                "transaction_id": transaction_id,
                                "reason": "M-Pesa payment record not found"
                            })
                            logger.warning(f"M-Pesa payment record not found for transaction ID: {transaction_id}")
                            continue
                        
                        mpesa_payment = frappe.get_doc("Mpesa Payment", mpesa_payment_name)
                        
                        if mpesa_payment:
                            # Check if this payment is already allocated to this invoice
                            if (hasattr(mpesa_payment, 'applied_to_invoice') and 
                                mpesa_payment.applied_to_invoice == doc.name):
                                # Already allocated to this invoice, skip
                                continue
                            
                            # Check available balance
                            available_balance = mpesa_payment.get_available_balance()
                            
                            if available_balance > 0:
                                # Calculate how much to allocate from this payment
                                amount_to_allocate = min(available_balance, remaining_amount)
                                amount_to_allocate = round(amount_to_allocate, 2)
                                
                                # Allocate the amount
                                mpesa_payment.allocate_amount(amount_to_allocate)
                                
                                # Mark as applied to this invoice
                                mpesa_payment.db_set("applied_to_invoice", doc.name)
                                mpesa_payment.db_set("status", "Applied")
                                
                                # Update remaining amount
                                remaining_amount = round(remaining_amount - amount_to_allocate, 2)
                                
                                successful_allocations.append({
                                    "transaction_id": transaction_id,
                                    "amount": amount_to_allocate,
                                    "mpesa_payment": mpesa_payment.name
                                })
                                
                                logger.info(f"Allocated {amount_to_allocate} from M-Pesa payment {mpesa_payment.name} to Sales Invoice {doc.name}. Remaining: {remaining_amount}")
                            else:
                                failed_allocations.append({
                                    "transaction_id": transaction_id,
                                    "reason": f"No available balance. Available: {available_balance}",
                                    "available_balance": available_balance,
                                    "required_amount": remaining_amount
                                })
                                logger.warning(f"Cannot allocate from M-Pesa payment {transaction_id}. No available balance: {available_balance}")
                        else:
                            failed_allocations.append({
                                "transaction_id": transaction_id,
                                "reason": "M-Pesa payment record not found"
                            })
                            logger.warning(f"M-Pesa payment record not found for transaction ID: {transaction_id}")
                            
                    except Exception as e:
                        failed_allocations.append({
                            "transaction_id": transaction_id,
                            "reason": f"Error during allocation: {str(e)}"
                        })
                        logger.error(f"Error allocating M-Pesa payment {transaction_id}: {str(e)}")
                
                # Log summary
                if successful_allocations:
                    total_allocated = sum([alloc['amount'] for alloc in successful_allocations])
                    logger.info(f"Successfully allocated {len(successful_allocations)} M-Pesa payments totaling {total_allocated} for Sales Invoice {doc.name}")
                
                if failed_allocations:
                    logger.warning(f"Failed to allocate {len(failed_allocations)} M-Pesa payments for Sales Invoice {doc.name}: {failed_allocations}")
                
                # Check if full amount was allocated
                if remaining_amount > 0.01:  # More than 1 cent remaining
                    logger.warning(f"Could not allocate full amount for Sales Invoice {doc.name}. Remaining: {remaining_amount}")
                    # Optionally, you can throw an error to prevent submission if critical
                    # Uncomment the next line if you want to prevent submission on allocation failures
                    # frappe.throw(f"Could not allocate full amount. Remaining: {remaining_amount}")
                
    except Exception as e:
        logger.error(f"Error in allocate_mpesa_payments_on_submit for Sales Invoice {doc.name}: {str(e)}")
        # Optionally throw error to prevent submission
        # frappe.throw(f"Error allocating M-Pesa payments: {str(e)}")

def deallocate_mpesa_payments_on_cancel(doc):
    """
    Deallocate M-Pesa payments when Sales Invoice is cancelled
    """
    try:
        for payment_row in doc.payments:
            if payment_row.mode_of_payment == "Mpesa Paybill" and payment_row.reference_no:
                # Validate payment row amount
                if not payment_row.amount or float(payment_row.amount) <= 0:
                    logger.warning(f"Invalid payment amount in row for cancelled Sales Invoice {doc.name}")
                    continue
                    
                # Extract transaction IDs from reference_no field
                transaction_ids = [tid.strip() for tid in payment_row.reference_no.split(',') if tid.strip()]
                
                # Sequential deallocation strategy - deallocate based on actual allocated amounts
                total_amount_to_deallocate = round(float(payment_row.amount), 2)
                if not transaction_ids or len(transaction_ids) == 0:
                    logger.warning(f"No valid transaction IDs found in payment row for cancelled Sales Invoice {doc.name}")
                    continue
                
                successful_deallocations = []
                failed_deallocations = []
                remaining_to_deallocate = total_amount_to_deallocate
                
                for transaction_id in transaction_ids:
                    if not transaction_id:  # Skip empty transaction IDs
                        continue
                        
                    # Break if we've deallocated all the needed amount
                    if remaining_to_deallocate <= 0:
                        break
                        
                    try:
                        # Find the M-Pesa payment record
                        mpesa_payment_name = frappe.db.get_value("Mpesa Payment", {"transaction_id": transaction_id}, "name")
                        if not mpesa_payment_name:
                            failed_deallocations.append({
                                "transaction_id": transaction_id,
                                "reason": "M-Pesa payment record not found"
                            })
                            logger.warning(f"M-Pesa payment record not found for transaction ID: {transaction_id}")
                            continue
                        
                        mpesa_payment = frappe.get_doc("Mpesa Payment", mpesa_payment_name)
                        
                        if mpesa_payment:
                            # Check if this payment was allocated to this invoice
                            if (hasattr(mpesa_payment, 'applied_to_invoice') and 
                                mpesa_payment.applied_to_invoice == doc.name):
                                
                                # Calculate how much to deallocate from this payment
                                # Problem: We need to track per-invoice allocations, not total allocations
                                # For now, we'll deallocate based on sequential allocation logic
                                transaction_amount = float(mpesa_payment.transaction_amount or 0)
                                available_balance = mpesa_payment.get_available_balance()
                                
                                # Calculate maximum amount that could be allocated from this payment
                                max_allocatable = transaction_amount - available_balance
                                
                                # Calculate amount to deallocate (minimum of what's needed and what was allocated)
                                amount_to_deallocate = min(remaining_to_deallocate, max_allocatable)
                                amount_to_deallocate = round(amount_to_deallocate, 2)
                                
                                if amount_to_deallocate > 0:
                                    # Deallocate the amount
                                    mpesa_payment.deallocate_amount(amount_to_deallocate)
                                    
                                    # Update remaining amount
                                    remaining_to_deallocate = round(remaining_to_deallocate - amount_to_deallocate, 2)
                                    
                                    # Clear the applied_to_invoice reference if this payment is no longer allocated to this invoice
                                    # Check if this was the last allocation from this payment for this invoice
                                    new_available_balance = mpesa_payment.get_available_balance()
                                    new_transaction_amount = float(mpesa_payment.transaction_amount or 0)
                                    
                                    # If fully deallocated or no longer has allocations to this invoice
                                    if abs(new_available_balance - new_transaction_amount) <= 0.01:  # Within 1 cent tolerance
                                        mpesa_payment.db_set("applied_to_invoice", None)
                                        mpesa_payment.db_set("status", "Draft")  # Reset to original status
                                    
                                    successful_deallocations.append({
                                        "transaction_id": transaction_id,
                                        "amount": amount_to_deallocate,
                                        "mpesa_payment": mpesa_payment.name
                                    })
                                    
                                    logger.info(f"Deallocated {amount_to_deallocate} from M-Pesa payment {mpesa_payment.name} for cancelled Sales Invoice {doc.name}. Remaining: {remaining_to_deallocate}")
                                else:
                                    logger.info(f"No amount to deallocate from M-Pesa payment {transaction_id} for invoice {doc.name}")
                            else:
                                # Payment was not allocated to this invoice
                                logger.info(f"M-Pesa payment {transaction_id} was not allocated to this invoice {doc.name}")
                        else:
                            failed_deallocations.append({
                                "transaction_id": transaction_id,
                                "reason": "M-Pesa payment record not found"
                            })
                            logger.warning(f"M-Pesa payment record not found for transaction ID: {transaction_id}")
                            
                    except Exception as e:
                        failed_deallocations.append({
                            "transaction_id": transaction_id,
                            "reason": f"Error during deallocation: {str(e)}"
                        })
                        logger.error(f"Error deallocating M-Pesa payment {transaction_id}: {str(e)}")
                
                # Log summary
                if successful_deallocations:
                    total_deallocated = sum([dealloc['amount'] for dealloc in successful_deallocations])
                    logger.info(f"Successfully deallocated {len(successful_deallocations)} M-Pesa payments totaling {total_deallocated} for cancelled Sales Invoice {doc.name}")
                
                if failed_deallocations:
                    logger.warning(f"Failed to deallocate {len(failed_deallocations)} M-Pesa payments for cancelled Sales Invoice {doc.name}: {failed_deallocations}")
                
                # Check if full amount was deallocated
                if remaining_to_deallocate > 0.01:  # More than 1 cent remaining
                    logger.warning(f"Could not deallocate full amount for cancelled Sales Invoice {doc.name}. Remaining: {remaining_to_deallocate}")
                
    except Exception as e:
        logger.error(f"Error in deallocate_mpesa_payments_on_cancel for Sales Invoice {doc.name}: {str(e)}")

@frappe.whitelist()
def apply_mpesa_payments(doc):
    """
    Legacy method - kept for backward compatibility
    """
    return {"success": True, "message": "Please use the new M-Pesa payment dialog"}


# Function to get user territory-------


@frappe.whitelist()
def get_user_territory(user):
    # Check if user is exempted from territory auto-assignment
    settings = frappe.get_single("MbQuarry Settings")
    exempted_users = [row.user for row in settings.exempted_territory_users]

    if user in exempted_users:
        return None

    territories = frappe.get_all(
        "Territory",
        fields=["name"]
    )

    matching_territories = []
    for t in territories:
        users = frappe.get_all(
            "Territory Users",
            filters={"parent": t.name, "user": user},
            limit=1
        )
        if users:
            matching_territories.append(t.name)

    if len(matching_territories) == 1:
        return matching_territories[0]
    elif len(matching_territories) > 1:
        frappe.throw("You are assigned to multiple territories. Please contact your Administrator.")
    else:
        frappe.throw("No territory has been assigned. Please contact your Administrator.")

def validate_mpesa_payments(doc):
    '''
    Method that checks that Mpesa Payments applied to Sales Invoice are valid
    '''
    # Validate Mpesa Express Payments
    validate_mpesa_express_payments(doc)

    # Validate Mpesa Paybill Payments
    validate_mpesa_paybill_payments(doc)

def validate_mpesa_express_payments(doc):
    '''
    Method that checks that Mpesa Express Payments applied to Sales Invoice are valid
    '''
    # Find Mpesa Express payments
    mpesa_express_payments = [p for p in doc.payments if p.mode_of_payment == "Mpesa Express"]
    
    # Only validate if there are Mpesa Express payments
    if not mpesa_express_payments:
        return  # No Mpesa Express payments to validate
    
    # Check if express payment has been received
    if not doc.custom_express_recieved:
        frappe.throw("Mpesa Express Payment Has Not Been Received")

def validate_mpesa_paybill_payments(doc):
    '''
    Method that checks that Mpesa Paybill Payments applied to Sales Invoice are valid
    '''
    # Find Mpesa Paybill payments
    mpesa_paybill_payments = [p for p in doc.payments if p.mode_of_payment == "Mpesa Paybill"]

    # Only validate if there are Mpesa Paybill payments
    if not mpesa_paybill_payments:
        return  # No Mpesa Paybill payments to validate

    # Validate each Mpesa Paybill payment
    for payment in mpesa_paybill_payments:
        if not payment.reference_no:
            frappe.throw("Mpesa Paybill payment has not been applied")