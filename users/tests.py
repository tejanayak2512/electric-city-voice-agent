from decimal import Decimal
from io import BytesIO

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone
from pypdf import PdfReader

from admins.models import (
    DepartmentEmployee,
    MaintenanceSchedule,
    OperationMaintenanceNotification,
    SiteMediaSetting,
    SiteNewsAnnouncement,
    TariffSetting,
    Transformer,
)
from llm_config.models import LLMConfiguration
from llm_config.services import call_llm, get_active_llm_config, test_llm_connection

from .forms import ConnectionApplicationForm, CreateAccountForm, UserApplianceForm
from .models import ConnectionApplication, PowerBill, UserAccount, UserAppliance


class UserPortalTests(TestCase):
    """End-to-end tests for Services dropdown, Domestic/Commercial simulations, 2FA Dept login, and workflow."""

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.valid_data = {
            "full_name": "Ravi Kumar",
            "username": "ravikumar1",
            "email": "ravi@example.com",
            "mobile_number": "9876543210",
            "password": "Pass1234",
            "confirm_password": "Pass1234",
        }

    def _get_csrf(self, client=None, url="/"):
        c = client or self.client
        resp = c.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.cookies["csrftoken"].value, resp.content.decode("utf-8")

    def _login_admin(self):
        admin_client = Client(enforce_csrf_checks=True)
        csrf, _ = self._get_csrf(admin_client, "/")
        resp = admin_client.post(
            reverse("admin_login"),
            data={"username": "admin", "password": "admin", "csrfmiddlewaretoken": csrf},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        return admin_client, resp

    def test_base_contains_dropdowns_modals_and_strength_checker(self):
        _, html = self._get_csrf()
        self.assertIn('class="nav-dropdown"', html)
        self.assertIn('class="nav-dropdown-menu"', html)
        self.assertIn('id="adminLoginModal"', html)
        self.assertIn('id="createAccountModal"', html)
        self.assertIn('id="userLoginModal"', html)
        self.assertIn('id="departmentLoginModal"', html)
        self.assertIn('id="passwordStrengthMeter"', html)
        self.assertIn("Department 2FA Login", html)
        self.assertIn(reverse("department_verify_employee_id"), html)

    def test_create_account_and_userhome_services_dropdown(self):
        csrf, _ = self._get_csrf()
        resp = self.client.post(
            reverse("create_account"),
            data={**self.valid_data, "csrfmiddlewaretoken": csrf},
            follow=True,
        )
        self.assertEqual(resp.redirect_chain, [(reverse("user_login"), 302)])

        csrf2 = self.client.cookies["csrftoken"].value
        home_resp = self.client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": csrf2},
            follow=True,
        )
        self.assertEqual(home_resp.redirect_chain, [(reverse("user_home"), 302)])
        html = home_resp.content.decode("utf-8")
        self.assertIn("Welcome, Ravi Kumar!", html)
        self.assertIn('id="userServicesDropdown"', html)
        self.assertIn('id="navNewConnectionBtn"', html)
        self.assertIn(reverse("user_services"), html)
        self.assertIn(reverse("user_new_connection"), html)
        self.assertIn(reverse("user_connection_status"), html)
        self.assertIn(reverse("user_power_simulation"), html)
        self.assertIn(reverse("user_account_profile"), html)
        self.assertIn('id="pageBreadcrumb"', html)
        self.assertIn('id="domesticSvg"', html)
        self.assertIn("Power Not Connected", html)

    def test_connection_form_commercial_requires_industry_and_renders_commercial_sim(self):
        Transformer.ensure_defaults()
        user_form = CreateAccountForm(data=self.valid_data)
        self.assertTrue(user_form.is_valid())
        user_form.save()

        # Commercial without industry_type must fail validation; user form must not expose transformer or kW
        bad_comm = ConnectionApplicationForm(
            data={
                "applicant_name": "Ravi Kumar",
                "mobile_number": "9876543210",
                "house_address": "Industrial Plot 12, Phase 2, Hyderabad",
                "property_type": "Commercial",
                "industry_type": "",
                "id_proof_number": "GSTIN36ABCDE1234",
            }
        )
        self.assertFalse(bad_comm.is_valid())
        self.assertIn("industry_type", bad_comm.errors)
        self.assertNotIn("requested_load_kw", bad_comm.fields)
        self.assertNotIn("transformer", bad_comm.fields)

        # Login user and verify New Connection page does not offer transformer dropdown
        csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": csrf},
            follow=True,
        )
        new_conn_html = self.client.get(reverse("user_new_connection")).content.decode("utf-8")
        self.assertNotIn('name="transformer"', new_conn_html)

        # Invalid commercial POST re-renders dedicated New Connection page with validation error & breadcrumb
        bad_post = self.client.post(
            reverse("apply_connection"),
            data={
                "applicant_name": "Ravi Kumar",
                "mobile_number": "9876543210",
                "house_address": "Industrial Plot 12, Phase 2, Hyderabad",
                "property_type": "Commercial",
                "industry_type": "",
                "id_proof_number": "GSTIN36ABCDE1234",
                "csrfmiddlewaretoken": self.client.cookies["csrftoken"].value,
            },
        )
        self.assertEqual(bad_post.status_code, 200)
        bad_html = bad_post.content.decode("utf-8")
        self.assertIn('id="applyConnectionForm"', bad_html)
        self.assertIn("Please specify the industry type for a Commercial connection.", bad_html)
        self.assertIn("New Connection", bad_html)

        # Valid commercial POST generates REQ- ID, leaves transformer unassigned for Dept, and renders Commercial SVG
        good_post = self.client.post(
            reverse("apply_connection"),
            data={
                "applicant_name": "Ravi Kumar",
                "mobile_number": "9876543210",
                "house_address": "Industrial Plot 12, Phase 2, Hyderabad",
                "property_type": "Commercial",
                "industry_type": "Manufacturing / Factory",
                "id_proof_number": "GSTIN36ABCDE1234",
                "csrfmiddlewaretoken": self.client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        self.assertEqual(good_post.status_code, 200)
        app = ConnectionApplication.objects.get(applicant_name="Ravi Kumar")
        self.assertTrue(app.application_id.startswith("REQ-"))
        self.assertIsNone(app.transformer)
        self.assertEqual(app.requested_load_kw, 15)
        comm_html = good_post.content.decode("utf-8")
        self.assertNotIn('name="requested_load_kw"', comm_html)
        self.assertIn(app.application_id, comm_html)
        self.assertIn(app.application_number, comm_html)
        self.assertEqual(app.connection_fee_amount, Decimal("1500.00"))
        self.assertIn("Pay Rs 1500.00", comm_html)
        self.assertIn('id="commercialSvg"', comm_html)
        self.assertIn("COMMERCIAL: MANUFACTURING / FACTORY", comm_html)
        self.assertIn("SCADA Workstations", comm_html)
        self.assertIn("Central HVAC Chiller", comm_html)
        self.assertIn("Industry Plant Unit", comm_html)
        # Still unpowered until Dept assigns transformer -> Admin approves -> Dept connects
        self.assertIn("Power Not Connected", comm_html)

        # Request ID is visible across all user pages where the connection status/profile goes
        for url_name in ("user_connection_status", "user_power_simulation", "user_account_profile", "user_new_connection"):
            self.assertIn(app.application_id, self.client.get(reverse(url_name)).content.decode("utf-8"))
        self.assertIn("Central HVAC Chiller", comm_html)
        self.assertIn("Industry Plant Unit", comm_html)
        # Still unpowered until Dept verifies -> Admin issues meter -> Dept connects
        self.assertIn("Power Not Connected", comm_html)

    def test_department_2fa_employee_id_and_credentials_login(self):
        admin_client, _ = self._login_admin()
        t2 = Transformer.objects.get(code="T-02")

        admin_client.post(
            reverse("admin_employees"),
            data={
                "full_name": "Suresh Rao",
                "username": "suresh_dept",
                "email": "suresh@dept.gov",
                "mobile_number": "9988776655",
                "designation": "Field Engineer",
                "assigned_transformer": t2.id,
                "password": "DeptPass1",
                "confirm_password": "DeptPass1",
                "is_active": "on",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        emp = DepartmentEmployee.objects.get(username="suresh_dept")

        dept_client = Client(enforce_csrf_checks=True)
        dept_csrf, _ = self._get_csrf(dept_client, "/")

        # Factor 1: Wrong Employee ID rejected
        bad_id = dept_client.post(
            reverse("department_verify_employee_id"),
            data={"employee_id": "DEPT-999", "csrfmiddlewaretoken": dept_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(bad_id.status_code, 404)
        self.assertFalse(bad_id.json()["verified"])

        # Factor 1: Valid active Employee ID verified
        good_id = dept_client.post(
            reverse("department_verify_employee_id"),
            data={"employee_id": emp.employee_id, "csrfmiddlewaretoken": dept_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(good_id.status_code, 200)
        self.assertTrue(good_id.json()["verified"])
        self.assertEqual(good_id.json()["full_name"], "Suresh Rao")

        # Factor 2: Mismatched Employee ID + Username rejected
        bad_2fa = dept_client.post(
            reverse("department_login"),
            data={
                "employee_id": "DEPT-999",
                "username": "suresh_dept",
                "password": "DeptPass1",
                "csrfmiddlewaretoken": dept_csrf,
            },
        )
        self.assertIn("Invalid Employee ID", bad_2fa.content.decode("utf-8"))

        # Factor 2: Valid 2FA succeeds
        good_2fa = dept_client.post(
            reverse("department_login"),
            data={
                "employee_id": emp.employee_id,
                "username": "suresh_dept",
                "password": "DeptPass1",
                "csrfmiddlewaretoken": dept_csrf,
            },
            follow=True,
        )
        self.assertEqual(good_2fa.redirect_chain, [(reverse("department_home"), 302)])
        self.assertIn("Welcome, Suresh Rao!", good_2fa.content.decode("utf-8"))

        # Admin disables employee -> Factor 1 & Factor 2 both denied
        admin_client.post(
            reverse("toggle_employee_status", args=[emp.id]),
            data={"csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value},
            follow=True,
        )
        deactivated_id = dept_client.post(
            reverse("department_verify_employee_id"),
            data={"employee_id": emp.employee_id, "csrfmiddlewaretoken": dept_client.cookies["csrftoken"].value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(deactivated_id.status_code, 403)
        self.assertIn("deactivated", deactivated_id.json()["error"])

    def test_full_connection_workflow_and_power_isolation(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        t2 = Transformer.objects.get(code="T-02")

        emp = DepartmentEmployee(
            employee_id="DEPT-001",
            full_name="Kiran Engineer",
            username="kiran_dept",
            email="kiran@dept.gov",
            mobile_number="9123456780",
            designation="Meter Inspector",
            assigned_transformer=t2,
            is_active=True,
        )
        emp.set_password("DeptPass1")
        emp.save()

        user_form = CreateAccountForm(data=self.valid_data)
        self.assertTrue(user_form.is_valid())
        user_form.save()

        admin_client, _ = self._login_admin()

        user_client = Client(enforce_csrf_checks=True)
        u_csrf, _ = self._get_csrf(user_client, "/")
        user_client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": u_csrf},
            follow=True,
        )

        # User applies for Domestic connection without choosing transformer
        user_client.post(
            reverse("apply_connection"),
            data={
                "applicant_name": "Ravi Kumar",
                "mobile_number": "9876543210",
                "house_address": "Plot 45, Madhapur Main Road, Hyderabad",
                "property_type": "Domestic",
                "industry_type": "",
                "id_proof_number": "AADHAAR998877",
                "csrfmiddlewaretoken": user_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        app = ConnectionApplication.objects.get(applicant_name="Ravi Kumar")
        self.assertTrue(app.application_id.startswith("REQ-"))
        self.assertTrue(app.application_number.startswith("APP-"))
        self.assertIsNone(app.transformer)
        self.assertEqual(app.property_type, "Domestic")
        self.assertEqual(app.requested_load_kw, 2)
        self.assertEqual(app.connection_fee_amount, Decimal("500.00"))
        self.assertFalse(app.connection_fee_paid)
        self.assertFalse(app.payment_verified)

        # Cannot download Application PDF before paying connection charges
        locked_pdf = user_client.get(reverse("download_connection_pdf", args=[app.id]))
        self.assertEqual(locked_pdf.status_code, 403)

        # Department logs in via 2FA; sees Request ID + realistic substations/transformers bay
        dept_client = Client(enforce_csrf_checks=True)
        d_csrf, _ = self._get_csrf(dept_client, "/")
        dept_login_resp = dept_client.post(
            reverse("department_login"),
            data={
                "employee_id": "DEPT-001",
                "username": "kiran_dept",
                "password": "DeptPass1",
                "csrfmiddlewaretoken": d_csrf,
            },
            follow=True,
        )
        dept_html = dept_login_resp.content.decode("utf-8")
        self.assertIn("deptOperationsDropdown", dept_html)
        verify_page_html = dept_client.get(reverse("department_verify_requests")).content.decode("utf-8")
        self.assertIn(app.application_id, verify_page_html)
        self.assertIn(app.application_number, verify_page_html)
        sub_page_html = dept_client.get(reverse("department_substations")).content.decode("utf-8")
        self.assertIn('id="substationRealisticSvg"', sub_page_html)
        self.assertIn('id="svgAllocatedMetersBay"', sub_page_html)
        self.assertIn("Central 33/11 kV Grid Substation", sub_page_html)

        # Department cannot forward before consumer pays New Connection Charges
        unpaid_fwd = dept_client.post(
            reverse("department_verify_connection", args=[app.id]),
            data={
                "action": "verify",
                "transformer_id": t2.id,
                "department_remarks": "Attempt before payment",
                "csrfmiddlewaretoken": dept_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ConnectionApplication.STATUS_APPLIED)
        self.assertIn("have not been paid yet", unpaid_fwd.content.decode("utf-8"))

        # Simulated bank decline on Connection Fee Gateway leaves fee unpaid
        fail_fee = user_client.post(
            reverse("user_pay_connection_fee"),
            data={
                "app_id": app.id,
                "payment_method": "UPI",
                "simulate_outcome": "failed",
                "csrfmiddlewaretoken": user_client.cookies["csrftoken"].value,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(fail_fee.status_code, 400)
        app.refresh_from_db()
        self.assertFalse(app.connection_fee_paid)

        # User pays New Connection Charges (Rs 500.00) via DISCOM Gateway
        paid_fee = user_client.post(
            reverse("user_pay_connection_fee"),
            data={
                "app_id": app.id,
                "payment_method": "Credit / Debit Card",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": user_client.cookies["csrftoken"].value,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(paid_fee.status_code, 200)
        paid_json = paid_fee.json()
        self.assertTrue(paid_json["ok"])
        self.assertTrue(paid_json["transaction_id"].startswith("TXN-"))
        self.assertTrue(paid_json["receipt_number"].startswith("CONNFEE-"))
        self.assertEqual(paid_json["application_number"], app.application_number)
        self.assertEqual(paid_json["request_id"], app.application_id)
        app.refresh_from_db()
        self.assertTrue(app.connection_fee_paid)

        # After payment, User downloads official Application PDF containing Application No., Request ID & TXN ID
        pdf_resp = user_client.get(reverse("download_connection_pdf", args=[app.id]))
        self.assertEqual(pdf_resp.status_code, 200)
        self.assertEqual(pdf_resp["Content-Type"], "application/pdf")
        reader = PdfReader(BytesIO(pdf_resp.content))
        pdf_text = "\n".join((page.extract_text() or "") for page in reader.pages)
        self.assertIn(app.application_number, pdf_text)
        self.assertIn(app.application_id, pdf_text)
        self.assertIn("Ravi Kumar", pdf_text)
        self.assertIn("500.00", pdf_text)
        self.assertIn(app.connection_transaction_id, pdf_text)

        # Verification 1 Gate: Department still cannot forward until Paid Charges are verified!
        unverified_pay_fwd = dept_client.post(
            reverse("department_verify_connection", args=[app.id]),
            data={
                "action": "verify",
                "transformer_id": t2.id,
                "department_remarks": "Attempt before Verification 1",
                "csrfmiddlewaretoken": dept_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ConnectionApplication.STATUS_APPLIED)
        self.assertIn("Complete Verification 1", unverified_pay_fwd.content.decode("utf-8"))

        # Department completes Verification 1 (Verify Paid Charges TXN)
        dept_client.post(
            reverse("department_verify_payment", args=[app.id]),
            data={
                "payment_verification_remarks": "Verified DISCOM receipt & bank TXN",
                "csrfmiddlewaretoken": dept_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        app.refresh_from_db()
        self.assertTrue(app.payment_verified)

        # Verification 2: Department still cannot forward without choosing a Substation/Transformer
        no_tf_resp = dept_client.post(
            reverse("department_verify_connection", args=[app.id]),
            data={
                "action": "verify",
                "transformer_id": "",
                "department_remarks": "Missing feeder choice",
                "csrfmiddlewaretoken": dept_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ConnectionApplication.STATUS_APPLIED)
        self.assertIn("Please choose a Substation", no_tf_resp.content.decode("utf-8"))

        # Department selects T-02 based on connection and sends to Administration
        dept_client.post(
            reverse("department_verify_connection", args=[app.id]),
            data={
                "action": "verify",
                "transformer_id": t2.id,
                "department_remarks": "Pole feasibility verified",
                "csrfmiddlewaretoken": dept_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        app.refresh_from_db()
        self.assertEqual(app.transformer_id, t2.id)
        self.assertEqual(app.status, ConnectionApplication.STATUS_VERIFIED)

        # Admin connections page displays Request ID & aligned Department transformer
        admin_conn_html = admin_client.get(reverse("admin_connections")).content.decode("utf-8")
        self.assertIn(app.application_id, admin_conn_html)
        self.assertIn("Aligned to T-02", admin_conn_html)

        # Admin cannot override aligned transformer to T-01
        mismatch_resp = admin_client.post(
            reverse("admin_process_connection", args=[app.id]),
            data={
                "action": "approve",
                "transformer_id": t1.id,
                "meter_number": "MTR-2026-9001",
                "original_meter_serial": "ORIG-PM-9001",
                "original_meter_verified": "on",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        app.refresh_from_db()
        self.assertEqual(app.status, ConnectionApplication.STATUS_VERIFIED)
        self.assertIn("Department-aligned transformer", mismatch_resp.content.decode("utf-8"))

        # Admin approves strictly to aligned T-02 & issues Meter Number + Original Power Meter
        admin_client.post(
            reverse("admin_process_connection", args=[app.id]),
            data={
                "action": "approve",
                "transformer_id": t2.id,
                "meter_number": "MTR-2026-9001",
                "original_meter_serial": "ORIG-PM-9001",
                "original_meter_verified": "on",
                "admin_remarks": "Original meter issued",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )

        # Power still blocked before physical household installation
        still_blocked = user_client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": user_client.cookies["csrftoken"].value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(still_blocked.json()["blocked"])

        # Department connects household to aligned T-02
        dept_client.post(
            reverse("department_connect_household", args=[app.id]),
            data={"csrfmiddlewaretoken": dept_client.cookies["csrftoken"].value},
            follow=True,
        )

        # Substation API & Admin/Dept views show allocated & connected meter M-TR-2026-9001 and Request ID under T-02
        t2_api = admin_client.get(f"{reverse('power_status_api')}?transformer_id={t2.id}").json()
        self.assertEqual(t2_api["substation_name"], "Central 33/11 kV Grid Substation")
        self.assertEqual(t2_api["allocated_count"], 1)
        self.assertEqual(t2_api["connected_count"], 1)
        self.assertEqual(t2_api["meters"][0]["request_id"], app.application_id)
        self.assertEqual(t2_api["meters"][0]["meter_number"], "MTR-2026-9001")
        self.assertIn("MTR-2026-9001", dept_client.get(reverse("department_connected_premises")).content.decode("utf-8"))
        self.assertIn("MTR-2026-9001", dept_client.get(reverse("department_home")).content.decode("utf-8"))
        self.assertIn(app.application_id, admin_client.get(f"{reverse('admin_home')}?transformer={t2.id}").content.decode("utf-8"))

        # User receives power from T-02; turning T-01 OFF leaves T-02 active
        live_on = user_client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": user_client.cookies["csrftoken"].value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(live_on.json()["effective_power_on"])

        admin_client.post(
            reverse("toggle_power_supply"),
            data={
                "action": "off",
                "transformer_id": t1.id,
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(user_client.get(reverse("power_status_api")).json()["is_power_on"])

        admin_client.post(
            reverse("toggle_power_supply"),
            data={
                "action": "off",
                "transformer_id": t2.id,
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertFalse(user_client.get(reverse("power_status_api")).json()["is_power_on"])

    def test_add_toggle_and_delete_custom_appliances_in_simulation(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        user_form = CreateAccountForm(data=self.valid_data)
        self.assertTrue(user_form.is_valid())
        account = user_form.save()

        # Connect user directly so we can test live appliance toggling
        ConnectionApplication.objects.create(
            user=account,
            application_id="CONN-8888",
            applicant_name="Ravi Kumar",
            mobile_number="9876543210",
            house_address="10 Main Road, Hyderabad",
            property_type="Domestic",
            requested_load_kw="4.0",
            id_proof_number="AADHAAR112233",
            transformer=t1,
            meter_number="MTR-8888",
            original_meter_serial="ORIG-8888",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        # Validation check on UserApplianceForm
        bad_app = UserApplianceForm(data={"name": "A", "appliance_type": "Fan"})
        self.assertFalse(bad_app.is_valid())
        self.assertIn("name", bad_app.errors)
        self.assertNotIn("wattage", bad_app.fields)

        csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": csrf},
            follow=True,
        )

        # Add custom appliances via AJAX
        u_csrf = self.client.cookies["csrftoken"].value
        add_resp = self.client.post(
            reverse("add_appliance"),
            data={
                "name": "Washing Machine",
                "appliance_type": "Motor",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(add_resp.status_code, 200)
        self.assertEqual(add_resp.json()["appliance"]["wattage"], 650)
        home_resp = self.client.get(reverse("user_home"))
        self.assertNotIn('name="wattage"', home_resp.content.decode("utf-8"))
        self.assertEqual(add_resp.status_code, 200)
        add_json = add_resp.json()
        self.assertTrue(add_json["ok"])
        self.assertEqual(len(add_json["appliances"]), 1)
        app_id = add_json["appliance"]["id"]

        # Page contains customAppliancesLayer, initialCustomAppliances JSON, Add Appliance modal, and highlighted navbar Meter No
        home_html = self.client.get(reverse("user_home")).content.decode("utf-8")
        self.assertIn('id="customAppliancesLayer"', home_html)
        self.assertIn('id="addApplianceModal"', home_html)
        self.assertIn('id="navMeterPill"', home_html)
        self.assertIn("MTR-8888", home_html)
        self.assertIn("Washing Machine", home_html)

        # Toggle custom appliance OFF and ON while connected & powered
        tog_off = self.client.post(
            reverse("toggle_appliance_state", args=[app_id]),
            data={"csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertFalse(tog_off["appliance"]["is_on"])

        tog_on = self.client.post(
            reverse("toggle_appliance_state", args=[app_id]),
            data={"csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(tog_on["appliance"]["is_on"])

        # Turn Transformer T-01 OFF -> toggling custom appliance is blocked
        t1.set_power(False)
        blocked_tog = self.client.post(
            reverse("toggle_appliance_state", args=[app_id]),
            data={"csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(blocked_tog["blocked"])

        # Delete custom appliance
        del_resp = self.client.post(
            reverse("delete_appliance", args=[app_id]),
            data={"csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(del_resp["ok"])
        self.assertEqual(len(del_resp["appliances"]), 0)
        self.assertFalse(UserAppliance.objects.filter(pk=app_id).exists())

    def test_user_services_redirect_pages_share_navbar_and_breadcrumbs(self):
        user_form = CreateAccountForm(data=self.valid_data)
        self.assertTrue(user_form.is_valid())
        user_form.save()

        # Unauthenticated requests to services pages redirect to user login
        anon_resp = self.client.get(reverse("user_new_connection"))
        self.assertEqual(anon_resp.status_code, 302)
        self.assertIn(reverse("user_login"), anon_resp.url)

        csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": csrf},
            follow=True,
        )

        pages = [
            ("user_services", ["Home", "Services"], 'id="userServicesToggle"'),
            ("user_new_connection", ["Home", "Services", "New Connection"], 'id="applyConnectionForm"'),
            ("user_connection_status", ["Home", "Services", "Connection Status"], 'id="connectionPanel"'),
            ("user_power_simulation", ["Home", "Services", "Power Simulation"], 'id="houseSimPanel"'),
            ("user_billing", ["Home", "Services", "Power Billing"], 'id="meterLookupForm"'),
            ("user_account_profile", ["Home", "Services", "Account Profile"], 'id="profilePanel"'),
        ]
        for url_name, expected_crumbs, marker in pages:
            resp = self.client.get(reverse(url_name))
            self.assertEqual(resp.status_code, 200)
            html = resp.content.decode("utf-8")
            # Shared navbar present on every page
            self.assertIn('class="user-navbar"', html)
            self.assertIn('id="userServicesDropdown"', html)
            self.assertIn('id="navMeterPill"', html)
            self.assertIn(reverse("user_logout"), html)
            # Breadcrumb trail rendered in order with > separators
            self.assertIn('id="pageBreadcrumb"', html)
            crumb_block = html.split('id="pageBreadcrumb"')[1].split("</nav>")[0]
            last_pos = -1
            for crumb in expected_crumbs:
                pos = crumb_block.find(crumb, last_pos + 1)
                self.assertGreater(pos, last_pos, f"Missing crumb '{crumb}' on {url_name}")
                last_pos = pos
            if len(expected_crumbs) > 1:
                self.assertIn("&gt;", crumb_block)
            self.assertIn(marker, html)

    def test_overall_and_individual_transformer_power_controls(self):
        Transformer.ensure_defaults()
        admin_client, admin_resp = self._login_admin()
        admin_html = admin_resp.content.decode("utf-8")
        self.assertIn('id="masterGridControlBar"', admin_html)
        self.assertIn('id="masterPowerOnBtn"', admin_html)
        self.assertIn('id="masterPowerOffBtn"', admin_html)

        t1 = Transformer.objects.get(code="T-01")
        t2 = Transformer.objects.get(code="T-02")
        t3 = Transformer.objects.get(code="T-03")
        csrf_token = admin_client.cookies["csrftoken"].value

        # Overall Power OFF shuts down ALL transformers
        all_off = admin_client.post(
            reverse("toggle_power_supply"),
            data={"action": "off", "scope": "all", "transformer_id": "all", "csrfmiddlewaretoken": csrf_token},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(all_off.status_code, 200)
        all_off_json = all_off.json()
        self.assertEqual(all_off_json["scope"], "all")
        self.assertTrue(all_off_json["overall_grid"]["all_off"])
        self.assertEqual(all_off_json["overall_grid"]["online"], 0)
        for tf in (t1, t2, t3):
            tf.refresh_from_db()
            self.assertFalse(tf.is_power_on)

        # Turn ONLY individual transformer T-02 ON
        ind_on = admin_client.post(
            reverse("toggle_power_supply"),
            data={"action": "on", "transformer_id": t2.id, "csrfmiddlewaretoken": csrf_token},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(ind_on["scope"], "individual")
        self.assertEqual(ind_on["overall_grid"]["online"], 1)
        self.assertFalse(Transformer.objects.get(pk=t1.id).is_power_on)
        self.assertTrue(Transformer.objects.get(pk=t2.id).is_power_on)
        self.assertFalse(Transformer.objects.get(pk=t3.id).is_power_on)

        # Overall Power ON energizes ALL transformers
        all_on = admin_client.post(
            reverse("toggle_power_supply"),
            data={"action": "on", "scope": "all", "transformer_id": "all", "csrfmiddlewaretoken": csrf_token},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(all_on["overall_grid"]["all_on"])
        self.assertEqual(all_on["overall_grid"]["online"], 3)
        for tf in (t1, t2, t3):
            tf.refresh_from_db()
            self.assertTrue(tf.is_power_on)

        # Turn ONLY individual transformer T-02 OFF while others remain ON
        ind_off = admin_client.post(
            reverse("toggle_power_supply"),
            data={"action": "off", "transformer_id": t2.id, "csrfmiddlewaretoken": csrf_token},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(ind_off["overall_grid"]["online"], 2)
        self.assertTrue(Transformer.objects.get(pk=t1.id).is_power_on)
        self.assertFalse(Transformer.objects.get(pk=t2.id).is_power_on)
        self.assertTrue(Transformer.objects.get(pk=t3.id).is_power_on)

    def test_individual_home_and_industry_power_cut_and_remark_updates(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value

        # Create Home user & Industry user sharing T-01
        home_form = CreateAccountForm(data=self.valid_data)
        self.assertTrue(home_form.is_valid())
        home_acc = home_form.save()

        ind_data = {
            "full_name": "Apex Industries",
            "username": "apexind1",
            "email": "ops@apexind.com",
            "mobile_number": "9876501234",
            "password": "Pass1234",
            "confirm_password": "Pass1234",
        }
        ind_form = CreateAccountForm(data=ind_data)
        self.assertTrue(ind_form.is_valid())
        ind_acc = ind_form.save()

        home_conn = ConnectionApplication.objects.create(
            user=home_acc,
            application_id="REQ-7001",
            applicant_name="Ravi Kumar",
            mobile_number="9876543210",
            house_address="Villa 12, Jubilee Hills, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR7001",
            transformer=t1,
            meter_number="MTR-HOME-01",
            original_meter_serial="ORIG-HOME-01",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )
        ind_conn = ConnectionApplication.objects.create(
            user=ind_acc,
            application_id="REQ-7002",
            applicant_name="Apex Factory",
            mobile_number="9876501234",
            house_address="Plot 88, IDA Jeedimetla, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
            industry_type="Manufacturing / Factory",
            requested_load_kw="15.00",
            id_proof_number="GSTIN7002",
            transformer=t1,
            meter_number="MTR-IND-02",
            original_meter_serial="ORIG-IND-02",
            original_meter_verified=True,
            prepaid_units_balance=Decimal("50.00"),
            prepaid_units_allocated_total=Decimal("50.00"),
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        # Log in both consumers in separate clients
        home_client = Client(enforce_csrf_checks=True)
        h_csrf, _ = self._get_csrf(home_client, "/")
        home_client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": h_csrf},
            follow=True,
        )
        ind_client = Client(enforce_csrf_checks=True)
        i_csrf, _ = self._get_csrf(ind_client, "/")
        ind_client.post(
            reverse("user_login"),
            data={"username": "apexind1", "password": "Pass1234", "csrfmiddlewaretoken": i_csrf},
            follow=True,
        )

        # Cut power individually ONLY for the Domestic Home with a remark
        cut_home_resp = admin_client.post(
            reverse("toggle_connection_power_cut", args=[home_conn.id]),
            data={
                "action": "cut",
                "power_cut_remark": "Domestic overdue bill audit",
                "csrfmiddlewaretoken": admin_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(cut_home_resp.status_code, 200)
        cut_home_json = cut_home_resp.json()
        self.assertTrue(cut_home_json["ok"])
        self.assertTrue(cut_home_json["connection"]["is_power_cut"])
        self.assertEqual(cut_home_json["connection"]["power_cut_remark"], "Domestic overdue bill audit")

        # Home supply is blocked with remark while Industry on the SAME T-01 stays live
        blocked_home = home_client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": home_client.cookies["csrftoken"].value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(blocked_home["blocked"])
        self.assertTrue(blocked_home["individual_power_cut"])
        self.assertIn("Domestic overdue bill audit", blocked_home["message"])
        self.assertFalse(home_client.get(reverse("power_status_api")).json()["has_active_supply"])

        live_ind = ind_client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": ind_client.cookies["csrftoken"].value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertFalse(live_ind["blocked"])
        self.assertTrue(live_ind["effective_power_on"])
        self.assertTrue(ind_client.get(reverse("power_status_api")).json()["has_active_supply"])

        # Update ONLY the remark while keeping power cut active
        rem_resp = admin_client.post(
            reverse("toggle_connection_power_cut", args=[home_conn.id]),
            data={
                "action": "remark",
                "power_cut_remark": "Meter inspection scheduled at 4 PM",
                "csrfmiddlewaretoken": admin_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(rem_resp["connection"]["is_power_cut"])
        self.assertEqual(rem_resp["connection"]["power_cut_remark"], "Meter inspection scheduled at 4 PM")

        # User connection status & simulation reflect updated remark and Individual Power Cut
        home_status_html = home_client.get(reverse("user_connection_status")).content.decode("utf-8")
        self.assertIn("Individual Power Cut", home_status_html)
        self.assertIn("Meter inspection scheduled at 4 PM", home_status_html)

        # Now cut power individually for Commercial Industry and restore Domestic Home
        admin_client.post(
            reverse("toggle_connection_power_cut", args=[ind_conn.id]),
            data={
                "action": "cut",
                "power_cut_remark": "Factory feeder load balancing",
                "csrfmiddlewaretoken": admin_csrf,
            },
            follow=True,
        )
        admin_client.post(
            reverse("toggle_connection_power_cut", args=[home_conn.id]),
            data={
                "action": "restore",
                "power_cut_remark": "Home bill cleared - supply restored",
                "csrfmiddlewaretoken": admin_csrf,
            },
            follow=True,
        )

        # Home is now live again, while Industry is blocked with its remark
        restored_home = home_client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": home_client.cookies["csrftoken"].value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(restored_home["effective_power_on"])

        blocked_ind = ind_client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": ind_client.cookies["csrftoken"].value},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(blocked_ind["blocked"])
        self.assertTrue(blocked_ind["individual_power_cut"])
        self.assertIn("Factory feeder load balancing", blocked_ind["message"])

        # Admin connections page displays individual controls & updated remarks cleanly
        conn_html = admin_client.get(reverse("admin_connections")).content.decode("utf-8")
        self.assertIn('id="connectedPowerControlsSection"', conn_html)
        self.assertIn("Factory feeder load balancing", conn_html)
        self.assertIn("Home bill cleared - supply restored", conn_html)

    def test_commercial_prepaid_billing_unit_allocation_and_exhaustion(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        acc = CreateAccountForm(data=self.valid_data).save()
        conn = ConnectionApplication.objects.create(
            user=acc,
            application_id="REQ-9001",
            applicant_name="Ravi Manufacturing",
            mobile_number="9876543210",
            house_address="Plot 10, Industrial Zone, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
            industry_type="Manufacturing / Factory",
            requested_load_kw="15.00",
            id_proof_number="GSTIN9001",
            transformer=t1,
            meter_number="MTR-COMM-01",
            original_meter_serial="ORIG-COMM-01",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": csrf},
            follow=True,
        )
        u_csrf = self.client.cookies["csrftoken"].value

        # Commercial starts with 0 allocated units -> power switch blocked until paid
        blocked = self.client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(blocked["blocked"])
        self.assertTrue(blocked["prepaid_units_exhausted"])

        # Meter lookup shows Base Fare Rs 30, Commercial rate Rs 7/unit, Bulk Packs & DISCOM Gateway modal
        bill_html = self.client.get(f"{reverse('user_billing')}?meter_number=MTR-COMM-01").content.decode("utf-8")
        self.assertIn("MTR-COMM-01", bill_html)
        self.assertIn("Rs 30.00", bill_html)
        self.assertIn("Rs 7.00 / Unit", bill_html)
        self.assertIn("commercialBulkPacksGrid", bill_html)
        self.assertIn("paymentGatewayModal", bill_html)
        self.assertIn("DISCOM PowerPay", bill_html)
        self.assertIn("BULK-50", bill_html)

        # Below-minimum prepaid payment rejected (must cover Rs 30 base fare + at least 10 bulk units = Rs 100)
        self.client.post(
            reverse("user_pay_bill"),
            data={"pay_kind": "prepaid", "meter_number": "MTR-COMM-01", "amount_paid": "50", "csrfmiddlewaretoken": u_csrf},
            follow=True,
        )
        conn.refresh_from_db()
        self.assertEqual(conn.prepaid_units_balance, Decimal("0.00"))

        # Simulated bank decline on Bulk Recharge Gateway must return 400 and allocate zero units
        declined_resp = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "bulk_recharge",
                "meter_number": "MTR-COMM-01",
                "pack_code": "BULK-50",
                "payment_method": "UPI",
                "simulate_outcome": "failed",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(declined_resp.status_code, 400)
        declined_json = declined_resp.json()
        self.assertFalse(declined_json["ok"])
        self.assertEqual(declined_json["gateway_status"], "Failed")
        conn.refresh_from_db()
        self.assertEqual(conn.prepaid_units_balance, Decimal("0.00"))

        # Pay Rs 450 -> Rs 30 Base Fare + Rs 420 Energy Credit (@ Rs 7/unit) -> allocates 60.00 Units + TXN & BLK bill
        pay_resp = self.client.post(
            reverse("user_pay_bill"),
            data={"pay_kind": "prepaid", "meter_number": "MTR-COMM-01", "amount_paid": "450", "csrfmiddlewaretoken": u_csrf},
            follow=True,
        )
        self.assertEqual(pay_resp.status_code, 200)
        conn.refresh_from_db()
        self.assertEqual(conn.prepaid_units_balance, Decimal("60.00"))
        paid_custom_bill = PowerBill.objects.filter(
            connection=conn, bill_type=PowerBill.TYPE_PREPAID, units_billed=Decimal("60.00"), status=PowerBill.STATUS_PAID
        ).first()
        self.assertIsNotNone(paid_custom_bill)
        self.assertTrue(paid_custom_bill.bill_number.startswith("BLK-"))
        self.assertTrue(paid_custom_bill.transaction_id.startswith("TXN-"))
        self.assertEqual(paid_custom_bill.gateway_status, "Success")

        # Now Facility Power turns ON
        on_resp = self.client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertFalse(on_resp["blocked"])
        self.assertTrue(on_resp["effective_power_on"])

        # Use 30.00 Units (10,000 W x 3.0 hrs) -> 30.00 Units left
        usage1 = self.client.post(
            reverse("record_power_usage"),
            data={"active_watts": "10000", "duration_hours": "3.0", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(usage1["prepaid_units_balance"], 30.0)
        self.assertFalse(usage1["exhausted"])

        # Use remaining 30+ Units (15,000 W x 2.5 hrs) -> exhausted & power automatically shut OFF
        usage2 = self.client.post(
            reverse("record_power_usage"),
            data={"active_watts": "15000", "duration_hours": "2.5", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(usage2["prepaid_units_balance"], 0.0)
        self.assertTrue(usage2["exhausted"])
        self.assertFalse(usage2["effective_power_on"])

        # Cannot restart until next prepaid payment
        reblock = self.client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(reblock["blocked"])
        self.assertTrue(reblock["prepaid_units_exhausted"])

        # Recharge with Commercial Bulk Pack BULK-50 via AJAX Dummy Payment Gateway
        bulk_pack_resp = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "bulk_recharge",
                "meter_number": "MTR-COMM-01",
                "pack_code": "BULK-50",
                "payment_method": "Net Banking",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(bulk_pack_resp.status_code, 200)
        bulk_pack_json = bulk_pack_resp.json()
        self.assertTrue(bulk_pack_json["ok"])
        self.assertEqual(bulk_pack_json["gateway_status"], "Success")
        self.assertTrue(bulk_pack_json["bill_number"].startswith("BLK-"))
        self.assertTrue(bulk_pack_json["transaction_id"].startswith("TXN-"))
        self.assertEqual(bulk_pack_json["payment_method"], "Net Banking")
        self.assertEqual(bulk_pack_json["allocated_units"], 50.0)
        self.assertEqual(bulk_pack_json["prepaid_units_balance"], 50.0)

        # Custom Bulk Units < 10 rejected cleanly
        small_bulk = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "bulk_recharge",
                "meter_number": "MTR-COMM-01",
                "bulk_units": "5",
                "payment_method": "Industrial Wallet",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(small_bulk.status_code, 400)
        self.assertFalse(small_bulk.json()["ok"])

        # Custom Bulk Units >= 10 succeeds (12 units -> Rs 30 + 84 = Rs 114)
        custom_bulk = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "bulk_recharge",
                "meter_number": "MTR-COMM-01",
                "bulk_units": "12",
                "payment_method": "Industrial Wallet",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(custom_bulk.status_code, 200)
        custom_bulk_json = custom_bulk.json()
        self.assertEqual(custom_bulk_json["allocated_units"], 12.0)
        self.assertEqual(custom_bulk_json["total_amount"], 114.0)
        self.assertEqual(custom_bulk_json["prepaid_units_balance"], 62.0)

        # Facility power can now turn ON again after bulk recharge
        restart_resp = self.client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertFalse(restart_resp["blocked"])
        self.assertTrue(restart_resp["effective_power_on"])

        # Simulation uses 1x real-time metering (stepRealTimeMetering) + backup runtime display
        sim_html = self.client.get(reverse("user_power_simulation")).content.decode("utf-8")
        self.assertIn("stepRealTimeMetering", sim_html)
        self.assertIn("simEstRuntime", sim_html)
        self.assertNotIn("recordUsageTick(0.05)", sim_html)

        # Real-time ~7.2s tick at 5.5 kW deducts 0.01 Units accurately (62.00 -> 61.99), not 45x inflated
        rt_tick = self.client.post(
            reverse("record_power_usage"),
            data={"active_watts": "5500", "duration_hours": "0.0020", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(rt_tick["recorded_units"], 0.01)
        self.assertEqual(rt_tick["prepaid_units_balance"], 61.99)

        # Short sub-0.01-hr tick below 0.005 kWh does not artificially over-charge 0.01 Units
        tiny_tick = self.client.post(
            reverse("record_power_usage"),
            data={"active_watts": "1000", "duration_hours": "0.0010", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(tiny_tick["recorded_units"], 0.0)
        self.assertEqual(tiny_tick["prepaid_units_balance"], 61.99)

    def test_domestic_monthly_bill_generation_meter_lookup_and_payment(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        acc = CreateAccountForm(data=self.valid_data).save()
        conn = ConnectionApplication.objects.create(
            user=acc,
            application_id="REQ-9101",
            applicant_name="Ravi Domestic",
            mobile_number="9876543210",
            house_address="House 12, Jubilee Hills, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR9101",
            transformer=t1,
            meter_number="MTR-DOM-01",
            original_meter_serial="ORIG-DOM-01",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )
        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value

        # Other user meter protection check
        other_acc = CreateAccountForm(
            data={**self.valid_data, "username": "otheruser1", "email": "other@example.com", "mobile_number": "9876509876"}
        ).save()
        ConnectionApplication.objects.create(
            user=other_acc,
            application_id="REQ-9102",
            applicant_name="Other Home",
            mobile_number="9876509876",
            house_address="House 99, Banjara Hills, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR9102",
            transformer=t1,
            meter_number="MTR-DOM-OTHER",
            original_meter_serial="ORIG-DOM-99",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "ravikumar1", "password": "Pass1234", "csrfmiddlewaretoken": csrf},
            follow=True,
        )
        u_csrf = self.client.cookies["csrftoken"].value

        # Domestic turns ON power and logs 10.00 Units (5000 W x 2.0 hrs)
        self.client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        usage = self.client.post(
            reverse("record_power_usage"),
            data={"active_watts": "5000", "duration_hours": "2.0", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(usage["unbilled_units"], 10.0)
        # Active Tariff: Rs 30 Base Fare + (10 Units x Rs 5/unit) = Rs 80.00
        self.assertEqual(usage["estimated_bill_rs"], 80.0)

        # BEFORE Admin generates Monthly Bill, Domestic consumer sees ONLY live usage & awaiting notice (NOT payable Rupee bill)
        pre_bill_html = self.client.get(f"{reverse('user_billing')}?meter_number=MTR-DOM-01").content.decode("utf-8")
        self.assertIn("Monthly Bill Not Generated Yet", pre_bill_html)
        self.assertIn("10.00 Units", pre_bill_html)
        self.assertNotIn("Rs 80.00", pre_bill_html)

        # User cannot look up another consumer's meter
        denied_lookup = self.client.get(f"{reverse('user_billing')}?meter_number=MTR-DOM-OTHER").content.decode("utf-8")
        self.assertIn("does not belong to your consumer account", denied_lookup)

        # Admin looks up MTR-DOM-01 and generates Monthly Bills for 2026-11
        admin_lookup_html = admin_client.get(f"{reverse('admin_billing')}?meter_number=MTR-DOM-01").content.decode("utf-8")
        self.assertIn("Rs 80.00", admin_lookup_html)

        gen_resp = admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": "2026-11", "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertEqual(gen_resp.status_code, 200)
        monthly_bill = PowerBill.objects.get(connection=conn, bill_type=PowerBill.TYPE_MONTHLY, billing_month="2026-11")
        self.assertEqual(monthly_bill.units_billed, Decimal("10.00"))
        self.assertEqual(monthly_bill.base_fare, Decimal("30.00"))
        self.assertEqual(monthly_bill.rate_per_unit, Decimal("5.00"))
        self.assertEqual(monthly_bill.energy_charge, Decimal("50.00"))
        self.assertEqual(monthly_bill.total_amount, Decimal("80.00"))
        self.assertEqual(monthly_bill.status, PowerBill.STATUS_UNPAID)

        conn.refresh_from_db()
        self.assertEqual(conn.unbilled_units, Decimal("0.00"))

        # Duplicate generation for same month is prevented cleanly
        admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": "2026-11", "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertEqual(PowerBill.objects.filter(connection=conn, billing_month="2026-11").count(), 1)

        # ONLY AFTER generation does Domestic user see Monthly Bill INV-... & Rs 80.00 and pay via DISCOM Gateway
        user_bill_html = self.client.get(f"{reverse('user_billing')}?meter_number=MTR-DOM-01").content.decode("utf-8")
        self.assertIn(monthly_bill.bill_number, user_bill_html)
        self.assertIn("Rs 80.00", user_bill_html)
        self.assertIn("paymentGatewayModal", user_bill_html)

        # Simulated failed gateway leaves Domestic bill Unpaid
        fail_dom_resp = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "monthly",
                "bill_id": monthly_bill.id,
                "meter_number": "MTR-DOM-01",
                "payment_method": "Credit / Debit Card",
                "simulate_outcome": "failed",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(fail_dom_resp.status_code, 400)
        monthly_bill.refresh_from_db()
        self.assertEqual(monthly_bill.status, PowerBill.STATUS_UNPAID)

        # Successful gateway payment settles Monthly Bill and records TXN ID + method
        ok_dom_resp = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "monthly",
                "bill_id": monthly_bill.id,
                "meter_number": "MTR-DOM-01",
                "payment_method": "Credit / Debit Card",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(ok_dom_resp.status_code, 200)
        ok_dom_json = ok_dom_resp.json()
        self.assertTrue(ok_dom_json["ok"])
        self.assertEqual(ok_dom_json["gateway_status"], "Success")
        self.assertTrue(ok_dom_json["transaction_id"].startswith("TXN-"))
        monthly_bill.refresh_from_db()
        self.assertEqual(monthly_bill.status, PowerBill.STATUS_PAID)
        self.assertEqual(monthly_bill.payment_method, "Credit / Debit Card")
        self.assertEqual(monthly_bill.gateway_status, "Success")
        self.assertEqual(monthly_bill.transaction_id, ok_dom_json["transaction_id"])

        # Transaction ID appears in both consumer billing and admin billing
        paid_user_html = self.client.get(f"{reverse('user_billing')}?meter_number=MTR-DOM-01").content.decode("utf-8")
        self.assertIn(monthly_bill.transaction_id, paid_user_html)
        paid_admin_html = admin_client.get(f"{reverse('admin_billing')}?meter_number=MTR-DOM-01").content.decode("utf-8")
        self.assertIn(monthly_bill.transaction_id, paid_admin_html)
        self.assertIn("Credit / Debit Card", paid_admin_html)

    def test_all_three_portals_ordered_navigation_and_steps(self):
        # 1) User Side: ordered workflow Steps 01 -> 05 + Section 1 -> Section 3
        Transformer.ensure_defaults()
        CreateAccountForm(data=self.valid_data).save()
        csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={
                "username": self.valid_data["username"],
                "password": self.valid_data["password"],
                "csrfmiddlewaretoken": csrf,
            },
        )
        home_resp = self.client.get(reverse("user_home"))
        self.assertEqual(home_resp.status_code, 200)
        home_html = home_resp.content.decode("utf-8")
        for needle in ("Step 01", "Step 02", "Step 03", "Step 04", "Step 05", "Section 1", "Section 2", "Section 3"):
            self.assertIn(needle, home_html)
        self.assertLess(home_html.index("01. New Connection"), home_html.index("02. Connection Status"))
        self.assertLess(home_html.index("02. Connection Status"), home_html.index("03. Power Billing"))
        self.assertLess(home_html.index("03. Power Billing"), home_html.index("04. Power Simulation"))
        self.assertLess(home_html.index("04. Power Simulation"), home_html.index("05. Account Profile"))
        self.assertLess(home_html.index("Section 1"), home_html.index("Section 2"))
        self.assertLess(home_html.index("Section 2"), home_html.index("Section 3"))

        # 2) Administration Side: shared navbar (1->6), breadcrumbs, and ordered sections on all admin pages
        admin_client, _ = self._login_admin()
        for url_name in (
            "admin_home",
            "admin_employees",
            "admin_connections",
            "admin_billing",
            "admin_maintenance",
            "admin_revenue",
        ):
            html = admin_client.get(reverse(url_name)).content.decode("utf-8")
            self.assertIn("admin-navbar", html)
            self.assertIn("admin-breadcrumb-bar", html)
            nav_start = html.index('aria-label="Administration Ordered Navigation"')
            nav_end = html.index("</nav>", nav_start)
            nav_html = html[nav_start:nav_end]
            self.assertLess(nav_html.index("Grid &amp; Transformers"), nav_html.index("Department Staff"))
            self.assertLess(nav_html.index("Department Staff"), nav_html.index("Connections &amp; Meters"))
            self.assertLess(nav_html.index("Connections &amp; Meters"), nav_html.index("Billing &amp; Tariffs"))
            self.assertLess(nav_html.index("Billing &amp; Tariffs"), nav_html.index("Maintenance Approvals"))
            self.assertIn('id="adminBillingDropdown"', nav_html)
            dd_start = nav_html.index('id="adminBillingDropdown"')
            dd_end = nav_html.index('Maintenance Approvals', dd_start)
            dd_html = nav_html[dd_start:dd_end]
            self.assertIn(reverse("admin_revenue"), dd_html)
            self.assertIn("Revenue &amp; Monthly Collections", dd_html)
            self.assertNotIn('class="nav-step-num">', nav_html)
            self.assertIn('class="nav-count-badge"', nav_html)

        # 3) Department Side: separate Operations dropdown pages (not stacked single page) & Substation Parts A->D
        emp = DepartmentEmployee.objects.create(
            employee_id="DEPT-777",
            full_name="Karthik Dept",
            username="karthikdept",
            email="karthik@dept.gov",
            mobile_number="9876543000",
            designation="Field Engineer",
            is_active=True,
        )
        emp.set_password("DeptPass@123")
        emp.save()
        dept_client = self.client.__class__()
        d_csrf = dept_client.get(reverse("home")).cookies["csrftoken"].value
        dept_client.post(
            reverse("department_verify_employee_id"),
            data={"employee_id": "DEPT-777", "csrfmiddlewaretoken": d_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        dept_resp = dept_client.post(
            reverse("department_login"),
            data={
                "employee_id": "DEPT-777",
                "username": "karthikdept",
                "password": "DeptPass@123",
                "csrfmiddlewaretoken": d_csrf,
            },
            follow=True,
        )
        dept_html = dept_resp.content.decode("utf-8")
        self.assertIn("deptOperationsDropdown", dept_html)
        self.assertNotIn('id="deptStep1"', dept_html)
        self.assertLess(dept_html.index("01. Verify &amp; Assign Requests"), dept_html.index("02. Install Approved Meters"))
        self.assertLess(dept_html.index("02. Install Approved Meters"), dept_html.index("03. Substations &amp; Meters Bay"))
        self.assertLess(dept_html.index("03. Substations &amp; Meters Bay"), dept_html.index("04. Connected &amp; Live Usage"))
        self.assertLess(dept_html.index("04. Connected &amp; Live Usage"), dept_html.index("05. Maintenance Schedules"))

        sub_html = dept_client.get(reverse("department_substations")).content.decode("utf-8")
        sub_pos = -1
        for section_marker in (
            "Overall Substation Grid Control",
            "Feeder Transformers Fleet",
            'id="transformerPanel"',
            'id="allocatedMetersSection"',
        ):
            nxt = sub_html.find(section_marker, sub_pos + 1)
            self.assertGreater(nxt, sub_pos, f"Missing ordered substation section '{section_marker}'")
            sub_pos = nxt

    def test_admin_tariff_revision_updates_bills_users_and_department_pagination(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        t2 = Transformer.objects.get(code="T-02")

        # Create 7 connected consumers (Domestic + Commercial) to test Department pagination & dropdown filters
        dom_acc = CreateAccountForm(data=self.valid_data).save()
        dom_conn = ConnectionApplication.objects.create(
            user=dom_acc,
            application_id="REQ-7001",
            applicant_name="Alpha Domestic",
            mobile_number="9876543210",
            house_address="Lane 1, North Colony",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR7001",
            transformer=t1,
            meter_number="MTR-PAGE-01",
            original_meter_serial="ORIG-PG-01",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
            unbilled_units=Decimal("10.00"),
            total_units_consumed=Decimal("10.00"),
        )
        comm_acc = CreateAccountForm(
            data={**self.valid_data, "username": "commuser7", "email": "c7@example.com", "mobile_number": "9876540007"}
        ).save()
        comm_conn = ConnectionApplication.objects.create(
            user=comm_acc,
            application_id="REQ-7002",
            applicant_name="Beta Steel Plant",
            mobile_number="9876540007",
            house_address="Plot 2, Central Zone",
            property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
            industry_type="Manufacturing / Factory",
            requested_load_kw="15.00",
            id_proof_number="GSTIN7002",
            transformer=t2,
            meter_number="MTR-PAGE-02",
            original_meter_serial="ORIG-PG-02",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
            is_power_cut=True,
            power_cut_remark="Maintenance Shutdown",
            prepaid_units_balance=Decimal("25.00"),
            total_units_consumed=Decimal("40.00"),
        )
        for idx in range(3, 8):
            acc_i = CreateAccountForm(
                data={
                    **self.valid_data,
                    "username": f"pageuser{idx}",
                    "email": f"pu{idx}@example.com",
                    "mobile_number": f"987650000{idx}",
                }
            ).save()
            ConnectionApplication.objects.create(
                user=acc_i,
                application_id=f"REQ-700{idx}",
                applicant_name=f"Consumer {idx}",
                mobile_number=f"987650000{idx}",
                house_address=f"Street {idx}",
                property_type=ConnectionApplication.PROPERTY_DOMESTIC,
                requested_load_kw="2.00",
                id_proof_number=f"AADHAAR700{idx}",
                transformer=t1,
                meter_number=f"MTR-PAGE-0{idx}",
                original_meter_serial=f"ORIG-PG-0{idx}",
                original_meter_verified=True,
                status=ConnectionApplication.STATUS_CONNECTED,
            )

        # Admin updates tariff charges live: Base Fare 45.00, Domestic 6.50, Commercial 9.00
        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value
        admin_client.post(
            reverse("admin_billing"),
            data={
                "action": "update_tariff",
                "base_fare_rs": "45.00",
                "domestic_rate_per_unit": "6.50",
                "commercial_rate_per_unit": "9.00",
                "revision_note": "Summer Peak Tariff Adjustment",
                "csrfmiddlewaretoken": admin_csrf,
            },
            follow=True,
        )

        # Updated fare reflected on User Billing + Domestic bill calculation (45 + 10*6.50 = Rs 110.00)
        u_csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": self.valid_data["username"], "password": self.valid_data["password"], "csrfmiddlewaretoken": u_csrf},
            follow=True,
        )
        user_home_html = self.client.get(reverse("user_home")).content.decode("utf-8")
        self.assertIn("Summer Peak Tariff Adjustment", user_home_html)
        self.assertIn("45.00", user_home_html)
        self.assertIn("6.50", user_home_html)
        self.assertIn("9.00", user_home_html)

        admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": "2026-12", "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        updated_bill = PowerBill.objects.get(connection=dom_conn, billing_month="2026-12")
        self.assertEqual(updated_bill.base_fare, Decimal("45.00"))
        self.assertEqual(updated_bill.rate_per_unit, Decimal("6.50"))
        self.assertEqual(updated_bill.total_amount, Decimal("110.00"))

        # Updated Commercial BULK-50 calculates at Rs 45 + 50*9 = Rs 495.00 via DISCOM Gateway
        comm_client = Client(enforce_csrf_checks=True)
        c_csrf, _ = self._get_csrf(comm_client)
        comm_client.post(
            reverse("user_login"),
            data={"username": "commuser7", "password": self.valid_data["password"], "csrfmiddlewaretoken": c_csrf},
            follow=True,
        )
        comm_csrf = comm_client.cookies["csrftoken"].value
        bulk_updated = comm_client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "bulk_recharge",
                "meter_number": "MTR-PAGE-02",
                "pack_code": "BULK-50",
                "payment_method": "BBPS / Utility Wallet",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": comm_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(bulk_updated["ok"])
        self.assertEqual(bulk_updated["total_amount"], 495.0)
        self.assertEqual(bulk_updated["billing_agency"], "Electricity Department DISCOM Authorized Payment Desk")

        # Department Login: verify updated tariff, pagination, dropdown filters, search, and live usage API
        emp = DepartmentEmployee.objects.create(
            employee_id="DEPT-888",
            full_name="Meena Dept",
            username="meenadept",
            email="meena@dept.gov",
            mobile_number="9876543888",
            designation="Substation Officer",
            is_active=True,
        )
        emp.set_password("DeptPass@123")
        emp.save()
        dept_client = Client(enforce_csrf_checks=True)
        d_csrf, _ = self._get_csrf(dept_client)
        dept_client.post(
            reverse("department_verify_employee_id"),
            data={"employee_id": "DEPT-888", "csrfmiddlewaretoken": d_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        dept_client.post(
            reverse("department_login"),
            data={
                "employee_id": "DEPT-888",
                "username": "meenadept",
                "password": "DeptPass@123",
                "csrfmiddlewaretoken": d_csrf,
            },
            follow=True,
        )

        # Department Home shows updated Tariff strip + live usage snapshot
        dh = dept_client.get(reverse("department_home")).content.decode("utf-8")
        self.assertIn("Summer Peak Tariff Adjustment", dh)
        self.assertIn("45.00", dh)

        # Connected Premises pagination (per_page=5 across 7 records ordered newest first -> Page 1 and Page 2)
        p1_html = dept_client.get(f"{reverse('department_connected_premises')}?per_page=5&page=1").content.decode("utf-8")
        p2_html = dept_client.get(f"{reverse('department_connected_premises')}?per_page=5&page=2").content.decode("utf-8")
        self.assertIn("Page 1 of 2", p1_html)
        self.assertIn("Page 2 of 2", p2_html)
        self.assertIn("REQ-7007", p1_html)
        self.assertIn("REQ-7001", p2_html)

        # Search + Dropdown filters narrow to Commercial & Power Cut entry
        filt_html = dept_client.get(
            f"{reverse('department_connected_premises')}?q=Steel&property_type=Commercial&transformer_id={t2.id}&supply_status=power_cut"
        ).content.decode("utf-8")
        self.assertIn("Beta Steel Plant", filt_html)
        self.assertNotIn("Alpha Domestic", filt_html)

        # Department Live Usage JSON reflects active updated tariff + live usage values
        live_json = dept_client.get(f"{reverse('department_live_usage_api')}?ids={dom_conn.id},{comm_conn.id}").json()
        self.assertEqual(live_json["tariff"]["base_fare_rs"], 45.0)
        self.assertEqual(live_json["tariff"]["domestic_rate"], 6.5)
        self.assertEqual(live_json["tariff"]["commercial_rate"], 9.0)
        self.assertEqual(len(live_json["items"]), 2)

    def test_delete_transformer_and_maintenance_permission_power_cut_workflow(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")

        # Create a connected Domestic consumer on T-01
        acc = CreateAccountForm(data=self.valid_data).save()
        conn = ConnectionApplication.objects.create(
            user=acc,
            application_id="REQ-6101",
            applicant_name="Surya Home",
            mobile_number="9876543210",
            house_address="House 6, Madhapur, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR6101",
            transformer=t1,
            meter_number="MTR-MNT-01",
            original_meter_serial="ORIG-MNT-01",
            original_meter_verified=True,
            connection_fee_amount=Decimal("500.00"),
            connection_fee_paid=True,
            payment_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        # Commission spare Transformer T-04, check Delete button, then delete T-04 cleanly
        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value
        admin_client.post(
            reverse("add_transformer"),
            data={
                "code": "T-04",
                "substation_name": "East 33/11 kV Substation",
                "name": "East Spare Transformer T-04",
                "zone": "East Industrial Park",
                "capacity_kva": "500",
                "csrfmiddlewaretoken": admin_csrf,
            },
            follow=True,
        )
        t4 = Transformer.objects.get(code="T-04")
        home_with_t4 = admin_client.get(reverse("admin_home")).content.decode("utf-8")
        self.assertIn(f'id="tfCardDelete-{t4.id}"', home_with_t4)

        del_t4 = admin_client.post(
            reverse("delete_transformer", args=[t4.id]),
            data={"csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertEqual(del_t4.status_code, 200)
        self.assertFalse(Transformer.objects.filter(code="T-04").exists())

        # Assigned T-01 cannot be deleted while connected meter exists
        block_t1 = admin_client.post(
            reverse("delete_transformer", args=[t1.id]),
            data={"csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertTrue(Transformer.objects.filter(pk=t1.id).exists())
        self.assertIn("assigned to it", block_t1.content.decode("utf-8"))

        # Department Employee sends Maintenance Schedule requesting Power Cut Permission on T-01
        emp = DepartmentEmployee.objects.create(
            employee_id="DEPT-909",
            full_name="Arjun Maintenance",
            username="arjun_mnt",
            email="arjun@dept.gov",
            mobile_number="9876543909",
            designation="Substation Officer",
            is_active=True,
        )
        emp.set_password("DeptPass@123")
        emp.save()
        dept_client = Client(enforce_csrf_checks=True)
        d_csrf, _ = self._get_csrf(dept_client)
        dept_client.post(
            reverse("department_verify_employee_id"),
            data={"employee_id": "DEPT-909", "csrfmiddlewaretoken": d_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        dept_client.post(
            reverse("department_login"),
            data={
                "employee_id": "DEPT-909",
                "username": "arjun_mnt",
                "password": "DeptPass@123",
                "csrfmiddlewaretoken": d_csrf,
            },
            follow=True,
        )
        dept_csrf = dept_client.cookies["csrftoken"].value

        mnt_resp = dept_client.post(
            reverse("department_maintenance"),
            data={
                "scope": MaintenanceSchedule.SCOPE_TRANSFORMER,
                "transformer_id": t1.id,
                "title": "Feeder Busbar Overhaul",
                "reason": "Replace worn isolator contacts on T-01 feeder",
                "scheduled_start": "2026-11-12T11:00",
                "scheduled_end": "2026-11-12T13:00",
                "csrfmiddlewaretoken": dept_csrf,
            },
            follow=True,
        )
        self.assertEqual(mnt_resp.status_code, 200)
        sched = MaintenanceSchedule.objects.get(title="Feeder Busbar Overhaul")
        self.assertTrue(sched.schedule_id.startswith("MNT-"))
        self.assertEqual(sched.status, MaintenanceSchedule.STATUS_PENDING)

        # Administration cannot execute power cuts; execution belongs to Department staff
        admin_cut_attempt = admin_client.post(
            reverse("admin_process_maintenance", args=[sched.id]),
            data={"action": "execute_cut", "admin_remarks": "Admin attempting power cut", "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        sched.refresh_from_db()
        t1.refresh_from_db()
        self.assertEqual(sched.status, MaintenanceSchedule.STATUS_PENDING)
        self.assertTrue(t1.is_power_on)
        self.assertIn("must be executed by the Department staff", admin_cut_attempt.content.decode("utf-8"))

        # Department CANNOT execute power cut before Admin approves permission
        early_dept_cut = dept_client.post(
            reverse("department_process_maintenance", args=[sched.id]),
            data={"action": "execute_cut", "dept_remarks": "Trying before approval", "csrfmiddlewaretoken": dept_csrf},
            follow=True,
        )
        sched.refresh_from_db()
        t1.refresh_from_db()
        self.assertEqual(sched.status, MaintenanceSchedule.STATUS_PENDING)
        self.assertTrue(t1.is_power_on)
        self.assertIn("must have Approved permission from Administration first", early_dept_cut.content.decode("utf-8"))

        # Admin reviews and approves permission -> User sees Scheduled Maintenance Notice on Connection Status!
        admin_client.post(
            reverse("admin_process_maintenance", args=[sched.id]),
            data={"action": "approve", "admin_remarks": "Approved safety shutdown window", "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        sched.refresh_from_db()
        self.assertEqual(sched.status, MaintenanceSchedule.STATUS_APPROVED)

        u_csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": self.valid_data["username"], "password": self.valid_data["password"], "csrfmiddlewaretoken": u_csrf},
            follow=True,
        )
        status_html = self.client.get(reverse("user_connection_status")).content.decode("utf-8")
        self.assertIn("Scheduled Maintenance Notice", status_html)
        self.assertIn(sched.schedule_id, status_html)

        # Department executes power cut AFTER Admin approval -> T-01 turns OFF cleanly
        dept_client.post(
            reverse("department_process_maintenance", args=[sched.id]),
            data={"action": "execute_cut", "dept_remarks": "Executing approved T-01 isolation", "csrfmiddlewaretoken": dept_csrf},
            follow=True,
        )
        sched.refresh_from_db()
        t1.refresh_from_db()
        self.assertEqual(sched.status, MaintenanceSchedule.STATUS_ACTIVE_CUT)
        self.assertFalse(t1.is_power_on)

        # Department completes maintenance & restores power -> T-01 back ON
        dept_client.post(
            reverse("department_process_maintenance", args=[sched.id]),
            data={"action": "complete_restore", "dept_remarks": "Busbar overhaul done", "csrfmiddlewaretoken": dept_csrf},
            follow=True,
        )
        sched.refresh_from_db()
        t1.refresh_from_db()
        self.assertEqual(sched.status, MaintenanceSchedule.STATUS_COMPLETED)
        self.assertTrue(t1.is_power_on)

        # Individual Premise Maintenance Request (approved by admin, then cut & restored by department)
        dept_client.post(
            reverse("department_maintenance"),
            data={
                "scope": MaintenanceSchedule.SCOPE_PREMISE,
                "target_connection_id": conn.id,
                "title": "Meter Seal Replacement",
                "reason": "Tamper-proof seal upgrade",
                "scheduled_start": "2026-11-12T14:00",
                "scheduled_end": "2026-11-12T15:00",
                "csrfmiddlewaretoken": dept_csrf,
            },
            follow=True,
        )
        premise_sched = MaintenanceSchedule.objects.get(title="Meter Seal Replacement")
        admin_client.post(
            reverse("admin_process_maintenance", args=[premise_sched.id]),
            data={"action": "approve", "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        premise_sched.refresh_from_db()
        self.assertEqual(premise_sched.status, MaintenanceSchedule.STATUS_APPROVED)

        # Department executes the premise power cut
        dept_client.post(
            reverse("department_process_maintenance", args=[premise_sched.id]),
            data={"action": "execute_cut", "csrfmiddlewaretoken": dept_csrf},
            follow=True,
        )
        conn.refresh_from_db()
        self.assertTrue(conn.is_power_cut)
        self.assertIn(premise_sched.schedule_id, conn.power_cut_remark)

        # Department completes maintenance and restores power to premise
        dept_client.post(
            reverse("department_process_maintenance", args=[premise_sched.id]),
            data={"action": "complete_restore", "csrfmiddlewaretoken": dept_csrf},
            follow=True,
        )
        conn.refresh_from_db()
        self.assertFalse(conn.is_power_cut)

    def test_appliance_on_off_consumption_runtime_and_billing_breakdown(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        comm_acc = CreateAccountForm(
            data={**self.valid_data, "username": "commapp1", "email": "commapp1@example.com", "mobile_number": "9876511101"}
        ).save()
        comm_conn = ConnectionApplication.objects.create(
            user=comm_acc,
            application_id="REQ-9501",
            applicant_name="Vortex Precision",
            mobile_number="9876511101",
            house_address="Plot 5, Tech Park, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
            industry_type="IT / Office Park",
            requested_load_kw="10.00",
            id_proof_number="GSTIN9501",
            transformer=t1,
            meter_number="MTR-APP-C1",
            original_meter_serial="ORIG-APP-C1",
            original_meter_verified=True,
            prepaid_units_balance=Decimal("250.00"),
            prepaid_units_allocated_total=Decimal("250.00"),
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "commapp1", "password": self.valid_data["password"], "csrfmiddlewaretoken": csrf},
            follow=True,
        )
        u_csrf = self.client.cookies["csrftoken"].value

        # Initially all 6 built-in Commercial appliances are ON -> 5500W (5.50 kW)
        status_full = self.client.get(reverse("power_status_api")).json()
        self.assertEqual(status_full["telemetry"]["active_watts"], 5500)
        self.assertIn("@ 5.50 kW", status_full["est_runtime_text"])

        # Turn OFF AC (2500W), Fan1 (250W), Fan2 (250W), Lights (300W) -> leaves TV (450W) + Plant/Fridge (1750W) = 2200W (2.20 kW)
        for key in ("ac", "fan1", "fan2", "lights"):
            tog = self.client.post(
                reverse("toggle_builtin_appliance", args=[key]),
                data={"csrfmiddlewaretoken": u_csrf},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            ).json()
            self.assertTrue(tog["ok"])
            self.assertFalse(tog["is_on"])

        # Persists across reloads, Billing, Simulation, and power_status_api with updated @ 2.20 kW runtime
        status_22 = self.client.get(reverse("power_status_api")).json()
        self.assertEqual(status_22["telemetry"]["active_watts"], 2200)
        self.assertEqual(status_22["telemetry"]["active_kw"], 2.2)
        self.assertEqual(status_22["telemetry"]["hourly_cost_rs"], 15.4)
        self.assertIn("@ 2.20 kW", status_22["est_runtime_text"])

        sim_html = self.client.get(reverse("user_power_simulation")).content.decode("utf-8")
        self.assertIn("2200 W", sim_html)
        self.assertIn("@ 2.20 kW", sim_html)

        bill_html = self.client.get(f"{reverse('user_billing')}?meter_number=MTR-APP-C1").content.decode("utf-8")
        self.assertIn("2.20 kW", bill_html)
        self.assertIn("@ 2.20 kW", bill_html)
        self.assertIn("billApplianceTable", bill_html)

        # Record 1.00 hour using ONLY persisted ON appliances -> deducts 2.20 Units (250.00 -> 247.80)
        usage_resp = self.client.post(
            reverse("record_power_usage"),
            data={"use_server_appliances": "1", "duration_hours": "1.0", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(usage_resp["ok"])
        self.assertEqual(usage_resp["recorded_units"], 2.2)
        self.assertEqual(usage_resp["prepaid_units_balance"], 247.8)
        by_key = {a["key"]: a for a in usage_resp["telemetry"]["appliances"]}
        self.assertEqual(by_key["ac"]["consumed_units"], 0.0)
        self.assertEqual(by_key["tv"]["consumed_units"], 0.45)
        self.assertEqual(by_key["fridge"]["consumed_units"], 1.75)

        # Turn OFF the remaining 2 appliances -> 0 W load, Standby runtime, and 0.00 Units consumed
        for key in ("tv", "fridge"):
            self.client.post(
                reverse("toggle_builtin_appliance", args=[key]),
                data={"csrfmiddlewaretoken": u_csrf},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        zero_usage = self.client.post(
            reverse("record_power_usage"),
            data={"use_server_appliances": "1", "duration_hours": "2.0", "csrfmiddlewaretoken": u_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(zero_usage["recorded_units"], 0.0)
        self.assertEqual(zero_usage["prepaid_units_balance"], 247.8)
        self.assertIn("0.00 kW", zero_usage["est_runtime_text"])

        # Domestic appliance-wise consumption & Admin monthly bill breakdown
        dom_acc = CreateAccountForm(
            data={**self.valid_data, "username": "domapp1", "email": "domapp1@example.com", "mobile_number": "9876511102"}
        ).save()
        dom_conn = ConnectionApplication.objects.create(
            user=dom_acc,
            application_id="REQ-9502",
            applicant_name="Home Consumer",
            mobile_number="9876511102",
            house_address="Flat 301, Madhapur, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.50",
            id_proof_number="AADHAAR9502",
            transformer=t1,
            meter_number="MTR-APP-D1",
            original_meter_serial="ORIG-APP-D1",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )
        dom_client = Client(enforce_csrf_checks=True)
        d_csrf, _ = self._get_csrf(dom_client)
        dom_client.post(
            reverse("user_login"),
            data={"username": "domapp1", "password": self.valid_data["password"], "csrfmiddlewaretoken": d_csrf},
            follow=True,
        )
        dom_csrf = dom_client.cookies["csrftoken"].value

        # Leave only Split AC (1500W) and Fridge (220W) ON = 1720W (1.72 kW) -> 10 hrs = 17.20 Units
        for key in ("fan1", "fan2", "lights", "tv"):
            dom_client.post(
                reverse("toggle_builtin_appliance", args=[key]),
                data={"csrfmiddlewaretoken": dom_csrf},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
        dom_usage = dom_client.post(
            reverse("record_power_usage"),
            data={"use_server_appliances": "1", "duration_hours": "10.0", "csrfmiddlewaretoken": dom_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertEqual(dom_usage["unbilled_units"], 17.2)

        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value
        admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": "2026-11", "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        dom_bill = PowerBill.objects.get(connection=dom_conn, billing_month="2026-11")
        self.assertEqual(dom_bill.units_billed, Decimal("17.20"))
        self.assertEqual(dom_bill.total_amount, Decimal("116.00"))
        breakdown_map = {it["name"]: it for it in dom_bill.appliance_breakdown}
        self.assertEqual(breakdown_map["Split AC (22°C)"]["units"], 15.0)
        self.assertEqual(breakdown_map["Split AC (22°C)"]["energy_charge_rs"], 75.0)
        self.assertEqual(breakdown_map["Refrigerator (4°C)"]["units"], 2.2)
        self.assertEqual(breakdown_map["Refrigerator (4°C)"]["energy_charge_rs"], 11.0)
        self.assertNotIn("Living Room Fan", breakdown_map)

        dom_bill_html = dom_client.get(f"{reverse('user_billing')}?meter_number=MTR-APP-D1").content.decode("utf-8")
        self.assertIn("17.20 Units", dom_bill_html)
        self.assertIn('id="billApplianceTotalRow"', dom_bill_html)
        self.assertIn("Total: 17.20 U", dom_bill_html)
        self.assertNotIn("Split AC (22°C): 15.00 U (Rs 75.00)", dom_bill_html)

        # Legacy unbilled/total units on an existing connection with empty appliance_usage_stats reconcile proportionally instead of showing 0.00
        dom_conn.unbilled_units = Decimal("0.58")
        dom_conn.total_units_consumed = Decimal("18.00")
        dom_conn.appliance_usage_stats = {}
        dom_conn.save(update_fields=["unbilled_units", "total_units_consumed", "appliance_usage_stats"])
        admin_reconciled_html = admin_client.get(f"{reverse('admin_billing')}?meter_number=MTR-APP-D1").content.decode("utf-8")
        self.assertNotIn("0.00 U cycle / <strong>0.00 U</strong> total", admin_reconciled_html)
        # Active AC (1500W of 1720W) receives 0.58 * 1500/1720 = 0.51 U cycle
        self.assertIn("0.51 U", admin_reconciled_html)

        # Subsequent tiny 0.01 kWh live tick persists legacy units and displays sub-0.01 shares accurately
        tick = dom_client.post(
            reverse("record_power_usage"),
            data={"use_server_appliances": "1", "duration_hours": "0.01", "csrfmiddlewaretoken": dom_csrf},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(tick["ok"])
        self.assertGreater(tick["unbilled_units"], 0.58)
        by_key_dom = {a["key"]: a for a in tick["telemetry"]["appliances"]}
        self.assertNotEqual(by_key_dom["fridge"]["unbilled_units_display"], "0.00")

        # Continuous server-side consumption over 4 elapsed hours when tab is closed / Admin checks Billing:
        # Turn ON fan1(250)+fan2(250)+lights(300)+tv(450)+custom fan(75) = 1325W (1.325 kW); AC & Plant stay OFF.
        from datetime import timedelta
        from django.utils import timezone as dj_tz

        UserAppliance.objects.create(user=comm_acc, name="FAN NUMBER", appliance_type="Fan", wattage=75, is_on=True)
        comm_conn.appliance_states = {
            "fan1": True,
            "fan2": True,
            "lights": True,
            "tv": True,
            "ac": False,
            "fridge": False,
        }
        comm_conn.prepaid_units_balance = Decimal("249.67")
        comm_conn.total_units_consumed = Decimal("50.33")
        comm_conn.pending_energy_kwh = Decimal("0.000000")
        comm_conn.last_usage_synced_at = dj_tz.now() - timedelta(hours=4)
        comm_conn.save()
        comm_acc.house_power_on = True
        comm_acc.save(update_fields=["house_power_on"])

        # Admin Commercial Billing lookup automatically advances elapsed 4h * 1.325 kW = 5.30 Units (249.67 -> 244.37)
        comm_after_4h = admin_client.get(
            f"{reverse('admin_billing_commercial')}?meter_number=MTR-APP-C1"
        ).content.decode("utf-8")
        comm_conn.refresh_from_db()
        self.assertEqual(comm_conn.prepaid_units_balance, Decimal("244.37"))
        self.assertEqual(comm_conn.total_units_consumed, Decimal("55.63"))
        self.assertIn("244.37 Units", comm_after_4h)
        self.assertIn("55.63 Units", comm_after_4h)
        self.assertNotIn("0.00 U cycle", comm_after_4h)

        # When Main Power is OFF, elapsed 4 hours consumes 0.00 Units
        comm_acc.house_power_on = False
        comm_acc.save(update_fields=["house_power_on"])
        comm_conn.last_usage_synced_at = dj_tz.now() - timedelta(hours=4)
        comm_conn.save(update_fields=["last_usage_synced_at"])
        self.client.get(reverse("power_status_api"))
        comm_conn.refresh_from_db()
        self.assertEqual(comm_conn.prepaid_units_balance, Decimal("244.37"))

    def test_admin_billing_month_filter_paid_unpaid_and_commercial_publish_flow(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        dom_acc = CreateAccountForm(
            data={**self.valid_data, "username": "domledger1", "email": "dl1@example.com", "mobile_number": "9876522201"}
        ).save()
        dom_conn = ConnectionApplication.objects.create(
            user=dom_acc,
            application_id="REQ-9601",
            applicant_name="Domestic Ledger User",
            mobile_number="9876522201",
            house_address="Plot 1, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR9601",
            transformer=t1,
            meter_number="MTR-LDG-D1",
            original_meter_serial="ORIG-LDG-D1",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )
        comm_acc = CreateAccountForm(
            data={**self.valid_data, "username": "commledger1", "email": "cl1@example.com", "mobile_number": "9876522202"}
        ).save()
        comm_conn = ConnectionApplication.objects.create(
            user=comm_acc,
            application_id="REQ-9602",
            applicant_name="Titan Forging Works",
            mobile_number="9876522202",
            house_address="Unit 8, Industrial Estate",
            property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
            industry_type="Manufacturing / Factory",
            requested_load_kw="15.00",
            id_proof_number="GSTIN9602",
            transformer=t1,
            meter_number="MTR-LDG-C1",
            original_meter_serial="ORIG-LDG-C1",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        from django.utils import timezone
        from users.billing import record_power_usage
        cur_month = timezone.localdate().strftime("%Y-%m")
        prev_month = "2026-10" if cur_month != "2026-10" else "2026-09"

        # Seed Domestic Paid bill in current month & Unpaid bill in previous month
        dom_cur_paid = PowerBill.objects.create(
            connection=dom_conn,
            bill_number="INV-CUR-PAID-1",
            bill_type=PowerBill.TYPE_MONTHLY,
            billing_month=cur_month,
            units_billed=Decimal("10.00"),
            base_fare=Decimal("30.00"),
            rate_per_unit=Decimal("5.00"),
            energy_charge=Decimal("50.00"),
            total_amount=Decimal("80.00"),
            status=PowerBill.STATUS_PAID,
            transaction_id="TXN-CUR-PAID",
        )
        dom_prev_unpaid = PowerBill.objects.create(
            connection=dom_conn,
            bill_number="INV-PREV-UNP-1",
            bill_type=PowerBill.TYPE_MONTHLY,
            billing_month=prev_month,
            units_billed=Decimal("20.00"),
            base_fare=Decimal("30.00"),
            rate_per_unit=Decimal("5.00"),
            energy_charge=Decimal("100.00"),
            total_amount=Decimal("130.00"),
            status=PowerBill.STATUS_UNPAID,
        )

        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value

        # 1) Default Domestic Billing page shows ONLY Domestic things and ONLY current month data
        dom_page = admin_client.get(reverse("admin_billing")).content.decode("utf-8")
        self.assertIn('id="generateMonthlyBillsForm"', dom_page)
        self.assertNotIn('id="commercialOverviewLookupCard"', dom_page)
        self.assertIn("INV-CUR-PAID-1", dom_page)
        self.assertNotIn("INV-PREV-UNP-1", dom_page)

        # Calendar month filter to prev_month shows ONLY prev_month data
        prev_dom_page = admin_client.get(f"{reverse('admin_billing')}?ledger_month={prev_month}").content.decode("utf-8")
        self.assertIn("INV-PREV-UNP-1", prev_dom_page)
        self.assertNotIn("INV-CUR-PAID-1", prev_dom_page)

        # Paid / Unpaid filter narrows Domestic ledger accurately
        unpaid_only_cur = admin_client.get(
            f"{reverse('admin_billing')}?ledger_month={cur_month}&ledger_status=Unpaid"
        ).content.decode("utf-8")
        self.assertNotIn("INV-CUR-PAID-1", unpaid_only_cur)
        paid_only_cur = admin_client.get(
            f"{reverse('admin_billing')}?ledger_month={cur_month}&ledger_status=Paid"
        ).content.decode("utf-8")
        self.assertIn("INV-CUR-PAID-1", paid_only_cur)

        # Selecting "All Bills (Paid & Unpaid - All Months)" shows ALL months' bills in the table
        all_months_page = admin_client.get(
            f"{reverse('admin_billing')}?ledger_month={cur_month}&ledger_status=all"
        ).content.decode("utf-8")
        self.assertIn("INV-CUR-PAID-1", all_months_page)
        self.assertIn("INV-PREV-UNP-1", all_months_page)

        # 2) Clicking Commercial opens Commercial page showing ONLY Commercial things (no Domestic generator)
        comm_page = admin_client.get(reverse("admin_billing_commercial")).content.decode("utf-8")
        self.assertIn('id="commercialOverviewLookupCard"', comm_page)
        self.assertNotIn('id="generateMonthlyBillsForm"', comm_page)
        self.assertNotIn("INV-CUR-PAID-1", comm_page)

        # 3) Commercial consumer recharges BULK-50 (Rs 380 -> +50 Units) and consumes 5 Units -> 45.00 Units Left
        c_csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "commledger1", "password": self.valid_data["password"], "csrfmiddlewaretoken": c_csrf},
            follow=True,
        )
        u_tok = self.client.cookies["csrftoken"].value
        bulk_pay = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "bulk_recharge",
                "meter_number": "MTR-LDG-C1",
                "pack_code": "BULK-50",
                "payment_method": "UPI",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": u_tok,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        ).json()
        self.assertTrue(bulk_pay["ok"])
        self.client.post(
            reverse("toggle_house_power"),
            data={"action": "on", "csrfmiddlewaretoken": u_tok},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.client.post(
            reverse("record_power_usage"),
            data={"active_watts": "5000", "duration_hours": "1.0", "csrfmiddlewaretoken": u_tok},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        # 4) Entering ONLY Commercial Meter Number shows Recharged Amount, Units Got, & Live Remaining Units in table
        comm_lookup_html = admin_client.get(
            f"{reverse('admin_billing_commercial')}?meter_number=MTR-LDG-C1"
        ).content.decode("utf-8")
        self.assertIn('id="commercialMeterRechargesTable"', comm_lookup_html)
        self.assertNotIn('id="commOverviewUnitsInput"', comm_lookup_html)
        self.assertIn("Rs 380.00", comm_lookup_html)
        self.assertIn("+50.00 Units", comm_lookup_html)
        self.assertIn("45.00 Units", comm_lookup_html)
        self.assertIn("45.00 Units Left", comm_lookup_html)

        # Commercial Paid / Unpaid filter works cleanly
        comm_paid_html = admin_client.get(
            f"{reverse('admin_billing_commercial')}?ledger_month={cur_month}&ledger_status=Paid"
        ).content.decode("utf-8")
        self.assertIn(bulk_pay["bill_number"], comm_paid_html)
        comm_unpaid_html = admin_client.get(
            f"{reverse('admin_billing_commercial')}?ledger_month={cur_month}&ledger_status=Unpaid"
        ).content.decode("utf-8")
        self.assertNotIn(bulk_pay["bill_number"], comm_unpaid_html)

    def test_admin_revenue_separate_page_monthly_filter_and_totals(self):
        from datetime import datetime
        from django.utils import timezone
        from users.billing import record_power_usage

        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        dom_acc = CreateAccountForm(
            data={**self.valid_data, "username": "revdom1", "email": "revdom1@example.com", "mobile_number": "9876533301"}
        ).save()
        comm_acc = CreateAccountForm(
            data={**self.valid_data, "username": "revcomm1", "email": "revcomm1@example.com", "mobile_number": "9876533302"}
        ).save()

        cur_month = timezone.localdate().strftime("%Y-%m")
        prev_month = "2026-10" if cur_month != "2026-10" else "2026-09"
        prev_yr, prev_mo = map(int, prev_month.split("-"))
        prev_dt = timezone.make_aware(datetime(prev_yr, prev_mo, 15, 11, 0, 0))

        dom_conn = ConnectionApplication.objects.create(
            user=dom_acc,
            application_id="REQ-9701",
            applicant_name="Rev Domestic",
            mobile_number="9876533301",
            house_address="House 1, North",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR9701",
            transformer=t1,
            meter_number="MTR-REV-D1",
            original_meter_serial="ORIG-REV-D1",
            original_meter_verified=True,
            connection_fee_amount=Decimal("500.00"),
            connection_fee_paid=True,
            connection_transaction_id="TXN-FEE-PREV",
            connection_receipt_number="CONNFEE-PREV-1",
            connection_paid_at=prev_dt,
            status=ConnectionApplication.STATUS_CONNECTED,
        )
        comm_conn = ConnectionApplication.objects.create(
            user=comm_acc,
            application_id="REQ-9702",
            applicant_name="Rev Commercial",
            mobile_number="9876533302",
            house_address="Plot 9, South",
            property_type=ConnectionApplication.PROPERTY_COMMERCIAL,
            industry_type="IT / Office Park",
            requested_load_kw="10.00",
            id_proof_number="GSTIN9702",
            transformer=t1,
            meter_number="MTR-REV-C1",
            original_meter_serial="ORIG-REV-C1",
            original_meter_verified=True,
            connection_fee_amount=Decimal("1500.00"),
            connection_fee_paid=True,
            connection_transaction_id="TXN-FEE-CUR",
            connection_receipt_number="CONNFEE-CUR-1",
            connection_paid_at=timezone.now(),
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        # Current month bills: Rs 80 Domestic Paid + Rs 380 Commercial Paid (+ Rs 1500 conn fee = Rs 1960)
        PowerBill.objects.create(
            connection=dom_conn,
            bill_number="INV-REV-CUR-1",
            bill_type=PowerBill.TYPE_MONTHLY,
            billing_month=cur_month,
            units_billed=Decimal("10.00"),
            base_fare=Decimal("30.00"),
            rate_per_unit=Decimal("5.00"),
            energy_charge=Decimal("50.00"),
            total_amount=Decimal("80.00"),
            status=PowerBill.STATUS_PAID,
            transaction_id="TXN-REV-D-CUR",
            paid_at=timezone.now(),
        )
        PowerBill.objects.create(
            connection=comm_conn,
            bill_number="BLK-REV-CUR-1",
            bill_type=PowerBill.TYPE_PREPAID,
            billing_month=cur_month,
            units_billed=Decimal("50.00"),
            base_fare=Decimal("30.00"),
            rate_per_unit=Decimal("7.00"),
            energy_charge=Decimal("350.00"),
            total_amount=Decimal("380.00"),
            status=PowerBill.STATUS_PAID,
            transaction_id="TXN-REV-C-CUR",
            paid_at=timezone.now(),
        )
        # Previous month bill: Rs 230 Domestic Paid (+ Rs 500 conn fee = Rs 730) -> Grand Total = 1960 + 730 = Rs 2690
        PowerBill.objects.create(
            connection=dom_conn,
            bill_number="INV-REV-PREV-1",
            bill_type=PowerBill.TYPE_MONTHLY,
            billing_month=prev_month,
            units_billed=Decimal("40.00"),
            base_fare=Decimal("30.00"),
            rate_per_unit=Decimal("5.00"),
            energy_charge=Decimal("200.00"),
            total_amount=Decimal("230.00"),
            status=PowerBill.STATUS_PAID,
            transaction_id="TXN-REV-D-PREV",
            paid_at=prev_dt,
        )

        admin_client, _ = self._login_admin()

        # Billing pages must NOT display revenue totals/collected counters OR the Tariff KPI summary strip
        for billing_url in (reverse("admin_billing"), reverse("admin_billing_commercial")):
            b_html = admin_client.get(billing_url).content.decode("utf-8")
            self.assertNotIn("Paid Revenue", b_html)
            self.assertNotIn("2026-11 Collected", b_html)
            self.assertNotIn("Connected Domestic Homes", b_html)
            self.assertNotIn("Domestic Tariff Rate", b_html)

        # Tariff & Fare Settings page hosts Connected Homes/Industries, Tariff Rates & Paid/Unpaid month summary
        tariffs_html = admin_client.get(reverse("admin_billing_tariffs")).content.decode("utf-8")
        self.assertIn("Connected Domestic Homes", tariffs_html)
        self.assertIn("Domestic Tariff Rate", tariffs_html)
        self.assertIn(f"Paid ({cur_month})", tariffs_html)
        self.assertIn(f"Unpaid ({cur_month})", tariffs_html)

        # Dedicated Revenue page defaults to current month revenue + all-time grand total
        rev_cur_html = admin_client.get(reverse("admin_revenue")).content.decode("utf-8")
        self.assertIn('id="revenueWelcomeBanner"', rev_cur_html)
        self.assertIn("Rs 1960.00", rev_cur_html)
        self.assertIn("Rs 2690.00", rev_cur_html)
        self.assertIn("INV-REV-CUR-1", rev_cur_html)
        self.assertIn("BLK-REV-CUR-1", rev_cur_html)
        self.assertNotIn("INV-REV-PREV-1", rev_cur_html)

        # Filtering by prev_month updates selected month revenue while preserving Grand Total Revenue
        rev_prev_html = admin_client.get(f"{reverse('admin_revenue')}?revenue_month={prev_month}").content.decode("utf-8")
        self.assertIn("Rs 730.00", rev_prev_html)
        self.assertIn("Rs 2690.00", rev_prev_html)
        self.assertIn("INV-REV-PREV-1", rev_prev_html)
        self.assertNotIn("INV-REV-CUR-1", rev_prev_html)

    def test_domestic_previous_month_blocked_and_cumulative_reading_carryover(self):
        Transformer.ensure_defaults()
        t1 = Transformer.objects.first()
        dom_acc = CreateAccountForm(
            data={**self.valid_data, "username": "domcarry1", "email": "dc1@example.com", "mobile_number": "9876540101"}
        ).save()
        conn = ConnectionApplication.objects.create(
            user=dom_acc,
            application_id="REQ-9801",
            applicant_name="Neha Kulkarni",
            mobile_number="9876540101",
            house_address="42 Lakeview Colony",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="4.00",
            id_proof_number="AADHAR9801",
            transformer=t1,
            meter_number="MTR-CARRY-1",
            original_meter_serial="ORIG-CARRY-1",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )

        from django.utils import timezone
        from users.billing import record_power_usage
        cur_date = timezone.localdate()
        cur_month = cur_date.strftime("%Y-%m")
        prev_year, prev_m = (cur_date.year - 1, 12) if cur_date.month == 1 else (cur_date.year, cur_date.month - 1)
        prev_month = f"{prev_year:04d}-{prev_m:02d}"
        next_year, next_m = (cur_date.year + 1, 1) if cur_date.month == 12 else (cur_date.year, cur_date.month + 1)
        next_month = f"{next_year:04d}-{next_m:02d}"

        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value

        # 1) HTML input restricts min month to current month & POST for previous month is rejected
        dom_page = admin_client.get(reverse("admin_billing")).content.decode("utf-8")
        self.assertIn(f'min="{cur_month}"', dom_page)

        prev_resp = admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": prev_month, "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertEqual(prev_resp.status_code, 200)
        self.assertIn("Cannot generate Domestic monthly bills for previous month", prev_resp.content.decode("utf-8"))
        self.assertEqual(PowerBill.objects.filter(connection=conn, bill_type=PowerBill.TYPE_MONTHLY).count(), 0)

        # 2) Consume 50.00 Units in current month -> bill closes 0.00 -> 50.00 (50.00 Units billed)
        record_power_usage(conn.id, dom_acc, active_watts=5000, duration_hours=Decimal("10.00"))
        m1_resp = admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": cur_month, "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertEqual(m1_resp.status_code, 200)
        bill_m1 = PowerBill.objects.get(connection=conn, bill_type=PowerBill.TYPE_MONTHLY, billing_month=cur_month)
        self.assertEqual(bill_m1.previous_reading, Decimal("0.00"))
        self.assertEqual(bill_m1.current_reading, Decimal("50.00"))
        self.assertEqual(bill_m1.units_billed, Decimal("50.00"))
        self.assertEqual(bill_m1.arrears_amount, Decimal("0.00"))
        self.assertEqual(bill_m1.total_amount, Decimal("280.00"))  # Rs 30 base + 50 * Rs 5

        conn.refresh_from_db()
        self.assertEqual(conn.last_billed_reading, Decimal("50.00"))
        self.assertEqual(conn.total_units_consumed, Decimal("50.00"))
        self.assertEqual(conn.unbilled_units, Decimal("0.00"))

        # Add a second domestic meter AFTER cur_month is already generated (so cur_month bill was NOT generated for it)
        dom_acc_missed = CreateAccountForm(
            data={**self.valid_data, "username": "dommissed1", "email": "dm1@example.com", "mobile_number": "9876540102"}
        ).save()
        conn_missed = ConnectionApplication.objects.create(
            user=dom_acc_missed,
            application_id="REQ-9802",
            applicant_name="Rohan Deshmukh",
            mobile_number="9876540102",
            house_address="43 Lakeview Colony",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="4.00",
            id_proof_number="AADHAR9802",
            transformer=t1,
            meter_number="MTR-CARRY-2",
            original_meter_serial="ORIG-CARRY-2",
            original_meter_verified=True,
            status=ConnectionApplication.STATUS_CONNECTED,
        )
        # Uses 20.00 Units during cur_month (no bill generated) + 15.00 Units in next_month -> all 35.00 U billed in next_month
        record_power_usage(conn_missed.id, dom_acc_missed, active_watts=4000, duration_hours=Decimal("5.00"))
        record_power_usage(conn_missed.id, dom_acc_missed, active_watts=3000, duration_hours=Decimal("5.00"))

        # 3) Leave bill_m1 UNPAID and consume 35.00 more Units (total 85.00 U) on conn -> next_month carries BOTH reading (50->85) AND unpaid Rs 280.00 arrears!
        record_power_usage(conn.id, dom_acc, active_watts=5000, duration_hours=Decimal("7.00"))
        m2_resp = admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": next_month, "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertEqual(m2_resp.status_code, 200)

        bill_m1.refresh_from_db()
        self.assertEqual(bill_m1.status, PowerBill.STATUS_CARRIED_FORWARD)

        bill_m2 = PowerBill.objects.get(connection=conn, bill_type=PowerBill.TYPE_MONTHLY, billing_month=next_month)
        self.assertEqual(bill_m2.previous_reading, Decimal("50.00"))
        self.assertEqual(bill_m2.current_reading, Decimal("85.00"))
        self.assertEqual(bill_m2.units_billed, Decimal("35.00"))
        self.assertEqual(bill_m2.arrears_amount, Decimal("280.00"))
        self.assertEqual(bill_m2.carried_from_bills, bill_m1.bill_number)
        self.assertEqual(bill_m2.total_amount, Decimal("485.00"))  # (Rs 30 + 35*5 = Rs 205) + Rs 280 unpaid carry-forward

        # Missed-month meter gets all unbilled units carried forward into next_month (0.00 -> 35.00 = 35.00 U)
        bill_missed = PowerBill.objects.get(connection=conn_missed, bill_type=PowerBill.TYPE_MONTHLY, billing_month=next_month)
        self.assertEqual(bill_missed.previous_reading, Decimal("0.00"))
        self.assertEqual(bill_missed.current_reading, Decimal("35.00"))
        self.assertEqual(bill_missed.units_billed, Decimal("35.00"))
        self.assertEqual(bill_missed.total_amount, Decimal("205.00"))

        conn.refresh_from_db()
        self.assertEqual(conn.last_billed_reading, Decimal("85.00"))
        self.assertEqual(conn.total_units_consumed, Decimal("85.00"))
        self.assertEqual(conn.unbilled_units, Decimal("0.00"))

        # Re-running cur_month after next_month is already billed skips cleanly
        admin_client.post(
            reverse("admin_billing"),
            data={"action": "generate_monthly", "billing_month": cur_month, "csrfmiddlewaretoken": admin_csrf},
            follow=True,
        )
        self.assertEqual(PowerBill.objects.filter(connection=conn, bill_type=PowerBill.TYPE_MONTHLY).count(), 2)

        # User cannot separately pay the Carried Forward bill_m1, and billing portal shows consolidated Rs 485.00 payable
        u_csrf, _ = self._get_csrf()
        self.client.post(
            reverse("user_login"),
            data={"username": "domcarry1", "password": self.valid_data["password"], "csrfmiddlewaretoken": u_csrf},
            follow=True,
        )
        pay_old_resp = self.client.post(
            reverse("user_pay_bill"),
            data={
                "pay_kind": "monthly",
                "bill_id": bill_m1.id,
                "meter_number": "MTR-CARRY-1",
                "payment_method": "UPI",
                "simulate_outcome": "success",
                "csrfmiddlewaretoken": u_csrf,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(pay_old_resp.status_code, 400)
        self.assertIn("carried forward", pay_old_resp.json()["error"].lower())

        user_html = self.client.get(f"{reverse('user_billing')}?meter_number=MTR-CARRY-1").content.decode("utf-8")
        self.assertIn("Last Billed Meter Reading", user_html)
        self.assertIn("85.00 Units", user_html)
        self.assertIn("50.00 U &rarr; 85.00 U", user_html)
        self.assertIn("Rs 485.00", user_html)
        self.assertIn("+ Rs 280.00 Unpaid Carry-Forward", user_html)
        self.assertIn("Carried Forward", user_html)

    def test_admin_department_staff_dropdown_navigation_and_section_functions(self):
        admin_client, _ = self._login_admin()
        admin_csrf = admin_client.cookies["csrftoken"].value

        # 1) Admin navbar contains Department Staff dropdown with both options
        home_html = admin_client.get(reverse("admin_home")).content.decode("utf-8")
        self.assertIn('id="adminStaffDropdown"', home_html)
        self.assertIn("View Department Employees", home_html)
        self.assertIn("Add Employee into Department", home_html)

        # 2) View Department Employees opens Directory table function (not the Add form)
        view_html = admin_client.get(reverse("admin_employees")).content.decode("utf-8")
        self.assertIn("Department Employees Directory", view_html)
        self.assertIn("Manage Department Staff &amp; Toggle Login Status", view_html)
        self.assertNotIn("empFullName", view_html)

        # 3) Add Employee into Department opens Add form function (not the Directory table)
        add_html = admin_client.get(reverse("admin_add_employee")).content.decode("utf-8")
        self.assertIn("Add Department Employee &amp; Assign Transformer Zone", add_html)
        self.assertIn("empFullName", add_html)
        self.assertNotIn("Department Employees Directory", add_html)

        # 4) Submitting to admin_add_employee creates the employee and redirects to directory
        create_resp = admin_client.post(
            reverse("admin_add_employee"),
            data={
                "full_name": "Kavita Sharma",
                "username": "kavita_dept",
                "email": "kavita@dept.gov",
                "mobile_number": "9876543299",
                "designation": "Meter Inspector",
                "password": "Password@123",
                "confirm_password": "Password@123",
                "is_active": "on",
                "csrfmiddlewaretoken": admin_csrf,
            },
            follow=True,
        )
        self.assertEqual(create_resp.status_code, 200)
        self.assertRedirects(create_resp, reverse("admin_employees"))
        dir_html = create_resp.content.decode("utf-8")
        self.assertIn("Department Employees Directory", dir_html)
        self.assertIn("Kavita Sharma", dir_html)
        self.assertIn("kavita_dept", dir_html)

    def test_admin_connections_dropdown_navigation_and_section_functions(self):
        admin_client, _ = self._login_admin()

        # 1) Admin navbar contains Connections & Meters dropdown with all 4 options
        home_html = admin_client.get(reverse("admin_home")).content.decode("utf-8")
        self.assertIn('id="adminConnectionsDropdown"', home_html)
        self.assertIn("Issue Original Meters", home_html)
        self.assertIn("Premises Power Control", home_html)
        self.assertIn("Connection Applications Ledger", home_html)
        self.assertIn("All Connections &amp; Controls", home_html)

        # 2) Issue Original Meters opens only the cross-check & meter issuance function
        issue_html = admin_client.get(reverse("admin_connections_issue_meters")).content.decode("utf-8")
        self.assertIn('id="adminStageApprove"', issue_html)
        self.assertIn("Cross-Check Verified Requests &amp; Issue Original Meters", issue_html)
        self.assertNotIn('id="connectedPowerControlsSection"', issue_html)
        self.assertNotIn('id="adminAllAppsLedger"', issue_html)

        # 3) Premises Power Control opens only individual power controls function
        power_html = admin_client.get(reverse("admin_connections_power_controls")).content.decode("utf-8")
        self.assertIn('id="connectedPowerControlsSection"', power_html)
        self.assertIn("Individual Home &amp; Commercial Industry Power Cut", power_html)
        self.assertNotIn('id="adminStageApprove"', power_html)
        self.assertNotIn('id="adminAllAppsLedger"', power_html)

        # 4) Applications Ledger opens only connection ledger function
        ledger_html = admin_client.get(reverse("admin_connections_ledger")).content.decode("utf-8")
        self.assertIn('id="adminAllAppsLedger"', ledger_html)
        self.assertIn("Complete Consumer Connection Applications Ledger", ledger_html)
        self.assertNotIn('id="adminStageApprove"', ledger_html)
        self.assertNotIn('id="connectedPowerControlsSection"', ledger_html)

        # 5) All Operations renders complete combined overview
        all_html = admin_client.get(reverse("admin_connections")).content.decode("utf-8")
        self.assertIn('id="adminStageApprove"', all_html)
        self.assertIn('id="connectedPowerControlsSection"', all_html)
        self.assertIn('id="adminAllAppsLedger"', all_html)

    def test_admin_settings_news_notifications_and_llm_database_configuration(self):
        """Test Settings navigation, dynamic News & Operation Notifications on base.html, and database LLM configuration."""
        admin_client, _ = self._login_admin()

        # 1) Admin navbar contains Settings dropdown with links to news, notifications, llm
        settings_html = admin_client.get(reverse("admin_settings")).content.decode("utf-8")
        self.assertIn('id="adminSettingsDropdown"', settings_html)
        self.assertIn(reverse("admin_settings_news"), settings_html)
        self.assertIn(reverse("admin_settings_notifications"), settings_html)
        self.assertIn(reverse("admin_settings_llm"), settings_html)
        self.assertIn("News &amp; Public Announcements", settings_html)
        self.assertIn("Operation &amp; Maintenance Alerts", settings_html)
        self.assertIn("AI &amp; LLM Configurations", settings_html)

        # 2) Publish News Announcement from Admin Settings
        news_resp = admin_client.post(
            reverse("admin_settings"),
            data={
                "action": "add_news",
                "title": "Solar Subsidy DISCOM Scheme 2026",
                "badge_label": "Subsidy",
                "content": "Apply online for 40% government subsidy on domestic solar rooftop installation.",
                "link_url": "https://energy.gov/solar",
                "is_ticker": "1",
                "is_active": "1",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        self.assertEqual(news_resp.status_code, 200)
        news_item = SiteNewsAnnouncement.objects.get(title="Solar Subsidy DISCOM Scheme 2026")
        self.assertTrue(news_item.is_active)
        self.assertEqual(news_item.badge_label, "Subsidy")

        # 3) News immediately appears dynamically on public base.html
        public_resp = self.client.get(reverse("home"))
        self.assertEqual(public_resp.status_code, 200)
        pub_html = public_resp.content.decode("utf-8")
        self.assertIn("Solar Subsidy DISCOM Scheme 2026", pub_html)
        self.assertIn("Subsidy", pub_html)
        self.assertIn("Apply online for 40% government subsidy", pub_html)
        self.assertIn("publicNoticeBar", pub_html)

        # 4) Broadcast Operation & Maintenance Notification from Admin Settings
        tf = Transformer.objects.create(code="T-99", name="Industrial Feeder T-99", zone="Heavy Industrial Sector", is_power_on=True)
        note_resp = admin_client.post(
            reverse("admin_settings"),
            data={
                "action": "add_notification",
                "title": "Scheduled Substation Transformer Maintenance T-99",
                "category": OperationMaintenanceNotification.CATEGORY_MAINTENANCE,
                "severity": OperationMaintenanceNotification.SEVERITY_WARNING,
                "transformer_id": tf.id,
                "affected_areas": "Heavy Industrial Sector Phase 1 & 2",
                "message": "Power cut scheduled from 10:00 to 14:00 on Sunday for breaker overhaul.",
                "is_active": "1",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        self.assertEqual(note_resp.status_code, 200)
        note = OperationMaintenanceNotification.objects.get(title="Scheduled Substation Transformer Maintenance T-99")
        self.assertTrue(note.is_active)
        self.assertEqual(note.severity, OperationMaintenanceNotification.SEVERITY_WARNING)

        # 5) Operation notice reflects on public base.html in ticker and alert drawer
        pub_html_2 = self.client.get(reverse("home")).content.decode("utf-8")
        self.assertIn("Scheduled Substation Transformer Maintenance T-99", pub_html_2)
        self.assertIn("Heavy Industrial Sector Phase 1 &amp; 2", pub_html_2)
        self.assertIn("Power cut scheduled from 10:00 to 14:00", pub_html_2)
        self.assertIn("alertsDrawer", pub_html_2)

        # 6) Toggle news and notification inactive -> no longer displayed on base.html
        admin_client.post(
            reverse("admin_settings"),
            data={"action": "toggle_news", "news_id": news_item.id, "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value},
            follow=True,
        )
        admin_client.post(
            reverse("admin_settings"),
            data={"action": "toggle_notification", "notification_id": note.id, "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value},
            follow=True,
        )
        pub_html_3 = self.client.get(reverse("home")).content.decode("utf-8")
        self.assertNotIn("Solar Subsidy DISCOM Scheme 2026", pub_html_3)
        self.assertNotIn("Scheduled Substation Transformer Maintenance T-99", pub_html_3)

        # 7) Add LLM Configuration to Database (no .env required)
        llm_resp = admin_client.post(
            reverse("admin_settings"),
            data={
                "action": "save_llm",
                "name": "DISCOM Primary Gemini Assistant",
                "provider": LLMConfiguration.PROVIDER_GEMINI,
                "model_name": "gemini-1.5-flash",
                "api_key": "AIzaSyFakeKeyForTesting12345678",
                "api_url": "",
                "temperature": "0.65",
                "max_tokens": "2048",
                "system_prompt": "You are the official voice assistant for DISCOM.",
                "is_active": "1",
                "is_default": "1",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        self.assertEqual(llm_resp.status_code, 200)

        # 8) Verify LLM config stored in database and fetched by get_active_llm_config()
        active_llm = get_active_llm_config()
        self.assertEqual(active_llm.name, "DISCOM Primary Gemini Assistant")
        self.assertEqual(active_llm.provider, LLMConfiguration.PROVIDER_GEMINI)
        self.assertEqual(active_llm.model_name, "gemini-1.5-flash")
        self.assertEqual(active_llm.temperature, Decimal("0.65"))
        self.assertEqual(active_llm.max_tokens, 2048)
        self.assertTrue(active_llm.is_default)
        self.assertEqual(active_llm.api_key, "AIzaSyFakeKeyForTesting12345678")

        # 9) Test connection endpoint returns JSON response
        test_api_resp = admin_client.get(reverse("admin_settings_llm_test_api", args=[active_llm.id]))
        self.assertEqual(test_api_resp.status_code, 200)
        self.assertIn("ok", test_api_resp.json())

    def test_single_line_navbar_and_base_html_background_and_video_settings(self):
        """Test that Admin navbar options stay in a single line, and Settings updates base.html background and video."""
        admin_client, _ = self._login_admin()

        # 1) Admin navbar has flex-wrap: nowrap and single-line styling
        admin_html = admin_client.get(reverse("admin_home")).content.decode("utf-8")
        self.assertIn(".admin-nav-links {", admin_html)
        self.assertIn("flex-wrap: nowrap;", admin_html)
        self.assertIn("white-space: nowrap;", admin_html)
        self.assertNotIn('class="nav-step-num"', admin_html)
        self.assertIn('class="nav-count-badge"', admin_html)
        self.assertIn(reverse("admin_settings_media"), admin_html)

        # 2) Base.html public navbar also has single-line styling
        public_html = self.client.get(reverse("home")).content.decode("utf-8")
        self.assertIn(".nav-links {", public_html)
        self.assertIn("flex-wrap: nowrap;", public_html)

        # 3) Access Media settings tab
        media_page = admin_client.get(reverse("admin_settings_media")).content.decode("utf-8")
        self.assertIn("Hero Background Image &amp; Voice Agent Video on base.html", media_page)
        self.assertIn("Update base.html Background &amp; Video", media_page)

        # 4) Update base.html background image, video, and title
        update_resp = admin_client.post(
            reverse("admin_settings"),
            data={
                "action": "save_media",
                "hero_bg_image_url": "/static/images/custom-solar-grid-bg.png",
                "hero_video_url": "/static/videos/custom-ai-voice-agent-hd.mp4",
                "hero_video_poster_url": "/static/images/custom-solar-poster.png",
                "hero_heading": "Southern Power Distribution Co.\nAI Voice Assistant",
                "hero_subheading": "Instant 24x7 power outage resolution & interactive billing telemetry.",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        self.assertEqual(update_resp.status_code, 200)

        # 5) Verify base.html reflects the new background image, video source, and custom headings
        pub_updated = self.client.get(reverse("home")).content.decode("utf-8")
        self.assertIn("/static/images/custom-solar-grid-bg.png", pub_updated)
        self.assertIn("/static/videos/custom-ai-voice-agent-hd.mp4", pub_updated)
        self.assertIn("Southern Power Distribution Co.", pub_updated)
        self.assertIn("Instant 24x7 power outage resolution", pub_updated)

        # 6) Reset media back to defaults
        reset_resp = admin_client.post(
            reverse("admin_settings"),
            data={
                "action": "reset_media",
                "csrfmiddlewaretoken": admin_client.cookies["csrftoken"].value,
            },
            follow=True,
        )
        self.assertEqual(reset_resp.status_code, 200)
        pub_reset = self.client.get(reverse("home")).content.decode("utf-8")
        self.assertIn("/static/images/electricity-grid-background.png", pub_reset)
        self.assertIn("/static/videos/ai-electricity-voice-agent.mp4", pub_reset)

    def test_admin_tariff_and_connection_cost_cards_and_updates(self):
        """Test Tariff & Fare Settings mentions Base Price, Unit Rate, and Connection Cost in KPI cards and updates them."""
        admin_client, _ = self._login_admin()
        tariffs_page = admin_client.get(reverse("admin_billing_tariffs")).content.decode("utf-8")

        # 1) Verify KPI cards explicitly mention Current Base Price, Unit Rate, Connection Cost, Paid, and Unpaid
        self.assertIn("Current Base Price", tariffs_page)
        self.assertIn("Domestic Tariff Rate (Current Price)", tariffs_page)
        self.assertIn("Domestic Connection Cost", tariffs_page)
        self.assertIn("Commercial Tariff Rate (Current Price)", tariffs_page)
        self.assertIn("Commercial Connection Cost", tariffs_page)
        self.assertIn("Fixed Monthly Meter Base", tariffs_page)
        self.assertIn("New Connection Fee", tariffs_page)
        self.assertIn("Paid (", tariffs_page)
        self.assertIn("Unpaid (", tariffs_page)

        # 2) Post update for Base Price, Unit Prices, and New Connection Costs
        admin_csrf = admin_client.cookies["csrftoken"].value
        update_resp = admin_client.post(
            reverse("admin_billing_tariffs"),
            data={
                "action": "update_tariff",
                "base_fare_rs": "42.50",
                "domestic_rate_per_unit": "5.75",
                "commercial_rate_per_unit": "8.25",
                "domestic_connection_fee": "650.00",
                "commercial_connection_fee": "1850.00",
                "revision_note": "Quarterly Regulatory Tariff & Connection Fee Revision",
                "csrfmiddlewaretoken": admin_csrf,
            },
            follow=True,
        )
        self.assertEqual(update_resp.status_code, 200)

        # 3) Check database reflects updated values
        tariff = TariffSetting.get_active()
        self.assertEqual(tariff.base_fare_rs, Decimal("42.50"))
        self.assertEqual(tariff.domestic_rate_per_unit, Decimal("5.75"))
        self.assertEqual(tariff.commercial_rate_per_unit, Decimal("8.25"))
        self.assertEqual(tariff.domestic_connection_fee, Decimal("650.00"))
        self.assertEqual(tariff.commercial_connection_fee, Decimal("1850.00"))

        # 4) Verify rendered page reflects updated Base Price, Connection Costs, and Unit Rates in the cards
        updated_html = admin_client.get(reverse("admin_billing_tariffs")).content.decode("utf-8")
        self.assertIn("Rs 42.50", updated_html)
        self.assertIn("Rs 5.75 / Unit", updated_html)
        self.assertIn("Rs 8.25 / Unit", updated_html)
        self.assertIn("Rs 650.00", updated_html)
        self.assertIn("Rs 1850.00", updated_html)
        self.assertIn("Quarterly Regulatory Tariff &amp; Connection Fee Revision", updated_html)

    def test_user_home_notifications_maintenance_bill_reminders_and_live_status(self):
        """Test that user home page displays real-time Live Grid status, Maintenance notices, and Bill reminders."""
        Transformer.ensure_defaults()
        t1 = Transformer.objects.get(code="T-01")
        acc = CreateAccountForm(data={
            "full_name": "Ravi Kumar",
            "username": "ravikumar_notif",
            "email": "ravi@notif.test",
            "mobile_number": "9876501234",
            "password": "UserPass@123",
            "confirm_password": "UserPass@123",
        }).save()

        user_client = Client(enforce_csrf_checks=True)
        u_csrf, _ = self._get_csrf(user_client)
        user_client.post(
            reverse("user_login"),
            data={"username": "ravikumar_notif", "password": "UserPass@123", "csrfmiddlewaretoken": u_csrf},
            follow=True,
        )

        # 1) When user has no connection, home page shows onboarding live notification & header bell
        home_empty = user_client.get(reverse("user_home")).content.decode("utf-8")
        self.assertIn("userNotificationsCenter", home_empty)
        self.assertIn("userHeaderBellBtn", home_empty)
        self.assertIn("navNotificationsLink", home_empty)
        self.assertIn("Register for New Electricity Connection", home_empty)

        # 2) Create connection application with pending fee -> displays Bill Reminder for Connection Fee
        conn = ConnectionApplication.objects.create(
            user=acc,
            application_id="REQ-9101",
            application_number="APP-2026-9101",
            applicant_name="Ravi Kumar",
            mobile_number="9876501234",
            house_address="Plot 5, Hitech City, Hyderabad",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            requested_load_kw="2.00",
            id_proof_number="AADHAAR9101",
            connection_fee_amount=Decimal("500.00"),
            connection_fee_paid=False,
            payment_verified=False,
            status=ConnectionApplication.STATUS_APPLIED,
        )
        home_fee_due = user_client.get(reverse("user_home")).content.decode("utf-8")
        self.assertIn("Connection Charges Pending: Rs 500.00", home_fee_due)
        self.assertIn("Pay Fee (Rs 500.00)", home_fee_due)

        # 3) Connect premise to Transformer T-01
        conn.connection_fee_paid = True
        conn.payment_verified = True
        conn.transformer = t1
        conn.meter_number = "MTR-NOTIF-01"
        conn.status = ConnectionApplication.STATUS_CONNECTED
        conn.save()

        # Generate an unpaid domestic monthly bill
        PowerBill.objects.create(
            connection=conn,
            bill_number="INV-NOTIF-101",
            bill_type=PowerBill.TYPE_MONTHLY,
            billing_month="2026-11",
            units_billed=Decimal("50.00"),
            base_fare=Decimal("30.00"),
            rate_per_unit=Decimal("5.00"),
            energy_charge=Decimal("250.00"),
            total_amount=Decimal("280.00"),
            status=PowerBill.STATUS_UNPAID,
        )

        # Create an approved maintenance schedule on T-01
        dept_emp = DepartmentEmployee.objects.create(
            employee_id="DEPT-101",
            full_name="Sunil Engineer",
            username="sunil_eng",
            email="sunil@dept.gov",
            mobile_number="9876543101",
            designation="Substation Engineer",
            is_active=True,
        )
        dept_emp.set_password("DeptPass@123")
        dept_emp.save()

        MaintenanceSchedule.objects.create(
            schedule_id="MNT-9001",
            requested_by=dept_emp,
            scope=MaintenanceSchedule.SCOPE_TRANSFORMER,
            transformer=t1,
            title="T-01 Busbar Routine Maintenance",
            reason="Safety isolator inspection",
            scheduled_start=timezone.now() + timezone.timedelta(hours=2),
            scheduled_end=timezone.now() + timezone.timedelta(hours=5),
            status=MaintenanceSchedule.STATUS_APPROVED,
        )

        # Also create a DISCOM OperationMaintenanceNotification
        OperationMaintenanceNotification.objects.create(
            title="Transformer Feeder Maintenance Advisory",
            message="Planned power line upgrade on Madhapur East Sector.",
            category=OperationMaintenanceNotification.CATEGORY_MAINTENANCE,
            severity=OperationMaintenanceNotification.SEVERITY_WARNING,
            related_transformer=t1,
            affected_areas="Hitech City & Madhapur",
            is_active=True,
        )

        # Check user home: Live supply energized, Unpaid bill reminder, and Maintenance notices all appear!
        home_connected = user_client.get(reverse("user_home")).content.decode("utf-8")
        self.assertIn("Live Notifications, Maintenance &amp; Bill Reminders", home_connected)
        self.assertIn("Grid Power Active &amp; Energized", home_connected)
        self.assertIn("Electricity Bill Due: 2026-11", home_connected)
        self.assertIn("INV-NOTIF-101", home_connected)
        self.assertIn("Rs 280.00", home_connected)
        self.assertIn("Pay Online (Rs 280.00)", home_connected)
        self.assertIn("Maintenance Notice: T-01 Busbar Routine Maintenance", home_connected)
        self.assertIn("MNT-9001", home_connected)
        self.assertIn("Transformer Feeder Maintenance Advisory", home_connected)

        # 4) Test Live Power Cut on Premise: notification instantly reflects POWER CUT ACTIVE
        conn.is_power_cut = True
        conn.power_cut_remark = "Safety disconnect due to meter relocation"
        conn.save()
        home_cut = user_client.get(reverse("user_home")).content.decode("utf-8")
        self.assertIn("POWER CUT ACTIVE", home_cut)
        self.assertIn("Premise Power Cut on Meter MTR-NOTIF-01", home_cut)
        self.assertIn("Safety disconnect due to meter relocation", home_cut)

        # 5) Test Feeder Outage: when T-01 is turned off, user gets FEEDER OUTAGE notification
        conn.is_power_cut = False
        conn.save()
        t1.set_power(False)
        home_tf_down = user_client.get(reverse("user_home")).content.decode("utf-8")
        self.assertIn("FEEDER OUTAGE", home_tf_down)
        self.assertIn("Substation Feeder T-01 Supply Down", home_tf_down)











