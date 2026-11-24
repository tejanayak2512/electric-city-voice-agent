from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations


DEFAULT_STATES = {
    "fan1": True,
    "fan2": True,
    "lights": True,
    "tv": True,
    "ac": True,
    "fridge": True,
}

CATALOG = {
    "Domestic": [
        ("fan1", 75),
        ("fan2", 75),
        ("lights", 60),
        ("tv", 140),
        ("ac", 1500),
        ("fridge", 220),
    ],
    "Commercial": [
        ("fan1", 250),
        ("fan2", 250),
        ("lights", 300),
        ("tv", 450),
        ("ac", 2500),
        ("fridge", 1750),
    ],
}


def q4(val):
    return Decimal(val).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def q2(val):
    return Decimal(val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def backfill_forward(apps, schema_editor):
    ConnectionApplication = apps.get_model("users", "ConnectionApplication")
    UserAppliance = apps.get_model("users", "UserAppliance")

    for conn in ConnectionApplication.objects.all():
        unbilled = max(Decimal("0.00"), q2(conn.unbilled_units or Decimal("0")))
        total = max(unbilled, q2(conn.total_units_consumed or Decimal("0")))
        if unbilled <= Decimal("0.00") and total <= Decimal("0.00"):
            continue
        existing_stats = conn.appliance_usage_stats or {}
        if existing_stats:
            continue

        customs = list(UserAppliance.objects.filter(user_id=conn.user_id).order_by("created_at", "id"))
        if any((c.consumed_units or Decimal("0")) > Decimal("0") for c in customs):
            continue

        cat = "Commercial" if conn.property_type == "Commercial" else "Domestic"
        states = {k: bool((conn.appliance_states or {}).get(k, True)) for k in DEFAULT_STATES}
        candidates = [
            ("builtin", key, w)
            for key, w in CATALOG[cat]
            if states.get(key, True)
        ] + [
            ("custom", c, int(c.wattage))
            for c in customs
            if c.is_on and c.wattage > 0
        ]
        if not candidates:
            candidates = [("builtin", key, w) for key, w in CATALOG[cat]] + [
                ("custom", c, int(c.wattage)) for c in customs if c.wattage > 0
            ]

        total_w = sum(w for _, _, w in candidates)
        if total_w <= 0:
            continue

        stats = {}
        is_comm = conn.property_type == "Commercial"
        for kind, ref, w in candidates:
            ratio = Decimal(w) / Decimal(total_w)
            tot_share = q4(total * ratio)
            unb_share = q4(unbilled * ratio) if not is_comm else Decimal("0.0000")
            if kind == "builtin":
                stats[ref] = {"total_units": str(tot_share), "unbilled_units": str(unb_share)}
            else:
                ref.consumed_units = tot_share
                ref.unbilled_consumed_units = unb_share
                ref.save(update_fields=["consumed_units", "unbilled_consumed_units"])
        conn.appliance_usage_stats = stats
        conn.save(update_fields=["appliance_usage_stats"])


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0012_connectionapplication_appliance_states_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_forward, migrations.RunPython.noop),
    ]
