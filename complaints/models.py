import uuid
from django.db import models
from django.utils import timezone


class Complaint(models.Model):
    """Consumer complaint and outage reporting record registered by the AI Voice Agent
    as specified in IEEE paper 'AI-Powered Voice Agent for Electricity Department'.
    """

    STATUS_REGISTERED = "REGISTERED"
    STATUS_ASSIGNED = "ASSIGNED"
    STATUS_IN_PROGRESS = "IN_PROGRESS"
    STATUS_RESOLVED = "RESOLVED"
    STATUS_CLOSED = "CLOSED"

    STATUS_CHOICES = [
        (STATUS_REGISTERED, "Registered — Escalated to Control Room"),
        (STATUS_ASSIGNED, "Assigned to Field Line Crew"),
        (STATUS_IN_PROGRESS, "Field Team Working on Site"),
        (STATUS_RESOLVED, "Resolved & Power Supply Restored"),
        (STATUS_CLOSED, "Closed"),
    ]

    PRIORITY_LOW = "LOW"
    PRIORITY_NORMAL = "NORMAL"
    PRIORITY_HIGH = "HIGH"
    PRIORITY_CRITICAL = "CRITICAL_HAZARD"

    PRIORITY_CHOICES = [
        (PRIORITY_LOW, "Low"),
        (PRIORITY_NORMAL, "Normal"),
        (PRIORITY_HIGH, "High (Area Outage)"),
        (PRIORITY_CRITICAL, "Critical (Electrical Hazard / Sparking)"),
    ]

    ISSUE_POWER_OUTAGE = "Power Outage / No Power"
    ISSUE_VOLTAGE_FLUCTUATION = "Voltage Fluctuation / Low Voltage"
    ISSUE_TRANSFORMER_HAZARD = "Transformer Sparking / Hazard"
    ISSUE_WIRE_SNAP = "Fallen Wire / Broken Conductor"
    ISSUE_METER_FAULT = "Meter Burnt / Faulty Meter Display"
    ISSUE_BILLING_DISPUTE = "Billing Issue / Arrears Query"
    ISSUE_OTHER = "General Electricity Grievance"

    ISSUE_CHOICES = [
        (ISSUE_POWER_OUTAGE, "Power Outage / No Power"),
        (ISSUE_VOLTAGE_FLUCTUATION, "Voltage Fluctuation / Low Voltage"),
        (ISSUE_TRANSFORMER_HAZARD, "Transformer Sparking / Hazard"),
        (ISSUE_WIRE_SNAP, "Fallen Wire / Broken Conductor"),
        (ISSUE_METER_FAULT, "Meter Burnt / Faulty Meter Display"),
        (ISSUE_BILLING_DISPUTE, "Billing Issue / Arrears Query"),
        (ISSUE_OTHER, "General Electricity Grievance"),
    ]

    complaint_number = models.CharField(
        max_length=32,
        unique=True,
        help_text="Human-readable ID e.g. CMP-202611-0001"
    )
    complaint_id = models.CharField(
        max_length=8,
        unique=True,
        help_text="8-character unique tracking code e.g. A3F91B7C"
    )
    user_account = models.ForeignKey(
        "users.UserAccount",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="complaints"
    )
    connection = models.ForeignKey(
        "users.ConnectionApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="complaints"
    )
    caller_name = models.CharField(max_length=120)
    service_number = models.CharField(max_length=60, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    location = models.CharField(max_length=255)
    landmark = models.CharField(max_length=255, blank=True)
    issue_type = models.CharField(max_length=60, choices=ISSUE_CHOICES, default=ISSUE_POWER_OUTAGE)
    outage_duration = models.CharField(max_length=100, blank=True)
    impact_area = models.CharField(max_length=150, blank=True)
    hazard_observed = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_REGISTERED)
    priority = models.CharField(max_length=30, choices=PRIORITY_CHOICES, default=PRIORITY_NORMAL)
    estimated_resolution = models.CharField(max_length=120, default="Within 2 to 4 hours")
    assigned_field_team = models.CharField(max_length=150, blank=True)
    department_notes = models.TextField(blank=True)
    source = models.CharField(max_length=40, default="AI_VOICE_CALL")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.complaint_number} ({self.complaint_id}) - {self.issue_type} [{self.status}]"

    @classmethod
    def generate_next_identifiers(cls):
        """Generate human-readable complaint_number (CMP-YYYYMM-XXXX) and 8-character complaint_id."""
        now = timezone.now()
        prefix = f"CMP-{now.strftime('%Y%m')}-"
        count = cls.objects.filter(complaint_number__startswith=prefix).count() + 1
        comp_num = f"{prefix}{count:04d}"
        while cls.objects.filter(complaint_number=comp_num).exists():
            count += 1
            comp_num = f"{prefix}{count:04d}"

        # 8-character unique alphanumeric ID (upper)
        rand_id = uuid.uuid4().hex[:8].upper()
        while cls.objects.filter(complaint_id=rand_id).exists():
            rand_id = uuid.uuid4().hex[:8].upper()

        return comp_num, rand_id

    def save(self, *args, **kwargs):
        if not self.complaint_number or not self.complaint_id:
            num, cid = self.generate_next_identifiers()
            if not self.complaint_number:
                self.complaint_number = num
            if not self.complaint_id:
                self.complaint_id = cid
        super().save(*args, **kwargs)
