"""Knowledge Base Builder for AI-Powered Voice Calling Agent.

Dynamically extracts live database facts (user connection, power supply,
transformers, tariffs, billing, maintenance notices) to ground LLM responses
in real-time project state.
"""

from decimal import Decimal
from typing import Any, Dict, Optional
from django.utils import timezone


def get_live_knowledge_base(user_account=None) -> Dict[str, Any]:
    """Compile comprehensive live database state for the voice agent knowledge base."""
    from admins.models import (
        MaintenanceSchedule,
        OperationMaintenanceNotification,
        SiteNewsAnnouncement,
        TariffSetting,
        Transformer,
    )
    from complaints.models import Complaint
    from users.models import ConnectionApplication, PowerBill

    # 1. Tariff & Pricing Settings
    tariff = TariffSetting.get_active()
    tariff_info = {
        "domestic_base_price": float(tariff.base_fare_rs),
        "domestic_unit_rate": float(tariff.domestic_rate_per_unit),
        "commercial_base_price": float(tariff.base_fare_rs),
        "commercial_unit_rate": float(tariff.commercial_rate_per_unit),
        "domestic_connection_fee": float(tariff.domestic_connection_fee),
        "commercial_connection_fee": float(tariff.commercial_connection_fee),
    }

    # 2. Grid & Substation Transformers
    transformers = Transformer.objects.all()
    grid_units = []
    for tf in transformers:
        grid_units.append({
            "code": tf.code,
            "name": tf.name,
            "substation": tf.substation_name,
            "zone": tf.zone,
            "capacity_kva": tf.capacity_kva,
            "is_power_on": tf.is_power_on,
            "voltage": tf.voltage,
            "frequency": tf.frequency,
        })

    # 3. Active & Scheduled Maintenance
    active_maint = MaintenanceSchedule.objects.filter(
        status__in=[
            MaintenanceSchedule.STATUS_APPROVED,
            MaintenanceSchedule.STATUS_ACTIVE_CUT,
            MaintenanceSchedule.STATUS_PENDING,
        ]
    ).order_by("scheduled_start")[:5]

    maintenance_list = []
    for m in active_maint:
        maintenance_list.append({
            "title": m.title,
            "substation": m.transformer.substation_name if m.transformer else "General Grid",
            "feeder_line": m.transformer.name if m.transformer else "Area Feeder",
            "start": m.scheduled_start.strftime("%Y-%m-%d %H:%M") if m.scheduled_start else "N/A",
            "end": m.scheduled_end.strftime("%Y-%m-%d %H:%M") if m.scheduled_end else "N/A",
            "status": m.status,
            "reason": m.reason,
            "is_power_cut_active": m.status == MaintenanceSchedule.STATUS_ACTIVE_CUT,
        })

    # 4. News & Live Advisories
    news_items = list(
        SiteNewsAnnouncement.objects.filter(is_active=True).values_list("title", flat=True)[:3]
    )
    advisories = list(
        OperationMaintenanceNotification.objects.filter(is_active=True).values_list("message", flat=True)[:3]
    )

    # 5. User Specific Profile (if authenticated or identified)
    user_info = None
    if user_account:
        conn = (
            ConnectionApplication.objects.filter(
                user=user_account,
                status=ConnectionApplication.STATUS_CONNECTED,
            ).order_by("-updated_at").first()
            or ConnectionApplication.objects.filter(user=user_account).order_by("-created_at").first()
        )
        if not conn and user_account.mobile_number:
            conn = (
                ConnectionApplication.objects.filter(
                    mobile_number=user_account.mobile_number,
                    status=ConnectionApplication.STATUS_CONNECTED,
                ).first()
                or ConnectionApplication.objects.filter(mobile_number=user_account.mobile_number).first()
            )

        req_id = (conn.application_id or conn.application_number or f"REQ-{conn.id}") if conn else "No Request ID"
        meter_num = conn.meter_number if (conn and conn.meter_number) else ("Pending Allotment" if conn else "No Meter Assigned")

        latest_bill = None
        latest_paid_bill = None
        bills_unpaid = []
        all_bills = []
        unbilled_units = 0.0
        total_consumed = 0.0
        last_billed = 0.0
        current_reading = 0.0

        if conn:
            unbilled_units = float(conn.unbilled_units or Decimal("0.00"))
            total_consumed = float(conn.total_units_consumed or Decimal("0.00"))
            last_billed = float(conn.last_billed_reading or Decimal("0.00"))
            current_reading = round(last_billed + unbilled_units, 2)

            bill_qs = PowerBill.objects.filter(connection=conn).order_by("-billing_month", "-created_at")
            latest_bill = bill_qs.first()
            paid_qs = bill_qs.filter(status=PowerBill.STATUS_PAID)
            latest_paid_bill = paid_qs.first()
            for b in bill_qs[:8]:
                all_bills.append({
                    "bill_number": b.bill_number,
                    "month": b.billing_month,
                    "units_billed": float(b.units_billed),
                    "amount": float(b.total_amount),
                    "base_fare": float(b.base_fare),
                    "energy_charge": float(b.energy_charge),
                    "status": b.status,
                    "created_at": b.created_at.strftime("%Y-%m-%d") if b.created_at else "N/A",
                })
            unpaid_qs = bill_qs.filter(status=PowerBill.STATUS_UNPAID)
            for b in unpaid_qs:
                bills_unpaid.append({
                    "bill_number": b.bill_number,
                    "month": b.billing_month,
                    "units": float(b.units_billed),
                    "base_fare": float(b.base_fare),
                    "energy_charge": float(b.energy_charge),
                    "amount": float(b.total_amount),
                    "created_at": b.created_at.strftime("%Y-%m-%d") if b.created_at else "N/A",
                })

        app_telem = None
        if conn:
            try:
                from users.billing import get_connection_appliance_telemetry
                raw_telem = get_connection_appliance_telemetry(conn)
                if raw_telem:
                    app_telem = {
                        "on_count": raw_telem.get("on_count", 0),
                        "running_count": raw_telem.get("running_count", 0),
                        "total_count": raw_telem.get("total_count", 0),
                        "active_watts": raw_telem.get("active_watts", 0),
                        "active_kw": raw_telem.get("active_kw", 0.0),
                        "hourly_cost_rs": raw_telem.get("hourly_cost_rs", 0.0),
                        "appliances": [
                            {
                                "key": a.get("key"),
                                "name": a.get("name"),
                                "wattage": a.get("wattage"),
                                "is_on": a.get("switch_on"),
                                "is_running": a.get("is_running"),
                                "active_watts": a.get("active_watts"),
                            }
                            for a in raw_telem.get("appliances", [])
                        ],
                    }
            except Exception as e:
                logger.warning(f"Error fetching appliance telemetry: {e}")

        user_complaints = list(
            Complaint.objects.filter(user_account=user_account).values(
                "complaint_number", "complaint_id", "issue_type", "status", "estimated_resolution", "created_at"
            )[:5]
        )

        user_info = {
            "full_name": user_account.full_name,
            "username": user_account.username,
            "mobile": user_account.mobile_number,
            "email": user_account.email,
            "house_power_on": user_account.house_power_on,
            "connection_status": conn.status if conn else "No Active Connection",
            "request_id": req_id,
            "application_id": conn.application_id if conn else None,
            "application_number": conn.application_number if conn else None,
            "meter_number": meter_num,
            "has_active_meter": bool(conn and conn.meter_number),
            "property_type": conn.property_type if conn else "Domestic",
            "requested_load_kw": f"{conn.requested_load_kw} kW" if (conn and conn.requested_load_kw) else "1.0 kW",
            "house_address": conn.house_address if (conn and conn.house_address) else "Registered Address",
            "application_created_at": conn.created_at.strftime("%d %B %Y at %I:%M %p") if (conn and conn.created_at) else None,
            "application_created_date": conn.created_at.strftime("%d %B %Y") if (conn and conn.created_at) else None,
            "application_verified_at": conn.verified_at.strftime("%d %B %Y at %I:%M %p") if (conn and conn.verified_at) else None,
            "meter_approved_at": conn.approved_at.strftime("%d %B %Y at %I:%M %p") if (conn and conn.approved_at) else None,
            "meter_approved_date": conn.approved_at.strftime("%d %B %Y") if (conn and conn.approved_at) else None,
            "connection_connected_at": conn.connected_at.strftime("%d %B %Y at %I:%M %p") if (conn and conn.connected_at) else None,
            "connection_connected_date": conn.connected_at.strftime("%d %B %Y") if (conn and conn.connected_at) else None,
            "transformer": conn.transformer.name if conn and conn.transformer else "Unassigned",
            "transformer_code": conn.transformer.code if conn and conn.transformer else "N/A",
            "transformer_power_on": conn.transformer.is_power_on if conn and conn.transformer else True,
            "is_power_cut": conn.is_power_cut if conn else False,
            "power_cut_reason": (getattr(conn, "power_cut_remark", None) or getattr(conn, "power_cut_reason", None)) if conn and conn.is_power_cut else None,
            "effective_power_supply": (
                user_account.house_power_on
                and (not conn.is_power_cut if conn else True)
                and (conn.transformer.is_power_on if conn and conn.transformer else True)
            ),
            "prepaid_units_remaining": (
                float(conn.prepaid_units_remaining)
                if conn and conn.property_type == ConnectionApplication.PROPERTY_COMMERCIAL and hasattr(conn, 'prepaid_units_remaining')
                else None
            ),
            "latest_bill": {
                "month": latest_bill.billing_month if latest_bill else "None",
                "total_units": float(latest_bill.units_billed) if latest_bill else 0,
                "amount": float(latest_bill.total_amount) if latest_bill else 0,
                "base_fare": float(latest_bill.base_fare) if latest_bill else 0,
                "status": latest_bill.status if latest_bill else "N/A",
            } if latest_bill else None,
            "latest_paid_bill": {
                "bill_number": latest_paid_bill.bill_number if latest_paid_bill else None,
                "month": latest_paid_bill.billing_month if latest_paid_bill else "None",
                "total_units": float(latest_paid_bill.units_billed) if latest_paid_bill else 0,
                "amount": float(latest_paid_bill.total_amount) if latest_paid_bill else 0,
                "base_fare": float(latest_paid_bill.base_fare) if latest_paid_bill else 0,
                "status": latest_paid_bill.status if latest_paid_bill else "N/A",
            } if latest_paid_bill else None,
            "unbilled_units": unbilled_units,
            "total_units_consumed": total_consumed,
            "last_billed_reading": last_billed,
            "current_meter_reading": current_reading,
            "monthly_bills": all_bills,
            "unpaid_bills": bills_unpaid,
            "existing_complaints": user_complaints,
            "appliance_telemetry": app_telem,
        }

    return {
        "department_name": "Electricity Department / State DISCOM",
        "service": "24/7 AI-Powered Telephonic Consumer Care Voice Agent",
        "tariffs": tariff_info,
        "grid_transformers": grid_units,
        "maintenance_schedules": maintenance_list,
        "news_announcements": news_items,
        "advisories": advisories,
        "caller_profile": user_info,
        "standard_sla": {
            "power_outage": "2 to 4 hours",
            "voltage_fluctuation": "4 to 6 hours",
            "critical_hazard": "Immediate emergency response (within 30 to 45 minutes)",
            "meter_fault": "24 to 48 hours",
        },
        "appliance_ratings": {
            "Air Conditioner (1.5 Ton)": "1500 Watts (approx 1.5 units/hr)",
            "Water Heater / Geyser": "2000 Watts (approx 2 units/hr)",
            "Refrigerator": "250 Watts (approx 2 to 3 units/day)",
            "Ceiling Fan": "75 Watts (approx 0.075 units/hr)",
            "LED Bulb": "10 Watts (approx 0.01 units/hr)",
            "Television": "100 Watts (approx 0.1 units/hr)",
            "Washing Machine": "500 Watts",
            "Microwave Oven": "1200 Watts",
            "Induction Cooktop": "1800 Watts",
        },
    }


def format_knowledge_base_prompt(kb: Dict[str, Any]) -> str:
    """Format live knowledge base facts into a concise markdown context prompt for the LLM."""
    lines = [
        "### REAL-TIME ELECTRICITY DEPARTMENT KNOWLEDGE BASE (LIVE DATABASE STATE):",
        f"- Utility: {kb['department_name']} ({kb['service']})",
        f"- Domestic Tariff: Base Price Rs {kb['tariffs']['domestic_base_price']}, Unit Rate Rs {kb['tariffs']['domestic_unit_rate']}/kWh.",
        f"- Commercial Tariff: Base Price Rs {kb['tariffs']['commercial_base_price']}, Unit Rate Rs {kb['tariffs']['commercial_unit_rate']}/kWh.",
        f"- Connection Fees: Domestic Rs {kb['tariffs']['domestic_connection_fee']}, Commercial Rs {kb['tariffs']['commercial_connection_fee']}.",
    ]

    # Grid Substations
    lines.append("- Substation Transformers:")
    for tf in kb["grid_transformers"]:
        status = "ONLINE" if tf["is_power_on"] else "OFFLINE (POWER CUT)"
        lines.append(f"  * [{tf['code']}] {tf['name']} ({tf['substation']}) - Voltage: {tf['voltage']}, Status: {status}")

    # Active Maintenance
    if kb["maintenance_schedules"]:
        lines.append("- Active / Upcoming Maintenance:")
        for m in kb["maintenance_schedules"]:
            cut_status = "POWER CUT IS CURRENTLY ACTIVE" if m["is_power_cut_active"] else "Planned Scheduled Maintenance"
            lines.append(f"  * {m['title']} at {m['substation']} ({m['feeder_line']}) | {m['start']} to {m['end']} | {cut_status} | Reason: {m['reason']}")
    else:
        lines.append("- Maintenance: No scheduled power cut active at this moment across primary grid.")

    # News & Advisories
    if kb["news_announcements"]:
        lines.append("- Live News Announcements: " + "; ".join(kb["news_announcements"]))
    if kb["advisories"]:
        lines.append("- Operating Advisories: " + "; ".join(kb["advisories"]))

    # Caller Details
    cp = kb.get("caller_profile")
    if cp:
        lines.append("\n### CURRENT IDENTIFIED CALLER (CONNECTED USER ACCOUNT):")
        lines.append(f"- Name: {cp['full_name']} (Username: {cp['username']}, Mobile: {cp['mobile']})")
        lines.append(f"- Connection Status: {cp['connection_status']}, Property: {cp['property_type']}")
        lines.append(f"- Connection Request ID: {cp['request_id']}, Meter Number: {cp['meter_number']}, Sanctioned Load: {cp['requested_load_kw']}")
        lines.append(f"- Address: {cp['house_address']}")
        if cp.get("application_created_at"):
            lines.append(f"- Application Submission Date & Time: {cp['application_created_at']}")
        if cp.get("meter_approved_at"):
            lines.append(f"- Smart Meter Issuance Date & Time: {cp['meter_approved_at']}")
        if cp.get("connection_connected_at"):
            lines.append(f"- Premise Connection Energized Date & Time: {cp['connection_connected_at']}")
        lines.append(f"- Connected Transformer: {cp['transformer']} (Code: {cp['transformer_code']}, Online: {cp['transformer_power_on']})")
        lines.append(f"- Premise Breaker Switch: {'ON' if cp['house_power_on'] else 'OFF (User House Switch is Turned Off)'}")
        lines.append(f"- Feeder Power Cut: {'YES - ' + str(cp['power_cut_reason']) if cp['is_power_cut'] else 'NO (Feeder is Live)'}")
        lines.append(f"- Effective Power Delivery to Premise: {'LIVE (POWER IS ON)' if cp['effective_power_supply'] else 'OUTAGE (POWER IS CUT)'}")

        if cp.get("prepaid_units_remaining") is not None:
            lines.append(f"- Commercial Prepaid Units Remaining: {cp['prepaid_units_remaining']} units")

        lines.append(f"- Active Meter Unbilled Consumption (Current Month / November Ongoing): {cp.get('unbilled_units', 0.0)} units")
        lines.append(f"- Total Cumulative Units Consumed: {cp.get('total_units_consumed', 0.0)} units")
        lines.append(f"- Current Live Meter Reading: {cp.get('current_meter_reading', 0.0)} kWh (Last Billed Reading: {cp.get('last_billed_reading', 0.0)} kWh)")

        if cp.get("monthly_bills"):
            b_list = [f"{b['month']}: {b['units_billed']} units (Rs {b['amount']}, {b['status']})" for b in cp["monthly_bills"]]
            lines.append(f"- Historical Monthly Consumption: {'; '.join(b_list)}")

        if cp.get("latest_bill"):
            b = cp["latest_bill"]
            lines.append(f"- Latest Generated Bill: Month {b['month']}, Amount Rs {b['amount']}, Units: {b['total_units']}, Status: {b['status']}")

        if cp.get("latest_paid_bill"):
            pb = cp["latest_paid_bill"]
            lines.append(f"- Latest Paid Bill: Month {pb['month']}, Amount Rs {pb['amount']}, Units: {pb['total_units']}, Status: {pb['status']}")

        if cp.get("unpaid_bills"):
            unpaid_str = ", ".join([f"{u['month']}: Rs {u['amount']} (Base Fare: Rs {u.get('base_fare', 40.0)})" for u in cp["unpaid_bills"]])
            lines.append(f"- Unpaid Bills Requiring Payment: {unpaid_str}")

        if cp.get("appliance_telemetry"):
            at = cp["appliance_telemetry"]
            lines.append(f"- Premise Appliances Real-Time Telemetry: {at['on_count']} of {at['total_count']} appliances are currently powered ON (Active Total Load: {at['active_kw']} kW / {at['active_watts']} Watts, Hourly Burn Rate: Rs {at['hourly_cost_rs']:.2f}/hour).")
            on_apps = [f"{a['name']} ({a['wattage']}W)" for a in at["appliances"] if a["is_on"]]
            off_apps = [f"{a['name']} ({a['wattage']}W)" for a in at["appliances"] if not a["is_on"]]
            if on_apps:
                lines.append(f"  * Appliances Powered ON ({len(on_apps)}): {', '.join(on_apps)}")
            if off_apps:
                lines.append(f"  * Appliances Powered OFF ({len(off_apps)}): {', '.join(off_apps)}")

        if cp.get("existing_complaints"):
            c_str = "; ".join([f"{c['complaint_number']} ({c['complaint_id']}) - {c['issue_type']}: {c['status']}" for c in cp["existing_complaints"]])
            lines.append(f"- Caller Existing Complaints: {c_str}")

    return "\n".join(lines)
