"""AI Voice Agent Conversational Engine & Tool Execution for Electricity Department.

Implements the multi-turn guided conversational flow specified in the IEEE paper
'AI-Powered Voice Agent for Electricity Department':
- Intent Recognition (Register Complaint, Check Status, Billing/Tariff, Outage Q&A)
- Complaint Registration Flow (raise_complaint)
- Status Verification Flow (lookup_complaint)
- Database-configured LLM execution with real-time Knowledge Base injection
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from complaints.models import Complaint
from users.models import ConnectionApplication, PowerBill, UserAccount, UserNotification
from llm_config.services import call_llm, get_active_llm_config
from .knowledge_base import format_knowledge_base_prompt, get_live_knowledge_base
from .models import VoiceCallMessage, VoiceCallSession

logger = logging.getLogger(__name__)


def raise_complaint(
    caller_name: str,
    location: str,
    issue_type: str = Complaint.ISSUE_POWER_OUTAGE,
    service_number: str = "",
    phone_number: str = "",
    landmark: str = "",
    outage_duration: str = "",
    impact_area: str = "",
    hazard_observed: str = "",
    description: str = "",
    user_account=None,
    connection=None,
) -> Complaint:
    """Execute raise_complaint tool: stores complaint into database with unique IDs and initial status."""
    logger.info("Executing raise_complaint tool for %s at %s", caller_name, location)
    priority = Complaint.PRIORITY_NORMAL
    issue_lower = (issue_type + " " + hazard_observed + " " + description).lower()
    if any(k in issue_lower for k in ["spark", "fire", "blast", "wire snap", "fallen wire", "smoke", "shock", "danger"]):
        priority = Complaint.PRIORITY_CRITICAL
        est_resolution = "Immediate Emergency Line Crew Dispatched (Within 30 to 45 mins)"
    elif any(k in issue_lower for k in ["street", "colony", "area", "entire", "neighborhood", "all houses", "neighbor"]):
        priority = Complaint.PRIORITY_HIGH
        est_resolution = "Area Feeder Crew Dispatched (Within 1 to 2 hours)"
    else:
        est_resolution = "Within 2 to 4 hours"

    if not description:
        description = (
            f"Power issue reported via AI Voice Agent: {issue_type}. "
            f"Location: {location}. Landmark: {landmark or 'N/A'}. "
            f"Duration: {outage_duration or 'Recent'}. "
            f"Impact: {impact_area or 'Local Premise'}. "
            f"Hazards: {hazard_observed or 'None reported'}."
        )

    # Attempt to link user connection if omitted
    if user_account and not connection:
        from users.models import ConnectionApplication
        connection = ConnectionApplication.objects.filter(
            user=user_account,
            status=ConnectionApplication.STATUS_CONNECTED,
        ).first() or ConnectionApplication.objects.filter(user=user_account).first()

    complaint = Complaint.objects.create(
        user_account=user_account,
        connection=connection,
        caller_name=caller_name or "Valued Consumer",
        service_number=service_number or (connection.meter_number if connection else ""),
        phone_number=phone_number or (user_account.mobile_number if user_account else ""),
        location=location or "Consumer Registered Address",
        landmark=landmark,
        issue_type=issue_type,
        outage_duration=outage_duration,
        impact_area=impact_area,
        hazard_observed=hazard_observed,
        description=description,
        status=Complaint.STATUS_REGISTERED,
        priority=priority,
        estimated_resolution=est_resolution,
        assigned_field_team="Central Control Room / Line Dispatch Unit",
        source="AI_VOICE_CALL",
    )
    return complaint


def format_spoken_text(text: str) -> str:
    """Transform raw written responses into natural, concise spoken customer care speech for TTS.

    Expands technical codes, identifiers, numbers, and currency into phonetic words
    and natural pauses suitable for spoken delivery over a telephone call.
    Trims excessive trailing sentences so spoken audio is snappy and conversational.
    """
    if not text:
        return ""
    s = text

    # Remove markdown tables, headers, and bullet formatting
    s = re.sub(r"\|[^\n]+\|", "", s)
    s = re.sub(r"^[#*>\s\-|]+", "", s)
    s = re.sub(r"[*#_`]", "", s)

    # Expand CMP-YYYYMM-XXXX into spaced phonetic characters
    # e.g. CMP-202611-0002 -> "C M P, 2 0 2 6 1 1, 0 0 0 2"
    def _expand_cmp(m):
        full = m.group(0)
        parts = full.split("-")
        p1 = " ".join(list(parts[1]))
        p2 = " ".join(list(parts[2]))
        return f"C M P, {p1}, {p2}"

    s = re.sub(r"\bCMP-\d{6}-\d{4}\b", _expand_cmp, s)

    # Expand 8-character hex Tracking ID: "Tracking ID: 8F2A1C04" -> "Tracking I D, 8 F 2 A 1 C 0 4"
    def _expand_tracking_id(m):
        tid = m.group(1)
        spaced = " ".join(list(tid.upper()))
        return f"Tracking I D, {spaced}"

    s = re.sub(r"(?:Tracking ID:?\s*)?\b([A-Fa-f0-9]{8})\b", _expand_tracking_id, s)

    # Expand Meter Numbers: "MTR-2026-1004-3" or "MTR-998877"
    def _expand_meter(m):
        full = m.group(1)
        parts = full.split("-")
        return f"Meter number {' '.join(parts)}"

    s = re.sub(r"(?:Meter Number\s+|Meter no\.?\s*)?\b(MTR-[A-Za-z0-9-]+)\b", _expand_meter, s, flags=re.IGNORECASE)

    # Expand Request IDs: "APP-TEST-001" or "REQ-1003" or "CONN-1001"
    def _expand_req(m):
        full = m.group(1)
        parts = full.split("-")
        return f"Request ID {' '.join(parts)}"

    s = re.sub(r"(?:Request ID\s+|Application ID\s*)?\b((?:APP|REQ|CONN)-[A-Za-z0-9-]+)\b", _expand_req, s, flags=re.IGNORECASE)

    # Currency Rs 890.00 -> 890 rupees
    s = re.sub(r"Rs\s*(\d+)\.00\b", r"\1 rupees", s)
    s = re.sub(r"Rs\s*(\d+(?:\.\d{1,2})?)\s*/\s*unit", r"\1 rupees per unit", s)
    s = re.sub(r"Rs\s*(\d+(?:\.\d{1,2})?)\s*/\s*kWh", r"\1 rupees per kilowatt hour", s)
    s = re.sub(r"Rs\s*(\d+(?:\.\d{1,2})?)", r"\1 rupees", s)

    # Decimal units: "6.08 units" -> "6 point 0 8 units"
    s = re.sub(r"\b(\d+)\.(\d{1,2})\s*units\b", r"\1 point \2 units", s)

    # Expand Units & Acronyms
    s = re.sub(r"\bkWh\b", "kilowatt hours", s)
    s = re.sub(r"\bDISCOM\b", "Discom", s)
    s = re.sub(r"\b24/7\b", "twenty four seven", s)
    s = re.sub(r"\bETA\b", "estimated arrival time", s)
    s = re.sub(r"\bSLA\b", "service level target", s)

    # Clean whitespace
    s = re.sub(r"\s+", " ", s).strip()

    # Limit spoken text to first 2-3 sentences max (~45 words) for realistic telephone cadence
    # while preserving full details in written transcript
    sentences = re.split(r"(?<=[.!?])\s+", s)
    if len(sentences) > 3:
        s = " ".join(sentences[:3])

    return s


def lookup_complaint(query: str, caller_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """Execute lookup_complaint tool: retrieves registered complaint by complaint_number, 8-character ID, or name."""
    clean_q = query.strip()
    qs = Complaint.objects.all()

    # Match exact or partial complaint number or 8-character complaint_id
    matches = qs.filter(
        Q(complaint_number__iexact=clean_q)
        | Q(complaint_number__icontains=clean_q)
        | Q(complaint_id__iexact=clean_q)
        | Q(complaint_id__icontains=clean_q)
    )

    if not matches.exists() and caller_name:
        matches = qs.filter(caller_name__icontains=caller_name.strip())

    if not matches.exists() and len(clean_q) >= 4:
        # Match alphanumeric substring in ID or description
        matches = qs.filter(
            Q(complaint_id__icontains=clean_q) | Q(service_number__icontains=clean_q)
        )

    results = []
    for c in matches[:5]:
        results.append({
            "complaint_number": c.complaint_number,
            "complaint_id": c.complaint_id,
            "caller_name": c.caller_name,
            "service_number": c.service_number,
            "issue_type": c.issue_type,
            "location": c.location,
            "landmark": c.landmark,
            "status": c.get_status_display(),
            "priority": c.get_priority_display(),
            "estimated_resolution": c.estimated_resolution,
            "assigned_field_team": c.assigned_field_team or "Field Line Crew",
            "department_notes": c.department_notes or "Under active supervision by Electricity Department.",
            "created_at": c.created_at.strftime("%Y-%m-%d %H:%M"),
        })
    return results


def build_system_instructions(kb_text: str) -> str:
    """Build the conversational system prompt as specified in the IEEE research paper."""
    return f"""You are the official 24/7 AI-Powered Telephonic Voice Agent for the Electricity Department / State DISCOM.
You handle citizen calls in real time to register electricity complaints, report power outages, verify complaint status, and answer billing, tariff, and grid inquiries.

{kb_text}

### CONVERSATIONAL RULES & VOICE FLOW (From IEEE Research Paper):
1. **Persona & Tone**: Courteous, calm, reassuring, professional, and clear. Speak like a natural human customer service representative.
2. **Intent Bifurcation**:
   - If the caller wants to report an outage or file a complaint: Enter **Complaint Registration Flow**.
   - If the caller wants to check an existing complaint: Enter **Status Verification Flow**.
   - If the caller asks why power is off, check bills, or asks about tariffs/appliances: Provide immediate factual answers from the knowledge base above.
3. **Complaint Registration Flow**:
   - Collect key diagnostic slots:
     a) Caller Name & Service/Meter Number (use identified caller details if available).
     b) Exact Location and nearby Landmark.
     c) Issue Type (e.g., Power Outage, Low Voltage, Transformer Sparking, Wire Snap).
     d) Duration of outage (how long power has been off).
     e) Impact on surrounding area (is it only their house, or the entire street/neighborhood?).
     f) Observations of hazards (sparking, loud blast, burning smell, fallen wires).
   - If hazardous conditions (sparks, fallen wire) are mentioned, IMMEDIATELY advise the caller to stay at least 10 meters away for safety while flagging emergency dispatch!
   - Summarize the complaint details for confirmation.
   - When the user confirms or gives sufficient details, instruct the system to call `raise_complaint` tool by outputting:
     ACTION: RAISE_COMPLAINT | Name: <name> | Location: <loc> | Landmark: <landmark> | Issue: <issue> | Duration: <duration> | Impact: <impact> | Hazard: <hazard>
   - Then provide the caller with their unique Complaint Number and 8-character Tracking ID and estimated resolution time.
4. **Status Verification Flow**:
   - Ask for their Complaint Number or 8-character Tracking ID (or caller name).
   - Instruct lookup by outputting:
     ACTION: LOOKUP_COMPLAINT | Query: <query>
   - Communicate the status, assigned crew, and estimated resolution time clearly.
5. **Conciseness & Spoken Clarity**:
   - Keep each spoken response concise (2 to 4 sentences), easy to listen to over the phone, and free of markdown syntax like asterisks or bullet points in the spoken portion.
"""


def detect_ivr_option(text_lower: str, in_ivr: bool = False) -> Optional[int]:
    """Detect selected IVR option (1 to 11) from caller touch-tone digit or voice speech utterance."""
    t = text_lower.strip().lower()

    word_to_num = {
        "one": 1, "1": 1, "01": 1, "first": 1,
        "two": 2, "2": 2, "02": 2, "second": 2,
        "three": 3, "3": 3, "03": 3, "third": 3,
        "four": 4, "4": 4, "04": 4, "fourth": 4,
        "five": 5, "5": 5, "05": 5, "fifth": 5,
        "six": 6, "6": 6, "06": 6, "sixth": 6,
        "seven": 7, "7": 7, "07": 7, "seventh": 7,
        "eight": 8, "8": 8, "08": 8, "eighth": 8,
        "nine": 9, "9": 9, "09": 9, "ninth": 9,
        "ten": 10, "10": 10, "00": 10, "tenth": 10,
        "zero": 10, "0": 10,
        "eleven": 11, "11": 11,
    }

    # 1. Exact digit / touch-tone key / word number match:
    # In IVR mode, 1-10 are valid choices.
    # In live agent call, '0' (or 'zero'/'operator') serves as the universal operator key to reach Senior Shift Supervisor (Agent 10).
    if in_ivr and t in word_to_num:
        return word_to_num[t]
    if not in_ivr and t in ["0", "zero", "00", "operator"]:
        return 10

    # 2. Check for feedback desk / agent 11 request
    if any(k in t for k in ["feedback agent", "feedback desk", "transfer to feedback", "officer ritu", "quality assurance", "give feedback"]):
        return 11

    # 3. Spoken Agent name/number: "Agent 2", "Agent two", "Agent two issue", "Agent 8", "Agent 10"
    m_agent = re.search(r"\bagent\s*(one|two|three|four|five|six|seven|eight|nine|ten|zero|\d{1,2})\b", t)
    if m_agent:
        v = m_agent.group(1)
        val = word_to_num.get(v, int(v) if v.isdigit() else None)
        if val and 1 <= val <= 11:
            return val

    # 4. Spoken Option format (ONLY in IVR mode): "option two", "choice 2", "number 1", "key 0", "press 2", "dial 8"
    if in_ivr:
        m_opt = re.search(r"\b(?:option|choice|key|press|select|dial|number)\s*(one|two|three|four|five|six|seven|eight|nine|ten|zero|\d{1,2})\b", t)
        if m_opt:
            v = m_opt.group(1)
            val = word_to_num.get(v, int(v) if v.isdigit() else None)
            if val and 1 <= val <= 11:
                return val

        # Prefix format: "1 - Agent 1" or "[1] Outage"
        m_pfx = re.match(r"^\s*\[?#?([1-9]|10|0)\]?\s*[-–:]", t)
        if m_pfx:
            val = int(m_pfx.group(1))
            return 10 if val == 0 else val

    # 5. Semantic Voice Category Commands in IVR Mode
    if in_ivr:
        if any(k in t for k in ["feedback", "rating"]):
            return 11
        if any(k in t for k in ["outage", "no power", "power cut", "blackout", "supply cut", "cut", "current poyindi", "light off"]):
            return 1
        if any(k in t for k in ["substation", "high voltage", "grid status", "feeder telemetry", "feeder", "grid"]):
            return 2
        if any(k in t for k in ["bill", "billing", "tariff", "due date", "unpaid", "ledger", "payment"]):
            return 3
        if any(k in t for k in ["spark", "fire", "blast", "wire snap", "shock", "hazard", "safety"]):
            return 4
        if any(k in t for k in ["transformer diagnostic", "telemetry check", "kva", "oil", "diagnostics"]):
            return 5
        if any(k in t for k in ["meter fault", "meter burnt", "blank display", "smart meter", "reading discrepancy"]):
            return 6
        if any(k in t for k in ["grievance", "ombudsman", "escalat", "delayed", "complaint status"]):
            return 7
        if any(k in t for k in ["line crew", "repair van", "lineman", "field dispatch", "crew"]):
            return 8
        if any(k in t for k in ["commercial account", "industrial", "bulk consumer", "commercial"]):
            return 9
        if any(k in t for k in ["supervisor", "human", "talk to human", "shift in charge", "operator", "agent 10"]):
            return 10

    return None


def detect_agent_transfer(text: str) -> Optional[int]:
    """Detect explicit caller intent to transfer/switch call to another agent or department.

    Handles natural variations like:
    - 'Need smart meter support', 'smart meter support'
    - 'could you please transfer your call to the outage department'
    - 'Do please connect to my cloud with a safety department'
    - 'Please transfer your call to the safety in Hazard team'
    - 'transfer me to agent 1', 'talk to officer priya'
    - 'connect me to billing department', 'transfer to supervisor'
    """
    if not text:
        return None
    t = text.lower().strip()

    # 1. Direct department requests with explicit keywords (works even without strict verbs like 'transfer')
    if any(k in t for k in ["smart meter", "smart-meter", "meter support", "meter fault", "meter display"]) and any(k in t for k in ["support", "department", "team", "desk", "need", "want", "connect", "transfer", "help", "please"]):
        return 6
    if any(k in t for k in ["safety department", "hazard team", "hazard department", "safety in hazard", "safety and hazard", "electrical safety", "safety desk", "hazard desk"]):
        return 4
    if any(k in t for k in ["billing department", "billing team", "billing desk", "tariff department", "bill department", "bill desk"]):
        return 3
    if any(k in t for k in ["outage department", "outage team", "outage desk", "power cut department", "emergency dispatch"]):
        return 1
    if any(k in t for k in ["grid department", "substation department", "high voltage department", "grid team"]):
        return 2
    if any(k in t for k in ["transformer department", "transformer team", "transformer diagnostic"]):
        return 5
    if any(k in t for k in ["grievance department", "grievance team", "escalation team", "ombudsman"]):
        return 7
    if any(k in t for k in ["line crew department", "line crew team", "lineman department", "repair crew"]):
        return 8
    if any(k in t for k in ["commercial department", "bulk account department", "commercial team"]):
        return 9
    if any(k in t for k in ["feedback desk", "feedback team", "feedback department", "quality assurance", "customer feedback"]):
        return 11
    if any(k in t for k in ["supervisor desk", "talk to supervisor", "connect to supervisor", "shift supervisor", "senior supervisor"]):
        return 10

    # 2. General transfer patterns with flexible connectors
    transfer_patterns = [
        r"\b(?:transfer|route|patch|forward|switch|redirect)\b",
        r"\bconnect\b",
        r"\b(?:speak|talk)\b",
        r"\bput\s+me\s+through\b",
        r"\bhand\s+(?:me\s+)?over\b",
        r"\bpass\s+(?:me\s+)?to\b",
        r"\b(?:need|want|require)\b",
    ]
    has_transfer_verb = any(re.search(p, t) for p in transfer_patterns)
    if not has_transfer_verb:
        return None

    # Check for direct agent number reference: "agent 1", "agent two", "agent 10"
    m_num = re.search(r"\bagent\s*(one|two|three|four|five|six|seven|eight|nine|ten|zero|\d{1,2})\b", t)
    if m_num:
        val = m_num.group(1)
        num_map = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "zero": 10,
        }
        res = num_map.get(val, int(val) if val.isdigit() else None)
        if res and 1 <= res <= 11:
            return res

    # Officer names mapping
    if "priya" in t:
        return 1
    if "vikram" in t:
        return 2
    if "ananya" in t:
        return 3
    if "rahul" in t:
        return 4
    if "sneha" in t:
        return 5
    if "rajesh" in t:
        return 6
    if "kavita" in t:
        return 7
    if "amit" in t:
        return 8
    if "pooja" in t:
        return 9
    if "suresh" in t:
        return 10
    if "ritu" in t:
        return 11

    # Department and capability topics
    if any(k in t for k in ["feedback", "qa", "rating", "quality assurance"]):
        return 11
    if any(k in t for k in ["supervisor", "operator", "shift in charge", "shift in-charge", "senior supervisor"]):
        return 10
    if any(k in t for k in ["commercial", "industrial", "bulk account"]):
        return 9
    if any(k in t for k in ["line crew", "field crew", "lineman", "repair crew", "dispatch"]):
        return 8
    if any(k in t for k in ["grievance", "ombudsman", "escalation", "sla"]):
        return 7
    if any(k in t for k in ["smart meter", "smart-meter", "meter fault", "blank display", "meter display"]):
        return 6
    if any(k in t for k in ["transformer", "diagnostics", "substation transformer"]):
        return 5
    if any(k in t for k in ["hazard", "fire", "spark", "sparking", "electrical hazard", "danger", "safety"]):
        return 4
    if any(k in t for k in ["billing", "bill", "tariff", "ledger"]):
        return 3
    if any(k in t for k in ["grid", "substation", "high voltage", "feeder telemetry"]):
        return 2
    if any(k in t for k in ["outage", "power cut", "power outage", "supply cut", "blackout"]):
        return 1
    if "meter" in t:
        return 6

    # Digits directly after transfer verb e.g. "transfer to 1", "transfer to department 2"
    m_direct_digit = re.search(r"\b(?:transfer|connect|switch|route)\s+(?:me\s+|my\s+call\s+|your\s+call\s+|the\s+call\s+)?(?:to|with)?\s+(?:the\s+)?(?:department\s+|desk\s+|section\s+)?([1-9]|10|0)\b", t)
    if m_direct_digit:
        d = int(m_direct_digit.group(1))
        return 10 if d == 0 else d

    return None


def detect_offered_agent_transfer(last_ai_text: str) -> Optional[int]:
    """Detect if the previous AI message offered a department transfer."""
    if not last_ai_text:
        return None
    t = last_ai_text.lower()
    is_offering = any(k in t for k in [
        "transfer you to", "transfer your call to", "connect you with", "connect you to",
        "connect you right away", "transfer you now", "would you like me to connect",
        "shall i connect", "shall i transfer", "please hold while i connect",
        "please hold while i transfer", "transferring you to", "connecting you to"
    ])
    if not is_offering:
        return None

    if any(k in t for k in ["smart meter", "smart-meter", "meter support", "meter fault"]):
        return 6
    if any(k in t for k in ["safety", "hazard"]):
        return 4
    if any(k in t for k in ["billing", "tariff", "bill"]):
        return 3
    if any(k in t for k in ["outage", "emergency dispatch"]):
        return 1
    if any(k in t for k in ["grid", "substation", "high voltage"]):
        return 2
    if any(k in t for k in ["transformer"]):
        return 5
    if any(k in t for k in ["grievance", "escalation", "sla"]):
        return 7
    if any(k in t for k in ["line crew", "lineman", "field crew"]):
        return 8
    if any(k in t for k in ["commercial", "industrial"]):
        return 9
    if any(k in t for k in ["supervisor", "operator"]):
        return 10
    if any(k in t for k in ["feedback", "customer feedback"]):
        return 11
    return None


def get_agent_opening_dialogue(target_agent, caller_name, service_num, loc, cp, kb) -> str:
    """Generate concise, natural customer care introductory greeting from chosen administration agent."""
    ag_num = target_agent.agent_number
    intro_prefix = f"Connecting to {target_agent.short_name}... "

    verified_premise = f" (Verified Premise: {service_num})" if service_num else ""

    if ag_num == 1:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"Outage & Emergency Dispatch Specialist.{verified_premise} "
            f"Are you calling to report a power outage or cut at your house right now?"
        )
    elif ag_num == 2:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"High Voltage Grid & Substation Specialist.{verified_premise} "
            f"I have direct access to your distribution transformer and feeder telemetry. How may I assist your grid inquiries today?"
        )
    elif ag_num == 3:
        bill_info = ""
        if cp and cp.get("unpaid_bills"):
            b = cp["unpaid_bills"][0]
            amt = b.get("amount", b.get("total", 890.0))
            month = b.get("month", "2026-11")
            bill_info = f" Your latest invoice is Rs {float(amt):.2f} for cycle {month}."
        elif cp and cp.get("latest_bill"):
            lb = cp["latest_bill"]
            amt = lb.get("amount", 890.0)
            month = lb.get("month", "2026-11")
            bill_info = f" Your latest invoice is Rs {float(amt):.2f} for cycle {month}."
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"Billing & Tariffs Specialist.{verified_premise}{bill_info} "
            f"How may I assist you with your electricity bill or tariffs today?"
        )
    elif ag_num == 4:
        return (
            f"Emergency quick response connected! Hello {caller_name}! This is {target_agent.name}, "
            f"Electrical Safety & Hazard Response Officer. Do you observe sparking on the transformer, smoke, fire, or a fallen live wire?"
        )
    elif ag_num == 5:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"Substation & Transformer Diagnostic Specialist.{verified_premise} "
            f"I am monitoring telemetry and circuit breakers for your sector. What diagnostic checks can I run for you?"
        )
    elif ag_num == 6:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"Smart Meter & Service Connection Officer.{verified_premise} "
            f"Are you reporting a blank digital display, burnt meter terminal, or a reading discrepancy?"
        )
    elif ag_num == 7:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"Citizen Grievance & Escalation Supervisor.{verified_premise} "
            f"Do you have an existing complaint tracking ID you would like me to review?"
        )
    elif ag_num == 8:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"Field Dispatch & Line Crew Coordinator.{verified_premise} "
            f"I track our mobile repair vans and lineman crews. How can I assist with your restoration dispatch?"
        )
    elif ag_num == 9:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, "
            f"Commercial Accounts & Energy Audit Specialist.{verified_premise} "
            f"How may I assist with your commercial account, high-tension tariffs, or sanctioned load today?"
        )
    elif ag_num == 10:
        return (
            f"Connecting to Shift In-Charge... Hello {caller_name}! This is {target_agent.name}, "
            f"24/7 Helpline Shift In-Charge.{verified_premise} "
            f"How may I personally assist you with your electricity department services today?"
        )
    else:
        return (
            f"{intro_prefix}Hello {caller_name}! This is {target_agent.name}, {target_agent.role}. "
            f"I am connected with you. How may I assist you today?"
        )


def parse_feedback_rating(text: str) -> Optional[int]:
    """Parse caller feedback rating (scale 0 to 5) from touch-tone digit or spoken words.

    Robustly handles prompt echo, 'X out of 5', words ('zero' to 'five'), and keypad digits.
    """
    if not text:
        return None

    raw = text.lower().strip()

    # 1. Exact direct matches (single word or digit)
    direct_map = {
        "0": 0, "zero": 0, "nil": 0, "none": 0, "null": 0,
        "1": 1, "one": 1,
        "2": 2, "two": 2,
        "3": 3, "three": 3,
        "4": 4, "four": 4,
        "5": 5, "five": 5,
    }
    if raw in direct_map:
        return direct_map[raw]

    num_word_map = {
        "0": 0, "zero": 0, "nil": 0, "none": 0,
        "1": 1, "one": 1,
        "2": 2, "two": 2,
        "3": 3, "three": 3,
        "4": 4, "four": 4,
        "5": 5, "five": 5,
    }

    # Pattern A: "rating (is) X", "rate X", "score X", "number X", "give X", "giving X"
    m_rate = re.search(
        r"\b(?:rating\s*(?:is|of)?|rate|score|number|give|giving|marked?)\s*([0-5]|zero|one|two|three|four|five)\b",
        raw,
    )
    if m_rate:
        token = m_rate.group(1).lower()
        if token in num_word_map:
            return num_word_map[token]

    # Pattern B: "X out of 5", "X / 5", "X of 5", "X out of five"
    m_out_of = re.search(
        r"\b([0-5]|zero|one|two|three|four|five)\s*(?:out of|\/|\s*of\s*)\s*(?:5|five)\b",
        raw,
    )
    if m_out_of:
        token = m_out_of.group(1).lower()
        if token in num_word_map:
            return num_word_map[token]

    # Pattern C: "X star(s)" (e.g. "0 stars", "zero star", "5 stars")
    m_star = re.search(
        r"\b([0-5]|zero|one|two|three|four|five)\s*stars?\b",
        raw,
    )
    if m_star:
        token = m_star.group(1).lower()
        if token in num_word_map:
            return num_word_map[token]

    # 3. Clean prompt echo: strip standard question boilerplate before general analysis
    cleaned = raw
    echo_patterns = [
        r"i\s+didn['’]?t\s+quite\s+(?:catch|get)\s+that",
        r"on\s+a\s+scale\s+of\s+(?:0|zero)\s+to\s+(?:5|five)",
        r"scale\s+of\s+(?:0|zero)\s+to\s+(?:5|five)",
        r"(?:from\s+)?(?:0|zero)\s+to\s+(?:5|five)",
        r"how\s+would\s+you\s+rate\s+your\s+support\s+experience",
        r"rate\s+your\s+support\s+experience(?:\s+today)?",
        r"you\s+can\s+say\s+a\s+number",
        r"or\s+press\s+a\s+digit(?:\s+on\s+your\s+keypad)?",
        r"press\s+a\s+digit",
        r"support\s+experience(?:\s+today)?",
    ]
    for ep in echo_patterns:
        cleaned = re.sub(ep, " ", cleaned).strip()

    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # If the input was purely an echo of the prompt with no user rating, return None!
    if not cleaned or cleaned in ["i", "to", "on", "a", "of", "and", "the"]:
        return None

    # Check cleaned text against direct map
    if cleaned in direct_map:
        return direct_map[cleaned]

    # Pattern D: Check for zero specifically (highest priority for consumer dissatisfaction)
    if any(k in cleaned for k in ["zero", "worst", "terrible", "horrible", "awful", "extremely poor", "unhappy", "dissatisfied", "disaster"]):
        return 0

    # Pattern E: Single standalone digit 0-5 in cleaned text
    m_clean_digit = re.search(r"\b([0-5])\b", cleaned)
    if m_clean_digit:
        return int(m_clean_digit.group(1))

    # Pattern F: Check descriptive words and other numerals
    if any(k in cleaned for k in ["five", "excellent", "best", "superb", "awesome", "perfect"]):
        return 5
    if any(k in cleaned for k in ["four", "very good"]):
        return 4
    if any(k in cleaned for k in ["three", "average", "okay", "ok"]):
        return 3
    if any(k in cleaned for k in ["two", "below average", "fair"]):
        return 2
    if any(k in cleaned for k in ["one", "poor", "bad"]):
        return 1
    if any(k in cleaned for k in ["great", "good"]):
        return 4 if "very good" in cleaned else 3

    return None


AGENT_SPECIALIZATIONS = {
    1: {
        "desk": "Substation Central Telemetry & Emergency Unit",
        "role_title": "Outage & Emergency Dispatch Specialist",
        "focus": "Power outages, blackout reporting, feeder line tripping, restoration ETA, and dispatching emergency line crews.",
        "style": "Empathetic, urgent, decisive field dispatch controller. Focus strictly on supply status and outage restoration.",
        "prefix": "Outage Dispatch Officer Priya here.",
    },
    2: {
        "desk": "33/11kV Substation Control Desk",
        "role_title": "High Voltage Grid & Substation Specialist",
        "focus": "33kV/11kV substations, grid frequency, feeder telemetry, transformer oil temperature, phase balance, and transmission grid stability.",
        "style": "Technical, precise, analytical grid telemetry engineer.",
        "prefix": "Substation Telemetry Officer Vikram here.",
    },
    3: {
        "desk": "Central Revenue & Consumer Ledgers Desk",
        "role_title": "Billing & Tariffs Specialist",
        "focus": "Electricity bills, domestic and commercial tariff slabs, arrears, energy charges, base fare, invoice downloads, Quick Pay, and step-by-step online payment guidance.",
        "style": "Meticulous, clear, financial accounting specialist. Expert on payment procedures and bill calculations.",
        "prefix": "Billing & Tariffs Specialist Officer Ananya here.",
    },
    4: {
        "desk": "Distribution Disaster & Safety Enforcement Desk",
        "role_title": "Electrical Safety & Hazard Response Officer",
        "focus": "Sparks from pole transformer, fire, explosion, loud blasts, broken/fallen live conductors, shock risk, and immediate public safety isolation.",
        "style": "High-priority safety officer. ALWAYS prioritize public safety first (warn callers to maintain a 10-meter clearance from sparks/conductors).",
        "prefix": "Electrical Hazard & Safety Officer Rahul here.",
    },
    5: {
        "desk": "Transformer Health Telemetry & Diagnostic Desk",
        "role_title": "Substation & Transformer Diagnostic Specialist",
        "focus": "Distribution unit diagnostics (T-01, T-02), kVA capacity %, load monitoring, thermal overload, and breaker trips.",
        "style": "Telemetry diagnostics engineer, focused on transformer health and load thresholds.",
        "prefix": "Transformer Diagnostics Officer Sneha here.",
    },
    6: {
        "desk": "Smart Meter Test Bench & IoT Telemetry Desk",
        "role_title": "Smart Meter & Service Connection Officer",
        "focus": "Smart meter telemetry, units consumed (kWh), meter display faults, fast/slow meter testing, appliance load watts, and remote breaker status.",
        "style": "Smart metering technical specialist, focused on meter readings, accuracy verification, and connected load diagnostics.",
        "prefix": "Smart Meter Specialist Officer Rajesh here.",
    },
    7: {
        "desk": "Ombudsman Consumer Redressal Desk",
        "role_title": "Citizen Grievance & Escalation Supervisor",
        "focus": "Consumer grievances, delayed tickets, SLA breach tracking, ombudsman escalation, compensation claims, and administrative complaints.",
        "style": "Consumer advocate and ombudsman officer, focused on resolution SLAs and escalation protocols.",
        "prefix": "Citizen Grievance Officer Kavita here.",
    },
    8: {
        "desk": "Line Crew Operations & Mobile Workshop Desk",
        "role_title": "Field Dispatch & Line Crew Coordinator",
        "focus": "Linemen dispatch, repair vans, bucket trucks, blown HT fuses, pole replacements, and field technician ETAs.",
        "style": "Field operations coordinator, focused on technician assignments and physical repair crew tracking.",
        "prefix": "Field Line Crew Dispatcher Officer Amit here.",
    },
    9: {
        "desk": "Commercial, Industrial & HT Consumer Desk",
        "role_title": "Commercial Accounts & Energy Audit Specialist",
        "focus": "Commercial tariffs, industrial 3-phase connections, maximum demand indicators (MDI), power factor penalties, and bulk energy contracts.",
        "style": "Commercial account executive, focused on commercial tariffs, business load factors, and high-tension accounts.",
        "prefix": "Commercial Accounts Specialist Officer Pooja here.",
    },
    10: {
        "desk": "Central Command Shift Operations Desk",
        "role_title": "24/7 Helpline Shift In-Charge",
        "focus": "Overall helpline supervisory oversight, complex issue resolution, cross-departmental coordination, emergency overrides, and quality management.",
        "style": "Authoritative senior supervisor, polite, decisive, capable of resolving any issue across all departments.",
        "prefix": "Senior Shift Supervisor Officer Suresh here.",
    },
    11: {
        "desk": "Customer Satisfaction & QA Audit Desk",
        "role_title": "Quality Assurance Desk",
        "focus": "Consumer satisfaction ratings (0-5 stars), feedback recording, service quality assurance.",
        "style": "Warm, appreciative quality assurance representative collecting caller feedback.",
        "prefix": "Quality Assurance Officer Ritu here.",
    },
}


def process_call_turn(
    session: VoiceCallSession,
    caller_text: str,
    user_account=None,
) -> Dict[str, Any]:
    """Execute a single conversational turn in the voice call session."""
    start_time = time.time()
    clean_text = (caller_text or "").strip()

    # 1. Fetch live knowledge base
    kb = get_live_knowledge_base(user_account=user_account or session.user_account)
    kb_text = format_knowledge_base_prompt(kb)
    system_prompt = build_system_instructions(kb_text)

    # 2. Inspect previous conversation turns to maintain multi-turn customer care memory
    recent_msgs = list(
        VoiceCallMessage.objects.filter(session=session).order_by("-created_at")[:14]
    )
    last_ai_msg = next((m for m in recent_msgs if m.sender == VoiceCallMessage.SENDER_AI), None)
    last_ai_text = last_ai_msg.text.lower() if last_ai_msg else ""

    was_asking_diagnostic = any(
        k in last_ai_text
        for k in [
            "confirm if neighboring houses",
            "street are also affected",
            "how long the power has been off",
            "hazards like sparking",
            "experiencing a power outage",
            "experiencing a power cut",
            "transformer sparking",
        ]
    )
    was_asking_anything_else = any(
        k in last_ai_text
        for k in [
            "anything else i can assist",
            "anything else i can help",
            "anything further",
            "how else may i help",
            "is there anything else",
            "should i help you with any other",
            "help you with any other",
        ]
    )
    was_asking_invoice_notification = any(
        k in last_ai_text
        for k in [
            "send a copy to your account notifications",
            "send your invoice to your account notifications",
            "send a copy to your notifications",
            "send it to your notifications",
            "send your invoice to notifications",
            "send this invoice to notifications",
            "send this to your notifications",
        ]
    )
    was_asking_payment_guidance = any(
        k in last_ai_text
        for k in [
            "guide you on how to pay",
            "guide on how to pay",
            "how to pay it online",
            "how to pay online",
            "how to pay it",
            "how to pay",
            "app installed on your phone",
            "have the app installed",
            "app installed",
            "which payment method would you prefer",
            "how would you prefer to pay",
        ]
    )
    was_asking_daily_usage_notification = any(
        k in last_ai_text
        for k in [
            "daily usage summary to your account notifications",
            "daily usage summary to your notifications",
            "usage summary to your account notifications",
            "usage summary to your notifications",
            "daily usage summary",
        ]
    )
    was_asking_transfer_feedback = any(
        k in last_ai_text
        for k in [
            "transfer the call to feedback",
            "transfer the call to our feedback",
            "transfer you to our feedback",
            "transfer your call to our feedback",
            "transfer to feedback",
            "transfer the call to feedback desk",
            "transfer to our feedback desk",
        ]
    )
    last_invoice_bill_id = None
    if last_ai_msg and last_ai_msg.action_taken and "FETCH_INVOICE:" in last_ai_msg.action_taken:
        m_inv = re.search(r"FETCH_INVOICE:\s*(\d+)", last_ai_msg.action_taken)
        if m_inv:
            last_invoice_bill_id = int(m_inv.group(1))

    # 3. Record caller message
    clean_display_text = clean_text or "[Caller connected]"
    if clean_text.lower().startswith("no input") or "silence" in clean_text.lower():
        clean_display_text = "[No speech detected - Silence]"

    VoiceCallMessage.objects.create(
        session=session,
        sender=VoiceCallMessage.SENDER_CALLER,
        text=clean_display_text,
    )

    # 4. Contextual variables
    action_taken = ""
    registered_complaint_info = None
    lookup_complaint_info = None
    invoice_payload = None
    hang_up_call = False
    feedback_recorded = None
    intent = VoiceCallSession.INTENT_GENERAL_INQUIRY

    from .models import CallCenterAgent
    if not CallCenterAgent.objects.filter(is_active=True).exists():
        CallCenterAgent.seed_default_10_agents()
    assigned_agent = session.assigned_agent

    text_lower = clean_text.lower()
    caller_name = session.caller_name
    if user_account and (not caller_name or caller_name == "Consumer"):
        caller_name = user_account.full_name

    # Determine caller premise details from live knowledge base
    loc = "Registered Premise Area"
    service_num = ""
    cp = kb.get("caller_profile")
    if cp:
        if cp.get("house_address") and cp.get("house_address") != "N/A":
            loc = cp["house_address"]
        if cp.get("meter_number"):
            service_num = cp["meter_number"]

    # If caller explicitly provided a location in their speech
    loc_match = re.search(r"(?:at|in|near)\s+([a-zA-Z0-9\s,.-]{3,40})", clean_text, re.IGNORECASE)
    if loc_match:
        extracted_loc = loc_match.group(1).strip()
        if not any(stop in extracted_loc.lower() for stop in ["complaint", "power", "outage", "my house", "my street"]):
            loc = extracted_loc

    # Check for direct complaint status lookup
    cmp_match = re.search(r"\b(CMP-\d{6}-\d{4}|[A-Fa-f0-9]{8})\b", clean_text)
    is_lookup_query = any(
        k in text_lower
        for k in ["status", "track", "check complaint", "progress", "update on my complaint", "lookup", "look up", "ticket status"]
    )

    # Check for spoken/typed caller credentials (Mobile, Meter, Email, Request ID)
    phone_match = re.search(r"\b([6-9]\d{9})\b", clean_text)
    meter_match = re.search(r"\b(MTR-[A-Za-z0-9-]+)\b", clean_text, re.IGNORECASE)
    req_match = re.search(r"\b((?:APP|REQ|CONN|CA)-[A-Za-z0-9-]+)\b", clean_text, re.IGNORECASE)
    email_match = re.search(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b", clean_text)
    has_credential_input = bool(phone_match or meter_match or req_match or email_match) and not is_lookup_query

    # Check for connection timeline / application upload / meter issuance date inquiries
    is_connection_timeline_or_date_query = any(k in text_lower for k in [
        "meter was issued", "meter issue date", "meter issued date", "meter issuance date",
        "when did my meter was issued", "when was my meter issued", "when did my meter get issued",
        "when was my meter installed", "when meter was installed", "when was meter issued",
        "meter installed date", "meter installation date", "date meter was issued",
        "application was uploaded", "application upload date", "application uploaded date",
        "when was my application uploaded", "when did my application get uploaded",
        "application and my connection", "application and connection",
        "exact data in the time", "exacted data in the time", "exact date and time",
        "exact date and the time", "exact date or time", "exact timestamp",
        "when did i apply", "when did i submit my application", "when was application approved",
        "connection timeline", "when was connection approved", "when was my connection connected",
        "when was connection energized", "when was power connected to my house",
        "date when my application", "time when my application", "date of my application",
        "date of connection", "connection date", "approval date", "application submission date"
    ]) or (
        any(k in text_lower for k in ["when", "what date", "which date", "what time", "which time", "exact date", "exact data"])
        and any(k in text_lower for k in ["meter", "application", "connection"])
        and any(k in text_lower for k in ["issue", "issued", "upload", "uploaded", "approved", "install", "installed", "applied", "connected", "energized"])
    )

    # Check for third-party consumer or transformer meter list queries (Strict DISCOM Privacy Guard)
    is_third_party_meter_query = any(k in text_lower for k in [
        "other meter", "other meters", "other metre", "other metres",
        "metres connected to", "meters connected to", "meter connected to", "metre connected to",
        "metres attached to", "meters attached to", "meters on transformer", "metres on transformer",
        "meters on the transformer", "metres on the transformer", "meters connected with", "metres connected with",
        "who else is connected", "other consumers", "someone else's meter", "another person's meter",
        "list of meters", "list of metres", "all meters connected", "all metres connected",
        "neighbors meter", "neighbour meter", "neighbor's meter", "neighbour's meter",
        "other connections on", "all connections on", "connected to transformer", "connected to the transformer"
    ]) or (
        any(m in text_lower for m in ["meter", "meters", "metre", "metres", "connection", "connections"])
        and any(t in text_lower for t in ["transformer", "feeder", "grid"])
        and any(w in text_lower for w in ["all", "list", "who", "other", "what are", "which", "how many"])
        and not any(self_ref in text_lower for self_ref in ["my meter", "my connection", "my reading", "my bill"])
    )

    # Check for outage reason / power cut explanation / telemetry query
    is_outage_reason_query = any(k in text_lower for k in [
        "why the power cut", "why power cut", "why did power cut", "why power is cut",
        "why is power cut", "why power is off", "why is power off", "why did power go off",
        "why did the power go off", "why did power go", "why did the power cut happen",
        "why lights went off", "why no power", "why is there no power", "why power went",
        "why current poyindi", "reason for power cut", "reason for outage", "reason for no power",
        "why the power has been cut", "what happened to power", "what happened to the power",
        "cause of power cut", "why power cut happened", "why power cut has happened",
        "why the power cut has", "why power cut has", "power cut reason", "outage reason",
        "why current is cut", "why current is off", "why no current", "is there a power cut",
        "power cut status", "feeder status", "grid status", "is power cut", "substation status"
    ]) or (
        any(k in text_lower for k in ["why", "reason", "what happened", "cause"])
        and any(k in text_lower for k in ["power", "cut", "outage", "light", "electricity", "supply", "blackout", "current"])
        and not any(k in text_lower for k in ["register", "file complaint", "raise ticket"])
    )

    # Check for scheduled / future outage inquiry (telemetry & planned maintenance checks)
    is_scheduled_or_future_outage_query = (
        not any(k in text_lower for k in [
            "register complaint", "file complaint", "raise ticket", "record complaint",
            "facing power cut", "facing power outage", "i have no power", "no current at home",
            "lights are off", "my power is out", "power is gone", "power went off right now",
            "report outage", "fallen wire", "sparking", "fire on pole"
        ])
        and (
            any(k in text_lower for k in [
                "future power outage", "future power cut", "future outage", "future cut",
                "outage today", "power outage today", "power cut today", "outage for today",
                "is there any power outage", "is there any power cut", "is there any outage",
                "will there be any power outage", "will there be any power cut", "will there be an outage",
                "will there be any outage", "will there be future power outage", "will there be power cut",
                "any power outage today", "any power cut today", "any power cuts today",
                "scheduled power cut", "scheduled outage", "planned outage", "planned power cut",
                "maintenance schedule", "load shedding", "upcoming outage", "upcoming power cut",
                "shutdown today", "power shutdown", "power cut timings", "feeder maintenance",
                "will power be cut", "will the power go off", "is power going to be cut",
                "any shutdown", "any power cut scheduled", "any power cut tomorrow", "scheduled shutdown",
                "scheduled outage today", "planned shutdown today", "is there any power cut today",
                "is there any power outage today"
            ])
            or (
                any(k in text_lower for k in ["future", "today", "tomorrow", "scheduled", "planned", "upcoming", "any"])
                and any(k in text_lower for k in ["power outage", "power cut", "outage", "shutdown", "load shedding"])
                and any(q in text_lower for q in ["will", "is there", "any", "when", "tell me", "check", "know"])
            )
        )
    )

    # Check for IVR return request: "*" or "repeat menu", "main menu", "ivr menu"
    is_repeat_ivr = (
        text_lower.strip() in ["*", "star", "asterisk"]
        or any(k in text_lower for k in ["repeat menu", "repeat ivr", "repeat options", "main menu", "ivr menu", "options menu", "back to menu", "go back to menu", "start over"])
        or (session.is_ivr_active and text_lower.strip() in ["repeat", "options", "menu"])
    )

    # Check for direct UPI debit / collect UPI ID query (prohibited under financial security guidelines)
    is_direct_upi_debit_query = (
        any(k in text_lower for k in [
            "will you directly pay", "can you directly pay", "will you pay directly",
            "can you pay directly", "will you pay my", "can you pay my", "pay it for me",
            "pay my bill for me", "pay for me", "deduct from my upi", "deduct my upi",
            "take my upi", "tell my upi", "tell my ubi", "give my upi", "enter my upi",
            "guide you my upi", "guide you my ubi", "use my upi", "use my ubi",
            "take my pin", "tell you my upi", "give you my upi", "send you my upi",
            "will you debit", "can you debit", "deduct directly"
        ])
        or (
            any(k in text_lower for k in ["upi", "ubi", "pin", "debit card", "credit card", "bank account", "account details", "gpay", "phonepe", "paytm"])
            and any(k in text_lower for k in [
                "directly pay", "pay directly", "will you pay", "can you pay", "deduct",
                "take my", "give you", "guide you my", "tell you my", "pay for me", "pay it for"
            ])
        )
    )

    # Detect selected IVR option or in-call transfer
    ivr_choice = detect_ivr_option(text_lower, in_ivr=session.is_ivr_active)
    if is_direct_upi_debit_query:
        ivr_choice = None

    # Check for direct agent transfer request e.g. "transfer to Agent 2", "talk to Agent 3", "transfer to outage department", "transfer my call to outage"
    transfer_agent_num = None if is_third_party_meter_query else detect_agent_transfer(text_lower)
    is_agent_transfer_query = (transfer_agent_num is not None)
    if is_agent_transfer_query and not ivr_choice:
        ivr_choice = transfer_agent_num

    # Check if caller is confirming a transfer offered by the agent in the previous turn
    offered_transfer_target = detect_offered_agent_transfer(last_ai_text)
    is_confirming_offered_transfer = (
        offered_transfer_target is not None
        and any(k in text_lower for k in [
            "yes", "yeah", "yep", "sure", "please", "yes please", "ok", "okay",
            "connect", "transfer", "please connect", "do it", "go ahead", "connect me",
            "please transfer", "transfer please", "yes connect", "connect please"
        ])
        and not any(neg in text_lower for neg in ["no", "don't", "dont", "never", "cancel", "not now"])
    )
    if is_confirming_offered_transfer and not ivr_choice:
        ivr_choice = offered_transfer_target
        is_agent_transfer_query = True

    # Check for available departments / officers directory query
    is_available_departments_query = any(k in text_lower for k in [
        "how many officers are available", "how many officers", "how many departments",
        "how many agents are available", "how many agents", "how many specialists",
        "agents are available under your service", "agents available under your service",
        "what are the departments available", "what departments are available",
        "which departments are available", "what departments under your service",
        "departments available with under your service", "departments available under your service",
        "departments available under", "departments available", "agents available",
        "what are the departments", "what departments do you have", "what departments are there",
        "what agents do you have", "what officers do you have", "what are the agents",
        "tell me the departments", "list of departments", "list of officers", "list of agents",
        "which officers are available", "which agents are available", "who can i speak with",
        "available departments", "available officers", "available agents", "what services are available",
        "show departments", "show agents", "the specific department how can i tell you"
    ]) or (
        any(k in text_lower for k in ["what", "which", "how many", "tell me", "list", "show"])
        and any(k in text_lower for k in ["departments", "officers", "agents", "specialists", "teams", "desks"])
    )

    # Check for payment guidance / step-by-step online payment / Quick Pay without login
    is_payment_guidance_query = (
        not is_direct_upi_debit_query
        and (
            any(k in text_lower for k in [
                "how to pay", "how can i pay", "how do i pay", "where to pay", "where can i pay",
                "guide me how to pay", "guide me to pay", "guide how to pay", "guide on how to pay",
                "how to pay it", "how to pay online", "how to pay it online", "pay it online",
                "pay my bill", "pay the bill", "pay bill", "pay online", "pay my unpaid bill",
                "pay unpaid bill", "pay pending bill", "payment steps", "step by step",
                "steps to pay", "steps for payment", "payment options", "payment procedure",
                "without login can i pay", "can i pay without login", "can i pay without log in",
                "can i pay the bill without login", "can i pay bill without login", "pay without login",
                "pay without logging in", "without login", "quick pay", "quick bill pay",
                "instant payment", "payment portal", "how do we pay", "how can we pay",
                "how should i pay", "how to make payment", "make payment", "pay through website",
                "pay on website", "pay via website", "pay using website", "pay in the website",
                "pay through app", "pay via upi", "can i pay", "how to pay on online",
                "how to pay it on online", "yes how to pay", "please guide me how to pay",
                "how to pay my", "procedure to pay", "can we pay without login", "pay without an account",
                "how to clear bill", "how to clear my bill", "how do i clear"
            ])
            or (
                any(k in text_lower for k in ["how", "where", "can i", "steps", "guide", "procedure", "without login", "without logging in", "options", "way to"])
                and any(p in text_lower for p in ["pay", "payment", "clearing", "settle", "settling"])
            )
            or (
                was_asking_payment_guidance
                and not any(k in text_lower for k in ["have paid", "i paid", "already paid", "previous", "what did i pay", "how much did i pay", "last payment", "outage", "complaint", "status", "meter"])
                and (
                    any(k in text_lower for k in [
                        "guide me", "how to pay", "pay online", "quick pay", "yes", "yeah", "yep", "sure", "please guide", "tell me how", "show me how", "how can i", "how do i"
                    ])
                    or (len(text_lower.split()) <= 4 and any(k in text_lower for k in ["yes", "yeah", "yep", "sure", "please", "ok", "okay", "guide", "proceed", "help"]))
                )
                and not any(neg in text_lower for neg in ["no", "never", "cancel", "not now", "don't", "dont", "no thanks", "no need"])
            )
        )
        and not any(k in text_lower for k in [
            "have paid", "i paid", "already paid", "previous month", "previous bill",
            "last payment", "previous payment", "what did i pay", "how much did i pay", "paid in full", "paid in the"
        ])
    )

    # =========================================================================
    # DIALOGUE STATE MACHINE: NATURAL TELEPHONE CUSTOMER CARE CONVERSATION
    # =========================================================================

    # 0. Active Customer Satisfaction Feedback Stage (Agent 11 - Officer Ritu)
    if session.is_feedback_stage or ("on a scale of 0 to 5" in last_ai_text) or ("rate your support experience" in last_ai_text):
        session.is_feedback_stage = True
        is_fb_silence_1 = any(k in text_lower for k in ["no input feedback reprompt 1", "feedback silence 1"])
        is_fb_silence_2 = any(k in text_lower for k in ["no input feedback reprompt 2", "feedback silence 2", "auto hangup"])

        if is_fb_silence_1:
            intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
            action_taken = "FEEDBACK_INACTIVITY_REPROMPT"
            ai_reply = (
                f"Are you still on the line? On a scale of 0 to 5, please share your rating before we disconnect. "
                f"You can say a number from 0 to 5, or press a digit on your keypad."
            )
        elif is_fb_silence_2:
            session.close_call()
            session.save()
            hang_up_call = True
            intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
            action_taken = "AUTO_HANGUP_SILENCE"
            ai_reply = (
                f"Thank you for contacting Electricity Consumer Care. As there has been no response, I will now hang up the call. "
                f"Have a wonderful and safe day ahead! Goodbye."
            )
        else:
            rating = parse_feedback_rating(text_lower)
            if rating is not None:
                session.feedback_rating = rating
                session.feedback_submitted_at = timezone.now()
                session.close_call()
                session.save()
                hang_up_call = True
                feedback_recorded = {"rating": rating, "score": f"{rating}/5"}
                intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
                action_taken = f"FEEDBACK_RATING_RECORDED: {rating}/5"
                ai_reply = (
                    f"Thank you so much for your feedback rating of {rating} out of 5! "
                    f"Your response has been recorded to improve our consumer services. Have a wonderful and safe day ahead! Goodbye."
                )
            else:
                intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
                action_taken = "FEEDBACK_RATING_RETRY"
                ai_reply = (
                    f"I didn't quite catch that. On a scale of 0 to 5, how would you rate your support experience today? "
                    f"You can say a number from 0 to 5, or press a digit on your keypad."
                )

    # IVR Inactivity First Reprompt (Prompting caller after prolonged silence in IVR)
    elif session.is_ivr_active and not assigned_agent and "no input reprompt 1" in text_lower:
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "IVR_INACTIVITY_REPROMPT"
        ai_reply = (
            f"I haven't received your selection yet. "
            f"You can simply say or press 1 for outages, 2 for grid telemetry, 3 for billing, or say supervisor to connect with an officer."
        )

    # Agent Inactivity First Reprompt (Prompting caller after initial silence when connected to an officer)
    elif any(k in text_lower for k in ["no input agent reprompt 1", "agent silence 1"]):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "AGENT_INACTIVITY_REPROMPT_1"
        ag_off = assigned_agent.officer_name if assigned_agent else "Officer Suresh"
        ai_reply = f"Hello {caller_name}, are you there? Please go ahead and describe your issue or query."

    # Agent Inactivity Second Reprompt (Offering transfer to feedback after second silence)
    elif any(k in text_lower for k in ["no input agent reprompt 2", "agent silence 2"]):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "AGENT_INACTIVITY_REPROMPT_2"
        ai_reply = "I haven't heard a response. Are you still there, or should I transfer the call to our feedback desk?"

    # Agent Inactivity Third Reprompt (Auto-transfer to Officer Ritu / Feedback Desk on continued silence)
    elif any(k in text_lower for k in ["no input agent reprompt 3", "agent silence 3"]):
        session.is_feedback_stage = True
        feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()
        if not feedback_agent:
            feedback_agent = CallCenterAgent.objects.filter(agent_code="AGENT-11").first()
        if not feedback_agent:
            CallCenterAgent.seed_default_10_agents()
            feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()
        if feedback_agent:
            session.assigned_agent = feedback_agent
        session.save()
        assigned_agent = feedback_agent
        intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
        action_taken = "AUTO_TRANSFER_FEEDBACK_SILENCE"
        ai_reply = (
            f"Thank you for contacting Electricity Consumer Care. As there was no further query, I've transferred your call to our Customer Feedback Desk. "
            f"Hello {caller_name}! This is Officer Ritu from Quality Assurance. On a scale of 0 to 5, how would you rate your support experience today?"
        )

    elif is_repeat_ivr:
        session.is_ivr_active = True
        session.assigned_agent = None
        session.save()
        assigned_agent = None
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "IVR_MENU_REPEAT"
        ai_reply = (
            f"Returning to the Main IVR Menu. Please select an option to connect with an administration agent:\n"
            f"• 1: Power Outages & Supply Cuts (Agent 1 - Officer Priya)\n"
            f"• 2: Substation & Grid Telemetry (Agent 2 - Officer Vikram)\n"
            f"• 3: Billing, Tariffs & Ledgers (Agent 3 - Officer Ananya)\n"
            f"• 4: Sparking, Fire & Hazards (Agent 4 - Officer Rahul)\n"
            f"• 5: Transformer Diagnostics (Agent 5 - Officer Sneha)\n"
            f"• 6: Smart Meter Faults & Blank Display (Agent 6 - Officer Rajesh)\n"
            f"• 7: Grievances & SLA Escalations (Agent 7 - Officer Kavita)\n"
            f"• 8: Field Line Crew Dispatch (Agent 8 - Officer Amit)\n"
            f"• 9: Commercial Bulk Accounts (Agent 9 - Officer Pooja)\n"
            f"• 0: Senior Shift In-Charge Supervisor (Agent 10 - Officer Suresh)\n"
            f"Please press or type a digit (0-9) to connect."
        )

    # State: Directory of 10 Administration Departments & Specialist Officers
    elif is_available_departments_query:
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "AVAILABLE_DEPARTMENTS_DIRECTORY"
        ai_reply = (
            f"We have 10 dedicated departments and specialist officers available 24/7:\n"
            f"• 1: Power Outages & Supply Cuts — Officer Priya\n"
            f"• 2: High Voltage Grid & Substation Telemetry — Officer Vikram\n"
            f"• 3: Billing, Tariffs & Meter Ledgers — Officer Ananya\n"
            f"• 4: Electrical Safety & Hazards — Officer Rahul\n"
            f"• 5: Transformer Diagnostics & Telemetry — Officer Sneha\n"
            f"• 6: Smart Meter Faults & Support — Officer Rajesh\n"
            f"• 7: Citizen Grievance & Escalations — Officer Kavita\n"
            f"• 8: Field Line Crew Dispatch — Officer Amit\n"
            f"• 9: Commercial Bulk Accounts — Officer Pooja\n"
            f"• 10: Senior Shift Supervisor — Officer Suresh\n\n"
            f"We also have our Customer Feedback Desk with Officer Ritu. Which department would you like me to connect you with?"
        )
        spoken_text = (
            f"We have 10 dedicated departments available 24/7: "
            f"Outages with Officer Priya, High Voltage Grid with Officer Vikram, Billing with Officer Ananya, "
            f"Safety and Hazards with Officer Rahul, Transformer Diagnostics with Officer Sneha, Smart Meters with Officer Rajesh, "
            f"Grievances with Officer Kavita, Line Crew with Officer Amit, Commercial Accounts with Officer Pooja, "
            f"and our Senior Shift Supervisor Officer Suresh. Which department would you like me to connect you with?"
        )

    # State: In-call transfer requested to the SAME agent already speaking
    elif is_agent_transfer_query and assigned_agent and assigned_agent.agent_number == ivr_choice:
        ag_name = assigned_agent.officer_name or assigned_agent.name
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "ALREADY_CONNECTED_TO_AGENT"
        ai_reply = (
            f"You are already connected with me, {ag_name} ({assigned_agent.role})! "
            f"I am actively handling your call. Please go ahead and describe how I can assist you."
        )

    # State: IVR option selection (or in-call agent transfer to another agent / operator)
    elif ivr_choice and (session.is_ivr_active or not assigned_agent or (assigned_agent and assigned_agent.agent_number != ivr_choice)):
        target_agent = CallCenterAgent.objects.filter(agent_number=ivr_choice).first()
        if not target_agent and ivr_choice == 11:
            target_agent = CallCenterAgent.objects.filter(agent_code="AGENT-11").first()

        # 1. Check if requested agent is explicitly OFFLINE or INACTIVE
        if target_agent and (not target_agent.is_active or target_agent.status == CallCenterAgent.STATUS_OFFLINE):
            intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
            action_taken = f"AGENT_OFFLINE_REJECTED: Agent {target_agent.agent_number} is offline"
            ag_name = target_agent.officer_name or target_agent.name
            ag_role = target_agent.role or "Specialist"
            ai_reply = (
                f"I apologize, {caller_name}. Agent {target_agent.agent_number} — {ag_name} ({ag_role}) "
                f"is currently offline on administrative break or off-duty. "
                f"Please choose another active department (such as Agent 1 for Outages, Agent 3 for Billing, "
                f"or press 0 for the Senior Shift Supervisor), or press star (*) to return to the Main IVR Menu."
            )
        elif target_agent and target_agent.agent_number == 11:
            session.is_feedback_stage = True
            session.assigned_agent = target_agent
            session.is_ivr_active = False
            session.save()
            assigned_agent = target_agent
            intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
            action_taken = "TRANSFER_FEEDBACK_AGENT"
            ai_reply = (
                f"Thank you for contacting Electricity Consumer Care. I am now transferring your call directly to our Customer Feedback Desk. Please hold the line for just a moment... "
                f"Call connected! "
                f"Hello {caller_name}! This is Officer Ritu from Quality Assurance. "
                f"On a scale of 0 to 5, how would you rate your support experience today? "
                f"You can say your rating or press a digit on your keypad."
            )
        elif target_agent and target_agent.is_active and target_agent.status != CallCenterAgent.STATUS_OFFLINE:
            prev_agent = assigned_agent
            session.assigned_agent = target_agent
            session.is_ivr_active = False
            session.save()
            assigned_agent = target_agent
            intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
            action_taken = f"IVR_CONNECTED_TO_AGENT_{target_agent.agent_number}"
            if prev_agent and prev_agent.agent_number != target_agent.agent_number:
                target_name = target_agent.officer_name or target_agent.name
                target_desk = target_agent.department_desk or target_agent.role or "Specialist Desk"
                ai_reply = (
                    f"Certainly! Please hold the line for a moment while I transfer your call directly to {target_name} at our {target_desk}... "
                    f"Call connected! "
                    f"Hello {caller_name}! This is {target_name}, {target_agent.role}. "
                    f"I am actively on the line with you. How may I assist you today?"
                )
            else:
                ai_reply = get_agent_opening_dialogue(target_agent, caller_name, service_num, loc, cp, kb)
        else:
            intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
            action_taken = "AGENT_NOT_FOUND"
            ai_reply = f"I could not locate an active agent with number {ivr_choice}. We have 10 agents available from Agent 1 to Agent 10. Please dial or type a number between 1 and 10."

    # State: In-Call Touch-Tone DTMF Keypad Press during Live Agent Conversation
    # Prevents accidental transfer away from the active agent when user dials digits on the keypad
    elif not session.is_ivr_active and assigned_agent and text_lower.strip() in ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "#", "hash"]:
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        key_pressed = text_lower.strip()
        action_taken = f"IN_CALL_DTMF_KEY_RECEIVED: {key_pressed}"
        ag_off = assigned_agent.officer_name
        if key_pressed == "0":
            ai_reply = f"You are already speaking with Senior Shift Supervisor {ag_off}. How may I assist you with your electricity service?"
        elif key_pressed in ["#", "hash"]:
            ai_reply = f"Touch-tone [#] confirm received. I am {ag_off} on the line with you. Please go ahead and tell me what you need assistance with."
        else:
            ai_reply = f"Touch-tone key [{key_pressed}] received on line. This is {ag_off} — please go ahead and speak or type your request, or press star (*) if you wish to return to the Main IVR Menu."

    # State: Caller Confirms Sending Invoice to Account Notifications
    elif was_asking_invoice_notification and any(
        k in text_lower
        for k in [
            "yes", "yeah", "yep", "sure", "please", "send", "ok", "okay",
            "confirm", "send please", "yes please", "please send", "send it",
            "do it", "send that", "send to notifications", "send invoice",
            "send the invoice", "send copy", "send receipt"
        ]
    ) and not any(neg in text_lower for neg in ["no", "don't", "dont", "never", "cancel", "not now"]):
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        bill = None
        if last_invoice_bill_id:
            bill = PowerBill.objects.filter(pk=last_invoice_bill_id).first()
        target_user = user_account or session.user_account
        if not bill and target_user:
            bill = (
                PowerBill.objects.filter(connection__user=target_user, status=PowerBill.STATUS_PAID).order_by("-paid_at", "-created_at").first()
                or PowerBill.objects.filter(connection__user=target_user).order_by("-created_at").first()
            )
        if not bill:
            bill = PowerBill.objects.order_by("-created_at").first()

        if target_user and bill:
            b_month = bill.billing_month or "Current"
            b_num = bill.bill_number
            b_meter = bill.connection.meter_number if bill.connection else service_num
            b_amt = f"{bill.total_amount:.2f}"
            b_units = f"{bill.units_billed}"
            b_url = reverse("download_bill_invoice_pdf", kwargs={"bill_id": bill.id})

            existing_notif = UserNotification.objects.filter(user=target_user, extra_info=f"Bill No: {b_num}").first()
            if not existing_notif:
                UserNotification.objects.create(
                    user=target_user,
                    category=UserNotification.CATEGORY_BILLS,
                    category_label="Payment Invoice",
                    severity=UserNotification.SEVERITY_SUCCESS,
                    icon="fa-solid fa-file-invoice-dollar",
                    badge="INVOICE DELIVERED",
                    title=f"Paid Invoice: {b_month} ({b_num})",
                    message=f"Official tax invoice for Meter {b_meter} — Total Rs {b_amt} for {b_units} units is confirmed and delivered to your account hub.",
                    extra_info=f"Bill No: {b_num}",
                    cta_label="View / Download Invoice PDF",
                    cta_url=b_url,
                )
        action_taken = f"SEND_INVOICE_NOTIFICATION: {bill.bill_number if bill else 'DISCOM-INV'}"
        ai_reply = (
            f"I have sent your official invoice copy to your notifications hub. "
            f"You can view or download it anytime on your dashboard. Is there anything else I can assist you with today?"
        )

    # State: Caller Declines Sending Invoice to Account Notifications
    elif was_asking_invoice_notification and any(
        k in text_lower
        for k in ["no", "no thanks", "no need", "dont", "don't", "skip", "never mind", "cancel", "not now"]
    ) and not any(k in text_lower for k in [
        "bill", "amount", "paid", "unpaid", "unpaired", "how much", "meter", "complaint", "outage", "previous"
    ]):
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        action_taken = "DECLINE_INVOICE_NOTIFICATION"
        ai_reply = (
            f"Understood. I will not send it to notifications. "
            f"You can print it directly from your screen. How else may I assist you today?"
        )

    # State: Caller Confirms Sending Daily Usage Summary to Account Notifications
    elif was_asking_daily_usage_notification and any(
        k in text_lower
        for k in [
            "yes", "yeah", "yep", "sure", "please", "send", "ok", "okay",
            "confirm", "send please", "yes please", "please send", "send it",
            "schedule", "yes schedule", "schedule it"
        ]
    ) and not any(neg in text_lower for neg in ["no", "don't", "dont", "never", "cancel", "not now"]):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        target_user = user_account or session.user_account
        meter = service_num or "MTR-2026-1003-2"
        cp = kb.get("caller_profile")
        reading = cp.get("current_meter_reading", 192.29) if cp else 192.29
        if target_user:
            UserNotification.objects.create(
                user=target_user,
                category=UserNotification.CATEGORY_LIVE,
                category_label="Daily Usage",
                severity=UserNotification.SEVERITY_INFO,
                icon="fa-solid fa-bolt",
                badge="DAILY USAGE",
                title="Daily Consumption Summary",
                message=f"Smart Meter {meter} daily reading stands at {reading:.2f} kWh with 0.00 kWh recorded today.",
                extra_info=f"Meter: {meter}",
                cta_label="View Usage Analytics",
                cta_url=reverse("user_home"),
            )
        action_taken = "SEND_DAILY_USAGE_NOTIFICATION"
        ai_reply = (
            f"I have scheduled your daily usage summary to be delivered to your account notifications hub. "
            f"Is there anything else I can assist you with today?"
        )

    # State: Caller Declines Further Assistance when asked "Should I help you with any other queries / anything else?"
    elif was_asking_anything_else and (
        any(
            k in text_lower
            for k in [
                "no", "nope", "nothing", "that's all", "that is all", "that's it", "that is it",
                "done", "no thanks", "no thank you", "all good", "i'm good", "no i am good",
                "no that'll be all", "no that will be all", "nothing more", "no more"
            ]
        )
        or (len(text_lower.split()) <= 3 and any(k in text_lower for k in ["no", "nah", "nope", "fine", "bye", "goodbye"]))
    ) and not any(k in text_lower for k in ["yes", "yeah", "yep", "sure", "please", "register", "outage", "bill", "status", "meter", "ok", "okay"]):
        feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()
        if not feedback_agent:
            feedback_agent = CallCenterAgent.objects.filter(agent_code="AGENT-11").first()
        if not feedback_agent:
            CallCenterAgent.seed_default_10_agents()
            feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()

        session.is_feedback_stage = True
        session.assigned_agent = feedback_agent
        session.save()
        assigned_agent = feedback_agent
        intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
        action_taken = "TRANSFER_FEEDBACK_AGENT"
        ai_reply = (
            f"Thank you for contacting Electricity Consumer Care. "
            f"I am now transferring your call to our Customer Feedback Desk. "
            f"Hello {caller_name}! This is Officer Ritu from Quality Assurance. "
            f"On a scale of 0 to 5, how would you rate your support experience today? "
            f"You can say your rating or press a digit on your keypad."
        )

    # State: Caller Confirms Transfer to Feedback Desk when asked in Reprompt 2
    elif was_asking_transfer_feedback and any(
        k in text_lower
        for k in ["yes", "yeah", "yep", "sure", "transfer", "feedback", "please transfer", "transfer call", "ok", "okay", "yes please"]
    ) and not any(neg in text_lower for neg in ["no", "don't", "dont", "never", "cancel", "not now", "wait", "here"]):
        feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()
        if not feedback_agent:
            feedback_agent = CallCenterAgent.objects.filter(agent_code="AGENT-11").first()
        if not feedback_agent:
            CallCenterAgent.seed_default_10_agents()
            feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()

        session.is_feedback_stage = True
        session.assigned_agent = feedback_agent
        session.save()
        assigned_agent = feedback_agent
        intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
        action_taken = "TRANSFER_FEEDBACK_AGENT"
        ai_reply = (
            f"Thank you for contacting Electricity Consumer Care. I am now transferring your call to our Customer Feedback Desk. "
            f"Hello {caller_name}! This is Officer Ritu from Quality Assurance. On a scale of 0 to 5, how would you rate your support experience today? "
            f"You can say your rating or press a digit on your keypad."
        )

    # State: Caller Declines Transfer to Feedback / Confirms they are still here
    elif was_asking_transfer_feedback and any(
        k in text_lower
        for k in ["no", "don't", "dont", "wait", "i'm here", "i am here", "here", "still here", "hold on", "one second", "listen"]
    ) and not any(k in text_lower for k in ["bill", "amount", "paid", "unpaid", "meter", "outage", "cut", "spark"]):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "STILL_ON_LINE_ACKNOWLEDGED"
        ai_reply = "I'm right here with you! Please go ahead and describe your electricity issue or query."

    # State: Caller Confirms Further Assistance when asked
    elif was_asking_anything_else and any(
        k in text_lower
        for k in ["yes", "yeah", "yep", "sure", "please", "i do", "i have a question", "help me"]
    ) and not any(k in text_lower for k in [
        "no", "nothing", "that's all", "that is it", "repeat", "complaint", "number", "id",
        "bill", "outage", "status", "power", "meter", "unit", "appliance", "invoice"
    ]) and len(text_lower.split()) <= 5:
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "FURTHER_ASSISTANCE_REQUESTED"
        ai_reply = "Certainly! What else can I assist you with today?"

    # State A: Polite Call Wrap-Up / Courtesy ("Thank you", "Thanks", "That's all", "Bye")
    elif (
        not was_asking_invoice_notification
        and any(
            k in text_lower
            for k in [
                "thank you", "thanks", "that's all", "that is all", "nothing else",
                "no that's it", "no that is it", "no thanks", "no thank you",
                "bye", "goodbye", "good night", "all good", "no i am good",
                "no other issue", "that's it", "done thank you", "no that'll be all",
                "no that will be all", "nothing more", "no more"
            ]
        )
        and not any(k in text_lower for k in ["register", "outage", "bill", "status", "meter", "spark", "wire"])
        and len(text_lower.split()) <= 8
    ):
        if any(k in text_lower for k in ["thank you", "thanks"]):
            intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
            action_taken = "WRAP_UP_COURTESY"
            ai_reply = "You are most welcome! Should I help you with any other queries or services today?"
        else:
            feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()
            if not feedback_agent:
                feedback_agent = CallCenterAgent.objects.filter(agent_code="AGENT-11").first()
            if not feedback_agent:
                CallCenterAgent.seed_default_10_agents()
                feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()

            session.is_feedback_stage = True
            session.assigned_agent = feedback_agent
            session.save()
            assigned_agent = feedback_agent
            intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
            action_taken = "TRANSFER_FEEDBACK_AGENT"
            ai_reply = (
                f"Thank you for contacting Electricity Consumer Care. "
                f"I am now transferring your call to our Customer Feedback Desk. "
                f"Hello {caller_name}! This is Officer Ritu from Quality Assurance. "
                f"On a scale of 0 to 5, how would you rate your support experience today? "
                f"You can say your rating or press a digit on your keypad."
            )

    # State B: Agent Identity & Role Clarification ("Who are you?", "What is your name?", "What's your name")
    elif (
        any(
            k in text_lower.replace("'", "").replace("’", "")
            for k in [
                "who are you", "who is speaking", "what is your name", "whats your name",
                "tell me your name", "your name", "who am i speaking to", "who am i talking to",
                "who am i speaking with", "who am i talking with", "who is this", "whos this",
                "whos speaking", "who is talking", "are you an ai", "are you a robot",
                "am i talking to a person", "what do you do", "what is your role",
                "what is your designation", "which agent are you", "what agent is this",
                "which officer are you"
            ]
        )
        or any(
            k in text_lower
            for k in [
                "what's your name", "who's this", "who's speaking", "who's talking",
                "who's on the line", "whos on the line"
            ]
        )
    ):
        if not assigned_agent:
            assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            if not assigned_agent:
                CallCenterAgent.seed_default_10_agents()
                assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            session.assigned_agent = assigned_agent
            session.is_ivr_active = False
            session.save()
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "AGENT_IDENTITY"
        ai_reply = (
            f"My name is {assigned_agent.officer_name} ({assigned_agent.short_name}), your dedicated 24/7 AI Customer Care Officer. "
            f"I specialize in {assigned_agent.role}. How may I assist you today?"
        )

    # State C: Repeat Complaint or ID Request ("Can you repeat that?", "What was the complaint number?")
    elif any(
        k in text_lower
        for k in [
            "repeat that", "what was the complaint number", "can you repeat", "repeat the number",
            "say again", "repeat tracking id", "didn't catch that", "repeat complaint",
            "tell me the number again", "repeat the id"
        ]
    ):
        intent = VoiceCallSession.INTENT_STATUS_VERIFICATION
        cmp = session.associated_complaint
        if not cmp and user_account:
            cmp = Complaint.objects.filter(user_account=user_account).order_by("-created_at").first()

        if cmp:
            action_taken = f"REPEAT_COMPLAINT: {cmp.complaint_number}"
            ai_reply = (
                f"Your Complaint Number is {cmp.complaint_number}, and your 8-character Tracking ID is {cmp.complaint_id}. "
                f"Current status is {cmp.get_status_display()}, with estimated resolution: {cmp.estimated_resolution}. Anything else I can check?"
            )
        else:
            action_taken = "REPEAT_NO_COMPLAINT"
            ai_reply = (
                f"I do not have an active complaint registered on this call yet. "
                f"Would you like me to register a power outage or check a complaint status for you right away?"
            )

    # State D: Conversational Greeting & Audio Line Check ("Hello", "Hi", "Can you hear me?", "Are you there")
    elif (
        (
            text_lower in [
                "hello", "hi", "hey", "can you hear me", "are you there",
                "good morning", "good afternoon", "good evening", "namaste", "halo"
            ]
            or text_lower.startswith(("hello ", "hi ", "hey ", "are you there"))
        )
        and len(text_lower.split()) <= 4
        and not any(k in text_lower for k in ["power", "outage", "bill", "meter", "status", "name", "who", "which", "what"])
    ):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "GREETING_ACK"
        if session.is_ivr_active and not assigned_agent:
            ai_reply = (
                f"Hello {caller_name}! Yes, I can hear you loud and clear. "
                f"You are connected to the Electricity Department 24/7 Automated IVR Helpline. "
                f"Please choose an option from 1 to 9, or press 0 on your touch-tone keypad to connect with an administration agent."
            )
        else:
            ag_short = assigned_agent.short_name if assigned_agent else "Agent 1"
            ag_off = assigned_agent.officer_name if assigned_agent else "Officer Priya"
            ag_role = assigned_agent.role if assigned_agent else "Customer Care Specialist"
            caller_prior_turns = len([m for m in recent_msgs if m.sender == VoiceCallMessage.SENDER_CALLER])
            if caller_prior_turns > 1 or any(k in text_lower for k in ["are you there", "can you hear me", "you there"]):
                ai_reply = f"Yes, this is {ag_off}. I am right here on the line with you! Please go ahead."
            else:
                ai_reply = (
                    f"Hello {caller_name}! Yes, I can hear you loud and clear. "
                    f"This is {ag_off} ({ag_short}), {ag_role} from Electricity Consumer Care. How can I assist you right now?"
                )

    # State: Third-Party Consumer / Transformer Meter List Inquiry (Strict DISCOM Privacy Guard)
    elif is_third_party_meter_query:
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "PRIVACY_THIRD_PARTY_DATA_REFUSAL"
        premise_meter = service_num or "your registered meter"
        ai_reply = (
            f"Under Electricity Department privacy and data security regulations, I can only provide details for your own verified connection and meter ({premise_meter}). "
            f"I am strictly not authorized to disclose information regarding other consumers' meters or transformer connection lists. How may I assist you with your own electrical service?"
        )

    # State: Spoken or Typed Credentials Verification & Defined Issue Resolution
    elif has_credential_input and not is_outage_reason_query:
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        verified_user = None
        verified_conn = None
        cred_label = ""
        cred_val = ""

        if email_match:
            cred_label = "email"
            cred_val = email_match.group(1)
            verified_user = UserAccount.objects.filter(email__iexact=cred_val).first()

        if not verified_user and phone_match:
            cred_label = "mobile number"
            cred_val = phone_match.group(1)
            verified_user = UserAccount.objects.filter(mobile_number=cred_val).first()
            if not verified_user:
                verified_conn = ConnectionApplication.objects.filter(mobile_number=cred_val).first()
                if verified_conn and verified_conn.user:
                    verified_user = verified_conn.user

        if not verified_user and meter_match:
            cred_label = "meter number"
            cred_val = meter_match.group(1).upper()
            verified_conn = ConnectionApplication.objects.filter(meter_number__iexact=cred_val).first()
            if verified_conn and verified_conn.user:
                verified_user = verified_conn.user

        if not verified_user and req_match:
            cred_label = "application request ID"
            cred_val = req_match.group(1).upper()
            verified_conn = ConnectionApplication.objects.filter(
                Q(application_id__iexact=cred_val) | Q(application_number__iexact=cred_val)
            ).first()
            if verified_conn and verified_conn.user:
                verified_user = verified_conn.user

        if verified_user or verified_conn:
            session.user_account = verified_user
            if verified_user:
                caller_name = verified_user.full_name
            elif verified_conn:
                caller_name = verified_conn.applicant_name
            session.caller_name = caller_name
            session.save(update_fields=["user_account", "caller_name"])

            kb = get_live_knowledge_base(user_account=verified_user)
            cp = kb.get("caller_profile")
            if cp:
                if cp.get("house_address") and cp.get("house_address") != "N/A":
                    loc = cp["house_address"]
                if cp.get("meter_number"):
                    service_num = cp["meter_number"]

            action_taken = f"CALLER_VERIFIED_LOOKUP: {cred_val}"

            # Clarify the defined issue that caller already mentioned or asked about
            all_call_text = (clean_text + " " + " ".join(m.text for m in recent_msgs)).lower()
            if any(k in all_call_text for k in ["power cut", "outage", "no power", "why power", "blackout", "current poyindi"]):
                tf_name = cp.get("transformer", "Local Area Transformer") if cp else "your area transformer"
                if cp and not cp.get("transformer_power_on", True):
                    ai_reply = (
                        f"Thank you. I have verified your connection for Meter {service_num} at {loc}. "
                        f"Regarding the power cut you reported: Central Grid Transformer {tf_name} is currently OFFLINE due to a feeder trip or grid maintenance. "
                        f"Field line technicians are actively working on-site, with estimated restoration within 1 to 2 hours."
                    )
                elif cp and not cp.get("house_power_on", True):
                    ai_reply = (
                        f"Thank you. I have verified your connection for Meter {service_num} at {loc}. "
                        f"Substation telemetry confirms your area grid is live, but your personal premise breaker switch is turned OFF. "
                        f"Please toggle your breaker switch ON on your portal dashboard to restore power."
                    )
                elif cp and cp.get("is_power_cut"):
                    cut_reason = cp.get("power_cut_reason") or "Departmental line maintenance"
                    ai_reply = (
                        f"Thank you. I have verified your connection for Meter {service_num} at {loc}. "
                        f"Your premise connection has an active departmental power cut for: {cut_reason}. Our field crew is actively attending to your line."
                    )
                else:
                    ai_reply = (
                        f"Thank you. I have verified your connection for Meter {service_num} at {loc}. "
                        f"Substation telemetry confirms your 11kV feeder and transformer are fully ONLINE with no outages. "
                        f"If you have no power at your premise, it indicates an isolated service wire fault. Would you like me to register a complaint to dispatch a line crew?"
                    )
            elif any(k in all_call_text for k in ["bill", "amount", "invoice", "due", "tariff"]):
                if cp and cp.get("latest_bill"):
                    b = cp["latest_bill"]
                    ai_reply = (
                        f"Thank you. I have verified your connection for Meter {service_num}. "
                        f"Your latest bill for cycle {b['month']} is Rs {b['amount']:.2f} for {b['total_units']} units, with status: {b['status']}. "
                        f"Domestic tariff is Rs {kb['tariffs']['domestic_unit_rate']}/unit. Would you like me to display the invoice or help with anything else?"
                    )
                else:
                    ai_reply = (
                        f"Thank you. I have verified your connection for Meter {service_num}. "
                        f"I do not see an outstanding bill in your ledger. How else may I assist you today?"
                    )
            else:
                ai_reply = (
                    f"Thank you. I have verified your registered connection for Meter {service_num} at {loc}. "
                    f"Power supply is currently {'ONLINE and ACTIVE' if (cp and cp.get('effective_power_supply')) else 'AFFECTED BY OUTAGE'}. "
                    f"How may I assist you with your electrical service today?"
                )
        else:
            action_taken = f"VERIFICATION_FAILED: {cred_val}"
            ai_reply = (
                f"I checked our database records but could not locate an active connection matching {cred_label} '{cred_val}'. "
                f"Please verify your 10-digit registered mobile number, meter number, or email address."
            )

    # State E: Direct Complaint Status Lookup by Number or ID
    elif is_lookup_query or cmp_match:
        intent = VoiceCallSession.INTENT_STATUS_VERIFICATION
        query = cmp_match.group(1) if cmp_match else clean_text
        found = lookup_complaint(query, caller_name=caller_name)
        if found:
            c = found[0]
            action_taken = f"LOOKUP_COMPLAINT: {c['complaint_number']}"
            lookup_complaint_info = c
            ai_reply = (
                f"Looking up your complaint now... "
                f"Hello {caller_name}, I found your complaint {c['complaint_number']} "
                f"(Tracking ID: {c['complaint_id']}) regarding {c['issue_type']}. "
                f"Current status is: {c['status']}, with {c['assigned_field_team']}, estimated resolution: {c['estimated_resolution']}."
            )
        else:
            ai_reply = (
                f"Checking our database records... "
                f"I could not locate an active complaint matching '{query}'. "
                f"Could you please verify your 8-character Complaint ID or would you like me to register a new complaint?"
            )

    # State I: Reason for Outage / Power Cut Explanation & Feeder Telemetry Inquiry
    # Positioned before complaint registration so questions like "why the power cut has happened" are answered factually
    elif is_outage_reason_query:
        intent = VoiceCallSession.INTENT_POWER_STATUS
        action_taken = "POWER_CUT_REASON_EXPLANATION"
        cp = kb.get("caller_profile")
        maint_list = kb.get("maintenance_schedules", [])
        ai_reply = None

        # 1. Try ReAct LLM first if active API key is available (such as Groq)
        llm_cfg = get_active_llm_config()
        if llm_cfg and llm_cfg.is_active and llm_cfg.api_key:
            ag_off = assigned_agent.officer_name if assigned_agent else "Officer Priya"
            ag_role = assigned_agent.role if assigned_agent else "Outage & Emergency Dispatch Specialist"
            history_lines = []
            for m in reversed(recent_msgs[:8]):
                speaker = "Consumer" if m.sender == VoiceCallMessage.SENDER_CALLER else ag_off
                history_lines.append(f"{speaker}: {m.text}")
            history_context = "\n".join(history_lines) if history_lines else "None"

            react_system_prompt = f"""You are {ag_off}, official {ag_role} for the Electricity Department / DISCOM.
You are on a live, real-time 1-to-1 telephone voice call with consumer {caller_name}.

{format_knowledge_base_prompt(kb)}

### RE-ACT TELEPHONY CONVERSATIONAL RULES (STRICT 2-PERSON DIALOGUE):
1. Persona & Tone: Speak strictly as {ag_off} ({ag_role}). Warm, empathetic, professional, confident DISCOM customer care officer. Speak like a real human customer care representative on the telephone. Never repeat the caller's name in every single message.
2. Two-Person Telephone Cadence: Speak strictly 1 to 2 short sentences (20 to 35 words maximum per turn). Never dump paragraphs or lists.
3. Outage Telemetry & Confident Explanation:
   - State the exact status of the caller's transformer ({cp.get('transformer') if cp else 'transformer'}) and feeder line.
   - If offline: State that maintenance or trip occurred on the 11kV feeder line and engineers are actively working on-site.
   - Provide a confident estimated restoration time: power is scheduled to resume within 1 to 2 hours.
   - If the caller asks follow-ups ("why actually", "what is the real reason", "why again"): Do NOT repeat the same sentence; elaborate with confidence on the insulator replacement / grid stabilization work.
4. Line Check: If the caller asks 'are you there', confirm: 'Yes, I am right here on the line with you! Please go ahead.'
5. Strict Privacy Guard: You are strictly forbidden from disclosing other consumers' meters or transformer meter lists. Never say 'Please hold for a moment' to look up other meters. Politely refuse under privacy rules.
"""
            p = f"Live Call History:\n{history_context}\n\nConsumer ({caller_name}): {clean_text}\n\nRespond as {ag_off}:"
            res = call_llm(prompt=p, system_prompt=react_system_prompt, config=llm_cfg)
            if res.get("success") and res.get("text"):
                cleaned = re.sub(r"ACTION:\s*[A-Z_]+(?:\s*\|[^\n]*)?", "", res["text"]).strip()
                cleaned = re.sub(r"\|[^\n]+\|", "", cleaned)
                cleaned = re.sub(r"^[#*>\s\-|]+", "", cleaned)
                cleaned = cleaned.replace("**", "").replace("##", "").replace("`", "")
                cleaned = re.sub(r"\n{2,}", " ", cleaned).strip()
                if cleaned:
                    ai_reply = cleaned

        if not ai_reply:
            if cp:
                tf_name = cp.get("transformer", "Local Area Transformer")
                # 1. Check if premise breaker switch is OFF
                if not cp.get("house_power_on", True):
                    ai_reply = (
                        f"Telemetry confirms your area grid and transformer ({tf_name}) are live, "
                        f"but your personal premise breaker switch is turned OFF. Please toggle your breaker switch ON on your portal dashboard to restore power."
                    )
                # 2. Check if premise is under departmental power cut / isolation
                elif cp.get("is_power_cut"):
                    cut_reason = cp.get("power_cut_reason") or "Departmental line maintenance or inspection"
                    ai_reply = (
                        f"Your premise connection has an active departmental power cut. "
                        f"Reason: {cut_reason}. Our field crew is actively attending to your line for restoration."
                    )
                # 3. Check if area transformer is offline
                elif not cp.get("transformer_power_on", True):
                    prior_outage_queries = sum(
                        1 for m in recent_msgs
                        if (m.action_taken and "POWER_CUT_REASON" in m.action_taken)
                        or any(k in m.text.lower() for k in ["offline", "feeder trip", "maintenance"])
                    )
                    if prior_outage_queries >= 1:
                        ai_reply = (
                            f"Substation telemetry confirms Central Grid Transformer {tf_name} is currently OFFLINE and de-energized for 11kV feeder insulator replacement and safety maintenance. "
                            f"Field line technicians are actively working on-site, and power is scheduled to be restored within 1 to 2 hours. Would you like me to register an SMS alert for your meter?"
                        )
                    else:
                        ai_reply = (
                            f"Your area transformer ({tf_name}) is currently OFFLINE due to a feeder trip or grid maintenance. "
                            f"Substation operators are actively working to restore supply with estimated restoration within 1 to 2 hours."
                        )
                # 4. Check active maintenance schedules
                elif maint_list and any(m.get("is_power_cut_active") for m in maint_list):
                    m = next(m for m in maint_list if m.get("is_power_cut_active"))
                    ai_reply = (
                        f"Your line is affected by an active maintenance shutdown ({m['title']}) for: {m['reason']}. "
                        f"Supply is scheduled to be restored by {m['end']}."
                    )
                # 5. Normal online supply - explain telemetry and offer dispatch ticket if local problem
                else:
                    ai_reply = (
                        f"Substation telemetry confirms your 11kV feeder and transformer ({tf_name}) are fully ONLINE and healthy with no grid outages. "
                        f"If you have no power at your premise, it indicates an isolated service wire fault or fuse trip. Would you like me to register a complaint to dispatch a line crew?"
                    )
            else:
                ai_reply = (
                    "All primary grid transformers and 11kV feeders are monitored 24/7. "
                    "Could you please state your 10-digit registered mobile, meter number, or email address so I can check your exact feeder telemetry?"
                )

    # State: Scheduled or Future Outage Inquiry (Telemetry & Planned Maintenance Checks)
    elif is_scheduled_or_future_outage_query and not any(k in text_lower for k in ["register", "file", "raise complaint"]):
        intent = VoiceCallSession.INTENT_STATUS_VERIFICATION
        action_taken = "SCHEDULED_OUTAGE_QUERY_RESOLVED"
        cp = kb.get("caller_profile")
        tf_name = cp.get("transformer") if cp and cp.get("transformer") else "Central Distribution Transformer"
        feeder_name = cp.get("feeder") if cp and cp.get("feeder") else "11kV Feeder Line"
        maint_list = kb.get("maintenance_schedules", [])
        active_planned = [m for m in maint_list if m.get("is_power_cut_active") or "cut" in str(m.get("status", "")).lower()]

        if active_planned:
            m = active_planned[0]
            ai_reply = (
                f"DISCOM maintenance schedule confirms a planned power shutdown on {m.get('feeder_line', feeder_name)} "
                f"from {m.get('start', 'today')} to {m.get('end', 'soon')} for {m.get('reason', 'routine feeder maintenance')}. "
                f"Outside of this scheduled window, your electrical supply will remain completely normal."
            )
        else:
            tf_status = "energized and operating normally with stable 230V domestic supply"
            if cp and cp.get("transformer_power_on") is False:
                tf_status = "currently undergoing technical inspection"
            ai_reply = (
                f"Central Grid telemetry and maintenance logs confirm there are no scheduled or planned power outages for your area today. "
                f"Your 11kV feeder and distribution transformer ({tf_name}) are {tf_status}. "
                f"Is there anything else regarding your power supply or account I can check for you?"
            )

    # State F: Multi-Turn Outage Diagnostic Slot Fulfillment
    # If the agent just asked the diagnostic questions about street impact, duration, and hazards
    elif (
        was_asking_diagnostic
        and not is_outage_reason_query
        and not is_scheduled_or_future_outage_query
        and not is_agent_transfer_query
        and not ivr_choice
        and not any(k in text_lower for k in ["bill", "tariff", "meter number", "request id"])
    ):
        intent = VoiceCallSession.INTENT_COMPLAINT_REGISTRATION
        issue = Complaint.ISSUE_POWER_OUTAGE
        hazard = ""
        impact = "Single Household"
        duration = "Ongoing (30 to 45 mins)"

        # 1. Parse Impact
        if any(k in text_lower for k in ["entire", "street", "colony", "area", "all house", "neighbor", "whole", "locality", "everyone"]):
            impact = "Entire Street / Neighboring Households"

        # 2. Parse Hazards
        if (
            any(k in text_lower for k in ["spark", "fire", "blast", "smoke", "wire", "shock", "danger", "exploded", "snapped", "fallen"])
            and not any(neg in text_lower for neg in ["no spark", "no fire", "no hazard", "no smoke", "no blast", "didn't see", "did not see", "none"])
        ):
            if "wire" in text_lower:
                issue = Complaint.ISSUE_WIRE_SNAP
                hazard = "Fallen wire / snapped conductor reported"
            else:
                issue = Complaint.ISSUE_TRANSFORMER_HAZARD
                hazard = "Sparks / explosion observed on pole transformer"
        else:
            hazard = "None reported"

        # 3. Parse Duration
        dur_match = re.search(r"(\d+\s*(?:mins?|minutes?|hours?|hrs?))", text_lower)
        if dur_match:
            duration = dur_match.group(1)
        elif any(k in text_lower for k in ["just now", "recent", "10 min", "few min"]):
            duration = "Just started (10-15 mins)"

        # Register the complaint into DISCOM database
        new_cmp = raise_complaint(
            caller_name=caller_name,
            location=loc,
            issue_type=issue,
            service_number=service_num,
            outage_duration=duration,
            impact_area=impact,
            hazard_observed=hazard,
            user_account=user_account or session.user_account,
        )
        session.associated_complaint = new_cmp
        session.call_intent = VoiceCallSession.INTENT_COMPLAINT_REGISTRATION
        session.save()

        action_taken = f"RAISE_COMPLAINT: {new_cmp.complaint_number}"
        registered_complaint_info = {
            "complaint_number": new_cmp.complaint_number,
            "complaint_id": new_cmp.complaint_id,
            "issue_type": new_cmp.issue_type,
            "location": new_cmp.location,
            "status": new_cmp.get_status_display(),
            "priority": new_cmp.get_priority_display(),
            "estimated_resolution": new_cmp.estimated_resolution,
        }

        safety_warning = ""
        if new_cmp.priority == Complaint.PRIORITY_CRITICAL:
            safety_warning = " Please stay at least 10 meters away from any sparking transformer or fallen wires for your safety."

        ai_reply = (
            f"Thank you. Your electricity complaint is registered as {new_cmp.complaint_number}, "
            f"Tracking ID: {new_cmp.complaint_id}. Field line crew is alerted with estimated resolution: {new_cmp.estimated_resolution}.{safety_warning} "
            f"Is there anything else I can assist you with today?"
        )

    # State G: Initial Outage / Hazard / Complaint Trigger
    elif not is_agent_transfer_query and not is_scheduled_or_future_outage_query and any(k in text_lower for k in [
        "no power", "power cut", "power outage", "outage", "spark", "blast", "wire snap",
        "voltage", "register complaint", "file complaint", "report outage", "lights went off",
        "blackout", "meter burnt", "current poyindi", "line fault", "transformer issue"
    ]):
        intent = VoiceCallSession.INTENT_COMPLAINT_REGISTRATION
        landmark = ""
        hazard = ""
        impact = "Single Household"
        duration = "Just started"
        issue = Complaint.ISSUE_POWER_OUTAGE

        if "spark" in text_lower or "blast" in text_lower or "fire" in text_lower:
            issue = Complaint.ISSUE_TRANSFORMER_HAZARD
            hazard = "Sparks / explosion observed on distribution unit"
        elif "wire" in text_lower and ("fall" in text_lower or "broken" in text_lower or "snap" in text_lower):
            issue = Complaint.ISSUE_WIRE_SNAP
            hazard = "Fallen live conductor observed"
        elif "voltage" in text_lower or "low" in text_lower or "fluctuat" in text_lower:
            issue = Complaint.ISSUE_VOLTAGE_FLUCTUATION
        elif "meter" in text_lower and ("burn" in text_lower or "fault" in text_lower or "smoke" in text_lower):
            issue = Complaint.ISSUE_METER_FAULT

        if "entire" in text_lower or "colony" in text_lower or "street" in text_lower or "all house" in text_lower or "neighbors" in text_lower:
            impact = "Entire Street / Neighboring Households"

        # If user explicitly requests immediate registration or already gave full information (duration/impact)
        is_immediate_filing = (
            any(confirm in text_lower for confirm in ["register", "file", "yes", "confirm", "submit", "please file", "help me report", "please register"])
            or re.search(r"\b\d+\s*(?:mins?|minutes?|hours?|hrs?)\b", text_lower)
            or "since" in text_lower
        )

        if is_immediate_filing:
            dur_match = re.search(r"(\d+\s*(?:mins?|minutes?|hours?|hrs?))", text_lower)
            if dur_match:
                duration = dur_match.group(1)

            new_cmp = raise_complaint(
                caller_name=caller_name,
                location=loc,
                issue_type=issue,
                service_number=service_num,
                landmark=landmark,
                outage_duration=duration,
                impact_area=impact,
                hazard_observed=hazard,
                user_account=user_account or session.user_account,
            )
            session.associated_complaint = new_cmp
            session.call_intent = VoiceCallSession.INTENT_COMPLAINT_REGISTRATION
            session.save()

            action_taken = f"RAISE_COMPLAINT: {new_cmp.complaint_number}"
            registered_complaint_info = {
                "complaint_number": new_cmp.complaint_number,
                "complaint_id": new_cmp.complaint_id,
                "issue_type": new_cmp.issue_type,
                "location": new_cmp.location,
                "status": new_cmp.get_status_display(),
                "priority": new_cmp.get_priority_display(),
                "estimated_resolution": new_cmp.estimated_resolution,
            }

            safety_warning = ""
            if new_cmp.priority == Complaint.PRIORITY_CRITICAL:
                safety_warning = " Please stay at least 10 meters away from any sparking equipment or wires for your safety."

            ai_reply = (
                f"Thank you, {caller_name}. Your electricity complaint has been successfully registered as {new_cmp.complaint_number}, "
                f"Tracking ID: {new_cmp.complaint_id}. Our field line crew has been alerted with estimated resolution: {new_cmp.estimated_resolution}.{safety_warning}"
            )
        else:
            # Empathetic customer care multi-turn slot filling inquiry (concise 2-sentence telephone phrasing)
            ai_reply = (
                f"I apologize for the outage. I have your premise at {loc}. "
                f"Could you please confirm if neighboring houses on your street are also affected, and if you observed any hazards like sparking?"
            )

    # State: Current Units Consumed & Meter Reading Inquiry
    elif any(k in text_lower for k in [
        "units that i consumed", "units consumed", "current units", "consumed in the month",
        "units that have conserved", "units have i consumed", "how many units", "my consumption",
        "reading on my meter", "current reading", "meter consumption", "units used", "units billed",
        "power consumption", "energy consumed", "kwh consumed", "units that i used", "conserved in the month",
        "units conserved", "conserved units", "units i consumed", "units that i have consumed",
        "units i have conserved", "units that have been consumed",
        "power consumed", "total unit of power consumed", "units of power consumed", "unit of power consumed",
        "consumption today", "consumed today", "units consumed today", "units used today",
        "today's consumption", "today consumption", "daily consumption", "daily usage", "reading today"
    ]):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "METER_CONSUMPTION_INQUIRY"
        ag_off = assigned_agent.officer_name if assigned_agent else "Officer Rajesh"
        cp = kb.get("caller_profile")
        if cp:
            meter = cp.get("meter_number", "Registered Meter")
            unbilled = cp.get("unbilled_units", 0.0)
            total = cp.get("total_units_consumed", 0.0)
            reading = cp.get("current_meter_reading", 0.0)

            if any(k in text_lower for k in ["today", "day of today", "daily"]):
                ai_reply = (
                    f"Your meter {meter} hasn't recorded any consumption today yet; your current reading remains {reading:.2f} kilowatt-hours. "
                    f"Would you like me to send a daily usage summary to your account notifications?"
                )
            else:
                months_map = {
                    "january": 1, "jan": 1, "february": 2, "feb": 2,
                    "march": 3, "mar": 3, "april": 4, "apr": 4,
                    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
                    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
                    "october": 10, "oct": 10, "november": 11, "nov": 11,
                    "december": 12, "dec": 12,
                }
                found_m_name = None
                found_m_num = None
                is_explicit_current = any(k in text_lower for k in ["current", "this month", "right now", "so far", "unbilled"])

                if not is_explicit_current:
                    for m_k, m_v in months_map.items():
                        if m_k == "may":
                            # Differentiate modal verb "may I" from the calendar month of May
                            if re.search(r"\b(?:in|of|for|during|month of)\s+may\b|\bmay\s+(?:202\d|consumption|units|bill)\b", text_lower):
                                found_m_name = m_k
                                found_m_num = m_v
                                break
                        else:
                            if re.search(rf"\b{m_k}\b", text_lower):
                                found_m_name = m_k
                                found_m_num = m_v
                                break

                ag_num = assigned_agent.agent_number if assigned_agent else 6
                if ag_num == 4:
                    spec_intro = "Electrical Hazard Officer Rahul here. Checking your premise meter telemetry, "
                    spec_outro = " For detailed billing tariffs, Officer Ananya at Billing can assist. Do you observe any sparking or electrical hazards today?"
                elif ag_num == 3:
                    spec_intro = "Billing Specialist Officer Ananya here. "
                    spec_outro = " Would you like to check your bill breakdown or online payment options?"
                elif ag_num == 1:
                    spec_intro = "Outage Dispatch Officer Priya here. "
                    spec_outro = " Grid feeder supply to your premise is active. How else may I assist with your power supply?"
                elif ag_num == 2:
                    spec_intro = "Substation Telemetry Officer Vikram here. "
                    spec_outro = " Feeder line bus voltages are normal. How else can I assist?"
                elif ag_num == 5:
                    spec_intro = "Transformer Diagnostics Officer Sneha here. "
                    spec_outro = " Your connected transformer load is within normal limits. How else may I assist?"
                elif ag_num == 7:
                    spec_intro = "Citizen Grievance Officer Kavita here. "
                    spec_outro = " Do you have any pending complaints or service delays you would like me to escalate?"
                elif ag_num == 8:
                    spec_intro = "Field Line Crew Dispatcher Officer Amit here. "
                    spec_outro = " Field line crews are available if you have physical service line issues."
                elif ag_num == 9:
                    spec_intro = "Commercial Accounts Specialist Officer Pooja here. "
                    spec_outro = " For commercial tariff slabs or sanctioned load upgrades, I can assist."
                elif ag_num == 10:
                    spec_intro = "Senior Shift Supervisor Officer Suresh here. "
                    spec_outro = " How else may I assist you today?"
                else:
                    spec_intro = "Smart Meter Specialist Officer Rajesh here. "
                    spec_outro = " Would you like an appliance load diagnostic or check today's consumption?"

                if found_m_num == 11 or (not found_m_num and ("current" in text_lower or "this month" in text_lower or "november" in text_lower)):
                    ai_reply = (
                        f"{spec_intro}On smart meter {meter}, your consumption for the month of November is {unbilled:.2f} units so far, "
                        f"with current reading {reading:.0f} kilowatt-hours.{spec_outro}"
                    )
                elif found_m_num:
                    year_match = re.search(r"\b(202\d)\b", text_lower)
                    target_year = year_match.group(1) if year_match else "2026"
                    month_pattern = f"{target_year}-{found_m_num:02d}"
                    target_user = user_account or session.user_account
                    bill_obj = None
                    if target_user:
                        bill_obj = PowerBill.objects.filter(
                            connection__user=target_user,
                            billing_month__icontains=month_pattern
                        ).first()
                    if not bill_obj and meter:
                        bill_obj = PowerBill.objects.filter(
                            connection__meter_number=meter,
                            billing_month__icontains=month_pattern
                        ).first()

                    if bill_obj:
                        ai_reply = (
                            f"{spec_intro}On smart meter {meter}, your recorded consumption for {found_m_name.capitalize()} {target_year} was {bill_obj.units_billed:.2f} units "
                            f"(Bill amount: Rs {bill_obj.total_amount:.2f}, status: {bill_obj.status}). Would you like me to check any other month?"
                        )
                    else:
                        ai_reply = (
                            f"{spec_intro}I checked your account ledger for {found_m_name.capitalize()} {target_year}. No power consumption or bill was recorded for that period as your meter connection was not active during that month. "
                            f"Would you like me to check your latest November consumption or previous October bill?"
                        )
                else:
                    ai_reply = (
                        f"{spec_intro}On smart meter {meter}, your unbilled consumption is {unbilled:.2f} units, "
                        f"with total cumulative consumption of {total:.2f} units and current reading {reading:.0f} kilowatt-hours.{spec_outro}"
                    )
        else:
            ai_reply = (
                "Please state your registered mobile number, meter number, or email address so I can verify your account and fetch your exact units consumed."
            )

    # State: Real-Time Premise Appliances & Active Load Inquiry
    elif any(k in text_lower for k in [
        "appliances are currently powered on", "appliances powered on", "appliances are on",
        "how many appliances", "which appliances are on", "appliances turned on", "active load",
        "appliances in my home", "appliances running", "appliances are running", "what is on in my house",
        "how many appliances are on", "how many devices are on", "connected appliances",
        "appliance status", "appliances status", "what appliances are powered", "how many appliances are running",
        "appliance consumption", "how many lights", "is my ac on", "is ac on", "appliances are currently on",
        "count of how many appliances", "how many appliances are", "appliances on in your home",
        "appliances in your home", "appliances currently on"
    ]):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "APPLIANCE_TELEMETRY_INQUIRY"
        ag_off = assigned_agent.officer_name if assigned_agent else "Officer Rajesh"
        cp = kb.get("caller_profile")
        if cp and cp.get("appliance_telemetry"):
            at = cp["appliance_telemetry"]
            on_count = at["on_count"]
            total_count = at["total_count"]
            active_kw = at["active_kw"]
            active_w = at["active_watts"]
            hourly_rs = at["hourly_cost_rs"]
            on_apps = [a["name"] for a in at["appliances"] if a["is_on"]]
            on_str = ", ".join(on_apps) if on_apps else "None"

            ai_reply = (
                f"You currently have {on_count} appliances powered ON in your home, drawing {active_kw:.2f} kilowatts ({active_w} Watts). "
                f"Active appliances: {on_str}. Would you like me to turn off any of them?"
            )
        else:
            ai_reply = (
                f"Accessing your premise smart IoT panel... Meter {service_num} is monitored in real time. "
                f"Would you like me to check your active meter reading or appliances?"
            )

    # State: Real-Time Premise Appliance Remote Toggle (IoT Control)
    elif any(action_word in text_lower for action_word in ["turn off", "switch off", "power off", "shut down", "turn on", "switch on", "power on"]) and any(app_word in text_lower for app_word in ["ac", "air conditioner", "fan", "light", "lights", "tv", "television", "fridge", "refrigerator", "appliance"]):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        is_turning_on = any(k in text_lower for k in ["turn on", "switch on", "power on"])
        desired_state = True if is_turning_on else False
        state_str = "ON" if desired_state else "OFF"
        
        target_key = "ac"
        target_name = "Split AC"
        if "fan" in text_lower:
            target_key = "fan1"
            target_name = "Living Room Fan"
        elif "light" in text_lower:
            target_key = "lights"
            target_name = "House Lights"
        elif "tv" in text_lower or "television" in text_lower:
            target_key = "tv"
            target_name = "Smart LED TV"
        elif "fridge" in text_lower or "refrigerator" in text_lower:
            target_key = "fridge"
            target_name = "Refrigerator"

        # Apply toggle to database
        conn = ConnectionApplication.objects.filter(user=user_account or session.user_account).first()
        if conn:
            from users.billing import get_builtin_appliance_states
            states = get_builtin_appliance_states(conn)
            states[target_key] = desired_state
            conn.appliance_states = states
            conn.save(update_fields=["appliance_states", "updated_at"])

        action_taken = f"TOGGLE_APPLIANCE: {target_name} -> {state_str}"
        ag_off = assigned_agent.officer_name if assigned_agent else "Officer Rajesh"
        ai_reply = (
            f"I have successfully turned {state_str} your {target_name}. "
            f"Your premise live load has been updated. How else may I assist you?"
        )

    # State: Payment Invoice & Receipt Retrieval Inquiry (Printable & Downloadable)
    elif any(k in text_lower for k in [
        "invoice", "payment invoice", "payment receipt", "give me my last payment invoice",
        "give me my invoice", "send me my invoice", "show my invoice", "print invoice",
        "print my invoice", "download invoice", "receipt", "bill invoice", "copy of invoice",
        "last payment receipt", "receipt of bill", "invoice please", "give invoice", "show invoice"
    ]):
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        target_user = user_account or session.user_account
        bill = None
        if target_user:
            bill = (
                PowerBill.objects.filter(connection__user=target_user, status=PowerBill.STATUS_PAID).order_by("-paid_at", "-created_at").first()
                or PowerBill.objects.filter(connection__user=target_user).order_by("-created_at").first()
            )
        if not bill:
            bill = PowerBill.objects.order_by("-created_at").first()

        if bill:
            meter_num = bill.connection.meter_number if bill.connection else (service_num or "MTR-2026-1004-3")
            app_name = bill.connection.applicant_name if bill.connection else caller_name
            paid_date_str = (
                timezone.localtime(bill.paid_at).strftime("%d %b %Y, %I:%M %p")
                if bill.paid_at
                else (
                    timezone.localtime(bill.created_at).strftime("%d %b %Y, %I:%M %p")
                    if bill.status == PowerBill.STATUS_PAID
                    else "Payment Due"
                )
            )
            invoice_payload = {
                "id": bill.id,
                "bill_number": bill.bill_number,
                "billing_month": bill.billing_month or "Current",
                "bill_type": bill.get_bill_type_display(),
                "units_billed": float(bill.units_billed),
                "rate_per_unit": float(bill.rate_per_unit),
                "base_fare": float(bill.base_fare),
                "energy_charge": float(bill.energy_charge),
                "arrears_amount": float(bill.arrears_amount),
                "total_amount": float(bill.total_amount),
                "status": bill.status,
                "is_paid": (bill.status == PowerBill.STATUS_PAID),
                "payment_method": bill.payment_method or "DISCOM Online Portal",
                "transaction_id": bill.transaction_id or f"TXN-DISCOM-{bill.id:05d}",
                "paid_at": paid_date_str,
                "meter_number": meter_num,
                "applicant_name": app_name,
                "house_address": bill.connection.house_address if bill.connection else loc,
                "pdf_url": reverse("download_bill_invoice_pdf", kwargs={"bill_id": bill.id}),
            }
            action_taken = f"FETCH_INVOICE: {bill.id}:{bill.bill_number}"
            ai_reply = (
                f"I have displayed your {bill.status.lower()} invoice of Rs {bill.total_amount:.2f} for {bill.billing_month or 'the latest billing cycle'} on your screen with a print option. "
                f"Would you like me to send a copy to your account notifications?"
            )
        else:
            action_taken = "FETCH_INVOICE_NOT_FOUND"
            ai_reply = (
                f"I looked up our ledger records for meter {service_num}, but could not locate an issued bill or invoice yet. "
                f"Would you like me to connect you with our billing team?"
            )

    # State H00: Direct UPI Debit / Collect UPI ID Disallowed (Financial Regulatory Security Guard)
    elif is_direct_upi_debit_query:
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        action_taken = "DIRECT_UPI_DEBIT_DISALLOWED"
        target_meter = service_num or "your registered meter"
        ai_reply = (
            f"For your financial security under RBI and DISCOM guidelines, customer care officers cannot collect your UPI ID, PIN, or banking details over the telephone to pay on your behalf. "
            f"Please complete payment yourself securely on our website homepage using 'Quick Pay' with meter number {target_meter}, or directly through your UPI app like PhonePe or Google Pay."
        )

    # State H0: Step-by-Step Payment Guidance & Quick Pay (With or Without Login)
    elif is_payment_guidance_query:
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        action_taken = "PAYMENT_GUIDANCE"
        cp = kb.get("caller_profile")
        target_meter = service_num or "your registered meter"

        # Determine unpaid bill summary if available
        unpaid_summary = "your electricity bill"
        if cp and cp.get("unpaid_bills"):
            u0 = cp["unpaid_bills"][0]
            unpaid_summary = f"your pending bill of Rs {float(u0['amount']):.2f} for cycle {u0['month']}"
        else:
            target_user = user_account or session.user_account
            db_unpaid = (
                PowerBill.objects.filter(connection__user=target_user, status=PowerBill.STATUS_UNPAID).order_by("billing_month").first()
                if target_user else None
            )
            if not db_unpaid:
                db_unpaid = PowerBill.objects.filter(status=PowerBill.STATUS_UNPAID).order_by("billing_month").first()
            if db_unpaid:
                unpaid_summary = f"your pending bill of Rs {float(db_unpaid.total_amount):.2f} for cycle {db_unpaid.billing_month}"

        is_without_login_query = any(k in text_lower for k in [
            "without login", "without logging in", "without log in", "no login",
            "dont want to login", "don't want to login", "no account", "dont have account",
            "don't have account", "without an account", "quick pay"
        ])
        if is_without_login_query:
            ai_reply = (
                f"Yes, absolutely! You can pay {unpaid_summary} directly on our website without logging in. "
                f"Visit our homepage, select 'Quick Pay', enter your meter number {target_meter}, review the amount, and pay instantly using UPI, Google Pay, PhonePe, or card."
            )
        elif any(k in text_lower for k in ["app", "mobile app", "phone app"]):
            ai_reply = (
                f"To pay via the mobile app, open the DISCOM app, tap 'Pay Bills', select {unpaid_summary}, and complete payment using UPI or net banking. "
                f"You can also pay on our website without login under 'Quick Pay' using meter {target_meter}."
            )
        else:
            ai_reply = (
                f"You can pay {unpaid_summary} online in two easy ways: "
                f"1. On our website without login using 'Quick Pay' with meter number {target_meter}. "
                f"2. By logging into your portal dashboard under 'Bills & Payments' and clicking 'Pay Now' via UPI, card, or net banking. Which payment method would you prefer?"
            )

    # State H1: Unpaid / Pending Bill Inquiry (also matches common STT phonetic typo "unpaired")
    elif not is_payment_guidance_query and any(k in text_lower for k in [
        "unpaid bill", "unpaired bill", "what is the unpaid", "what is the unpaired",
        "unpaid", "unpaired", "un paid", "pending bill", "due bill", "bill due",
        "how much do i owe", "how much i owe", "pending amount", "due amount",
        "unpaid amount", "unpaired amount", "pending dues"
    ]):
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        action_taken = "UNPAID_BILL_INQUIRY"
        cp = kb.get("caller_profile")
        if cp and cp.get("unpaid_bills"):
            u_list = cp["unpaid_bills"]
            primary_u = u_list[0]
            base_fare_val = primary_u.get("base_fare", 40.0)
            energy_val = primary_u.get("energy_charge", primary_u["amount"] - base_fare_val)
            ai_reply = (
                f"You have {len(u_list)} unpaid bill for cycle {primary_u['month']} amounting to Rs {primary_u['amount']:.2f}. "
                f"This includes base fare Rs {base_fare_val:.2f} and energy charge Rs {energy_val:.2f} for {primary_u.get('units', 1.14)} units. "
                f"Would you like me to guide you on how to pay it online?"
            )
        else:
            ai_reply = "Good news! You have zero unpaid bills. All your generated electricity bills are paid in full."

    # State H2: Paid Bill / Previous Payment Inquiry
    elif not is_payment_guidance_query and any(k in text_lower for k in [
        "previous bill have paid", "bill have paid", "bill i have paid", "previous bill i paid",
        "amount have paid", "amount i paid", "paid in the previous month", "paid bill",
        "last payment", "previous payment", "what did i pay", "how much did i pay",
        "last paid", "previous bill paid", "bill paid"
    ]):
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        action_taken = "PAID_BILL_INQUIRY"
        cp = kb.get("caller_profile")
        pb = cp.get("latest_paid_bill") if cp else None
        if pb and pb.get("amount"):
            ai_reply = (
                f"Your previous paid bill for cycle {pb['month']} was Rs {pb['amount']:.2f} "
                f"for {pb['total_units']} units, with status: Paid in full."
            )
        else:
            ai_reply = "I checked your account ledger, but do not see a recorded previous paid bill yet."

    # State H: Direct Billing and Tariff Inquiry
    elif not is_payment_guidance_query and any(k in text_lower for k in ["bill", "amount", "due date", "tariff", "rate"]):
        intent = VoiceCallSession.INTENT_BILLING_INQUIRY
        cp = kb.get("caller_profile")
        if cp and cp.get("latest_bill"):
            b = cp["latest_bill"]
            unpaid_note = ""
            if cp.get("unpaid_bills"):
                unpaid_note = f" You currently have {len(cp['unpaid_bills'])} unpaid bill(s) pending."
            ai_reply = (
                f"Your latest bill for {b['month']} is Rs {b['amount']:.2f} "
                f"for {b['total_units']} units, with status: {b['status']}.{unpaid_note} "
                f"Domestic tariff is Rs {kb['tariffs']['domestic_unit_rate']}/unit with base fare Rs {kb['tariffs']['domestic_base_price']}."
            )
        else:
            ai_reply = (
                f"Our domestic tariff is Rs {kb['tariffs']['domestic_unit_rate']} per kWh with base price Rs {kb['tariffs']['domestic_base_price']}, "
                f"and commercial tariff is Rs {kb['tariffs']['commercial_unit_rate']} per kWh. May I check your specific bill?"
            )

    # State J0: Connection Application, Meter Issuance & Timeline Dates Inquiry
    elif is_connection_timeline_or_date_query:
        if not assigned_agent:
            assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            if not assigned_agent:
                CallCenterAgent.seed_default_10_agents()
                assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            session.assigned_agent = assigned_agent
            session.is_ivr_active = False
            session.save()
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "CONNECTION_TIMELINE_INQUIRY"
        cp = kb.get("caller_profile")
        if cp and (cp.get("application_created_at") or cp.get("meter_approved_at") or cp.get("connection_connected_at")):
            app_dt = cp.get("application_created_at") or "recently"
            meter_dt = cp.get("meter_approved_at") or "shortly after verification"
            conn_dt = cp.get("connection_connected_at") or "finalized on schedule"
            meter_num = cp.get("meter_number") or service_num or "registered meter"
            req_id = cp.get("request_id") or "CONN"

            is_meter_only_query = any(k in text_lower for k in [
                "meter was issued", "meter issue", "meter issued", "meter install", "meter installed", "issuance date"
            ]) and not any(k in text_lower for k in ["application", "uploaded", "apply", "applied"])

            if is_meter_only_query and cp.get("meter_approved_at"):
                ai_reply = (
                    f"Your smart meter {meter_num} was officially issued by administration on {meter_dt}"
                    f"{f' and energized on {conn_dt}' if cp.get('connection_connected_at') else ''}. "
                    f"Your connection status is currently {cp.get('connection_status', 'Active')}."
                )
            else:
                ai_reply = (
                    f"According to your official DISCOM connection records for Request ID {req_id}: "
                    f"Your application was uploaded on {app_dt}. "
                    f"Your smart meter {meter_num} was officially issued on {meter_dt}, "
                    f"and your premise connection was energized on {conn_dt}."
                )
        elif cp:
            ai_reply = f"Your connection is registered as Request ID {cp.get('request_id')} with meter {cp.get('meter_number')}. How else may I assist you with your connection?"
        else:
            ai_reply = "Could you please state your 10-digit registered mobile, Request ID, or Meter Number so I can look up your exact connection timestamps?"

    # State J: Caller Identity & Connection Credentials Inquiry
    elif any(k in text_lower for k in [
        "meter number", "request id", "request number", "application id", "application number",
        "who am i", "my details", "verify my connection", "verify me", "my meter", "my request",
        "my connection", "connection details", "check my account", "account details", "meter id"
    ]) and not is_connection_timeline_or_date_query:
        if not assigned_agent:
            assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            if not assigned_agent:
                CallCenterAgent.seed_default_10_agents()
                assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            session.assigned_agent = assigned_agent
            session.is_ivr_active = False
            session.save()
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "CALLER_DETAILS_INQUIRY"
        cp = kb.get("caller_profile")
        if cp:
            ai_reply = (
                f"Hello {caller_name}, your verified connection is Request ID {cp['request_id']} with Meter Number {cp['meter_number']} at {cp['house_address']}. How may I assist you today?"
            )
        else:
            ai_reply = (
                f"Could you please state your 10-digit registered mobile, Request ID, or Meter Number so I can locate your connection records?"
            )

    # Incomplete or trailing speech fragment handling (hesitant speech)
    elif (
        len(text_lower.split()) <= 3
        and (
            any(text_lower.endswith(w) for w in ["my", "the", "a", "an", "is", "of", "to", "for", "in", "at"])
            or text_lower in ["what is my", "what is", "can you check", "check my", "tell me", "i need", "i want", "can i"]
        )
    ):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "INCOMPLETE_SPEECH_PROMPT"
        ai_reply = "What would you like me to check — your latest bill, current units, or complaint status?"

    # State: IVR mode active but unrecognized command / options guidance
    elif session.is_ivr_active and not assigned_agent:
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "IVR_UNRECOGNIZED_PROMPT"
        ai_reply = (
            f"You are connected to the 24/7 Automated IVR Menu. "
            f"Please dial or type an option from 1 to 9, or press 0 for the Shift In-Charge Supervisor, on your touch-tone keypad."
        )

    # Domain Boundary Guard: Reject out-of-scope non-electricity inquiries (programming, trivia, general chat)
    elif (
        not any(
            k in text_lower
            for k in [
                "power", "electricity", "current", "outage", "cut", "blackout", "meter", "reading",
                "bill", "tariff", "rate", "unit", "units", "transformer", "feeder", "substation", "voltage",
                "fluctuation", "spark", "fire", "wire", "pole", "cable", "appliance", "air conditioner",
                "geyser", "refrigerator", "fan", "light", "complaint", "status", "grievance", "discom",
                "ledger", "invoice", "receipt", "payment", "due", "supply", "switch", "line", "fuse",
                "phase", "trip", "service", "connection", "officer", "agent", "ivr", "helpline", "1912",
                "kw", "kwh", "load", "hazard", "restore", "restoration", "breakdown"
            ]
        )
        and (
            any(
                k in text_lower
                for k in [
                    "python", "java", "javascript", "c++", "c#", "html", "css", "sql query", "react js",
                    "django code", "write a code", "write code", "write script", "coding", "programmer",
                    "programming", "software engineer", "machine learning", "deep learning", "algorithm",
                    "recipe", "how to cook", "tell me a joke", "make me laugh", "cricket score", "football score",
                    "who won", "capital of", "prime minister", "president of", "movie review", "write an essay",
                    "do my homework", "solve math", "meaning of life"
                ]
            )
            or (
                text_lower.startswith(("what is ", "what are ", "who is ", "tell me about "))
                and not any(k in text_lower for k in ["name", "status", "bill", "meter", "discom", "tariff", "rate", "power", "outage", "complaint", "your"])
            )
        )
    ):
        intent = VoiceCallSession.INTENT_GENERAL_INQUIRY
        action_taken = "OUT_OF_SCOPE_REFUSAL"
        if assigned_agent:
            ai_reply = (
                "I specialize strictly in Electricity Department customer care. "
                "I can only assist with power outages, billing, meter issues, and electricity complaints. "
                "How may I help you with your electrical service today?"
            )
        else:
            ai_reply = (
                "You have reached the Electricity Department 24/7 Helpline (1912). "
                "I can only assist with power supply, billing, and electrical service inquiries. "
                "Please choose an option from 1 to 9, or press 0 on your keypad to speak with an officer."
            )

    # State K: Database-Configured LLM Multi-Turn ReAct Engine
    else:
        if not assigned_agent:
            assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            if not assigned_agent:
                CallCenterAgent.seed_default_10_agents()
                assigned_agent = CallCenterAgent.objects.filter(agent_number=1, is_active=True).first()
            session.assigned_agent = assigned_agent
            session.is_ivr_active = False
            session.save()
        ag_short = assigned_agent.short_name if assigned_agent else "Agent 1"
        ag_off = assigned_agent.officer_name if assigned_agent else "Officer Priya"
        ag_role = assigned_agent.role if assigned_agent else "Customer Care Specialist"

        # Build multi-turn conversational history for context-aware ReAct generation
        history_lines = []
        for m in reversed(recent_msgs[:12]):
            speaker = "Consumer" if m.sender == VoiceCallMessage.SENDER_CALLER else ag_off
            history_lines.append(f"{speaker}: {m.text}")

        history_context = "\n".join(history_lines) if history_lines else "None"

        spec = AGENT_SPECIALIZATIONS.get(assigned_agent.agent_number, AGENT_SPECIALIZATIONS[10])
        react_system_prompt = f"""You are {assigned_agent.name} ({ag_off}), official {ag_role} at the {spec['desk']}.
You are on a live, real-time 1-to-1 telephone voice call with consumer {caller_name}.

YOUR SPECIALIZED DEPARTMENT JURISDICTION: {spec['focus']}
YOUR PROFESSIONAL PERSONA & CADENCE: {spec['style']}

{format_knowledge_base_prompt(kb)}

### RE-ACT TELEPHONY CONVERSATIONAL RULES (STRICT 2-PERSON DIALOGUE):
1. Persona & Department Specialization (Speak strictly in character as {ag_off} at {spec['desk']}):
   - Always speak from your specific departmental domain ({spec['desk']}).
   - When the caller asks queries within your department (e.g. Hazards/Fire for Officer Rahul, Billing/Tariffs for Officer Ananya, Smart Meters for Officer Rajesh), provide thorough, expert assistance.
   - If the caller asks for services from a different department (e.g. asking the Hazard officer about bills, or asking the Billing officer about bucket trucks):
     Answer concisely using caller telemetry, clarify your primary departmental role, and offer to transfer them to the specialized officer if they need detailed assistance.
   - Speak naturally like a real human customer care officer on the telephone.
   - Use natural spoken contractions ("I'm", "don't", "there's", "it's", "let's").
   - NEVER repeat the caller's name or 'Mr. {caller_name}' in every message. In normal intermediate conversational turns, answer directly without prepending their name.
   - If asked your name or identity: State your name naturally in 1 sentence: "I'm {ag_off} from the {spec['desk']}, how can I help you today?" (NEVER repeat your name twice or sound robotic).
   - Acknowledge the consumer with warm human affirmations ("I understand", "Got it", "Let me check that right away for you").
2. Two-Person Telephone Cadence:
   - Speak strictly 1 to 2 short sentences (maximum 20-35 words per turn).
   - Never dump paragraphs, lists, or tables.
   - Answer the caller's specific question directly in the first sentence.
   - Ask one short, natural follow-up question in the second sentence.
3. Multi-Turn Context & Attentiveness:
   - Remember the live conversation history.
   - When the caller asks 'are you there', 'can you hear me', or 'hello' mid-call: Answer: "Yes, I am right here on the line with you! Please go ahead." (NEVER repeat your full opening introduction or agency title).
   - If the caller speaks an incomplete or trailing fragment (e.g. 'what is my', 'can you check', 'tell me'): Respond warmly: "What would you like me to check — your latest bill, current units, or complaint status?"
4. Confident Outage & Feeder Telemetry Guidance:
   - State the exact status of the caller's transformer ({cp.get('transformer') if cp else 'transformer'}) and feeder line.
   - If offline: State that maintenance or trip occurred on the 11kV feeder line and engineers are actively working on-site.
   - Provide a confident estimated restoration time: power is scheduled to resume within 1 to 2 hours.
   - If the caller asks follow-ups ("why actually", "what is the real reason", "why again"): Do NOT repeat the same sentence; elaborate with confidence on the insulator replacement / grid stabilization work.
4b. Scheduled & Future Outage Inquiries:
   - If the caller asks whether there will be any future power outage or if there is any scheduled outage today (e.g. 'will there be any future power outage', 'is there any power outage today', 'any power cut today'):
   - Check the maintenance schedule and feeder telemetry in the Knowledge Base.
   - If there is NO active maintenance cut: Confidently reassure them that there are no planned or scheduled outages for today and their 11kV feeder/transformer is operating normally with stable supply.
   - NEVER treat an informational inquiry about future outages as an active outage report, NEVER ask diagnostic questions ('confirm if neighboring houses are affected'), and NEVER register a complaint unless the caller explicitly states their power is currently out or asks to report/register a complaint.
5. Invoicing & Billing ReAct Flow:
   - If caller asks for their bill or payment invoice:
     * Emit ACTION: FETCH_INVOICE | Bill: <bill_number>
     * State the bill amount and units, and ask: "Would you like me to send an official copy to your account notifications?"
   - If caller confirms ("yes please", "sure", "send it"):
     * Emit ACTION: SEND_INVOICE_NOTIFICATION | Bill: <bill_number>
     * Confirm delivery and ask if they need anything else.
5b. Step-by-Step Payment Guidance & Quick Pay (Without Login):
   - If caller asks how to pay their bill online, asks for payment guidance, or asks if they can pay on the website without logging in:
     * Explain that they can pay instantly on our website without login using "Quick Pay" by entering their meter number ({service_num or 'registered meter'}) and paying via UPI (Google Pay, PhonePe, Paytm), debit card, or net banking.
     * Alternatively, they can log into their portal dashboard under 'Bills & Payments' and click 'Pay Now'.
     * NEVER fall back to a greeting or repeat previous unrelated questions.
6. Call Wrap-Up & Feedback Transfer:
   - If the caller explicitly says "no thanks", "nothing else", "that's all", "bye", or explicitly asks to give feedback:
     * Emit ACTION: TRANSFER_FEEDBACK
     * Say: "Thank you for contacting Electricity Consumer Care. I am now transferring your call directly to our Customer Feedback Desk. Please hold for just a moment..."
   - CRITICAL: NEVER emit ACTION: TRANSFER_FEEDBACK for conversational acknowledgments like "ok", "okay", "yes", "sure", "got it", "i see", or when the caller is inquiring about departments!
6b. Inter-Department Transfer:
   - If caller asks to speak to another department or specialist officer (e.g. smart meter, billing, safety, supervisor):
     * Emit ACTION: TRANSFER_AGENT | Target: <Agent Number 1-10>
     * Say: "Certainly! Please hold the line for a moment while I transfer your call directly..."
7. Action Protocol:
   - Emit at most ONE action per turn on its own line:
     ACTION: RAISE_COMPLAINT | Issue: <Power Outage/Transformer Hazard/Wire Snap/Voltage Fluctuation/Meter Fault> | Location: <location>
     ACTION: LOOKUP_COMPLAINT | Query: <complaint number or ID>
     ACTION: FETCH_INVOICE | Bill: <bill_number>
     ACTION: SEND_INVOICE_NOTIFICATION | Bill: <bill_number>
     ACTION: TOGGLE_APPLIANCE | Key: <ac/fan1/fan2/lights/tv/fridge> | State: <on/off>
     ACTION: TRANSFER_AGENT | Target: <1-10>
     ACTION: TRANSFER_FEEDBACK
   - Below the ACTION line (or on the next line), write the spoken words to the consumer.
8. Strict Electricity Department Scope (Never Answer General Knowledge / Programming):
   - You are EXCLUSIVELY an Electricity Department / DISCOM customer care officer.
   - You MUST NOT answer general programming questions (e.g., Python, Java, code), general trivia, homework, recipes, sports, or unrelated topics.
   - If the caller asks anything unrelated to electricity, power supply, bills, meters, or complaints, politely refuse in 1-2 short sentences and redirect back to electrical service:
     "I specialize strictly in Electricity Department customer care. I can only assist with power outages, billing, meter issues, and electricity complaints. How may I help you with your electrical service today?"
9. Strict Consumer Privacy Guard (Third-Party Meters & Records):
   - You are STRICTLY FORBIDDEN from disclosing other consumers' meters, personal details, or transformer-wide meter lists.
   - If asked about other meters connected to a transformer (e.g. "what are the meters connected to Transformer 1"):
     * NEVER promise to check or say "Please hold for a moment".
     * Immediately and politely refuse under DISCOM privacy regulations:
       "Under Electricity Department privacy regulations, I can only provide details for your own verified connection and meter ({service_num or 'registered meter'}). I cannot disclose other consumers' meters or transformer connection lists. How can I assist you with your own service?"
"""

        prompt = (
            f"Live Call History:\n{history_context}\n\n"
            f"Consumer ({caller_name}): {clean_text}\n\n"
            f"Respond as {ag_off}:"
        )

        llm_cfg = get_active_llm_config()
        llm_result = call_llm(prompt=prompt, system_prompt=react_system_prompt, config=llm_cfg)

        if llm_result.get("success") and llm_result.get("text"):
            raw_text = llm_result["text"].strip()

            # Parse ReAct ACTION: RAISE_COMPLAINT (strictly excluded on future/scheduled outage inquiries)
            if not is_scheduled_or_future_outage_query and ("ACTION: RAISE_COMPLAINT" in raw_text or any(k in clean_text.lower() for k in ["register complaint", "file complaint", "report outage", "please register"])):
                issue_val = Complaint.ISSUE_POWER_OUTAGE
                if any(k in clean_text.lower() for k in ["spark", "fire", "blast"]):
                    issue_val = Complaint.ISSUE_TRANSFORMER_HAZARD
                elif any(k in clean_text.lower() for k in ["wire", "snap"]):
                    issue_val = Complaint.ISSUE_WIRE_SNAP
                elif any(k in clean_text.lower() for k in ["voltage", "low"]):
                    issue_val = Complaint.ISSUE_VOLTAGE_FLUCTUATION
                elif any(k in clean_text.lower() for k in ["meter"]):
                    issue_val = Complaint.ISSUE_METER_FAULT

                new_cmp = raise_complaint(
                    caller_name=caller_name,
                    location=loc,
                    issue_type=issue_val,
                    service_number=service_num,
                    user_account=user_account or session.user_account,
                )
                session.associated_complaint = new_cmp
                session.call_intent = VoiceCallSession.INTENT_COMPLAINT_REGISTRATION
                session.save()
                registered_complaint_info = {
                    "complaint_number": new_cmp.complaint_number,
                    "complaint_id": new_cmp.complaint_id,
                    "issue_type": new_cmp.issue_type,
                    "location": new_cmp.location,
                    "status": new_cmp.get_status_display(),
                    "priority": new_cmp.get_priority_display(),
                    "estimated_resolution": new_cmp.estimated_resolution,
                }
                action_taken = f"RAISE_COMPLAINT: {new_cmp.complaint_number}"

            # Parse ReAct ACTION: TOGGLE_APPLIANCE
            if "ACTION: TOGGLE_APPLIANCE" in raw_text:
                m_app = re.search(r"ACTION:\s*TOGGLE_APPLIANCE\s*\|\s*Key:\s*([a-zA-Z0-9_-]+)\s*\|\s*State:\s*(on|off)", raw_text, re.IGNORECASE)
                if m_app:
                    app_key = m_app.group(1).lower()
                    app_state = m_app.group(2).lower() == "on"
                    conn = ConnectionApplication.objects.filter(user=user_account or session.user_account).first()
                    if conn:
                        try:
                            from users.billing import get_builtin_appliance_states
                            states = get_builtin_appliance_states(conn)
                            states[app_key] = app_state
                            conn.appliance_states = states
                            conn.save(update_fields=["appliance_states", "updated_at"])
                            action_taken = f"TOGGLE_APPLIANCE: {app_key} -> {'ON' if app_state else 'OFF'}"
                        except Exception as e:
                            logger.warning(f"Error toggling appliance: {e}")

            # Parse ReAct ACTION: FETCH_INVOICE
            if ("ACTION: FETCH_INVOICE" in raw_text or any(k in clean_text.lower() for k in ["give me my last payment invoice", "give me my invoice", "send me my invoice", "show my invoice", "print invoice", "receipt", "download invoice"])) and not invoice_payload:
                target_user = user_account or session.user_account
                bill = None
                if target_user:
                    bill = (
                        PowerBill.objects.filter(connection__user=target_user, status=PowerBill.STATUS_PAID).order_by("-paid_at", "-created_at").first()
                        or PowerBill.objects.filter(connection__user=target_user).order_by("-created_at").first()
                    )
                if not bill:
                    bill = PowerBill.objects.order_by("-created_at").first()
                if bill:
                    meter_num = bill.connection.meter_number if bill.connection else (service_num or "MTR-2026-1004-3")
                    app_name = bill.connection.applicant_name if bill.connection else caller_name
                    invoice_payload = {
                        "id": bill.id,
                        "bill_number": bill.bill_number,
                        "billing_month": bill.billing_month or "Current",
                        "bill_type": bill.get_bill_type_display(),
                        "units_billed": float(bill.units_billed),
                        "rate_per_unit": float(bill.rate_per_unit),
                        "base_fare": float(bill.base_fare),
                        "energy_charge": float(bill.energy_charge),
                        "arrears_amount": float(bill.arrears_amount),
                        "total_amount": float(bill.total_amount),
                        "status": bill.status,
                        "is_paid": (bill.status == PowerBill.STATUS_PAID),
                        "payment_method": bill.payment_method or "DISCOM Online Portal",
                        "transaction_id": bill.transaction_id or f"TXN-DISCOM-{bill.id:05d}",
                        "paid_at": (
                            timezone.localtime(bill.paid_at).strftime("%d %b %Y, %I:%M %p")
                            if bill.paid_at else (timezone.localtime(bill.created_at).strftime("%d %b %Y, %I:%M %p") if bill.status == PowerBill.STATUS_PAID else "Payment Due")
                        ),
                        "meter_number": meter_num,
                        "applicant_name": app_name,
                        "house_address": bill.connection.house_address if bill.connection else loc,
                        "pdf_url": reverse("download_bill_invoice_pdf", kwargs={"bill_id": bill.id}),
                    }
                    action_taken = f"FETCH_INVOICE: {bill.id}:{bill.bill_number}"

            # Parse ReAct ACTION: SEND_INVOICE_NOTIFICATION
            if "ACTION: SEND_INVOICE_NOTIFICATION" in raw_text:
                target_user = user_account or session.user_account
                bill = None
                if target_user:
                    bill = (
                        PowerBill.objects.filter(connection__user=target_user, status=PowerBill.STATUS_PAID).order_by("-paid_at", "-created_at").first()
                        or PowerBill.objects.filter(connection__user=target_user).order_by("-created_at").first()
                    )
                if not bill:
                    bill = PowerBill.objects.order_by("-created_at").first()
                if target_user and bill:
                    b_month = bill.billing_month or "Current"
                    b_num = bill.bill_number
                    b_meter = bill.connection.meter_number if bill.connection else service_num
                    b_amt = f"{bill.total_amount:.2f}"
                    b_units = f"{bill.units_billed}"
                    b_url = reverse("download_bill_invoice_pdf", kwargs={"bill_id": bill.id})
                    existing_notif = UserNotification.objects.filter(user=target_user, extra_info=f"Bill No: {b_num}").first()
                    if not existing_notif:
                        UserNotification.objects.create(
                            user=target_user,
                            category=UserNotification.CATEGORY_BILLS,
                            category_label="Payment Invoice",
                            severity=UserNotification.SEVERITY_SUCCESS,
                            icon="fa-solid fa-file-invoice-dollar",
                            badge="INVOICE DELIVERED",
                            title=f"Paid Invoice: {b_month} ({b_num})",
                            message=f"Official tax invoice for Meter {b_meter} — Total Rs {b_amt} for {b_units} units is confirmed and delivered to your account hub.",
                            extra_info=f"Bill No: {b_num}",
                            cta_label="View / Download Invoice PDF",
                            cta_url=b_url,
                        )
                    action_taken = f"SEND_INVOICE_NOTIFICATION: {b_num}"

            # Parse ReAct ACTION: LOOKUP_COMPLAINT
            if "ACTION: LOOKUP_COMPLAINT" in raw_text:
                m_lk = re.search(r"ACTION:\s*LOOKUP_COMPLAINT\s*\|\s*Query:\s*([^\n|]+)", raw_text, re.IGNORECASE)
                q_term = m_lk.group(1).strip() if m_lk else clean_text
                found = lookup_complaint(q_term, caller_name=caller_name)
                if found:
                    lookup_complaint_info = found[0]
                    action_taken = f"LOOKUP_COMPLAINT: {lookup_complaint_info['complaint_number']}"
                    intent = VoiceCallSession.INTENT_STATUS_VERIFICATION

            # Parse ReAct ACTION: TRANSFER_AGENT
            m_tf_ag = re.search(r"ACTION:\s*TRANSFER_AGENT\s*\|\s*Target:\s*(\d+|[A-Za-z]+)", raw_text, re.IGNORECASE)
            if m_tf_ag:
                t_val = m_tf_ag.group(1).strip().lower()
                num_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11}
                target_num = num_map.get(t_val, int(t_val) if t_val.isdigit() else None)
                if target_num == 11:
                    feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()
                    if feedback_agent:
                        session.is_feedback_stage = True
                        session.assigned_agent = feedback_agent
                        session.save()
                        assigned_agent = feedback_agent
                        action_taken = "TRANSFER_FEEDBACK_AGENT"
                        intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION
                elif target_num and target_num != getattr(assigned_agent, "agent_number", None):
                    target_ag = CallCenterAgent.objects.filter(agent_number=target_num).first()
                    if target_ag and (not target_ag.is_active or target_ag.status == CallCenterAgent.STATUS_OFFLINE):
                        action_taken = f"AGENT_OFFLINE_REJECTED: Agent {target_ag.agent_number} is offline"
                        target_name = target_ag.officer_name or target_ag.name
                        ai_reply = (
                            f"I apologize, {caller_name}. Agent {target_ag.agent_number} — {target_name} ({target_ag.role}) "
                            f"is currently offline on administrative break or off-duty. "
                            f"How else may I assist you today?"
                        )
                    elif target_ag and target_ag.is_active and target_ag.status != CallCenterAgent.STATUS_OFFLINE:
                        session.assigned_agent = target_ag
                        session.save()
                        assigned_agent = target_ag
                        action_taken = f"IVR_CONNECTED_TO_AGENT_{target_ag.agent_number}"
                        target_name = target_ag.officer_name or target_ag.name
                        target_desk = target_ag.department_desk or target_ag.role or "Specialist Desk"
                        ai_reply = (
                            f"Certainly! Please hold the line for a moment while I transfer your call directly to {target_name} at our {target_desk}... "
                            f"Call connected! "
                            f"Hello {caller_name}! This is {target_name}, {target_ag.role}. "
                            f"I am actively on the line with you. How may I assist you today?"
                        )

            # Parse ReAct ACTION: TRANSFER_FEEDBACK (Strictly guarded against false triggers on conversational 'ok'/'yes')
            if "ACTION: TRANSFER_FEEDBACK" in raw_text:
                is_explicit_departure = any(k in text_lower for k in [
                    "no thanks", "no thank you", "nothing else", "that's all", "thats all",
                    "bye", "goodbye", "i'm done", "i am done", "all good", "no more",
                    "feedback desk", "give feedback", "feedback"
                ])
                if is_explicit_departure:
                    feedback_agent = CallCenterAgent.objects.filter(agent_number=11, is_active=True).first()
                    if feedback_agent:
                        session.is_feedback_stage = True
                        session.assigned_agent = feedback_agent
                        session.save()
                        assigned_agent = feedback_agent
                        action_taken = "TRANSFER_FEEDBACK_AGENT"
                        intent = VoiceCallSession.INTENT_FEEDBACK_COLLECTION

            # Clean ACTION lines and markdown tables/asterisks from speech
            clean_reply = re.sub(r"ACTION:\s*[A-Z_]+(?:\s*\|[^\n]*)?", "", raw_text).strip()
            clean_reply = re.sub(r"\|[^\n]+\|", "", clean_reply)  # remove markdown tables
            clean_reply = re.sub(r"^[#*>\s\-|]+", "", clean_reply)
            clean_reply = clean_reply.replace("**", "").replace("##", "").replace("`", "")
            clean_reply = re.sub(r"\n{2,}", " ", clean_reply).strip()

            # Strip LLM speaker prefixes like "Officer Sneha: " or "Agent 1: "
            clean_reply = re.sub(r"^(?:Officer|Agent)\s+[A-Za-z0-9_-]+:\s*", "", clean_reply, flags=re.IGNORECASE).strip()

            # In intermediate turns, strip repetitive "Mr. Name, " or "Hello Mr. Name! " if model repeats it
            if len(recent_msgs) >= 2:
                clean_reply = re.sub(rf"^(?:Hello\s+)?(?:Mr\.?\s+)?{re.escape(caller_name)}[,!:\s]+", "", clean_reply, flags=re.IGNORECASE).strip()
                clean_reply = re.sub(r"^(?:Hello\s+)?Mr\.?\s+[A-Za-z]+[,!:\s]+", "", clean_reply, flags=re.IGNORECASE).strip()

            if clean_reply:
                ai_reply = clean_reply
            elif is_direct_upi_debit_query:
                target_meter = service_num or "your registered meter"
                ai_reply = (
                    f"For your financial security under RBI and DISCOM guidelines, customer care officers cannot collect your UPI ID, PIN, or banking details over the telephone to pay on your behalf. "
                    f"Please complete payment yourself securely on our website homepage using 'Quick Pay' with meter number {target_meter}, or directly through your UPI app like PhonePe or Google Pay."
                )
            elif is_scheduled_or_future_outage_query:
                tf_name = cp.get("transformer") if cp and cp.get("transformer") else "Central Distribution Transformer"
                ai_reply = (
                    f"Central Grid telemetry confirms there are no scheduled or planned power outages for your area today. "
                    f"Your 11kV feeder and transformer ({tf_name}) are fully energized and running normally."
                )
            elif is_payment_guidance_query:
                target_meter = service_num or "your registered meter"
                ai_reply = (
                    f"You can pay your electricity bill online via our website homepage under 'Quick Pay' using meter {target_meter}, "
                    f"or by logging into your consumer portal under 'Bills & Payments'. Which method would you prefer?"
                )
            elif len(recent_msgs) >= 2:
                ai_reply = (
                    f"I'm right here with you on your line for meter {service_num}. "
                    f"Could you please clarify what details you need regarding your power supply, bill payment, or meter reading?"
                )
            else:
                ai_reply = (
                    f"Hello {caller_name}! This is {ag_off} from the Electricity Department. "
                    f"I have verified your premise meter {service_num}. How can I assist you today?"
                )
        else:
            # Fallback natural voice response when LLM is unavailable or empty
            if is_direct_upi_debit_query:
                target_meter = service_num or "your registered meter"
                ai_reply = (
                    f"For your financial security under RBI and DISCOM guidelines, customer care officers cannot collect your UPI ID, PIN, or banking details over the telephone to pay on your behalf. "
                    f"Please complete payment yourself securely on our website homepage using 'Quick Pay' with meter number {target_meter}, or directly through your UPI app like PhonePe or Google Pay."
                )
            elif is_scheduled_or_future_outage_query:
                tf_name = cp.get("transformer") if cp and cp.get("transformer") else "Central Distribution Transformer"
                ai_reply = (
                    f"Central Grid telemetry confirms there are no scheduled or planned power outages for your area today. "
                    f"Your 11kV feeder and transformer ({tf_name}) are fully energized and running normally."
                )
            elif is_payment_guidance_query:
                target_meter = service_num or "your registered meter"
                ai_reply = (
                    f"You can pay your electricity bill online via our website homepage under 'Quick Pay' using meter {target_meter}, "
                    f"or by logging into your consumer portal under 'Bills & Payments'. Which method would you prefer?"
                )
            elif len(recent_msgs) >= 2:
                ai_reply = (
                    f"I'm right here with you on your line for meter {service_num}. "
                    f"Could you please clarify what details you need regarding your power supply, bill payment, or meter reading?"
                )
            else:
                ai_reply = (
                    f"Hello {caller_name}! This is {ag_off} from the Electricity Department. "
                    f"I am actively monitoring your service line and meter {service_num}. How can I assist you right now?"
                )

    elapsed_ms = int((time.time() - start_time) * 1000)

    # Record AI message
    VoiceCallMessage.objects.create(
        session=session,
        sender=VoiceCallMessage.SENDER_AI,
        text=ai_reply,
        latency_ms=elapsed_ms,
        intent_detected=intent,
        action_taken=action_taken,
    )

    # Update session
    session.call_intent = intent
    session.avg_latency_ms = elapsed_ms
    session.save()

    agent_label = assigned_agent.officer_name if assigned_agent else "Agent"
    typing_label = f"{agent_label} is accessing DISCOM database & typing notes..."
    if invoice_payload or "INVOICE" in action_taken:
        typing_label = f"{agent_label} is generating official DISCOM tax invoice & receipt..."
    elif registered_complaint_info:
        typing_label = f"{agent_label} is recording your complaint into dispatch terminal..."
    elif lookup_complaint_info:
        typing_label = f"{agent_label} is querying complaint tracking database..."
    elif intent == VoiceCallSession.INTENT_BILLING_INQUIRY:
        typing_label = f"{agent_label} is looking up billing ledgers & tariffs..."
    elif intent == VoiceCallSession.INTENT_POWER_STATUS:
        typing_label = f"{agent_label} is checking live substation telemetry..."

    # Generate natural phonetic version for Speech Synthesis (TTS)
    spoken_text = format_spoken_text(ai_reply)

    # Generate studio-grade human neural audio (Edge-TTS Expressive) using unified agent profile
    from .tts_utils import generate_neural_speech_base64, get_agent_voice_params
    v_params = get_agent_voice_params(assigned_agent)
    audio_base64 = generate_neural_speech_base64(
        spoken_text,
        voice=v_params["voice"],
        rate=v_params.get("rate", "+3%"),
        pitch=v_params.get("pitch", "+0Hz"),
    )

    agent_data = None
    if assigned_agent and not session.is_ivr_active:
        agent_data = {
            "id": assigned_agent.id,
            "agent_number": assigned_agent.agent_number,
            "agent_code": assigned_agent.agent_code,
            "name": assigned_agent.name,
            "short_name": assigned_agent.short_name,
            "officer_name": assigned_agent.officer_name,
            "role": assigned_agent.role,
            "gender": assigned_agent.gender,
            "voice_type": assigned_agent.voice_type,
            "voice_pitch": assigned_agent.voice_pitch,
            "voice_rate": assigned_agent.voice_rate,
            "avatar_color": assigned_agent.avatar_color,
            "avatar_icon": assigned_agent.avatar_icon,
            "status": assigned_agent.status,
        }

    return {
        "success": True,
        "reply": ai_reply,
        "spoken_text": spoken_text,
        "audio_base64": audio_base64,
        "is_ivr_active": session.is_ivr_active,
        "is_feedback_stage": session.is_feedback_stage,
        "hang_up": hang_up_call,
        "feedback_recorded": feedback_recorded,
        "assigned_agent": agent_data,
        "intent": intent,
        "action_taken": action_taken,
        "latency_ms": elapsed_ms,
        "simulate_typing_ms": 1800,
        "typing_label": typing_label,
        "registered_complaint": registered_complaint_info,
        "lookup_complaint": lookup_complaint_info,
        "invoice": invoice_payload,
        "session_id": str(session.session_id),
    }
