import re
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import DepartmentEmployee, Transformer


class TransformerForm(forms.ModelForm):
    """Form for adding a new substation transformer unit."""

    substation_name = forms.CharField(max_length=120, required=False)

    class Meta:
        model = Transformer
        fields = ["code", "name", "substation_name", "zone", "capacity_kva"]

    def clean_code(self):
        code = self.cleaned_data.get("code", "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9\-]{2,20}", code):
            raise ValidationError("Transformer code must be 2-20 alphanumeric/hyphen characters (e.g. T-04).")
        if Transformer.objects.filter(code__iexact=code).exists():
            raise ValidationError("A transformer with this code already exists.")
        return code

    def clean_name(self):
        name = " ".join(self.cleaned_data.get("name", "").split())
        if len(name) < 4:
            raise ValidationError("Transformer name must be at least 4 characters.")
        return name

    def clean_zone(self):
        zone = " ".join(self.cleaned_data.get("zone", "").split())
        if len(zone) < 3:
            raise ValidationError("Zone name must be at least 3 characters.")
        return zone

    def clean_substation_name(self):
        sub = " ".join(self.cleaned_data.get("substation_name", "").split())
        if sub and len(sub) < 4:
            raise ValidationError("Substation name must be at least 4 characters.")
        return sub

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("substation_name"):
            zone = cleaned.get("zone", "Grid")
            cleaned["substation_name"] = f"{zone} 33/11 kV Substation"
        return cleaned

    def clean_capacity_kva(self):
        cap = self.cleaned_data.get("capacity_kva")
        if cap is None or cap < 50 or cap > 10000:
            raise ValidationError("Capacity must be between 50 and 10000 kVA.")
        return cap


class DepartmentEmployeeForm(forms.ModelForm):
    """Admin form to create Department employees with validation."""

    password = forms.CharField(
        widget=forms.PasswordInput,
        min_length=6,
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput,
    )

    class Meta:
        model = DepartmentEmployee
        fields = [
            "full_name",
            "username",
            "email",
            "mobile_number",
            "designation",
            "assigned_transformer",
            "password",
            "is_active",
        ]

    def clean_full_name(self):
        name = " ".join(self.cleaned_data.get("full_name", "").split())
        if len(name) < 3 or not re.fullmatch(r"[A-Za-z.\-\s]+", name):
            raise ValidationError("Enter a valid full name (at least 3 letters).")
        return name

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,30}", username):
            raise ValidationError("Username must be 4-30 alphanumeric/underscore characters.")
        if username.lower() == "admin":
            raise ValidationError("Username 'admin' is reserved.")
        if DepartmentEmployee.objects.filter(username__iexact=username).exists():
            raise ValidationError("This department username is already in use.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if DepartmentEmployee.objects.filter(email__iexact=email).exists():
            raise ValidationError("An employee with this email already exists.")
        return email

    def clean_mobile_number(self):
        mobile = re.sub(r"[\s\-]", "", self.cleaned_data.get("mobile_number", "").strip())
        if not re.fullmatch(r"\d{10}", mobile) or len(set(mobile)) == 1:
            raise ValidationError("Please enter a valid 10-digit mobile number.")
        return mobile

    def clean(self):
        cleaned = super().clean()
        pwd = cleaned.get("password")
        cpwd = cleaned.get("confirm_password")
        if pwd and cpwd and pwd != cpwd:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned

    def save(self, commit=True):
        emp = super().save(commit=False)
        emp.set_password(self.cleaned_data["password"])
        if not emp.employee_id:
            last_id = DepartmentEmployee.objects.order_by("-id").values_list("id", flat=True).first() or 0
            seq = last_id + 1
            while DepartmentEmployee.objects.filter(employee_id=f"DEPT-{seq:03d}").exists():
                seq += 1
            emp.employee_id = f"DEPT-{seq:03d}"
        if commit:
            emp.save()
        return emp


class DepartmentLoginForm(forms.Form):
    """2FA login form validating Employee ID and active DepartmentEmployee username/password."""

    employee_id = forms.CharField(
        max_length=20,
        strip=True,
        error_messages={"required": "Please enter and verify your Employee ID."},
    )
    username = forms.CharField(
        max_length=100,
        strip=True,
        error_messages={"required": "Please enter your department username or email."},
    )
    password = forms.CharField(
        widget=forms.PasswordInput,
        error_messages={"required": "Please enter your password."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authenticated_employee = None

    def clean(self):
        cleaned = super().clean()
        emp_id = cleaned.get("employee_id", "").strip().upper()
        identifier = cleaned.get("username", "").strip()
        password = cleaned.get("password", "")
        if not emp_id or not identifier or not password:
            return cleaned

        emp = DepartmentEmployee.objects.filter(employee_id__iexact=emp_id).first()
        if emp is None:
            raise ValidationError("Invalid Employee ID. Verification failed.")

        if not emp.is_active:
            raise ValidationError(
                "Access Denied: Your department account is currently deactivated by Administration."
            )

        matches_user = (
            emp.username.lower() == identifier.lower()
            or emp.email.lower() == identifier.lower()
        )
        if not matches_user or not emp.check_password(password):
            raise ValidationError(
                "2FA Verification failed: Username or password does not match this Employee ID."
            )

        self.authenticated_employee = emp
        return cleaned
