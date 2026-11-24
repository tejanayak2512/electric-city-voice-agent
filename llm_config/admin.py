from django.contrib import admin
from .models import LLMConfiguration


@admin.register(LLMConfiguration)
class LLMConfigurationAdmin(admin.ModelAdmin):
    list_display = ("name", "provider", "model_name", "temperature", "max_tokens", "is_default", "is_active", "updated_at")
    list_filter = ("provider", "is_default", "is_active")
    search_fields = ("name", "model_name", "system_prompt")
