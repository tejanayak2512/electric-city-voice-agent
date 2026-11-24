from django.contrib import admin

from .models import (
    DepartmentEmployee,
    MaintenanceSchedule,
    OperationMaintenanceNotification,
    SiteMediaSetting,
    SiteNewsAnnouncement,
    TariffSetting,
    Transformer,
)


@admin.register(Transformer)
class TransformerAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "zone", "capacity_kva", "is_power_on", "voltage", "updated_at")
    list_filter = ("is_power_on",)
    search_fields = ("code", "name", "zone")


@admin.register(DepartmentEmployee)
class DepartmentEmployeeAdmin(admin.ModelAdmin):
    list_display = (
        "employee_id",
        "full_name",
        "username",
        "designation",
        "assigned_transformer",
        "is_active",
        "created_at",
    )
    list_filter = ("is_active", "designation")
    search_fields = ("employee_id", "full_name", "username", "email")


@admin.register(TariffSetting)
class TariffSettingAdmin(admin.ModelAdmin):
    list_display = ("base_fare_rs", "domestic_rate_per_unit", "commercial_rate_per_unit", "updated_by", "updated_at")


@admin.register(MaintenanceSchedule)
class MaintenanceScheduleAdmin(admin.ModelAdmin):
    list_display = ("schedule_id", "title", "scope", "status", "scheduled_start", "scheduled_end")
    list_filter = ("status", "scope")
    search_fields = ("schedule_id", "title", "reason")


@admin.register(SiteNewsAnnouncement)
class SiteNewsAnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "badge_label", "is_ticker", "is_active", "created_at")
    list_filter = ("is_active", "is_ticker")
    search_fields = ("title", "content")


@admin.register(OperationMaintenanceNotification)
class OperationMaintenanceNotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "category", "severity", "related_transformer", "affected_areas", "is_active", "created_at")
    list_filter = ("is_active", "category", "severity")
    search_fields = ("title", "message", "affected_areas")


@admin.register(SiteMediaSetting)
class SiteMediaSettingAdmin(admin.ModelAdmin):
    list_display = ("effective_bg_image_url", "effective_video_url", "hero_heading", "updated_at")

