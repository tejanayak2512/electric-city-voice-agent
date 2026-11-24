from decimal import Decimal

from django.contrib.auth.hashers import check_password as django_check_password
from django.contrib.auth.hashers import make_password
from django.db import models


class Transformer(models.Model):
    """Represents a substation distribution transformer controlled by Administration."""

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    substation_name = models.CharField(max_length=120, default="Main 33/11 kV Substation")
    zone = models.CharField(max_length=100)
    capacity_kva = models.PositiveIntegerField(default=500)
    is_power_on = models.BooleanField(default=True)
    voltage = models.CharField(max_length=30, default="11 kV / 230 V")
    frequency = models.CharField(max_length=20, default="50 Hz")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    @classmethod
    def ensure_defaults(cls):
        """Seed initial multi-transformer grid units if none exist yet."""
        if not cls.objects.exists():
            defaults = [
                {
                    "code": "T-01",
                    "name": "North Substation Transformer T-01",
                    "substation_name": "North 33/11 kV Primary Substation",
                    "zone": "North Residential Feeder",
                    "capacity_kva": 500,
                    "is_power_on": True,
                },
                {
                    "code": "T-02",
                    "name": "Central Grid Transformer T-02",
                    "substation_name": "Central 33/11 kV Grid Substation",
                    "zone": "Central Commercial & Housing",
                    "capacity_kva": 750,
                    "is_power_on": True,
                },
                {
                    "code": "T-03",
                    "name": "South Substation Transformer T-03",
                    "substation_name": "South 33/11 kV Industrial Substation",
                    "zone": "South Industrial & Colony",
                    "capacity_kva": 1000,
                    "is_power_on": True,
                },
            ]
            for item in defaults:
                cls.objects.create(**item)
        return cls.objects.all()

    def set_power(self, is_on):
        """Update power state and live electrical telemetry fields."""
        self.is_power_on = bool(is_on)
        self.voltage = "11 kV / 230 V" if self.is_power_on else "0 V (Tripped)"
        self.frequency = "50 Hz" if self.is_power_on else "0 Hz"
        self.save(update_fields=["is_power_on", "voltage", "frequency", "updated_at"])

    def __str__(self):
        state = "ON" if self.is_power_on else "OFF"
        return f"{self.code} - {self.zone} ({state})"


class DepartmentEmployee(models.Model):
    """Department employee account created and toggled by Administration."""

    ROLE_CHOICES = [
        ("Field Engineer", "Field Engineer"),
        ("Meter Inspector", "Meter Inspector"),
        ("Line Technician", "Line Technician"),
        ("Substation Officer", "Substation Officer"),
    ]

    employee_id = models.CharField(max_length=20, unique=True)
    full_name = models.CharField(max_length=120)
    username = models.CharField(max_length=50, unique=True)
    email = models.EmailField(unique=True)
    mobile_number = models.CharField(max_length=15)
    designation = models.CharField(
        max_length=60,
        choices=ROLE_CHOICES,
        default="Field Engineer",
    )
    assigned_transformer = models.ForeignKey(
        Transformer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="employees",
    )
    password = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return django_check_password(raw_password, self.password)

    def __str__(self):
        state = "Active" if self.is_active else "Inactive"
        return f"{self.employee_id} - {self.full_name} ({state})"


class TariffSetting(models.Model):
    """Dynamic grid tariff configuration managed by Administration and published to Users & Department."""

    DEFAULT_BASE_FARE = Decimal("30.00")
    DEFAULT_DOMESTIC_RATE = Decimal("5.00")
    DEFAULT_COMMERCIAL_RATE = Decimal("7.00")
    DEFAULT_DOMESTIC_CONN_FEE = Decimal("500.00")
    DEFAULT_COMMERCIAL_CONN_FEE = Decimal("1500.00")

    base_fare_rs = models.DecimalField(
        max_digits=8, decimal_places=2, default=DEFAULT_BASE_FARE
    )
    domestic_rate_per_unit = models.DecimalField(
        max_digits=8, decimal_places=2, default=DEFAULT_DOMESTIC_RATE
    )
    commercial_rate_per_unit = models.DecimalField(
        max_digits=8, decimal_places=2, default=DEFAULT_COMMERCIAL_RATE
    )
    domestic_connection_fee = models.DecimalField(
        max_digits=8, decimal_places=2, default=DEFAULT_DOMESTIC_CONN_FEE
    )
    commercial_connection_fee = models.DecimalField(
        max_digits=8, decimal_places=2, default=DEFAULT_COMMERCIAL_CONN_FEE
    )
    revision_note = models.CharField(
        max_length=200,
        blank=True,
        default="Standard DISCOM Approved Tariff",
    )
    updated_by = models.CharField(max_length=60, default="admin")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_active(cls):
        """Fetch or initialize the active singleton tariff configuration."""
        obj = cls.objects.order_by("id").first()
        if obj is None:
            obj = cls.objects.create(
                base_fare_rs=cls.DEFAULT_BASE_FARE,
                domestic_rate_per_unit=cls.DEFAULT_DOMESTIC_RATE,
                commercial_rate_per_unit=cls.DEFAULT_COMMERCIAL_RATE,
                domestic_connection_fee=cls.DEFAULT_DOMESTIC_CONN_FEE,
                commercial_connection_fee=cls.DEFAULT_COMMERCIAL_CONN_FEE,
                revision_note="Standard DISCOM Approved Tariff",
                updated_by="admin",
            )
        return obj

    def __str__(self):
        return (
            f"Base Rs {self.base_fare_rs} | Domestic Rs {self.domestic_rate_per_unit}/Unit | "
            f"Commercial Rs {self.commercial_rate_per_unit}/Unit"
        )


class MaintenanceSchedule(models.Model):
    """Maintenance schedule & power cut permission request submitted by Department and approved/executed by Admin."""

    SCOPE_TRANSFORMER = "transformer"
    SCOPE_PREMISE = "premise"
    SCOPE_ALL = "all"
    SCOPE_CHOICES = [
        (SCOPE_TRANSFORMER, "Transformer Feeder"),
        (SCOPE_PREMISE, "Individual Home / Industry"),
        (SCOPE_ALL, "Overall Grid (All Transformers)"),
    ]

    STATUS_PENDING = "Pending"
    STATUS_APPROVED = "Approved"
    STATUS_ACTIVE_CUT = "Power Cut Active"
    STATUS_COMPLETED = "Completed"
    STATUS_REJECTED = "Rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending Admin Approval"),
        (STATUS_APPROVED, "Approved (Ready for Cut)"),
        (STATUS_ACTIVE_CUT, "Power Cut Active"),
        (STATUS_COMPLETED, "Completed & Restored"),
        (STATUS_REJECTED, "Rejected"),
    ]

    schedule_id = models.CharField(max_length=24, unique=True)
    requested_by = models.ForeignKey(
        DepartmentEmployee,
        on_delete=models.CASCADE,
        related_name="maintenance_requests",
    )
    scope = models.CharField(
        max_length=20,
        choices=SCOPE_CHOICES,
        default=SCOPE_TRANSFORMER,
    )
    transformer = models.ForeignKey(
        Transformer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="maintenance_schedules",
    )
    target_connection = models.ForeignKey(
        "users.ConnectionApplication",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="maintenance_schedules",
    )
    title = models.CharField(max_length=120)
    reason = models.TextField(max_length=400)
    scheduled_start = models.DateTimeField()
    scheduled_end = models.DateTimeField()
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    admin_remarks = models.CharField(max_length=255, blank=True)
    reviewed_by = models.CharField(max_length=60, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    cut_executed_at = models.DateTimeField(null=True, blank=True)
    restored_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        target = (
            self.target_connection.meter_number
            if self.scope == self.SCOPE_PREMISE and self.target_connection
            else (self.transformer.code if self.transformer else "ALL TRANSFORMERS")
        )
        return f"{self.schedule_id} - {self.title} ({target} : {self.status})"


class SiteNewsAnnouncement(models.Model):
    """Public news and general announcements shown on the public portal (base.html)."""

    title = models.CharField(max_length=255)
    content = models.TextField(blank=True)
    link_url = models.CharField(max_length=500, blank=True)
    badge_label = models.CharField(max_length=40, default="New")
    is_ticker = models.BooleanField(
        default=True,
        help_text="Display in top breaking news / ticker bar"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        status = "Active" if self.is_active else "Inactive"
        return f"{self.title} [{self.badge_label}] ({status})"


class OperationMaintenanceNotification(models.Model):
    """Operation & Maintenance notifications, power cut advisories, and feeder alerts pushed to base.html."""

    CATEGORY_MAINTENANCE = "maintenance"
    CATEGORY_GRID_OPERATION = "operation"
    CATEGORY_OUTAGE_ALERT = "alert"
    CATEGORY_EMERGENCY = "emergency"
    CATEGORY_ADVISORY = "advisory"

    CATEGORY_CHOICES = [
        (CATEGORY_MAINTENANCE, "Scheduled Grid Maintenance"),
        (CATEGORY_GRID_OPERATION, "Feeder Operations Notice"),
        (CATEGORY_OUTAGE_ALERT, "Power Cut / Outage Alert"),
        (CATEGORY_EMERGENCY, "Emergency Disruption"),
        (CATEGORY_ADVISORY, "General Consumer Advisory"),
    ]

    SEVERITY_INFO = "info"
    SEVERITY_WARNING = "warning"
    SEVERITY_CRITICAL = "critical"

    SEVERITY_CHOICES = [
        (SEVERITY_INFO, "Info (Blue)"),
        (SEVERITY_WARNING, "Warning (Amber)"),
        (SEVERITY_CRITICAL, "Critical (Red)"),
    ]

    title = models.CharField(max_length=255)
    message = models.TextField()
    category = models.CharField(
        max_length=40,
        choices=CATEGORY_CHOICES,
        default=CATEGORY_MAINTENANCE,
    )
    severity = models.CharField(
        max_length=20,
        choices=SEVERITY_CHOICES,
        default=SEVERITY_WARNING,
    )
    related_transformer = models.ForeignKey(
        Transformer,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    affected_areas = models.CharField(max_length=200, blank=True, help_text="e.g. North Zone, Feeder 2")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        status = "Active" if self.is_active else "Inactive"
        return f"[{self.get_severity_display()}] {self.title} ({status})"


class SiteMediaSetting(models.Model):
    """Configuration for base.html background image, hero video, and promotional visual media."""

    hero_bg_image = models.FileField(
        upload_to="site_media/backgrounds/",
        null=True,
        blank=True,
        help_text="Upload custom background image for base.html hero section",
    )
    hero_bg_image_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Direct URL or static path (e.g. /static/images/electricity-grid-background.png or https://...)",
    )
    hero_video = models.FileField(
        upload_to="site_media/videos/",
        null=True,
        blank=True,
        help_text="Upload custom hero video for base.html",
    )
    hero_video_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Direct URL or static path (e.g. /static/videos/ai-electricity-voice-agent.mp4)",
    )
    hero_video_poster = models.FileField(
        upload_to="site_media/posters/",
        null=True,
        blank=True,
        help_text="Upload custom video poster image",
    )
    hero_video_poster_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Poster URL or static path (optional)",
    )
    hero_heading = models.CharField(
        max_length=200,
        blank=True,
        help_text="Custom hero title override",
    )
    hero_subheading = models.TextField(
        blank=True,
        help_text="Custom hero subtitle text override",
    )
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get_active(cls):
        obj = cls.objects.first()
        if not obj:
            obj = cls.objects.create(
                hero_bg_image_url="/static/images/electricity-grid-background.png",
                hero_video_url="/static/videos/ai-electricity-voice-agent.mp4",
                hero_video_poster_url="/static/images/electricity-grid-background.png",
            )
        return obj

    @property
    def effective_bg_image_url(self):
        if self.hero_bg_image:
            try:
                return self.hero_bg_image.url
            except Exception:
                pass
        if self.hero_bg_image_url and self.hero_bg_image_url.strip():
            return self.hero_bg_image_url.strip()
        return "/static/images/electricity-grid-background.png"

    @property
    def effective_video_url(self):
        if self.hero_video:
            try:
                return self.hero_video.url
            except Exception:
                pass
        if self.hero_video_url and self.hero_video_url.strip():
            return self.hero_video_url.strip()
        return "/static/videos/ai-electricity-voice-agent.mp4"

    @property
    def effective_poster_url(self):
        if self.hero_video_poster:
            try:
                return self.hero_video_poster.url
            except Exception:
                pass
        if self.hero_video_poster_url and self.hero_video_poster_url.strip():
            return self.hero_video_poster_url.strip()
        return self.effective_bg_image_url

    def __str__(self):
        return f"Site Media Settings (Updated {self.updated_at.strftime('%Y-%m-%d %H:%M') if self.updated_at else 'Initial'})"


