from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from admins.models import (
    MaintenanceSchedule,
    OperationMaintenanceNotification,
    SiteNewsAnnouncement,
    Transformer,
)

from .billing import (
    DEFAULT_BUILTIN_STATES,
    MIN_COMMERCIAL_BULK_UNITS,
    build_meter_billing_summary,
    get_active_tariff,
    get_builtin_appliance_states,
    get_commercial_bulk_packs,
    get_connection_appliance_telemetry,
    get_connection_fee_breakdown,
    pay_connection_application_fee,
    pay_domestic_monthly_bill,
    pay_published_commercial_bill,
    process_commercial_prepaid_payment,
    quantize_2,
    record_power_usage,
    sync_elapsed_appliance_usage,
)
from .pdf_utils import build_connection_application_pdf, build_power_bill_invoice_pdf
from .forms import (
    ConnectionApplicationForm,
    CreateAccountForm,
    UserApplianceForm,
    UserLoginForm,
)
from .models import ConnectionApplication, PowerBill, UserAccount, UserAppliance, UserNotification


def _get_authenticated_user(request):
    user_id = request.session.get("user_account_id")
    account = UserAccount.objects.filter(id=user_id).first() if user_id else None
    if account is None or account.status in (
        UserAccount.STATUS_INACTIVE,
        UserAccount.STATUS_SUSPENDED,
    ):
        return None
    return account


def _get_user_connection(account):
    return (
        ConnectionApplication.objects.filter(user=account)
        .select_related("transformer", "verified_by", "connected_by")
        .order_by("-created_at")
        .first()
    )


def create_account(request):
    """Handle self-service registration via the Create Account modal on base.html."""
    if request.method == "POST":
        form = CreateAccountForm(request.POST)
        if form.is_valid():
            account = form.save()
            request.session["user_login_success"] = (
                f"Account created successfully! Current account status: {account.status}. "
                "Please sign in with your credentials."
            )
            request.session["prefill_user_username"] = account.username
            return redirect("user_login")

        return render(
            request,
            "base.html",
            {
                "create_account_form": form,
                "open_create_account_modal": True,
            },
        )

    return render(
        request,
        "base.html",
        {
            "create_account_form": CreateAccountForm(),
            "open_create_account_modal": True,
        },
    )


def user_login(request):
    """Authenticate user credentials via UserAccount and redirect to users/userhome.html."""
    if request.method == "POST":
        form = UserLoginForm(request.POST)
        if form.is_valid():
            account = form.authenticated_account
            request.session["user_account_id"] = account.id
            request.session["user_username"] = account.username
            return redirect("user_home")

        return render(
            request,
            "base.html",
            {
                "user_login_form": form,
                "open_user_login_modal": True,
                "entered_user_username": request.POST.get("username", "").strip(),
            },
        )

    user_login_success = request.session.pop("user_login_success", None)
    prefill_username = request.session.pop("prefill_user_username", "")
    context = {
        "user_login_form": UserLoginForm(),
        "open_user_login_modal": True,
        "user_login_success": user_login_success,
        "entered_user_username": prefill_username,
    }
    if request.GET.get("auth_required") == "1":
        context["user_login_auth_error"] = "Please sign in to view your consumer dashboard."
    return render(request, "base.html", context)


def _require_user_or_redirect(request):
    account = _get_authenticated_user(request)
    if account is None:
        request.session.pop("user_account_id", None)
        request.session.pop("user_username", None)
        return None, redirect(f"{reverse('user_login')}?auth_required=1")
    return account, None


def _build_user_notifications(account, connection, effective_power_on=True, billing_summary=None):
    """Compile rich, real-time consumer notifications for Maintenance, Bill Reminders, and Live Grid/Power Status."""
    notifications = []
    now_local = timezone.localtime()

    # ---------------------------------------------------------
    # 0. PERSISTENT INVOICE & VOICE AGENT DELIVERED USER NOTIFICATIONS
    # ---------------------------------------------------------
    if account:
        try:
            custom_notifs = UserNotification.objects.filter(user=account).order_by("-created_at")[:10]
            for un in custom_notifs:
                notifications.append({
                    "id": f"custom-notif-{un.id}",
                    "category": un.category,
                    "category_label": un.category_label,
                    "severity": un.severity,
                    "icon": un.icon,
                    "badge": un.badge,
                    "title": un.title,
                    "message": un.message,
                    "extra_info": un.extra_info,
                    "timestamp": un.created_at,
                    "cta_label": un.cta_label,
                    "cta_url": un.cta_url,
                    "is_live": un.is_live,
                })
        except Exception:
            pass

    # ---------------------------------------------------------
    # 1. LIVE POWER & GRID STATUS NOTIFICATIONS
    # ---------------------------------------------------------
    if connection and connection.is_connected:
        tf = connection.transformer
        if tf and not tf.is_power_on:
            notifications.append({
                "id": f"live-tf-outage-{tf.id}",
                "category": "live",
                "category_label": "Live Feeder Alert",
                "severity": "critical",
                "icon": "fa-solid fa-triangle-exclamation",
                "badge": "FEEDER OUTAGE",
                "title": f"Substation Feeder {tf.code} Supply Down",
                "message": f"Transformer {tf.code} ({tf.substation_name}, {tf.zone}) is currently de-energized. Grid supply to your connection is temporarily suspended.",
                "extra_info": f"Substation: {tf.substation_name}",
                "timestamp": now_local,
                "cta_label": "Check Feeder Status",
                "cta_url": reverse("user_connection_status"),
                "is_live": True,
            })
        elif connection.is_power_cut:
            notifications.append({
                "id": f"live-premise-cut-{connection.id}",
                "category": "live",
                "category_label": "Live Power Cut",
                "severity": "critical",
                "icon": "fa-solid fa-plug-circle-xmark",
                "badge": "POWER CUT ACTIVE",
                "title": f"Premise Power Cut on Meter {connection.meter_number}",
                "message": f"Electricity supply to your premise has been isolated. Reason: {connection.power_cut_remark or 'Supply isolation executed by DISCOM/Department.'}",
                "extra_info": f"Meter: {connection.meter_number}",
                "timestamp": connection.power_cut_updated_at or now_local,
                "cta_label": "View Cut Details",
                "cta_url": reverse("user_connection_status"),
                "is_live": True,
            })
        elif connection.is_commercial and connection.prepaid_units_balance <= Decimal("0.00"):
            notifications.append({
                "id": f"live-commercial-exhausted-{connection.id}",
                "category": "bills",
                "category_label": "Prepaid Exhaustion",
                "severity": "critical",
                "icon": "fa-solid fa-battery-empty",
                "badge": "0.00 UNITS LEFT",
                "title": "Commercial Prepaid Energy Quota Exhausted",
                "message": f"Meter {connection.meter_number} has consumed all prepaid bulk units (0.00 Units left). Power supply is isolated until top-up.",
                "extra_info": "Zero Balance Isolation",
                "timestamp": now_local,
                "cta_label": "Recharge Bulk Units Now",
                "cta_url": f"{reverse('user_billing')}?meter_number={connection.meter_number}",
                "is_live": True,
            })
        elif not account.house_power_on:
            notifications.append({
                "id": f"live-breaker-off-{account.id}",
                "category": "live",
                "category_label": "Main Breaker Standby",
                "severity": "warning",
                "icon": "fa-solid fa-power-off",
                "badge": "MAIN BREAKER OFF",
                "title": "House Main Switch is Turned OFF",
                "message": f"Grid supply from Feeder {tf.code if tf else ''} is online, but your premise indoor main breaker is open. Switch it ON in simulation to run appliances.",
                "extra_info": "Premise Breaker Open",
                "timestamp": now_local,
                "cta_label": "Turn ON Main Switch",
                "cta_url": reverse("user_power_simulation"),
                "is_live": True,
            })
        else:
            telemetry = get_connection_appliance_telemetry(connection)
            active_kw = telemetry.get("active_kw", Decimal("0.00"))
            active_count = telemetry.get("active_count", 0)
            notifications.append({
                "id": f"live-power-ok-{connection.id}",
                "category": "live",
                "category_label": "Live Grid Supply",
                "severity": "success",
                "icon": "fa-solid fa-bolt",
                "badge": "LIVE ENERGIZED",
                "title": f"Grid Power Active & Energized ({tf.code if tf else 'Online'})",
                "message": f"Substation Feeder {tf.code if tf else ''} ({tf.substation_name if tf else ''}) is active. Running {active_count} appliance(s) at {active_kw} kW load. Recorded: {connection.total_units_consumed} Units.",
                "extra_info": f"Live Load: {active_kw} kW",
                "timestamp": now_local,
                "cta_label": "Open Live Simulation",
                "cta_url": reverse("user_power_simulation"),
                "is_live": True,
            })
    elif connection and not connection.is_connected:
        notifications.append({
            "id": f"live-app-progress-{connection.id}",
            "category": "live",
            "category_label": "Connection Tracking",
            "severity": "info",
            "icon": "fa-solid fa-hourglass-half",
            "badge": connection.status.upper(),
            "title": f"Application {connection.application_number or connection.application_id} In Progress",
            "message": f"Status: {connection.status}. Payment verified: {'Yes' if connection.payment_verified else 'No'}. Assigned Feeder: {connection.transformer.code if connection.transformer else 'Pending assignment'}.",
            "extra_info": f"Req ID: {connection.application_id}",
            "timestamp": connection.updated_at or now_local,
            "cta_label": "Track Application Progress",
            "cta_url": reverse("user_connection_status"),
            "is_live": True,
        })
    elif not connection:
        notifications.append({
            "id": "live-new-user-welcome",
            "category": "live",
            "category_label": "Onboarding",
            "severity": "info",
            "icon": "fa-solid fa-plug-circle-plus",
            "badge": "GET CONNECTED",
            "title": "Welcome! Register for New Electricity Connection",
            "message": "Apply for Domestic or Commercial power connection to receive your DISCOM Request ID and smart meter.",
            "extra_info": "Domestic & Commercial",
            "timestamp": now_local,
            "cta_label": "Apply New Connection",
            "cta_url": reverse("user_new_connection"),
            "is_live": False,
        })

    # ---------------------------------------------------------
    # 2. MAINTENANCE SCHEDULES & DISCOM ADVISORIES
    # ---------------------------------------------------------
    mnt_qs = MaintenanceSchedule.objects.filter(
        status__in=[
            MaintenanceSchedule.STATUS_APPROVED,
            MaintenanceSchedule.STATUS_ACTIVE_CUT,
        ]
    )
    if connection and connection.transformer:
        mnt_qs = mnt_qs.filter(
            Q(scope=MaintenanceSchedule.SCOPE_ALL)
            | Q(scope=MaintenanceSchedule.SCOPE_TRANSFORMER, transformer=connection.transformer)
            | Q(scope=MaintenanceSchedule.SCOPE_PREMISE, target_connection=connection)
        )
    else:
        mnt_qs = mnt_qs.filter(scope=MaintenanceSchedule.SCOPE_ALL)

    for m in mnt_qs.select_related("transformer", "target_connection")[:5]:
        is_active_cut = (m.status == MaintenanceSchedule.STATUS_ACTIVE_CUT)
        target_str = (
            "Overall Grid" if m.scope == MaintenanceSchedule.SCOPE_ALL
            else (f"Feeder {m.transformer.code}" if m.transformer else "Premise")
        )
        notifications.append({
            "id": f"mnt-sched-{m.id}",
            "category": "maintenance",
            "category_label": "Maintenance Advisory",
            "severity": "critical" if is_active_cut else "warning",
            "icon": "fa-solid fa-screwdriver-wrench",
            "badge": "POWER CUT IN PROGRESS" if is_active_cut else "APPROVED MAINTENANCE",
            "title": f"Maintenance Notice: {m.title} ({m.schedule_id})",
            "message": (
                f"{m.reason} | Target: {target_str} | "
                f"Window: {timezone.localtime(m.scheduled_start).strftime('%d %b %I:%M %p')} to {timezone.localtime(m.scheduled_end).strftime('%d %b %I:%M %p')}."
            ),
            "extra_info": f"Schedule ID: {m.schedule_id}",
            "timestamp": m.cut_executed_at or m.reviewed_at or m.created_at,
            "cta_label": "View Maintenance Notice",
            "cta_url": reverse("user_connection_status"),
            "is_live": is_active_cut,
        })

    try:
        op_notifs = OperationMaintenanceNotification.objects.filter(is_active=True).select_related("related_transformer")
        if connection and connection.transformer:
            op_notifs = op_notifs.filter(
                Q(related_transformer=connection.transformer) | Q(related_transformer__isnull=True)
            )
        for op in op_notifs[:5]:
            notifications.append({
                "id": f"op-notif-{op.id}",
                "category": "maintenance",
                "category_label": op.get_category_display(),
                "severity": op.severity,
                "icon": "fa-solid fa-bullhorn",
                "badge": op.severity.upper(),
                "title": f"DISCOM Alert: {op.title}",
                "message": f"{op.message}" + (f" (Affected: {op.affected_areas})" if op.affected_areas else ""),
                "extra_info": op.get_category_display(),
                "timestamp": op.created_at,
                "cta_label": "View Portal Advisory",
                "cta_url": reverse("home"),
                "is_live": (op.severity == "critical"),
            })
    except Exception:
        pass

    # ---------------------------------------------------------
    # 3. BILL REMINDERS & PAYMENT ALERTS
    # ---------------------------------------------------------
    if connection:
        if not connection.connection_fee_paid:
            notifications.append({
                "id": f"bill-conn-fee-{connection.id}",
                "category": "bills",
                "category_label": "Connection Fee",
                "severity": "warning",
                "icon": "fa-solid fa-receipt",
                "badge": "FEE DUE",
                "title": f"Connection Charges Pending: Rs {connection.connection_fee_amount}",
                "message": f"Application {connection.application_number or connection.application_id} charges (Rs {connection.connection_fee_amount}) are unpaid. Complete payment to initiate field verification.",
                "extra_info": f"App: {connection.application_id}",
                "timestamp": connection.created_at,
                "cta_label": f"Pay Fee (Rs {connection.connection_fee_amount})",
                "cta_url": reverse("user_connection_status"),
                "is_live": False,
            })

        unpaid_domestic = PowerBill.objects.filter(
            connection=connection,
            bill_type=PowerBill.TYPE_MONTHLY,
            status=PowerBill.STATUS_UNPAID,
        ).order_by("-created_at")
        for b in unpaid_domestic:
            notifications.append({
                "id": f"bill-domestic-unpaid-{b.id}",
                "category": "bills",
                "category_label": "Monthly Bill Reminder",
                "severity": "critical" if (b.arrears_amount and b.arrears_amount > 0) else "warning",
                "icon": "fa-solid fa-file-invoice-dollar",
                "badge": "UNPAID BILL",
                "title": f"Electricity Bill Due: {b.billing_month} ({b.bill_number})",
                "message": f"Amount Payable: Rs {b.total_amount} | Billed Units: {b.units_billed} U | Energy: Rs {b.energy_charge} | Meter: {connection.meter_number}.",
                "extra_info": f"Month: {b.billing_month}",
                "timestamp": b.created_at,
                "cta_label": f"Pay Online (Rs {b.total_amount})",
                "cta_url": f"{reverse('user_billing')}?meter_number={connection.meter_number}",
                "is_live": False,
            })

        unpaid_commercial = PowerBill.objects.filter(
            connection=connection,
            bill_type=PowerBill.TYPE_COMMERCIAL,
            status=PowerBill.STATUS_UNPAID,
        ).order_by("-created_at")
        for b in unpaid_commercial:
            notifications.append({
                "id": f"bill-comm-unpaid-{b.id}",
                "category": "bills",
                "category_label": "Commercial Invoice",
                "severity": "critical",
                "icon": "fa-solid fa-building-circle-exclamation",
                "badge": "COMMERCIAL DUE",
                "title": f"Commercial Invoice Due: {b.billing_month} ({b.bill_number})",
                "message": f"Invoice Total: Rs {b.total_amount} for {b.units_billed} Units. Settle through DISCOM payment gateway.",
                "extra_info": f"Invoice {b.bill_number}",
                "timestamp": b.created_at,
                "cta_label": f"Settle Invoice (Rs {b.total_amount})",
                "cta_url": f"{reverse('user_billing')}?meter_number={connection.meter_number}",
                "is_live": False,
            })

        if connection.is_commercial and Decimal("0.00") < connection.prepaid_units_balance <= Decimal("50.00"):
            notifications.append({
                "id": f"bill-comm-low-{connection.id}",
                "category": "bills",
                "category_label": "Prepaid Quota Low",
                "severity": "warning",
                "icon": "fa-solid fa-gauge-simple-high",
                "badge": "LOW UNITS",
                "title": f"Low Bulk Prepaid Balance ({connection.prepaid_units_balance} Units Left)",
                "message": f"Meter {connection.meter_number} is running low on units. Recharge a bulk package to prevent automatic supply cutoff.",
                "extra_info": f"Balance: {connection.prepaid_units_balance} U",
                "timestamp": now_local,
                "cta_label": "Recharge Bulk Units",
                "cta_url": f"{reverse('user_billing')}?meter_number={connection.meter_number}",
                "is_live": True,
            })

    return notifications


def _user_home_context(
    account,
    conn_form=None,
    active_nav="home",
    active_subnav="",
    breadcrumbs=None,
):
    transformers = list(Transformer.ensure_defaults())
    connection = _get_user_connection(account)
    if connection and connection.is_connected:
        sync_elapsed_appliance_usage(connection.id)
        connection.refresh_from_db()
        account.refresh_from_db()
    is_connected = bool(connection and connection.is_connected)
    transformer_power_on = bool(is_connected and connection.transformer.is_power_on)
    individual_power_cut = bool(is_connected and connection.is_power_cut)
    prepaid_units_exhausted = bool(
        is_connected
        and connection.is_commercial
        and connection.prepaid_units_balance <= Decimal("0.00")
    )
    power_cut_remark = connection.power_cut_remark if connection else ""
    effective_power_on = bool(
        transformer_power_on
        and (not individual_power_cut)
        and (not prepaid_units_exhausted)
        and account.house_power_on
    )
    can_apply = connection is None or connection.status == ConnectionApplication.STATUS_REJECTED
    is_commercial = bool(
        connection and connection.property_type == ConnectionApplication.PROPERTY_COMMERCIAL
    )
    billing_summary = build_meter_billing_summary(connection) if connection else None

    if can_apply:
        next_step = {
            "step": "Step 1",
            "title": "Apply for New Connection",
            "desc": "Choose Domestic or Commercial (Industry Type) to generate Application No. & Request ID.",
            "url": reverse("user_new_connection"),
            "cta": "Apply New Connection",
        }
    elif not connection.connection_fee_paid:
        next_step = {
            "step": "Step 1B",
            "title": f"Pay Connection Charges (Rs {connection.connection_fee_amount})",
            "desc": f"Settle charges for {connection.application_number or connection.application_id} via DISCOM Gateway to download Application PDF & unlock Verification.",
            "url": reverse("user_connection_status"),
            "cta": "Pay Connection Fee",
        }
    elif not connection.payment_verified:
        next_step = {
            "step": "Verification 1",
            "title": f"Payment Verification Pending ({connection.application_id})",
            "desc": "DISCOM payment received! Waiting for Department / Accounts to verify your paid connection fee receipt before field inspection.",
            "url": reverse("user_connection_status"),
            "cta": "View Status / Download PDF",
        }
    elif not is_connected:
        next_step = {
            "step": "Step 2",
            "title": f"Track Request {connection.application_id} ({connection.status})",
            "desc": "Department assigns transformer; Admin issues Meter No.; Department connects premises.",
            "url": reverse("user_connection_status"),
            "cta": "Track Connection Status",
        }
    elif prepaid_units_exhausted:
        next_step = {
            "step": "Step 3",
            "title": "Recharge Commercial Bulk Units",
            "desc": f"Meter {connection.meter_number} has 0.00 Bulk Units left. Complete Payment Gateway checkout to unlock supply.",
            "url": f"{reverse('user_billing')}?meter_number={connection.meter_number}",
            "cta": "Bulk Recharge & Pay",
        }
    else:
        next_step = {
            "step": "Step 4",
            "title": "Operate Live Simulation & Billing",
            "desc": f"Meter {connection.meter_number} is connected on {connection.transformer.code}. Control appliances or check billing.",
            "url": reverse("user_power_simulation"),
            "cta": "Open Power Simulation",
        }

    if conn_form is None:
        conn_form = ConnectionApplicationForm(
            initial={
                "applicant_name": account.full_name,
                "mobile_number": account.mobile_number,
                "property_type": ConnectionApplication.PROPERTY_DOMESTIC,
            }
        )

    custom_appliances = [
        {
            "id": item.id,
            "name": item.name,
            "appliance_type": item.appliance_type,
            "wattage": item.wattage,
            "is_on": item.is_on,
        }
        for item in UserAppliance.objects.filter(user=account)
    ]

    base_fare_rs, dom_rate, comm_rate, tariff_setting = get_active_tariff()
    live_bulk_packs = get_commercial_bulk_packs(base_fare_rs, comm_rate)
    min_comm_payment = quantize_2(base_fare_rs + (MIN_COMMERCIAL_BULK_UNITS * comm_rate))
    domestic_fee_info = get_connection_fee_breakdown(ConnectionApplication.PROPERTY_DOMESTIC)
    commercial_fee_info = get_connection_fee_breakdown(ConnectionApplication.PROPERTY_COMMERCIAL)
    connection_fee_info = (
        get_connection_fee_breakdown(
            connection.property_type,
            total_override=connection.connection_fee_amount or None,
        )
        if connection
        else None
    )
    active_maintenance_alerts = []
    if connection and connection.is_connected:
        active_maintenance_alerts = list(
            MaintenanceSchedule.objects.filter(
                status__in=[
                    MaintenanceSchedule.STATUS_APPROVED,
                    MaintenanceSchedule.STATUS_ACTIVE_CUT,
                ]
            )
            .filter(
                Q(scope=MaintenanceSchedule.SCOPE_ALL)
                | Q(scope=MaintenanceSchedule.SCOPE_TRANSFORMER, transformer=connection.transformer)
                | Q(scope=MaintenanceSchedule.SCOPE_PREMISE, target_connection=connection)
            )
            .select_related("transformer")
        )

    user_notifications = _build_user_notifications(
        account,
        connection,
        effective_power_on=effective_power_on,
        billing_summary=billing_summary,
    )
    live_count = sum(1 for n in user_notifications if n["category"] == "live")
    maintenance_count = sum(1 for n in user_notifications if n["category"] == "maintenance")
    bills_count = sum(1 for n in user_notifications if n["category"] == "bills")
    urgent_count = sum(1 for n in user_notifications if n.get("severity") in ("critical", "warning"))

    return {
        "user_account": account,
        "transformers": transformers,
        "connection": connection,
        "active_maintenance_alerts": active_maintenance_alerts,
        "user_notifications": user_notifications,
        "user_notifications_count": len(user_notifications),
        "live_notifications_count": live_count,
        "maintenance_notifications_count": maintenance_count,
        "bills_notifications_count": bills_count,
        "urgent_notifications_count": urgent_count,
        "can_apply": can_apply,
        "conn_form": conn_form,
        "industry_choices": ConnectionApplication.INDUSTRY_CHOICES,
        "appliance_types": UserAppliance.TYPE_CHOICES,
        "custom_appliances": custom_appliances,
        "builtin_states": get_builtin_appliance_states(connection),
        "appliance_telemetry": get_connection_appliance_telemetry(connection),
        "is_commercial": is_commercial,
        "is_connected": is_connected,
        "transformer_power_on": transformer_power_on,
        "individual_power_cut": individual_power_cut,
        "prepaid_units_exhausted": prepaid_units_exhausted,
        "power_cut_remark": power_cut_remark,
        "effective_power_on": effective_power_on,
        "billing_summary": billing_summary,
        "next_step": next_step,
        "tariff_setting": tariff_setting,
        "base_fare_rs": base_fare_rs,
        "domestic_rate": dom_rate,
        "commercial_rate": comm_rate,
        "domestic_fee_info": domestic_fee_info,
        "commercial_fee_info": commercial_fee_info,
        "connection_fee_info": connection_fee_info,
        "min_commercial_bulk_units": MIN_COMMERCIAL_BULK_UNITS,
        "min_commercial_payment": min_comm_payment,
        "commercial_bulk_packs": live_bulk_packs,
        "active_nav": active_nav,
        "active_subnav": active_subnav,
        "breadcrumbs": breadcrumbs if breadcrumbs is not None else [("Home", None)],
    }


def _new_connection_breadcrumbs():
    return _service_breadcrumbs("New Connection")


def _service_breadcrumbs(current_label=None):
    crumbs = [("Home", reverse("user_home"))]
    if current_label:
        crumbs.append(("Services", reverse("user_services")))
        crumbs.append((current_label, None))
    else:
        crumbs.append(("Services", None))
    return crumbs


def _render_user_page(request, template_name, active_nav="services", active_subnav="", breadcrumbs=None):
    account, redir = _require_user_or_redirect(request)
    if redir:
        return redir
    return render(
        request,
        template_name,
        _user_home_context(
            account,
            active_nav=active_nav,
            active_subnav=active_subnav,
            breadcrumbs=breadcrumbs,
        ),
    )


def _process_connection_application(request, account):
    existing = _get_user_connection(account)
    if existing and existing.status != ConnectionApplication.STATUS_REJECTED:
        messages.info(
            request,
            f"You already have an active connection request ({existing.application_id} - {existing.status}).",
        )
        return redirect("user_connection_status")

    Transformer.ensure_defaults()
    form = ConnectionApplicationForm(request.POST)
    if form.is_valid():
        app = form.save(commit=False)
        app.user = account
        app.transformer = None
        last_id = ConnectionApplication.objects.order_by("-id").values_list("id", flat=True).first() or 0
        seq = last_id + 1001
        while ConnectionApplication.objects.filter(application_id=f"REQ-{seq}").exists():
            seq += 1
        year = timezone.localdate().strftime("%Y")
        app.application_id = f"REQ-{seq}"
        app.application_number = f"APP-{year}-{seq}"
        while ConnectionApplication.objects.filter(application_number=app.application_number).exists():
            seq += 1
            app.application_id = f"REQ-{seq}"
            app.application_number = f"APP-{year}-{seq}"
        app.connection_fee_amount = get_connection_fee_breakdown(app.property_type)["total"]
        app.status = ConnectionApplication.STATUS_APPLIED
        app.save()
        messages.success(
            request,
            f"Application {app.application_number} (Request ID {app.application_id}) created! "
            f"Please pay Connection Charges (Rs {app.connection_fee_amount}) via DISCOM Gateway to verify payment and download Application PDF.",
        )
        return redirect("user_home")

    return render(
        request,
        "users/new_connection.html",
        _user_home_context(
            account,
            conn_form=form,
            active_nav="services",
            active_subnav="new_connection",
            breadcrumbs=_new_connection_breadcrumbs(),
        ),
    )


@never_cache
def user_home(request):
    """Render users/userhome.html with connection summary, services links, and live simulation."""
    return _render_user_page(
        request,
        "users/userhome.html",
        active_nav="home",
        breadcrumbs=[("Home", None)],
    )


@never_cache
def user_services(request):
    """Render consumer Services directory page with Home > Services breadcrumb."""
    return _render_user_page(
        request,
        "users/services.html",
        breadcrumbs=_service_breadcrumbs(),
    )


@never_cache
def user_new_connection(request):
    """Render dedicated New Connection page with Home > Services > New Connection breadcrumb."""
    account, redir = _require_user_or_redirect(request)
    if redir:
        return redir
    if request.method == "POST":
        return _process_connection_application(request, account)
    return render(
        request,
        "users/new_connection.html",
        _user_home_context(
            account,
            active_nav="services",
            active_subnav="new_connection",
            breadcrumbs=_new_connection_breadcrumbs(),
        ),
    )


@never_cache
def user_connection_status(request):
    """Render dedicated Connection Status page with Home > Services > Connection Status breadcrumb."""
    return _render_user_page(
        request,
        "users/connection_status.html",
        active_subnav="connection_status",
        breadcrumbs=_service_breadcrumbs("Connection Status"),
    )


@never_cache
def user_power_simulation(request):
    """Render dedicated Power Simulation page with Home > Services > Power Simulation breadcrumb."""
    return _render_user_page(
        request,
        "users/power_simulation.html",
        active_subnav="power_simulation",
        breadcrumbs=_service_breadcrumbs("Power Simulation"),
    )


@never_cache
def user_account_profile(request):
    """Render dedicated Account Profile page with Home > Services > Account Profile breadcrumb."""
    return _render_user_page(
        request,
        "users/account_profile.html",
        active_subnav="account_profile",
        breadcrumbs=_service_breadcrumbs("Account Profile"),
    )


@require_POST
def apply_connection(request):
    """Submit a new electricity connection application for Department verification."""
    account, redir = _require_user_or_redirect(request)
    if redir:
        return redir
    return _process_connection_application(request, account)


@require_POST
def toggle_house_power(request):
    """Toggle the user's house power switch only after household connection and active transformer supply."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    account = _get_authenticated_user(request)
    if account is None:
        if wants_json:
            return JsonResponse({"error": "Authentication required."}, status=403)
        return redirect(f"{reverse('user_login')}?auth_required=1")

    action = request.POST.get("action", "").strip().lower()
    if action not in ("on", "off"):
        if wants_json:
            return JsonResponse({"error": "Invalid house power action."}, status=400)
        return redirect("user_home")

    connection = _get_user_connection(account)
    if not connection or not connection.is_connected:
        message = (
            "Power cannot reach your house yet. Complete the New Connection process "
            "(Department verification -> Admin Original Meter -> Department connection)."
        )
        if wants_json:
            return JsonResponse(
                {
                    "is_connected": False,
                    "connection_status": connection.status if connection else "None",
                    "house_power_on": account.house_power_on,
                    "transformer_power_on": False,
                    "effective_power_on": False,
                    "blocked": True,
                    "message": message,
                }
            )
        messages.warning(request, message)
        return redirect("user_home")

    transformer = connection.transformer
    if action == "on" and not transformer.is_power_on:
        message = (
            f"Transformer {transformer.code} Power Supply is currently OFF from Administration. "
            "House appliances cannot be energized until supply is restored."
        )
        if wants_json:
            return JsonResponse(
                {
                    "is_connected": True,
                    "connection_status": connection.status,
                    "house_power_on": account.house_power_on,
                    "transformer_power_on": False,
                    "individual_power_cut": connection.is_power_cut,
                    "power_cut_remark": connection.power_cut_remark,
                    "effective_power_on": False,
                    "blocked": True,
                    "message": message,
                }
            )
        messages.warning(request, message)
        return redirect("user_home")

    if action == "on" and connection.is_power_cut:
        remark_part = f" Remark: {connection.power_cut_remark}" if connection.power_cut_remark else ""
        message = (
            f"Individual power supply is currently CUT for your {connection.property_type} unit "
            f"(Request ID {connection.application_id}).{remark_part}"
        )
        if wants_json:
            return JsonResponse(
                {
                    "is_connected": True,
                    "connection_status": connection.status,
                    "house_power_on": account.house_power_on,
                    "transformer_power_on": transformer.is_power_on,
                    "individual_power_cut": True,
                    "power_cut_remark": connection.power_cut_remark,
                    "effective_power_on": False,
                    "blocked": True,
                    "message": message,
                }
            )
        messages.warning(request, message)
        return redirect("user_home")

    if action == "on" and connection.is_commercial and connection.prepaid_units_balance <= Decimal("0.00"):
        message = (
            f"Allocated Prepaid Units exhausted (0.00 Units remaining for Meter {connection.meter_number}). "
            "Pay a bill on Services -> Power Billing to allocate units before switching ON."
        )
        if wants_json:
            return JsonResponse(
                {
                    "is_connected": True,
                    "connection_status": connection.status,
                    "house_power_on": account.house_power_on,
                    "transformer_power_on": transformer.is_power_on,
                    "individual_power_cut": connection.is_power_cut,
                    "prepaid_units_exhausted": True,
                    "prepaid_units_balance": 0.0,
                    "power_cut_remark": connection.power_cut_remark,
                    "effective_power_on": False,
                    "blocked": True,
                    "message": message,
                }
            )
        messages.warning(request, message)
        return redirect("user_home")

    sync_elapsed_appliance_usage(connection.id)
    connection.refresh_from_db()
    account.refresh_from_db()

    account.house_power_on = action == "on"
    account.save(update_fields=["house_power_on", "updated_at"])

    exhausted = bool(connection.is_commercial and connection.prepaid_units_balance <= Decimal("0.00"))
    effective_on = (
        transformer.is_power_on
        and (not connection.is_power_cut)
        and (not exhausted)
        and account.house_power_on
    )
    message = (
        f"House power is ON (Meter {connection.meter_number}, Transformer {transformer.code})."
        if effective_on
        else "House main switch turned OFF."
    )
    if wants_json:
        return JsonResponse(
            {
                "is_connected": True,
                "connection_status": connection.status,
                "house_power_on": account.house_power_on,
                "transformer_power_on": transformer.is_power_on,
                "individual_power_cut": connection.is_power_cut,
                "prepaid_units_exhausted": exhausted,
                "prepaid_units_balance": float(connection.prepaid_units_balance),
                "power_cut_remark": connection.power_cut_remark,
                "effective_power_on": effective_on,
                "blocked": False,
                "telemetry": get_connection_appliance_telemetry(connection),
                "message": message,
            }
        )
    return redirect("user_home")


def _serialize_appliances(account):
    return [
        {
            "id": item.id,
            "name": item.name,
            "appliance_type": item.appliance_type,
            "wattage": item.wattage,
            "is_on": item.is_on,
        }
        for item in UserAppliance.objects.filter(user=account)
    ]


@require_POST
def add_appliance(request):
    """Add a new custom electronic appliance to the user's house/commercial simulation."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    account = _get_authenticated_user(request)
    if account is None:
        if wants_json:
            return JsonResponse({"error": "Authentication required."}, status=403)
        return redirect(f"{reverse('user_login')}?auth_required=1")

    form = UserApplianceForm(request.POST)
    if form.is_valid():
        conn = _get_user_connection(account)
        if conn and conn.is_connected:
            sync_elapsed_appliance_usage(conn.id)
            conn.refresh_from_db()
        appliance = form.save(commit=False)
        appliance.user = account
        appliance.is_on = True
        appliance.save()
        if wants_json:
            return JsonResponse(
                {
                    "ok": True,
                    "appliance": {
                        "id": appliance.id,
                        "name": appliance.name,
                        "appliance_type": appliance.appliance_type,
                        "wattage": appliance.wattage,
                        "is_on": appliance.is_on,
                    },
                    "appliances": _serialize_appliances(account),
                    "telemetry": get_connection_appliance_telemetry(conn),
                }
            )
        messages.success(request, f"Added '{appliance.name}' ({appliance.wattage} W) to simulation.")
        return redirect("user_home")

    if wants_json:
        first_error = next(iter(form.errors.values()))[0] if form.errors else "Invalid appliance details."
        return JsonResponse({"ok": False, "error": first_error, "errors": form.errors}, status=400)
    messages.error(request, "Please enter a valid appliance name and wattage.")
    return redirect("user_home")


@require_POST
def toggle_appliance_state(request, app_id):
    """Toggle a user-added appliance ON or OFF (respecting grid and house power state)."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    account = _get_authenticated_user(request)
    if account is None:
        if wants_json:
            return JsonResponse({"error": "Authentication required."}, status=403)
        return redirect(f"{reverse('user_login')}?auth_required=1")

    appliance = get_object_or_404(UserAppliance, pk=app_id, user=account)
    connection = _get_user_connection(account)
    if not connection or not connection.is_connected:
        msg = "Connection is not completed yet. Power cannot reach your appliances."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg, "appliances": _serialize_appliances(account)})
        messages.warning(request, msg)
        return redirect("user_home")

    if not connection.transformer.is_power_on:
        msg = f"Transformer {connection.transformer.code} Power Supply is currently OFF."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg, "appliances": _serialize_appliances(account)})
        messages.warning(request, msg)
        return redirect("user_home")

    if connection.is_power_cut:
        remark_part = f" Remark: {connection.power_cut_remark}" if connection.power_cut_remark else ""
        msg = f"Power supply is individually CUT for your {connection.property_type} connection.{remark_part}"
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg, "appliances": _serialize_appliances(account)})
        messages.warning(request, msg)
        return redirect("user_home")

    if connection.is_commercial and connection.prepaid_units_balance <= Decimal("0.00"):
        msg = "Allocated Prepaid Units exhausted (0.00 Units). Pay a Commercial bill to recharge units."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg, "appliances": _serialize_appliances(account)})
        messages.warning(request, msg)
        return redirect("user_home")

    if not account.house_power_on:
        msg = "Turn ON Main Power first to operate appliances."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg, "appliances": _serialize_appliances(account)})
        messages.warning(request, msg)
        return redirect("user_home")

    sync_elapsed_appliance_usage(connection.id)
    connection.refresh_from_db()
    appliance.is_on = not appliance.is_on
    appliance.save(update_fields=["is_on"])
    if wants_json:
        return JsonResponse(
            {
                "ok": True,
                "blocked": False,
                "appliance": {
                    "id": appliance.id,
                    "name": appliance.name,
                    "appliance_type": appliance.appliance_type,
                    "wattage": appliance.wattage,
                    "is_on": appliance.is_on,
                },
                "appliances": _serialize_appliances(account),
                "telemetry": get_connection_appliance_telemetry(connection),
            }
        )
    return redirect("user_home")


@require_POST
def toggle_builtin_appliance(request, app_key):
    """Persist ON/OFF state of built-in appliances in DB so simulation & billing stay synced."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    account = _get_authenticated_user(request)
    if account is None:
        if wants_json:
            return JsonResponse({"error": "Authentication required."}, status=403)
        return redirect(f"{reverse('user_login')}?auth_required=1")

    if app_key not in DEFAULT_BUILTIN_STATES:
        if wants_json:
            return JsonResponse({"ok": False, "error": "Unknown appliance key."}, status=400)
        return redirect("user_home")

    connection = _get_user_connection(account)
    if not connection or not connection.is_connected:
        msg = "Connection is not completed yet. Power cannot reach your appliances."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg})
        messages.warning(request, msg)
        return redirect("user_home")

    if not connection.transformer.is_power_on:
        msg = f"Transformer {connection.transformer.code} Power Supply is currently OFF."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg})
        messages.warning(request, msg)
        return redirect("user_home")

    if connection.is_power_cut:
        remark_part = f" Remark: {connection.power_cut_remark}" if connection.power_cut_remark else ""
        msg = f"Power supply is individually CUT for your {connection.property_type} connection.{remark_part}"
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg})
        messages.warning(request, msg)
        return redirect("user_home")

    if connection.is_commercial and connection.prepaid_units_balance <= Decimal("0.00"):
        msg = "Allocated Prepaid Units exhausted (0.00 Units). Pay a Commercial bill to recharge units."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg})
        messages.warning(request, msg)
        return redirect("user_home")

    if not account.house_power_on:
        msg = "Turn ON Main Power first to operate appliances."
        if wants_json:
            return JsonResponse({"ok": False, "blocked": True, "message": msg})
        messages.warning(request, msg)
        return redirect("user_home")

    sync_elapsed_appliance_usage(connection.id)
    connection.refresh_from_db()
    states = get_builtin_appliance_states(connection)
    states[app_key] = not states.get(app_key, True)
    connection.appliance_states = states
    connection.save(update_fields=["appliance_states", "updated_at"])
    telemetry = get_connection_appliance_telemetry(connection)
    if wants_json:
        return JsonResponse(
            {
                "ok": True,
                "blocked": False,
                "key": app_key,
                "is_on": states[app_key],
                "builtin_states": states,
                "telemetry": telemetry,
            }
        )
    return redirect("user_home")


@require_POST
def delete_appliance(request, app_id):
    """Remove a custom appliance from the user's simulation."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    account = _get_authenticated_user(request)
    if account is None:
        if wants_json:
            return JsonResponse({"error": "Authentication required."}, status=403)
        return redirect(f"{reverse('user_login')}?auth_required=1")

    appliance = get_object_or_404(UserAppliance, pk=app_id, user=account)
    conn = _get_user_connection(account)
    if conn and conn.is_connected:
        sync_elapsed_appliance_usage(conn.id)
        conn.refresh_from_db()
    name = appliance.name
    appliance.delete()
    if wants_json:
        return JsonResponse(
            {
                "ok": True,
                "appliances": _serialize_appliances(account),
                "telemetry": get_connection_appliance_telemetry(conn),
            }
        )
    messages.success(request, f"Removed '{name}' from simulation.")
    return redirect("user_home")


@never_cache
def user_billing(request):
    """Meter number bill lookup, Commercial prepaid unit allocation, and Domestic monthly bills."""
    account, redir = _require_user_or_redirect(request)
    if redir:
        return redir

    ctx = _user_home_context(
        account,
        active_nav="services",
        active_subnav="billing",
        breadcrumbs=_service_breadcrumbs("Power Billing"),
    )

    entered_meter = request.GET.get("meter_number", "").strip().upper()
    lookup_error = None
    lookup_summary = None

    if entered_meter:
        matched = (
            ConnectionApplication.objects.filter(meter_number__iexact=entered_meter)
            .select_related("transformer", "user")
            .prefetch_related("bills")
            .first()
        )
        if matched is None:
            lookup_error = f"No electricity connection found for Meter Number '{entered_meter}'."
        elif matched.user_id != account.id:
            lookup_error = f"Meter Number '{entered_meter}' does not belong to your consumer account."
        else:
            if matched.is_connected:
                sync_elapsed_appliance_usage(matched.id)
                matched.refresh_from_db()
            lookup_summary = build_meter_billing_summary(matched)

    ctx["entered_meter"] = entered_meter
    ctx["lookup_error"] = lookup_error
    ctx["lookup_summary"] = lookup_summary
    return render(request, "users/billing.html", ctx)


@require_POST
def user_pay_bill(request):
    """Process Commercial bulk recharge or Domestic monthly payment via Dummy Payment Gateway."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    account = _get_authenticated_user(request)
    if account is None:
        if wants_json:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=403)
        return redirect(f"{reverse('user_login')}?auth_required=1")

    meter_number = request.POST.get("meter_number", "").strip().upper()
    pay_kind = request.POST.get("pay_kind", "").strip().lower()
    payment_method = request.POST.get("payment_method", "UPI").strip()
    simulate_outcome = request.POST.get("simulate_outcome", "success").strip().lower()
    connection = _get_user_connection(account)

    def _fail(msg, target_url=None):
        if wants_json:
            return JsonResponse({"ok": False, "error": msg, "gateway_status": "Failed"}, status=400)
        messages.error(request, msg)
        return redirect(target_url or "user_billing")

    if pay_kind in ("prepaid", "bulk_recharge"):
        if not connection or not connection.is_connected or not connection.meter_number:
            return _fail("You must have an active connected Meter Number before bulk recharging units.")
        if meter_number and meter_number != connection.meter_number.upper():
            return _fail("Entered Meter Number does not match your connected Commercial meter.")
        pack_code = request.POST.get("pack_code", "").strip()
        bulk_units = request.POST.get("bulk_units", "").strip()
        amount_raw = request.POST.get("amount_paid", "").strip()
        try:
            bill, updated_conn = process_commercial_prepaid_payment(
                connection.id,
                amount_paid=amount_raw or None,
                bulk_units=bulk_units or None,
                pack_code=pack_code,
                payment_method=payment_method,
                simulate_outcome=simulate_outcome,
            )
        except ValueError as exc:
            return _fail(str(exc), f"{reverse('user_billing')}?meter_number={connection.meter_number}")

        success_msg = (
            f"Bulk Recharge Success ({bill.bill_number} • TXN {bill.transaction_id} via {bill.payment_method}): "
            f"Paid Rs {bill.total_amount} -> Credited {bill.units_billed} Bulk Units. "
            f"Balance: {updated_conn.prepaid_units_balance} Units!"
        )
        if wants_json:
            return JsonResponse(
                {
                    "ok": True,
                    "gateway_status": "Success",
                    "billing_agency": "Electricity Department DISCOM Authorized Payment Desk",
                    "bill_number": bill.bill_number,
                    "transaction_id": bill.transaction_id,
                    "payment_method": bill.payment_method,
                    "pack_name": bill.bulk_pack_name,
                    "meter_number": updated_conn.meter_number,
                    "consumer_name": updated_conn.applicant_name,
                    "category": f"Commercial LT-II ({updated_conn.industry_type or 'General'})",
                    "substation": f"{updated_conn.transformer.code} - {updated_conn.transformer.substation_name}" if updated_conn.transformer else "--",
                    "base_fare": float(bill.base_fare),
                    "rate_per_unit": float(bill.rate_per_unit),
                    "energy_charge": float(bill.energy_charge),
                    "allocated_units": float(bill.units_billed),
                    "total_amount": float(bill.total_amount),
                    "prepaid_units_balance": float(updated_conn.prepaid_units_balance),
                    "paid_at_ist": timezone.localtime(bill.paid_at).strftime("%d %b %Y, %I:%M:%S %p IST"),
                    "message": success_msg,
                    "redirect_url": f"{reverse('user_billing')}?meter_number={updated_conn.meter_number}",
                }
            )
        messages.success(request, success_msg)
        return redirect(f"{reverse('user_billing')}?meter_number={updated_conn.meter_number}")

    if pay_kind == "commercial_bill":
        bill_id = request.POST.get("bill_id", "").strip()
        if not bill_id.isdigit():
            return _fail("Invalid Commercial published bill selected.")
        bill = get_object_or_404(PowerBill, pk=int(bill_id), connection__user=account)
        target_meter = meter_number or bill.connection.meter_number or ""
        try:
            paid_bill, updated_conn = pay_published_commercial_bill(
                bill.id,
                payment_method=payment_method,
                simulate_outcome=simulate_outcome,
            )
        except ValueError as exc:
            return _fail(str(exc), f"{reverse('user_billing')}?meter_number={target_meter}")

        success_msg = (
            f"Published Commercial Bill {paid_bill.bill_number} paid via {paid_bill.payment_method} "
            f"(TXN {paid_bill.transaction_id}) for Rs {paid_bill.total_amount}! "
            f"Credited +{paid_bill.units_billed} Units (Balance: {updated_conn.prepaid_units_balance} Units)."
        )
        if wants_json:
            return JsonResponse(
                {
                    "ok": True,
                    "gateway_status": "Success",
                    "billing_agency": "Electricity Department DISCOM Authorized Payment Desk",
                    "bill_number": paid_bill.bill_number,
                    "transaction_id": paid_bill.transaction_id,
                    "payment_method": paid_bill.payment_method,
                    "pack_name": paid_bill.bulk_pack_name,
                    "billing_month": paid_bill.billing_month,
                    "meter_number": target_meter,
                    "consumer_name": updated_conn.applicant_name,
                    "category": f"Commercial LT-II ({updated_conn.industry_type or 'General'})",
                    "substation": f"{updated_conn.transformer.code} - {updated_conn.transformer.substation_name}" if updated_conn.transformer else "--",
                    "base_fare": float(paid_bill.base_fare),
                    "rate_per_unit": float(paid_bill.rate_per_unit),
                    "energy_charge": float(paid_bill.energy_charge),
                    "allocated_units": float(paid_bill.units_billed),
                    "total_amount": float(paid_bill.total_amount),
                    "prepaid_units_balance": float(updated_conn.prepaid_units_balance),
                    "paid_at_ist": timezone.localtime(paid_bill.paid_at).strftime("%d %b %Y, %I:%M:%S %p IST"),
                    "message": success_msg,
                    "redirect_url": f"{reverse('user_billing')}?meter_number={target_meter}",
                }
            )
        messages.success(request, success_msg)
        return redirect(f"{reverse('user_billing')}?meter_number={target_meter}")

    if pay_kind == "monthly":
        bill_id = request.POST.get("bill_id", "").strip()
        if not bill_id.isdigit():
            return _fail("Invalid monthly bill selected.")
        bill = get_object_or_404(PowerBill, pk=int(bill_id), connection__user=account)
        target_meter = meter_number or bill.connection.meter_number or ""
        try:
            paid_bill = pay_domestic_monthly_bill(
                bill.id,
                payment_method=payment_method,
                simulate_outcome=simulate_outcome,
            )
        except ValueError as exc:
            return _fail(str(exc), f"{reverse('user_billing')}?meter_number={target_meter}")

        success_msg = (
            f"Monthly Bill {paid_bill.bill_number} paid via {paid_bill.payment_method} "
            f"(TXN {paid_bill.transaction_id}) for Rs {paid_bill.total_amount}!"
        )
        if wants_json:
            return JsonResponse(
                {
                    "ok": True,
                    "gateway_status": "Success",
                    "billing_agency": "Electricity Department DISCOM Authorized Payment Desk",
                    "bill_number": paid_bill.bill_number,
                    "transaction_id": paid_bill.transaction_id,
                    "payment_method": paid_bill.payment_method,
                    "billing_month": paid_bill.billing_month,
                    "meter_number": target_meter,
                    "consumer_name": paid_bill.connection.applicant_name,
                    "category": "Domestic LT-I Residential",
                    "substation": f"{paid_bill.connection.transformer.code} - {paid_bill.connection.transformer.substation_name}" if paid_bill.connection.transformer else "--",
                    "units_billed": float(paid_bill.units_billed),
                    "base_fare": float(paid_bill.base_fare),
                    "rate_per_unit": float(paid_bill.rate_per_unit),
                    "energy_charge": float(paid_bill.energy_charge),
                    "total_amount": float(paid_bill.total_amount),
                    "paid_at_ist": timezone.localtime(paid_bill.paid_at).strftime("%d %b %Y, %I:%M:%S %p IST"),
                    "message": success_msg,
                    "redirect_url": f"{reverse('user_billing')}?meter_number={target_meter}",
                }
            )
        messages.success(request, success_msg)
        return redirect(f"{reverse('user_billing')}?meter_number={target_meter}")

    return _fail("Invalid billing payment action.")


@require_POST
def record_power_usage_view(request):
    """Record live kWh consumption from simulation ticks and enforce Commercial unit limits."""
    account = _get_authenticated_user(request)
    if account is None:
        return JsonResponse({"error": "Authentication required."}, status=403)

    connection = _get_user_connection(account)
    if not connection or not connection.is_connected:
        return JsonResponse({"ok": False, "blocked": True, "effective_power_on": False})

    use_server_appliances = request.POST.get("use_server_appliances", "").strip().lower() in ("1", "true", "yes")
    raw_watts = request.POST.get("active_watts")
    if use_server_appliances or raw_watts is None or str(raw_watts).strip() == "":
        watts = None
    else:
        try:
            watts = Decimal(str(raw_watts))
        except (InvalidOperation, ValueError):
            watts = Decimal("0")
    try:
        hours = Decimal(str(request.POST.get("duration_hours", "0.05")))
    except (InvalidOperation, ValueError):
        hours = Decimal("0.05")

    result = record_power_usage(
        connection.id,
        account,
        watts,
        hours,
        use_server_appliances=use_server_appliances,
    )
    return JsonResponse(result)


@require_POST
def user_pay_connection_fee(request):
    """Pay New Connection charges via DISCOM Gateway to unlock PDF download & Payment Verification."""
    wants_json = (
        request.headers.get("x-requested-with") == "XMLHttpRequest"
        or "application/json" in request.headers.get("accept", "")
    )
    account = _get_authenticated_user(request)
    if account is None:
        if wants_json:
            return JsonResponse({"ok": False, "error": "Authentication required."}, status=403)
        return redirect(f"{reverse('user_login')}?auth_required=1")

    app_id = request.POST.get("app_id", "").strip()
    payment_method = request.POST.get("payment_method", "UPI").strip()
    simulate_outcome = request.POST.get("simulate_outcome", "success").strip().lower()
    if app_id.isdigit():
        conn = get_object_or_404(ConnectionApplication, pk=int(app_id), user=account)
    else:
        conn = _get_user_connection(account)
    if conn is None:
        msg = "No active connection application found."
        if wants_json:
            return JsonResponse({"ok": False, "error": msg, "gateway_status": "Failed"}, status=400)
        messages.error(request, msg)
        return redirect("user_new_connection")

    try:
        paid_app, fee = pay_connection_application_fee(
            conn.id,
            account,
            payment_method=payment_method,
            simulate_outcome=simulate_outcome,
        )
    except ValueError as exc:
        if wants_json:
            return JsonResponse({"ok": False, "error": str(exc), "gateway_status": "Failed"}, status=400)
        messages.error(request, str(exc))
        return redirect("user_connection_status")

    pdf_url = reverse("download_connection_pdf", args=[paid_app.id])
    success_msg = (
        f"New Connection Charges Paid (Rs {paid_app.connection_fee_amount}) for "
        f"Application {paid_app.application_number} / {paid_app.application_id}! "
        f"TXN: {paid_app.connection_transaction_id}. Official Application PDF is ready to download."
    )
    if wants_json:
        return JsonResponse(
            {
                "ok": True,
                "gateway_status": "Success",
                "billing_agency": "Electricity Department DISCOM New Connection Desk",
                "application_number": paid_app.application_number,
                "request_id": paid_app.application_id,
                "receipt_number": paid_app.connection_receipt_number,
                "transaction_id": paid_app.connection_transaction_id,
                "payment_method": paid_app.connection_payment_method,
                "consumer_name": paid_app.applicant_name,
                "category": f"{paid_app.property_type}" + (f" ({paid_app.industry_type})" if paid_app.industry_type else ""),
                "processing_fee": float(fee["processing_fee"]),
                "service_line_charge": float(fee["service_line_charge"]),
                "security_deposit": float(fee["security_deposit"]),
                "total_amount": float(paid_app.connection_fee_amount),
                "paid_at_ist": timezone.localtime(paid_app.connection_paid_at).strftime("%d %b %Y, %I:%M:%S %p IST"),
                "pdf_url": pdf_url,
                "redirect_url": reverse("user_connection_status"),
                "message": success_msg,
            }
        )
    messages.success(request, success_msg)
    return redirect("user_connection_status")


@never_cache
def download_connection_pdf(request, app_id):
    """Download official Application PDF with Application No., Request ID & Paid Connection Fee Receipt."""
    account, redir = _require_user_or_redirect(request)
    if redir:
        return redir

    app = get_object_or_404(ConnectionApplication, pk=app_id, user=account)
    if not app.connection_fee_paid:
        return HttpResponse(
            "Connection charges must be paid via DISCOM Gateway before downloading Application PDF.",
            status=403,
        )

    pdf_bytes = build_connection_application_pdf(app)
    app_no = app.application_number or app.application_id
    filename = f"Electricity_Application_{app_no}_{app.application_id}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@never_cache
def download_bill_invoice_pdf(request, bill_id):
    """Download official DISCOM Consumer Electricity Tax Invoice & Payment Receipt PDF."""
    bill = get_object_or_404(
        PowerBill.objects.select_related("connection__user", "connection__transformer"),
        pk=bill_id,
    )
    account = _get_authenticated_user(request)
    is_owner = bool(account and bill.connection and bill.connection.user_id == account.id)
    is_admin = bool(request.session.get("admin_logged_in") or request.session.get("dept_user"))
    if not (is_owner or is_admin):
        user_id = request.session.get("user_account_id")
        if not (user_id and bill.connection and bill.connection.user_id == user_id):
            messages.error(request, "Please log in to your consumer account to download this invoice.")
            return redirect("user_login")

    pdf_bytes = build_power_bill_invoice_pdf(bill)
    filename = f"DISCOM_Tax_Invoice_{bill.bill_number}.pdf"
    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


def user_logout(request):
    """Clear the user session and redirect to the public home page."""
    request.session.pop("user_account_id", None)
    request.session.pop("user_username", None)
    messages.success(request, "You have been logged out of your consumer account.")
    return redirect("home")
