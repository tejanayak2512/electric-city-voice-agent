"""Context processor for user navbar and profile data.

Ensures that templates extending user_base.html always have access to
the active logged-in user account, their ConnectionApplication,
and active tariff rates, regardless of which view renders the template.
"""

from decimal import Decimal
from admins.models import TariffSetting
from users.models import ConnectionApplication, UserAccount


def user_navbar_context(request):
    """Context processor providing logged-in user account, active connection,

    and active tariffs to all templates extending user_base.html.
    """
    context = {}
    try:
        user_id = request.session.get("user_account_id") or request.session.get("user_id")
        user = None
        if user_id:
            user = UserAccount.objects.filter(id=user_id).first()
        elif request.session.get("user_username"):
            user = UserAccount.objects.filter(username=request.session.get("user_username")).first()
        elif hasattr(request, "user") and request.user.is_authenticated:
            user = UserAccount.objects.filter(username=request.user.username).first()

        if user:
            context["user_account"] = user
            context["account"] = user

            # Find connection: prefer Connected status, then latest application
            conn = (
                ConnectionApplication.objects.filter(
                    user=user, status=ConnectionApplication.STATUS_CONNECTED
                )
                .select_related("transformer", "verified_by", "connected_by")
                .order_by("-updated_at")
                .first()
                or ConnectionApplication.objects.filter(user=user)
                .select_related("transformer", "verified_by", "connected_by")
                .order_by("-created_at")
                .first()
            )
            if not conn and user.mobile_number:
                conn = (
                    ConnectionApplication.objects.filter(
                        mobile_number=user.mobile_number, status=ConnectionApplication.STATUS_CONNECTED
                    )
                    .select_related("transformer", "verified_by", "connected_by")
                    .first()
                    or ConnectionApplication.objects.filter(mobile_number=user.mobile_number)
                    .select_related("transformer", "verified_by", "connected_by")
                    .first()
                )

            context["connection"] = conn

        # Tariff settings for top notification bar
        tariff = TariffSetting.get_active()
        if tariff:
            context["tariff_setting"] = tariff
            context["base_fare_rs"] = tariff.base_fare_rs
            context["domestic_rate"] = tariff.domestic_rate_per_unit
            context["commercial_rate"] = tariff.commercial_rate_per_unit
    except Exception:
        pass

    return context
