import re
from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q

from .models import ConnectionApplication, UserAccount, UserAppliance


class CreateAccountForm(forms.ModelForm):
    """Form for user login account creation with field and password strength validations."""

    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Create a strong password"}),
        min_length=8,
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"placeholder": "Re-enter password"}),
    )

    class Meta:
        model = UserAccount
        fields = [
            "full_name",
            "username",
            "email",
            "mobile_number",
            "password",
        ]

    def clean_full_name(self):
        full_name = " ".join(self.cleaned_data.get("full_name", "").split())
        if len(full_name) < 3:
            raise ValidationError("Full name must be at least 3 characters long.")
        if not re.fullmatch(r"[A-Za-z.\-\s]+", full_name):
            raise ValidationError(
                "Full name may only contain letters, spaces, dots, and hyphens."
            )
        return full_name

    def clean_username(self):
        username = self.cleaned_data.get("username", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{4,30}", username):
            raise ValidationError(
                "Username must be 4-30 characters using only letters, numbers, and underscores."
            )
        if username.lower() == "admin":
            raise ValidationError("This username is reserved. Please choose another.")
        if UserAccount.objects.filter(username__iexact=username).exists():
            raise ValidationError("This username is already taken.")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email", "").strip().lower()
        if UserAccount.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email address already exists.")
        return email

    def clean_mobile_number(self):
        raw_mobile = self.cleaned_data.get("mobile_number", "").strip()
        mobile = re.sub(r"[\s\-]", "", raw_mobile)
        if not re.fullmatch(r"\d{10}", mobile) or len(set(mobile)) == 1:
            raise ValidationError("Please enter a valid 10-digit mobile number.")
        if UserAccount.objects.filter(mobile_number=mobile).exists():
            raise ValidationError("This mobile number is already registered.")
        return mobile

    def clean_password(self):
        password = self.cleaned_data.get("password", "")
        if len(password) < 8:
            raise ValidationError("Password must be at least 8 characters long.")
        if (
            not re.search(r"[A-Z]", password)
            or not re.search(r"[a-z]", password)
            or not re.search(r"\d", password)
        ):
            raise ValidationError(
                "Password must include uppercase, lowercase, and a number."
            )
        return password

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get("password")
        confirm_password = cleaned_data.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            self.add_error("confirm_password", "Passwords do not match.")
        return cleaned_data

    def save(self, commit=True):
        account = super().save(commit=False)
        account.status = UserAccount.STATUS_ACTIVE
        account.set_password(self.cleaned_data["password"])
        if commit:
            account.save()
        return account


class UserLoginForm(forms.Form):
    """Form validating self-registered UserAccount login credentials and status."""

    username = forms.CharField(
        max_length=100,
        strip=True,
        error_messages={"required": "Please enter your username or email."},
    )
    password = forms.CharField(
        widget=forms.PasswordInput,
        error_messages={"required": "Please enter your password."},
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authenticated_account = None

    def clean(self):
        cleaned_data = super().clean()
        identifier = cleaned_data.get("username", "").strip()
        password = cleaned_data.get("password", "")

        if not identifier or not password:
            return cleaned_data

        account = UserAccount.objects.filter(
            Q(username__iexact=identifier) | Q(email__iexact=identifier)
        ).first()

        if account is None or not account.check_password(password):
            raise ValidationError("Invalid username/email or password. Please try again.")

        if account.status in (UserAccount.STATUS_INACTIVE, UserAccount.STATUS_SUSPENDED):
            raise ValidationError(
                f"Your account status is {account.status}. Please contact Electricity Department support."
            )

        self.authenticated_account = account
        return cleaned_data


class ConnectionApplicationForm(forms.ModelForm):
    """Form for user household electricity connection application."""

    class Meta:
        model = ConnectionApplication
        fields = [
            "applicant_name",
            "mobile_number",
            "house_address",
            "property_type",
            "industry_type",
            "id_proof_number",
        ]

    def clean_applicant_name(self):
        name = " ".join(self.cleaned_data.get("applicant_name", "").split())
        if len(name) < 3:
            raise ValidationError("Applicant name must be at least 3 characters.")
        return name

    def clean_mobile_number(self):
        mobile = re.sub(r"[\s\-]", "", self.cleaned_data.get("mobile_number", "").strip())
        if not re.fullmatch(r"\d{10}", mobile) or len(set(mobile)) == 1:
            raise ValidationError("Enter a valid 10-digit mobile number.")
        return mobile

    def clean_house_address(self):
        addr = " ".join(self.cleaned_data.get("house_address", "").split())
        if len(addr) < 10:
            raise ValidationError("Please enter a complete household address (at least 10 characters).")
        return addr

    def clean_id_proof_number(self):
        proof = self.cleaned_data.get("id_proof_number", "").strip().upper()
        if len(proof) < 6:
            raise ValidationError("ID proof number must be at least 6 characters.")
        return proof

    def clean(self):
        cleaned = super().clean()
        prop_type = cleaned.get("property_type")
        industry = " ".join(cleaned.get("industry_type", "").split())
        if prop_type == ConnectionApplication.PROPERTY_COMMERCIAL:
            if not industry:
                self.add_error(
                    "industry_type",
                    "Please specify the industry type for a Commercial connection.",
                )
            else:
                cleaned["industry_type"] = industry
        else:
            cleaned["industry_type"] = ""
        return cleaned

    def save(self, commit=True):
        app = super().save(commit=False)
        app.requested_load_kw = (
            15.00
            if app.property_type == ConnectionApplication.PROPERTY_COMMERCIAL
            else 2.00
        )
        if commit:
            app.save()
        return app


DEFAULT_APPLIANCE_WATTS = {
    "Fan": 75,
    "Light": 40,
    "AC": 1500,
    "TV": 140,
    "Motor": 650,
    "Heater": 1800,
    "Server": 350,
    "Charger": 2200,
}


class UserApplianceForm(forms.ModelForm):
    """Form for adding a custom electronic appliance with automatic wattage rating."""

    class Meta:
        model = UserAppliance
        fields = ["name", "appliance_type"]

    def clean_name(self):
        name = " ".join(self.cleaned_data.get("name", "").split())
        if len(name) < 2:
            raise ValidationError("Appliance name must be at least 2 characters.")
        return name

    def save(self, commit=True):
        appliance = super().save(commit=False)
        appliance.wattage = DEFAULT_APPLIANCE_WATTS.get(appliance.appliance_type, 150)
        if commit:
            appliance.save()
        return appliance
