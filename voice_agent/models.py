import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone


class CallCenterAgent(models.Model):
    """Represents an official 24/7 AI Voice Calling Agent executive in the administration call center."""

    STATUS_AVAILABLE = "AVAILABLE"
    STATUS_ON_CALL = "ON_CALL"
    STATUS_BUSY = "BUSY"
    STATUS_OFFLINE = "OFFLINE"

    STATUS_CHOICES = [
        (STATUS_AVAILABLE, "Available / Online"),
        (STATUS_ON_CALL, "On Call with Caller"),
        (STATUS_BUSY, "Busy / In Diagnostic"),
        (STATUS_OFFLINE, "Offline / On Break"),
    ]

    GENDER_FEMALE = "FEMALE"
    GENDER_MALE = "MALE"
    GENDER_CHOICES = [
        (GENDER_FEMALE, "Female Voice Executive"),
        (GENDER_MALE, "Male Voice Executive"),
    ]

    agent_code = models.CharField(max_length=20, unique=True)
    agent_number = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120)
    short_name = models.CharField(max_length=50)
    officer_name = models.CharField(max_length=60)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, default=GENDER_FEMALE)
    role = models.CharField(max_length=140)
    department_desk = models.CharField(max_length=140, default="Central 24/7 Consumer Care Helpline")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_AVAILABLE)
    voice_type = models.CharField(max_length=60, default="en-IN Female")
    voice_pitch = models.FloatField(default=1.02)
    voice_rate = models.FloatField(default=0.96)
    avatar_color = models.CharField(max_length=30, default="#0284c7")
    avatar_icon = models.CharField(max_length=50, default="fa-headset")
    calls_handled = models.PositiveIntegerField(default=0)
    satisfaction_rating = models.DecimalField(max_digits=3, decimal_places=1, default=4.9)
    shift_timings = models.CharField(max_length=80, default="24/7 Live Shift")
    greeting_sample = models.TextField(blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["agent_number"]

    def __str__(self):
        return f"{self.short_name}: {self.officer_name} ({self.status})"

    @classmethod
    def get_available_agent(cls, preferred_agent_id=None):
        """Retrieve an available active agent (Agent 1 to 10) for incoming call routing."""
        cls.seed_default_10_agents()
        if preferred_agent_id:
            try:
                # Exclude Agent 11 (Feedback Agent) and only accept active agents that are NOT offline
                agent = (
                    cls.objects.filter(
                        Q(id=preferred_agent_id) | Q(agent_number=preferred_agent_id),
                        is_active=True,
                        agent_number__gte=1,
                        agent_number__lte=10,
                    )
                    .exclude(status=cls.STATUS_OFFLINE)
                    .first()
                )
                if agent:
                    return agent
                # If specific preferred agent was requested but is offline/inactive, return None
                # Do NOT silently substitute another agent when the caller asked for a specific officer!
                return None
            except Exception:
                return None

        # Try to find an Available agent first among Agent 1 to 10, prioritizing Agent 1 (Officer Priya)
        agent = (
            cls.objects.filter(
                status=cls.STATUS_AVAILABLE,
                is_active=True,
                agent_number__gte=1,
                agent_number__lte=10,
            )
            .order_by("agent_number", "calls_handled")
            .first()
        )
        if not agent:
            # Fallback to any active agent that is not offline (strictly Agents 1 to 10)
            agent = (
                cls.objects.filter(
                    is_active=True,
                    agent_number__gte=1,
                    agent_number__lte=10,
                )
                .exclude(status=cls.STATUS_OFFLINE)
                .order_by("agent_number")
                .first()
            )
        return agent

    @classmethod
    def seed_default_10_agents(cls):
        """Ensure all 10 standard administration call center agents exist."""
        default_agents = [
            {
                "agent_code": "AGENT-01",
                "agent_number": 1,
                "name": "Agent 1 - Officer Priya",
                "short_name": "Agent 1",
                "officer_name": "Officer Priya",
                "gender": cls.GENDER_FEMALE,
                "role": "Outage & Emergency Dispatch Specialist",
                "department_desk": "Substation Central Telemetry & Emergency Unit",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Female (Priya / Natural)",
                "voice_pitch": 1.02,
                "voice_rate": 0.96,
                "avatar_color": "#0284c7",
                "avatar_icon": "fa-headset",
                "calls_handled": 248,
                "satisfaction_rating": 4.9,
                "description": "Primary voice agent specializing in power cut diagnosis, feeder telemetry verification, and emergency field crew dispatch.",
            },
            {
                "agent_code": "AGENT-02",
                "agent_number": 2,
                "name": "Agent 2 - Officer Vikram",
                "short_name": "Agent 2",
                "officer_name": "Officer Vikram",
                "gender": cls.GENDER_MALE,
                "role": "High Voltage Grid & Substation Specialist",
                "department_desk": "33/11kV Substation Control Desk",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Male (Vikram / Natural)",
                "voice_pitch": 0.94,
                "voice_rate": 0.96,
                "avatar_color": "#0891b2",
                "avatar_icon": "fa-tower-broadcast",
                "calls_handled": 215,
                "satisfaction_rating": 4.8,
                "description": "Specialist in distribution transformer health, 33/11kV primary feeder trips, and high voltage load diagnostics.",
            },
            {
                "agent_code": "AGENT-03",
                "agent_number": 3,
                "name": "Agent 3 - Officer Ananya",
                "short_name": "Agent 3",
                "officer_name": "Officer Ananya",
                "gender": cls.GENDER_FEMALE,
                "role": "Billing, Tariffs & Meter Ledger Specialist",
                "department_desk": "Revenue & Billing Audit Desk",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Female (Ananya / Natural)",
                "voice_pitch": 1.04,
                "voice_rate": 0.96,
                "avatar_color": "#7c3aed",
                "avatar_icon": "fa-file-invoice-dollar",
                "calls_handled": 312,
                "satisfaction_rating": 4.9,
                "description": "Specialist in consumer billing ledgers, tariff slabs, arrears, due date queries, and online payment guidance.",
            },
            {
                "agent_code": "AGENT-04",
                "agent_number": 4,
                "name": "Agent 4 - Officer Rahul",
                "short_name": "Agent 4",
                "officer_name": "Officer Rahul",
                "gender": cls.GENDER_MALE,
                "role": "Electrical Safety & Hazard Response Officer",
                "department_desk": "Emergency Quick Response Unit",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Male (Rahul / Natural)",
                "voice_pitch": 0.92,
                "voice_rate": 0.96,
                "avatar_color": "#dc2626",
                "avatar_icon": "fa-triangle-exclamation",
                "calls_handled": 178,
                "satisfaction_rating": 5.0,
                "description": "Emergency safety officer specializing in transformer sparking, fallen wire isolation, and immediate public safety protocols.",
            },
            {
                "agent_code": "AGENT-05",
                "agent_number": 5,
                "name": "Agent 5 - Officer Sneha",
                "short_name": "Agent 5",
                "officer_name": "Officer Sneha",
                "gender": cls.GENDER_FEMALE,
                "role": "Substation & Transformer Diagnostic Specialist",
                "department_desk": "Grid Telemetry & Maintenance Cell",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Female (Sneha / Natural)",
                "voice_pitch": 1.03,
                "voice_rate": 0.96,
                "avatar_color": "#059669",
                "avatar_icon": "fa-microchip",
                "calls_handled": 194,
                "satisfaction_rating": 4.8,
                "description": "Diagnostics officer monitoring transformer load kVA, oil temperature alarms, and planned maintenance schedules.",
            },
            {
                "agent_code": "AGENT-06",
                "agent_number": 6,
                "name": "Agent 6 - Officer Rajesh",
                "short_name": "Agent 6",
                "officer_name": "Officer Rajesh",
                "gender": cls.GENDER_MALE,
                "role": "Smart Meter & Service Connection Officer",
                "department_desk": "Metering & Meter Testing Department",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Male (Rajesh / Natural)",
                "voice_pitch": 0.95,
                "voice_rate": 0.96,
                "avatar_color": "#d97706",
                "avatar_icon": "fa-gauge-high",
                "calls_handled": 260,
                "satisfaction_rating": 4.7,
                "description": "Specialist in new service applications, digital meter reading accuracy, meter replacement, and load sanctions.",
            },
            {
                "agent_code": "AGENT-07",
                "agent_number": 7,
                "name": "Agent 7 - Officer Kavita",
                "short_name": "Agent 7",
                "officer_name": "Officer Kavita",
                "gender": cls.GENDER_FEMALE,
                "role": "Citizen Grievance & Escalation Desk",
                "department_desk": "Consumer Ombudsman & Grievance Cell",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Female (Kavita / Natural)",
                "voice_pitch": 1.01,
                "voice_rate": 0.96,
                "avatar_color": "#db2777",
                "avatar_icon": "fa-clipboard-check",
                "calls_handled": 226,
                "satisfaction_rating": 4.9,
                "description": "Escalation supervisor handling delayed complaints, resolution verification, and field team compliance.",
            },
            {
                "agent_code": "AGENT-08",
                "agent_number": 8,
                "name": "Agent 8 - Officer Amit",
                "short_name": "Agent 8",
                "officer_name": "Officer Amit",
                "gender": cls.GENDER_MALE,
                "role": "Field Dispatch & Line Crew Coordinator",
                "department_desk": "Field Operation & Crew Logistics",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Male (Amit / Natural)",
                "voice_pitch": 0.93,
                "voice_rate": 0.96,
                "avatar_color": "#2563eb",
                "avatar_icon": "fa-truck-fast",
                "calls_handled": 185,
                "satisfaction_rating": 4.8,
                "description": "Coordinator tracking real-time line repair vans, replacement transformer transport, and restoration ETAs.",
            },
            {
                "agent_code": "AGENT-09",
                "agent_number": 9,
                "name": "Agent 9 - Officer Pooja",
                "short_name": "Agent 9",
                "officer_name": "Officer Pooja",
                "gender": cls.GENDER_FEMALE,
                "role": "Commercial Accounts & Energy Audit Specialist",
                "department_desk": "Commercial & Industrial Bulk Consumer Desk",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Female (Pooja / Natural)",
                "voice_pitch": 1.05,
                "voice_rate": 0.96,
                "avatar_color": "#4f46e5",
                "avatar_icon": "fa-industry",
                "calls_handled": 142,
                "satisfaction_rating": 4.9,
                "description": "Specialist in industrial three-phase connections, sanctioned load increases, and power factor audits.",
            },
            {
                "agent_code": "AGENT-10",
                "agent_number": 10,
                "name": "Agent 10 - Officer Suresh",
                "short_name": "Agent 10",
                "officer_name": "Officer Suresh",
                "gender": cls.GENDER_MALE,
                "role": "24/7 Helpline Shift In-Charge",
                "department_desk": "Central Operations Command Center",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Male (Suresh / Natural)",
                "voice_pitch": 0.91,
                "voice_rate": 0.96,
                "avatar_color": "#0f766e",
                "avatar_icon": "fa-user-tie",
                "calls_handled": 340,
                "satisfaction_rating": 5.0,
                "description": "Senior shift in-charge overseeing all telephone helplines, grid alerts, and inter-departmental communications.",
            },
            {
                "agent_code": "AGENT-11",
                "agent_number": 11,
                "name": "Agent 11 - Officer Ritu",
                "short_name": "Feedback Agent",
                "officer_name": "Officer Ritu",
                "gender": cls.GENDER_FEMALE,
                "role": "Quality Assurance & Consumer Feedback Specialist",
                "department_desk": "Quality Assurance & Feedback Desk",
                "status": cls.STATUS_AVAILABLE,
                "voice_type": "en-IN Female (Ritu / Natural)",
                "voice_pitch": 1.03,
                "voice_rate": 0.96,
                "avatar_color": "#ec4899",
                "avatar_icon": "fa-star",
                "calls_handled": 620,
                "satisfaction_rating": 4.95,
                "description": "Specialized quality assurance officer collecting post-call CSAT feedback ratings (scale 0-5) and closing calls.",
            },
        ]

        for item in default_agents:
            code = item["agent_code"]
            cls.objects.get_or_create(agent_code=code, defaults=item)


class VoiceCallSession(models.Model):
    """Tracks each call placed to the AI Voice Agent (audio calling session)."""

    STATUS_ACTIVE = "ACTIVE"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_DISCONNECTED = "DISCONNECTED"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active Call"),
        (STATUS_COMPLETED, "Completed Normally"),
        (STATUS_DISCONNECTED, "Disconnected / User Hung Up"),
    ]

    INTENT_COMPLAINT_REGISTRATION = "COMPLAINT_REGISTRATION"
    INTENT_STATUS_VERIFICATION = "STATUS_VERIFICATION"
    INTENT_BILLING_INQUIRY = "BILLING_INQUIRY"
    INTENT_POWER_STATUS = "POWER_STATUS"
    INTENT_GENERAL_INQUIRY = "GENERAL_INQUIRY"
    INTENT_FEEDBACK_COLLECTION = "FEEDBACK_COLLECTION"

    INTENT_CHOICES = [
        (INTENT_COMPLAINT_REGISTRATION, "Complaint Registration"),
        (INTENT_STATUS_VERIFICATION, "Status Verification"),
        (INTENT_BILLING_INQUIRY, "Billing & Tariff Inquiry"),
        (INTENT_POWER_STATUS, "Power Outage & Feeder Status"),
        (INTENT_GENERAL_INQUIRY, "General Electricity Assistance"),
        (INTENT_FEEDBACK_COLLECTION, "Quality Assurance & Feedback Rating"),
    ]

    session_id = models.CharField(max_length=64, unique=True, default=uuid.uuid4)
    user_account = models.ForeignKey(
        "users.UserAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voice_calls"
    )
    caller_name = models.CharField(max_length=120, default="Consumer")
    caller_phone = models.CharField(max_length=20, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    call_intent = models.CharField(max_length=60, choices=INTENT_CHOICES, default=INTENT_GENERAL_INQUIRY)
    associated_complaint = models.ForeignKey(
        "complaints.Complaint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="call_sessions"
    )
    assigned_agent = models.ForeignKey(
        "CallCenterAgent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="call_sessions"
    )
    is_ivr_active = models.BooleanField(default=True)
    is_feedback_stage = models.BooleanField(default=False)
    feedback_rating = models.PositiveSmallIntegerField(null=True, blank=True)
    feedback_submitted_at = models.DateTimeField(null=True, blank=True)
    summary = models.TextField(blank=True)
    avg_latency_ms = models.PositiveIntegerField(default=1200)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Call {self.session_id[:8]} - {self.caller_name} ({self.status})"

    def close_call(self):
        if not self.ended_at:
            self.ended_at = timezone.now()
            if self.started_at:
                diff = (self.ended_at - self.started_at).total_seconds()
                self.duration_seconds = max(1, int(diff))
        self.status = self.STATUS_COMPLETED
        self.save()


class VoiceCallMessage(models.Model):
    """Turn-by-turn dialogue messages in an AI voice calling session."""

    SENDER_CALLER = "CALLER"
    SENDER_AI = "AI_AGENT"
    SENDER_SYSTEM = "SYSTEM"

    SENDER_CHOICES = [
        (SENDER_CALLER, "Caller (Consumer)"),
        (SENDER_AI, "AI Voice Agent"),
        (SENDER_SYSTEM, "System Notification"),
    ]

    session = models.ForeignKey(
        VoiceCallSession,
        on_delete=models.CASCADE,
        related_name="messages"
    )
    sender = models.CharField(max_length=20, choices=SENDER_CHOICES)
    text = models.TextField()
    latency_ms = models.PositiveIntegerField(default=0)
    intent_detected = models.CharField(max_length=60, blank=True)
    action_taken = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"[{self.sender}] {self.text[:40]}"
