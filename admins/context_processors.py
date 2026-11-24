from django.db import DatabaseError

from .models import OperationMaintenanceNotification, SiteMediaSetting, SiteNewsAnnouncement
from llm_config.models import LLMConfiguration


def site_settings_context(request):
    """Context processor providing live site news, maintenance notifications, site media, and LLM configuration to public templates."""
    try:
        active_news = list(
            SiteNewsAnnouncement.objects.filter(is_active=True).order_by("-created_at")[:10]
        )
        active_notifications = list(
            OperationMaintenanceNotification.objects.filter(is_active=True)
            .select_related("related_transformer")
            .order_by("-created_at")[:10]
        )

        ticker_items = []
        for n in active_notifications:
            ticker_items.append({
                "type": "notification",
                "severity": n.severity,
                "category": n.get_category_display(),
                "prefix": f"ALERT: {n.get_category_display()}",
                "title": n.title,
                "extra": n.affected_areas or (f"Feeder {n.related_transformer.code}" if n.related_transformer else ""),
                "message": n.message,
            })

        for a in active_news:
            if a.is_ticker:
                ticker_items.append({
                    "type": "news",
                    "severity": "info",
                    "category": "Announcement",
                    "prefix": f"{a.badge_label.upper()}" if a.badge_label else "NOTICE",
                    "title": a.title,
                    "extra": (a.content[:75] + "...") if len(a.content) > 75 else a.content,
                    "message": a.content,
                })

        try:
            default_llm = LLMConfiguration.objects.filter(is_active=True, is_default=True).first()
        except DatabaseError:
            default_llm = None

        try:
            site_media = SiteMediaSetting.get_active()
        except DatabaseError:
            site_media = None

        return {
            "active_site_news": active_news,
            "active_site_notifications": active_notifications,
            "active_ticker_items": ticker_items,
            "default_llm_config": default_llm,
            "site_media": site_media,
        }
    except DatabaseError:
        return {
            "active_site_news": [],
            "active_site_notifications": [],
            "active_ticker_items": [],
            "default_llm_config": None,
            "site_media": None,
        }

