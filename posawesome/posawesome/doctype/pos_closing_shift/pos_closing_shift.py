# -*- coding: utf-8 -*-
# Copyright (c) 2020, Youssef Restom and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import json
import os
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

# Predefined MOP mapping for summary fields
# Keys are lowercase for case-insensitive matching
MOP_FIELD_MAP = {
    "cash": "total_cash",
    "knet": "total_knet",
    "k-net": "total_knet",
    "online": "total_online",
    "visa": "total_visa",
    "credit card": "total_visa",
}


class POSClosingShift(Document):
    def validate(self):
        user = frappe.get_all(
            "POS Closing Shift",
            filters={
                "user": self.user,
                "docstatus": 1,
                "pos_opening_shift": self.pos_opening_shift,
                "name": ["!=", self.name],
            },
        )

        if user:
            frappe.throw(
                _(
                    "POS Closing Shift {} against {} between selected period".format(
                        frappe.bold("already exists"), frappe.bold(self.user)
                    )
                ),
                title=_("Invalid Period"),
            )

        if (
            frappe.db.get_value("POS Opening Shift", self.pos_opening_shift, "status")
            != "Open"
        ):
            frappe.throw(
                _("Selected POS Opening Shift should be open."),
                title=_("Invalid Opening Entry"),
            )

        self.calculate_transaction_details()
        self.calculate_payment_details()
        self.set_status_indicators()
        self.calculate_summaries()
        self.update_payment_reconciliation()

    def before_save(self):
        self.set_status_indicators()

    def update_payment_reconciliation(self):
        # update the difference values in Payment Reconciliation child table
        # get default precision for site
        precision = (
            frappe.get_cached_value("System Settings", None, "currency_precision") or 3
        )
        for d in self.payment_reconciliation:
            d.difference = +flt(d.closing_amount, precision) - flt(
                d.expected_amount, precision
            )

    def on_submit(self):
        # Final recalculation to ensure submitted data is accurate
        self.calculate_transaction_details()
        self.calculate_payment_details()
        self.set_status_indicators()
        self.calculate_summaries()

        opening_entry = frappe.get_doc("POS Opening Shift", self.pos_opening_shift)
        opening_entry.pos_closing_shift = self.name
        opening_entry.set_status()
        self.delete_draft_invoices()
        opening_entry.save()

    def delete_draft_invoices(self):
        if frappe.get_value("POS Profile", self.pos_profile, "posa_allow_delete"):
            data = frappe.db.sql(
                """
                select
                    name
                from
                    `tabSales Invoice`
                where
                    docstatus = 0 and posa_is_printed = 0 and posa_pos_opening_shift = %s
                """,
                (self.pos_opening_shift),
                as_dict=1,
            )

            for invoice in data:
                frappe.delete_doc("Sales Invoice", invoice.name, force=1)

    # ──────────────────────────────────────────────────────────────
    # Transaction Detail Calculations (POS Transactions child table)
    # ──────────────────────────────────────────────────────────────
    def calculate_transaction_details(self):
        """Batch-fetch Sales Invoice data and enrich each pos_transactions row
        with mode_of_payment, payment_status, customer_outstanding,
        paid_amount, and credit_amount."""
        if not self.pos_transactions:
            return

        invoice_names = [
            d.sales_invoice for d in self.pos_transactions if d.sales_invoice
        ]
        if not invoice_names:
            return

        # Batch fetch invoice data — single query for all invoices
        invoices_data = frappe.db.get_all(
            "Sales Invoice",
            filters={
                "name": ["in", invoice_names],
                "docstatus": 1,
            },
            fields=[
                "name",
                "grand_total",
                "outstanding_amount",
                "status",
            ],
        )
        invoice_map = {d.name: d for d in invoices_data}

        # Batch fetch primary Mode of Payment per invoice using aggregated SQL
        # Gets the MOP with the highest amount for each invoice
        if invoice_names:
            mop_data = frappe.db.sql(
                """
                SELECT
                    sip.parent AS invoice_name,
                    sip.mode_of_payment,
                    sip.amount
                FROM `tabSales Invoice Payment` sip
                INNER JOIN `tabSales Invoice` si ON si.name = sip.parent
                WHERE sip.parent IN %(invoice_names)s
                    AND si.docstatus = 1
                ORDER BY sip.parent, sip.amount DESC
                """,
                {"invoice_names": invoice_names},
                as_dict=1,
            )

            # Build map: invoice_name -> primary MOP (highest amount)
            mop_map = {}
            for row in mop_data:
                if row.invoice_name not in mop_map:
                    mop_map[row.invoice_name] = row.mode_of_payment
        else:
            mop_map = {}

        # Enrich each transaction row
        for txn in self.pos_transactions:
            inv = invoice_map.get(txn.sales_invoice)
            if not inv:
                # Invoice not found or cancelled — mark as Outstanding
                txn.payment_status = "Outstanding"
                txn.customer_outstanding = flt(txn.grand_total)
                txn.paid_amount = 0
                txn.credit_amount = flt(txn.grand_total)
                txn.mode_of_payment = ""
                continue

            grand_total = flt(inv.grand_total)
            outstanding = flt(inv.outstanding_amount)
            paid = flt(grand_total - outstanding)

            txn.customer_outstanding = outstanding
            txn.paid_amount = paid
            txn.mode_of_payment = mop_map.get(txn.sales_invoice, "")

            # Determine payment status and credit amount
            if outstanding <= 0:
                txn.payment_status = "Paid"
                txn.credit_amount = 0
            elif outstanding < grand_total:
                txn.payment_status = "Partial"
                txn.credit_amount = outstanding
            else:
                txn.payment_status = "Outstanding"
                txn.credit_amount = grand_total

    # ──────────────────────────────────────────────────────────────
    # Payment Detail Calculations (POS Payments child table)
    # ──────────────────────────────────────────────────────────────
    def calculate_payment_details(self):
        """Batch-fetch Payment Entry data and enrich each pos_payments row
        with payment_status, outstanding_amount, sales_invoice,
        and collection_type."""
        if not self.pos_payments:
            return

        pe_names = [d.payment_entry for d in self.pos_payments if d.payment_entry]
        if not pe_names:
            return

        # Batch fetch Payment Entry data
        pe_data = frappe.db.get_all(
            "Payment Entry",
            filters={
                "name": ["in", pe_names],
                "docstatus": 1,
            },
            fields=[
                "name",
                "paid_amount",
                "unallocated_amount",
                "reference_no",
            ],
        )
        pe_map = {d.name: d for d in pe_data}

        # Batch fetch Payment Entry References to find related Sales Invoices
        pe_ref_data = frappe.db.get_all(
            "Payment Entry Reference",
            filters={
                "parent": ["in", pe_names],
                "reference_doctype": "Sales Invoice",
            },
            fields=[
                "parent",
                "reference_name",
                "allocated_amount",
            ],
            order_by="allocated_amount desc",
        )

        # Build map: PE name -> first (largest) referenced Sales Invoice
        pe_invoice_map = {}
        for ref in pe_ref_data:
            if ref.parent not in pe_invoice_map:
                pe_invoice_map[ref.parent] = ref.reference_name

        # Get the set of invoice names from pos_transactions for comparison
        txn_invoice_names = set()
        if self.pos_transactions:
            txn_invoice_names = {
                d.sales_invoice for d in self.pos_transactions if d.sales_invoice
            }

        # Enrich each payment row
        for pay in self.pos_payments:
            pe = pe_map.get(pay.payment_entry)
            if not pe:
                pay.payment_status = "Unallocated"
                pay.outstanding_amount = flt(pay.paid_amount)
                pay.collection_type = "New Sale"
                continue

            unallocated = flt(pe.unallocated_amount)
            paid = flt(pe.paid_amount)

            pay.outstanding_amount = unallocated

            # Determine payment status
            if unallocated <= 0:
                pay.payment_status = "Allocated"
            elif unallocated < paid:
                pay.payment_status = "Partial"
            else:
                pay.payment_status = "Unallocated"

            # Set related Sales Invoice
            pay.sales_invoice = pe_invoice_map.get(pay.payment_entry, "")

            # Determine collection type:
            # If the related invoice is NOT in the current shift's transactions,
            # it's an outstanding collection from a previous shift
            related_invoice = pe_invoice_map.get(pay.payment_entry)
            if related_invoice and related_invoice not in txn_invoice_names:
                pay.collection_type = "Outstanding Collection"
            else:
                pay.collection_type = "New Sale"

    # ──────────────────────────────────────────────────────────────
    # Summary Calculations
    # ──────────────────────────────────────────────────────────────
    def calculate_summaries(self):
        """Aggregate totals from child table rows into summary fields."""
        # Transaction summaries
        total_sales = 0
        total_paid = 0
        total_credit_sales = 0
        total_outstanding = 0

        for txn in self.pos_transactions or []:
            total_sales += flt(txn.grand_total)
            total_paid += flt(txn.paid_amount)
            total_outstanding += flt(txn.customer_outstanding)
            if txn.payment_status in ("Partial", "Outstanding"):
                total_credit_sales += flt(txn.grand_total)

        self.total_sales = total_sales
        self.total_paid = total_paid
        self.total_credit_sales = total_credit_sales
        self.total_outstanding = total_outstanding

        # Payment summaries — per Mode of Payment
        mop_totals = {}  # mode_of_payment -> total amount
        total_collected_outstanding = 0

        # Aggregate from payment reconciliation (which includes both
        # invoice payments and payment entries)
        for pay in self.payment_reconciliation or []:
            mop = (pay.mode_of_payment or "").strip()
            amount = flt(pay.expected_amount) - flt(pay.opening_amount)
            mop_totals[mop] = flt(mop_totals.get(mop, 0)) + amount

        # Calculate collected outstanding from pos_payments
        for pay in self.pos_payments or []:
            if pay.collection_type == "Outstanding Collection":
                total_collected_outstanding += flt(pay.paid_amount)

        self.total_collected_outstanding = total_collected_outstanding

        # Map MOP totals to predefined summary fields
        self.total_cash = 0
        self.total_knet = 0
        self.total_online = 0
        self.total_visa = 0
        self.total_other_mop = 0

        for mop, amount in mop_totals.items():
            field = MOP_FIELD_MAP.get(mop.lower())
            if field:
                current = flt(getattr(self, field, 0))
                setattr(self, field, current + amount)
            else:
                self.total_other_mop += amount

    # ──────────────────────────────────────────────────────────────
    # Status Indicators
    # ──────────────────────────────────────────────────────────────
    def set_status_indicators(self):
        """Set payment_status on each child row based on calculated amounts.
        Status colors in the form view:
        - Green  = Paid / Allocated
        - Orange = Partial
        - Red    = Outstanding / Unallocated
        """
        for txn in self.pos_transactions or []:
            outstanding = flt(txn.customer_outstanding)
            grand_total = flt(txn.grand_total)
            if outstanding <= 0:
                txn.payment_status = "Paid"
            elif outstanding < grand_total:
                txn.payment_status = "Partial"
            else:
                txn.payment_status = "Outstanding"

        for pay in self.pos_payments or []:
            unallocated = flt(pay.outstanding_amount)
            paid = flt(pay.paid_amount)
            if unallocated <= 0:
                pay.payment_status = "Allocated"
            elif unallocated < paid:
                pay.payment_status = "Partial"
            else:
                pay.payment_status = "Unallocated"

    @frappe.whitelist()
    def recalculate_closing_shift(self):
        """Manual trigger to recalculate all extended fields.
        Callable from the form via frappe.call."""
        self.calculate_transaction_details()
        self.calculate_payment_details()
        self.set_status_indicators()
        self.calculate_summaries()
        self.save()
        return {"message": _("Closing Shift recalculated successfully.")}

    @frappe.whitelist()
    def get_payment_reconciliation_details(self):
        currency = frappe.get_cached_value("Company", self.company, "default_currency")
        template_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "closing_shift_details.html",
        )
        with open(template_path, "r") as template_file:
            template = template_file.read()
        return frappe.render_template(
            template, {"data": self, "currency": currency}
        )


@frappe.whitelist()
def get_cashiers(doctype, txt, searchfield, start, page_len, filters):
    cashiers_list = frappe.get_all("POS Profile User", filters=filters, fields=["user"])
    return [c["user"] for c in cashiers_list]


@frappe.whitelist()
def get_pos_invoices(pos_opening_shift):
    submit_printed_invoices(pos_opening_shift)
    data = frappe.db.sql(
        """
	select
		name
	from
		`tabSales Invoice`
	where
		docstatus = 1 and posa_pos_opening_shift = %s
	""",
        (pos_opening_shift),
        as_dict=1,
    )

    data = [frappe.get_doc("Sales Invoice", d.name).as_dict() for d in data]

    return data


@frappe.whitelist()
def get_payments_entries(pos_opening_shift):
    return frappe.get_all(
        "Payment Entry",
        filters={
            "docstatus": 1,
            "reference_no": pos_opening_shift,
            "payment_type": "Receive",
        },
        fields=[
            "name",
            "mode_of_payment",
            "paid_amount",
            "reference_no",
            "posting_date",
            "party",
            "unallocated_amount",
        ],
    )


@frappe.whitelist()
def make_closing_shift_from_opening(opening_shift):
    opening_shift = json.loads(opening_shift)
    submit_printed_invoices(opening_shift.get("name"))
    closing_shift = frappe.new_doc("POS Closing Shift")
    closing_shift.pos_opening_shift = opening_shift.get("name")
    closing_shift.period_start_date = opening_shift.get("period_start_date")
    closing_shift.period_end_date = frappe.utils.get_datetime()
    closing_shift.pos_profile = opening_shift.get("pos_profile")
    closing_shift.user = opening_shift.get("user")
    closing_shift.company = opening_shift.get("company")
    closing_shift.grand_total = 0
    closing_shift.net_total = 0
    closing_shift.total_quantity = 0

    invoices = get_pos_invoices(opening_shift.get("name"))

    # Collect invoice names for batch queries
    invoice_names = [d.name for d in invoices]

    # Batch fetch outstanding amounts for all invoices
    invoice_outstanding_map = {}
    if invoice_names:
        outstanding_data = frappe.db.get_all(
            "Sales Invoice",
            filters={
                "name": ["in", invoice_names],
                "docstatus": 1,
            },
            fields=["name", "grand_total", "outstanding_amount"],
        )
        invoice_outstanding_map = {d.name: d for d in outstanding_data}

    # Batch fetch primary MOP per invoice
    mop_map = {}
    if invoice_names:
        mop_data = frappe.db.sql(
            """
            SELECT
                sip.parent AS invoice_name,
                sip.mode_of_payment,
                sip.amount
            FROM `tabSales Invoice Payment` sip
            INNER JOIN `tabSales Invoice` si ON si.name = sip.parent
            WHERE sip.parent IN %(invoice_names)s
                AND si.docstatus = 1
            ORDER BY sip.parent, sip.amount DESC
            """,
            {"invoice_names": invoice_names},
            as_dict=1,
        )
        for row in mop_data:
            if row.invoice_name not in mop_map:
                mop_map[row.invoice_name] = row.mode_of_payment

    pos_transactions = []
    taxes = []
    payments = []
    pos_payments_table = []
    for detail in opening_shift.get("balance_details"):
        payments.append(
            frappe._dict(
                {
                    "mode_of_payment": detail.get("mode_of_payment"),
                    "opening_amount": detail.get("amount") or 0,
                    "expected_amount": detail.get("amount") or 0,
                }
            )
        )

    for d in invoices:
        inv_data = invoice_outstanding_map.get(d.name)
        outstanding = flt(inv_data.outstanding_amount) if inv_data else 0
        grand_total = flt(d.grand_total)
        paid_amt = flt(grand_total - outstanding)
        primary_mop = mop_map.get(d.name, "")

        # Determine payment status
        if outstanding <= 0:
            payment_status = "Paid"
            credit_amount = 0
        elif outstanding < grand_total:
            payment_status = "Partial"
            credit_amount = outstanding
        else:
            payment_status = "Outstanding"
            credit_amount = grand_total

        pos_transactions.append(
            frappe._dict(
                {
                    "sales_invoice": d.name,
                    "posting_date": d.posting_date,
                    "grand_total": d.grand_total,
                    "customer": d.customer,
                    "mode_of_payment": primary_mop,
                    "payment_status": payment_status,
                    "customer_outstanding": outstanding,
                    "paid_amount": paid_amt,
                    "credit_amount": credit_amount,
                }
            )
        )
        closing_shift.grand_total += flt(d.grand_total)
        closing_shift.net_total += flt(d.net_total)
        closing_shift.total_quantity += flt(d.total_qty)

        for t in d.taxes:
            existing_tax = [
                tx
                for tx in taxes
                if tx.account_head == t.account_head and tx.rate == t.rate
            ]
            if existing_tax:
                existing_tax[0].amount += flt(t.tax_amount)
            else:
                taxes.append(
                    frappe._dict(
                        {
                            "account_head": t.account_head,
                            "rate": t.rate,
                            "amount": t.tax_amount,
                        }
                    )
                )

        for p in d.payments:
            existing_pay = [
                pay for pay in payments if pay.mode_of_payment == p.mode_of_payment
            ]
            if existing_pay:
                cash_mode_of_payment = frappe.get_value(
                    "POS Profile",
                    opening_shift.get("pos_profile"),
                    "posa_cash_mode_of_payment",
                )
                if not cash_mode_of_payment:
                    cash_mode_of_payment = "Cash"
                if existing_pay[0].mode_of_payment == cash_mode_of_payment:
                    amount = p.amount - d.change_amount
                else:
                    amount = p.amount
                existing_pay[0].expected_amount += flt(amount)
            else:
                payments.append(
                    frappe._dict(
                        {
                            "mode_of_payment": p.mode_of_payment,
                            "opening_amount": 0,
                            "expected_amount": p.amount,
                        }
                    )
                )

    # Collect current shift invoice names for collection_type detection
    txn_invoice_names = set(invoice_names)

    pos_payments = get_payments_entries(opening_shift.get("name"))

    # Batch fetch Payment Entry references for collection type detection
    pe_names = [py.name for py in pos_payments]
    pe_invoice_map = {}
    if pe_names:
        pe_ref_data = frappe.db.get_all(
            "Payment Entry Reference",
            filters={
                "parent": ["in", pe_names],
                "reference_doctype": "Sales Invoice",
            },
            fields=["parent", "reference_name", "allocated_amount"],
            order_by="allocated_amount desc",
        )
        for ref in pe_ref_data:
            if ref.parent not in pe_invoice_map:
                pe_invoice_map[ref.parent] = ref.reference_name

    for py in pos_payments:
        unallocated = flt(py.get("unallocated_amount", 0))
        paid = flt(py.paid_amount)
        related_invoice = pe_invoice_map.get(py.name, "")

        # Determine payment status
        if unallocated <= 0:
            pe_status = "Allocated"
        elif unallocated < paid:
            pe_status = "Partial"
        else:
            pe_status = "Unallocated"

        # Determine collection type
        if related_invoice and related_invoice not in txn_invoice_names:
            collection_type = "Outstanding Collection"
        else:
            collection_type = "New Sale"

        pos_payments_table.append(
            frappe._dict(
                {
                    "payment_entry": py.name,
                    "mode_of_payment": py.mode_of_payment,
                    "paid_amount": py.paid_amount,
                    "posting_date": py.posting_date,
                    "customer": py.party,
                    "payment_status": pe_status,
                    "outstanding_amount": unallocated,
                    "sales_invoice": related_invoice,
                    "collection_type": collection_type,
                }
            )
        )
        existing_pay = [
            pay for pay in payments if pay.mode_of_payment == py.mode_of_payment
        ]
        if existing_pay:
            existing_pay[0].expected_amount += flt(py.paid_amount)
        else:
            payments.append(
                frappe._dict(
                    {
                        "mode_of_payment": py.mode_of_payment,
                        "opening_amount": 0,
                        "expected_amount": py.paid_amount,
                    }
                )
            )

    closing_shift.set("pos_transactions", pos_transactions)
    closing_shift.set("payment_reconciliation", payments)
    closing_shift.set("taxes", taxes)
    closing_shift.set("pos_payments", pos_payments_table)

    # Calculate summaries on the new doc
    _calculate_summaries_for_new_doc(closing_shift, payments, pos_payments_table)

    return closing_shift


def _calculate_summaries_for_new_doc(closing_shift, payments, pos_payments_table):
    """Calculate summary fields for a newly created closing shift doc
    (before it's saved, so we can't use self methods)."""
    total_sales = 0
    total_paid = 0
    total_credit_sales = 0
    total_outstanding = 0

    for txn in closing_shift.pos_transactions:
        total_sales += flt(txn.grand_total)
        total_paid += flt(txn.paid_amount)
        total_outstanding += flt(txn.customer_outstanding)
        if txn.payment_status in ("Partial", "Outstanding"):
            total_credit_sales += flt(txn.grand_total)

    closing_shift.total_sales = total_sales
    closing_shift.total_paid = total_paid
    closing_shift.total_credit_sales = total_credit_sales
    closing_shift.total_outstanding = total_outstanding

    # Payment MOP summaries
    total_cash = 0
    total_knet = 0
    total_online = 0
    total_visa = 0
    total_other_mop = 0
    total_collected_outstanding = 0

    for pay in payments:
        mop = (pay.mode_of_payment or "").strip()
        amount = flt(pay.expected_amount) - flt(pay.opening_amount)
        field = MOP_FIELD_MAP.get(mop.lower())
        if field == "total_cash":
            total_cash += amount
        elif field == "total_knet":
            total_knet += amount
        elif field == "total_online":
            total_online += amount
        elif field == "total_visa":
            total_visa += amount
        else:
            total_other_mop += amount

    for pay in pos_payments_table:
        if pay.get("collection_type") == "Outstanding Collection":
            total_collected_outstanding += flt(pay.paid_amount)

    closing_shift.total_cash = total_cash
    closing_shift.total_knet = total_knet
    closing_shift.total_online = total_online
    closing_shift.total_visa = total_visa
    closing_shift.total_other_mop = total_other_mop
    closing_shift.total_collected_outstanding = total_collected_outstanding


@frappe.whitelist()
def submit_closing_shift(closing_shift):
    closing_shift = json.loads(closing_shift)
    closing_shift_doc = frappe.get_doc(closing_shift)
    closing_shift_doc.flags.ignore_permissions = True
    closing_shift_doc.save()
    closing_shift_doc.submit()
    return closing_shift_doc.name


def submit_printed_invoices(pos_opening_shift):
    invoices_list = frappe.get_all(
        "Sales Invoice",
        filters={
            "posa_pos_opening_shift": pos_opening_shift,
            "docstatus": 0,
            "posa_is_printed": 1,
        },
    )
    for invoice in invoices_list:
        invoice_doc = frappe.get_doc("Sales Invoice", invoice.name)
        invoice_doc.submit()
