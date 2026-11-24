from decimal import Decimal
from django.db import models


class LLMConfiguration(models.Model):
    """Database-backed LLM configurations managed via Admin UI without requiring .env file changes."""

    PROVIDER_GEMINI = "gemini"
    PROVIDER_OPENAI = "openai"
    PROVIDER_GROQ = "groq"
    PROVIDER_ANTHROPIC = "anthropic"
    PROVIDER_OLLAMA = "ollama"
    PROVIDER_CUSTOM = "custom"

    PROVIDER_CHOICES = [
        (PROVIDER_GEMINI, "Google Gemini (GenAI)"),
        (PROVIDER_OPENAI, "OpenAI (GPT)"),
        (PROVIDER_GROQ, "Groq (Fast Cloud LLM)"),
        (PROVIDER_ANTHROPIC, "Anthropic Claude"),
        (PROVIDER_OLLAMA, "Ollama (Local LLM)"),
        (PROVIDER_CUSTOM, "Custom OpenAI-Compatible API"),
    ]

    name = models.CharField(max_length=120, help_text="Display name for this LLM configuration")
    provider = models.CharField(max_length=30, choices=PROVIDER_CHOICES, default=PROVIDER_GEMINI)
    api_key = models.CharField(max_length=255, blank=True, help_text="API Key (stored securely in DB; leave blank for Ollama/local)")
    api_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Custom API endpoint (e.g. http://localhost:11434/v1 for Ollama, or OpenAI endpoint)"
    )
    model_name = models.CharField(max_length=120, default="gemini-1.5-flash", help_text="e.g. gemini-1.5-flash, gpt-4o-mini, llama-3.1-70b-versatile")
    temperature = models.DecimalField(max_digits=3, decimal_places=2, default=Decimal("0.70"))
    max_tokens = models.PositiveIntegerField(default=1024)
    system_prompt = models.TextField(
        default="You are an intelligent, courteous AI voice and chat assistant for the Electricity Department / DISCOM.",
        help_text="Default system instructions for AI queries"
    )
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(
        default=False,
        help_text="If True, this configuration is fetched by default when any feature calls an LLM"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "-updated_at", "-id"]

    def save(self, *args, **kwargs):
        # If this config is marked default, unset is_default on others
        if self.is_default:
            LLMConfiguration.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_active_config(cls):
        """Fetch the default active LLM configuration from database, seeding a default record if none exists."""
        cfg = cls.objects.filter(is_active=True, is_default=True).first()
        if not cfg:
            cfg = cls.objects.filter(is_active=True).first()
        if not cfg:
            cfg = cls.objects.create(
                name="Default Gemini 1.5 Flash",
                provider=cls.PROVIDER_GEMINI,
                model_name="gemini-1.5-flash",
                api_key="",
                api_url="",
                temperature=Decimal("0.70"),
                max_tokens=1024,
                system_prompt="You are an intelligent, courteous AI assistant for the Electricity Department.",
                is_active=True,
                is_default=True,
            )
        return cfg

    @property
    def masked_api_key(self):
        """Display masked API key for security."""
        if not self.api_key:
            return "Not Configured"
        k = self.api_key.strip()
        if len(k) <= 8:
            return "••••" + k[-2:]
        return k[:4] + "••••••••" + k[-4:]

    def __str__(self):
        default_tag = " [DEFAULT]" if self.is_default else ""
        state_tag = "Active" if self.is_active else "Inactive"
        return f"{self.name} ({self.get_provider_display()} - {self.model_name}){default_tag} - {state_tag}"
