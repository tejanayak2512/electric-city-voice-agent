from io import BytesIO

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .billing import get_connection_fee_breakdown


def build_connection_application_pdf(connection):
    """Generate official DISCOM New Connection Application & Paid Receipt PDF."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=34,
        rightMargin=34,
        topMargin=30,
        bottomMargin=30,
        title=f"Application {connection.application_number or connection.application_id}",
    )

    styles = getSampleStyleSheet()
    style_title = ParagraphStyle(
        "DiscomHeaderTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=15,
        leading=19,
        textColor=colors.white,
    )
    style_sub = ParagraphStyle(
        "DiscomHeaderSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor("#dbeafe"),
    )
    style_section = ParagraphStyle(
        "SectionTitle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#063b78"),
        spaceBefore=8,
        spaceAfter=5,
    )
    style_label = ParagraphStyle(
        "CellLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#102b4c"),
    )
    style_val = ParagraphStyle(
        "CellVal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#0f172a"),
    )
    style_note = ParagraphStyle(
        "FootNote",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#475569"),
    )

    fee = get_connection_fee_breakdown(
        connection.property_type,
        total_override=connection.connection_fee_amount or None,
    )
    submitted_ist = timezone.localtime(connection.created_at).strftime("%d %b %Y, %I:%M %p IST")
    paid_ist = (
        timezone.localtime(connection.connection_paid_at).strftime("%d %b %Y, %I:%M:%S %p IST")
        if connection.connection_paid_at
        else "Settled"
    )
    app_number = connection.application_number or f"APP-{timezone.localdate().year}-{connection.id + 1000}"

    elements = []

    # Header Banner Table
    header_table = Table(
        [
            [
                Paragraph("ELECTRICITY DEPARTMENT &bull; DISCOM CONSUMER SERVICES", style_title),
                "",
            ],
            [
                Paragraph("Official New Electricity Connection Application &amp; Fee Receipt", style_sub),
                Paragraph(
                    f"<b>Application No: {app_number}</b><br/><b>Request ID: {connection.application_id}</b>",
                    ParagraphStyle(
                        "HdrRef",
                        parent=styles["Normal"],
                        fontName="Helvetica-Bold",
                        fontSize=9.5,
                        leading=13,
                        textColor=colors.HexColor("#fef08a"),
                        alignment=2,
                    ),
                ),
            ],
        ],
        colWidths=[327, 200],
    )
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#063b78")),
                ("SPAN", (0, 0), (-1, 0)),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elements.append(header_table)
    elements.append(Spacer(1, 10))

    # Section 1: Application & Consumer Details
    elements.append(Paragraph("1. Official Application &amp; Premises Particulars", style_section))
    category_label = connection.property_type
    if connection.industry_type:
        category_label = f"{connection.property_type} ({connection.industry_type})"

    applicant_rows = [
        [
            Paragraph("Application Number", style_label),
            Paragraph(app_number, style_val),
            Paragraph("Request ID", style_label),
            Paragraph(connection.application_id, style_val),
        ],
        [
            Paragraph("Applicant Name", style_label),
            Paragraph(connection.applicant_name, style_val),
            Paragraph("Consumer Account", style_label),
            Paragraph(f"@{connection.user.username}", style_val),
        ],
        [
            Paragraph("Mobile Number", style_label),
            Paragraph(connection.mobile_number, style_val),
            Paragraph("ID Proof / License No.", style_label),
            Paragraph(connection.id_proof_number, style_val),
        ],
        [
            Paragraph("Connection Category", style_label),
            Paragraph(category_label, style_val),
            Paragraph("Sanctioned Load", style_label),
            Paragraph(f"{connection.requested_load_kw} kW", style_val),
        ],
        [
            Paragraph("Premises Address", style_label),
            Paragraph(connection.house_address, style_val),
            Paragraph("Applied On", style_label),
            Paragraph(submitted_ist, style_val),
        ],
    ]
    app_table = Table(applicant_rows, colWidths=[110, 155, 110, 152])
    app_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#bfd6f2")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eff6ff")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eff6ff")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    elements.append(app_table)
    elements.append(Spacer(1, 8))

    # Section 2: New Connection Charges & Gateway Settlement
    elements.append(Paragraph("2. New Connection Charges &amp; DISCOM Payment Receipt", style_section))
    fee_rows = [
        [
            Paragraph("Fee Receipt No.", style_label),
            Paragraph(connection.connection_receipt_number or "--", style_val),
            Paragraph("Gateway TXN ID", style_label),
            Paragraph(connection.connection_transaction_id or "--", style_val),
        ],
        [
            Paragraph("Payment Method", style_label),
            Paragraph(connection.connection_payment_method or "DISCOM Gateway", style_val),
            Paragraph("Paid Timestamp", style_label),
            Paragraph(paid_ist, style_val),
        ],
        [
            Paragraph("Processing &amp; Survey Charge", style_label),
            Paragraph(f"Rs {fee['processing_fee']}", style_val),
            Paragraph("Service Line Charge", style_label),
            Paragraph(f"Rs {fee['service_line_charge']}", style_val),
        ],
        [
            Paragraph("Meter Security Deposit", style_label),
            Paragraph(f"Rs {fee['security_deposit']}", style_val),
            Paragraph("Total Connection Fee Paid", style_label),
            Paragraph(f"Rs {fee['total']} (PAID)", style_label),
        ],
    ]
    fee_table = Table(fee_rows, colWidths=[125, 140, 125, 137])
    fee_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#a7f3d0")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#ecfdf5")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#ecfdf5")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    elements.append(fee_table)
    elements.append(Spacer(1, 8))

    # Section 3: Verification & Grid Alignment Status
    elements.append(Paragraph("3. Multi-Stage Verification &amp; Grid Allocation Status", style_section))
    tf_label = (
        f"{connection.transformer.code} - {connection.transformer.substation_name} ({connection.transformer.zone})"
        if connection.transformer
        else "Pending Department Alignment"
    )
    pay_ver_label = (
        f"Verified ({connection.payment_verified_by})"
        if connection.payment_verified
        else "Pending Accounts / Dept Payment Verification"
    )
    field_ver_label = (
        f"Verified ({connection.verified_by.employee_id})"
        if connection.verified_by
        else "Pending Field Inspection"
    )
    verify_rows = [
        [
            Paragraph("Verification 1 (Fee Paid)", style_label),
            Paragraph(pay_ver_label, style_val),
        ],
        [
            Paragraph("Verification 2 (Field &amp; Feeder)", style_label),
            Paragraph(field_ver_label, style_val),
        ],
        [
            Paragraph("Aligned Substation &amp; Transformer", style_label),
            Paragraph(tf_label, style_val),
        ],
        [
            Paragraph("Official Meter No. / Serial", style_label),
            Paragraph(
                f"{connection.meter_number} / {connection.original_meter_serial}"
                if connection.meter_number
                else "Pending Administration Approval",
                style_val,
            ),
        ],
        [
            Paragraph("Current Application Stage", style_label),
            Paragraph(connection.status, style_val),
        ],
    ]
    verify_table = Table(verify_rows, colWidths=[175, 352])
    verify_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f8fafc")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    elements.append(verify_table)
    elements.append(Spacer(1, 14))

    elements.append(
        Paragraph(
            f"Note: System-generated official Electricity Department Application Certificate for {app_number} "
            f"(Request ID: {connection.application_id}). Retain this PDF for physical inspection and meter commissioning.",
            style_note,
        )
    )

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


def build_power_bill_invoice_pdf(bill):
    """Generate official DISCOM Consumer Electricity Tax Invoice & Payment Receipt PDF."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=34,
        rightMargin=34,
        topMargin=28,
        bottomMargin=28,
        title=f"Invoice {bill.bill_number}",
    )

    styles = getSampleStyleSheet()
    style_title = ParagraphStyle(
        "DiscomInvTitle",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=14,
        leading=18,
        textColor=colors.white,
    )
    style_sub = ParagraphStyle(
        "DiscomInvSub",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
        textColor=colors.HexColor("#dbeafe"),
    )
    style_section = ParagraphStyle(
        "InvSectionTitle",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor("#063b78"),
        spaceBefore=7,
        spaceAfter=4,
    )
    style_label = ParagraphStyle(
        "InvLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#102b4c"),
    )
    style_val = ParagraphStyle(
        "InvVal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=11.5,
        textColor=colors.HexColor("#0f172a"),
    )
    style_total_label = ParagraphStyle(
        "InvTotalLabel",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=10,
        leading=13,
        textColor=colors.HexColor("#063b78"),
    )
    style_total_val = ParagraphStyle(
        "InvTotalVal",
        parent=styles["Normal"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#047857"),
    )
    style_note = ParagraphStyle(
        "InvNote",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=8,
        leading=11,
        textColor=colors.HexColor("#475569"),
    )

    connection = bill.connection
    user = connection.user if connection else None
    is_paid = (bill.status == "Paid")
    status_bg = "#059669" if is_paid else "#d97706"
    status_text = "PAID IN FULL" if is_paid else "PAYMENT DUE"

    paid_str = (
        timezone.localtime(bill.paid_at).strftime("%d %b %Y, %I:%M:%S %p IST")
        if bill.paid_at
        else (timezone.localtime(bill.created_at).strftime("%d %b %Y, %I:%M %p IST") if is_paid else "Pending Settle")
    )

    elements = []

    # 1. Header Table
    header_table = Table(
        [
            [
                Paragraph("ELECTRICITY DEPARTMENT &bull; DISCOM CONSUMER REVENUE DIVISION", style_title),
                "",
            ],
            [
                Paragraph("Official Electricity Consumer Tax Invoice &amp; Payment Receipt", style_sub),
                Paragraph(
                    f"<b>Bill No: {bill.bill_number}</b><br/>"
                    f"<b>Status: <font color='{status_bg}'>{status_text}</font></b>",
                    ParagraphStyle(
                        "HdrBillRef",
                        parent=styles["Normal"],
                        fontName="Helvetica-Bold",
                        fontSize=9,
                        leading=12,
                        textColor=colors.HexColor("#fef08a"),
                        alignment=2,
                    ),
                ),
            ],
        ],
        colWidths=[337, 190],
    )
    header_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#063b78")),
                ("SPAN", (0, 0), (-1, 0)),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elements.append(header_table)
    elements.append(Spacer(1, 8))

    # 2. Section: Consumer & Connection Details
    elements.append(Paragraph("1. Consumer Account &amp; Connection Particulars", style_section))
    tf_str = (
        f"{connection.transformer.code} ({connection.transformer.substation_name})"
        if (connection and connection.transformer)
        else "Primary Feeder Grid"
    )
    consumer_rows = [
        [
            Paragraph("Consumer Name", style_label),
            Paragraph(connection.applicant_name if connection else (user.full_name if user else "Consumer"), style_val),
            Paragraph("Consumer ID / User", style_label),
            Paragraph(f"@{user.username}" if user else "--", style_val),
        ],
        [
            Paragraph("Smart Meter Number", style_label),
            Paragraph(connection.meter_number if connection else "--", style_val),
            Paragraph("Request / App ID", style_label),
            Paragraph(connection.application_id if connection else "--", style_val),
        ],
        [
            Paragraph("Premises Address", style_label),
            Paragraph(connection.house_address if connection else "Registered Premise", style_val),
            Paragraph("Feeder / Transformer", style_label),
            Paragraph(tf_str, style_val),
        ],
        [
            Paragraph("Tariff Category", style_label),
            Paragraph(f"{bill.bill_type} ({connection.property_type if connection else 'Domestic'})", style_val),
            Paragraph("Billing Cycle / Month", style_label),
            Paragraph(bill.billing_month or "Current", style_val),
        ],
    ]
    consumer_table = Table(consumer_rows, colWidths=[115, 150, 115, 147])
    consumer_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#bfd6f2")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eff6ff")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eff6ff")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    elements.append(consumer_table)
    elements.append(Spacer(1, 8))

    # 3. Section: Energy Assessment & Meter Ledger Breakdown
    elements.append(Paragraph("2. Energy Assessment &amp; Meter Ledger Charges", style_section))
    ledger_rows = [
        [
            Paragraph("<b>Particulars / Item</b>", style_label),
            Paragraph("<b>Meter Units / Metric</b>", style_label),
            Paragraph("<b>Rate (₹)</b>", style_label),
            Paragraph("<b>Total Amount (₹)</b>", style_label),
        ],
        [
            Paragraph("Previous Meter Reading", style_val),
            Paragraph(f"{bill.previous_reading} kWh", style_val),
            Paragraph("--", style_val),
            Paragraph("--", style_val),
        ],
        [
            Paragraph("Current Meter Reading", style_val),
            Paragraph(f"{bill.current_reading} kWh", style_val),
            Paragraph("--", style_val),
            Paragraph("--", style_val),
        ],
        [
            Paragraph("Net Energy Consumed &amp; Billed", style_val),
            Paragraph(f"<b>{bill.units_billed} kWh</b>", style_val),
            Paragraph(f"₹ {bill.rate_per_unit}/unit", style_val),
            Paragraph(f"₹ {bill.energy_charge:.2f}", style_val),
        ],
        [
            Paragraph("Base / Fixed Line Charge", style_val),
            Paragraph("1 Cycle Base", style_val),
            Paragraph(f"₹ {bill.base_fare:.2f}", style_val),
            Paragraph(f"₹ {bill.base_fare:.2f}", style_val),
        ],
        [
            Paragraph("Arrears / Carried Surcharge", style_val),
            Paragraph(bill.carried_from_bills or "Nil", style_val),
            Paragraph("--", style_val),
            Paragraph(f"₹ {bill.arrears_amount:.2f}", style_val),
        ],
        [
            Paragraph("<b>NET INVOICE TOTAL PAYABLE</b>", style_total_label),
            Paragraph(f"<b>{bill.units_billed} Units Total</b>", style_total_label),
            Paragraph("", style_val),
            Paragraph(f"<b>₹ {bill.total_amount:.2f}</b>", style_total_val),
        ],
    ]
    ledger_table = Table(ledger_rows, colWidths=[187, 130, 95, 115])
    ledger_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#ecfdf5")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    elements.append(ledger_table)
    elements.append(Spacer(1, 8))

    # 4. Section: Gateway Settlement & Payment Receipt
    elements.append(Paragraph("3. Gateway Settlement &amp; Payment Confirmation", style_section))
    txn_ref = bill.transaction_id or (f"TXN-DISCOM-{bill.id:05d}" if is_paid else "Awaiting Settlement")
    method_ref = bill.payment_method or ("Online DISCOM Gateway" if is_paid else "Pending")

    settle_rows = [
        [
            Paragraph("Payment Status", style_label),
            Paragraph(f"<b>{status_text}</b>", style_total_val if is_paid else style_val),
            Paragraph("Paid Amount", style_label),
            Paragraph(f"<b>₹ {bill.total_amount:.2f}</b>", style_total_val if is_paid else style_val),
        ],
        [
            Paragraph("Gateway Transaction ID", style_label),
            Paragraph(txn_ref, style_val),
            Paragraph("Payment Method", style_label),
            Paragraph(method_ref, style_val),
        ],
        [
            Paragraph("Settlement Timestamp", style_label),
            Paragraph(paid_str, style_val),
            Paragraph("Receipt Clearance", style_label),
            Paragraph("DISCOM Server Verified &bull; Legally Audited" if is_paid else "Unpaid Bill", style_val),
        ],
    ]
    settle_table = Table(settle_rows, colWidths=[130, 135, 125, 137])
    settle_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#a7f3d0" if is_paid else "#fde68a")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#ecfdf5" if is_paid else "#fffbeb")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#ecfdf5" if is_paid else "#fffbeb")),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    elements.append(settle_table)
    elements.append(Spacer(1, 12))

    # Footer note
    elements.append(
        Paragraph(
            f"Official System-Generated Tax Invoice for Meter {connection.meter_number if connection else '--'} "
            f"(Bill Number: {bill.bill_number}). This document serves as the official financial receipt for DISCOM consumer accounts. "
            f"Electricity Helpline: 1912 &bull; 24/7 Consumer Care Portal.",
            style_note,
        )
    )

    doc.build(elements)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes
