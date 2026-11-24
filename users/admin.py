from django.contrib import admin

from .models import ConnectionApplication, UserAccount, UserAppliance


@admin.register(UserAccount)
class UserAccountAdmin(admin.ModelAdmin):
    list_display = (
        "username",
        "full_name",
        "email",
        "mobile_number",
        "status",
        "created_at",
    )
    list_filter = ("status", "created_at")
    search_fields = ("username", "full_name", "email", "mobile_number")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ConnectionApplication)
class ConnectionApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "application_id",
        "applicant_name",
        "user",
        "transformer",
        "meter_number",
        "original_meter_verified",
        "status",
        "created_at",
    )
    list_filter = ("status", "original_meter_verified", "transformer")
    search_fields = ("application_id", "applicant_name", "meter_number", "id_proof_number")


@admin.register(UserAppliance)
class UserApplianceAdmin(admin.ModelAdmin):
    list_display = ("name", "appliance_type", "wattage", "is_on", "user", "created_at")
    list_filter = ("appliance_type", "is_on")
    search_fields = ("name", "user__username")
