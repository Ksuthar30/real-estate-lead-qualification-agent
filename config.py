import os
from dotenv import load_dotenv

load_dotenv()

# =========================================================================================
#  🤖 RAPID X AI - AGENT CONFIGURATION
#  Use this file to customize your agent's personality, models, and behavior.
# =========================================================================================

# --- 1. AGENT PERSONA & PROMPTS ---
# The main instructions for the AI. Defines who it is and how it behaves.
SYSTEM_PROMPT = """
You are Riya, a clear human-like real-estate calling agent for Proviso Group.

Business goal: segregate leads into interested, not_interested, callback_later, wrong_lead, or unclear.
If interested, collect only BHK and timeline.

Style: simple Indian English. Listen, acknowledge, answer safely, then ask one next question.
Never invent exact prices, project names, offers, discounts, or availability.
Do not argue with firm rejection; respect it and close.
If the customer says English, hello repeatedly, or does not understand, switch to very simple English immediately.

Voice rules: <=18 words, one question max, no emoji, no markdown, no JSON, no tool/debug text, no Hindi script.
Controller decides flow. You only phrase the Controller block naturally.
"""

# The explicit first message the agent speaks when the user picks up.
# This ensures the user knows who is calling immediately.
INITIAL_GREETING = "Hi, this is Riya from Proviso Group. You had a property enquiry. Is this a good time?"

# If the user initiates the call (inbound) or is already there:
fallback_greeting = "Hi, this is Riya from Proviso Group. How can I help with your property enquiry?"


# --- 2. SPEECH-TO-TEXT (STT) SETTINGS ---
# We use Deepgram for high-speed transcription.
STT_PROVIDER = "deepgram"
STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")
STT_LANGUAGE = "multi"


# --- 3. TEXT-TO-SPEECH (TTS) SETTINGS ---
# Choose your voice provider: "openai", "sarvam" (Indian voices), or "cartesia" (Ultra-fast)
DEFAULT_TTS_PROVIDER = "sarvam"
DEFAULT_TTS_VOICE = "anushka"    # OpenAI: alloy, echo, shimmer | Sarvam: anushka, aravind
AGENT_GENDER = os.getenv("AGENT_GENDER", "female")

# Sarvam AI Specifics (for Indian Context)
SARVAM_MODEL = "bulbul:v2"
SARVAM_LANGUAGE = "en-IN"
SARVAM_PACE = 0.82

# Cartesia Specifics
CARTESIA_MODEL = "sonic-2"
CARTESIA_VOICE = "f786b574-daa5-4673-aa0c-cbe3e8534c02"


# --- 4. LARGE LANGUAGE MODEL (LLM) SETTINGS ---
# Choose "openai" or "groq"
DEFAULT_LLM_PROVIDER = "openai"
DEFAULT_LLM_MODEL = "gpt-4o-mini" # OpenAI default
DEFAULT_LLM_TEMPERATURE = 0.2

# Groq Specifics (Faster inference)
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TEMPERATURE = 0.2


# --- 5. TELEPHONY & TRANSFERS ---
# Default number to transfer calls to if no specific destination is asked.
DEFAULT_TRANSFER_NUMBER = os.getenv("DEFAULT_TRANSFER_NUMBER")

# Vobiz Trunk Details (Loaded from .env usually, but you can hardcode if needed)
SIP_TRUNK_ID = os.getenv("VOBIZ_SIP_TRUNK_ID")
SIP_DOMAIN = os.getenv("VOBIZ_SIP_DOMAIN")
SIP_OUTBOUND_NUMBER = os.getenv("VOBIZ_OUTBOUND_NUMBER")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


OUTBOUND_RINGING_TIMEOUT_SECONDS = _env_int("OUTBOUND_RINGING_TIMEOUT_SECONDS", 40)
