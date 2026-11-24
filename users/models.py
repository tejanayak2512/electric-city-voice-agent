from decimal import Decimal

from django.contrib.auth.hashers import check_password as django_check_password
from django.contrib.auth.hashers import make_password
from django.db import models


class UserAccount(models.Model):
    """Self-registered user account for portal login with account status."""

    STATUS_ACTIVE = "Active"
    STATUS_PENDING = "Pending"
    STATUS_INACTIVE = "Inactive"
    STATUS_SUSPENDED = "Suspended"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_PENDING, "Pending"),
        (STATUS_INACTIVE, "Inactive"),
        (STATUS_SUSPENDED, "Suspended"),
    ]

    full_name = models.CharField(max_length=120)
    username = models.CharField(max_length=50, unique=True)
    email = models.EmailField(unique=True)
    mobile_number = models.CharField(max_length=15, unique=True)
    password = models.CharField(max_length=255)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
    )
    house_power_on = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def set_password(self, raw_password):
        """Hash and store the user's password."""
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        """Verify a raw password against the stored password hash."""
        return django_check_password(raw_password, self.password)

    def __str__(self):
        return f"{self.full_name} ({self.username} - {self.status})"


class ConnectionApplication(models.Model):
    """New electricity connection workflow: User Apply -> Dept Verify -> Admin Meter -> Dept Connect."""

    STATUS_APPLIED = "Applied"
    STATUS_VERIFIED = "Verified"
    STATUS_METER_ISSUED = "Meter Issued"
    STATUS_CONNECTED = "Connected"
    STATUS_REJECTED = "Rejected"

    STATUS_CHOICES = [
        (STATUS_APPLIED, "Applied (Pending Department Verification)"),
        (STATUS_VERIFIED, "Verified by Department (Sent to Administration)"),
        (STATUS_METER_ISSUED, "Meter Issued by Admin (Pending Household Connection)"),
        (STATUS_CONNECTED, "Connected to Household"),
        (STATUS_REJECTED, "Rejected"),
    ]

    PROPERTY_DOMESTIC = "Domestic"
    PROPERTY_COMMERCIAL = "Commercial"

    PROPERTY_CHOICES = [
        (PROPERTY_DOMESTIC, "Domestic"),
        (PROPERTY_COMMERCIAL, "Commercial"),
    ]

    INDUSTRY_CHOICES = [
        ("Manufacturing / Factory", "Manufacturing / Factory"),
        ("IT / Office Park", "IT / Office Park"),
        ("Retail Mall / Showroom", "Retail Mall / Showroom"),
        ("Cold Storage / Food Processing", "Cold Storage / Food Processing"),
        ("Hospital / Healthcare", "Hospital / Healthcare"),
        ("Textile / Workshop", "Textile / Workshop"),
    ]

    user = models.ForeignKey(
        UserAccount,
        on_delete=models.CASCADE,
        related_name="connections",
    )
    application_id = models.CharField(max_length=30, unique=True)
    application_number = models.CharField(max_length=32, unique=True, null=True, blank=True)
    applicant_name = models.CharField(max_length=120)
    mobile_number = models.CharField(max_length=15)
    house_address = models.TextField(max_length=300)
    property_type = models.CharField(
        max_length=30,
        choices=PROPERTY_CHOICES,
        default=PROPERTY_DOMESTIC,
    )
    industry_type = models.CharField(max_length=80, blank=True)
    requested_load_kw = models.DecimalField(max_digits=5, decimal_places=2, default=2.00)
    id_proof_number = models.CharField(max_length=40)
    transformer = models.ForeignKey(
        "admins.Transformer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="connections",
    )
    status = models.CharField(
        max_length=30,
        choices=STATUS_CHOICES,
        default=STATUS_APPLIED,
    )

    department_remarks = models.CharField(max_length=255, blank=True)
    verified_by = models.ForeignKey(
        "admins.DepartmentEmployee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="verified_applications",
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    connection_fee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    connection_fee_paid = models.BooleanField(default=False)
    connection_payment_method = models.CharField(max_length=40, blank=True)
    connection_transaction_id = models.CharField(max_length=50, blank=True)
    connection_receipt_number = models.CharField(max_length=40, blank=True)
    connection_paid_at = models.DateTimeField(null=True, blank=True)
    payment_verified = models.BooleanField(default=False)
    payment_verified_by = models.CharField(max_length=80, blank=True)
    payment_verified_at = models.DateTimeField(null=True, blank=True)
    payment_verification_remarks = models.CharField(max_length=255, blank=True)

    meter_number = models.CharField(max_length=30, unique=True, null=True, blank=True)
    original_meter_serial = models.CharField(max_length=40, null=True, blank=True)
    original_meter_verified = models.BooleanField(default=False)
    admin_remarks = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)

    connected_by = models.ForeignKey(
        "admins.DepartmentEmployee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="connected_applications",
    )
    connected_at = models.DateTimeField(null=True, blank=True)

    is_power_cut = models.BooleanField(default=False)
    power_cut_remark = models.CharField(max_length=255, blank=True)
    power_cut_updated_at = models.DateTimeField(null=True, blank=True)

    unbilled_units = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    last_billed_reading = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_units_consumed = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    prepaid_units_balance = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    prepaid_units_allocated_total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    appliance_states = models.JSONField(default=dict, blank=True)
    appliance_usage_stats = models.JSONField(default=dict, blank=True)
    last_usage_synced_at = models.DateTimeField(null=True, blank=True)
    pending_energy_kwh = models.DecimalField(max_digits=12, decimal_places=6, default=Decimal("0.000000"))

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if not self.application_number:
            from django.utils import timezone

            year = timezone.localdate().strftime("%Y")
            raw_suffix = (self.application_id or "").replace("REQ-", "").strip() or str(
                (ConnectionApplication.objects.order_by("-id").values_list("id", flat=True).first() or 0) + 1001
            )
            candidate = f"APP-{year}-{raw_suffix}"
            while ConnectionApplication.objects.filter(application_number=candidate).exclude(pk=self.pk).exists():
                raw_suffix = f"{raw_suffix}A"
                candidate = f"APP-{year}-{raw_suffix}"
            self.application_number = candidate
        super().save(*args, **kwargs)

    @property
    def is_connected(self):
        return self.status == self.STATUS_CONNECTED and self.transformer is not None

    @property
    def is_commercial(self):
        return self.property_type == self.PROPERTY_COMMERCIAL

    @property
    def has_prepaid_credits(self):
        return (not self.is_commercial) or self.prepaid_units_balance > Decimal("0.00")

    @property
    def has_active_grid_supply(self):
        return bool(
            self.is_connected
            and self.transformer.is_power_on
            and (not self.is_power_cut)
            and self.has_prepaid_credits
        )

    def __str__(self):
        return f"{self.application_id} - {self.applicant_name} ({self.status})"


class PowerBill(models.Model):
    """Unified Domestic monthly postpaid bill and Commercial prepaid allocation bill."""

    TYPE_MONTHLY = "Monthly"
    TYPE_PREPAID = "Prepaid"
    TYPE_COMMERCIAL = "Commercial"

    TYPE_CHOICES = [
        (TYPE_MONTHLY, "Domestic Monthly Bill"),
        (TYPE_PREPAID, "Commercial Bulk Recharge"),
        (TYPE_COMMERCIAL, "Commercial Published Bill"),
    ]

    STATUS_UNPAID = "Unpaid"
    STATUS_PAID = "Paid"
    STATUS_CARRIED_FORWARD = "Carried Forward"

    STATUS_CHOICES = [
        (STATUS_UNPAID, "Unpaid"),
        (STATUS_PAID, "Paid"),
        (STATUS_CARRIED_FORWARD, "Carried Forward"),
    ]

    connection = models.ForeignKey(
        ConnectionApplication,
        on_delete=models.CASCADE,
        related_name="bills",
    )
    bill_number = models.CharField(max_length=32, unique=True)
    bill_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    billing_month = models.CharField(max_length=7, blank=True)
    previous_reading = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    current_reading = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    units_billed = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    base_fare = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("30.00"))
    rate_per_unit = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("5.00"))
    energy_charge = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    arrears_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    carried_from_bills = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_UNPAID)
    bulk_pack_name = models.CharField(max_length=80, blank=True)
    appliance_breakdown = models.JSONField(default=list, blank=True)
    payment_method = models.CharField(max_length=30, blank=True)
    transaction_id = models.CharField(max_length=50, blank=True)
    gateway_status = models.CharField(max_length=30, default="Pending")
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.bill_number} - {self.connection.meter_number} ({self.bill_type}: Rs {self.total_amount})"


class UserAppliance(models.Model):
    """Custom electronic appliance added by a user for their house/commercial simulation."""

    TYPE_CHOICES = [
        ("Fan", "Ceiling / Exhaust Fan"),
        ("Light", "LED Light / Lamp"),
        ("AC", "Air Conditioner / Cooling"),
        ("TV", "TV / Display Monitor"),
        ("Motor", "Washing Machine / Pump / Motor"),
        ("Heater", "Water Heater / Microwave"),
        ("Server", "Computer / Router / Server"),
        ("Charger", "EV Charger / Inverter"),
    ]

    user = models.ForeignKey(
        UserAccount,
        on_delete=models.CASCADE,
        related_name="appliances",
    )
    name = models.CharField(max_length=80)
    appliance_type = models.CharField(
        max_length=30,
        choices=TYPE_CHOICES,
        default="Fan",
    )
    wattage = models.PositiveIntegerField(default=150)
    is_on = models.BooleanField(default=True)
    consumed_units = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    unbilled_consumed_units = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0.0000"))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.name} ({self.appliance_type} - {self.wattage}W)"


class UserNotification(models.Model):
    """Consumer portal notifications (invoice deliveries, voice agent actions, advisories)."""

    CATEGORY_BILLS = "bills"
    CATEGORY_LIVE = "live"
    CATEGORY_MAINTENANCE = "maintenance"

    CATEGORY_CHOICES = [
        (CATEGORY_BILLS, "Billing & Invoices"),
        (CATEGORY_LIVE, "Live Grid & Telemetry"),
        (CATEGORY_MAINTENANCE, "Maintenance & Outages"),
    ]

    SEVERITY_INFO = "info"
    SEVERITY_SUCCESS = "success"
    SEVERITY_WARNING = "warning"
    SEVERITY_CRITICAL = "critical"

    SEVERITY_CHOICES = [
        (SEVERITY_INFO, "Info"),
        (SEVERITY_SUCCESS, "Success"),
        (SEVERITY_WARNING, "Warning"),
        (SEVERITY_CRITICAL, "Critical"),
    ]

    user = models.ForeignKey(
        UserAccount,
        on_delete=models.CASCADE,
        related_name="custom_notifications",
    )
    category = models.CharField(max_length=30, choices=CATEGORY_CHOICES, default=CATEGORY_BILLS)
    category_label = models.CharField(max_length=60, default="Billing & Invoices")
    severity = models.CharField(max_length=20, choices=SEVERITY_CHOICES, default=SEVERITY_INFO)
    icon = models.CharField(max_length=60, default="fa-solid fa-file-invoice-dollar")
    badge = models.CharField(max_length=40, default="INVOICE DELIVERED")
    title = models.CharField(max_length=150)
    message = models.TextField()
    extra_info = models.CharField(max_length=120, blank=True)
    cta_label = models.CharField(max_length=60, default="View / Download Invoice")
    cta_url = models.CharField(max_length=255, blank=True)
    is_live = models.BooleanField(default=False)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.username} - {self.badge}: {self.title}"
