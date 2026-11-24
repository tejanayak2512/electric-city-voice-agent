import logging
from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from admins.models import DepartmentEmployee, Transformer
from complaints.models import Complaint

logger = logging.getLogger(__name__)


def _is_admin(request):
    return bool(request.session.get("admin_logged_in"))


def _get_department_employee(request):
    emp_id = request.session.get("dept_emp_id")
    if emp_id:
        try:
            return DepartmentEmployee.objects.get(id=emp_id)
        except DepartmentEmployee.DoesNotExist:
            return None
    return None


def admin_complaints_list(request):
    """Admin portal view: Monitor all complaints filed via AI Voice Agent."""
    if not _is_admin(request):
        messages.error(request, "Please log in to access the Administration Portal.")
        return redirect("admin_login")

    qs = Complaint.objects.all().select_related("user_account", "connection")

    status_filter = request.GET.get("status", "").strip()
    priority_filter = request.GET.get("priority", "").strip()
    search = request.GET.get("search", "").strip()

    if status_filter:
        qs = qs.filter(status=status_filter)
    if priority_filter:
        qs = qs.filter(priority=priority_filter)
    if search:
        qs = qs.filter(
            Q(complaint_number__icontains=search)
            | Q(complaint_id__icontains=search)
            | Q(caller_name__icontains=search)
            | Q(location__icontains=search)
            | Q(service_number__icontains=search)
        )

    context = {
        "complaints": qs,
        "total_count": Complaint.objects.count(),
        "registered_count": Complaint.objects.filter(status=Complaint.STATUS_REGISTERED).count(),
        "in_progress_count": Complaint.objects.filter(status__in=[Complaint.STATUS_ASSIGNED, Complaint.STATUS_IN_PROGRESS]).count(),
        "resolved_count": Complaint.objects.filter(status=Complaint.STATUS_RESOLVED).count(),
        "critical_count": Complaint.objects.filter(priority=Complaint.PRIORITY_CRITICAL).count(),
        "status_filter": status_filter,
        "priority_filter": priority_filter,
        "search": search,
        "active_admin_nav": "connections",
        "active_admin_sub": "complaints",
    }
    return render(request, "admins/complaints.html", context)


def department_complaints_list(request):
    """Department portal view: View, dispatch line crews, and resolve complaints."""
    emp = _get_department_employee(request)
    if not emp:
        messages.error(request, "Please log in with Department Employee credentials.")
        return redirect("department_login")

    qs = Complaint.objects.all().select_related("user_account", "connection")

    status_filter = request.GET.get("status", "").strip()
    if status_filter:
        qs = qs.filter(status=status_filter)

    context = {
        "employee": emp,
        "complaints": qs,
        "status_filter": status_filter,
        "active_dept_nav": "operations",
        "active_dept_sub": "complaints",
    }
    return render(request, "department/complaints.html", context)


@require_POST
def department_process_complaint(request, comp_id):
    """Update complaint status by department staff (assign line team, mark in-progress or resolved)."""
    emp = _get_department_employee(request)
    if not emp:
        messages.error(request, "Unauthorized department action.")
        return redirect("department_login")

    complaint = get_object_or_404(Complaint, id=comp_id)
    action = request.POST.get("action", "").strip()
    assigned_team = request.POST.get("assigned_field_team", "").strip()
    notes = request.POST.get("department_notes", "").strip()

    if action == "assign":
        complaint.status = Complaint.STATUS_ASSIGNED
        complaint.assigned_field_team = assigned_team or f"Line Crew {emp.full_name}"
        if notes:
            complaint.department_notes = notes
        messages.success(request, f"Complaint {complaint.complaint_number} assigned to {complaint.assigned_field_team}.")
    elif action == "in_progress":
        complaint.status = Complaint.STATUS_IN_PROGRESS
        if notes:
            complaint.department_notes = notes
        messages.success(request, f"Complaint {complaint.complaint_number} is now marked In Progress.")
    elif action == "resolve":
        from django.utils import timezone
        complaint.status = Complaint.STATUS_RESOLVED
        complaint.resolved_at = timezone.now()
        complaint.estimated_resolution = "Resolved & Power Restored"
        if notes:
            complaint.department_notes = notes
        messages.success(request, f"Complaint {complaint.complaint_number} resolved successfully!")

    complaint.save()
    return redirect("department_complaints")
