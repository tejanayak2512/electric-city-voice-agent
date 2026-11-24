import json
from decimal import Decimal
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from admins.models import MaintenanceSchedule, TariffSetting, Transformer
from complaints.models import Complaint
from llm_config.models import LLMConfiguration
from users.models import ConnectionApplication, PowerBill, UserAccount, UserNotification
from voice_agent.agent import lookup_complaint, process_call_turn, raise_complaint
from voice_agent.knowledge_base import get_live_knowledge_base
from voice_agent.models import CallCenterAgent, VoiceCallMessage, VoiceCallSession


class VoiceAgentCallingTests(TestCase):
    """Test suite for the real-time AI Voice Calling Agent, Complaint Registration,

    Status Verification, and Knowledge Base grounding from database.
    """

    def setUp(self):
        self.client = Client()

        # Seed Active Tariff
        self.tariff = TariffSetting.get_active()
        self.tariff.base_fare_rs = Decimal("50.00")
        self.tariff.domestic_rate_per_unit = Decimal("7.00")
        self.tariff.commercial_rate_per_unit = Decimal("12.00")
        self.tariff.domestic_connection_fee = Decimal("1500.00")
        self.tariff.commercial_connection_fee = Decimal("5000.00")
        self.tariff.save()

        # Seed Transformer
        self.tf = Transformer.objects.create(
            code="T-TEST",
            name="Test Substation Transformer",
            substation_name="Test 33/11kV Substation",
            zone="Central Zone",
            capacity_kva=500,
            is_power_on=True,
        )

        # Seed User Account
        self.user = UserAccount.objects.create(
            full_name="Rajesh Kumar",
            username="rajesh2026",
            email="rajesh@test.com",
            mobile_number="9876543210",
            status=UserAccount.STATUS_ACTIVE,
            house_power_on=True,
        )
        self.user.set_password("SecurePass123!")
        self.user.save()

        # Seed Active Connection
        self.conn = ConnectionApplication.objects.create(
            user=self.user,
            application_id="APP-TEST-001",
            application_number="CA-TEST-001",
            applicant_name="Rajesh Kumar",
            mobile_number="9876543210",
            house_address="Plot 42, Green Avenue, Sector 5",
            property_type=ConnectionApplication.PROPERTY_DOMESTIC,
            meter_number="MTR-998877",
            status=ConnectionApplication.STATUS_CONNECTED,
            transformer=self.tf,
            is_power_cut=False,
        )

        # Seed Power Bill
        self.bill = PowerBill.objects.create(
            connection=self.conn,
            bill_number="BILL-TEST-001",
            billing_month="2026-11",
            units_billed=Decimal("120.00"),
            base_fare=Decimal("50.00"),
            rate_per_unit=Decimal("7.00"),
            energy_charge=Decimal("840.00"),
            total_amount=Decimal("890.00"),
            status=PowerBill.STATUS_UNPAID,
            bill_type=PowerBill.TYPE_MONTHLY,
        )

        # Seed Paid Power Bill for invoice retrieval tests
        self.paid_bill = PowerBill.objects.create(
            connection=self.conn,
            bill_number="BILL-TEST-PAID-002",
            billing_month="2026-10",
            units_billed=Decimal("50.00"),
            base_fare=Decimal("40.00"),
            rate_per_unit=Decimal("7.00"),
            energy_charge=Decimal("350.00"),
            total_amount=Decimal("390.00"),
            status=PowerBill.STATUS_PAID,
            bill_type=PowerBill.TYPE_MONTHLY,
            payment_method="Online DISCOM Gateway",
            transaction_id="TXN-TEST-998811",
            paid_at=timezone.now(),
        )

        # Seed Database LLM Configuration
        self.llm_cfg = LLMConfiguration.objects.create(
            name="Test Mock Provider",
            provider=LLMConfiguration.PROVIDER_CUSTOM,
            model_name="mock-gpt-4o-mini",
            api_key="",
            is_active=True,
            is_default=True,
            system_prompt="You are an AI assistant for the Electricity Department.",
        )

    def test_complaint_model_identifier_generation(self):
        """Verify Complaint auto-generates CMP-YYYYMM-XXXX and 8-character tracking ID."""
        cmp = Complaint.objects.create(
            caller_name="Rajesh Kumar",
            location="Sector 5 Green Avenue",
            issue_type=Complaint.ISSUE_POWER_OUTAGE,
        )
        self.assertTrue(cmp.complaint_number.startswith("CMP-"))
        self.assertEqual(len(cmp.complaint_id), 8)
        self.assertEqual(cmp.status, Complaint.STATUS_REGISTERED)
        self.assertEqual(cmp.priority, Complaint.PRIORITY_NORMAL)

    def test_raise_complaint_hazard_priority_escalation(self):
        """Verify raise_complaint elevates sparking / wire snap to CRITICAL_HAZARD."""
        cmp = raise_complaint(
            caller_name="Rajesh Kumar",
            location="Sector 5 Green Avenue",
            issue_type=Complaint.ISSUE_TRANSFORMER_HAZARD,
            hazard_observed="Heavy sparking and loud explosions on pole transformer",
            user_account=self.user,
            connection=self.conn,
        )
        self.assertEqual(cmp.priority, Complaint.PRIORITY_CRITICAL)
        self.assertIn("Immediate Emergency", cmp.estimated_resolution)

    def test_lookup_complaint_by_number_id_and_name(self):
        """Verify lookup_complaint finds complaints by number, 8-character ID, or name."""
        cmp = raise_complaint(
            caller_name="Rajesh Kumar",
            location="Sector 5",
            issue_type=Complaint.ISSUE_POWER_OUTAGE,
            user_account=self.user,
        )
        # 1. Lookup by full complaint number
        res1 = lookup_complaint(cmp.complaint_number)
        self.assertTrue(len(res1) >= 1)
        self.assertEqual(res1[0]["complaint_number"], cmp.complaint_number)

        # 2. Lookup by 8-character ID
        res2 = lookup_complaint(cmp.complaint_id)
        self.assertTrue(len(res2) >= 1)
        self.assertEqual(res2[0]["complaint_id"], cmp.complaint_id)

        # 3. Lookup by caller name
        res3 = lookup_complaint(query="random", caller_name="Rajesh")
        self.assertTrue(len(res3) >= 1)

    def test_live_knowledge_base_extraction(self):
        """Verify get_live_knowledge_base accurately pulls live tariffs, transformer, and user state."""
        kb = get_live_knowledge_base(user_account=self.user)
        self.assertEqual(kb["tariffs"]["domestic_unit_rate"], 7.00)
        self.assertEqual(kb["tariffs"]["domestic_base_price"], 50.00)
        self.assertIsNotNone(kb["caller_profile"])
        self.assertEqual(kb["caller_profile"]["meter_number"], "MTR-998877")
        self.assertTrue(kb["caller_profile"]["effective_power_supply"])
        self.assertEqual(len(kb["caller_profile"]["unpaid_bills"]), 1)

    def test_telephony_api_call_lifecycle(self):
        """Verify start-call, sending speech messages, and end-call endpoints."""
        session = self.client.session
        session["user_id"] = self.user.id
        session.save()

        # 1. Start Call
        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        self.assertEqual(start_res.status_code, 200)
        data = start_res.json()
        self.assertTrue(data["success"])
        session_id = data["session_id"]
        self.assertIn("Welcome", data["welcome_message"])

        call_sess = VoiceCallSession.objects.get(session_id=session_id)
        self.assertEqual(call_sess.status, VoiceCallSession.STATUS_ACTIVE)

        # 2. Send Message: Check Bill Query
        msg_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "What is my latest electricity bill?"},
            content_type="application/json",
        )
        self.assertEqual(msg_res.status_code, 200)
        msg_data = msg_res.json()
        self.assertTrue(msg_data["success"])
        self.assertIn("890.00", msg_data["reply"])
        self.assertIn("2026-11", msg_data["reply"])

        # 3. Send Message: Register Outage Complaint
        outage_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={
                "session_id": session_id,
                "message": "Please register a complaint. No power at my house since 1 hour.",
            },
            content_type="application/json",
        )
        self.assertEqual(outage_res.status_code, 200)
        outage_data = outage_res.json()
        self.assertTrue(outage_data["success"])
        self.assertIsNotNone(outage_data.get("registered_complaint"))
        comp_num = outage_data["registered_complaint"]["complaint_number"]
        comp_id = outage_data["registered_complaint"]["complaint_id"]
        self.assertTrue(comp_num.startswith("CMP-"))
        self.assertEqual(len(comp_id), 8)

        # 4. Send Message: Track the registered complaint
        track_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": f"Check status of {comp_id}"},
            content_type="application/json",
        )
        self.assertEqual(track_res.status_code, 200)
        track_data = track_res.json()
        self.assertTrue(track_data["success"])
        self.assertIn(comp_num, track_data["reply"])

        # 5. End Call
        end_res = self.client.post(
            reverse("api_voice_agent_end_call"),
            data={"session_id": session_id},
            content_type="application/json",
        )
        self.assertEqual(end_res.status_code, 200)
        end_data = end_res.json()
        self.assertTrue(end_data["success"])

        call_sess.refresh_from_db()
        self.assertEqual(call_sess.status, VoiceCallSession.STATUS_COMPLETED)

    def test_caller_verification_and_connection_retrieval(self):
        """Verify logged-in caller details (meter number, request ID, address) are automatically verified."""
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # 1. Start Call and verify automatic caller identity grounding
        res = self.client.post(reverse("api_voice_agent_start_call"))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["caller_verified"])
        self.assertIn("Rajesh Kumar", data["welcome_message"])
        self.assertIn("MTR-998877", data["welcome_message"])
        self.assertIn("APP-TEST-001", data["welcome_message"])

        # 2. Query for meter number and request ID
        msg_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": data["session_id"], "message": "What is my meter number and request id?"},
            content_type="application/json",
        )
        self.assertEqual(msg_res.status_code, 200)
        msg_data = msg_res.json()
        self.assertTrue(msg_data["success"])
        self.assertIn("APP-TEST-001", msg_data["reply"])
        self.assertIn("MTR-998877", msg_data["reply"])

    def test_voice_agent_page_render_and_dropdown_navigation(self):
        """Verify user voice agent call page renders with breadcrumbs and telephony card."""
        session = self.client.session
        session["user_id"] = self.user.id
        session.save()

        response = self.client.get(reverse("user_voice_agent"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "24/7 AI-Powered Voice Calling Agent")
        self.assertContains(response, "telephonyCard")
        self.assertContains(response, "startCallBtn")
        self.assertContains(response, "endCallBtn")
        self.assertContains(response, "navVoiceAgentBtn")
        self.assertContains(response, "MTR-998877")
        self.assertContains(response, "APP-TEST-001")

    def test_customer_care_telephone_flow_and_spoken_phonetics(self):
        """Verify 1-to-1 telephone customer care conversational state machine:

        - Multi-turn outage diagnosis (inquiry -> user slot answers -> ticket creation)
        - Agent persona identification (Officer Priya)
        - Professional closing & courtesy wrap-up ("Thank you, that's all")
        - Spoken phonetic number & currency transformation
        """
        from voice_agent.agent import format_spoken_text

        # 1. Phonetic speech transformation testing
        raw_text = "Your Complaint Number is CMP-202611-0002 with Tracking ID: 8F2A1C04 for Rs 890.00 at meter MTR-998877."
        spoken = format_spoken_text(raw_text)
        self.assertIn("C M P", spoken)
        self.assertIn("Tracking I D", spoken)
        self.assertIn("890 rupees", spoken)
        self.assertIn("Meter number", spoken)

        # 2. Start Call
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        res = self.client.post(reverse("api_voice_agent_start_call"))
        data = res.json()
        session_id = data["session_id"]
        self.assertIn("welcome_spoken_text", data)

        # 3. Agent identity inquiry ("Who is speaking?")
        ident_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "Who is speaking?"},
            content_type="application/json",
        )
        ident_data = ident_res.json()
        self.assertTrue(ident_data["success"])
        self.assertIn("Officer Priya", ident_data["reply"])
        self.assertEqual(ident_data["action_taken"], "AGENT_IDENTITY")

        # 4. Multi-turn Outage: Turn 1 - Initial outage report
        outage_turn1 = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "Power cut at my house"},
            content_type="application/json",
        )
        t1_data = outage_turn1.json()
        self.assertTrue(t1_data["success"])
        self.assertIn("neighboring houses", t1_data["reply"])

        # Multi-turn Outage: Turn 2 - User provides diagnostic slot answers
        outage_turn2 = self.client.post(
            reverse("api_voice_agent_message"),
            data={
                "session_id": session_id,
                "message": "It's the entire street, power has been out for 30 minutes, no sparks",
            },
            content_type="application/json",
        )
        t2_data = outage_turn2.json()
        self.assertTrue(t2_data["success"])
        self.assertIsNotNone(t2_data.get("registered_complaint"))
        self.assertTrue(t2_data["registered_complaint"]["complaint_number"].startswith("CMP-"))
        self.assertIn(t2_data["registered_complaint"]["complaint_number"], t2_data["reply"])

        # 5. Repeat Complaint inquiry ("Can you repeat that?")
        repeat_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "Can you repeat that number?"},
            content_type="application/json",
        )
        rep_data = repeat_res.json()
        self.assertTrue(rep_data["success"])
        self.assertIn(t2_data["registered_complaint"]["complaint_number"], rep_data["reply"])

        # 6. Customer Care Call Closing ("Thank you, that's all")
        close_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "Thank you Priya, that is all"},
            content_type="application/json",
        )
        close_data = close_res.json()
        self.assertTrue(close_data["success"])
        self.assertIn("most welcome", close_data["reply"].lower())
        self.assertEqual(close_data["action_taken"], "WRAP_UP_COURTESY")

    def test_administration_10_agents_and_user_call_routing(self):
        """Verify:
        1. 10 Call Center Agents exist in DB (Agent 1 to Agent 10).
        2. Admin Voice Agents dashboard (/admins/voice-agents/) renders all 10 agents with status & metrics.
        3. Admin status toggle API updates agent availability.
        4. User start-call connects to preferred agent (e.g. Agent 2).
        5. In-call agent transfer dynamically routes call from Agent 1 to Agent 2.
        """
        from voice_agent.models import CallCenterAgent
        # 1. Verify 10 Department Agents Seeded (plus Feedback Agent)
        agents = CallCenterAgent.seed_default_10_agents()
        self.assertEqual(CallCenterAgent.objects.filter(agent_number__lte=10).count(), 10)
        self.assertTrue(CallCenterAgent.objects.filter(agent_number=11).exists())
        agent1 = CallCenterAgent.objects.get(agent_number=1)
        agent2 = CallCenterAgent.objects.get(agent_number=2)
        self.assertEqual(agent1.short_name, "Agent 1")
        self.assertEqual(agent2.short_name, "Agent 2")
        self.assertIn("Priya", agent1.officer_name)
        self.assertIn("Vikram", agent2.officer_name)
        self.assertEqual(agent1.gender, "FEMALE")
        self.assertEqual(agent2.gender, "MALE")

        # 2. Admin Voice Agents Dashboard
        login_res = self.client.post(
            reverse("admin_login"),
            {"username": "admin", "password": "admin"},
        )
        self.assertEqual(login_res.status_code, 302)

        admin_page = self.client.get(reverse("admin_voice_agents"))
        self.assertEqual(admin_page.status_code, 200)
        self.assertContains(admin_page, "Agent 1")
        self.assertContains(admin_page, "Agent 2")
        self.assertContains(admin_page, "Agent 10")
        self.assertContains(admin_page, "AI Voice Agents")
        self.assertContains(admin_page, "Officer Priya")
        self.assertContains(admin_page, "Officer Vikram")

        # 3. Admin Status Toggle API
        toggle_res = self.client.post(
            reverse("admin_toggle_agent_status", args=[agent2.id]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(toggle_res.status_code, 200)
        t_data = toggle_res.json()
        self.assertTrue(t_data["success"])
        self.assertEqual(t_data["status"], "OFFLINE")

        # Toggle back to AVAILABLE
        self.client.post(
            reverse("admin_toggle_agent_status", args=[agent2.id]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        agent2.refresh_from_db()
        self.assertEqual(agent2.status, CallCenterAgent.STATUS_AVAILABLE)

        # 4. User Start Call with preferred Agent 2
        user_sess = self.client.session
        user_sess["user_account_id"] = self.user.id
        user_sess.save()

        call_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data={"agent_id": agent2.id},
            content_type="application/json",
        )
        self.assertEqual(call_res.status_code, 200)
        c_data = call_res.json()
        self.assertTrue(c_data["success"])
        self.assertIsNotNone(c_data["assigned_agent"])
        self.assertEqual(c_data["assigned_agent"]["id"], agent2.id)
        self.assertEqual(c_data["assigned_agent"]["short_name"], "Agent 2")
        self.assertIn("Vikram", c_data["welcome_message"])

        session_id = c_data["session_id"]

        # 5. Verify 1-to-1 dialogue with Agent 2
        ident_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "Who are you?"},
            content_type="application/json",
        )
        ident_data = ident_res.json()
        self.assertTrue(ident_data["success"])
        self.assertIn("Agent 2", ident_data["reply"])
        self.assertIn("Vikram", ident_data["reply"])

        # 6. In-call agent transfer to Agent 1
        transfer_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "Please transfer me to Agent 1"},
            content_type="application/json",
        )
        trans_data = transfer_res.json()
        self.assertTrue(trans_data["success"])
        self.assertEqual(trans_data["assigned_agent"]["id"], agent1.id)
        self.assertEqual(trans_data["assigned_agent"]["short_name"], "Agent 1")
        self.assertIn("Agent 1", trans_data["reply"])
        self.assertIn("Priya", trans_data["reply"])

    def test_ivr_initial_call_mode_and_keypad_routing_to_agents(self):
        """Verify user call starts in IVR mode, and typing options (1-9, 0) routes to the exact agent."""
        CallCenterAgent.seed_default_10_agents()

        # 1. Start user call without specifying an agent -> starts in IVR mode
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        res = self.client.post(reverse("api_voice_agent_start_call"))
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data.get("is_ivr_active"))
        self.assertIsNone(data.get("assigned_agent"))
        self.assertIn("24/7 Interactive Telephony Helpline", data["welcome_message"])
        self.assertIn("Press or type 1", data["welcome_message"])
        self.assertIn("Press 0", data["welcome_message"])

        session_id = data["session_id"]

        # 2. Type/Dial Option 1: Power Outages -> Connect to Agent 1 (Officer Priya)
        opt1_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "1"},
            content_type="application/json",
        )
        self.assertEqual(opt1_res.status_code, 200)
        opt1_data = opt1_res.json()
        self.assertTrue(opt1_data["success"])
        self.assertFalse(opt1_data["is_ivr_active"])
        self.assertEqual(opt1_data["assigned_agent"]["agent_number"], 1)
        self.assertEqual(opt1_data["assigned_agent"]["officer_name"], "Officer Priya")
        self.assertIn("Officer Priya", opt1_data["reply"])
        self.assertIn("Outage & Emergency Dispatch Specialist", opt1_data["reply"])

        # 3. Dial * to return to Main IVR Menu
        star_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "*"},
            content_type="application/json",
        )
        self.assertEqual(star_res.status_code, 200)
        star_data = star_res.json()
        self.assertTrue(star_data["success"])
        self.assertTrue(star_data["is_ivr_active"])
        self.assertIsNone(star_data["assigned_agent"])
        self.assertIn("Returning to the Main IVR Menu", star_data["reply"])

        # 4. Type/Dial Option 3: Billing & Tariffs -> Connect to Agent 3 (Officer Ananya)
        opt3_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "3"},
            content_type="application/json",
        )
        self.assertEqual(opt3_res.status_code, 200)
        opt3_data = opt3_res.json()
        self.assertTrue(opt3_data["success"])
        self.assertFalse(opt3_data["is_ivr_active"])
        self.assertEqual(opt3_data["assigned_agent"]["agent_number"], 3)
        self.assertEqual(opt3_data["assigned_agent"]["officer_name"], "Officer Ananya")
        self.assertIn("Officer Ananya", opt3_data["reply"])
        self.assertIn("Billing & Tariffs Specialist", opt3_data["reply"])

        # 5. Type/Dial Option 0: Supervisor -> Connect to Agent 10 (Officer Suresh)
        opt0_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "0"},
            content_type="application/json",
        )
        self.assertEqual(opt0_res.status_code, 200)
        opt0_data = opt0_res.json()
        self.assertTrue(opt0_data["success"])
        self.assertFalse(opt0_data["is_ivr_active"])
        self.assertEqual(opt0_data["assigned_agent"]["agent_number"], 10)
        self.assertEqual(opt0_data["assigned_agent"]["officer_name"], "Officer Suresh")
        self.assertIn("Officer Suresh", opt0_data["reply"])
        self.assertIn("Shift In-Charge", opt0_data["reply"])

    def test_agent_6_consumption_lookup_and_persona_consistency(self):
        """Verify connecting to Agent 6 answers consumed units, November consumption, and maintains persona."""
        CallCenterAgent.seed_default_10_agents()

        # Update test connection with unbilled consumption
        self.conn.unbilled_units = Decimal("6.08")
        self.conn.total_units_consumed = Decimal("6.08")
        self.conn.last_billed_reading = Decimal("0.00")
        self.conn.save()

        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # 1. Start call in IVR
        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        self.assertEqual(start_res.status_code, 200)
        session_id = start_res.json()["session_id"]

        # 2. Select Option 6 -> Agent 6 (Officer Rajesh)
        opt6_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "6"},
            content_type="application/json",
        )
        self.assertEqual(opt6_res.status_code, 200)
        opt6_data = opt6_res.json()
        self.assertEqual(opt6_data["assigned_agent"]["agent_number"], 6)
        self.assertEqual(opt6_data["assigned_agent"]["officer_name"], "Officer Rajesh")
        self.assertIn("Officer Rajesh", opt6_data["reply"])

        # 3. Query current units consumed
        units_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "may I know my current units that I consumed"},
            content_type="application/json",
        )
        self.assertEqual(units_res.status_code, 200)
        units_data = units_res.json()
        self.assertTrue(units_data["success"])
        self.assertIn("6.08", units_data["reply"])
        self.assertIn("MTR-998877", units_data["reply"])

        # 4. Check "are you there" -> must NOT say Officer Priya, must say Officer Rajesh!
        there_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "are you there"},
            content_type="application/json",
        )
        self.assertEqual(there_res.status_code, 200)
        there_data = there_res.json()
        self.assertTrue(there_data["success"])
        self.assertIn("Officer Rajesh", there_data["reply"])
        self.assertNotIn("Officer Priya", there_data["reply"])

        # 5. Query November consumption
        nov_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "I want to know my current units that have conserved in the month of November"},
            content_type="application/json",
        )
        self.assertEqual(nov_res.status_code, 200)
        nov_data = nov_res.json()
        self.assertTrue(nov_data["success"])
        self.assertIn("6.08", nov_data["reply"])
        self.assertIn("November", nov_data["reply"])

        # 6. Query real-time appliances currently powered on
        app_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "How many appliances are currently powered on in my home?"},
            content_type="application/json",
        )
        self.assertEqual(app_res.status_code, 200)
        app_data = app_res.json()
        self.assertTrue(app_data["success"])
        self.assertIn("appliances powered ON", app_data["reply"])
        self.assertIn("kilowatts", app_data["reply"])

        # 7. Remote control: Turn off the AC
        ac_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "Turn off my AC"},
            content_type="application/json",
        )
        self.assertEqual(ac_res.status_code, 200)
        ac_data = ac_res.json()
        self.assertTrue(ac_data["success"])
        self.assertIn("turned OFF", ac_data["reply"])
        self.assertIn("Split AC", ac_data["reply"])

        # Verify DB appliance state updated
        self.conn.refresh_from_db()
        self.assertFalse(self.conn.appliance_states.get("ac", True))

    def test_invoice_inquiry_and_notification_react_flow(self):
        """Verify caller asking for payment invoice receives printable invoice and ReAct flow sends to notifications on confirmation."""
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        # 1. Start voice call session
        init_res = self.client.post(reverse("api_voice_agent_start_call"), data={"caller_name": "Rajesh Kumar"})
        self.assertEqual(init_res.status_code, 200)
        session_id = init_res.json()["session_id"]

        # 2. Select Agent 2 (Officer Vikram)
        connect_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "2"},
            content_type="application/json",
        )
        self.assertEqual(connect_res.status_code, 200)

        # 3. Caller asks for their last payment invoice
        inv_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "could you please give me my last payment invoice"},
            content_type="application/json",
        )
        self.assertEqual(inv_res.status_code, 200)
        inv_data = inv_res.json()
        self.assertTrue(inv_data["success"])
        self.assertIsNotNone(inv_data.get("invoice"))
        self.assertEqual(inv_data["invoice"]["bill_number"], self.paid_bill.bill_number)
        self.assertEqual(inv_data["invoice"]["total_amount"], 390.0)
        self.assertIn("send a copy to your account notifications", inv_data["reply"].lower())

        # 4. Caller confirms with "yes please"
        confirm_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "yes please send it"},
            content_type="application/json",
        )
        self.assertEqual(confirm_res.status_code, 200)
        confirm_data = confirm_res.json()
        self.assertTrue(confirm_data["success"])
        self.assertIn("sent your official invoice copy to your notifications hub", confirm_data["reply"].lower())

        # 5. Verify UserNotification created in database
        notif = UserNotification.objects.filter(user=self.user, category=UserNotification.CATEGORY_BILLS).first()
        self.assertIsNotNone(notif)
        self.assertEqual(notif.badge, "INVOICE DELIVERED")
        self.assertIn(self.paid_bill.bill_number, notif.extra_info)

        # 6. Verify notification appears in user hub compiler
        from users.views import _build_user_notifications
        user_notifs = _build_user_notifications(self.user, self.conn)
        self.assertTrue(any(n["badge"] == "INVOICE DELIVERED" for n in user_notifs))

    def test_invoice_decline_react_flow(self):
        """Verify caller asking for payment invoice but declining notification confirmation does not create notification."""
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        # 1. Start call
        init_res = self.client.post(reverse("api_voice_agent_start_call"), data={"caller_name": "Rajesh Kumar"})
        session_id = init_res.json()["session_id"]

        # 2. Connect to Agent 3
        self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "3"},
            content_type="application/json",
        )

        # 3. Request invoice
        inv_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "give me my last payment invoice"},
            content_type="application/json",
        )
        self.assertEqual(inv_res.status_code, 200)
        self.assertIsNotNone(inv_res.json().get("invoice"))

        # 4. Decline: "no thanks"
        dec_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "no thanks, that won't be needed"},
            content_type="application/json",
        )
        self.assertEqual(dec_res.status_code, 200)
        dec_data = dec_res.json()
        self.assertIn("will not send it to notifications", dec_data["reply"].lower())

        # Verify no notification created
        self.assertFalse(UserNotification.objects.filter(user=self.user, category=UserNotification.CATEGORY_BILLS).exists())

    def test_invoice_pdf_download_view(self):
        """Verify downloading the official DISCOM tax invoice PDF produces valid binary PDF content."""
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        url = reverse("download_bill_invoice_pdf", kwargs={"bill_id": self.paid_bill.id})
        res = self.client.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertTrue(res.content.startswith(b"%PDF-"))
        self.assertIn(f"DISCOM_Tax_Invoice_{self.paid_bill.bill_number}.pdf", res["Content-Disposition"])

    def test_call_wrapup_inquiry_and_feedback_agent_flow(self):
        """Verify polite wrap-up inquiry, transfer to Officer Ritu (Feedback Agent), 0-5 rating, and call hang up."""
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        # 1. Start call
        init_res = self.client.post(reverse("api_voice_agent_start_call"), data={"caller_name": "Rajesh Kumar"})
        session_id = init_res.json()["session_id"]

        # 2. Connect to Agent 1 (Officer Priya)
        self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "1"},
            content_type="application/json",
        )

        # 3. Caller says "thank you"
        wrapup_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "thank you for your help"},
            content_type="application/json",
        )
        self.assertEqual(wrapup_res.status_code, 200)
        wrapup_data = wrapup_res.json()
        self.assertTrue(wrapup_data["success"])
        self.assertIn("should i help you with any other", wrapup_data["reply"].lower())

        # 4. Caller declines further help: "no that's all"
        decline_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "no that's all"},
            content_type="application/json",
        )
        self.assertEqual(decline_res.status_code, 200)
        decline_data = decline_res.json()
        self.assertTrue(decline_data["success"])
        self.assertTrue(decline_data["is_feedback_stage"])
        self.assertIn("officer ritu", decline_data["reply"].lower())
        self.assertIn("scale of 0 to 5", decline_data["reply"].lower())
        self.assertEqual(decline_data["assigned_agent"]["officer_name"], "Officer Ritu")

        # 5. Caller provides 5-star rating: "5 stars"
        rating_res = self.client.post(
            reverse("api_voice_agent_message"),
            data={"session_id": session_id, "message": "5 stars"},
            content_type="application/json",
        )
        self.assertEqual(rating_res.status_code, 200)
        rating_data = rating_res.json()
        self.assertTrue(rating_data["success"])
        self.assertTrue(rating_data["hang_up"])
        self.assertIsNotNone(rating_data["feedback_recorded"])
        self.assertEqual(rating_data["feedback_recorded"]["rating"], 5)
        self.assertIn("feedback rating of 5 out of 5", rating_data["reply"].lower())
        self.assertIn("goodbye", rating_data["reply"].lower())

        # 6. Verify session saved in database with feedback rating and closed status
        db_session = VoiceCallSession.objects.get(session_id=session_id)
        self.assertEqual(db_session.feedback_rating, 5)
        self.assertEqual(db_session.status, VoiceCallSession.STATUS_COMPLETED)
        self.assertIsNotNone(db_session.feedback_submitted_at)
        self.assertIsNotNone(db_session.ended_at)

    def test_feedback_rating_parsing(self):
        """Verify parse_feedback_rating recognizes digits, words, stars, and sentiment."""
        from voice_agent.agent import parse_feedback_rating
        self.assertEqual(parse_feedback_rating("0"), 0)
        self.assertEqual(parse_feedback_rating("zero"), 0)
        self.assertEqual(parse_feedback_rating("1 star"), 1)
        self.assertEqual(parse_feedback_rating("poor"), 1)
        self.assertEqual(parse_feedback_rating("2 stars"), 2)
        self.assertEqual(parse_feedback_rating("3"), 3)
        self.assertEqual(parse_feedback_rating("good"), 3)
        self.assertEqual(parse_feedback_rating("4 stars"), 4)
        self.assertEqual(parse_feedback_rating("very good"), 4)
        self.assertEqual(parse_feedback_rating("5"), 5)
        self.assertEqual(parse_feedback_rating("5 out of 5"), 5)
        self.assertEqual(parse_feedback_rating("excellent"), 5)

    def test_why_power_cut_inquiry_telemetry_flow(self):
        """Verify caller asking 'why the power cut has happened' receives factual telemetry explanation instead of unwanted complaint."""
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session["user_id"] = self.user.id
        s_session.save()

        # 1. Start Call (Agent 1: Officer Priya)
        start_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"agent_id": 1, "direct_connect": True}),
            content_type="application/json",
        )
        session_id = start_res.json()["session_id"]
        initial_complaints = Complaint.objects.count()

        # Case A: Premise Breaker Switch OFF
        self.user.house_power_on = False
        self.user.save()
        res_a = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "why the power cut has happened"
            }),
            content_type="application/json",
        )
        data_a = res_a.json()
        self.assertTrue(data_a["success"])
        self.assertIn("breaker switch is turned OFF", data_a["reply"])
        self.assertIsNone(data_a.get("registered_complaint"))
        self.assertEqual(Complaint.objects.count(), initial_complaints)

        # Case B: Departmental Power Cut
        self.user.house_power_on = True
        self.user.save()
        self.conn.is_power_cut = True
        self.conn.power_cut_remark = "11kV feeder line pole maintenance"
        self.conn.power_cut_reason = "11kV feeder line pole maintenance"
        self.conn.save()
        res_b = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "why the power cut has"
            }),
            content_type="application/json",
        )
        data_b = res_b.json()
        self.assertTrue(data_b["success"])
        self.assertIn("11kV feeder line pole maintenance", data_b["reply"])
        self.assertIsNone(data_b.get("registered_complaint"))
        self.assertEqual(Complaint.objects.count(), initial_complaints)

        # Case C: Area Transformer Offline
        self.conn.is_power_cut = False
        self.conn.save()
        self.tf.is_power_on = False
        self.tf.save()
        res_c = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "what happened to power"
            }),
            content_type="application/json",
        )
        data_c = res_c.json()
        self.assertTrue(data_c["success"])
        self.assertIn("OFFLINE", data_c["reply"])
        self.assertIsNone(data_c.get("registered_complaint"))
        self.assertEqual(Complaint.objects.count(), initial_complaints)

        # Case D: Grid Telemetry Healthy (Normal)
        self.tf.is_power_on = True
        self.tf.save()
        res_d = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "reason for power cut"
            }),
            content_type="application/json",
        )
        data_d = res_d.json()
        self.assertTrue(data_d["success"])
        self.assertIn("ONLINE and healthy", data_d["reply"])
        self.assertIsNone(data_d.get("registered_complaint"))
        self.assertEqual(Complaint.objects.count(), initial_complaints)

    def test_agent_identity_on_whats_your_name_in_ivr_and_connected_agent(self):
        """Verify 'what's your name' accurately identifies the active connected officer (e.g. Officer Suresh, Officer Rajesh)."""
        CallCenterAgent.seed_default_10_agents()
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # 1. Start call in IVR mode
        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        self.assertEqual(start_res.status_code, 200)
        session_id = start_res.json()["session_id"]

        # 2. In IVR mode, audio line check "can you hear me" -> must identify as IVR Helpline, NOT Officer Priya
        ivr_hear_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "can you hear me"}),
            content_type="application/json",
        )
        ivr_hear_data = ivr_hear_res.json()
        self.assertTrue(ivr_hear_data["success"])
        self.assertEqual(ivr_hear_data["action_taken"], "GREETING_ACK")
        self.assertIn("Automated IVR Helpline", ivr_hear_data["reply"])
        self.assertNotIn("Officer Priya", ivr_hear_data["reply"])

        # 3. Dial Option 0 -> Connect to Agent 10 (Officer Suresh)
        opt0_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "0"}),
            content_type="application/json",
        )
        self.assertEqual(opt0_res.json()["assigned_agent"]["officer_name"], "Officer Suresh")

        # 4. Once connected to Agent 10, ask "what's your name" -> must identify as Officer Suresh
        suresh_ident_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "what's your name"}),
            content_type="application/json",
        )
        suresh_ident_data = suresh_ident_res.json()
        self.assertTrue(suresh_ident_data["success"])
        self.assertEqual(suresh_ident_data["action_taken"], "AGENT_IDENTITY")
        self.assertIn("Officer Suresh", suresh_ident_data["reply"])
        self.assertNotIn("Officer Priya", suresh_ident_data["reply"])

        # 5. Ask "hello what's your name" -> must also match Officer Suresh in State B, NOT audio check in State D
        suresh_hello_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "hello what's your name"}),
            content_type="application/json",
        )
        suresh_hello_data = suresh_hello_res.json()
        self.assertTrue(suresh_hello_data["success"])
        self.assertEqual(suresh_hello_data["action_taken"], "AGENT_IDENTITY")
        self.assertIn("Officer Suresh", suresh_hello_data["reply"])
        self.assertNotIn("Officer Priya", suresh_hello_data["reply"])

    def test_out_of_scope_query_refusal_for_python_and_general_knowledge(self):
        """Verify out-of-scope non-electricity inquiries ('what is python', 'who is prime minister') are refused."""
        CallCenterAgent.seed_default_10_agents()
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # Direct connect to Agent 1 (Officer Priya)
        agent1 = CallCenterAgent.objects.get(agent_number=1)
        start_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"agent_id": agent1.id, "direct_connect": True}),
            content_type="application/json",
        )
        session_id = start_res.json()["session_id"]

        # 1. Ask "what is python" -> Must politely refuse and redirect back to Electricity Department
        python_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "what is python"}),
            content_type="application/json",
        )
        python_data = python_res.json()
        self.assertTrue(python_data["success"])
        self.assertEqual(python_data["action_taken"], "OUT_OF_SCOPE_REFUSAL")
        self.assertIn("strictly in Electricity Department customer care", python_data["reply"])
        self.assertNotIn("Python is a high-level programming language", python_data["reply"])

        # 2. Ask "who is the prime minister" -> Must refuse
        trivia_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "who is the prime minister"}),
            content_type="application/json",
        )
        trivia_data = trivia_res.json()
        self.assertTrue(trivia_data["success"])
        self.assertEqual(trivia_data["action_taken"], "OUT_OF_SCOPE_REFUSAL")
        self.assertIn("Electricity Department", trivia_data["reply"])

    def test_voice_ivr_navigation_with_spoken_words_and_inactivity_reprompt(self):
        """Verify spoken word numbers ('agent two', 'option two', 'two', 'supervisor') route accurately and inactivity reprompt works."""
        CallCenterAgent.seed_default_10_agents()
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # 1. Start call in IVR mode
        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        self.assertEqual(start_res.status_code, 200)
        session_id = start_res.json()["session_id"]
        self.assertIsNone(start_res.json()["assigned_agent"])

        # 2. Test IVR silence/inactivity reprompt 1
        silence_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no input reprompt 1"}),
            content_type="application/json",
        )
        silence_data = silence_res.json()
        self.assertTrue(silence_data["success"])
        self.assertEqual(silence_data["action_taken"], "IVR_INACTIVITY_REPROMPT")
        self.assertIn("haven't received your selection yet", silence_data["reply"])

        # 3. Test spoken words: "agent two issue" -> routes to Agent 2 (Officer Vikram)
        ag2_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "agent two issue"}),
            content_type="application/json",
        )
        ag2_data = ag2_res.json()
        self.assertTrue(ag2_data["success"])
        self.assertEqual(ag2_data["assigned_agent"]["officer_name"], "Officer Vikram")
        self.assertEqual(ag2_data["assigned_agent"]["agent_number"], 2)

    def test_spoken_transfer_to_feedback_agent_and_rating_hangup(self):
        """Verify caller can ask by voice 'Please transfer my call to Feedback Agent', rate 0-5, and hang up."""
        CallCenterAgent.seed_default_10_agents()
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # 1. Start call in IVR mode
        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # 2. Ask by voice: "Please transfer my call to Feedback Agent"
        xfer_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "Please transfer my call to Feedback Agent"}),
            content_type="application/json",
        )
        xfer_data = xfer_res.json()
        self.assertTrue(xfer_data["success"])
        self.assertEqual(xfer_data["action_taken"], "TRANSFER_FEEDBACK_AGENT")
        self.assertEqual(xfer_data["assigned_agent"]["officer_name"], "Officer Ritu")
        self.assertIn("Officer Ritu", xfer_data["reply"])
        self.assertIn("scale of 0 to 5", xfer_data["reply"])

        # 3. Caller speaks rating: "five"
        rate_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "five"}),
            content_type="application/json",
        )
        rate_data = rate_res.json()
        self.assertTrue(rate_data["success"])
        self.assertTrue(rate_data["hang_up"])
        self.assertIn("FEEDBACK_RATING_RECORDED", rate_data["action_taken"])
        self.assertEqual(rate_data["feedback_recorded"]["rating"], 5)

    def test_unpaid_and_paid_bill_inquiries_with_phonetic_variations(self):
        """Verify unpaid (and STT typo 'unpaired') bill queries and paid bill queries return exact ledger records without notification hijacking."""
        CallCenterAgent.seed_default_10_agents()
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # Connect to Agent 10 (Officer Suresh)
        agent10 = CallCenterAgent.objects.get(agent_number=10)
        start_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"agent_id": agent10.id, "direct_connect": True}),
            content_type="application/json",
        )
        session_id = start_res.json()["session_id"]

        # 1. Ask "generate me the invoice"
        inv_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "generate me the invoice"}),
            content_type="application/json",
        )
        inv_data = inv_res.json()
        self.assertTrue(inv_data["success"])
        self.assertIn("FETCH_INVOICE", inv_data["action_taken"])
        self.assertIn("send a copy to your account notifications", inv_data["reply"])

        # 2. Ask "what is the unpaired bill" right after invoice prompt
        # Must NOT be intercepted by DECLINE_INVOICE_NOTIFICATION!
        unpaid_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "what is the unpaired bill"}),
            content_type="application/json",
        )
        unpaid_data = unpaid_res.json()
        self.assertTrue(unpaid_data["success"])
        self.assertEqual(unpaid_data["action_taken"], "UNPAID_BILL_INQUIRY")
        self.assertIn("unpaid bill", unpaid_data["reply"])
        self.assertIn("890.00", unpaid_data["reply"])
        self.assertNotIn("I will not send it to notifications", unpaid_data["reply"])

        # 3. Ask "please give me the amount have paid in the previous month"
        paid_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "please give me the amount have paid in the previous month"}),
            content_type="application/json",
        )
        paid_data = paid_res.json()
        self.assertTrue(paid_data["success"])
        self.assertEqual(paid_data["action_taken"], "PAID_BILL_INQUIRY")
        self.assertIn("390.00", paid_data["reply"])
        self.assertIn("2026-10", paid_data["reply"])
        self.assertIn("Paid in full", paid_data["reply"])

    def test_third_party_meter_privacy_refusal(self):
        """Verify queries for third-party meters or transformer meter lists are immediately refused under privacy regulations without hanging."""
        CallCenterAgent.seed_default_10_agents()
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        # Connect to Agent 5 (Officer Sneha - Substation & Transformer Diagnostics)
        agent5 = CallCenterAgent.objects.get(agent_number=5)
        start_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"agent_id": agent5.id, "direct_connect": True}),
            content_type="application/json",
        )
        session_id = start_res.json()["session_id"]

        # 1. Ask "what are the metres connected to the Transformer 1"
        res1 = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "what are the metres connected to the Transformer 1"}),
            content_type="application/json",
        )
        data1 = res1.json()
        self.assertTrue(data1["success"])
        self.assertEqual(data1["action_taken"], "PRIVACY_THIRD_PARTY_DATA_REFUSAL")
        self.assertIn("privacy and data security regulations", data1["reply"])
        self.assertNotIn("Please hold for a moment", data1["reply"])
        self.assertIn("MTR-998877", data1["reply"])

        # 2. Ask "who else is connected to this transformer"
        res2 = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "who else is connected to this transformer"}),
            content_type="application/json",
        )
        data2 = res2.json()
        self.assertTrue(data2["success"])
        self.assertEqual(data2["action_taken"], "PRIVACY_THIRD_PARTY_DATA_REFUSAL")
        self.assertIn("privacy and data security regulations", data2["reply"])

    def test_spoken_credentials_verification_and_defined_issue_resolution(self):
        """Verify dynamic verification flow: caller provides mobile/meter/email, system verifies against DB, and clarifies defined issue."""
        CallCenterAgent.seed_default_10_agents()
        agent1 = CallCenterAgent.objects.get(agent_number=1)
        # Start a call directly connected with Agent 1 without logged-in session (unverified guest caller)
        start_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"agent_id": agent1.id, "direct_connect": True}),
            content_type="application/json",
        )
        session_id = start_res.json()["session_id"]

        # Step 1: Caller states issue: "why the power cut has happened"
        issue_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "why the power cut has happened"}),
            content_type="application/json",
        )
        issue_data = issue_res.json()
        self.assertTrue(issue_data["success"])
        self.assertIn("mobile", issue_data["reply"].lower())

        # Step 2: Caller provides registered mobile "9876543210"
        cred_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "my registered mobile number is 9876543210"}),
            content_type="application/json",
        )
        cred_data = cred_res.json()
        self.assertTrue(cred_data["success"])
        self.assertIn("CALLER_VERIFIED_LOOKUP", cred_data["action_taken"])
        self.assertIn("MTR-998877", cred_data["reply"])
        # Should clarify the defined power cut issue reported in previous turn
        self.assertIn("telemetry", cred_data["reply"].lower())

    def test_natural_telephony_responses_omit_repetitive_honorifics(self):
        """Verify conversational turns speak naturally without repetitive 'Mr. {name}' in every turn."""
        CallCenterAgent.seed_default_10_agents()
        session = self.client.session
        session["user_account_id"] = self.user.id
        session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Turn 1: Select agent 3 (billing)
        t1_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "3"}),
            content_type="application/json",
        )
        t1_data = t1_res.json()
        self.assertTrue(t1_data["success"])

        # Turn 2: Ask "what is my bill"
        t2_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "what is my latest bill"}),
            content_type="application/json",
        )
        t2_data = t2_res.json()
        self.assertTrue(t2_data["success"])
        # Must NOT start with "Mr. Rajesh Kumar, "
        self.assertFalse(t2_data["reply"].startswith("Mr. Rajesh Kumar"))
        self.assertIn("890.00", t2_data["reply"])

    def test_payment_guidance_and_quick_pay_without_login(self):
        """Verify conversational payment guidance:
        1. Unpaid bill inquiry asks: 'Would you like me to guide you on how to pay it online?'
        2. Caller answers 'please guide me how to pay it online' -> provides 2 clear payment options without reset.
        3. Caller asks 'can I pay on the website without login' -> confirms Quick Pay with meter number.
        """
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Turn 1: Connect to Billing Agent (Officer Ananya / Agent 3)
        self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "3"}),
            content_type="application/json",
        )

        # Turn 2: Ask for unpaid bills
        unpaid_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "is there any unpaid bills"}),
            content_type="application/json",
        )
        unpaid_data = unpaid_res.json()
        self.assertTrue(unpaid_data["success"])
        self.assertEqual(unpaid_data["action_taken"], "UNPAID_BILL_INQUIRY")
        self.assertIn("890.00", unpaid_data["reply"])
        self.assertIn("guide you on how to pay it online", unpaid_data["reply"].lower())

        # Turn 3: Ask "please guide me how to pay it online"
        guide_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "please guide me how to pay it online"}),
            content_type="application/json",
        )
        guide_data = guide_res.json()
        self.assertTrue(guide_data["success"])
        self.assertEqual(guide_data["action_taken"], "PAYMENT_GUIDANCE")
        # Must NOT reset to initial greeting
        self.assertNotIn("actively monitoring your service line", guide_data["reply"])
        self.assertIn("quick pay", guide_data["reply"].lower())
        self.assertIn("MTR-998877", guide_data["reply"])

        # Turn 4: Ask "no I will use the website like without login can I pay the bill"
        no_login_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "no I will use in the website like without login can I pay the bill"
            }),
            content_type="application/json",
        )
        no_login_data = no_login_res.json()
        self.assertTrue(no_login_data["success"])
        self.assertEqual(no_login_data["action_taken"], "PAYMENT_GUIDANCE")
        # Must clearly answer YES and explain Quick Pay without login
        self.assertIn("yes", no_login_data["reply"].lower())
        self.assertIn("without logging in", no_login_data["reply"].lower())
        self.assertIn("quick pay", no_login_data["reply"].lower())
        self.assertIn("MTR-998877", no_login_data["reply"])

    def test_daily_consumption_inquiry_and_notification(self):
        """Verify daily consumption inquiry and notification scheduling."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Turn 1: Connect to Supervisor / Agent 10
        self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "0"}),
            content_type="application/json",
        )

        # Turn 2: Inquire today's units of power consumed
        usage_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "may I know that total unit of power consumed for that day of today yes please"
            }),
            content_type="application/json",
        )
        usage_data = usage_res.json()
        self.assertTrue(usage_data["success"])
        self.assertEqual(usage_data["action_taken"], "METER_CONSUMPTION_INQUIRY")
        self.assertIn("daily usage summary", usage_data["reply"].lower())

        # Turn 3: Confirm sending daily usage summary
        confirm_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "yes please"}),
            content_type="application/json",
        )
        confirm_data = confirm_res.json()
        self.assertTrue(confirm_data["success"])
        self.assertEqual(confirm_data["action_taken"], "SEND_DAILY_USAGE_NOTIFICATION")
        self.assertIn("scheduled your daily usage summary", confirm_data["reply"].lower())
        # Notification must be present in database
        notif = UserNotification.objects.filter(user=self.user, category=UserNotification.CATEGORY_LIVE).first()
        self.assertIsNotNone(notif)
        self.assertIn("MTR-998877", notif.extra_info)

    def test_in_call_silence_reprompt_sequence_and_feedback_transfer(self):
        """Verify:
        1. Silence 1: Agent checks 'are you there? Please go ahead and describe your issue'
        2. Silence 2: Agent asks 'should I transfer the call to our feedback desk?'
        3. Caller confirms: 'yes please transfer' -> transfers to Officer Ritu (Feedback Desk)
        """
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Turn 1: Connect to Officer Priya (Agent 1)
        self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "1"}),
            content_type="application/json",
        )

        # Silence 1 (~10s silence):
        s1_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no input agent reprompt 1"}),
            content_type="application/json",
        )
        s1_data = s1_res.json()
        self.assertTrue(s1_data["success"])
        self.assertEqual(s1_data["action_taken"], "AGENT_INACTIVITY_REPROMPT_1")
        self.assertIn("are you there", s1_data["reply"].lower())
        self.assertIn("describe your issue", s1_data["reply"].lower())

        # Silence 2 (~20s silence):
        s2_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no input agent reprompt 2"}),
            content_type="application/json",
        )
        s2_data = s2_res.json()
        self.assertTrue(s2_data["success"])
        self.assertEqual(s2_data["action_taken"], "AGENT_INACTIVITY_REPROMPT_2")
        self.assertIn("transfer the call to our feedback desk", s2_data["reply"].lower())

        # Caller says "yes please transfer"
        tf_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "yes please transfer"}),
            content_type="application/json",
        )
        tf_data = tf_res.json()
        self.assertTrue(tf_data["success"])
        self.assertEqual(tf_data["action_taken"], "TRANSFER_FEEDBACK_AGENT")
        self.assertTrue(tf_data["is_feedback_stage"])
        self.assertIn("officer ritu", tf_data["reply"].lower())
        self.assertIn("scale of 0 to 5", tf_data["reply"].lower())

    def test_in_call_silence_auto_transfer_to_feedback_and_auto_hangup(self):
        """Verify:
        1. Repeated silence during live call auto-transfers to Officer Ritu (Feedback Desk)
        2. Continued silence in feedback stage prompts rating reminder
        3. Final timeout automatically hangs up call with polite goodbye
        """
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Turn 1: Connect to Supervisor (Agent 10)
        self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "0"}),
            content_type="application/json",
        )

        # Silence 3 (~30s silence with no input): Auto-transfer to Feedback Desk
        s3_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no input agent reprompt 3"}),
            content_type="application/json",
        )
        s3_data = s3_res.json()
        self.assertTrue(s3_data["success"])
        self.assertEqual(s3_data["action_taken"], "AUTO_TRANSFER_FEEDBACK_SILENCE")
        self.assertTrue(s3_data["is_feedback_stage"])
        self.assertIn("officer ritu", s3_data["reply"].lower())

        # Silence in feedback stage (Reprompt 1):
        fb1_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no input feedback reprompt 1"}),
            content_type="application/json",
        )
        fb1_data = fb1_res.json()
        self.assertTrue(fb1_data["success"])
        self.assertEqual(fb1_data["action_taken"], "FEEDBACK_INACTIVITY_REPROMPT")
        self.assertIn("share your rating before we disconnect", fb1_data["reply"].lower())

        # Silence in feedback stage (Reprompt 2): Auto hangup
        fb2_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no input feedback reprompt 2"}),
            content_type="application/json",
        )
        fb2_data = fb2_res.json()
        self.assertTrue(fb2_data["success"])
        self.assertEqual(fb2_data["action_taken"], "AUTO_HANGUP_SILENCE")
        self.assertTrue(fb2_data["hang_up"])
        self.assertIn("hang up the call", fb2_data["reply"].lower())

        # Check session is closed in DB
        db_sess = VoiceCallSession.objects.get(session_id=session_id)
        self.assertEqual(db_sess.status, VoiceCallSession.STATUS_COMPLETED)
        self.assertIsNotNone(db_sess.ended_at)

    def test_in_call_silence_caller_declines_transfer_and_stays_connected(self):
        """Verify caller saying 'no wait I'm here' after Reprompt 2 stays connected with current officer."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Turn 1: Connect to Agent 1
        self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "1"}),
            content_type="application/json",
        )

        # Reprompt 2: Asks "should I transfer the call to our feedback desk?"
        self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no input agent reprompt 2"}),
            content_type="application/json",
        )

        # Caller answers "no wait I am here"
        stay_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "no wait I am here"}),
            content_type="application/json",
        )
        stay_data = stay_res.json()
        self.assertTrue(stay_data["success"])
        self.assertEqual(stay_data["action_taken"], "STILL_ON_LINE_ACKNOWLEDGED")
        self.assertFalse(stay_data["is_feedback_stage"])
        self.assertIn("right here with you", stay_data["reply"].lower())

    def test_direct_upi_debit_disallowed_security_guard(self):
        """Verify queries asking the agent to collect UPI ID or directly debit are rejected with RBI policy guidance."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Caller asks: "tell my up I will tell my UBI ID so you will you directly pay my unpaid bill"
        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "tell my up I will tell my UBI ID so you will you directly pay my unpaid bill"
            }),
            content_type="application/json",
        )
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "DIRECT_UPI_DEBIT_DISALLOWED")
        self.assertIn("rbi and discom guidelines", data["reply"].lower())
        self.assertIn("quick pay", data["reply"].lower())

    def test_historical_monthly_consumption_lookup(self):
        """Verify querying consumption for past months (e.g. October vs July) looks up database records accurately."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # 1. Query October 2026 (self.paid_bill exists: 50.00 units)
        res_oct = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "how much power I have consumed in the month of October 2026"
            }),
            content_type="application/json",
        )
        data_oct = res_oct.json()
        self.assertTrue(data_oct["success"])
        self.assertEqual(data_oct["action_taken"], "METER_CONSUMPTION_INQUIRY")
        self.assertIn("50.00 units", data_oct["reply"])
        self.assertIn("october", data_oct["reply"].lower())

        # 2. Query July 2026 (No bill exists: explains connection was not active during that month)
        res_jul = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "how much power I have consumed in the month of July"
            }),
            content_type="application/json",
        )
        data_jul = res_jul.json()
        self.assertTrue(data_jul["success"])
        self.assertEqual(data_jul["action_taken"], "METER_CONSUMPTION_INQUIRY")
        self.assertIn("july", data_jul["reply"].lower())
        self.assertIn("not active during that month", data_jul["reply"].lower())

    def test_in_call_dtmf_does_not_shift_agent(self):
        """Verify that pressing touch-tone keypad digits while connected to an officer does NOT switch agents."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # Turn 1: Connect to Agent 4 (Officer Rahul - Hazard Specialist)
        c4 = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "4"}),
            content_type="application/json",
        )
        self.assertEqual(c4.json()["assigned_agent"]["agent_number"], 4)

        # Turn 2: Press key '1' while connected to Agent 4 -> MUST NOT shift to Agent 1!
        key1_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "1"}),
            content_type="application/json",
        )
        data1 = key1_res.json()
        self.assertTrue(data1["success"])
        self.assertEqual(data1["action_taken"], "IN_CALL_DTMF_KEY_RECEIVED: 1")
        self.assertEqual(data1["assigned_agent"]["agent_number"], 4)
        self.assertIn("Officer Rahul", data1["reply"])

        # Turn 3: Press key '#' -> Acknowledged, remains Agent 4
        key_hash = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "#"}),
            content_type="application/json",
        )
        self.assertEqual(key_hash.json()["action_taken"], "IN_CALL_DTMF_KEY_RECEIVED: #")
        self.assertEqual(key_hash.json()["assigned_agent"]["agent_number"], 4)

        # Turn 4: Press key '*' -> Returns to Main IVR Menu
        star_res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "*"}),
            content_type="application/json",
        )
        self.assertEqual(star_res.json()["action_taken"], "IVR_MENU_REPEAT")
        self.assertTrue(star_res.json()["is_ivr_active"])

    def test_agent_specialization_distinct_perspective(self):
        """Verify that different agents frame inquiries through their specialized departmental jurisdiction."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        # Connect directly to Agent 4 (Hazards)
        start_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"agent_id": 4, "direct_connect": True}),
            content_type="application/json",
        )
        session_id = start_res.json()["session_id"]

        # Inquire consumption with Agent 4
        res4 = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": session_id,
                "message": "how many units consumed in november"
            }),
            content_type="application/json",
        )
        data4 = res4.json()
        self.assertTrue(data4["success"])
        self.assertIn("Officer Rahul", data4["reply"])
        self.assertIn("Officer Ananya", data4["reply"])  # Recommends Billing specialist
        self.assertIn("hazards", data4["reply"].lower())

    def test_offline_agent_not_connected_when_dialed(self):
        """Verify that when an agent is marked OFFLINE by administration, selecting that agent does NOT connect."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        # Mark Agent 2 (Officer Vikram) as OFFLINE
        ag2 = CallCenterAgent.objects.get(agent_number=2)
        ag2.status = CallCenterAgent.STATUS_OFFLINE
        ag2.is_active = False
        ag2.save()

        # Start call in IVR mode
        start_res = self.client.post(reverse("api_voice_agent_start_call"))
        session_id = start_res.json()["session_id"]

        # User attempts to select Option 2 (Officer Vikram)
        res2 = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({"session_id": session_id, "message": "2"}),
            content_type="application/json",
        )
        data2 = res2.json()
        self.assertTrue(data2["success"])
        self.assertIn("AGENT_OFFLINE_REJECTED", data2["action_taken"])
        self.assertIn("offline", data2["reply"].lower())
        self.assertIn("Officer Vikram", data2["reply"])
        # Crucial check: Must NOT be assigned to Agent 2
        self.assertIsNone(data2.get("assigned_agent"))
        self.assertTrue(data2.get("is_ivr_active"))

    def test_user_pure_voice_agent_page_renders_without_keypad(self):
        """Verify the new hands-free conversational voice calling page renders, has no keypad details, and starts call cleanly."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        res = self.client.get(reverse("user_pure_voice_agent"))
        self.assertEqual(res.status_code, 200)
        content = res.content.decode("utf-8")
        self.assertIn("Hands-Free AI Voice Calling", content)
        self.assertIn("100% Voice / No Keypad", content)
        # Verify keypad is absent
        self.assertNotIn("telephony-dialpad-drawer", content)
        self.assertNotIn("dialpad-grid", content)

        # Test starting a hands-free call
        start_res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"hands_free": True, "direct_connect": True}),
            content_type="application/json",
        )
        self.assertEqual(start_res.status_code, 200)
        start_data = start_res.json()
        self.assertTrue(start_data["success"])
        self.assertFalse(start_data["is_ivr_active"])
        self.assertEqual(start_data["assigned_agent"]["agent_number"], 1)
        self.assertIn("Officer Priya", start_data["assigned_agent"]["officer_name"])
        self.assertIn("Hands-Free", start_data["welcome_message"])
        self.assertIsNotNone(start_data["audio_base64"])

    def test_admin_agent_sample_voice_endpoint(self):
        """Verify admin can sample authentic neural voice audio for an agent."""
        from django.http import HttpResponse
        from admins.views import ADMIN_AUTH_COOKIE, ADMIN_AUTH_SALT, ADMIN_USERNAME

        CallCenterAgent.seed_default_10_agents()
        resp = HttpResponse()
        resp.set_signed_cookie(ADMIN_AUTH_COOKIE, ADMIN_USERNAME, salt=ADMIN_AUTH_SALT)
        self.client.cookies[ADMIN_AUTH_COOKIE] = resp.cookies[ADMIN_AUTH_COOKIE].value

        ag1 = CallCenterAgent.objects.get(agent_number=1)
        res = self.client.post(
            reverse("admin_agent_sample_voice", kwargs={"agent_id": ag1.id}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["officer_name"], "Officer Priya")
        self.assertEqual(data["voice_name"], "en-IN-NeerjaExpressiveNeural")
        self.assertIn("Officer Priya", data["sample_text"])

    def test_in_call_transfer_from_supervisor_to_outage_agent_no_complaint(self):
        """Verify transferring from Shift In-Charge (Agent 10) to outage department routes to Agent 1 and creates NO complaint."""
        CallCenterAgent.seed_default_10_agents()
        agent10 = CallCenterAgent.objects.get(agent_number=10)
        agent1 = CallCenterAgent.objects.get(agent_number=1)

        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent10,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        initial_complaints = Complaint.objects.count()

        # Caller asks to transfer to outage department
        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "I'm having a complaint regarding the outage could you please transfer your call to the outage department",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("IVR_CONNECTED_TO_AGENT_1", data["action_taken"])
        self.assertIn("Officer Priya", data["reply"])
        session.refresh_from_db()
        self.assertEqual(session.assigned_agent.agent_number, 1)
        # CRUCIAL: No unexpected complaint was registered
        self.assertEqual(Complaint.objects.count(), initial_complaints)

    def test_in_call_transfer_phrase_with_my_call_to_outage_agent(self):
        """Verify 'could you please transfer my call to the outage agent' transfers cleanly without filing complaint."""
        CallCenterAgent.seed_default_10_agents()
        agent10 = CallCenterAgent.objects.get(agent_number=10)

        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent10,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        initial_complaints = Complaint.objects.count()

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "could you please transfer my call to the outage agent",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("IVR_CONNECTED_TO_AGENT_1", data["action_taken"])
        self.assertIn("Officer Priya", data["reply"])
        session.refresh_from_db()
        self.assertEqual(session.assigned_agent.agent_number, 1)
        self.assertEqual(Complaint.objects.count(), initial_complaints)

    def test_hands_free_start_call_greeting_no_keypad_instructions(self):
        """Verify api_voice_agent_start_call with hands_free=True connects directly to Officer Priya with zero keypad mentions."""
        CallCenterAgent.seed_default_10_agents()
        s_session = self.client.session
        s_session["user_account_id"] = self.user.id
        s_session.save()

        res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"hands_free": True, "direct_connect": True}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertFalse(data["is_ivr_active"])
        self.assertIsNotNone(data["assigned_agent"])
        self.assertEqual(data["assigned_agent"]["officer_name"], "Officer Priya")
        spoken = data["welcome_spoken_text"].lower()
        self.assertNotIn("touch-tone", spoken)
        self.assertNotIn("keypad", spoken)
        self.assertNotIn("dial", spoken)
        self.assertIn("hands-free voice helpline", spoken)
        self.assertIn("officer priya", spoken)

    def test_in_call_transfer_to_offline_agent_rejected_gracefully(self):
        """Verify transferring in-call to an offline agent is rejected politely and caller stays with current officer."""
        CallCenterAgent.seed_default_10_agents()
        agent2 = CallCenterAgent.objects.get(agent_number=2)
        agent2.status = CallCenterAgent.STATUS_OFFLINE
        agent2.is_active = False
        agent2.save()

        agent10 = CallCenterAgent.objects.get(agent_number=10)

        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent10,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "could you please transfer my call to the grid department",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("AGENT_OFFLINE_REJECTED", data["action_taken"])
        self.assertIn("offline", data["reply"].lower())
        self.assertIn("Officer Vikram", data["reply"])
        session.refresh_from_db()
        # Still assigned to Agent 10, NOT moved to offline Agent 2
        self.assertEqual(session.assigned_agent.agent_number, 10)

    def test_future_outage_query_does_not_register_complaint(self):
        """Verify asking about future or scheduled outages provides grid telemetry and DOES NOT register a complaint."""
        CallCenterAgent.seed_default_10_agents()
        agent1 = CallCenterAgent.objects.get(agent_number=1)

        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent1,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        initial_complaints = Complaint.objects.filter(user_account=self.user).count()

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "Could you please tell me will there be any future power outage",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "SCHEDULED_OUTAGE_QUERY_RESOLVED")
        self.assertIn("scheduled or planned power outages", data["reply"].lower())
        self.assertIsNone(data["registered_complaint"])
        self.assertEqual(Complaint.objects.filter(user_account=self.user).count(), initial_complaints)

    def test_multiturn_is_there_any_power_outage_today_does_not_register_complaint(self):
        """Verify asking 'I'm asking you is there any power outage today' provides grid telemetry without filing a complaint."""
        CallCenterAgent.seed_default_10_agents()
        agent1 = CallCenterAgent.objects.get(agent_number=1)

        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent1,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        initial_complaints = Complaint.objects.filter(user_account=self.user).count()

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "I'm asking you is there any power outage today",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "SCHEDULED_OUTAGE_QUERY_RESOLVED")
        self.assertIn("telemetry", data["reply"].lower())
        self.assertIsNone(data["registered_complaint"])
        self.assertEqual(Complaint.objects.filter(user_account=self.user).count(), initial_complaints)

    def test_admin_agent_sample_voice_uses_unified_voice_profiles(self):
        """Verify admin voice test uses the exact unified neural voice profiles matching live interaction."""
        from django.http import HttpResponse
        from admins.views import ADMIN_AUTH_COOKIE, ADMIN_AUTH_SALT, ADMIN_USERNAME
        CallCenterAgent.seed_default_10_agents()

        resp = HttpResponse()
        resp.set_signed_cookie(
            ADMIN_AUTH_COOKIE,
            ADMIN_USERNAME,
            salt=ADMIN_AUTH_SALT,
            httponly=True,
            samesite="Lax",
        )
        self.client.cookies[ADMIN_AUTH_COOKIE] = resp.cookies[ADMIN_AUTH_COOKIE].value

        ag1 = CallCenterAgent.objects.get(agent_number=1)
        res1 = self.client.post(
            reverse("admin_agent_sample_voice", kwargs={"agent_id": ag1.id}),
            content_type="application/json",
        )
        self.assertEqual(res1.status_code, 200)
        data1 = res1.json()
        self.assertTrue(data1["success"])
        self.assertEqual(data1["voice_name"], "en-IN-NeerjaExpressiveNeural")

        ag2 = CallCenterAgent.objects.get(agent_number=2)
        res2 = self.client.post(
            reverse("admin_agent_sample_voice", kwargs={"agent_id": ag2.id}),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2["success"])
        self.assertEqual(data2["voice_name"], "en-IN-PrabhatNeural")

    def test_feedback_rating_zero_parsed_accurately(self):
        """Verify giving a rating of 0 ('zero out of five', 'number zero', '0') records satisfaction score 0 and NOT 5."""
        CallCenterAgent.seed_default_10_agents()
        feedback_agent = CallCenterAgent.objects.get(agent_number=11)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=feedback_agent,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
            is_feedback_stage=True,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "number zero",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "FEEDBACK_RATING_RECORDED: 0/5")
        self.assertIsNotNone(data["feedback_recorded"])
        self.assertEqual(data["feedback_recorded"]["rating"], 0)
        self.assertIn("0 out of 5", data["reply"])

        session.refresh_from_db()
        self.assertEqual(session.feedback_rating, 0)
        self.assertEqual(session.status, VoiceCallSession.STATUS_COMPLETED)

    def test_feedback_prompt_echo_not_parsed_as_five(self):
        """Verify prompt echo like 'on a scale of zero to five how would you rate' does NOT record 5 stars."""
        CallCenterAgent.seed_default_10_agents()
        feedback_agent = CallCenterAgent.objects.get(agent_number=11)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=feedback_agent,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
            is_feedback_stage=True,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "I didn't quite get that on a scale of zero to five how would you rate your support experience today",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "FEEDBACK_RATING_RETRY")
        self.assertIsNone(data["feedback_recorded"])
        self.assertIn("didn't quite catch", data["reply"].lower())

        session.refresh_from_db()
        self.assertIsNone(session.feedback_rating)
        self.assertEqual(session.status, VoiceCallSession.STATUS_ACTIVE)

    def test_when_did_my_meter_was_issued_query(self):
        """Verify asking 'When did my meter was issued' returns exact issuance timestamp from DB without canned greeting."""
        CallCenterAgent.seed_default_10_agents()
        agent5 = CallCenterAgent.objects.get(agent_number=5)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent5,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "When did my meter was issued",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "CONNECTION_TIMELINE_INQUIRY")
        self.assertIn("was officially issued", data["reply"])
        self.assertNotIn("Power supply is currently", data["reply"])

    def test_application_upload_date_query(self):
        """Verify asking about application upload and connection timestamps returns database dates."""
        CallCenterAgent.seed_default_10_agents()
        agent5 = CallCenterAgent.objects.get(agent_number=5)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent5,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "May know the exacted data in the time when my application and my connection of the meter has been uploaded",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "CONNECTION_TIMELINE_INQUIRY")
        self.assertIn("application was uploaded", data["reply"].lower())
        self.assertIn("meter", data["reply"].lower())

    def test_how_many_officers_available_query(self):
        """Verify asking 'How many officers are available' lists all 10 departments and officers."""
        CallCenterAgent.seed_default_10_agents()
        agent1 = CallCenterAgent.objects.get(agent_number=1)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent1,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "How many officers are available",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "AVAILABLE_DEPARTMENTS_DIRECTORY")
        self.assertIn("10 dedicated departments", data["reply"])
        self.assertIn("Officer Priya", data["reply"])
        self.assertIn("Officer Rajesh", data["reply"])
        self.assertIn("Officer Suresh", data["reply"])

        res2 = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "How many agents are available under your service",
            }),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        self.assertEqual(res2.json()["action_taken"], "AVAILABLE_DEPARTMENTS_DIRECTORY")

    def test_direct_transfer_to_smart_meter_support(self):
        """Verify saying 'Need smart meter support' while connected to Officer Rahul directly transfers to Agent 6."""
        CallCenterAgent.seed_default_10_agents()
        agent4 = CallCenterAgent.objects.get(agent_number=4)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent4,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "Need smart meter support",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "IVR_CONNECTED_TO_AGENT_6")
        self.assertEqual(data["assigned_agent"]["agent_number"], 6)
        self.assertIn("Officer Rajesh", data["reply"])
        self.assertIn("hold the line for a moment", data["reply"].lower())

        session.refresh_from_db()
        self.assertEqual(session.assigned_agent.agent_number, 6)

    def test_confirming_offered_agent_transfer_connects_immediately(self):
        """Verify saying 'Yes please' to a transfer offer immediately connects to that agent."""
        CallCenterAgent.seed_default_10_agents()
        agent4 = CallCenterAgent.objects.get(agent_number=4)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent4,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )
        # Previous agent message offering transfer
        VoiceCallMessage.objects.create(
            session=session,
            sender=VoiceCallMessage.SENDER_AI,
            text="For smart‑meter support I can transfer you to the Smart Meter Support team—would you like me to connect you?",
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "Yes please",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "IVR_CONNECTED_TO_AGENT_6")
        self.assertEqual(data["assigned_agent"]["agent_number"], 6)
        self.assertIn("Officer Rajesh", data["reply"])

        session.refresh_from_db()
        self.assertEqual(session.assigned_agent.agent_number, 6)

    def test_okay_after_transfer_hold_does_not_transfer_to_feedback(self):
        """Verify saying 'Okay' when agent says 'please hold while I connect you' routes to target agent, NOT feedback."""
        CallCenterAgent.seed_default_10_agents()
        agent4 = CallCenterAgent.objects.get(agent_number=4)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent4,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )
        # Previous message from Officer Rahul
        VoiceCallMessage.objects.create(
            session=session,
            sender=VoiceCallMessage.SENDER_AI,
            text="I can transfer you to Smart Meter Support now—please hold while I connect you.",
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "Okay",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        # Must connect to Agent 6 (Smart Meter Support), and NEVER to Agent 11 (Feedback)
        self.assertEqual(data["action_taken"], "IVR_CONNECTED_TO_AGENT_6")
        self.assertEqual(data["assigned_agent"]["agent_number"], 6)
        self.assertFalse(data["is_feedback_stage"])
        self.assertIn("Officer Rajesh", data["reply"])

        session.refresh_from_db()
        self.assertEqual(session.assigned_agent.agent_number, 6)
        self.assertFalse(session.is_feedback_stage)

    def test_direct_transfer_to_feedback_desk(self):
        """Verify explicitly asking for feedback desk transfers directly to Officer Ritu with hold notice."""
        CallCenterAgent.seed_default_10_agents()
        agent1 = CallCenterAgent.objects.get(agent_number=1)
        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=agent1,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=False,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "Please transfer to feedback desk",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["action_taken"], "TRANSFER_FEEDBACK_AGENT")
        self.assertTrue(data["is_feedback_stage"])
        self.assertEqual(data["assigned_agent"]["agent_number"], 11)
        self.assertIn("Officer Ritu", data["reply"])
        self.assertIn("scale of 0 to 5", data["reply"])

        session.refresh_from_db()
        self.assertEqual(session.assigned_agent.agent_number, 11)
        self.assertTrue(session.is_feedback_stage)

    def test_new_call_never_starts_as_feedback_agent_11(self):
        """Verify new incoming/started calls NEVER initialize with Agent 11 (Officer Ritu)."""
        CallCenterAgent.seed_default_10_agents()

        # 1. Standard inbound call with no agent_id
        res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["is_ivr_active"])
        self.assertIsNone(data["assigned_agent"])
        self.assertNotIn("Officer Ritu", data["welcome_message"])
        self.assertIn("1912", data["welcome_message"])

        # 2. Call attempting to request agent_id=11 directly
        res2 = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({"agent_id": 11, "direct_connect": True}),
            content_type="application/json",
        )
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2["success"])
        self.assertTrue(data2["is_ivr_active"])
        self.assertIsNone(data2["assigned_agent"])
        self.assertNotIn("Officer Ritu", data2["welcome_message"])

    def test_ivr_dial_2_when_agent_2_offline_rejects_politely(self):
        """Verify pressing 2 when Agent 2 is offline does not connect to Agent 2 and notifies caller."""
        CallCenterAgent.seed_default_10_agents()
        ag2 = CallCenterAgent.objects.get(agent_number=2)
        ag2.status = CallCenterAgent.STATUS_OFFLINE
        ag2.is_active = False
        ag2.save()

        session = VoiceCallSession.objects.create(
            user_account=self.user,
            assigned_agent=None,
            caller_name=self.user.full_name,
            caller_phone=self.user.mobile_number,
            status=VoiceCallSession.STATUS_ACTIVE,
            is_ivr_active=True,
        )

        res = self.client.post(
            reverse("api_voice_agent_message"),
            data=json.dumps({
                "session_id": str(session.session_id),
                "message": "2",
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("AGENT_OFFLINE_REJECTED", data["action_taken"])
        self.assertIn("offline", data["reply"].lower())
        self.assertIsNone(data["assigned_agent"])
        self.assertTrue(data["is_ivr_active"])

        session.refresh_from_db()
        self.assertIsNone(session.assigned_agent)
        self.assertTrue(session.is_ivr_active)

    def test_admin_toggle_agent_status_ajax(self):
        """Verify the administration toggle endpoint toggles agent availability and persists."""
        from admins.views import ADMIN_AUTH_COOKIE, ADMIN_AUTH_SALT, ADMIN_USERNAME
        from django.http import HttpResponse

        CallCenterAgent.seed_default_10_agents()
        ag2 = CallCenterAgent.objects.get(agent_number=2)
        self.assertEqual(ag2.status, CallCenterAgent.STATUS_AVAILABLE)

        resp = HttpResponse()
        resp.set_signed_cookie(ADMIN_AUTH_COOKIE, ADMIN_USERNAME, salt=ADMIN_AUTH_SALT)
        self.client.cookies[ADMIN_AUTH_COOKIE] = resp.cookies[ADMIN_AUTH_COOKIE].value

        # Toggle to OFFLINE
        res = self.client.post(f"/admins/voice-agents/{ag2.id}/toggle/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["status"], CallCenterAgent.STATUS_OFFLINE)
        self.assertFalse(data["is_active"])

        ag2.refresh_from_db()
        self.assertEqual(ag2.status, CallCenterAgent.STATUS_OFFLINE)
        self.assertFalse(ag2.is_active)

        # Toggle back to AVAILABLE
        res2 = self.client.post(f"/admins/voice-agents/{ag2.id}/toggle/")
        self.assertEqual(res2.status_code, 200)
        data2 = res2.json()
        self.assertTrue(data2["success"])
        self.assertEqual(data2["status"], CallCenterAgent.STATUS_AVAILABLE)
        self.assertTrue(data2["is_active"])

        ag2.refresh_from_db()
        self.assertEqual(ag2.status, CallCenterAgent.STATUS_AVAILABLE)
        self.assertTrue(ag2.is_active)

    def test_admin_sample_voice_endpoint(self):
        """Verify the administration sample voice endpoint returns authentic neural audio."""
        from admins.views import ADMIN_AUTH_COOKIE, ADMIN_AUTH_SALT, ADMIN_USERNAME
        from django.http import HttpResponse

        CallCenterAgent.seed_default_10_agents()
        ag2 = CallCenterAgent.objects.get(agent_number=2)

        resp = HttpResponse()
        resp.set_signed_cookie(ADMIN_AUTH_COOKIE, ADMIN_USERNAME, salt=ADMIN_AUTH_SALT)
        self.client.cookies[ADMIN_AUTH_COOKIE] = resp.cookies[ADMIN_AUTH_COOKIE].value

        res = self.client.post(f"/admins/voice-agents/{ag2.id}/sample-voice/")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["officer_name"], "Officer Vikram")
        self.assertEqual(data["voice_name"], "en-IN-PrabhatNeural")
        self.assertIn("Officer Vikram", data["sample_text"])

    def test_start_call_direct_connect_to_offline_agent_falls_back_to_ivr(self):
        """Verify direct call to an offline agent falls back to IVR with alert notice."""
        CallCenterAgent.seed_default_10_agents()
        ag2 = CallCenterAgent.objects.get(agent_number=2)
        ag2.status = CallCenterAgent.STATUS_OFFLINE
        ag2.is_active = False
        ag2.save()

        res = self.client.post(
            reverse("api_voice_agent_start_call"),
            data=json.dumps({
                "agent_id": ag2.id,
                "direct_connect": True,
            }),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["is_ivr_active"])
        self.assertIsNone(data["assigned_agent"])
        self.assertIn("Officer Vikram", data["welcome_message"])
        self.assertIn("offline", data["welcome_message"].lower())









