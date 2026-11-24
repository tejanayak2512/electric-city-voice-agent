from decimal import Decimal
from django.test import Client, TestCase
from django.urls import reverse

from admins.models import DepartmentEmployee, Transformer
from complaints.models import Complaint
from users.models import UserAccount


class ComplaintPortalViewsTests(TestCase):
    """Test suite for Admin and Department complaints supervision and dispatch."""

    def setUp(self):
        self.client = Client()

        self.user = UserAccount.objects.create(
            full_name="Ananya Sharma",
            username="ananya",
            email="ananya@test.com",
            mobile_number="9123456789",
            status=UserAccount.STATUS_ACTIVE,
        )

        self.tf = Transformer.objects.create(
            code="T-COMP",
            name="Downtown Transformer",
            substation_name="Downtown Substation",
            zone="Central Zone",
            capacity_kva=500,
        )

        self.employee = DepartmentEmployee.objects.create(
            employee_id="EMP-2026-9999",
            full_name="Officer Suresh Line Incharge",
            username="suresh9999",
            mobile_number="9876500000",
            email="suresh@discom.gov.in",
            designation="Field Engineer",
            assigned_transformer=self.tf,
            is_active=True,
        )

        self.complaint = Complaint.objects.create(
            caller_name="Ananya Sharma",
            location="MG Road, Cross 4",
            issue_type=Complaint.ISSUE_POWER_OUTAGE,
            user_account=self.user,
            status=Complaint.STATUS_REGISTERED,
            priority=Complaint.PRIORITY_NORMAL,
        )

    def test_admin_complaints_list_requires_login_and_renders(self):
        """Admin complaints list requires admin session and renders metrics."""
        # Unauthenticated redirect
        res1 = self.client.get(reverse("admin_complaints"))
        self.assertEqual(res1.status_code, 302)

        # Authenticated Admin
        session = self.client.session
        session["admin_logged_in"] = True
        session.save()

        res2 = self.client.get(reverse("admin_complaints"))
        self.assertEqual(res2.status_code, 200)
        self.assertContains(res2, self.complaint.complaint_number)
        self.assertContains(res2, self.complaint.complaint_id)
        self.assertContains(res2, "Total Grievances")

    def test_department_complaints_list_and_status_update_workflow(self):
        """Department staff can view, assign crew, and resolve complaints."""
        session = self.client.session
        session["dept_emp_id"] = self.employee.id
        session.save()

        # View complaints list
        res = self.client.get(reverse("department_complaints"))
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, self.complaint.complaint_number)

        # 1. Assign Crew
        res_assign = self.client.post(
            reverse("department_process_complaint", args=[self.complaint.id]),
            data={"action": "assign", "assigned_field_team": "Line Unit Alpha"},
        )
        self.assertEqual(res_assign.status_code, 302)
        self.complaint.refresh_from_db()
        self.assertEqual(self.complaint.status, Complaint.STATUS_ASSIGNED)
        self.assertEqual(self.complaint.assigned_field_team, "Line Unit Alpha")

        # 2. Mark In Progress
        res_prog = self.client.post(
            reverse("department_process_complaint", args=[self.complaint.id]),
            data={"action": "in_progress"},
        )
        self.assertEqual(res_prog.status_code, 302)
        self.complaint.refresh_from_db()
        self.assertEqual(self.complaint.status, Complaint.STATUS_IN_PROGRESS)

        # 3. Mark Resolved
        res_res = self.client.post(
            reverse("department_process_complaint", args=[self.complaint.id]),
            data={"action": "resolve"},
        )
        self.assertEqual(res_res.status_code, 302)
        self.complaint.refresh_from_db()
        self.assertEqual(self.complaint.status, Complaint.STATUS_RESOLVED)
        self.assertIsNotNone(self.complaint.resolved_at)
