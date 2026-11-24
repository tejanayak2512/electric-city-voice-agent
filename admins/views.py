import re

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from users.billing import (
    MIN_COMMERCIAL_BULK_UNITS,
    build_commercial_bill_overview,
    build_meter_billing_summary,
    calculate_energy_bill,
    generate_domestic_monthly_bills,
    get_active_tariff,
    get_connection_appliance_telemetry,
    get_tariff_rate,
    publish_commercial_bill,
    sync_elapsed_appliance_usage,
    update_active_tariff,
)
from users.models import ConnectionApplication, PowerBill, UserAppliance

from decimal import Decimal

from llm_config.models import LLMConfiguration
from llm_config.services import call_llm, get_active_llm_config, test_llm_connection
from .forms import DepartmentEmployeeForm, TransformerForm
from .models import (
    DepartmentEmployee,
    MaintenanceSchedule,
    OperationMaintenanceNotification,
    SiteMediaSetting,
    SiteNewsAnnouncement,
    Transformer,
)

# Hardcoded default admin credentials (overriding Django superuser auth)
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin"
ADMIN_AUTH_COOKIE = "admin_auth"
ADMIN_AUTH_SALT = "electricity-voice-agent-admin"


def _admin_shared_context(active_nav="", page_label=""):
    """Build shared navigation badges, active tariff rates, and header metadata for Admin views."""
    base_fare_rs, domestic_rate, commercial_rate, tariff_setting = get_active_tariff()
    breadcrumbs = [("Admin Home", reverse("admin_home"))]
    if page_label:
        breadcrumbs.append((page_label, None))
    else:
        breadcrumbs = [("Admin Home", None)]

    try:
        transformer_count = Transformer.objects.count()
    except Exception:
        transformer_count = 0

    try:
        employee_count = DepartmentEmployee.objects.count()
        active_employee_count = DepartmentEmployee.objects.filter(is_active=True).count()
    except Exception:
        employee_count = 0
        active_employee_count = 0

    try:
        total_connections_count = ConnectionApplication.objects.count()
        pending_admin_connections = ConnectionApplication.objects.filter(
            status=ConnectionApplication.STATUS_VERIFIED
        ).count()
        connected_households = ConnectionApplication.objects.filter(
            status=ConnectionApplication.STATUS_CONNECTED
        ).count()
    except Exception:
        total_connections_count = 0
        pending_admin_connections = 0
        connected_households = 0

    try:
        total_bills_count = PowerBill.objects.count()
        unpaid_bills_count = PowerBill.objects.filter(status=PowerBill.STATUS_UNPAID).count()
        domestic_bills_count = PowerBill.objects.filter(bill_type=PowerBill.TYPE_DOMESTIC).count()
        commercial_bills_count = PowerBill.objects.filter(bill_type=PowerBill.TYPE_COMMERCIAL).count()
        paid_bills_count = PowerBill.objects.filter(status=PowerBill.STATUS_PAID).count()
    except Exception:
        total_bills_count = 0
        unpaid_bills_count = 0
        domestic_bills_count = 0
        commercial_bills_count = 0
        paid_bills_count = 0

    try:
        pending_maintenance_count = MaintenanceSchedule.objects.filter(
            status=MaintenanceSchedule.STATUS_PENDING
        ).count()
        approved_maintenance_count = MaintenanceSchedule.objects.filter(
            status=MaintenanceSchedule.STATUS_APPROVED
        ).count()
        active_maintenance_count = MaintenanceSchedule.objects.filter(
            status=MaintenanceSchedule.STATUS_ACTIVE_CUT
        ).count()
    except Exception:
        pending_maintenance_count = 0
        approved_maintenance_count = 0
        active_maintenance_count = 0

    try:
        news_count = SiteNewsAnnouncement.objects.filter(is_active=True).count()
        notification_count = OperationMaintenanceNotification.objects.filter(is_active=True).count()
        llm_count = LLMConfiguration.objects.filter(is_active=True).count()
    except Exception:
        news_count = 0
        notification_count = 0
        llm_count = 0

    try:
        from voice_agent.models import CallCenterAgent
        total_voice_agents_count = CallCenterAgent.objects.count()
        available_voice_agents_count = CallCenterAgent.objects.filter(status=CallCenterAgent.STATUS_AVAILABLE).count()
    except Exception:
        total_voice_agents_count = 10
        available_voice_agents_count = 10

    return {
        "admin_username": ADMIN_USERNAME,
        "active_admin_nav": active_nav,
        "admin_breadcrumbs": breadcrumbs,
        "base_fare_rs": base_fare_rs,
        "domestic_rate": domestic_rate,
        "commercial_rate": commercial_rate,
        "tariff_setting": tariff_setting,
        "transformer_count": transformer_count,
        "employee_count": employee_count,
        "active_employee_count": active_employee_count,
        "total_connections_count": total_connections_count,
        "pending_admin_connections": pending_admin_connections,
        "connected_households": connected_households,
        "total_bills_count": total_bills_count,
        "unpaid_bills_count": unpaid_bills_count,
        "domestic_bills_count": domestic_bills_count,
        "commercial_bills_count": commercial_bills_count,
        "paid_bills_count": paid_bills_count,
        "pending_maintenance_count": pending_maintenance_count,
        "approved_maintenance_count": approved_maintenance_count,
        "active_maintenance_count": active_maintenance_count,
        "news_count": news_count,
        "notification_count": notification_count,
        "llm_count": llm_count,
        "settings_count": 4,
        "total_voice_agents_count": total_voice_agents_count,
        "available_voice_agents_count": available_voice_agents_count,
    }


def _is_admin_authenticated(request):
    """Return True when the request includes a valid signed admin auth cookie."""
    return (
        request.get_signed_cookie(
            ADMIN_AUTH_COOKIE,
            default=None,
            salt=ADMIN_AUTH_SALT,
        )
        == ADMIN_USERNAME
    )


def _summarize_transformer_meters(apps):
    """Serialize aligned connection requests/meters and compute connected/allocated counts."""
    meters = [
        {
            "id": app.id,
            "request_id": app.application_id,
            "applicant_name": app.applicant_name,
            "username": app.user.username,
            "house_address": app.house_address,
            "property_type": app.property_type,
            "industry_type": app.industry_type,
            "requested_load_kw": float(app.requested_load_kw),
            "status": app.status,
            "is_connected": app.status == ConnectionApplication.STATUS_CONNECTED,
            "is_power_cut": app.is_power_cut,
            "power_cut_remark": app.power_cut_remark,
            "power_cut_updated_at": (
                timezone.localtime(app.power_cut_updated_at).strftime("%d %b %Y, %I:%M %p")
                if app.power_cut_updated_at
                else ""
            ),
            "has_meter": bool(app.meter_number),
            "meter_number": app.meter_number or "Pending Admin Meter",
            "original_meter_serial": app.original_meter_serial or "--",
            "verified_by": app.verified_by.employee_id if app.verified_by else "--",
            "connected_by": app.connected_by.employee_id if app.connected_by else "--",
        }
        for app in apps
    ]
    connected_count = sum(1 for m in meters if m["is_connected"])
    allocated_count = sum(
        1
        for m in meters
        if m["status"]
        in (ConnectionApplication.STATUS_METER_ISSUED, ConnectionApplication.STATUS_CONNECTED)
    )
    return meters, connected_count, allocated_count


def _get_transformer_meters_payload(transformer):
    apps = (
        ConnectionApplication.objects.filter(transformer=transformer)
        .exclude(status=ConnectionApplication.STATUS_REJECTED)
        .select_related("user", "verified_by", "connected_by")
        .order_by("created_at", "id")
    )
    return _summarize_transformer_meters(apps)


def _serialize_transformer(transformer, include_meters=False):
    payload = {
        "transformer_id": transformer.id,
        "code": transformer.code,
        "transformer_name": transformer.name,
        "substation_name": transformer.substation_name,
        "zone": transformer.zone,
        "capacity_kva": transformer.capacity_kva,
        "is_power_on": transformer.is_power_on,
        "voltage": transformer.voltage,
        "frequency": transformer.frequency,
        "updated_at": timezone.localtime(transformer.updated_at).strftime(
            "%d %b %Y, %I:%M:%S %p"
        ),
    }
    if include_meters:
        meters, connected_count, allocated_count = _get_transformer_meters_payload(transformer)
        payload["meters"] = meters
        payload["connected_count"] = connected_count
        payload["allocated_count"] = allocated_count
        payload["assigned_count"] = len(meters)
    return payload


def _overall_grid_summary(transformers):
    total = len(transformers)
    online = sum(1 for t in transformers if t.is_power_on)
    return {
        "total": total,
        "online": online,
        "all_on": total > 0 and online == total,
        "all_off": total > 0 and online == 0,
    }


def build_substation_context(transformers, selected_transformer=None):
    """Build shared substation, transformer fleet, and allocated/connected meter data."""
    transformers = list(transformers)
    if selected_transformer is None and transformers:
        selected_transformer = transformers[0]

    all_apps = (
        ConnectionApplication.objects.filter(transformer__isnull=False)
        .exclude(status=ConnectionApplication.STATUS_REJECTED)
        .select_related("user", "transformer", "verified_by", "connected_by")
        .order_by("created_at", "id")
    )
    apps_by_tf = {}
    for app in all_apps:
        apps_by_tf.setdefault(app.transformer_id, []).append(app)

    fleet_items = []
    transformers_data = {}
    substations_map = {}

    for tf in transformers:
        meters, connected_count, allocated_count = _summarize_transformer_meters(
            apps_by_tf.get(tf.id, [])
        )
        item = {
            "tf": tf,
            "meters": meters,
            "connected_count": connected_count,
            "allocated_count": allocated_count,
            "assigned_count": len(meters),
        }
        fleet_items.append(item)
        transformers_data[str(tf.id)] = {
            "transformer_id": tf.id,
            "code": tf.code,
            "transformer_name": tf.name,
            "substation_name": tf.substation_name,
            "zone": tf.zone,
            "capacity_kva": tf.capacity_kva,
            "is_power_on": tf.is_power_on,
            "voltage": tf.voltage,
            "frequency": tf.frequency,
            "connected_count": connected_count,
            "allocated_count": allocated_count,
            "assigned_count": len(meters),
            "meters": meters,
        }
        sub = substations_map.setdefault(
            tf.substation_name,
            {
                "substation_name": tf.substation_name,
                "transformers": [],
                "total_capacity_kva": 0,
                "allocated_count": 0,
                "connected_count": 0,
                "online_count": 0,
            },
        )
        sub["transformers"].append(item)
        sub["total_capacity_kva"] += tf.capacity_kva
        sub["allocated_count"] += allocated_count
        sub["connected_count"] += connected_count
        if tf.is_power_on:
            sub["online_count"] += 1

    selected_item = (
        next((x for x in fleet_items if selected_transformer and x["tf"].id == selected_transformer.id), None)
        or (fleet_items[0] if fleet_items else None)
    )
    return {
        "transformers": transformers,
        "selected_transformer": selected_transformer,
        "selected_transformer_item": selected_item,
        "fleet_items": fleet_items,
        "substations_summary": list(substations_map.values()),
        "transformers_data": transformers_data,
        "overall_grid": _overall_grid_summary(transformers),
    }


def admin_login(request):
    """Validate hardcoded admin credentials and redirect to admins/adminhome.html."""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            response = redirect("admin_home")
            response.set_signed_cookie(
                ADMIN_AUTH_COOKIE,
                ADMIN_USERNAME,
                salt=ADMIN_AUTH_SALT,
                httponly=True,
                samesite="Lax",
            )
            return response

        return render(
            request,
            "base.html",
            {
                "open_admin_login_modal": True,
                "admin_login_error": "Invalid username or password. Please try again.",
                "entered_username": username,
            },
        )

    context = {"open_admin_login_modal": True}
    if request.GET.get("auth_required") == "1":
        context["admin_login_error"] = "Please sign in as administrator to continue."
    return render(request, "base.html", context)


@never_cache
def admin_home(request):
    """Render admins/adminhome.html with Multi-Transformer Substation Controls."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    transformers = list(Transformer.ensure_defaults())
    selected_id = request.GET.get("transformer")
    selected_transformer = None
    if selected_id and selected_id.isdigit():
        selected_transformer = next((t for t in transformers if t.id == int(selected_id)), None)
    if selected_transformer is None and transformers:
        selected_transformer = transformers[0]

    substation_ctx = build_substation_context(transformers, selected_transformer)
    return render(
        request,
        "admins/adminhome.html",
        {
            **_admin_shared_context(active_nav="grid"),
            **substation_ctx,
            "can_control_transformers": True,
            "transformer_form": TransformerForm(),
        },
    )


@require_POST
def add_transformer(request):
    """Create a new substation transformer unit from the Admin panel."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    form = TransformerForm(request.POST)
    if form.is_valid():
        t = form.save()
        messages.success(
            request,
            f"Transformer {t.code} under {t.substation_name} ({t.zone}) added successfully.",
        )
        return redirect(f"{reverse('admin_home')}?transformer={t.id}")

    transformers = list(Transformer.ensure_defaults())
    substation_ctx = build_substation_context(transformers)
    return render(
        request,
        "admins/adminhome.html",
        {
            **_admin_shared_context(active_nav="grid"),
            **substation_ctx,
            "can_control_transformers": True,
            "transformer_form": form,
        },
    )


@require_POST
def delete_transformer(request, tf_id):
    """Delete an unassigned substation transformer safely from Administration."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    tf = get_object_or_404(Transformer, pk=tf_id)
    if Transformer.objects.count() <= 1:
        messages.error(request, "Cannot delete the last remaining substation transformer.")
        return redirect("admin_home")

    assigned_count = (
        ConnectionApplication.objects.filter(transformer=tf)
        .exclude(status=ConnectionApplication.STATUS_REJECTED)
        .count()
    )
    if assigned_count > 0:
        messages.error(
            request,
            f"Cannot delete {tf.code} ({tf.substation_name}): {assigned_count} connection application(s)/meter(s) are assigned to it.",
        )
        return redirect(f"{reverse('admin_home')}?transformer={tf.id}")

    open_mnt = MaintenanceSchedule.objects.filter(
        transformer=tf,
        status__in=[
            MaintenanceSchedule.STATUS_PENDING,
            MaintenanceSchedule.STATUS_APPROVED,
            MaintenanceSchedule.STATUS_ACTIVE_CUT,
        ],
    ).exists()
    if open_mnt:
        messages.error(
            request,
            f"Cannot delete {tf.code} while pending/approved/active maintenance schedules exist on it.",
        )
        return redirect(f"{reverse('admin_home')}?transformer={tf.id}")

    code_label = f"{tf.code} ({tf.substation_name})"
    tf.delete()
    messages.success(request, f"Transformer {code_label} deleted from grid fleet.")
    return redirect("admin_home")


@require_POST
def toggle_power_supply(request):
    """Turn ALL transformers (overall grid) or a specific transformer ON or OFF."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    if not _is_admin_authenticated(request):
        if wants_json:
            return JsonResponse({"error": "Authentication required."}, status=403)
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    action = request.POST.get("action", "").strip().lower()
    if action not in ("on", "off"):
        if wants_json:
            return JsonResponse({"error": "Invalid power action."}, status=400)
        return redirect("admin_home")

    Transformer.ensure_defaults()
    scope = request.POST.get("scope", "").strip().lower()
    transformer_id = request.POST.get("transformer_id", "").strip()
    is_on = action == "on"
    state_label = "ON" if is_on else "OFF"

    if scope == "all" or transformer_id.lower() == "all":
        all_transformers = list(Transformer.objects.all())
        if not is_on:
            for c_id in ConnectionApplication.objects.filter(status=ConnectionApplication.STATUS_CONNECTED).values_list("id", flat=True):
                sync_elapsed_appliance_usage(c_id)
        for tf in all_transformers:
            tf.set_power(is_on)
            if is_on:
                for c_id in ConnectionApplication.objects.filter(transformer=tf, status=ConnectionApplication.STATUS_CONNECTED).values_list("id", flat=True):
                    sync_elapsed_appliance_usage(c_id)

        if wants_json:
            ctx = build_substation_context(all_transformers)
            return JsonResponse(
                {
                    "scope": "all",
                    "action": action,
                    "is_power_on": is_on,
                    "overall_grid": ctx["overall_grid"],
                    "transformers": list(ctx["transformers_data"].values()),
                    "substations": [
                        {
                            "substation_name": s["substation_name"],
                            "online_count": s["online_count"],
                            "total_count": len(s["transformers"]),
                        }
                        for s in ctx["substations_summary"]
                    ],
                }
            )

        messages.success(
            request,
            f"Overall Grid Power Supply turned {state_label} across all {len(all_transformers)} transformers.",
        )
        return redirect("admin_home")

    if transformer_id and transformer_id.isdigit():
        transformer = get_object_or_404(Transformer, pk=int(transformer_id))
    else:
        transformer = Transformer.objects.first()

    if not is_on:
        for c_id in ConnectionApplication.objects.filter(transformer=transformer, status=ConnectionApplication.STATUS_CONNECTED).values_list("id", flat=True):
            sync_elapsed_appliance_usage(c_id)
    transformer.set_power(is_on)
    if is_on:
        for c_id in ConnectionApplication.objects.filter(transformer=transformer, status=ConnectionApplication.STATUS_CONNECTED).values_list("id", flat=True):
            sync_elapsed_appliance_usage(c_id)

    if wants_json:
        all_transformers = list(Transformer.objects.all())
        payload = _serialize_transformer(transformer, include_meters=True)
        payload["scope"] = "individual"
        payload["overall_grid"] = _overall_grid_summary(all_transformers)
        return JsonResponse(payload)

    messages.success(request, f"Transformer {transformer.code} turned {state_label}.")
    return redirect(f"{reverse('admin_home')}?transformer={transformer.id}")


@never_cache
def power_status_api(request):
    """Return live transformer power status (and connected household info for logged-in users)."""
    try:
        Transformer.ensure_defaults()
    except Exception:
        pass
    transformer_id = request.GET.get("transformer_id")
    if transformer_id and transformer_id.isdigit():
        transformer = Transformer.objects.filter(pk=int(transformer_id)).first()
        if transformer:
            return JsonResponse(_serialize_transformer(transformer, include_meters=True))

    user_id = request.session.get("user_account_id")
    if user_id:
        conn = (
            ConnectionApplication.objects.filter(user_id=user_id)
            .select_related("transformer")
            .order_by("-created_at")
            .first()
        )
        if conn and conn.is_connected:
            try:
                sync_elapsed_appliance_usage(conn.id)
                conn.refresh_from_db()
            except Exception:
                pass
            base_fare_rs, dom_rate, comm_rate, tariff_setting = get_active_tariff()
            unit_rate = comm_rate if conn.is_commercial else dom_rate
            est_total = calculate_energy_bill(conn.unbilled_units, unit_rate, base_fare=base_fare_rs)["total_amount"]
            telemetry = get_connection_appliance_telemetry(conn, rate_per_unit=unit_rate)
            unpaid_monthly = list(
                PowerBill.objects.filter(
                    connection=conn,
                    bill_type=PowerBill.TYPE_MONTHLY,
                    status=PowerBill.STATUS_UNPAID,
                )
            )
            unpaid_monthly_total = sum((float(b.total_amount) for b in unpaid_monthly), 0.0)
            payload = _serialize_transformer(conn.transformer)
            payload.update(
                {
                    "is_connected": True,
                    "request_id": conn.application_id,
                    "connection_status": conn.status,
                    "property_type": conn.property_type,
                    "industry_type": conn.industry_type,
                    "meter_number": conn.meter_number,
                    "original_meter_serial": conn.original_meter_serial,
                    "house_power_on": conn.user.house_power_on,
                    "individual_power_cut": conn.is_power_cut,
                    "power_cut_remark": conn.power_cut_remark,
                    "unbilled_units": float(conn.unbilled_units),
                    "total_units_consumed": float(conn.total_units_consumed),
                    "prepaid_units_balance": float(conn.prepaid_units_balance),
                    "prepaid_units_exhausted": bool(conn.is_commercial and not conn.has_prepaid_credits),
                    "estimated_bill_rs": float(est_total),
                    "unpaid_monthly_count": len(unpaid_monthly),
                    "unpaid_monthly_total": round(unpaid_monthly_total, 2),
                    "base_fare_rs": float(base_fare_rs),
                    "rate_per_unit": float(unit_rate),
                    "tariff_revision_note": tariff_setting.revision_note,
                    "has_active_supply": conn.has_active_grid_supply,
                    "builtin_appliances": telemetry["builtin_states"],
                    "custom_appliances": [
                        {
                            "id": a.id,
                            "name": a.name,
                            "appliance_type": a.appliance_type,
                            "wattage": a.wattage,
                            "is_on": a.is_on,
                        }
                        for a in UserAppliance.objects.filter(user=conn.user)
                    ],
                    "telemetry": telemetry,
                    "est_runtime_text": telemetry["est_runtime_text"],
                }
            )
            return JsonResponse(payload)
        return JsonResponse(
            {
                "is_connected": False,
                "connection_status": conn.status if conn else "None",
                "meter_number": conn.meter_number if conn else None,
                "original_meter_serial": conn.original_meter_serial if conn else None,
                "is_power_on": False,
                "transformer_name": conn.transformer.name if (conn and conn.transformer) else "Not Assigned",
                "code": conn.transformer.code if (conn and conn.transformer) else "--",
                "voltage": "0 V (Not Connected)",
                "frequency": "0 Hz",
            }
        )

    transformer = Transformer.objects.first()
    return JsonResponse(_serialize_transformer(transformer))


@never_cache
def manage_employees(request, default_section="view"):
    """Create department employees and render the employees table with toggle and actions."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    Transformer.ensure_defaults()
    section = request.GET.get("section", request.GET.get("tab", default_section)).strip().lower()
    if request.GET.get("action") == "add":
        section = "add"
    if section not in ("view", "add"):
        section = "view"

    if request.method == "POST":
        form = DepartmentEmployeeForm(request.POST)
        if form.is_valid():
            emp = form.save()
            messages.success(
                request,
                f"Department employee {emp.full_name} ({emp.employee_id}) created successfully.",
            )
            return redirect("admin_employees")
        else:
            section = "add"
    else:
        form = DepartmentEmployeeForm()

    employees = DepartmentEmployee.objects.select_related("assigned_transformer").all()
    transformers = Transformer.objects.all()
    page_label = "Add Department Employee" if section == "add" else "Department Employees"
    return render(
        request,
        "admins/employees.html",
        {
            **_admin_shared_context(active_nav="employees", page_label=page_label),
            "form": form,
            "employees": employees,
            "transformers": transformers,
            "employee_section": section,
        },
    )


@never_cache
def add_employee_view(request):
    """Direct view for the Add Employee into Department function."""
    return manage_employees(request, default_section="add")


@require_POST
def toggle_employee_status(request, emp_id):
    """Toggle a department employee's active/inactive status controlling department login access."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    emp = get_object_or_404(DepartmentEmployee, pk=emp_id)
    emp.is_active = not emp.is_active
    emp.save(update_fields=["is_active", "updated_at"])
    state = "Active (Login Enabled)" if emp.is_active else "Inactive (Login Disabled)"
    messages.success(request, f"{emp.full_name} ({emp.employee_id}) status changed to {state}.")
    return redirect("admin_employees")


@require_POST
def delete_employee(request, emp_id):
    """Delete a department employee from Administration."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    emp = get_object_or_404(DepartmentEmployee, pk=emp_id)
    name = emp.full_name
    emp.delete()
    messages.success(request, f"Department employee {name} removed.")
    return redirect("admin_employees")


@never_cache
def manage_connections(request, default_section="all"):
    """View and cross-check department-verified connection applications in Administration."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    Transformer.ensure_defaults()
    section = request.GET.get("section", request.GET.get("tab", default_section)).strip().lower()
    if section not in ("all", "issue", "power", "ledger"):
        section = "all"

    verified_apps = (
        ConnectionApplication.objects.filter(status=ConnectionApplication.STATUS_VERIFIED)
        .select_related("user", "transformer", "verified_by")
    )
    all_apps = (
        ConnectionApplication.objects.select_related(
            "user", "transformer", "verified_by", "connected_by"
        )
        .all()
    )
    transformers = Transformer.objects.all()

    next_seq = (ConnectionApplication.objects.order_by("-id").values_list("id", flat=True).first() or 0) + 1001
    suggested_meter = f"MTR-2026-{next_seq}"
    suggested_serial = f"ORIG-PM-{next_seq}"

    connected_apps = all_apps.filter(status=ConnectionApplication.STATUS_CONNECTED)

    page_labels = {
        "issue": "Issue Original Meters",
        "power": "Premises Power Control",
        "ledger": "Connection Applications Ledger",
        "all": "Connection Approvals & Premises Control",
    }
    page_label = page_labels.get(section, "Connection Approvals & Premises Control")

    return render(
        request,
        "admins/connections.html",
        {
            **_admin_shared_context(active_nav="connections", page_label=page_label),
            "connection_section": section,
            "verified_apps": verified_apps,
            "connected_apps": connected_apps,
            "all_apps": all_apps,
            "transformers": transformers,
            "suggested_meter": suggested_meter,
            "suggested_serial": suggested_serial,
            "applied_count": all_apps.filter(status=ConnectionApplication.STATUS_APPLIED).count(),
            "verified_count": verified_apps.count(),
            "issued_count": all_apps.filter(status=ConnectionApplication.STATUS_METER_ISSUED).count(),
            "connected_count": connected_apps.count(),
            "rejected_count": all_apps.filter(status=ConnectionApplication.STATUS_REJECTED).count(),
        },
    )


@never_cache
def connections_issue_meters_view(request):
    """Direct view for the Cross-Check & Issue Original Meters function."""
    return manage_connections(request, default_section="issue")


@never_cache
def connections_power_controls_view(request):
    """Direct view for the Premises Power Control function."""
    return manage_connections(request, default_section="power")


@never_cache
def connections_ledger_view(request):
    """Direct view for the Connection Applications Ledger function."""
    return manage_connections(request, default_section="ledger")


@require_POST
def admin_verify_payment(request, app_id):
    """Verify applicant's paid New Connection Charges receipt from Administration."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    app = get_object_or_404(ConnectionApplication, pk=app_id)
    if not app.connection_fee_paid:
        messages.error(
            request,
            f"Cannot verify payment: {app.application_id} has not paid connection charges yet.",
        )
        return redirect("admin_connections")

    remarks = request.POST.get("payment_verification_remarks", "").strip()
    app.payment_verified = True
    app.payment_verified_by = f"Admin ({ADMIN_USERNAME})"
    app.payment_verified_at = timezone.now()
    app.payment_verification_remarks = remarks or (
        f"Verified by Admin: {app.connection_receipt_number} / {app.connection_transaction_id}"
    )
    app.save(
        update_fields=[
            "payment_verified",
            "payment_verified_by",
            "payment_verified_at",
            "payment_verification_remarks",
            "updated_at",
        ]
    )
    messages.success(
        request,
        f"Connection charges payment verified by Administration for {app.application_number or app.application_id}.",
    )
    return redirect("admin_connections")


@require_POST
def admin_process_connection(request, app_id):
    """Cross-check payment + application, assign unique meter number + original power meter, and approve or reject."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    app = get_object_or_404(ConnectionApplication, pk=app_id)
    action = request.POST.get("action", "").strip().lower()
    remarks = request.POST.get("admin_remarks", "").strip()

    if action == "reject":
        app.status = ConnectionApplication.STATUS_REJECTED
        app.admin_remarks = remarks or "Rejected by Administration during cross-check."
        app.save(update_fields=["status", "admin_remarks", "updated_at"])
        messages.warning(request, f"Application {app.application_id} rejected.")
        return redirect("admin_connections")

    if not app.connection_fee_paid or not app.payment_verified:
        messages.error(
            request,
            f"Cannot approve {app.application_id}: New Connection Charges must be paid AND payment-verified first.",
        )
        return redirect("admin_connections")

    if app.status != ConnectionApplication.STATUS_VERIFIED:
        messages.error(request, "Only Department-verified applications can be issued a meter.")
        return redirect("admin_connections")

    meter_number = request.POST.get("meter_number", "").strip().upper()
    original_serial = request.POST.get("original_meter_serial", "").strip().upper()
    original_checked = request.POST.get("original_meter_verified") == "on"
    transformer_id = request.POST.get("transformer_id")

    if not meter_number or len(meter_number) < 5:
        messages.error(request, "Please enter a valid Meter Number (at least 5 characters).")
        return redirect("admin_connections")
    if ConnectionApplication.objects.filter(meter_number__iexact=meter_number).exclude(pk=app.pk).exists():
        messages.error(request, f"Meter Number {meter_number} is already assigned to another household.")
        return redirect("admin_connections")
    if not original_serial or not original_checked:
        messages.error(
            request,
            "Please enter the Original Power Meter Serial and confirm original meter verification.",
        )
        return redirect("admin_connections")

    if app.transformer is None:
        messages.error(
            request,
            "Department must assign a Substation & Transformer before Administration approval.",
        )
        return redirect("admin_connections")

    if transformer_id and str(transformer_id) != str(app.transformer_id):
        messages.error(
            request,
            f"Approval must use the Department-aligned transformer ({app.transformer.code} - {app.transformer.substation_name}).",
        )
        return redirect("admin_connections")

    app.meter_number = meter_number
    app.original_meter_serial = original_serial
    app.original_meter_verified = True
    app.admin_remarks = remarks or "Cross-checked & Original Power Meter issued by Administration."
    app.approved_at = timezone.now()
    app.status = ConnectionApplication.STATUS_METER_ISSUED
    app.save()

    messages.success(
        request,
        f"Approved Request ID {app.application_id} on aligned {app.transformer.code} ({app.transformer.substation_name}): "
        f"Meter {meter_number} (Original Serial {original_serial}) issued for Department installation.",
    )
    return redirect("admin_connections")


@require_POST
def toggle_connection_power_cut(request, app_id):
    """Individually cut/restore power for a connected Home or Industry and update its remark."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    if not _is_admin_authenticated(request):
        if wants_json:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=403)
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    app = get_object_or_404(ConnectionApplication.objects.select_related("transformer", "user"), pk=app_id)
    action = request.POST.get("action", "").strip().lower()
    remark = " ".join(request.POST.get("power_cut_remark", "").split())
    redirect_to = "admin_home" if request.POST.get("next") == "admin_home" else "admin_connections"

    if action not in ("cut", "restore", "remark"):
        err = "Invalid action. Choose cut, restore, or remark."
        if wants_json:
            return JsonResponse({"ok": False, "error": err}, status=400)
        messages.error(request, err)
        return redirect(redirect_to)

    if action in ("cut", "restore") and not app.is_connected:
        err = "Individual power cut/restore is only available after the Home or Industry is Connected."
        if wants_json:
            return JsonResponse({"ok": False, "error": err}, status=400)
        messages.error(request, err)
        return redirect(redirect_to)

    if action == "remark" and not remark:
        err = "Please enter a remark before updating."
        if wants_json:
            return JsonResponse({"ok": False, "error": err}, status=400)
        messages.error(request, err)
        return redirect(redirect_to)

    if action in ("cut", "restore") and app.is_connected:
        sync_elapsed_appliance_usage(app.id)
        app.refresh_from_db()

    unit_label = f"{app.property_type} ({app.industry_type})" if app.industry_type else "Domestic Home"
    if action == "cut":
        app.is_power_cut = True
        app.power_cut_remark = remark or app.power_cut_remark or "Individual power cut applied by Administration."
        msg = f"Power CUT for {unit_label} [{app.application_id} - {app.applicant_name}]. Remark: {app.power_cut_remark}"
    elif action == "restore":
        app.is_power_cut = False
        app.power_cut_remark = remark or "Individual power supply restored by Administration."
        msg = f"Power RESTORED for {unit_label} [{app.application_id} - {app.applicant_name}]. Remark: {app.power_cut_remark}"
    else:
        app.power_cut_remark = remark
        msg = f"Updated remark for {unit_label} [{app.application_id} - {app.applicant_name}]: {app.power_cut_remark}"

    app.power_cut_updated_at = timezone.now()
    app.save(update_fields=["is_power_cut", "power_cut_remark", "power_cut_updated_at", "updated_at"])

    if wants_json:
        return JsonResponse(
            {
                "ok": True,
                "message": msg,
                "transformer": _serialize_transformer(app.transformer, include_meters=True)
                if app.transformer
                else None,
                "connection": {
                    "id": app.id,
                    "request_id": app.application_id,
                    "is_power_cut": app.is_power_cut,
                    "power_cut_remark": app.power_cut_remark,
                },
            }
        )

    messages.success(request, msg)
    return redirect(redirect_to)


def _render_admin_billing(request, default_section="domestic"):
    """Shared handler for separated Admin Billing pages (Domestic, Commercial, Tariffs) with month & status filters."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    default_month = timezone.localdate().strftime("%Y-%m")
    section = (request.GET.get("section") or request.POST.get("section") or default_section).strip().lower()
    if section not in ("domestic", "commercial", "tariffs"):
        section = default_section

    def _section_redirect_url(sec, extra_params=None):
        if sec == "commercial":
            base = reverse("admin_billing_commercial")
        elif sec == "tariffs":
            base = reverse("admin_billing_tariffs")
        else:
            base = reverse("admin_billing")
        if extra_params:
            from urllib.parse import urlencode
            return f"{base}?{urlencode(extra_params)}"
        return base

    commercial_overview = None
    commercial_overview_error = None

    if request.method == "POST":
        action = request.POST.get("action", "").strip().lower()
        if action == "update_tariff":
            try:
                updated = update_active_tariff(
                    request.POST.get("base_fare_rs", ""),
                    request.POST.get("domestic_rate_per_unit", ""),
                    request.POST.get("commercial_rate_per_unit", ""),
                    revision_note=request.POST.get("revision_note", ""),
                    updated_by=ADMIN_USERNAME,
                    domestic_connection_fee=request.POST.get("domestic_connection_fee", ""),
                    commercial_connection_fee=request.POST.get("commercial_connection_fee", ""),
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect(_section_redirect_url(section))

            messages.success(
                request,
                f"Grid Tariff Updated Live: Base Fare Rs {updated.base_fare_rs} | "
                f"Domestic Rs {updated.domestic_rate_per_unit}/Unit (Conn Fee Rs {updated.domestic_connection_fee}) | "
                f"Commercial Rs {updated.commercial_rate_per_unit}/Unit (Conn Fee Rs {updated.commercial_connection_fee}).",
            )
            return redirect(_section_redirect_url(section))

        if action == "generate_monthly":
            billing_month = request.POST.get("billing_month", default_month).strip()
            if not re.fullmatch(r"\d{4}-\d{2}", billing_month):
                messages.error(request, "Please select a valid Billing Month (YYYY-MM).")
                return redirect(_section_redirect_url("domestic"))

            base_fare_rs, dom_rate, _, _ = get_active_tariff()
            try:
                created_bills, skipped = generate_domestic_monthly_bills(billing_month=billing_month)
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect(_section_redirect_url("domestic"))
            if created_bills:
                messages.success(
                    request,
                    f"Generated {len(created_bills)} Domestic monthly bill(s) for {billing_month} "
                    f"(Base Fare Rs {base_fare_rs} + Rs {dom_rate}/unit). Now displayed to Domestic users.",
                )
            else:
                messages.info(
                    request,
                    f"No new Domestic monthly bills generated for {billing_month} "
                    f"({len(skipped)} skipped/already billed).",
                )
            return redirect(_section_redirect_url("domestic", {"ledger_month": billing_month}))

        if action == "publish_commercial_bill":
            meter_no = request.POST.get("meter_number", "").strip().upper()
            billing_month = request.POST.get("billing_month", default_month).strip()
            units_raw = request.POST.get("billed_units", "").strip()
            bill_title = request.POST.get("bill_title", "").strip()
            if not re.fullmatch(r"\d{4}-\d{2}", billing_month):
                messages.error(request, "Please select a valid Billing Month (YYYY-MM).")
                return redirect(_section_redirect_url("commercial"))
            conn = ConnectionApplication.objects.filter(
                meter_number__iexact=meter_no,
                property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
                status=ConnectionApplication.STATUS_CONNECTED,
            ).first()
            if conn is None:
                messages.error(request, f"No connected Commercial meter found for '{meter_no}'.")
                return redirect(_section_redirect_url("commercial"))
            try:
                published = publish_commercial_bill(
                    conn.id,
                    units_to_bill=units_raw,
                    billing_month=billing_month,
                    bill_title=bill_title,
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect(
                    _section_redirect_url(
                        "commercial",
                        {
                            "meter_number": meter_no,
                            "overview_units": units_raw or "100",
                            "overview_month": billing_month,
                            "generate_overview": "1",
                        },
                    )
                )
            messages.success(
                request,
                f"Published Commercial Bill {published.bill_number} for {conn.meter_number} "
                f"({published.billing_month}: {published.units_billed} Units = Rs {published.total_amount}). "
                "Consumer can now view & pay via DISCOM Gateway.",
            )
            return redirect(
                _section_redirect_url(
                    "commercial",
                    {
                        "meter_number": conn.meter_number,
                        "ledger_month": billing_month,
                    },
                )
            )

    entered_meter = request.GET.get("meter_number", "").strip().upper()
    overview_units = request.GET.get("overview_units", "100").strip()
    overview_month = request.GET.get("overview_month", default_month).strip()
    if not re.fullmatch(r"\d{4}-\d{2}", overview_month):
        overview_month = default_month
    overview_title = request.GET.get("overview_title", "").strip()
    generate_overview_flag = request.GET.get("generate_overview", "").strip() == "1"

    lookup_error = None
    lookup_summary = None
    if entered_meter:
        matched_qs = ConnectionApplication.objects.filter(
            meter_number__iexact=entered_meter
        ).select_related("transformer", "user").prefetch_related("bills")
        matched = matched_qs.first()
        if matched is None:
            lookup_error = f"No connection found with Meter Number '{entered_meter}'."
        elif section == "domestic" and matched.is_commercial:
            lookup_error = (
                f"Meter '{entered_meter}' is a Commercial meter. Switch to Commercial Billing to generate Commercial bill overviews."
            )
        elif section == "commercial" and not matched.is_commercial:
            lookup_error = (
                f"Meter '{entered_meter}' is a Domestic meter. Switch to Domestic Billing to inspect Domestic usage."
            )
        else:
            if matched.is_connected:
                sync_elapsed_appliance_usage(matched.id)
                matched.refresh_from_db()
            lookup_summary = build_meter_billing_summary(matched)
            if section == "commercial" and generate_overview_flag:
                try:
                    commercial_overview = build_commercial_bill_overview(
                        matched,
                        units_to_bill=overview_units,
                        billing_month=overview_month,
                        bill_title=overview_title,
                    )
                except ValueError as exc:
                    commercial_overview_error = str(exc)

    ledger_month = request.GET.get("ledger_month", default_month).strip()
    if not re.fullmatch(r"\d{4}-\d{2}", ledger_month):
        ledger_month = default_month
    ledger_status = request.GET.get("ledger_status", "").strip()
    if ledger_status not in ("", "all", PowerBill.STATUS_PAID, PowerBill.STATUS_UNPAID):
        ledger_status = ""

    base_ledger_qs = PowerBill.objects.select_related(
        "connection", "connection__user", "connection__transformer"
    )
    if section == "commercial":
        for cid in ConnectionApplication.objects.filter(
            status=ConnectionApplication.STATUS_CONNECTED,
            property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
        ).values_list("id", flat=True):
            sync_elapsed_appliance_usage(cid)
        category_bills_qs = base_ledger_qs.filter(
            bill_type__in=[PowerBill.TYPE_PREPAID, PowerBill.TYPE_COMMERCIAL]
        )
    else:
        for cid in ConnectionApplication.objects.filter(
            status=ConnectionApplication.STATUS_CONNECTED,
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
        ).values_list("id", flat=True):
            sync_elapsed_appliance_usage(cid)
        category_bills_qs = base_ledger_qs.filter(bill_type=PowerBill.TYPE_MONTHLY)

    all_months_mode = ledger_status == "all"
    month_bills_all = list(
        category_bills_qs if all_months_mode else category_bills_qs.filter(billing_month=ledger_month)
    )
    month_paid_bills = [b for b in month_bills_all if b.status == PowerBill.STATUS_PAID]
    month_unpaid_bills = [b for b in month_bills_all if b.status == PowerBill.STATUS_UNPAID]
    month_paid_revenue = sum((b.total_amount for b in month_paid_bills), start=0)
    month_unpaid_total = sum((b.total_amount for b in month_unpaid_bills), start=0)

    if ledger_status == PowerBill.STATUS_PAID:
        filtered_bills = month_paid_bills
    elif ledger_status == PowerBill.STATUS_UNPAID:
        filtered_bills = month_unpaid_bills
    else:
        filtered_bills = month_bills_all

    connected_domestic = ConnectionApplication.objects.filter(
        status=ConnectionApplication.STATUS_CONNECTED,
        property_type=ConnectionApplication.PROPERTY_DOMESTIC,
    ).select_related("transformer", "user")
    connected_commercial = ConnectionApplication.objects.filter(
        status=ConnectionApplication.STATUS_CONNECTED,
        property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
    ).select_related("transformer", "user")

    dom_month_all = list(
        base_ledger_qs.filter(bill_type=PowerBill.TYPE_MONTHLY, billing_month=ledger_month)
    )
    dom_month_paid = [b for b in dom_month_all if b.status == PowerBill.STATUS_PAID]
    dom_month_unpaid = [b for b in dom_month_all if b.status == PowerBill.STATUS_UNPAID]
    dom_month_unpaid_total = sum((b.total_amount for b in dom_month_unpaid), start=0)

    comm_month_all = list(
        base_ledger_qs.filter(
            bill_type__in=[PowerBill.TYPE_PREPAID, PowerBill.TYPE_COMMERCIAL],
            billing_month=ledger_month,
        )
    )
    comm_month_paid = [b for b in comm_month_all if b.status == PowerBill.STATUS_PAID]
    comm_month_unpaid = [b for b in comm_month_all if b.status == PowerBill.STATUS_UNPAID]
    comm_month_unpaid_total = sum((b.total_amount for b in comm_month_unpaid), start=0)

    page_label_map = {
        "domestic": "Billing · Domestic",
        "commercial": "Billing · Commercial",
        "tariffs": "Billing · Tariff & Fare Settings",
    }

    return render(
        request,
        "admins/billing.html",
        {
            **_admin_shared_context(active_nav="billing", page_label=page_label_map[section]),
            "billing_section": section,
            "default_month": default_month,
            "ledger_month": ledger_month,
            "ledger_status": ledger_status,
            "all_months_mode": all_months_mode,
            "entered_meter": entered_meter,
            "overview_units": overview_units,
            "overview_month": overview_month,
            "overview_title": overview_title,
            "generate_overview_flag": generate_overview_flag,
            "commercial_overview": commercial_overview,
            "commercial_overview_error": commercial_overview_error,
            "min_commercial_bulk_units": MIN_COMMERCIAL_BULK_UNITS,
            "lookup_error": lookup_error,
            "lookup_summary": lookup_summary,
            "connected_domestic": connected_domestic,
            "connected_commercial": connected_commercial,
            "all_bills": filtered_bills,
            "total_bills_count": len(month_bills_all),
            "paid_bills_count": len(month_paid_bills),
            "unpaid_month_count": len(month_unpaid_bills),
            "unpaid_month_total_rs": month_unpaid_total,
            "dom_total_count": len(dom_month_all),
            "dom_paid_count": len(dom_month_paid),
            "dom_unpaid_count": len(dom_month_unpaid),
            "dom_unpaid_total_rs": dom_month_unpaid_total,
            "comm_total_count": len(comm_month_all),
            "comm_paid_count": len(comm_month_paid),
            "comm_unpaid_count": len(comm_month_unpaid),
            "comm_unpaid_total_rs": comm_month_unpaid_total,
        },
    )


@never_cache
def admin_billing(request):
    """Admin Domestic Billing & Month-Wise Ledger (default individual Domestic billing view)."""
    return _render_admin_billing(request, default_section="domestic")


@never_cache
def admin_billing_commercial(request):
    """Admin Commercial Billing, Bill Overview -> Publish Workflow & Commercial Ledger."""
    return _render_admin_billing(request, default_section="commercial")


@never_cache
def admin_billing_tariffs(request):
    """Admin DISCOM Tariff, Base Fare & Connection Charge Revision Page."""
    return _render_admin_billing(request, default_section="tariffs")


@never_cache
def admin_revenue(request):
    """Dedicated DISCOM Revenue page with month-wise calendar filter, category breakdown, and grand total revenue."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    default_month = timezone.localdate().strftime("%Y-%m")
    revenue_month = request.GET.get("revenue_month", default_month).strip()
    if not re.fullmatch(r"\d{4}-\d{2}", revenue_month):
        revenue_month = default_month

    category_filter = request.GET.get("category", "").strip()
    if category_filter not in ("", "Domestic", "Commercial", "Connection Fee"):
        category_filter = ""

    from decimal import Decimal

    paid_bills = list(
        PowerBill.objects.filter(status=PowerBill.STATUS_PAID)
        .select_related("connection", "connection__user", "connection__transformer")
        .order_by("-paid_at", "-id")
    )
    paid_conn_fees = list(
        ConnectionApplication.objects.filter(
            connection_fee_paid=True,
            connection_fee_amount__gt=Decimal("0.00"),
        )
        .select_related("user", "transformer")
        .order_by("-connection_paid_at", "-id")
    )

    total_domestic_rev = Decimal("0.00")
    total_commercial_rev = Decimal("0.00")
    total_conn_fee_rev = Decimal("0.00")

    month_domestic_rev = Decimal("0.00")
    month_commercial_rev = Decimal("0.00")
    month_conn_fee_rev = Decimal("0.00")
    month_dom_count = 0
    month_comm_count = 0
    month_conn_count = 0

    monthly_buckets = {}
    month_transactions = []

    def _bucket(ym):
        if ym not in monthly_buckets:
            monthly_buckets[ym] = {
                "month": ym,
                "domestic": Decimal("0.00"),
                "commercial": Decimal("0.00"),
                "connection_fee": Decimal("0.00"),
                "total": Decimal("0.00"),
                "count": 0,
            }
        return monthly_buckets[ym]

    for bill in paid_bills:
        amt = bill.total_amount or Decimal("0.00")
        ym = bill.billing_month or (
            timezone.localtime(bill.paid_at or bill.created_at).strftime("%Y-%m")
        )
        is_comm = bill.bill_type in (PowerBill.TYPE_PREPAID, PowerBill.TYPE_COMMERCIAL)
        bkt = _bucket(ym)
        bkt["count"] += 1
        bkt["total"] += amt
        if is_comm:
            total_commercial_rev += amt
            bkt["commercial"] += amt
        else:
            total_domestic_rev += amt
            bkt["domestic"] += amt

        if ym == revenue_month:
            if is_comm:
                month_commercial_rev += amt
                month_comm_count += 1
            else:
                month_domestic_rev += amt
                month_dom_count += 1
            cat_label = "Commercial" if is_comm else "Domestic"
            if not category_filter or category_filter == cat_label:
                month_transactions.append(
                    {
                        "ref_no": bill.bill_number,
                        "category": cat_label,
                        "source": bill.get_bill_type_display(),
                        "consumer": bill.connection.applicant_name,
                        "meter_or_req": bill.connection.meter_number or bill.connection.application_id,
                        "method": bill.payment_method or "DISCOM Gateway",
                        "transaction_id": bill.transaction_id or "--",
                        "amount": amt,
                        "paid_at": bill.paid_at or bill.created_at,
                    }
                )

    for app in paid_conn_fees:
        amt = app.connection_fee_amount or Decimal("0.00")
        dt = app.connection_paid_at or app.created_at
        ym = timezone.localtime(dt).strftime("%Y-%m")
        bkt = _bucket(ym)
        bkt["count"] += 1
        bkt["connection_fee"] += amt
        bkt["total"] += amt
        total_conn_fee_rev += amt

        if ym == revenue_month:
            month_conn_fee_rev += amt
            month_conn_count += 1
            if not category_filter or category_filter == "Connection Fee":
                month_transactions.append(
                    {
                        "ref_no": app.connection_receipt_number or app.application_number or app.application_id,
                        "category": "Connection Fee",
                        "source": f"New Connection Charge ({app.property_type})",
                        "consumer": app.applicant_name,
                        "meter_or_req": app.application_id,
                        "method": app.connection_payment_method or "DISCOM Gateway",
                        "transaction_id": app.connection_transaction_id or "--",
                        "amount": amt,
                        "paid_at": dt,
                    }
                )

    month_transactions.sort(key=lambda x: x["paid_at"], reverse=True)
    monthly_breakdown = sorted(monthly_buckets.values(), key=lambda x: x["month"], reverse=True)

    month_energy_rev = month_domestic_rev + month_commercial_rev
    month_total_rev = month_energy_rev + month_conn_fee_rev
    total_energy_rev = total_domestic_rev + total_commercial_rev
    grand_total_rev = total_energy_rev + total_conn_fee_rev

    return render(
        request,
        "admins/revenue.html",
        {
            **_admin_shared_context(active_nav="revenue", page_label="Revenue & Financial Analytics"),
            "default_month": default_month,
            "revenue_month": revenue_month,
            "category_filter": category_filter,
            "month_domestic_revenue_rs": month_domestic_rev,
            "month_commercial_revenue_rs": month_commercial_rev,
            "month_energy_revenue_rs": month_energy_rev,
            "month_connection_fee_revenue_rs": month_conn_fee_rev,
            "month_total_revenue_rs": month_total_rev,
            "month_dom_count": month_dom_count,
            "month_comm_count": month_comm_count,
            "month_conn_count": month_conn_count,
            "month_tx_count": month_dom_count + month_comm_count + month_conn_count,
            "total_domestic_revenue_rs": total_domestic_rev,
            "total_commercial_revenue_rs": total_commercial_rev,
            "total_energy_revenue_rs": total_energy_rev,
            "total_connection_fee_revenue_rs": total_conn_fee_rev,
            "total_revenue_rs": grand_total_rev,
            "total_paid_bills_count": len(paid_bills),
            "total_conn_fees_count": len(paid_conn_fees),
            "month_transactions": month_transactions,
            "monthly_breakdown": monthly_breakdown,
        },
    )


@never_cache
def admin_maintenance(request):
    """Administration Maintenance Schedule Permission Review & Approved Power Cut Execution."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    Transformer.ensure_defaults()
    q = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip()
    scope_filter = request.GET.get("scope", "").strip()
    transformer_filter = request.GET.get("transformer_id", "").strip()
    per_page_raw = request.GET.get("per_page", "10").strip()
    try:
        per_page = int(per_page_raw)
    except ValueError:
        per_page = 10
    if per_page not in (5, 10, 20, 50):
        per_page = 10

    qs = MaintenanceSchedule.objects.select_related(
        "requested_by", "transformer", "target_connection"
    ).all()
    if q:
        qs = qs.filter(
            Q(schedule_id__icontains=q)
            | Q(title__icontains=q)
            | Q(reason__icontains=q)
            | Q(transformer__code__icontains=q)
            | Q(target_connection__meter_number__icontains=q)
            | Q(requested_by__employee_id__icontains=q)
        )
    if status_filter:
        qs = qs.filter(status=status_filter)
    if scope_filter in (
        MaintenanceSchedule.SCOPE_TRANSFORMER,
        MaintenanceSchedule.SCOPE_PREMISE,
        MaintenanceSchedule.SCOPE_ALL,
    ):
        qs = qs.filter(scope=scope_filter)
    if transformer_filter and transformer_filter.isdigit():
        qs = qs.filter(transformer_id=int(transformer_filter))

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)

    return render(
        request,
        "admins/maintenance.html",
        {
            **_admin_shared_context(active_nav="maintenance", page_label="Maintenance Approvals & Scheduled Power Cuts"),
            "page_obj": page_obj,
            "transformers": Transformer.objects.all(),
            "status_choices": MaintenanceSchedule.STATUS_CHOICES,
            "scope_choices": MaintenanceSchedule.SCOPE_CHOICES,
            "search_q": q,
            "selected_status": status_filter,
            "selected_scope": scope_filter,
            "selected_transformer_filter": transformer_filter,
            "per_page": per_page,
            "per_page_choices": (5, 10, 20, 50),
            "query_no_page": params.urlencode(),
            "completed_maintenance_count": MaintenanceSchedule.objects.filter(
                status=MaintenanceSchedule.STATUS_COMPLETED
            ).count(),
        },
    )


@require_POST
def admin_process_maintenance(request, sched_id):
    """Approve/Reject Department Maintenance Schedule and Execute/Restore Power Cut strictly on Approved schedules."""
    if not _is_admin_authenticated(request):
        return redirect(f"{reverse('admin_login')}?auth_required=1")

    sched = get_object_or_404(
        MaintenanceSchedule.objects.select_related("transformer", "target_connection"),
        pk=sched_id,
    )
    action = request.POST.get("action", "").strip().lower()
    remarks = request.POST.get("admin_remarks", "").strip()

    if action == "approve":
        if sched.status != MaintenanceSchedule.STATUS_PENDING:
            messages.error(request, f"Maintenance {sched.schedule_id} is not pending approval.")
            return redirect("admin_maintenance")
        sched.status = MaintenanceSchedule.STATUS_APPROVED
        sched.reviewed_by = ADMIN_USERNAME
        sched.reviewed_at = timezone.now()
        sched.admin_remarks = remarks or "Approved by Administration. Scheduled power cut permission granted."
        sched.save()
        messages.success(
            request,
            f"Permission Approved for {sched.schedule_id} ({sched.title}). Department staff can now execute the scheduled power cut when work begins.",
        )
        return redirect("admin_maintenance")

    if action == "reject":
        if sched.status != MaintenanceSchedule.STATUS_PENDING:
            messages.error(request, f"Only pending maintenance requests can be rejected.")
            return redirect("admin_maintenance")
        sched.status = MaintenanceSchedule.STATUS_REJECTED
        sched.reviewed_by = ADMIN_USERNAME
        sched.reviewed_at = timezone.now()
        sched.admin_remarks = remarks or "Rejected by Administration."
        sched.save()
        messages.warning(request, f"Maintenance schedule {sched.schedule_id} rejected.")
        return redirect("admin_maintenance")

    if action in ("execute_cut", "complete_restore"):
        messages.error(
            request,
            "Maintenance power cuts and power restoration must be executed by the Department staff on the Department Portal.",
        )
        return redirect("admin_maintenance")

    messages.error(request, "Invalid maintenance action.")
    return redirect("admin_maintenance")

    messages.error(request, "Invalid maintenance action.")
    return redirect("admin_maintenance")


def admin_logout(request):
    """Clear the admin auth cookie and redirect back to the public home page."""
    messages.success(request, "You have been logged out from the Administration Portal.")
    response = redirect("home")
    response.delete_cookie(ADMIN_AUTH_COOKIE)
    return response


def admin_settings(request, active_tab="all"):
    """Administration settings dashboard for News Announcements, Operation & Maintenance Notifications, and AI/LLM configurations."""
    if not _is_admin_authenticated(request):
        messages.error(request, "Please log in as Administrator to access system settings.")
        return redirect("home")

    tab = request.GET.get("tab") or active_tab or "all"
    test_result = None

    if request.method == "POST":
        action = request.POST.get("action", "").strip()

        # --- 1. NEWS ANNOUNCEMENTS ACTIONS ---
        if action == "add_news":
            title = request.POST.get("title", "").strip()
            content = request.POST.get("content", "").strip()
            badge_label = request.POST.get("badge_label", "New").strip()
            link_url = request.POST.get("link_url", "").strip()
            is_ticker = request.POST.get("is_ticker") == "1"
            is_active = request.POST.get("is_active") == "1"

            if not title:
                messages.error(request, "News announcement title is required.")
            else:
                SiteNewsAnnouncement.objects.create(
                    title=title,
                    content=content,
                    badge_label=badge_label or "New",
                    link_url=link_url,
                    is_ticker=is_ticker,
                    is_active=is_active,
                )
                messages.success(request, f"News announcement '{title}' published successfully.")
            return redirect(f"{reverse('admin_settings')}?tab=news")

        elif action == "toggle_news":
            news_id = request.POST.get("news_id")
            news_obj = get_object_or_404(SiteNewsAnnouncement, pk=news_id)
            news_obj.is_active = not news_obj.is_active
            news_obj.save(update_fields=["is_active", "updated_at"])
            state_str = "activated and will show on public portal" if news_obj.is_active else "deactivated"
            messages.success(request, f"News announcement '{news_obj.title}' {state_str}.")
            return redirect(f"{reverse('admin_settings')}?tab=news")

        elif action == "delete_news":
            news_id = request.POST.get("news_id")
            news_obj = get_object_or_404(SiteNewsAnnouncement, pk=news_id)
            title = news_obj.title
            news_obj.delete()
            messages.success(request, f"News announcement '{title}' deleted.")
            return redirect(f"{reverse('admin_settings')}?tab=news")

        # --- 2. OPERATION & MAINTENANCE NOTIFICATIONS ACTIONS ---
        elif action == "add_notification":
            title = request.POST.get("title", "").strip()
            message = request.POST.get("message", "").strip()
            category = request.POST.get("category", OperationMaintenanceNotification.CATEGORY_MAINTENANCE)
            severity = request.POST.get("severity", OperationMaintenanceNotification.SEVERITY_WARNING)
            transformer_id = request.POST.get("transformer_id") or None
            affected_areas = request.POST.get("affected_areas", "").strip()
            is_active = request.POST.get("is_active") == "1"

            related_tf = None
            if transformer_id:
                try:
                    related_tf = Transformer.objects.get(pk=transformer_id)
                except (Transformer.DoesNotExist, ValueError):
                    pass

            if not title or not message:
                messages.error(request, "Both title and message are required for an Operation & Maintenance alert.")
            else:
                OperationMaintenanceNotification.objects.create(
                    title=title,
                    message=message,
                    category=category,
                    severity=severity,
                    related_transformer=related_tf,
                    affected_areas=affected_areas,
                    is_active=is_active,
                )
                messages.success(request, f"Operation & Maintenance notice '{title}' broadcast to base.html successfully.")
            return redirect(f"{reverse('admin_settings')}?tab=notifications")

        elif action == "toggle_notification":
            note_id = request.POST.get("notification_id")
            note_obj = get_object_or_404(OperationMaintenanceNotification, pk=note_id)
            note_obj.is_active = not note_obj.is_active
            note_obj.save(update_fields=["is_active", "updated_at"])
            state_str = "broadcast active" if note_obj.is_active else "hidden from public portal"
            messages.success(request, f"Notification '{note_obj.title}' is now {state_str}.")
            return redirect(f"{reverse('admin_settings')}?tab=notifications")

        elif action == "delete_notification":
            note_id = request.POST.get("notification_id")
            note_obj = get_object_or_404(OperationMaintenanceNotification, pk=note_id)
            title = note_obj.title
            note_obj.delete()
            messages.success(request, f"Notification '{title}' removed.")
            return redirect(f"{reverse('admin_settings')}?tab=notifications")

        # --- 3. AI & LLM CONFIGURATION ACTIONS ---
        elif action == "save_llm":
            config_id = request.POST.get("config_id")
            name = request.POST.get("name", "").strip()
            provider = request.POST.get("provider", LLMConfiguration.PROVIDER_GEMINI).strip()
            model_name = request.POST.get("model_name", "gemini-1.5-flash").strip()
            api_key = request.POST.get("api_key", "").strip()
            api_url = request.POST.get("api_url", "").strip()
            temperature_raw = request.POST.get("temperature", "0.70").strip()
            max_tokens_raw = request.POST.get("max_tokens", "1024").strip()
            system_prompt = request.POST.get("system_prompt", "").strip()
            is_active = request.POST.get("is_active") == "1"
            is_default = request.POST.get("is_default") == "1"

            try:
                temperature = Decimal(temperature_raw)
            except Exception:
                temperature = Decimal("0.70")

            try:
                max_tokens = int(max_tokens_raw)
            except Exception:
                max_tokens = 1024

            if not name or not model_name:
                messages.error(request, "Configuration Name and Model Name are required.")
                return redirect(f"{reverse('admin_settings')}?tab=llm")

            if config_id:
                cfg = get_object_or_404(LLMConfiguration, pk=config_id)
                cfg.name = name
                cfg.provider = provider
                cfg.model_name = model_name
                if api_key != "" or "api_key" in request.POST:
                    cfg.api_key = api_key
                cfg.api_url = api_url
                cfg.temperature = temperature
                cfg.max_tokens = max_tokens
                cfg.system_prompt = system_prompt
                cfg.is_active = is_active
                cfg.is_default = is_default
                cfg.save()
                messages.success(request, f"LLM Configuration '{name}' updated successfully in database.")
            else:
                cfg = LLMConfiguration.objects.create(
                    name=name,
                    provider=provider,
                    model_name=model_name,
                    api_key=api_key,
                    api_url=api_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    is_active=is_active,
                    is_default=is_default,
                )
                messages.success(request, f"LLM Configuration '{name}' saved to database.")

            return redirect(f"{reverse('admin_settings')}?tab=llm")

        elif action == "set_default_llm":
            config_id = request.POST.get("config_id")
            cfg = get_object_or_404(LLMConfiguration, pk=config_id)
            cfg.is_default = True
            cfg.is_active = True
            cfg.save()
            messages.success(request, f"'{cfg.name}' is now set as the default active LLM configuration.")
            return redirect(f"{reverse('admin_settings')}?tab=llm")

        elif action == "toggle_llm":
            config_id = request.POST.get("config_id")
            cfg = get_object_or_404(LLMConfiguration, pk=config_id)
            cfg.is_active = not cfg.is_active
            cfg.save(update_fields=["is_active", "updated_at"])
            state_str = "Active" if cfg.is_active else "Inactive"
            messages.success(request, f"LLM configuration '{cfg.name}' marked {state_str}.")
            return redirect(f"{reverse('admin_settings')}?tab=llm")

        elif action == "delete_llm":
            config_id = request.POST.get("config_id")
            cfg = get_object_or_404(LLMConfiguration, pk=config_id)
            if cfg.is_default:
                messages.error(request, "Cannot delete the default active LLM configuration. Set another configuration as default first.")
            else:
                name = cfg.name
                cfg.delete()
                messages.success(request, f"LLM configuration '{name}' deleted.")
            return redirect(f"{reverse('admin_settings')}?tab=llm")

        elif action == "test_llm":
            config_id = request.POST.get("config_id")
            test_result = test_llm_connection(int(config_id))
            if test_result["ok"]:
                messages.success(request, f"Test Successful: {test_result['message']} (Reply: {test_result.get('reply', '')})")
            else:
                messages.error(request, f"Test Connection Failed: {test_result['error']}")
            return redirect(f"{reverse('admin_settings')}?tab=llm")

        # --- 4. HERO MEDIA & BACKGROUND ACTIONS ---
        elif action == "save_media":
            media = SiteMediaSetting.get_active()
            if "hero_bg_image" in request.FILES:
                media.hero_bg_image = request.FILES["hero_bg_image"]
            bg_url = request.POST.get("hero_bg_image_url", "").strip()
            if bg_url != "" or "hero_bg_image" not in request.FILES:
                media.hero_bg_image_url = bg_url

            if "hero_video" in request.FILES:
                media.hero_video = request.FILES["hero_video"]
            vid_url = request.POST.get("hero_video_url", "").strip()
            if vid_url != "" or "hero_video" not in request.FILES:
                media.hero_video_url = vid_url

            if "hero_video_poster" in request.FILES:
                media.hero_video_poster = request.FILES["hero_video_poster"]
            poster_url = request.POST.get("hero_video_poster_url", "").strip()
            if poster_url != "" or "hero_video_poster" not in request.FILES:
                media.hero_video_poster_url = poster_url

            media.hero_heading = request.POST.get("hero_heading", "").strip()
            media.hero_subheading = request.POST.get("hero_subheading", "").strip()
            media.save()
            messages.success(request, "base.html hero background image and video updated successfully.")
            return redirect(f"{reverse('admin_settings')}?tab=media")

        elif action == "reset_media":
            media = SiteMediaSetting.get_active()
            media.hero_bg_image = None
            media.hero_bg_image_url = "/static/images/electricity-grid-background.png"
            media.hero_video = None
            media.hero_video_url = "/static/videos/ai-electricity-voice-agent.mp4"
            media.hero_video_poster = None
            media.hero_video_poster_url = "/static/images/electricity-grid-background.png"
            media.hero_heading = ""
            media.hero_subheading = ""
            media.save()
            messages.success(request, "Hero background image and video reset to default assets.")
            return redirect(f"{reverse('admin_settings')}?tab=media")

    # GET request: load records
    news_list = SiteNewsAnnouncement.objects.all().order_by("-created_at")
    notifications_list = OperationMaintenanceNotification.objects.select_related("related_transformer").order_by("-created_at")
    llm_configs = LLMConfiguration.objects.all().order_by("-is_default", "-updated_at")
    active_default_llm = LLMConfiguration.get_active_config()
    transformers = list(Transformer.objects.all().order_by("code"))
    site_media = SiteMediaSetting.get_active()

    page_label = "System Settings"
    if tab == "news":
        page_label = "News & Public Announcements"
    elif tab == "notifications":
        page_label = "Operation & Maintenance Alerts"
    elif tab == "llm":
        page_label = "AI & LLM Configurations"
    elif tab == "media":
        page_label = "Hero Media & Background Settings"

    context = {
        **_admin_shared_context(active_nav="settings", page_label=page_label),
        "active_tab": tab,
        "news_list": news_list,
        "notifications_list": notifications_list,
        "llm_configs": llm_configs,
        "active_default_llm": active_default_llm,
        "transformers": transformers,
        "site_media": site_media,
        "provider_choices": LLMConfiguration.PROVIDER_CHOICES,
        "category_choices": OperationMaintenanceNotification.CATEGORY_CHOICES,
        "severity_choices": OperationMaintenanceNotification.SEVERITY_CHOICES,
        "test_result": test_result,
    }
    return render(request, "admins/settings.html", context)


def admin_settings_news(request):
    """Direct route for News & Public Announcements tab."""
    return admin_settings(request, active_tab="news")


def admin_settings_notifications(request):
    """Direct route for Operation & Maintenance Alerts tab."""
    return admin_settings(request, active_tab="notifications")


def admin_settings_media(request):
    """Direct route for Background & Video Media tab."""
    return admin_settings(request, active_tab="media")


def admin_settings_llm(request):
    """Direct route for AI & LLM Configurations tab."""
    return admin_settings(request, active_tab="llm")


def admin_settings_llm_test_api(request, config_id):
    """API endpoint for testing LLM connection via AJAX/JSON."""
    if not _is_admin_authenticated(request):
        return JsonResponse({"ok": False, "error": "Admin authentication required."}, status=403)
    res = test_llm_connection(config_id)
    return JsonResponse(res)


@never_cache
def admin_voice_agents(request):
    """Administration directory and live monitoring for the 10 Call Center AI Voice Agents."""
    if not _is_admin_authenticated(request):
        return redirect("admin_login")

    from voice_agent.models import CallCenterAgent, VoiceCallSession
    CallCenterAgent.seed_default_10_agents()

    status_filter = request.GET.get("status", "").strip().upper()
    role_filter = request.GET.get("role", "").strip()

    agents_qs = CallCenterAgent.objects.filter(agent_number__gte=1, agent_number__lte=10).order_by("agent_number")
    if status_filter in [
        CallCenterAgent.STATUS_AVAILABLE,
        CallCenterAgent.STATUS_ON_CALL,
        CallCenterAgent.STATUS_BUSY,
        CallCenterAgent.STATUS_OFFLINE,
    ]:
        agents_qs = agents_qs.filter(status=status_filter)

    agents = list(agents_qs)
    total_count = CallCenterAgent.objects.filter(agent_number__gte=1, agent_number__lte=10).count()
    available_count = CallCenterAgent.objects.filter(agent_number__gte=1, agent_number__lte=10, status=CallCenterAgent.STATUS_AVAILABLE).count()
    on_call_count = CallCenterAgent.objects.filter(agent_number__gte=1, agent_number__lte=10, status=CallCenterAgent.STATUS_ON_CALL).count()
    offline_count = CallCenterAgent.objects.filter(agent_number__gte=1, agent_number__lte=10, status=CallCenterAgent.STATUS_OFFLINE).count()
    total_calls_handled = sum(a.calls_handled for a in CallCenterAgent.objects.filter(agent_number__gte=1, agent_number__lte=10))

    # Recent calling sessions
    recent_call_sessions = (
        VoiceCallSession.objects.select_related("assigned_agent", "user_account")
        .order_by("-started_at")[:12]
    )

    ctx = _admin_shared_context(
        active_nav="voice_agents",
        page_label="24/7 AI Voice Calling Agents (10 Executives)"
    )
    ctx.update({
        "agents": agents,
        "total_count": total_count,
        "available_count": available_count,
        "on_call_count": on_call_count,
        "offline_count": offline_count,
        "total_calls_handled": total_calls_handled,
        "recent_call_sessions": recent_call_sessions,
        "status_filter": status_filter,
    })
    return render(request, "admins/voice_agents.html", ctx)


@csrf_exempt
@require_POST
def admin_toggle_agent_status(request, agent_id):
    """Toggle agent status between Available, On Call, Busy, and Offline."""
    if not _is_admin_authenticated(request):
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=401)

    from voice_agent.models import CallCenterAgent
    agent = get_object_or_404(CallCenterAgent, id=agent_id)

    new_status = request.POST.get("status", "").strip().upper()
    if new_status in [
        CallCenterAgent.STATUS_AVAILABLE,
        CallCenterAgent.STATUS_ON_CALL,
        CallCenterAgent.STATUS_BUSY,
        CallCenterAgent.STATUS_OFFLINE,
    ]:
        agent.status = new_status
    else:
        # Cycle through statuses: AVAILABLE -> OFFLINE -> AVAILABLE
        if agent.status == CallCenterAgent.STATUS_AVAILABLE:
            agent.status = CallCenterAgent.STATUS_OFFLINE
        else:
            agent.status = CallCenterAgent.STATUS_AVAILABLE

    agent.is_active = (agent.status != CallCenterAgent.STATUS_OFFLINE)
    agent.save()
    return JsonResponse({
        "success": True,
        "agent_id": agent.id,
        "agent_number": agent.agent_number,
        "short_name": agent.short_name,
        "name": agent.name,
        "status": agent.status,
        "status_display": agent.get_status_display(),
        "is_active": agent.is_active,
    })


AGENT_OFFICER_SAMPLE_SPEECHES = {
    1: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Agent 1 - Officer Priya, Outage & Emergency Dispatch Specialist. Are you calling to report a power outage or cut at your house right now?",
    2: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Agent 2 - Officer Vikram, High Voltage Grid & Substation Specialist. I have direct access to your distribution transformer and feeder telemetry. How may I assist your grid inquiries today?",
    3: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Agent 3 - Officer Ananya, Billing & Tariffs Specialist. I can help verify your meter ledgers, explain tariff calculations, or guide you through online payments. How may I assist you?",
    4: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Agent 4 - Officer Rahul, Electrical Safety & Hazard Response Officer. Are you reporting a live sparking wire, fallen electrical cable, or transformer hazard?",
    5: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Agent 5 - Officer Sneha, Substation & Transformer Diagnostic Specialist. I am monitoring circuit breakers and load telemetry for your sector. What diagnostic checks can I run for you?",
    6: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Agent 6 - Officer Rajesh, Smart Meter & Service Connection Specialist. Are you reporting a blank digital display, burnt meter terminal, or a reading discrepancy?",
    7: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Agent 7 - Officer Kavita, Citizen Grievance & SLA Escalation Officer. I am here to ensure delayed complaints are prioritized. How may I assist your grievance today?",
    8: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Officer Amit, Field Line Crew Coordinator. I track our mobile repair vans and lineman crews in real time. How can I assist with your restoration dispatch?",
    9: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Officer Pooja, Commercial Accounts & Energy Audit Specialist. How may I assist with your high-tension tariffs, commercial billing, or sanctioned load today?",
    10: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Officer Suresh, Senior Shift In-Charge Supervisor. I oversee all helpline operations and emergency dispatch. How can I assist you with your department inquiry?",
    11: "Hello! Welcome to the Electricity Department 24/7 Helpline. This is Officer Ritu from Quality Assurance and Customer Feedback. On a scale of 0 to 5, how would you rate your support experience today?",
}


@csrf_exempt
@require_POST
def admin_agent_sample_voice(request, agent_id):
    """Generate and return studio-grade real human neural audio (Edge-TTS) for testing agent voice."""
    if not _is_admin_authenticated(request):
        return JsonResponse({"success": False, "error": "Unauthorized"}, status=401)

    from voice_agent.models import CallCenterAgent
    from voice_agent.tts_utils import generate_neural_speech_base64, get_agent_voice_params
    agent = get_object_or_404(CallCenterAgent, id=agent_id)

    ag_num = getattr(agent, "agent_number", None)
    sample_text = (
        agent.greeting_sample
        or AGENT_OFFICER_SAMPLE_SPEECHES.get(ag_num)
        or f"Hello! Welcome to the Electricity Department 24/7 Helpline. This is {agent.short_name}, {agent.officer_name}, {agent.role}. I am online and ready to assist you. How can I help you today?"
    )

    v_params = get_agent_voice_params(agent)
    audio_base64 = generate_neural_speech_base64(
        sample_text,
        voice=v_params["voice"],
        rate=v_params.get("rate", "+3%"),
        pitch=v_params.get("pitch", "+0Hz"),
    )

    return JsonResponse({
        "success": True,
        "agent_id": agent.id,
        "officer_name": agent.officer_name,
        "gender": agent.gender,
        "voice_name": v_params["voice"],
        "rate": v_params.get("rate", "+3%"),
        "pitch": v_params.get("pitch", "+0Hz"),
        "sample_text": sample_text,
        "audio_base64": audio_base64,
    })

