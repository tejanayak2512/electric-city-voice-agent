import json
import logging
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from complaints.models import Complaint
from users.models import ConnectionApplication, UserAccount
from .agent import format_spoken_text, lookup_complaint, process_call_turn, raise_complaint
from .knowledge_base import get_live_knowledge_base
from .models import CallCenterAgent, VoiceCallMessage, VoiceCallSession

logger = logging.getLogger(__name__)


def _get_logged_in_user(request):
    """Retrieve logged in user from session or authentication state."""
    # Check standard session keys used across the application
    uid = request.session.get("user_account_id") or request.session.get("user_id")
    if uid:
        try:
            return UserAccount.objects.get(id=uid)
        except UserAccount.DoesNotExist:
            pass

    username = request.session.get("user_username")
    if username:
        try:
            return UserAccount.objects.get(username=username)
        except UserAccount.DoesNotExist:
            pass

    if hasattr(request, "user") and request.user.is_authenticated:
        try:
            return UserAccount.objects.get(username=request.user.username)
        except UserAccount.DoesNotExist:
            pass

    return None


def user_voice_agent_call(request):
    """Render the 24/7 AI-Powered Telephonic Voice Calling Agent Screen."""
    user = _get_logged_in_user(request)

    conn = None
    if user:
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

    # Fetch live facts and user history
    kb = get_live_knowledge_base(user_account=user)
    recent_calls = []
    my_complaints = []

    if user:
        recent_calls = VoiceCallSession.objects.filter(user_account=user).order_by("-started_at")[:10]
        my_complaints = Complaint.objects.filter(user_account=user).order_by("-created_at")[:15]
    else:
        # For public demo or unregistered guest callers
        recent_calls = VoiceCallSession.objects.filter(user_account__isnull=True).order_by("-started_at")[:5]

    CallCenterAgent.seed_default_10_agents()
    available_agents = list(
        CallCenterAgent.objects.filter(is_active=True, agent_number__gte=1, agent_number__lte=10)
        .exclude(status=CallCenterAgent.STATUS_OFFLINE)
        .order_by("agent_number")
    )

    context = {
        "user_account": user,
        "account": user,
        "connection": conn,
        "kb": kb,
        "recent_calls": recent_calls,
        "my_complaints": my_complaints,
        "available_agents": available_agents,
        "active_nav": "services",
        "active_subnav": "voice_agent",
        "active_user_nav": "services",
        "active_service_sub": "voice_agent",
        "breadcrumbs": [("Services", None), ("24/7 AI Voice Calling Agent", None)],
    }
    return render(request, "users/voice_agent_call.html", context)


def user_pure_voice_agent_call(request):
    """Render the 24/7 Hands-Free Conversational Voice AI Calling Screen (100% Voice / No Keypad)."""
    user = _get_logged_in_user(request)

    conn = None
    if user:
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

    kb = get_live_knowledge_base(user_account=user)
    recent_calls = []
    my_complaints = []

    if user:
        recent_calls = VoiceCallSession.objects.filter(user_account=user).order_by("-started_at")[:10]
        my_complaints = Complaint.objects.filter(user_account=user).order_by("-created_at")[:15]
    else:
        recent_calls = VoiceCallSession.objects.filter(user_account__isnull=True).order_by("-started_at")[:5]

    CallCenterAgent.seed_default_10_agents()
    available_agents = list(
        CallCenterAgent.objects.filter(is_active=True, agent_number__gte=1, agent_number__lte=10)
        .exclude(status=CallCenterAgent.STATUS_OFFLINE)
        .order_by("agent_number")
    )

    context = {
        "user_account": user,
        "account": user,
        "connection": conn,
        "kb": kb,
        "recent_calls": recent_calls,
        "my_complaints": my_complaints,
        "available_agents": available_agents,
        "active_nav": "services",
        "active_subnav": "pure_voice_calling",
        "active_user_nav": "services",
        "active_service_sub": "pure_voice_calling",
        "breadcrumbs": [("Services", None), ("Hands-Free AI Voice Calling (Voice-Only)", None)],
    }
    return render(request, "users/pure_voice_agent_call.html", context)


@csrf_exempt
@require_POST
def api_start_call(request):
    """Initialize a live telephone calling session (starts in IVR mode by default unless an agent is specifically requested)."""
    try:
        req_data = json.loads(request.body.decode("utf-8"))
    except Exception:
        req_data = request.POST

    from .models import CallCenterAgent
    CallCenterAgent.seed_default_10_agents()

    preferred_agent_id = req_data.get("agent_id") or request.POST.get("agent_id")
    is_hands_free = bool(
        req_data.get("hands_free")
        or req_data.get("mode") == "hands_free"
        or request.POST.get("hands_free")
    )
    direct_connect = bool(req_data.get("direct_connect") or request.POST.get("direct_connect"))

    agent = None
    offline_requested_agent = None

    if preferred_agent_id and (direct_connect or is_hands_free):
        from django.db.models import Q
        # Check if the requested agent exists and if they are offline
        req_agent = CallCenterAgent.objects.filter(
            Q(id=preferred_agent_id) | Q(agent_number=preferred_agent_id)
        ).first()
        if req_agent and (not req_agent.is_active or req_agent.status == CallCenterAgent.STATUS_OFFLINE):
            offline_requested_agent = req_agent
            agent = None  # Force IVR mode with alert that officer is offline
        else:
            agent = CallCenterAgent.get_available_agent(preferred_agent_id=preferred_agent_id)
            if agent and agent.agent_number == 11:
                agent = None  # Agent 11 is strictly QA/Feedback, never initial call agent
    elif is_hands_free:
        agent = (
            CallCenterAgent.objects.filter(agent_number=1, is_active=True).exclude(status=CallCenterAgent.STATUS_OFFLINE).first()
            or CallCenterAgent.objects.filter(is_active=True, agent_number__gte=1, agent_number__lte=10).exclude(status=CallCenterAgent.STATUS_OFFLINE).first()
        )

    user = _get_logged_in_user(request)
    caller_name = user.full_name if user else "Valued Consumer"
    caller_phone = user.mobile_number if user else ""

    is_ivr = (agent is None)

    session = VoiceCallSession.objects.create(
        user_account=user,
        assigned_agent=agent,
        caller_name=caller_name,
        caller_phone=caller_phone,
        status=VoiceCallSession.STATUS_ACTIVE,
        is_ivr_active=is_ivr,
    )

    # Fetch live facts to provide personalized verification greeting
    kb = get_live_knowledge_base(user_account=user)
    cp = kb.get("caller_profile")

    if is_hands_free and agent:
        ag_name = agent.officer_name or agent.name
        ag_role = agent.role or "Outage & Emergency Specialist"
        premise_info = ""
        if cp and cp.get("has_active_meter") and cp.get("meter_number") not in ["N/A", "No Meter Assigned", "Pending Allotment"]:
            premise_info = f" [Verified Premise: Meter {cp['meter_number']} • {cp['house_address']}]"
        elif cp and cp.get("request_id") and cp.get("request_id") not in ["N/A", "No Request ID"]:
            premise_info = f" [Verified Request ID: {cp['request_id']} • Status: {cp['connection_status']}]"

        welcome_text = (
            f"Welcome to the Electricity Department 24/7 Hands-Free AI Voice Calling Helpline (1912).\n\n"
            f"Hello {caller_name}!{premise_info}\n"
            f"This is {ag_name}, {ag_role}. You are connected directly in hands-free voice mode. "
            f"You can speak naturally to report power cuts, check bill ledgers, report hazards, or ask to transfer to any department or shift supervisor.\n\n"
            f"How may I assist you today?"
        )
        welcome_spoken_text = (
            f"Welcome to Electricity Department 24/7 Hands-Free Voice Helpline. "
            f"Hello {caller_name}! This is {ag_name}, {ag_role}. "
            f"You can speak naturally through your microphone. How may I assist you today?"
        )
    elif is_ivr:
        premise_badge = ""
        if cp and cp.get("has_active_meter") and cp.get("meter_number") not in ["N/A", "No Meter Assigned", "Pending Allotment"]:
            premise_badge = f" [Verified Connection: Request ID {cp['request_id']} • Meter {cp['meter_number']} • {cp['house_address']}]"
        elif cp and cp.get("request_id") and cp.get("request_id") not in ["N/A", "No Request ID"]:
            premise_badge = f" [Verified Request ID: {cp['request_id']} • Status: {cp['connection_status']}]"

        from django.db.models import Q
        offline_agents = set(
            CallCenterAgent.objects.filter(
                Q(is_active=False) | Q(status=CallCenterAgent.STATUS_OFFLINE)
            ).values_list("agent_number", flat=True)
        )

        def _opt_status(num, text):
            if num in offline_agents:
                return f"{text} — [Currently Offline / Off-Duty]"
            return text

        offline_alert_prefix = ""
        if offline_requested_agent:
            off_num = offline_requested_agent.agent_number
            off_name = offline_requested_agent.officer_name or offline_requested_agent.name
            off_role = offline_requested_agent.role or "Specialist"
            offline_alert_prefix = (
                f"Notice: Agent {off_num} — {off_name} ({off_role}) is currently offline on administrative break or off-duty.\n"
                f"You have been connected to our 24/7 Automated IVR System.\n\n"
            )

        welcome_text = (
            f"{offline_alert_prefix}"
            f"Welcome to the Electricity Department 24/7 Interactive Telephony Helpline (1912).\n\n"
            f"Hello {caller_name}!{premise_badge}\n"
            f"Please select an option to connect with our dedicated administration agents:\n"
            f"• Press or type 1 for {_opt_status(1, 'Power Outages & Supply Cuts — Agent 1 (Officer Priya)')}\n"
            f"• Press 2 for {_opt_status(2, 'High Voltage Grid & Substation Telemetry — Agent 2 (Officer Vikram)')}\n"
            f"• Press 3 for {_opt_status(3, 'Billing, Tariffs & Meter Ledgers — Agent 3 (Officer Ananya)')}\n"
            f"• Press 4 for {_opt_status(4, 'Sparks, Fire & Electrical Hazards — Agent 4 (Officer Rahul)')}\n"
            f"• Press 5 for {_opt_status(5, 'Transformer Diagnostics & Telemetry — Agent 5 (Officer Sneha)')}\n"
            f"• Press 6 for {_opt_status(6, 'Smart Meter Faults & Blank Display — Agent 6 (Officer Rajesh)')}\n"
            f"• Press 7 for {_opt_status(7, 'Citizen Grievance & SLA Escalations — Agent 7 (Officer Kavita)')}\n"
            f"• Press 8 for {_opt_status(8, 'Field Line Crew Dispatch — Agent 8 (Officer Amit)')}\n"
            f"• Press 9 for {_opt_status(9, 'Commercial Bulk Accounts — Agent 9 (Officer Pooja)')}\n"
            f"• Press 0 for {_opt_status(10, 'Senior Shift In-Charge Supervisor — Agent 10 (Officer Suresh)')}\n\n"
            f"Please enter an option (0-9) on the touch-tone keypad or type your choice below."
        )
        welcome_spoken_text = (
            f"Welcome to Electricity Department 24/7 Helpline 1912. Hello {caller_name}. "
            f"Please enter an option from 1 to 9 on your touch-tone keypad, or press 0 for Senior Supervisor, to connect with an agent."
        )
    else:
        agent_name_intro = f"My name is {agent.name}."
        if cp and cp.get("has_active_meter") and cp.get("meter_number") not in ["N/A", "No Meter Assigned", "Pending Allotment"]:
            welcome_text = (
                f"Welcome to the Electricity Department 24/7 Consumer Care AI Voice Agent. "
                f"{agent_name_intro} "
                f"Hello {caller_name}! I have verified your registered number {cp['mobile']}. "
                f"Your active connection is Request ID {cp['request_id']} with Meter Number {cp['meter_number']} at {cp['house_address']}. "
                f"Are you calling to report a power outage, check complaint status, or check your electricity bill?"
            )
        elif cp and cp.get("request_id") and cp.get("request_id") not in ["N/A", "No Request ID"]:
            welcome_text = (
                f"Welcome to the Electricity Department 24/7 Consumer Care AI Voice Agent. "
                f"{agent_name_intro} "
                f"Hello {caller_name}! I have verified your registered number {cp['mobile']}. "
                f"Your electricity application Request ID is {cp['request_id']} with status {cp['connection_status']}. "
                f"How may I assist you with your electricity service today?"
            )
        elif user:
            welcome_text = (
                f"Welcome to the Electricity Department 24/7 Consumer Care AI Voice Agent. "
                f"{agent_name_intro} "
                f"Hello {caller_name}! I have verified your registered consumer account for mobile number {user.mobile_number}. "
                f"How may I assist you with your electricity service today?"
            )
        else:
            welcome_text = (
                f"Welcome to the Electricity Department 24/7 Consumer Care AI Voice Agent. "
                f"{agent_name_intro} "
                f"Hello! How may I assist you with your electricity service today? "
                f"You can report a power outage, track an existing complaint, or check your electricity bill and tariffs."
            )
        welcome_spoken_text = format_spoken_text(welcome_text)

    VoiceCallMessage.objects.create(
        session=session,
        sender=VoiceCallMessage.SENDER_AI,
        text=welcome_text,
        latency_ms=450,
        intent_detected=VoiceCallSession.INTENT_GENERAL_INQUIRY,
    )

    agent_data = None
    if agent:
        agent_data = {
            "id": agent.id,
            "agent_number": agent.agent_number,
            "agent_code": agent.agent_code,
            "name": agent.name,
            "short_name": agent.short_name,
            "officer_name": agent.officer_name,
            "gender": agent.gender,
            "role": agent.role,
            "voice_type": agent.voice_type,
            "voice_pitch": agent.voice_pitch,
            "voice_rate": agent.voice_rate,
            "avatar_color": agent.avatar_color,
            "avatar_icon": agent.avatar_icon,
            "status": agent.status,
        }

    # Generate natural neural audio for welcome message using unified agent profile
    from .tts_utils import generate_neural_speech_base64, get_agent_voice_params
    v_params = get_agent_voice_params(agent)
    welcome_audio_base64 = generate_neural_speech_base64(
        welcome_spoken_text,
        voice=v_params["voice"],
        rate=v_params.get("rate", "+3%"),
        pitch=v_params.get("pitch", "+0Hz"),
    )

    return JsonResponse({
        "success": True,
        "session_id": str(session.session_id),
        "caller_name": caller_name,
        "is_ivr_active": session.is_ivr_active,
        "assigned_agent": agent_data,
        "welcome_message": welcome_text,
        "welcome_spoken_text": welcome_spoken_text,
        "audio_base64": welcome_audio_base64,
        "status": session.status,
        "caller_verified": bool(user),
        "caller_profile": cp if cp else None,
    })


@csrf_exempt
@require_POST
def api_send_message(request):
    """Process an incoming voice or text utterance from the caller."""
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST

    session_id = data.get("session_id", "").strip()
    user_text = (data.get("message") or data.get("user_text") or "").strip()

    if not session_id:
        return JsonResponse({"success": False, "error": "session_id is required."}, status=400)

    try:
        session = VoiceCallSession.objects.get(session_id=session_id)
    except VoiceCallSession.DoesNotExist:
        return JsonResponse({"success": False, "error": "Session not found or expired."}, status=404)

    user = _get_logged_in_user(request) or session.user_account
    turn_result = process_call_turn(
        session=session,
        caller_text=user_text,
        user_account=user,
    )
    return JsonResponse(turn_result)


@csrf_exempt
@require_POST
def api_end_call(request):
    """End the active call session and persist call duration."""
    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        data = request.POST

    session_id = data.get("session_id", "").strip()
    if not session_id:
        return JsonResponse({"success": False, "error": "session_id required"}, status=400)

    try:
        session = VoiceCallSession.objects.get(session_id=session_id)
        session.close_call()
        return JsonResponse({
            "success": True,
            "session_id": str(session.session_id),
            "status": session.status,
            "duration_seconds": session.duration_seconds,
        })
    except VoiceCallSession.DoesNotExist:
        return JsonResponse({"success": False, "error": "Session not found"}, status=404)


def api_track_complaint(request, query):
    """Lookup a complaint by ID or number and return JSON payload."""
    results = lookup_complaint(query)
    if results:
        return JsonResponse({"success": True, "complaint": results[0]})
    return JsonResponse({"success": False, "error": f"No complaint found matching '{query}'"}, status=404)
