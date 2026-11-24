import uuid
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.db import DatabaseError, OperationalError, transaction
from django.utils import timezone

from admins.models import TariffSetting

from .models import ConnectionApplication, PowerBill, UserAppliance

BASE_FARE_RS = TariffSetting.DEFAULT_BASE_FARE
DOMESTIC_RATE_PER_UNIT = TariffSetting.DEFAULT_DOMESTIC_RATE
COMMERCIAL_RATE_PER_UNIT = TariffSetting.DEFAULT_COMMERCIAL_RATE
MIN_COMMERCIAL_BULK_UNITS = Decimal("10.00")
MIN_COMMERCIAL_PAYMENT_RS = BASE_FARE_RS + (MIN_COMMERCIAL_BULK_UNITS * COMMERCIAL_RATE_PER_UNIT)

ALLOWED_GATEWAY_METHODS = {
    "upi": "UPI",
    "upi / bhim": "UPI",
    "card": "Credit / Debit Card",
    "credit / debit card": "Credit / Debit Card",
    "net banking": "Net Banking",
    "netbanking": "Net Banking",
    "industrial wallet": "Industrial Wallet",
    "bbps / utility wallet": "BBPS / Utility Wallet",
    "bbps": "BBPS / Utility Wallet",
    "wallet": "BBPS / Utility Wallet",
}

_BULK_PACK_SPECS = [
    ("BULK-50", "Starter Workshop Bulk Pack", 50, "Small Industry"),
    ("BULK-150", "Business Operations Bulk Pack", 150, "Popular Bulk"),
    ("BULK-500", "Industrial Plant Bulk Pack", 500, "Heavy Manufacturing"),
    ("BULK-1000", "Mega Factory Bulk Pack", 1000, "Enterprise Grid"),
]

DEFAULT_BUILTIN_STATES = {
    "fan1": True,
    "fan2": True,
    "lights": True,
    "tv": True,
    "ac": True,
    "fridge": True,
}

BUILTIN_APPLIANCE_CATALOG = {
    ConnectionApplication.PROPERTY_DOMESTIC: [
        {"key": "fan1", "name": "Living Room Fan", "appliance_type": "Fan", "wattage": 75},
        {"key": "fan2", "name": "Bedroom Fan", "appliance_type": "Fan", "wattage": 75},
        {"key": "lights", "name": "House Lights", "appliance_type": "Light", "wattage": 60},
        {"key": "tv", "name": "Smart LED TV", "appliance_type": "TV", "wattage": 140},
        {"key": "ac", "name": "Split AC (22°C)", "appliance_type": "AC", "wattage": 1500},
        {"key": "fridge", "name": "Refrigerator (4°C)", "appliance_type": "Fridge", "wattage": 220},
    ],
    ConnectionApplication.PROPERTY_COMMERCIAL: [
        {"key": "fan1", "name": "Operations Exhaust Fan", "appliance_type": "Fan", "wattage": 250},
        {"key": "fan2", "name": "Control Bay Exhaust Fan", "appliance_type": "Fan", "wattage": 250},
        {"key": "lights", "name": "High-Bay Facility Lights", "appliance_type": "Light", "wattage": 300},
        {"key": "tv", "name": "SCADA Workstations", "appliance_type": "TV", "wattage": 450},
        {"key": "ac", "name": "Central HVAC Chiller", "appliance_type": "AC", "wattage": 2500},
        {"key": "fridge", "name": "Industry Plant Unit", "appliance_type": "Plant", "wattage": 1750},
    ],
}


def quantize_2(value):
    """Round any Decimal/numeric value cleanly to 2 decimal places."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quantize_4(value):
    """Round per-appliance kWh accumulators cleanly to 4 decimal places."""
    return Decimal(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def get_builtin_appliance_states(connection):
    """Return persisted ON/OFF booleans for built-in appliances (defaulting all to ON)."""
    raw = (getattr(connection, "appliance_states", None) or {}) if connection else {}
    return {key: bool(raw.get(key, default_val)) for key, default_val in DEFAULT_BUILTIN_STATES.items()}


def format_runtime_estimate(balance_units, active_watts, effective_power_on=True, is_exhausted=False):
    """Format Commercial remaining backup runtime cleanly matching active kW draw."""
    bal = float(balance_units or 0)
    watts = int(active_watts or 0)
    if is_exhausted or bal <= 0:
        return "(Exhausted — Recharge required)"
    if not effective_power_on:
        return "(Paused • 0.00 kW — Main Supply OFF)"
    if watts <= 0:
        return "(Standby • 0.00 kW — All Appliances OFF)"
    kw = watts / 1000.0
    hours_left = bal / kw
    total_mins = max(1, round(hours_left * 60))
    hh = total_mins // 60
    mm = total_mins % 60
    dur = f"{hh}h {mm}m" if hh > 0 else f"{mm}m"
    return f"(~{dur} @ {kw:.2f} kW)"


def _format_unit_display(val):
    """Display 2 decimals normally, or 4 decimals for small sub-0.01 non-zero live increments."""
    dec = quantize_4(val or Decimal("0"))
    if dec == Decimal("0.0000") or dec >= Decimal("0.0100"):
        return f"{quantize_2(dec):.2f}"
    return f"{dec:.4f}"


def backfill_connection_appliance_usage(conn, save=True):
    """Attribute pre-existing connection usage across active ON appliances when per-appliance stats are still empty."""
    if not conn:
        return {}
    unbilled_target = max(Decimal("0.00"), quantize_2(conn.unbilled_units or Decimal("0")))
    total_target = max(unbilled_target, quantize_2(conn.total_units_consumed or Decimal("0")))
    stats = dict(conn.appliance_usage_stats or {}) if isinstance(conn.appliance_usage_stats, dict) else {}
    custom_apps = list(UserAppliance.objects.filter(user=conn.user).order_by("created_at", "id"))

    tracked_total = sum(
        Decimal(str((v or {}).get("total_units", "0") or "0"))
        for v in stats.values()
        if isinstance(v, dict)
    ) + sum((app.consumed_units or Decimal("0")) for app in custom_apps)
    tracked_unbilled = sum(
        Decimal(str((v or {}).get("unbilled_units", "0") or "0"))
        for v in stats.values()
        if isinstance(v, dict)
    ) + sum((app.unbilled_consumed_units or Decimal("0")) for app in custom_apps)

    if (total_target <= Decimal("0.00") and unbilled_target <= Decimal("0.00")) or (
        tracked_total > Decimal("0.0000") or tracked_unbilled > Decimal("0.0000")
    ):
        return stats

    category_key = (
        ConnectionApplication.PROPERTY_COMMERCIAL
        if conn.is_commercial
        else ConnectionApplication.PROPERTY_DOMESTIC
    )
    builtin_states = get_builtin_appliance_states(conn)
    candidates = []
    for spec in BUILTIN_APPLIANCE_CATALOG[category_key]:
        if builtin_states.get(spec["key"], True) and spec["wattage"] > 0:
            candidates.append(("builtin", spec["key"], spec["wattage"]))
    for app in custom_apps:
        if app.is_on and app.wattage > 0:
            candidates.append(("custom", app, app.wattage))
    if not candidates:
        for spec in BUILTIN_APPLIANCE_CATALOG[category_key]:
            if spec["wattage"] > 0:
                candidates.append(("builtin", spec["key"], spec["wattage"]))
        for app in custom_apps:
            if app.wattage > 0:
                candidates.append(("custom", app, app.wattage))

    total_w = sum(w for _, _, w in candidates)
    if total_w <= 0:
        return stats

    for kind, ref, w in candidates:
        ratio = Decimal(w) / Decimal(total_w)
        tot_share = quantize_4(total_target * ratio)
        unb_share = quantize_4(unbilled_target * ratio) if not conn.is_commercial else Decimal("0.0000")
        if kind == "builtin":
            stats[ref] = {
                "total_units": str(tot_share),
                "unbilled_units": str(unb_share),
            }
        elif save:
            ref.consumed_units = tot_share
            ref.unbilled_consumed_units = unb_share
            ref.save(update_fields=["consumed_units", "unbilled_consumed_units"])

    if save:
        conn.appliance_usage_stats = stats
        conn.save(update_fields=["appliance_usage_stats", "updated_at"])
    return stats


def get_connection_appliance_telemetry(connection, rate_per_unit=None):
    """Compute live appliance ON/OFF load, consumption rates, runtime left, and billing breakdown."""
    if not connection:
        return {
            "builtin_states": dict(DEFAULT_BUILTIN_STATES),
            "appliances": [],
            "on_count": 0,
            "running_count": 0,
            "total_count": 0,
            "configured_on_watts": 0,
            "configured_on_kw": 0.0,
            "active_watts": 0,
            "active_kw": 0.0,
            "units_per_hour": 0.0,
            "hourly_cost_rs": 0.0,
            "effective_power_on": False,
            "est_runtime_text": "(Not Connected)",
        }

    if rate_per_unit is None:
        rate_per_unit = get_tariff_rate(connection)
    rate_dec = quantize_2(rate_per_unit)

    category_key = (
        ConnectionApplication.PROPERTY_COMMERCIAL
        if connection.is_commercial
        else ConnectionApplication.PROPERTY_DOMESTIC
    )
    builtin_states = get_builtin_appliance_states(connection)
    builtin_stats = connection.appliance_usage_stats or {}
    custom_apps = list(UserAppliance.objects.filter(user=connection.user).order_by("created_at", "id"))

    # If a pre-existing connection already accrued units before per-appliance tracking existed,
    # compute proportional virtual shares so billing never displays misleading 0.00 U per appliance.
    tracked_sum = sum(
        Decimal(str((v or {}).get("total_units", "0") or "0"))
        for v in (builtin_stats.values() if isinstance(builtin_stats, dict) else [])
        if isinstance(v, dict)
    ) + sum((a.consumed_units or Decimal("0")) for a in custom_apps)
    legacy_builtin_shares = {}
    legacy_custom_shares = {}
    if tracked_sum == Decimal("0.0000") and (
        (connection.unbilled_units or Decimal("0")) > Decimal("0")
        or (connection.total_units_consumed or Decimal("0")) > Decimal("0")
    ):
        on_pool = [
            ("builtin", spec["key"], int(spec["wattage"]))
            for spec in BUILTIN_APPLIANCE_CATALOG[category_key]
            if builtin_states.get(spec["key"], True) and spec["wattage"] > 0
        ] + [
            ("custom", app.id, int(app.wattage))
            for app in custom_apps
            if app.is_on and app.wattage > 0
        ]
        if not on_pool:
            on_pool = [
                ("builtin", spec["key"], int(spec["wattage"]))
                for spec in BUILTIN_APPLIANCE_CATALOG[category_key]
                if spec["wattage"] > 0
            ] + [
                ("custom", app.id, int(app.wattage))
                for app in custom_apps
                if app.wattage > 0
            ]
        pool_w = sum(w for _, _, w in on_pool)
        if pool_w > 0:
            unb_dec = max(Decimal("0.00"), quantize_2(connection.unbilled_units or Decimal("0")))
            tot_dec = max(unb_dec, quantize_2(connection.total_units_consumed or Decimal("0")))
            for kind, ref, w in on_pool:
                ratio = Decimal(w) / Decimal(pool_w)
                entry = (
                    quantize_4(tot_dec * ratio),
                    quantize_4(unb_dec * ratio) if not connection.is_commercial else Decimal("0.0000"),
                )
                if kind == "builtin":
                    legacy_builtin_shares[ref] = entry
                else:
                    legacy_custom_shares[ref] = entry

    transformer_on = bool(connection.is_connected and connection.transformer and connection.transformer.is_power_on)
    exhausted = bool(connection.is_commercial and connection.prepaid_units_balance <= Decimal("0.00"))
    effective_power_on = bool(
        connection.is_connected
        and transformer_on
        and (not connection.is_power_cut)
        and (not exhausted)
        and connection.user.house_power_on
    )

    items = []
    for spec in BUILTIN_APPLIANCE_CATALOG[category_key]:
        key = spec["key"]
        wattage = int(spec["wattage"])
        switch_on = builtin_states.get(key, True)
        is_running = effective_power_on and switch_on
        stat = builtin_stats.get(key, {}) if isinstance(builtin_stats, dict) else {}
        if key in legacy_builtin_shares:
            total_u, unbilled_u = legacy_builtin_shares[key]
        else:
            total_u = quantize_4(Decimal(str(stat.get("total_units", "0") or "0")))
            unbilled_u = quantize_4(Decimal(str(stat.get("unbilled_units", "0") or "0")))
        active_w = wattage if is_running else 0
        kw_draw = active_w / 1000.0
        cost_hr = float(quantize_2(Decimal(str(kw_draw)) * rate_dec))
        items.append(
            {
                "id": key,
                "key": key,
                "is_custom": False,
                "name": spec["name"],
                "appliance_type": spec["appliance_type"],
                "wattage": wattage,
                "switch_on": switch_on,
                "is_running": is_running,
                "active_watts": active_w,
                "active_kw": round(kw_draw, 3),
                "units_per_hour": round(kw_draw, 3),
                "cost_per_hour_rs": cost_hr,
                "consumed_units": float(total_u),
                "unbilled_consumed_units": float(unbilled_u),
                "consumed_units_display": _format_unit_display(total_u),
                "unbilled_units_display": _format_unit_display(unbilled_u),
                "unbilled_cost_rs": float(quantize_2(unbilled_u * rate_dec)),
                "total_cost_rs": float(quantize_2(total_u * rate_dec)),
            }
        )

    for app in custom_apps:
        switch_on = bool(app.is_on)
        is_running = effective_power_on and switch_on
        active_w = int(app.wattage) if is_running else 0
        kw_draw = active_w / 1000.0
        cost_hr = float(quantize_2(Decimal(str(kw_draw)) * rate_dec))
        if app.id in legacy_custom_shares:
            total_u, unbilled_u = legacy_custom_shares[app.id]
        else:
            total_u = quantize_4(app.consumed_units or Decimal("0.0000"))
            unbilled_u = quantize_4(app.unbilled_consumed_units or Decimal("0.0000"))
        items.append(
            {
                "id": app.id,
                "key": f"custom-{app.id}",
                "is_custom": True,
                "name": app.name,
                "appliance_type": app.appliance_type,
                "wattage": int(app.wattage),
                "switch_on": switch_on,
                "is_running": is_running,
                "active_watts": active_w,
                "active_kw": round(kw_draw, 3),
                "units_per_hour": round(kw_draw, 3),
                "cost_per_hour_rs": cost_hr,
                "consumed_units": float(total_u),
                "unbilled_consumed_units": float(unbilled_u),
                "consumed_units_display": _format_unit_display(total_u),
                "unbilled_units_display": _format_unit_display(unbilled_u),
                "unbilled_cost_rs": float(quantize_2(unbilled_u * rate_dec)),
                "total_cost_rs": float(quantize_2(total_u * rate_dec)),
            }
        )

    configured_on_watts = sum(i["wattage"] for i in items if i["switch_on"])
    active_watts = sum(i["active_watts"] for i in items)
    active_kw = round(active_watts / 1000.0, 2)
    hourly_cost_rs = float(quantize_2((Decimal(active_watts) / Decimal("1000")) * rate_dec))

    return {
        "builtin_states": builtin_states,
        "appliances": items,
        "on_count": sum(1 for i in items if i["switch_on"]),
        "running_count": sum(1 for i in items if i["is_running"]),
        "total_count": len(items),
        "configured_on_watts": configured_on_watts,
        "configured_on_kw": round(configured_on_watts / 1000.0, 2),
        "active_watts": active_watts,
        "active_kw": active_kw,
        "units_per_hour": active_kw,
        "hourly_cost_rs": hourly_cost_rs,
        "effective_power_on": effective_power_on,
        "est_runtime_text": format_runtime_estimate(
            connection.prepaid_units_balance,
            active_watts,
            effective_power_on=effective_power_on,
            is_exhausted=exhausted,
        ),
    }


def get_active_tariff():
    """Return active DB tariff rates (base_fare, domestic_rate, commercial_rate, setting)."""
    setting = TariffSetting.get_active()
    return (
        quantize_2(setting.base_fare_rs),
        quantize_2(setting.domestic_rate_per_unit),
        quantize_2(setting.commercial_rate_per_unit),
        setting,
    )


def update_active_tariff(
    base_fare_rs,
    domestic_rate_per_unit,
    commercial_rate_per_unit,
    revision_note="",
    updated_by="admin",
    domestic_connection_fee=None,
    commercial_connection_fee=None,
):
    """Validate and update live Base Fare, Unit Charges, and Connection Charges from Admin."""
    def _parse_rate(val, label):
        try:
            dec = quantize_2(Decimal(str(val)))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"Enter a valid numeric amount for {label}.") from exc
        if dec <= Decimal("0.00") or dec > Decimal("100000.00"):
            raise ValueError(f"{label} must be between Rs 0.01 and Rs 100000.00.")
        return dec

    base_dec = _parse_rate(base_fare_rs, "Base Fare")
    dom_dec = _parse_rate(domestic_rate_per_unit, "Domestic Unit Rate")
    comm_dec = _parse_rate(commercial_rate_per_unit, "Commercial Unit Rate")

    setting = TariffSetting.get_active()
    setting.base_fare_rs = base_dec
    setting.domestic_rate_per_unit = dom_dec
    setting.commercial_rate_per_unit = comm_dec
    if domestic_connection_fee not in (None, ""):
        setting.domestic_connection_fee = _parse_rate(domestic_connection_fee, "Domestic Connection Fee")
    if commercial_connection_fee not in (None, ""):
        setting.commercial_connection_fee = _parse_rate(commercial_connection_fee, "Commercial Connection Fee")
    note = (revision_note or "").strip()
    setting.revision_note = note or f"Updated by {updated_by}: Base Rs {base_dec}, Domestic Rs {dom_dec}/U, Commercial Rs {comm_dec}/U"
    setting.updated_by = updated_by or "admin"
    setting.save()
    return setting


def get_connection_fee_breakdown(property_type, total_override=None):
    """Compute itemized New Connection Charges for Domestic or Commercial service."""
    setting = TariffSetting.get_active()
    is_comm = property_type == ConnectionApplication.PROPERTY_COMMERCIAL
    if total_override is not None and Decimal(str(total_override)) > Decimal("0.00"):
        total = quantize_2(total_override)
    else:
        total = quantize_2(
            setting.commercial_connection_fee if is_comm else setting.domestic_connection_fee
        )

    processing_fee = quantize_2(total * (Decimal("0.20")))
    service_line_charge = quantize_2(total * (Decimal("0.35") if is_comm else Decimal("0.40")))
    security_deposit = quantize_2(total - processing_fee - service_line_charge)
    return {
        "property_type": property_type,
        "processing_fee": processing_fee,
        "service_line_charge": service_line_charge,
        "security_deposit": security_deposit,
        "total": total,
    }


def get_commercial_bulk_packs(base_fare=None, commercial_rate=None):
    """Build Commercial Bulk Unit Recharge Packs dynamically from active TariffSetting."""
    if base_fare is None or commercial_rate is None:
        active_base, _, active_comm, _ = get_active_tariff()
        base_fare = active_base if base_fare is None else quantize_2(base_fare)
        commercial_rate = active_comm if commercial_rate is None else quantize_2(commercial_rate)

    packs = []
    for code, name, units, tag in _BULK_PACK_SPECS:
        energy_charge = quantize_2(Decimal(units) * commercial_rate)
        packs.append(
            {
                "code": code,
                "name": name,
                "units": units,
                "base_fare": base_fare,
                "energy_charge": energy_charge,
                "total_amount": quantize_2(base_fare + energy_charge),
                "tag": tag,
            }
        )
    return packs


COMMERCIAL_BULK_PACKS = [
    {
        "code": code,
        "name": name,
        "units": units,
        "base_fare": BASE_FARE_RS,
        "energy_charge": quantize_2(Decimal(units) * COMMERCIAL_RATE_PER_UNIT),
        "total_amount": quantize_2(BASE_FARE_RS + Decimal(units) * COMMERCIAL_RATE_PER_UNIT),
        "tag": tag,
    }
    for code, name, units, tag in _BULK_PACK_SPECS
]


def get_tariff_rate(connection):
    """Return active per-unit rate (Rs/Unit) based on Domestic vs Commercial category."""
    _, dom_rate, comm_rate, _ = get_active_tariff()
    return comm_rate if connection.is_commercial else dom_rate


def calculate_energy_bill(units, rate_per_unit, base_fare=None):
    """Compute itemized bill breakdown using active Base Fare + (Units x Rate/Unit)."""
    if base_fare is None:
        base_fare, _, _, _ = get_active_tariff()
    units_dec = max(Decimal("0.00"), quantize_2(units))
    rate_dec = quantize_2(rate_per_unit)
    base_dec = quantize_2(base_fare)
    energy_charge = quantize_2(units_dec * rate_dec)
    total_amount = quantize_2(base_dec + energy_charge)
    return {
        "base_fare": base_dec,
        "units": units_dec,
        "rate_per_unit": rate_dec,
        "energy_charge": energy_charge,
        "total_amount": total_amount,
    }


def calculate_commercial_units_from_payment(amount_paid):
    """Given paid amount >= live minimum, deduct active base fare and credit bulk units at active commercial rate."""
    base_fare, _, comm_rate, _ = get_active_tariff()
    min_payment = quantize_2(base_fare + (MIN_COMMERCIAL_BULK_UNITS * comm_rate))
    try:
        paid = quantize_2(Decimal(str(amount_paid)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Enter a valid bulk recharge amount in Rupees.") from exc

    if paid < min_payment:
        raise ValueError(
            f"Minimum Commercial bulk recharge is Rs {min_payment} "
            f"(Rs {base_fare} Base Fare + {MIN_COMMERCIAL_BULK_UNITS} Bulk Units at Rs {comm_rate}/unit)."
        )

    energy_credit = quantize_2(paid - base_fare)
    allocated_units = quantize_2(energy_credit / comm_rate)
    return {
        "base_fare": base_fare,
        "rate_per_unit": comm_rate,
        "energy_charge": energy_credit,
        "allocated_units": allocated_units,
        "total_amount": paid,
        "pack_name": f"Custom Bulk Recharge ({allocated_units} Units)",
    }


def calculate_commercial_bulk_recharge(pack_code="", bulk_units=None, amount_paid=None):
    """Resolve Commercial bulk pack code, explicit bulk unit count, or rupee payment using live rates."""
    base_fare, _, comm_rate, _ = get_active_tariff()
    packs_by_code = {p["code"]: p for p in get_commercial_bulk_packs(base_fare, comm_rate)}
    code = (pack_code or "").strip().upper()
    if code and code in packs_by_code:
        pack = packs_by_code[code]
        return {
            "base_fare": pack["base_fare"],
            "rate_per_unit": comm_rate,
            "energy_charge": pack["energy_charge"],
            "allocated_units": quantize_2(pack["units"]),
            "total_amount": pack["total_amount"],
            "pack_name": pack["name"],
        }

    if bulk_units not in (None, ""):
        try:
            units_dec = quantize_2(Decimal(str(bulk_units)))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("Enter a valid bulk unit quantity.") from exc
        if units_dec < MIN_COMMERCIAL_BULK_UNITS:
            min_total = quantize_2(base_fare + (MIN_COMMERCIAL_BULK_UNITS * comm_rate))
            raise ValueError(
                f"Minimum Commercial bulk recharge is {MIN_COMMERCIAL_BULK_UNITS} Units "
                f"(Rs {min_total} total incl. Rs {base_fare} Base Fare)."
            )
        calc = calculate_energy_bill(units_dec, comm_rate, base_fare=base_fare)
        return {
            "base_fare": calc["base_fare"],
            "rate_per_unit": comm_rate,
            "energy_charge": calc["energy_charge"],
            "allocated_units": calc["units"],
            "total_amount": calc["total_amount"],
            "pack_name": f"Custom Bulk Pack ({calc['units']} Units)",
        }

    return calculate_commercial_units_from_payment(amount_paid)


def process_dummy_gateway_transaction(payment_method="UPI", simulate_outcome="success"):
    """Validate DISCOM payment gateway channel and generate a unique dummy TXN reference."""
    norm_method = ALLOWED_GATEWAY_METHODS.get(
        (payment_method or "UPI").strip().lower(), "UPI"
    )
    outcome = (simulate_outcome or "success").strip().lower()
    if outcome == "failed":
        raise ValueError("Payment Gateway Declined: Transaction failed during DISCOM bank clearing.")
    stamp = timezone.localdate().strftime("%Y%m%d")
    return norm_method, f"TXN-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def _next_bill_number(prefix):
    last_id = PowerBill.objects.order_by("-id").values_list("id", flat=True).first() or 0
    seq = last_id + 1001
    stamp = timezone.localdate().strftime("%Y%m")
    while PowerBill.objects.filter(bill_number=f"{prefix}-{stamp}-{seq}").exists():
        seq += 1
    return f"{prefix}-{stamp}-{seq}"


@transaction.atomic
def process_commercial_prepaid_payment(
    connection_id,
    amount_paid=None,
    *,
    bulk_units=None,
    pack_code="",
    payment_method="UPI",
    simulate_outcome="success",
):
    """Verify gateway settlement and credit Commercial bulk power units using live tariff."""
    conn = ConnectionApplication.objects.select_for_update().get(pk=connection_id)
    if not conn.is_connected or not conn.meter_number:
        raise ValueError("Power connection and official Meter Number must be active first.")
    if not conn.is_commercial:
        raise ValueError("Bulk recharge unit allocation applies only to Commercial connections.")

    gateway_method, txn_id = process_dummy_gateway_transaction(payment_method, simulate_outcome)
    calc = calculate_commercial_bulk_recharge(
        pack_code=pack_code,
        bulk_units=bulk_units,
        amount_paid=amount_paid,
    )
    bill = PowerBill.objects.create(
        connection=conn,
        bill_number=_next_bill_number("BLK"),
        bill_type=PowerBill.TYPE_PREPAID,
        billing_month=timezone.localdate().strftime("%Y-%m"),
        units_billed=calc["allocated_units"],
        base_fare=calc["base_fare"],
        rate_per_unit=calc["rate_per_unit"],
        energy_charge=calc["energy_charge"],
        total_amount=calc["total_amount"],
        status=PowerBill.STATUS_PAID,
        bulk_pack_name=calc["pack_name"],
        payment_method=gateway_method,
        transaction_id=txn_id,
        gateway_status="Success",
        paid_at=timezone.now(),
    )
    conn.prepaid_units_balance = quantize_2(conn.prepaid_units_balance + calc["allocated_units"])
    conn.prepaid_units_allocated_total = quantize_2(
        conn.prepaid_units_allocated_total + calc["allocated_units"]
    )
    conn.last_usage_synced_at = timezone.now()
    conn.save(
        update_fields=[
            "prepaid_units_balance",
            "prepaid_units_allocated_total",
            "last_usage_synced_at",
            "updated_at",
        ]
    )
    return bill, conn


def _attribute_usage_to_running_appliances(conn, actual_used, telemetry):
    """Distribute consumed kWh units proportionally across active running appliances."""
    if actual_used <= Decimal("0.00"):
        return
    running_items = [it for it in telemetry["appliances"] if it["is_running"] and it["wattage"] > 0]
    total_running_w = sum(it["wattage"] for it in running_items)
    if total_running_w <= 0:
        return
    stats = dict(conn.appliance_usage_stats or {})
    custom_deltas = {}
    for it in running_items:
        share = quantize_4(actual_used * Decimal(it["wattage"]) / Decimal(total_running_w))
        if not it["is_custom"]:
            prev = stats.get(it["key"], {}) if isinstance(stats.get(it["key"]), dict) else {}
            prev_tot = Decimal(str(prev.get("total_units", "0") or "0"))
            prev_unb = Decimal(str(prev.get("unbilled_units", "0") or "0"))
            stats[it["key"]] = {
                "total_units": str(quantize_4(prev_tot + share)),
                "unbilled_units": str(quantize_4(prev_unb + share)) if not conn.is_commercial else "0.0000",
            }
        else:
            custom_deltas[it["id"]] = share
    conn.appliance_usage_stats = stats
    if custom_deltas:
        for custom_app in UserAppliance.objects.filter(id__in=list(custom_deltas.keys()), user=conn.user):
            delta = custom_deltas[custom_app.id]
            custom_app.consumed_units = quantize_4((custom_app.consumed_units or Decimal("0")) + delta)
            if not conn.is_commercial:
                custom_app.unbilled_consumed_units = quantize_4(
                    (custom_app.unbilled_consumed_units or Decimal("0")) + delta
                )
            custom_app.save(update_fields=["consumed_units", "unbilled_consumed_units"])


def sync_elapsed_appliance_usage(connection_id, now=None):
    """Advance live unit consumption from real elapsed wall-clock time while ON appliances have supply."""
    if now is None:
        now = timezone.now()
    try:
        conn = (
            ConnectionApplication.objects
            .select_related("transformer", "user")
            .filter(pk=connection_id)
            .first()
        )
    except (DatabaseError, OperationalError):
        return None

    if conn is None:
        return None

    account = conn.user
    can_flow = bool(
        conn.is_connected
        and conn.transformer
        and conn.transformer.is_power_on
        and (not conn.is_power_cut)
        and account.house_power_on
        and ((not conn.is_commercial) or conn.prepaid_units_balance > Decimal("0.00"))
    )

    if not can_flow:
        if conn.last_usage_synced_at is None:
            conn.last_usage_synced_at = now
            try:
                conn.save(update_fields=["last_usage_synced_at"])
            except (DatabaseError, OperationalError):
                pass
        return conn

    if conn.last_usage_synced_at is None:
        conn.last_usage_synced_at = now
        try:
            conn.save(update_fields=["last_usage_synced_at"])
        except (DatabaseError, OperationalError):
            pass
        return conn

    telemetry = get_connection_appliance_telemetry(conn)
    active_watts = int(telemetry["active_watts"] or 0)
    if active_watts <= 0:
        if (now - conn.last_usage_synced_at).total_seconds() >= 30.0:
            conn.last_usage_synced_at = now
            try:
                conn.save(update_fields=["last_usage_synced_at"])
            except (DatabaseError, OperationalError):
                pass
        return conn

    elapsed_secs = min(2592000.0, max(0.0, (now - conn.last_usage_synced_at).total_seconds()))
    if elapsed_secs <= 0:
        return conn

    delta_kwh = (Decimal(active_watts) / Decimal("1000")) * (
        Decimal(str(elapsed_secs)) / Decimal("3600")
    )
    total_pending = (conn.pending_energy_kwh or Decimal("0")) + delta_kwh
    units_chunks = int(total_pending * Decimal("100"))
    units_to_bill = (Decimal(units_chunks) / Decimal("100")).quantize(Decimal("0.01"))
    leftover = (total_pending - units_to_bill).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    # Avoid DB write thrashing on rapid sub-second / few-second polling if no billable units have accumulated yet
    if units_to_bill <= Decimal("0.00") and elapsed_secs < 5.0:
        return conn

    if units_to_bill > Decimal("0.00"):
        backfill_connection_appliance_usage(conn, save=True)
        telemetry = get_connection_appliance_telemetry(conn)
        if conn.is_commercial:
            actual_used = min(units_to_bill, conn.prepaid_units_balance)
            conn.prepaid_units_balance = quantize_2(conn.prepaid_units_balance - actual_used)
            conn.total_units_consumed = quantize_2(conn.total_units_consumed + actual_used)
            if conn.prepaid_units_balance <= Decimal("0.00"):
                conn.prepaid_units_balance = Decimal("0.00")
                leftover = Decimal("0.000000")
                if account.house_power_on:
                    account.house_power_on = False
                    try:
                        account.save(update_fields=["house_power_on", "updated_at"])
                    except (DatabaseError, OperationalError):
                        pass
        else:
            actual_used = units_to_bill
            conn.unbilled_units = quantize_2(conn.unbilled_units + actual_used)
            conn.total_units_consumed = quantize_2(conn.total_units_consumed + actual_used)

        _attribute_usage_to_running_appliances(conn, actual_used, telemetry)

    conn.last_usage_synced_at = now
    conn.pending_energy_kwh = max(Decimal("0.000000"), leftover)
    try:
        conn.save(
            update_fields=[
                "unbilled_units",
                "prepaid_units_balance",
                "total_units_consumed",
                "appliance_usage_stats",
                "last_usage_synced_at",
                "pending_energy_kwh",
                "updated_at",
            ]
        )
    except (DatabaseError, OperationalError):
        pass
    return conn


@transaction.atomic
def record_power_usage(
    connection_id,
    account,
    active_watts=None,
    duration_hours=Decimal("0.05"),
    use_server_appliances=False,
):
    """Record consumed kWh units from live ON appliances; attribute consumption per appliance & cut Commercial at 0."""
    conn = (
        ConnectionApplication.objects.select_for_update()
        .select_related("transformer", "user")
        .get(pk=connection_id)
    )
    base_fare, _, _, _ = get_active_tariff()
    rate = get_tariff_rate(conn)
    if (
        not conn.is_connected
        or not conn.transformer.is_power_on
        or conn.is_power_cut
        or not account.house_power_on
    ):
        telemetry = get_connection_appliance_telemetry(conn, rate_per_unit=rate)
        return {
            "ok": False,
            "blocked": True,
            "exhausted": False,
            "effective_power_on": False,
            "recorded_units": 0.0,
            "unbilled_units": float(conn.unbilled_units),
            "prepaid_units_balance": float(conn.prepaid_units_balance),
            "total_units_consumed": float(conn.total_units_consumed),
            "estimated_bill_rs": float(calculate_energy_bill(conn.unbilled_units, rate, base_fare=base_fare)["total_amount"]),
            "telemetry": telemetry,
            "est_runtime_text": telemetry["est_runtime_text"],
        }

    backfill_connection_appliance_usage(conn, save=True)
    telemetry_before = get_connection_appliance_telemetry(conn, rate_per_unit=rate)
    if use_server_appliances or active_watts is None:
        watts_dec = Decimal(str(telemetry_before["active_watts"]))
    else:
        watts_dec = max(Decimal("0"), min(Decimal("50000"), Decimal(str(active_watts))))

    hours_dec = max(Decimal("0.0001"), min(Decimal("24.0000"), Decimal(str(duration_hours))))
    consumed = quantize_2((watts_dec / Decimal("1000")) * hours_dec)
    if watts_dec > 0 and hours_dec >= Decimal("0.01") and consumed <= Decimal("0.00"):
        consumed = Decimal("0.01")

    exhausted = False
    actual_used = consumed
    if conn.is_commercial:
        if conn.prepaid_units_balance <= Decimal("0.00"):
            if account.house_power_on:
                account.house_power_on = False
                account.save(update_fields=["house_power_on", "updated_at"])
            telemetry = get_connection_appliance_telemetry(conn, rate_per_unit=rate)
            return {
                "ok": False,
                "blocked": True,
                "exhausted": True,
                "effective_power_on": False,
                "recorded_units": 0.0,
                "prepaid_units_balance": 0.0,
                "unbilled_units": float(conn.unbilled_units),
                "total_units_consumed": float(conn.total_units_consumed),
                "estimated_bill_rs": 0.0,
                "telemetry": telemetry,
                "est_runtime_text": telemetry["est_runtime_text"],
                "message": "Allocated Prepaid Units exhausted (0.00 Units remaining). Recharge via DISCOM Gateway to restore supply.",
            }

        actual_used = min(consumed, conn.prepaid_units_balance)
        conn.prepaid_units_balance = quantize_2(conn.prepaid_units_balance - actual_used)
        conn.total_units_consumed = quantize_2(conn.total_units_consumed + actual_used)
        if conn.prepaid_units_balance <= Decimal("0.00"):
            conn.prepaid_units_balance = Decimal("0.00")
            exhausted = True
            if account.house_power_on:
                account.house_power_on = False
                account.save(update_fields=["house_power_on", "updated_at"])
    else:
        conn.unbilled_units = quantize_2(conn.unbilled_units + actual_used)
        conn.total_units_consumed = quantize_2(conn.total_units_consumed + actual_used)

    # Distribute consumed units proportionally across active ON appliances
    _attribute_usage_to_running_appliances(conn, actual_used, telemetry_before)

    conn.last_usage_synced_at = timezone.now()
    conn.save(
        update_fields=[
            "unbilled_units",
            "prepaid_units_balance",
            "total_units_consumed",
            "appliance_usage_stats",
            "last_usage_synced_at",
            "updated_at",
        ]
    )

    est_bill = calculate_energy_bill(conn.unbilled_units, rate, base_fare=base_fare)["total_amount"]
    telemetry_after = get_connection_appliance_telemetry(conn, rate_per_unit=rate)
    return {
        "ok": True,
        "blocked": False,
        "exhausted": exhausted,
        "effective_power_on": bool((not exhausted) and account.house_power_on),
        "recorded_units": float(actual_used),
        "unbilled_units": float(conn.unbilled_units),
        "prepaid_units_balance": float(conn.prepaid_units_balance),
        "total_units_consumed": float(conn.total_units_consumed),
        "estimated_bill_rs": float(est_bill),
        "telemetry": telemetry_after,
        "est_runtime_text": telemetry_after["est_runtime_text"],
    }


@transaction.atomic
def generate_domestic_monthly_bills(billing_month=None, connections=None):
    """Generate itemized Monthly bills using live active Base Fare + Domestic tariff + cumulative meter readings."""
    current_month = timezone.localdate().strftime("%Y-%m")
    if not billing_month:
        billing_month = current_month
    billing_month = str(billing_month).strip()
    if billing_month < current_month:
        raise ValueError(
            f"Cannot generate Domestic monthly bills for previous month ({billing_month}). "
            f"Only {current_month} or future months can be billed."
        )

    base_fare, dom_rate, _, _ = get_active_tariff()
    qs = connections if connections is not None else ConnectionApplication.objects.filter(
        status=ConnectionApplication.STATUS_CONNECTED,
        property_type=ConnectionApplication.PROPERTY_DOMESTIC,
        meter_number__isnull=False,
    )
    qs = qs.select_for_update().select_related("user", "transformer").order_by("id")

    created_bills = []
    skipped_connections = []
    for conn in qs:
        if not conn.is_connected or conn.is_commercial or not conn.meter_number:
            skipped_connections.append(conn)
            continue
        latest_monthly_bill = (
            PowerBill.objects.filter(
                connection=conn,
                bill_type=PowerBill.TYPE_MONTHLY,
            )
            .exclude(billing_month="")
            .order_by("-billing_month", "-id")
            .first()
        )
        if latest_monthly_bill and latest_monthly_bill.billing_month >= billing_month:
            skipped_connections.append(conn)
            continue

        sync_elapsed_appliance_usage(conn.id)
        conn.refresh_from_db()
        backfill_connection_appliance_usage(conn, save=True)

        prev_reading = quantize_2(conn.last_billed_reading)
        if prev_reading == Decimal("0.00") and latest_monthly_bill is None and conn.total_units_consumed > conn.unbilled_units:
            prev_reading = quantize_2(conn.total_units_consumed - conn.unbilled_units)

        unbilled = quantize_2(conn.unbilled_units)
        total_consumed = quantize_2(conn.total_units_consumed)
        meter_delta = quantize_2(max(Decimal("0.00"), total_consumed - prev_reading))
        billable_units = max(unbilled, meter_delta)
        curr_reading = quantize_2(prev_reading + billable_units)
        if total_consumed < curr_reading:
            conn.total_units_consumed = curr_reading

        calc = calculate_energy_bill(billable_units, dom_rate, base_fare=base_fare)
        telemetry = get_connection_appliance_telemetry(conn, rate_per_unit=dom_rate)
        breakdown = []
        for item in telemetry["appliances"]:
            units_u = quantize_2(Decimal(str(item["unbilled_consumed_units"])))
            if units_u > Decimal("0.00"):
                breakdown.append(
                    {
                        "name": item["name"],
                        "appliance_type": item["appliance_type"],
                        "wattage": item["wattage"],
                        "units": float(units_u),
                        "energy_charge_rs": float(quantize_2(units_u * dom_rate)),
                    }
                )

        prior_unpaid_bills = list(
            PowerBill.objects.filter(
                connection=conn,
                bill_type=PowerBill.TYPE_MONTHLY,
                status=PowerBill.STATUS_UNPAID,
            ).order_by("billing_month", "id")
        )
        arrears_amount = quantize_2(
            sum((b.total_amount for b in prior_unpaid_bills), Decimal("0.00"))
        )
        carried_numbers = ", ".join(b.bill_number for b in prior_unpaid_bills)
        if len(carried_numbers) > 160:
            carried_numbers = carried_numbers[:157] + "..."
        total_with_arrears = quantize_2(calc["total_amount"] + arrears_amount)

        bill = PowerBill.objects.create(
            connection=conn,
            bill_number=_next_bill_number("INV"),
            bill_type=PowerBill.TYPE_MONTHLY,
            billing_month=billing_month,
            previous_reading=prev_reading,
            current_reading=curr_reading,
            units_billed=calc["units"],
            base_fare=calc["base_fare"],
            rate_per_unit=calc["rate_per_unit"],
            energy_charge=calc["energy_charge"],
            arrears_amount=arrears_amount,
            total_amount=total_with_arrears,
            carried_from_bills=carried_numbers,
            appliance_breakdown=breakdown,
            status=PowerBill.STATUS_UNPAID,
        )
        for prior_bill in prior_unpaid_bills:
            prior_bill.status = PowerBill.STATUS_CARRIED_FORWARD
            prior_bill.gateway_status = f"Carried to {bill.bill_number}"
            prior_bill.save(update_fields=["status", "gateway_status"])
        # Carry forward closing meter reading and reset cycle unbilled counters
        stats = dict(conn.appliance_usage_stats or {})
        for k, v in list(stats.items()):
            if isinstance(v, dict):
                v["unbilled_units"] = "0.0000"
                stats[k] = v
        conn.appliance_usage_stats = stats
        conn.last_billed_reading = curr_reading
        conn.unbilled_units = Decimal("0.00")
        conn.save(
            update_fields=[
                "unbilled_units",
                "last_billed_reading",
                "total_units_consumed",
                "appliance_usage_stats",
                "updated_at",
            ]
        )
        UserAppliance.objects.filter(user=conn.user).update(unbilled_consumed_units=Decimal("0.0000"))
        created_bills.append(bill)

    return created_bills, skipped_connections


@transaction.atomic
def pay_domestic_monthly_bill(bill_id, *, payment_method="UPI", simulate_outcome="success"):
    """Process DISCOM gateway payment and mark an Unpaid Domestic monthly bill as Paid."""
    bill = PowerBill.objects.select_for_update().select_related("connection").get(pk=bill_id)
    if bill.bill_type != PowerBill.TYPE_MONTHLY:
        raise ValueError("Selected record is not a Domestic monthly bill.")
    if bill.status == PowerBill.STATUS_PAID:
        raise ValueError(f"Bill {bill.bill_number} is already paid.")
    if bill.status == PowerBill.STATUS_CARRIED_FORWARD:
        raise ValueError(
            f"Bill {bill.bill_number} has already been carried forward into a newer monthly bill. "
            "Please pay the latest consolidated monthly bill instead."
        )

    gateway_method, txn_id = process_dummy_gateway_transaction(payment_method, simulate_outcome)
    bill.payment_method = gateway_method
    bill.transaction_id = txn_id
    bill.gateway_status = "Success"
    bill.status = PowerBill.STATUS_PAID
    bill.paid_at = timezone.now()
    bill.save(update_fields=["payment_method", "transaction_id", "gateway_status", "status", "paid_at"])
    return bill


def build_commercial_bill_overview(connection, units_to_bill=None, billing_month=None, bill_title=""):
    """Generate a non-persisted itemized Commercial Bill Overview before Admin clicks Publish Bill."""
    if not connection or not connection.is_commercial:
        raise ValueError("Selected meter is not a Commercial connection.")
    if not connection.is_connected or not connection.meter_number:
        raise ValueError("Commercial connection and Meter Number must be active first.")

    if not billing_month:
        billing_month = timezone.localdate().strftime("%Y-%m")

    base_fare, _, comm_rate, _ = get_active_tariff()
    if units_to_bill in (None, ""):
        units_dec = Decimal("100.00")
    else:
        try:
            units_dec = quantize_2(Decimal(str(units_to_bill)))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError("Enter a valid numeric unit quantity for Commercial Bill Overview.") from exc
    if units_dec < MIN_COMMERCIAL_BULK_UNITS:
        raise ValueError(
            f"Minimum Commercial bill allocation is {MIN_COMMERCIAL_BULK_UNITS} Units."
        )

    calc = calculate_energy_bill(units_dec, comm_rate, base_fare=base_fare)
    telemetry = get_connection_appliance_telemetry(connection, rate_per_unit=comm_rate)
    breakdown = [
        {
            "name": item["name"],
            "appliance_type": item["appliance_type"],
            "wattage": item["wattage"],
            "switch_on": item["switch_on"],
            "units": item["consumed_units"],
            "active_kw": item["active_kw"],
            "cost_per_hour_rs": item["cost_per_hour_rs"],
        }
        for item in telemetry["appliances"]
    ]
    return {
        "connection": connection,
        "meter_number": connection.meter_number,
        "billing_month": billing_month,
        "bill_title": (bill_title or "").strip() or f"Published Commercial Bill ({units_dec} Units)",
        "units_billed": calc["units"],
        "base_fare": calc["base_fare"],
        "rate_per_unit": calc["rate_per_unit"],
        "energy_charge": calc["energy_charge"],
        "total_amount": calc["total_amount"],
        "telemetry": telemetry,
        "appliance_breakdown": breakdown,
        "runtime_for_billed_units": format_runtime_estimate(
            units_dec,
            telemetry["active_watts"],
            effective_power_on=True,
            is_exhausted=False,
        ),
    }


@transaction.atomic
def publish_commercial_bill(connection_id, units_to_bill, billing_month=None, bill_title=""):
    """Publish an Admin-reviewed Commercial Bill to the consumer's Billing & DISCOM Gateway."""
    conn = ConnectionApplication.objects.select_for_update().select_related("user", "transformer").get(pk=connection_id)
    overview = build_commercial_bill_overview(
        conn,
        units_to_bill=units_to_bill,
        billing_month=billing_month,
        bill_title=bill_title,
    )
    bill = PowerBill.objects.create(
        connection=conn,
        bill_number=_next_bill_number("CMI"),
        bill_type=PowerBill.TYPE_COMMERCIAL,
        billing_month=overview["billing_month"],
        units_billed=overview["units_billed"],
        base_fare=overview["base_fare"],
        rate_per_unit=overview["rate_per_unit"],
        energy_charge=overview["energy_charge"],
        total_amount=overview["total_amount"],
        bulk_pack_name=overview["bill_title"],
        appliance_breakdown=overview["appliance_breakdown"],
        status=PowerBill.STATUS_UNPAID,
    )
    return bill


@transaction.atomic
def pay_published_commercial_bill(bill_id, *, payment_method="UPI", simulate_outcome="success"):
    """Settle an Admin-published Unpaid Commercial Bill via Gateway and credit units to meter."""
    bill = PowerBill.objects.select_for_update().select_related("connection").get(pk=bill_id)
    if bill.bill_type != PowerBill.TYPE_COMMERCIAL:
        raise ValueError("Selected record is not a published Commercial bill.")
    if bill.status == PowerBill.STATUS_PAID:
        raise ValueError(f"Bill {bill.bill_number} is already paid.")
    if bill.status == PowerBill.STATUS_CARRIED_FORWARD:
        raise ValueError(
            f"Bill {bill.bill_number} has already been carried forward into a newer monthly bill. "
            "Please pay the latest consolidated monthly bill instead."
        )

    gateway_method, txn_id = process_dummy_gateway_transaction(payment_method, simulate_outcome)
    bill.payment_method = gateway_method
    bill.transaction_id = txn_id
    bill.gateway_status = "Success"
    bill.status = PowerBill.STATUS_PAID
    bill.paid_at = timezone.now()
    bill.save(update_fields=["payment_method", "transaction_id", "gateway_status", "status", "paid_at"])

    conn = ConnectionApplication.objects.select_for_update().get(pk=bill.connection_id)
    conn.prepaid_units_balance = quantize_2(conn.prepaid_units_balance + bill.units_billed)
    conn.prepaid_units_allocated_total = quantize_2(
        conn.prepaid_units_allocated_total + bill.units_billed
    )
    conn.last_usage_synced_at = timezone.now()
    conn.save(update_fields=["prepaid_units_balance", "prepaid_units_allocated_total", "last_usage_synced_at", "updated_at"])
    return bill, conn


def build_meter_billing_summary(connection):
    """Return consistent tariff, live usage, and generated/recharge bill breakdown."""
    if not connection:
        return None
    base_fare, dom_rate, comm_rate, tariff_setting = get_active_tariff()
    rate = comm_rate if connection.is_commercial else dom_rate
    est = calculate_energy_bill(connection.unbilled_units, rate, base_fare=base_fare)
    bills = list(connection.bills.all())
    unpaid_monthly_bills = [
        b for b in bills if b.bill_type == PowerBill.TYPE_MONTHLY and b.status == PowerBill.STATUS_UNPAID
    ]
    unpaid_commercial_bills = [
        b for b in bills if b.bill_type == PowerBill.TYPE_COMMERCIAL and b.status == PowerBill.STATUS_UNPAID
    ]
    generated_monthly_bills = [
        b for b in bills if b.bill_type == PowerBill.TYPE_MONTHLY
    ]
    unpaid_total = quantize_2(sum((b.total_amount for b in unpaid_monthly_bills), Decimal("0.00")))
    unpaid_commercial_total = quantize_2(
        sum((b.total_amount for b in unpaid_commercial_bills), Decimal("0.00"))
    )
    paid_commercial_recharges = [
        b for b in bills
        if b.bill_type in (PowerBill.TYPE_PREPAID, PowerBill.TYPE_COMMERCIAL) and b.status == PowerBill.STATUS_PAID
    ]
    total_recharged_rs = quantize_2(
        sum((b.total_amount for b in paid_commercial_recharges), Decimal("0.00"))
    )
    telemetry = get_connection_appliance_telemetry(connection, rate_per_unit=rate)

    return {
        "connection": connection,
        "meter_number": connection.meter_number,
        "is_commercial": connection.is_commercial,
        "base_fare": base_fare,
        "rate_per_unit": rate,
        "tariff_setting": tariff_setting,
        "unbilled_units": connection.unbilled_units,
        "last_billed_reading": connection.last_billed_reading,
        "total_units_consumed": connection.total_units_consumed,
        "prepaid_units_balance": connection.prepaid_units_balance,
        "prepaid_units_allocated_total": connection.prepaid_units_allocated_total,
        "total_recharged_rs": total_recharged_rs,
        "paid_recharge_count": len(paid_commercial_recharges),
        "estimated_energy_charge": est["energy_charge"],
        "estimated_total_bill": est["total_amount"],
        "unpaid_monthly_total": unpaid_total,
        "unpaid_commercial_bills": unpaid_commercial_bills,
        "unpaid_commercial_total": unpaid_commercial_total,
        "total_payable_bill": unpaid_commercial_total if connection.is_commercial else unpaid_total,
        "has_generated_monthly_bills": bool(generated_monthly_bills),
        "bills": bills,
        "unpaid_monthly_bills": unpaid_monthly_bills,
        "telemetry": telemetry,
    }


@transaction.atomic
def pay_connection_application_fee(app_id, account, *, payment_method="UPI", simulate_outcome="success"):
    """Settle New Connection charges via DISCOM Gateway and unlock PDF Application & Verification."""
    app = ConnectionApplication.objects.select_for_update().get(pk=app_id, user=account)
    if app.connection_fee_paid:
        raise ValueError(f"Connection charges for {app.application_id} are already paid.")

    breakdown = get_connection_fee_breakdown(
        app.property_type,
        total_override=app.connection_fee_amount if app.connection_fee_amount > Decimal("0.00") else None,
    )
    gateway_method, txn_id = process_dummy_gateway_transaction(payment_method, simulate_outcome)
    stamp = timezone.localdate().strftime("%Y%m")
    receipt_no = f"CONNFEE-{stamp}-{1000 + app.id}"

    app.connection_fee_amount = breakdown["total"]
    app.connection_fee_paid = True
    app.connection_payment_method = gateway_method
    app.connection_transaction_id = txn_id
    app.connection_receipt_number = receipt_no
    app.connection_paid_at = timezone.now()
    app.save(
        update_fields=[
            "connection_fee_amount",
            "connection_fee_paid",
            "connection_payment_method",
            "connection_transaction_id",
            "connection_receipt_number",
            "connection_paid_at",
            "updated_at",
        ]
    )
    return app, breakdown
