"""High-Fidelity Real Human Neural Audio Generation (Edge-TTS).

Synthesizes studio-grade natural human speech using Microsoft's
Neural Indian English voice models (e.g. en-IN-NeerjaNeural, en-IN-PrabhatNeural).
Returns base64 MP3 audio data for zero-latency, authentic human playback
in the user's browser, completely eliminating robotic TTS sounds.
"""

import asyncio
import base64
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Specific voice profiles per agent number for realistic distinct officer personas
AGENT_VOICE_PROFILES = {
    1: {"voice": "en-IN-NeerjaExpressiveNeural", "rate": "+3%", "pitch": "+1Hz"},   # Officer Priya (Outages)
    2: {"voice": "en-IN-PrabhatNeural", "rate": "+1%", "pitch": "-2Hz"},           # Officer Vikram (Grid)
    3: {"voice": "en-IN-NeerjaNeural", "rate": "+2%", "pitch": "+3Hz"},             # Officer Ananya (Billing)
    4: {"voice": "en-IN-PrabhatNeural", "rate": "+4%", "pitch": "+1Hz"},           # Officer Rahul (Hazards)
    5: {"voice": "en-IN-NeerjaExpressiveNeural", "rate": "+0%", "pitch": "-2Hz"},   # Officer Sneha (Telemetry)
    6: {"voice": "en-IN-PrabhatNeural", "rate": "-1%", "pitch": "-3Hz"},           # Officer Rajesh (Meters)
    7: {"voice": "en-IN-NeerjaNeural", "rate": "+1%", "pitch": "+0Hz"},             # Officer Kavita (Grievance)
    8: {"voice": "en-IN-PrabhatNeural", "rate": "+3%", "pitch": "+2Hz"},           # Officer Amit (Field Crew)
    9: {"voice": "en-IN-NeerjaExpressiveNeural", "rate": "+4%", "pitch": "+4Hz"},   # Officer Pooja (Commercial)
    10: {"voice": "en-IN-PrabhatNeural", "rate": "-2%", "pitch": "-4Hz"},          # Officer Suresh (Shift In-Charge)
    11: {"voice": "en-IN-NeerjaExpressiveNeural", "rate": "+2%", "pitch": "+2Hz"},  # Officer Ritu (Feedback QA)
}


def get_agent_voice_params(agent=None) -> dict:
    """Return the exact neural voice model, pitch, and rate for a call center agent.

    Guarantees that admin test voice and caller live telephony voice sound 100% identical.
    """
    if not agent:
        return {"voice": "en-IN-NeerjaExpressiveNeural", "rate": "+3%", "pitch": "+0Hz"}

    ag_num = getattr(agent, "agent_number", None)
    if ag_num in AGENT_VOICE_PROFILES:
        return AGENT_VOICE_PROFILES[ag_num]

    gender = getattr(agent, "gender", "FEMALE")
    if str(gender).upper() in ["MALE", "M"]:
        return {"voice": "en-IN-PrabhatNeural", "rate": "+2%", "pitch": "-1Hz"}
    return {"voice": "en-IN-NeerjaExpressiveNeural", "rate": "+3%", "pitch": "+1Hz"}


def generate_neural_speech_base64(
    text: str,
    voice: str = "en-IN-NeerjaExpressiveNeural",
    rate: str = "+3%",
    pitch: str = "+0Hz",
    timeout_secs: float = 6.0,
) -> Optional[str]:
    """Synthesize text into studio-grade natural human speech (ChatGPT Voice quality)

    using Microsoft Edge Neural Expressive models and return base64-encoded MP3 data.
    """
    if not text or not text.strip():
        return None

    clean_text = text.strip()

    try:
        import edge_tts

        async def _synthesize(target_voice: str):
            comm = edge_tts.Communicate(clean_text, voice=target_voice, rate=rate, pitch=pitch)
            chunks = []
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            if not chunks:
                return None
            return base64.b64encode(b"".join(chunks)).decode("ascii")

        try:
            return asyncio.run(asyncio.wait_for(_synthesize(voice), timeout=timeout_secs))
        except Exception as e_inner:
            # Fall back to standard NeerjaNeural if expressive voice unavailable
            if "Expressive" in voice:
                logger.info(f"Retrying with standard NeerjaNeural: {e_inner}")
                return asyncio.run(asyncio.wait_for(_synthesize("en-IN-NeerjaNeural"), timeout=timeout_secs))
            raise e_inner

    except Exception as e:
        logger.debug(f"Neural TTS fallback to browser synthesis: {e}")
        return None
