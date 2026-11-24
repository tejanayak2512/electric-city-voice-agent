from django.contrib import admin
from .models import CallCenterAgent, VoiceCallMessage, VoiceCallSession


@admin.register(CallCenterAgent)
class CallCenterAgentAdmin(admin.ModelAdmin):
    list_display = ("agent_number", "short_name", "officer_name", "role", "gender", "status", "calls_handled", "satisfaction_rating", "is_active")
    list_filter = ("status", "gender", "is_active")
    search_fields = ("name", "officer_name", "role", "agent_code")
    ordering = ("agent_number",)


@admin.register(VoiceCallSession)
class VoiceCallSessionAdmin(admin.ModelAdmin):
    list_display = ("session_id", "caller_name", "assigned_agent", "status", "call_intent", "duration_seconds", "started_at")
    list_filter = ("status", "call_intent", "assigned_agent")
    search_fields = ("session_id", "caller_name", "caller_phone")


@admin.register(VoiceCallMessage)
class VoiceCallMessageAdmin(admin.ModelAdmin):
    list_display = ("session", "sender", "latency_ms", "intent_detected", "created_at")
    list_filter = ("sender",)

