from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from users.billing import (
    get_active_tariff,
    get_connection_appliance_telemetry,
    sync_elapsed_appliance_usage,
)
from users.models import ConnectionApplication

from .forms import DepartmentLoginForm
from .models import DepartmentEmployee, MaintenanceSchedule, Transformer
from .views import _overall_grid_summary, build_substation_context


def _get_active_employee(request):
    emp_id = request.session.get("dept_employee_id")
    if not emp_id:
        return None
    return (
        DepartmentEmployee.objects.select_related("assigned_transformer")
        .filter(pk=emp_id, is_active=True)
        .first()
    )


def _dept_shared_context(emp, active_dept_nav="home", active_dept_sub="", page_label=""):
    """Build shared navigation badges, published DISCOM tariffs, and breadcrumbs for Department pages."""
    transformers = list(Transformer.ensure_defaults())
    base_fare_rs, domestic_rate, commercial_rate, tariff_setting = get_active_tariff()
    breadcrumbs = [("Department Home", reverse("department_home"))]
    if page_label:
        breadcrumbs.append(("Operations", reverse("department_home")))
        breadcrumbs.append((page_label, None))
    else:
        breadcrumbs = [("Department Home", None)]

    return {
        "employee": emp,
        "transformers": transformers,
        "overall_grid": _overall_grid_summary(transformers),
        "base_fare_rs": base_fare_rs,
        "domestic_rate": domestic_rate,
        "commercial_rate": commercial_rate,
        "tariff_setting": tariff_setting,
        "pending_verification_count": ConnectionApplication.objects.filter(
            status=ConnectionApplication.STATUS_APPLIED
        ).count(),
        "ready_to_connect_count": ConnectionApplication.objects.filter(
            status=ConnectionApplication.STATUS_METER_ISSUED
        ).count(),
        "connected_count": ConnectionApplication.objects.filter(
            status=ConnectionApplication.STATUS_CONNECTED
        ).count(),
        "pending_maintenance_count": MaintenanceSchedule.objects.filter(
            status=MaintenanceSchedule.STATUS_PENDING
        ).count(),
        "approved_maintenance_count": MaintenanceSchedule.objects.filter(
            status=MaintenanceSchedule.STATUS_APPROVED
        ).count(),
        "active_dept_nav": active_dept_nav,
        "active_dept_sub": active_dept_sub,
        "dept_breadcrumbs": breadcrumbs,
    }


def _filter_and_paginate_connections(request, qs, default_per_page=5):
    """Apply search query, category/transformer/supply dropdown filters, and pagination."""
    q = request.GET.get("q", "").strip()
    property_type = request.GET.get("property_type", "").strip()
    transformer_id = request.GET.get("transformer_id", "").strip()
    supply_status = request.GET.get("supply_status", "").strip()
    payment_status = request.GET.get("payment_status", "").strip()
    per_page_raw = request.GET.get("per_page", str(default_per_page)).strip()

    try:
        per_page = int(per_page_raw)
    except ValueError:
        per_page = default_per_page
    if per_page not in (5, 10, 20, 50):
        per_page = default_per_page

    if q:
        qs = qs.filter(
            Q(application_id__icontains=q)
            | Q(application_number__icontains=q)
            | Q(connection_transaction_id__icontains=q)
            | Q(applicant_name__icontains=q)
            | Q(user__username__icontains=q)
            | Q(meter_number__icontains=q)
            | Q(house_address__icontains=q)
            | Q(industry_type__icontains=q)
        )
    if property_type in (
        ConnectionApplication.PROPERTY_DOMESTIC,
        ConnectionApplication.PROPERTY_COMMERCIAL,
    ):
        qs = qs.filter(property_type=property_type)

    if transformer_id and transformer_id.isdigit():
        qs = qs.filter(transformer_id=int(transformer_id))

    if supply_status == "live":
        qs = qs.filter(is_power_cut=False, transformer__is_power_on=True)
    elif supply_status == "power_cut":
        qs = qs.filter(is_power_cut=True)
    elif supply_status == "grid_off":
        qs = qs.filter(is_power_cut=False, transformer__is_power_on=False)

    if payment_status == "unpaid":
        qs = qs.filter(connection_fee_paid=False)
    elif payment_status == "paid_unverified":
        qs = qs.filter(connection_fee_paid=True, payment_verified=False)
    elif payment_status == "verified":
        qs = qs.filter(connection_fee_paid=True, payment_verified=True)

    qs = qs.order_by("-created_at", "-id")
    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))

    query_params = request.GET.copy()
    query_params.pop("page", None)
    return {
        "page_obj": page_obj,
        "search_q": q,
        "selected_property_type": property_type,
        "selected_transformer_filter": transformer_id,
        "selected_supply_status": supply_status,
        "selected_payment_status": payment_status,
        "per_page": per_page,
        "per_page_choices": (5, 10, 20, 50),
        "query_no_page": query_params.urlencode(),
    }


@require_POST
def department_verify_employee_id(request):
    """Step 1 of 2FA Department login: verify active Employee ID before showing Step 2 credentials."""
    emp_id = request.POST.get("employee_id", "").strip().upper()
    if not emp_id:
        return JsonResponse(
            {"verified": False, "error": "Please enter your Employee ID (e.g. DEPT-001)."},
            status=400,
        )

    emp = DepartmentEmployee.objects.filter(employee_id__iexact=emp_id).first()
    if emp is None:
        return JsonResponse(
            {"verified": False, "error": f"No department employee found with ID '{emp_id}'."},
            status=404,
        )

    if not emp.is_active:
        return JsonResponse(
            {
                "verified": False,
                "error": "Access Denied: Your department account is currently deactivated by Administration.",
            },
            status=403,
        )

    request.session["dept_2fa_verified_emp_id"] = emp.id
    return JsonResponse(
        {
            "verified": True,
            "employee_id": emp.employee_id,
            "full_name": emp.full_name,
            "designation": emp.designation,
        }
    )


def department_login(request):
    """2FA Login: verify Employee ID + active Username/Password and redirect to department_home."""
    if request.method == "POST":
        form = DepartmentLoginForm(request.POST)
        if form.is_valid():
            emp = form.authenticated_employee
            request.session.pop("dept_2fa_verified_emp_id", None)
            request.session["dept_employee_id"] = emp.id
            request.session["dept_employee_username"] = emp.username
            return redirect("department_home")

        entered_emp_id = request.POST.get("employee_id", "").strip().upper()
        verified_emp = (
            DepartmentEmployee.objects.filter(
                employee_id__iexact=entered_emp_id, is_active=True
            ).first()
            if entered_emp_id
            else None
        )
        return render(
            request,
            "base.html",
            {
                "dept_login_form": form,
                "open_dept_login_modal": True,
                "entered_dept_emp_id": entered_emp_id,
                "entered_dept_username": request.POST.get("username", "").strip(),
                "dept_step2_ready": verified_emp is not None,
                "dept_verified_emp": verified_emp,
            },
        )

    context = {
        "dept_login_form": DepartmentLoginForm(),
        "open_dept_login_modal": True,
    }
    if request.GET.get("auth_required") == "1":
        context["dept_login_auth_error"] = (
            "Please complete 2FA sign-in with an active Department Employee account."
        )
    return render(request, "base.html", context)


@never_cache
def department_home(request):
    """Department Overview Hub with Operations dropdown navigation, live tariffs, and live usage summary."""
    emp = _get_active_employee(request)
    if emp is None:
        request.session.pop("dept_employee_id", None)
        request.session.pop("dept_employee_username", None)
        return redirect(f"{reverse('department_login')}?auth_required=1")

    live_recent = list(
        ConnectionApplication.objects.filter(status=ConnectionApplication.STATUS_CONNECTED)
        .select_related("user", "transformer")
        .order_by("-updated_at")[:5]
    )
    for app in live_recent:
        sync_elapsed_appliance_usage(app.id)
        app.refresh_from_db()
        app.live_telemetry = get_connection_appliance_telemetry(app)
    return render(
        request,
        "department/department_home.html",
        {
            **_dept_shared_context(emp, active_dept_nav="home"),
            "live_recent_connections": live_recent,
        },
    )


@never_cache
def department_verify_requests(request):
    """Dedicated Step 1 page: Search, filter, paginate & assign Substation/Transformer to new requests."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    shared = _dept_shared_context(
        emp,
        active_dept_nav="operations",
        active_dept_sub="verify_requests",
        page_label="01. Verify & Assign Requests",
    )
    substation_ctx = build_substation_context(shared["transformers"])
    qs = ConnectionApplication.objects.filter(
        status=ConnectionApplication.STATUS_APPLIED
    ).select_related("user", "transformer")
    filter_ctx = _filter_and_paginate_connections(request, qs, default_per_page=5)

    return render(
        request,
        "department/verify_requests.html",
        {
            **shared,
            **substation_ctx,
            **filter_ctx,
        },
    )


@never_cache
def department_install_meters(request):
    """Dedicated Step 2 page: Search, filter, paginate & physically install Admin-issued meters."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    shared = _dept_shared_context(
        emp,
        active_dept_nav="operations",
        active_dept_sub="install_meters",
        page_label="02. Install Approved Meters",
    )
    qs = ConnectionApplication.objects.filter(
        status=ConnectionApplication.STATUS_METER_ISSUED
    ).select_related("user", "transformer", "verified_by")
    filter_ctx = _filter_and_paginate_connections(request, qs, default_per_page=5)

    return render(
        request,
        "department/install_meters.html",
        {
            **shared,
            **filter_ctx,
        },
    )


@never_cache
def department_substations(request):
    """Dedicated Step 3 page: Substations, Step-Down Transformers SCADA & Allocated Meters."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    shared = _dept_shared_context(
        emp,
        active_dept_nav="operations",
        active_dept_sub="substations",
        page_label="03. Substations & Meters Bay",
    )
    transformers = shared["transformers"]
    selected_id = request.GET.get("transformer")
    selected_transformer = None
    if selected_id and selected_id.isdigit():
        selected_transformer = next((t for t in transformers if t.id == int(selected_id)), None)
    if selected_transformer is None:
        selected_transformer = emp.assigned_transformer or (transformers[0] if transformers else None)

    substation_ctx = build_substation_context(transformers, selected_transformer)
    return render(
        request,
        "department/substations.html",
        {
            **shared,
            **substation_ctx,
            "can_control_transformers": False,
        },
    )


@never_cache
def department_connected_premises(request):
    """Dedicated Step 4 page: Paginated, searchable & live-updating Connected Premises usage table."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    shared = _dept_shared_context(
        emp,
        active_dept_nav="operations",
        active_dept_sub="connected_premises",
        page_label="04. Connected Premises & Live Usage",
    )
    qs = ConnectionApplication.objects.filter(
        status=ConnectionApplication.STATUS_CONNECTED
    ).select_related("user", "transformer", "connected_by")
    filter_ctx = _filter_and_paginate_connections(request, qs, default_per_page=10)
    for app in filter_ctx["page_obj"]:
        sync_elapsed_appliance_usage(app.id)
        app.refresh_from_db()
        app.live_telemetry = get_connection_appliance_telemetry(app)

    return render(
        request,
        "department/connected_premises.html",
        {
            **shared,
            **filter_ctx,
        },
    )


@never_cache
def department_live_usage_api(request):
    """JSON live usage feed for Department portal (active tariffs + live consumer usage telemetry)."""
    emp = _get_active_employee(request)
    if emp is None:
        return JsonResponse({"error": "Department authentication required."}, status=403)

    base_fare_rs, domestic_rate, commercial_rate, tariff_setting = get_active_tariff()
    qs = ConnectionApplication.objects.filter(
        status=ConnectionApplication.STATUS_CONNECTED
    ).select_related("user", "transformer")

    ids_param = request.GET.get("ids", "").strip()
    if ids_param:
        id_list = [int(x) for x in ids_param.split(",") if x.strip().isdigit()]
        if id_list:
            qs = qs.filter(id__in=id_list)
    else:
        qs = qs.order_by("-updated_at")[:10]

    items = []
    for app in qs:
        sync_elapsed_appliance_usage(app.id)
        app.refresh_from_db()
        if app.is_power_cut:
            supply_state = "Individual Power Cut"
        elif not app.transformer or not app.transformer.is_power_on:
            supply_state = "Transformer OFF"
        elif app.is_commercial and app.prepaid_units_balance <= 0:
            supply_state = "Bulk Units Exhausted"
        else:
            supply_state = "Connected • Live"

        rate = commercial_rate if app.is_commercial else domestic_rate
        telemetry = get_connection_appliance_telemetry(app, rate_per_unit=rate)
        items.append(
            {
                "id": app.id,
                "request_id": app.application_id,
                "meter_number": app.meter_number,
                "applicant_name": app.applicant_name,
                "property_type": app.property_type,
                "industry_type": app.industry_type,
                "transformer_code": app.transformer.code if app.transformer else "--",
                "supply_state": supply_state,
                "is_power_cut": app.is_power_cut,
                "transformer_on": bool(app.transformer and app.transformer.is_power_on),
                "unbilled_units": float(app.unbilled_units),
                "prepaid_units_balance": float(app.prepaid_units_balance),
                "total_units_consumed": float(app.total_units_consumed),
                "active_watts": telemetry["active_watts"],
                "active_kw": telemetry["active_kw"],
                "on_appliances_count": telemetry["on_count"],
                "total_appliances_count": telemetry["total_count"],
                "est_runtime_text": telemetry["est_runtime_text"],
                "power_cut_remark": app.power_cut_remark or "Normal grid supply",
            }
        )

    return JsonResponse(
        {
            "ok": True,
            "tariff": {
                "base_fare_rs": float(base_fare_rs),
                "domestic_rate": float(domestic_rate),
                "commercial_rate": float(commercial_rate),
                "revision_note": tariff_setting.revision_note,
            },
            "items": items,
        }
    )


@require_POST
def department_verify_payment(request, app_id):
    """Verification 1: Verify applicant's paid New Connection Charges & Gateway TXN before transformer assignment."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    next_url = reverse("department_verify_requests")
    app = get_object_or_404(ConnectionApplication, pk=app_id)
    if not app.connection_fee_paid:
        messages.error(
            request,
            f"Cannot verify payment: Applicant for {app.application_id} has not paid the Rs {app.connection_fee_amount} connection charges yet.",
        )
        return redirect(next_url)

    remarks = request.POST.get("payment_verification_remarks", "").strip()
    app.payment_verified = True
    app.payment_verified_by = f"Dept: {emp.full_name} ({emp.employee_id})"
    app.payment_verified_at = timezone.now()
    app.payment_verification_remarks = remarks or (
        f"Verified Rs {app.connection_fee_amount} receipt {app.connection_receipt_number} ({app.connection_transaction_id})"
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
        f"Verification 1 Completed: Payment verified for {app.application_number or app.application_id} "
        f"(TXN {app.connection_transaction_id}). Now select Substation/Transformer to forward to Administration.",
    )
    return redirect(next_url)


@require_POST
def department_verify_connection(request, app_id):
    """Verification 2: Inspect paid/verified application, align Substation/Transformer & forward to Admin (or reject)."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    next_url = reverse("department_verify_requests")
    app = get_object_or_404(ConnectionApplication, pk=app_id)
    if app.status != ConnectionApplication.STATUS_APPLIED:
        messages.error(request, "Only newly applied connections can be verified.")
        return redirect(next_url)

    action = request.POST.get("action", "verify").strip().lower()
    remarks = request.POST.get("department_remarks", "").strip()
    transformer_id = request.POST.get("transformer_id")

    if action == "reject":
        app.status = ConnectionApplication.STATUS_REJECTED
        app.department_remarks = remarks or f"Rejected by Department ({emp.employee_id})."
        app.verified_by = emp
        app.verified_at = timezone.now()
        app.save()
        messages.warning(request, f"Request ID {app.application_id} rejected.")
        return redirect(next_url)

    if not app.connection_fee_paid:
        messages.error(
            request,
            f"Cannot verify & assign transformer for {app.application_id}: New Connection Charges (Rs {app.connection_fee_amount}) have not been paid yet.",
        )
        return redirect(next_url)

    if not app.payment_verified:
        messages.error(
            request,
            f"Complete Verification 1 (Verify Paid Connection Charges TXN) for {app.application_id} before assigning a Substation/Transformer.",
        )
        return redirect(next_url)

    selected = (
        Transformer.objects.filter(pk=int(transformer_id)).first()
        if transformer_id and str(transformer_id).isdigit()
        else None
    )
    if selected is None:
        messages.error(
            request,
            f"Please choose a Substation & Transformer for Request ID {app.application_id} before forwarding to Administration.",
        )
        return redirect(next_url)

    app.transformer = selected
    app.status = ConnectionApplication.STATUS_VERIFIED
    app.department_remarks = (
        remarks
        or f"Verified by {emp.full_name} ({emp.employee_id}) & aligned to {selected.code} ({selected.substation_name})."
    )
    app.verified_by = emp
    app.verified_at = timezone.now()
    app.save()

    messages.success(
        request,
        f"Request ID {app.application_id} aligned to {selected.code} ({selected.substation_name}) and sent to Administration for Meter Approval.",
    )
    return redirect(next_url)


@require_POST
def department_connect_household(request, app_id):
    """Install the Admin-issued original meter and connect the user's premises to the transformer."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    next_url = reverse("department_install_meters")
    app = get_object_or_404(ConnectionApplication, pk=app_id)
    if app.status != ConnectionApplication.STATUS_METER_ISSUED or not app.meter_number:
        messages.error(
            request,
            "Cannot connect premises: Administration must cross-check and issue an Original Meter first.",
        )
        return redirect(next_url)

    app.status = ConnectionApplication.STATUS_CONNECTED
    app.connected_by = emp
    app.connected_at = timezone.now()
    app.save(update_fields=["status", "connected_by", "connected_at", "updated_at"])

    messages.success(
        request,
        f"Request ID {app.application_id} connected! Meter {app.meter_number} is now live on "
        f"{app.transformer.code} ({app.transformer.substation_name}).",
    )
    return redirect(next_url)


@never_cache
def department_maintenance(request):
    """Dedicated Step 5 page: Submit Maintenance Schedules for Admin Power-Cut Permission & track statuses."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    shared = _dept_shared_context(
        emp,
        active_dept_nav="operations",
        active_dept_sub="maintenance",
        page_label="05. Maintenance Schedule & Cut Requests",
    )

    if request.method == "POST":
        scope = request.POST.get("scope", MaintenanceSchedule.SCOPE_TRANSFORMER).strip()
        transformer_id = request.POST.get("transformer_id", "").strip()
        target_conn_id = request.POST.get("target_connection_id", "").strip()
        title = request.POST.get("title", "").strip()
        reason = request.POST.get("reason", "").strip()
        start_raw = request.POST.get("scheduled_start", "").strip()
        end_raw = request.POST.get("scheduled_end", "").strip()

        if not title or not reason:
            messages.error(request, "Please enter both a Maintenance Title and Work Reason.")
            return redirect("department_maintenance")

        start_dt = parse_datetime(start_raw) if start_raw else None
        end_dt = parse_datetime(end_raw) if end_raw else None
        if start_dt is None or end_dt is None:
            messages.error(request, "Please choose valid Scheduled Start and End date/time values.")
            return redirect("department_maintenance")
        if timezone.is_naive(start_dt):
            start_dt = timezone.make_aware(start_dt)
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)
        if end_dt <= start_dt:
            messages.error(request, "Scheduled End time must be after Scheduled Start time.")
            return redirect("department_maintenance")

        tf = None
        target_conn = None
        if scope == MaintenanceSchedule.SCOPE_PREMISE:
            if not target_conn_id.isdigit():
                messages.error(request, "Please choose the target connected Home / Industry premise.")
                return redirect("department_maintenance")
            target_conn = get_object_or_404(
                ConnectionApplication,
                pk=int(target_conn_id),
                status=ConnectionApplication.STATUS_CONNECTED,
            )
            tf = target_conn.transformer
        elif scope == MaintenanceSchedule.SCOPE_TRANSFORMER:
            if not transformer_id.isdigit():
                messages.error(request, "Please choose the Substation & Transformer Feeder for maintenance.")
                return redirect("department_maintenance")
            tf = get_object_or_404(Transformer, pk=int(transformer_id))
        else:
            scope = MaintenanceSchedule.SCOPE_ALL

        last_id = MaintenanceSchedule.objects.order_by("-id").values_list("id", flat=True).first() or 0
        seq = last_id + 1001
        while MaintenanceSchedule.objects.filter(schedule_id=f"MNT-{seq}").exists():
            seq += 1

        sched = MaintenanceSchedule.objects.create(
            schedule_id=f"MNT-{seq}",
            requested_by=emp,
            scope=scope,
            transformer=tf,
            target_connection=target_conn,
            title=title,
            reason=reason,
            scheduled_start=start_dt,
            scheduled_end=end_dt,
            status=MaintenanceSchedule.STATUS_PENDING,
        )
        messages.success(
            request,
            f"Maintenance Schedule {sched.schedule_id} sent to Administration for Power Cut Permission approval.",
        )
        return redirect("department_maintenance")

    q = request.GET.get("q", "").strip()
    status_filter = request.GET.get("status", "").strip()
    scope_filter = request.GET.get("scope", "").strip()
    tf_filter = request.GET.get("transformer_id", "").strip()
    per_page_raw = request.GET.get("per_page", "5").strip()
    try:
        per_page = int(per_page_raw)
    except ValueError:
        per_page = 5
    if per_page not in (5, 10, 20, 50):
        per_page = 5

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
        )
    if status_filter:
        qs = qs.filter(status=status_filter)
    if scope_filter in (
        MaintenanceSchedule.SCOPE_TRANSFORMER,
        MaintenanceSchedule.SCOPE_PREMISE,
        MaintenanceSchedule.SCOPE_ALL,
    ):
        qs = qs.filter(scope=scope_filter)
    if tf_filter and tf_filter.isdigit():
        qs = qs.filter(transformer_id=int(tf_filter))

    paginator = Paginator(qs, per_page)
    page_obj = paginator.get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)

    now_local = timezone.localtime()
    default_start = now_local.strftime("%Y-%m-%dT%H:%M")
    default_end = (now_local + timezone.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")

    return render(
        request,
        "department/maintenance.html",
        {
            **shared,
            "connected_choices": ConnectionApplication.objects.filter(
                status=ConnectionApplication.STATUS_CONNECTED
            ).select_related("transformer"),
            "status_choices": MaintenanceSchedule.STATUS_CHOICES,
            "scope_choices": MaintenanceSchedule.SCOPE_CHOICES,
            "page_obj": page_obj,
            "search_q": q,
            "selected_status": status_filter,
            "selected_scope": scope_filter,
            "selected_transformer_filter": tf_filter,
            "per_page": per_page,
            "per_page_choices": (5, 10, 20, 50),
            "query_no_page": params.urlencode(),
            "default_start": default_start,
            "default_end": default_end,
        },
    )


@never_cache
@require_POST
def department_process_maintenance(request, sched_id):
    """Department staff executes power cut on Approved maintenance schedules, and restores power upon completion."""
    emp = _get_active_employee(request)
    if emp is None:
        return redirect(f"{reverse('department_login')}?auth_required=1")

    sched = get_object_or_404(
        MaintenanceSchedule.objects.select_related("transformer", "target_connection"),
        pk=sched_id,
    )
    action = request.POST.get("action", "").strip().lower()
    remarks = request.POST.get("dept_remarks", "").strip() or request.POST.get("admin_remarks", "").strip()

    if action == "execute_cut":
        if sched.status != MaintenanceSchedule.STATUS_APPROVED:
            messages.error(
                request,
                f"Cannot cut power: Maintenance Schedule {sched.schedule_id} must have Approved permission from Administration first (Current status: {sched.status}).",
            )
            return redirect("department_maintenance")

        now = timezone.now()
        cut_note = remarks or f"Approved Department Maintenance {sched.schedule_id} executed by {emp.full_name} ({emp.employee_id}): {sched.title}"
        if sched.scope == MaintenanceSchedule.SCOPE_ALL:
            for tf in Transformer.objects.all():
                tf.set_power(False)
            target_desc = "All Grid Transformers"
        elif sched.scope == MaintenanceSchedule.SCOPE_PREMISE and sched.target_connection:
            conn = sched.target_connection
            conn.is_power_cut = True
            conn.power_cut_remark = cut_note
            conn.power_cut_updated_at = now
            conn.save(update_fields=["is_power_cut", "power_cut_remark", "power_cut_updated_at", "updated_at"])
            target_desc = f"Premise {conn.meter_number} ({conn.application_id})"
        elif sched.transformer:
            sched.transformer.set_power(False)
            target_desc = f"Transformer {sched.transformer.code} ({sched.transformer.substation_name})"
        else:
            messages.error(request, "No valid transformer or premise target found for maintenance power cut.")
            return redirect("department_maintenance")

        sched.status = MaintenanceSchedule.STATUS_ACTIVE_CUT
        sched.cut_executed_at = now
        if remarks:
            sched.admin_remarks = f"{sched.admin_remarks} | Dept Cut: {remarks}".strip(" |")
        sched.save()
        messages.success(
            request,
            f"Department Power Cut executed successfully on {target_desc} for Maintenance Schedule {sched.schedule_id}.",
        )
        return redirect("department_maintenance")

    if action == "complete_restore":
        if sched.status != MaintenanceSchedule.STATUS_ACTIVE_CUT:
            messages.error(
                request,
                f"Maintenance {sched.schedule_id} does not currently have an active power cut to restore.",
            )
            return redirect("department_maintenance")

        now = timezone.now()
        restore_note = remarks or f"Department Maintenance {sched.schedule_id} completed by {emp.full_name} ({emp.employee_id}) — power restored"
        if sched.scope == MaintenanceSchedule.SCOPE_ALL:
            for tf in Transformer.objects.all():
                tf.set_power(True)
            target_desc = "All Grid Transformers"
        elif sched.scope == MaintenanceSchedule.SCOPE_PREMISE and sched.target_connection:
            conn = sched.target_connection
            conn.is_power_cut = False
            conn.power_cut_remark = restore_note
            conn.power_cut_updated_at = now
            conn.save(update_fields=["is_power_cut", "power_cut_remark", "power_cut_updated_at", "updated_at"])
            target_desc = f"Premise {conn.meter_number} ({conn.application_id})"
        elif sched.transformer:
            sched.transformer.set_power(True)
            target_desc = f"Transformer {sched.transformer.code}"
        else:
            target_desc = sched.schedule_id

        sched.status = MaintenanceSchedule.STATUS_COMPLETED
        sched.restored_at = now
        if remarks:
            sched.admin_remarks = f"{sched.admin_remarks} | Dept Restored: {remarks}".strip(" |")
        sched.save()
        messages.success(
            request,
            f"Department Maintenance {sched.schedule_id} marked Completed and power restored on {target_desc}.",
        )
        return redirect("department_maintenance")

    messages.error(request, "Invalid department maintenance action.")
    return redirect("department_maintenance")


def department_logout(request):
    """Clear the department employee session and redirect home."""
    request.session.pop("dept_employee_id", None)
    request.session.pop("dept_employee_username", None)
    request.session.pop("dept_2fa_verified_emp_id", None)
    messages.success(request, "You have been logged out from the Department Portal.")
    return redirect("home")
