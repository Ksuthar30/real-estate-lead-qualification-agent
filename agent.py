import os
import certifi
import tempfile

# Fix for macOS SSL Certificate errors - MUST be before other imports
os.environ['SSL_CERT_FILE'] = certifi.where()
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_AGENT_TMP_DIR = os.getenv("AGENT_TMP_DIR", os.path.join(_PROJECT_DIR, "agent_tmp"))
_CALL_STATUS_DIR = os.getenv("CALL_STATUS_DIR", os.path.join(_PROJECT_DIR, "call_status"))
os.makedirs(_AGENT_TMP_DIR, exist_ok=True)
os.makedirs(_CALL_STATUS_DIR, exist_ok=True)
os.environ["TMP"] = _AGENT_TMP_DIR
os.environ["TEMP"] = _AGENT_TMP_DIR
os.environ["TMPDIR"] = _AGENT_TMP_DIR
tempfile.tempdir = _AGENT_TMP_DIR

_BaseTemporaryDirectory = tempfile.TemporaryDirectory


class _SafeTemporaryDirectory(_BaseTemporaryDirectory):
    # LiveKit job workers can leave Windows temp handles open for a moment after
    # session shutdown. Ignoring cleanup races avoids a false job failure.
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("ignore_cleanup_errors", True)
        super().__init__(*args, **kwargs)


tempfile.TemporaryDirectory = _SafeTemporaryDirectory
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
for _proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    if os.environ.get(_proxy_name, "").strip() in {"http://127.0.0.1:9", "https://127.0.0.1:9"}:
        os.environ.pop(_proxy_name, None)

import asyncio
import logging
import json
import re
import time
from dataclasses import dataclass, field
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from livekit import agents, api
from livekit.agents import (
    AgentSession,
    Agent,
    RoomInputOptions,
    CloseEvent,
    ConversationItemAddedEvent,
    UserInputTranscribedEvent,
    APIConnectOptions,
)
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.agents import tts as agents_tts
from livekit.plugins import (
    openai,
    cartesia,
    deepgram,
    noise_cancellation,
    silero,
    sarvam,
)
from livekit.agents import llm
from typing import Annotated, Optional
from google.protobuf.duration_pb2 import Duration
from livekit.protocol.sip import ListSIPOutboundTrunkRequest

from sarvam_runtime_patch import apply_sarvam_wav_patch

# Load environment variables
load_dotenv(".env")
apply_sarvam_wav_patch()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbound-agent")

import config
import sheet_service

# TRUNK ID - Now loaded from config.py
# You can find this by running 'python setup_trunk.py --list' or checking LiveKit Dashboard 

DEEPGRAM_HINT_KEYWORDS = [
    ("hello", 2.0),
    ("who is this", 2.5),
    ("who's this", 2.5),
    ("who are you", 2.5),
    ("who's there", 2.5),
    ("haan", 2.5),
    ("haan ji", 2.5),
    ("hanji", 2.5),
    ("ji", 1.8),
    ("hmm", 2.0),
    ("hm", 2.0),
    ("achha", 2.0),
    ("acha", 2.0),
    ("badiya", 2.5),
    ("badhiya", 2.5),
    ("theek", 2.2),
    ("thik", 2.2),
    ("bolo", 2.0),
    ("boliye", 2.0),
    ("kharghar", 2.5),
    ("panvel", 2.5),
    ("panvel mein", 2.5),
    ("one crore", 2.5),
    ("ek crore", 2.5),
    ("site visit", 2.0),
    ("budget", 2.0),
    ("project", 2.0),
    ("location", 2.0),
    ("1 BHK", 3.0),
    ("2 BHK", 3.0),
    ("3 BHK", 3.0),
    ("one BHK", 3.0),
    ("two BHK", 3.0),
    ("three BHK", 3.0),
    ("ek BHK", 3.0),
    ("do BHK", 3.0),
    ("teen BHK", 3.0),
    ("whatsapp", 2.0),
    ("sunday", 2.3),
    ("callback", 2.0),
    ("bye", 2.0),
    ("samajh", 2.0),
    ("kya", 1.8),
]

SHORT_REPLY_REWRITES = {
    "haan": "The customer gave a short positive acknowledgement. Continue with the next relevant property question and do not repeat your introduction.",
    "ha": "The customer gave a short positive acknowledgement. Continue with the next relevant property question and do not repeat your introduction.",
    "haan ji": "The customer politely acknowledged you. Continue with the next relevant property question and do not repeat your introduction.",
    "hanji": "The customer politely acknowledged you. Continue with the next relevant property question and do not repeat your introduction.",
    "ji": "The customer acknowledged you and is listening.",
    "haan boliye": "The customer gave permission to continue. Ask the next relevant property question without repeating the introduction.",
    "ha boliye": "The customer gave permission to continue. Ask the next relevant property question without repeating the introduction.",
    "ji boliye": "The customer gave permission to continue. Ask the next relevant property question without repeating the introduction.",
    "boliye boliye": "The customer is asking you to continue. Ask the next relevant property question without sounding confused.",
    "bataiye": "The customer is asking you to continue. Ask the next relevant property question without repeating the introduction.",
    "bataye": "The customer is asking you to continue. Ask the next relevant property question without repeating the introduction.",
    "go ahead": "The customer is asking you to continue. Ask the next relevant property question.",
    "please continue": "The customer is asking you to continue. Ask the next relevant property question.",
    "bolivia": "The STT likely misheard 'boliye'. Treat it as permission to continue.",
    "a bolivia": "The STT likely misheard 'haan boliye'. Treat it as permission to continue.",
    "bolevia": "The STT likely misheard 'boliye'. Treat it as permission to continue.",
    "a bolevia": "The STT likely misheard 'haan boliye'. Treat it as permission to continue.",
    "muy bien": "The STT likely misheard 'haan boliye'. Treat it as permission to continue.",
    "moi bien": "The STT likely misheard 'haan boliye'. Treat it as permission to continue.",
    "muy bean": "The STT likely misheard 'haan boliye'. Treat it as permission to continue.",
    "muybien": "The STT likely misheard 'haan boliye'. Treat it as permission to continue.",
    "hmm": "The customer acknowledged you and is listening.",
    "hm": "The customer acknowledged you and is listening.",
    "achha": "The customer acknowledged you and wants you to continue.",
    "acha": "The customer acknowledged you and wants you to continue.",
    "bolo": "The customer is asking you to continue speaking. Continue with the next relevant property question and do not reintroduce yourself or the company.",
    "boliye": "The customer is asking you to continue speaking. Continue with the next relevant property question and do not reintroduce yourself or the company.",
    "badiya": "The customer says they are doing well. Acknowledge warmly and continue the conversation.",
    "badhiya": "The customer says they are doing well. Acknowledge warmly and continue the conversation.",
    "theek": "The customer says they are fine. Acknowledge warmly and continue the conversation.",
    "thik": "The customer says they are fine. Acknowledge warmly and continue the conversation.",
    "theek hu": "The customer says they are doing well. Acknowledge warmly and continue the conversation.",
    "thik hu": "The customer says they are doing well. Acknowledge warmly and continue the conversation.",
    "yeah": "The customer gave a short positive acknowledgement. Continue with the next relevant property question.",
    "yes": "The customer gave a short positive acknowledgement. Continue with the next relevant property question.",
    "yes yes": "The customer gave a clear positive answer. Continue to the next relevant property question.",
    "yes is": "The STT likely captured a short yes. Treat it as a positive answer to the current question.",
    "yep": "The customer gave a short positive acknowledgement. Continue with the next relevant property question.",
}

PERMISSION_ONLY_ACKS = {
    "boliye",
    "bolo",
    "haan boliye",
    "ha boliye",
    "ji boliye",
    "boliye boliye",
    "bataiye",
    "bataye",
    "go ahead",
    "please continue",
}

CONFUSION_PATTERNS = (
    "क्या बोल रहे",
    "क्या कह रहे",
    "समझ नहीं",
    "samajh nahi",
    "samjh nahi",
    "kya bol",
    "kya keh",
    "clear nahi",
    "sunai nahi",
    "hello?",
    "हेलो?",
)

HELLO_REPAIR_PATTERNS = (
    "hello",
    "hello?",
    "हेलो",
    "हेलो?",
)

IDENTITY_QUESTION_PATTERNS = (
    "who is this",
    "who's this",
    "who s this",
    "whos this",
    "who are you",
    "who is there",
    "who's there",
    "who s there",
    "whos there",
    "who there",
    "who called",
    "where are you calling from",
    "which company",
    "kaun bol",
    "kon bol",
    "kaun hai",
    "kon hai",
    "कौन बोल",
    "कौन है",
)

CALL_PURPOSE_QUESTION_PATTERNS = (
    "kis regarding",
    "regarding kya",
    "regarding call",
    "call regarding",
    "what regarding",
    "what is this call",
    "what is this about",
    "why did you call",
    "why are you calling",
    "kyu call",
    "kyun call",
    "kyu phone",
    "kyun phone",
    "phone kyu",
    "phone kyun",
    "call kyu",
    "call kyun",
    "aapne phone kyu",
    "aapne phone kyun",
    "aapne call kyu",
    "aapne call kyun",
    "kis liye call",
    "kisliye call",
    "kis liye phone",
    "kisliye phone",
    "kaunse matter",
    "kaun se matter",
    "matter me call",
    "matter mein call",
    "kis kaam se",
    "kya kaam hai",
    "किसलिए कॉल",
    "क्यों कॉल",
    "किस बारे में",
)

PROJECT_QUERY_PATTERNS = (
    "कौन सा प्रोजेक्ट",
    "कौनसा प्रोजेक्ट",
    "project hai",
    "project h",
    "kaun sa project",
    "kaunsa project",
    "kon sa project",
    "panvel mein",
    "panvel me",
    "kharghar mein",
    "kharghar me",
)

QUESTION_PATTERNS = (
    "क्या",
    "कौन",
    "कौन सा",
    "कहाँ",
    "किधर",
    "कितना",
    "कैसे",
    "कब",
    "kya",
    "kaun",
    "kaunsa",
    "kon sa",
    "kahan",
    "kidhar",
    "kitna",
    "kaise",
    "kab",
)

END_CALL_PATTERNS = (
    "bye",
    "bye bye",
    "बाय",
    "अलविदा",
    "call later",
    "callback",
    "baad mein call",
    "baad me call",
    "not interested",
    "nahi chahiye",
    "nahi चाहिए",
    "mat call",
    "gotta go",
    "got to go",
    "have to go",
    "need to go",
    "i have to leave",
    "abhi jana",
    "abhi jaana",
)

LANGUAGE_REQUEST_PATTERNS = (
    "english",
    "speak english",
    "talk in english",
    "can you speak english",
    "say in english",
    "please english",
    "hindi nahi",
    "no hindi",
)

AUDIO_CONFUSION_PATTERNS = (
    "i dont understand",
    "i don't understand",
    "i don t understand",
    "dont understand",
    "don't understand",
    "don t understand",
    "cannot understand",
    "can't understand",
    "cant understand",
    "can t understand",
    "not understanding",
    "what are you saying",
    "what you saying",
    "what you're saying",
    "what you are saying",
    "voice broke",
    "voice is breaking",
    "your voice broke",
    "voice not clear",
    "not clear",
    "audio not clear",
    "i cant hear",
    "i can't hear",
    "i can t hear",
    "cannot hear",
    "say again",
    "repeat",
)

STT_NOISE_PATTERNS = (
    "you",
    "u",
    "one",
    "angolia",
    "angolya",
    "bodith",
    "uh bodith",
    "you borro",
    "you borró",
    "you borr",
    "you borro?",
    "you borro",
    "you thik",
    "this",
    "these",
    "ek this",
    "theek ek this",
    "take this",
    "dejan",
    "you triste",
)

HARD_STOP_PATTERNS = (
    "i dont want to talk",
    "i don't want to talk",
    "i don t want to talk",
    "i do not want to talk",
    "dont want to talk",
    "don't want to talk",
    "don t want to talk",
    "do not call",
    "don't call",
    "don t call",
    "dont call",
    "stop calling",
    "stop the call",
    "stop this call",
    "cut the call",
    "end the call",
    "thank you bye",
    "thanks bye",
    "okay bye",
    "ok bye",
)

AFFIRMATIVE_PATTERNS = (
    "haan",
    "haan ji",
    "hanji",
    "ji",
    "theek",
    "thik",
    "bolo",
    "boliye",
    "ok",
    "okay",
    "ठीक",
    "हाँ",
)

SPOKEN_TEXT_REPLACEMENTS = (
    (r"\bhello\b", "नमस्ते"),
    (r"\bsir\b", "जी"),
    (r"\bproviso group\b", "प्रोविज़ो ग्रुप"),
    (r"\bprovizo group\b", "प्रोविज़ो ग्रुप"),
    (r"\bnavi mumbai\b", "नवी मुंबई"),
    (r"\bkharghar\b", "खारघर"),
    (r"\bpanvel\b", "पनवेल"),
    (r"\bpan\s*vel\b", "पनवेल"),
    (r"\bpan\s*well\b", "पनवेल"),
    (r"\bwhatsapp\b", "व्हाट्सऐप"),
    (r"\bminute\b", "मिनट"),
    (r"\bsite visit\b", "मुलाकात"),
    (r"\bvisit\b", "मुलाकात"),
    (r"\bschedule\b", "तय"),
    (r"\bdetails\b", "जानकारी"),
    (r"\bdetail\b", "जानकारी"),
    (r"\bshare\b", "भेज"),
    (r"\boption\b", "ऑप्शन"),
    (r"\boptions\b", "ऑप्शन्स"),
    (r"\bavailable\b", "मौजूद"),
    (r"\bprojects\b", "प्रोजेक्ट"),
    (r"\bproject\b", "प्रोजेक्ट"),
    (r"\bconnectivity\b", "आवागमन"),
    (r"\bproperty\b", "घर"),
    (r"\benquiry\b", "जानकारी"),
    (r"\bbudget\b", "बजट"),
    (r"\blocation\b", "एरिया"),
    (r"\bmessage\b", "मैसेज"),
    (r"\bmeeting\b", "मुलाकात"),
    (r"\bpreferable\b", "prefer"),
    (r"\bprefer\b", "prefer"),
    (r"\b1\s*bhk\b", "एक बीएचके"),
    (r"\b2\s*bhk\b", "दो बीएचके"),
    (r"\b3\s*bhk\b", "तीन बीएचके"),
    (r"\bsaturday\b", "सैटरडे"),
    (r"\bsunday\b", "संडे"),
    (r"\bthanks\b", "थैंक यू"),
    (r"\bthank you\b", "थैंक यू"),
    (r"\bperfect\b", "परफेक्ट"),
    (r"\bgreat\b", "ग्रेट"),
    (r"रविवार", "संडे"),
)

# Production voice rule: do not transliterate common English words into
# mixed-script text before TTS. The latest call recording proved that these
# replacements made phone audio unintelligible.
SPOKEN_TEXT_REPLACEMENTS = (
    (r"\bprovizo group\b", "Proviso Group"),
    (r"\bproviso group\b", "Proviso Group"),
    (r"\bwhats app\b", "WhatsApp"),
)

UNSAFE_SPOKEN_PATTERNS = [
    r"<[^>]+>",
    r"\blookup_user\b",
    r"\bend_call\b",
    r"\bfunction\b",
    r"\bcall_id\b",
    r"\btool\b",
    r"\bdouble quotes\b",
    r"\bunderscore\b",
    r"\bless than\b",
    r"\bgreater than\b",
    r"\bjson\b",
    r"\bxml\b",
]


def _uses_feminine_self_reference() -> bool:
    configured_gender = os.getenv("AGENT_GENDER", getattr(config, "AGENT_GENDER", "female"))
    return str(configured_gender).strip().lower() in {"female", "feminine", "woman"}


@dataclass
class CallState:
    area: str = ""
    bhk: str = ""
    budget: str = ""
    meeting_day: str = ""
    intro_completed: bool = False
    whatsapp_offered: bool = False
    next_step_confirmed: bool = False
    meeting_offer_made: bool = False
    meeting_confirmed: bool = False
    customer_requested_end: bool = False
    not_interested: bool = False
    last_agent_prompt: str = ""


def _normalize_state_text(text: str) -> str:
    cleaned = str(text or "").casefold()
    # Deepgram/telephony occasionally prepends mojibake/noise characters
    # such as "┬┐Hello?". Remove them before intent classification.
    cleaned = re.sub(r"[┬┐¿¡]+", " ", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_area(text: str) -> str:
    lowered = _normalize_state_text(text)
    if any(token in lowered for token in ("panvel", "पनवेल", "pan vel", "pan well")):
        return "Panvel"
    if "kharghar" in lowered or "खारघर" in lowered:
        return "Kharghar"
    return ""


def _extract_bhk(text: str) -> str:
    lowered = _normalize_state_text(text)
    if any(token in lowered for token in ("2 bhk", "2bhk", "दो बीएचके", "2 बीएचके", "to bhk", "two bhk")):
        return "2 BHK"
    if any(token in lowered for token in ("1 bhk", "1bhk", "एक बीएचके", "1 बीएचके", "one bhk")):
        return "1 BHK"
    if any(token in lowered for token in ("3 bhk", "3bhk", "तीन बीएचके", "3 बीएचके", "three bhk")):
        return "3 BHK"
    return ""


def _extract_meeting_day(text: str) -> str:
    lowered = _normalize_state_text(text)
    if "sunday" in lowered or "रविवार" in lowered or "संडे" in lowered:
        return "Sunday"
    if "saturday" in lowered or "शनिवार" in lowered or "सैटरडे" in lowered:
        return "Saturday"
    return ""


def _contains_affirmative(text: str) -> bool:
    lowered = _normalize_state_text(text)
    if "nahi" in lowered or "नहीं" in lowered:
        return False
    return any(pattern in lowered for pattern in AFFIRMATIVE_PATTERNS)


def _is_short_ack(text: str) -> bool:
    lowered = _normalize_state_text(text)
    return lowered in SHORT_REPLY_REWRITES


def _next_required_step(call_state: CallState | None) -> str:
    if not call_state:
        return ""
    if not call_state.area:
        return "area"
    if not call_state.bhk:
        return "bhk"
    if not call_state.budget:
        return "budget"
    if not call_state.whatsapp_offered:
        return "whatsapp"
    if not call_state.meeting_offer_made:
        return "meeting_offer"
    if call_state.meeting_offer_made and not call_state.meeting_confirmed:
        return "meeting_confirmation"
    return "closing"


def _canonical_question_for_step(call_state: CallState) -> str:
    step = _next_required_step(call_state)
    if step == "area":
        return "आप किस एरिया में घर देख रहे हैं, खारघर या पनवेल?"
    if step == "bhk":
        return "आप एक बीएचके देख रहे हैं या दो बीएचके?"
    if step == "budget":
        if call_state.bhk == "2 BHK":
            return "दो बीएचके के लिए आपका बजट क्या रहेगा?"
        if call_state.bhk == "1 BHK":
            return "एक बीएचके के लिए आपका बजट क्या रहेगा?"
        return "आपका बजट क्या रहेगा?"
    if step == "whatsapp":
        return "ठीक है, मैं आपको व्हाट्सऐप पर कुछ डिटेल्स भेज दूंगी।"
    if step == "meeting_offer":
        return "अगर आप चाहें तो एक विजिट भी तय कर सकते हैं। कौन सा दिन ठीक रहेगा?"
    if step == "meeting_confirmation":
        return "अगर संडे ठीक रहे तो बता दीजिए।"
    return "ठीक है जी।"


def _update_state_from_user_text(text: str, call_state: CallState | None) -> None:
    if not call_state:
        return

    area = _extract_area(text)
    if area:
        call_state.area = area

    bhk = _extract_bhk(text)
    if bhk:
        call_state.bhk = bhk

    if any(token in _normalize_state_text(text) for token in ("crore", "करोड़", "lakh", "लाख")):
        call_state.budget = text.strip()

    meeting_day = _extract_meeting_day(text)
    if meeting_day and call_state.meeting_offer_made:
        call_state.meeting_day = meeting_day
        call_state.meeting_confirmed = True
        call_state.next_step_confirmed = True

    lowered = _normalize_state_text(text)
    if any(pattern in lowered for pattern in END_CALL_PATTERNS):
        call_state.customer_requested_end = True
    if any(token in lowered for token in ("not interested", "nahi chahiye", "नहीं चाहिए")):
        call_state.not_interested = True

    if _contains_affirmative(text):
        if call_state.last_agent_prompt == "intro":
            call_state.intro_completed = True
        elif call_state.last_agent_prompt == "whatsapp":
            call_state.next_step_confirmed = True
        elif call_state.last_agent_prompt == "meeting":
            call_state.meeting_confirmed = True
            call_state.next_step_confirmed = True


def _update_state_from_agent_text(text: str, call_state: CallState | None) -> None:
    if not call_state:
        return

    lowered = _normalize_state_text(text)
    if "दो मिनट बात" in text:
        call_state.last_agent_prompt = "intro"
    elif ("खारघर" in text or "पनवेल" in text or "एरिया" in text) and ("?" in text or "क्या" in text):
        call_state.last_agent_prompt = "area"
    elif ("बीएचके" in text or "bhk" in lowered) and ("?" in text or "क्या" in text):
        call_state.last_agent_prompt = "bhk"
    elif "बजट" in text and ("?" in text or "क्या" in text):
        call_state.last_agent_prompt = "budget"
    elif "व्हाट्सऐप" in text:
        call_state.whatsapp_offered = True
        call_state.last_agent_prompt = "whatsapp"
    elif any(token in lowered for token in ("कब मिल", "कौन सा दिन", "which day", "किस दिन")):
        call_state.meeting_offer_made = True
        call_state.last_agent_prompt = "meeting"

    if any(token in lowered for token in ("मुलाकात पक्की", "मिलते हैं", "संडे के दिन मिलेंगे")):
        call_state.meeting_offer_made = True


def _guard_spoken_text(cleaned: str, call_state: CallState | None) -> str:
    if not call_state or not cleaned:
        return cleaned

    lowered = _normalize_state_text(cleaned)
    next_step = _next_required_step(call_state)

    if call_state.last_agent_prompt == "intro" and any(
        phrase in lowered
        for phrase in ("proviso group ke bare", "proviso group ke baare", "aapka naam", "नाम कुंदन")
    ):
        return _canonical_question_for_step(call_state)

    if next_step == "area" and ("खारघर" in cleaned or "पनवेल" in cleaned or "एरिया" in cleaned):
        return _canonical_question_for_step(call_state)

    if next_step == "bhk" and ("बीएचके" in cleaned or "bhk" in lowered):
        return _canonical_question_for_step(call_state)

    if next_step == "budget" and "बजट" in cleaned:
        return _canonical_question_for_step(call_state)

    if call_state.bhk == "2 BHK" and re.search(r"(एक बीएचके|1 bhk).*(बजट|budget)", cleaned, flags=re.IGNORECASE):
        return "दो बीएचके के लिए आपका बजट क्या रहेगा?"

    if call_state.bhk == "1 BHK" and re.search(r"(दो बीएचके|2 bhk).*(बजट|budget)", cleaned, flags=re.IGNORECASE):
        return "एक बीएचके के लिए आपका बजट क्या रहेगा?"

    if not call_state.meeting_confirmed and re.search(
        r"(संडे|सैटरडे|शनिवार|रविवार).*(मिलेंगे|मुलाकात पक्की|मिलते हैं)",
        cleaned,
        flags=re.IGNORECASE,
    ):
        if call_state.whatsapp_offered:
            return "अगर संडे ठीक रहे तो बता दीजिए, मैं डिटेल्स व्हाट्सऐप कर दूंगी।"
        return "मैं पहले आपको डिटेल्स व्हाट्सऐप कर दूंगी।"

    if not call_state.meeting_confirmed and any(
        token in lowered for token in ("अलविदा", "बाय", "bye", "thank you")
    ):
        if not (call_state.customer_requested_end or call_state.not_interested or call_state.next_step_confirmed):
            return ""

    return cleaned


@dataclass
class CallState:
    lead_disposition: str | None = None
    interest_level: str | None = None
    objection_type: str | None = None
    non_interest_reason: str | None = None
    next_action: str | None = None
    last_turn_type: str | None = None
    last_question_type: str | None = None
    last_direct_answer: str | None = None
    last_semantic_summary: str | None = None
    semantic_confidence: float | None = None
    language_mode: str = "english"
    confusion_count: int = 0
    noise_turn_count: int = 0
    last_audio_repair: str | None = None
    bhk: str | None = None
    timeline: str | None = None
    visited: bool | None = None
    budget: str | None = None
    location: str | None = None
    intent: str | None = None
    current_step: str = "interest"
    last_question: str | None = None
    asked_steps: set[str] = field(default_factory=set)
    step_attempts: dict[str, int] = field(default_factory=dict)
    step_repair_counts: dict[str, int] = field(default_factory=dict)
    customer_requested_end: bool = False
    not_interested: bool = False
    done_reason: str | None = None
    user_turn_count: int = 0

    def public_state(self) -> dict:
        return {
            "lead_disposition": self.lead_disposition,
            "interest_level": self.interest_level,
            "objection_type": self.objection_type,
            "non_interest_reason": self.non_interest_reason,
            "next_action": self.next_action,
            "last_turn_type": self.last_turn_type,
            "last_question_type": self.last_question_type,
            "last_direct_answer": self.last_direct_answer,
            "last_semantic_summary": self.last_semantic_summary,
            "semantic_confidence": self.semantic_confidence,
            "language_mode": self.language_mode,
            "confusion_count": self.confusion_count,
            "noise_turn_count": self.noise_turn_count,
            "last_audio_repair": self.last_audio_repair,
            "bhk": self.bhk,
            "timeline": self.timeline,
            "visited": self.visited,
            "budget": self.budget,
            "location": self.location,
            "intent": self.intent,
            "current_step": self.current_step,
            "last_question": self.last_question,
            "step_attempts": self.step_attempts,
            "step_repair_counts": self.step_repair_counts,
            "done_reason": self.done_reason,
            "user_turn_count": self.user_turn_count,
        }


@dataclass
class NormalizedUserInput:
    text: str
    lead_disposition: str | None = None
    interest_level: str | None = None
    objection_type: str | None = None
    non_interest_reason: str | None = None
    next_action: str | None = None
    bhk: str | None = None
    timeline: str | None = None
    visited: bool | None = None
    budget: str | None = None
    location: str | None = None
    intent: str | None = None
    wants_end: bool = False
    not_interested: bool = False
    soft_rejection: bool = False
    hello_repair: bool = False
    confusion: bool = False
    identity_question: bool = False
    ai_identity_question: bool = False
    call_purpose_question: bool = False
    short_ack: bool = False
    language_request: bool = False
    audio_confusion: bool = False
    stt_noise: bool = False
    hard_stop: bool = False
    business_query: str | None = None


class SalesTurnAnalysis(BaseModel):
    turn_type: str = Field(default="")
    question_type: str = Field(default="")
    disposition: str = Field(default="")
    interest_level: str = Field(default="")
    objection_type: str = Field(default="")
    reason: str = Field(default="")
    next_action: str = Field(default="")
    bhk: str = Field(default="")
    timeline: str = Field(default="")
    visited: str = Field(default="")
    budget: str = Field(default="")
    location: str = Field(default="")
    direct_answer: str = Field(default="")
    semantic_summary: str = Field(default="")
    buyer_signal: str = Field(default="")
    sales_move: str = Field(default="")
    response_strategy: str = Field(default="")
    confidence: float = Field(default=0.0)


QUESTION_INTENTS = {
    "interest": "Find out whether this lead is still looking for a property or not interested.",
    "bhk": "For an interested buyer, ask which configuration they are looking for: 1 BHK, 2 BHK, or 3 BHK.",
    "timeline": "For an interested buyer, ask when they are planning to buy or visit.",
    "engagement": "Answer the buyer's latest question and keep them moving toward WhatsApp details or a visit.",
    "done": "Do not ask more qualification questions. Close based on the lead disposition.",
}

STEP_EXAMPLE_RESPONSES = {
    "interest": "Are you looking for a property?",
    "bhk": "Sure, are you looking for 1 BHK, 2 BHK, or 3 BHK?",
    "timeline": "Got it. When are you planning to buy?",
    "engagement": "Sure, should I send the details on WhatsApp?",
    "done": "No problem, I will update this. Thank you.",
}

OFF_TOPIC_PATTERNS = (
    "are you single",
    "single ho",
    "married",
    "shaadi",
    "boyfriend",
    "girlfriend",
    "what are you doing",
    "kya kar rahi",
    "where do you live",
    "aap kaha rehte",
    "your age",
    "kitni age",
    "personal",
    "date pe",
    "i love you",
    "love you",
    "luv you",
    "shaadi karogi",
    "marry me",
    "aap cute",
    "you are cute",
    "voice achi",
    "voice acchi",
    "personal number",
    "instagram",
    "insta id",
)

AI_IDENTITY_PATTERNS = (
    "ai ho",
    "ai hai",
    "bot ho",
    "robot ho",
    "machine ho",
    "automated call",
    "virtual assistant",
    "real person",
    "human ho",
)

DEAL_OR_TIMING_OBJECTION_PATTERNS = (
    "good deal",
    "best deal",
    "if deal",
    "if good",
    "maybe earlier",
    "not immediately",
    "not immediate",
    "market down",
    "parents convincing",
)

LOW_CONFIDENCE_SEMANTIC_THRESHOLD = float(
    os.getenv("SEMANTIC_LOW_CONFIDENCE_THRESHOLD", "0.45")
)

VALID_DONE_REASONS = {
    "interested",
    "interested_missing_detail",
    "not_interested",
    "callback_later",
    "wrong_lead",
    "customer_exit",
    "unclear_interest",
}

BUSINESS_QUERY_PATTERNS = (
    "price",
    "pricing",
    "cost",
    "rate",
    "budget",
    "location",
    "area",
    "available",
    "availability",
    "possession",
    "amenities",
    "flat",
    "apartment",
    "site",
    "visit",
    "project",
    "bhk",
    "बीएचके",
    "कीमत",
    "बजट",
    "लोकेशन",
    "एरिया",
    "साइट",
    "विजिट",
    "प्रोजेक्ट",
)

PRICE_QUERY_PATTERNS = (
    "price",
    "pricing",
    "cost",
    "rate",
    "budget",
    "kitna",
    "kitni",
    "कितना",
    "कीमत",
    "बजट",
)

LOCATION_QUERY_PATTERNS = (
    "location",
    "area",
    "where",
    "project",
    "panvel",
    "kharghar",
    "लोकेशन",
    "एरिया",
    "कहाँ",
    "प्रोजेक्ट",
)

DETAIL_QUERY_PATTERNS = (
    "amenities",
    "possession",
    "available",
    "availability",
    "carpet",
    "floor",
    "ready possession",
    "ready to move",
    "सुविधा",
    "पजेशन",
)

INTERESTED_PATTERNS = (
    "interested",
    "looking",
    "searching",
    "need flat",
    "need home",
    "want flat",
    "want home",
    "looking for property",
    "looking for flat",
    "looking for home",
    "property dekh",
    "flat dekh",
    "ghar dekh",
    "home dekh",
    "dekh raha",
    "dekh rahi",
    "dekh rahe",
    "search kar",
    "dhundh raha",
    "dhund raha",
    "dhundh rahi",
    "dhund rahi",
    "property chahiye",
    "flat chahiye",
    "ghar chahiye",
    "enquiry ki thi",
    "enquired",
    "send details",
    "share details",
    "details bhejo",
    "whatsapp",
    "yes i am",
    "haan interested",
    "हाँ interested",
    "इंटरेस्टेड",
)

NON_INTERESTED_PATTERNS = (
    "not interested",
    "no requirement",
    "no need",
    "not looking",
    "already bought",
    "already purchased",
    "purchased",
    "bought",
    "nahi chahiye",
    "nahin chahiye",
    "mat call",
    "do not call",
    "dont call",
    "don't call",
    "गलत नंबर",
    "नहीं चाहिए",
)

SOFT_REJECTION_PATTERNS = (
    "not interested",
    "interest nahi",
    "interested nahi",
    "abhi interested nahi",
    "abhi interest nahi",
    "right now not interested",
    "not looking right now",
    "bas dekh raha",
    "bas dekh rahi",
)

HARD_NON_INTERESTED_PATTERNS = tuple(
    pattern
    for pattern in NON_INTERESTED_PATTERNS
    if pattern not in SOFT_REJECTION_PATTERNS
)

CALLBACK_PATTERNS = (
    "call later",
    "call me later",
    "call back later",
    "callback later",
    "call after",
    "phone later",
    "phone me later",
    "callback",
    "busy",
    "not now",
    "baad mein call",
    "baad me call",
    "baad mein phone",
    "baad me phone",
    "baad mein baat",
    "baad me baat",
    "later baat",
    "later call",
    "abhi busy",
    "kal call",
    "kal phone",
    "tomorrow call",
    "अभी busy",
    "बाद में call",
)

WRONG_LEAD_PATTERNS = (
    "wrong number",
    "galat number",
    "गलत नंबर",
    "who gave my number",
    "did not enquire",
    "never enquired",
    "did not inquire",
    "never inquired",
    "no enquiry",
    "no inquiry",
    "maine enquiry nahi",
    "maine koi enquiry nahi",
    "enquiry nahi ki",
    "koi enquiry nahi",
    "maine inquiry nahi",
    "koi inquiry nahi",
)


def _has_any(text: str, phrases: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(f" {phrase} " in padded or phrase in text for phrase in phrases)


def _is_contextual_positive_answer(text: str) -> bool:
    cleaned = _normalize_state_text(text)
    if not cleaned or cleaned in PERMISSION_ONLY_ACKS:
        return False
    if any(negative in cleaned for negative in ("no", "not", "nahi", "nahin", "mat")):
        return False
    if cleaned in SHORT_REPLY_REWRITES and cleaned not in PERMISSION_ONLY_ACKS:
        return True
    return bool(
        re.fullmatch(
            r"(yes|yeah|yep|haan|han|ha|ji|hanji|haan ji)"
            r"(?:\s+(yes|yeah|yep|haan|han|ha|ji|hanji|is|it|ok|okay|theek|thik))*",
            cleaned,
        )
    )


def _extract_bhk_from_noisy_stt(text: str) -> str | None:
    cleaned = _normalize_state_text(text)
    compact = cleaned.replace(" ", "")
    bhk_word = r"(?:b\s*h\s*k|bhk|p\s*h\s*p|php|b\s*hk|beech\s*ke)"
    if re.search(rf"\b(?:1|one|won|ek)\s+(?:theek\s+)?{bhk_word}\b", cleaned):
        return "1 BHK"
    if re.search(rf"\b(?:2|two|too|to|do|who)\s+(?:theek\s+)?{bhk_word}\b", cleaned):
        return "2 BHK"
    if re.search(rf"\b(?:3|three|tree|teen)\s+(?:theek\s+)?{bhk_word}\b", cleaned):
        return "3 BHK"
    # In PSTN Hinglish, "2 BHK" was observed as "PDHK"; treat only the
    # compact full-token form as a BHK answer, not arbitrary words containing it.
    if compact in {"pdhk", "pdhkji", "pdhkjiguess", "tdhk"} or re.search(r"\bpdhk\b", cleaned):
        return "2 BHK"
    return None


def _extract_month_timeline(text: str) -> tuple[str | None, str | None]:
    month_words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
        "eleven": 11,
        "twelve": 12,
        "ek": 1,
        "do": 2,
        "teen": 3,
        "char": 4,
        "chaar": 4,
        "paanch": 5,
        "panch": 5,
        "che": 6,
        "chhe": 6,
        "six": 6,
    }
    pattern = (
        r"\b(?:after|in|within|around|lagbhag|karib|tak|ke baad)?\s*"
        r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
        r"ek|do|teen|char|chaar|paanch|panch|che|chhe)\s*"
        r"(?:months?|month|mahine|mahina)\b"
    )
    match = re.search(pattern, text)
    if not match:
        return None, None

    raw_value = match.group(1)
    month_count = int(raw_value) if raw_value.isdigit() else month_words.get(raw_value)
    if not month_count:
        return None, None

    timeline = f"{month_count} months"
    if month_count <= 2:
        return timeline, "high"
    if month_count <= 4:
        return timeline, "medium"
    return timeline, "low"


def _extract_budget_signal(text: str) -> str | None:
    budget_context = any(
        token in text
        for token in (
            "emi",
            "budget",
            "lakh",
            "lac",
            "crore",
            "manageable",
            "afford",
            "price",
            "cost",
        )
    )
    if budget_context:
        numbers = re.findall(r"\b\d{1,3}\b", text)
        if len(numbers) >= 2:
            prefix = "EMI " if "emi" in text else ""
            return f"{prefix}{numbers[0]}-{numbers[1]}".strip()
        if len(numbers) == 1 and len(numbers[0]) > 1:
            prefix = "EMI " if "emi" in text else ""
            return f"{prefix}{numbers[0]}".strip()

    budget_match = re.search(
        r"\b(?:emi\s*)?(?:around|approx|lagbhag|karib|ke aas paas|aas paas)?\s*"
        r"(\d{1,3}(?:\s*[-to]+\s*\d{1,3})?)\s*"
        r"(?:lakh|lac|lakhs|cr|crore|k|thousand|emi)?\b",
        text,
        flags=re.IGNORECASE,
    )
    if not budget_match:
        return None
    raw = re.sub(r"\s+", " ", budget_match.group(0)).strip()
    if not re.search(r"\d", raw):
        return None
    if len(raw) <= 2 and "emi" not in text and "budget" not in text:
        return None
    return raw


def _extract_location_signal(text: str) -> str | None:
    known_locations = (
        ("gurgaon", "Gurgaon"),
        ("gurugram", "Gurgaon"),
        ("noida", "Noida"),
        ("panvel", "Panvel"),
        ("navi mumbai", "Navi Mumbai"),
        ("kharghar", "Kharghar"),
        ("thane", "Thane"),
        ("mumbai", "Mumbai"),
        ("pune", "Pune"),
        ("bangalore", "Bangalore"),
        ("bengaluru", "Bangalore"),
    )
    matches = []
    for token, canonical in known_locations:
        if token in text and canonical not in matches:
            matches.append(canonical)
    return ", ".join(matches) if matches else None


def is_off_topic_user_input(text: str, normalized: NormalizedUserInput | None = None) -> bool:
    cleaned = _normalize_state_text(text)
    if any(pattern in cleaned for pattern in OFF_TOPIC_PATTERNS):
        return True

    if normalized and (
        normalized.identity_question
        or normalized.ai_identity_question
        or normalized.call_purpose_question
        or normalized.hello_repair
        or normalized.confusion
        or normalized.short_ack
    ):
        return False

    if normalized and any(
        [
            normalized.bhk,
            normalized.timeline,
            normalized.visited is not None,
            normalized.budget,
            normalized.location,
            normalized.intent,
            normalized.lead_disposition,
            normalized.next_action,
            normalized.wants_end,
            normalized.not_interested,
            normalized.business_query,
        ]
    ):
        return False

    if any(
        pattern in cleaned
        for pattern in (
            CONFUSION_PATTERNS
            + HELLO_REPAIR_PATTERNS
            + IDENTITY_QUESTION_PATTERNS
            + AI_IDENTITY_PATTERNS
            + CALL_PURPOSE_QUESTION_PATTERNS
        )
    ):
        return False
    if cleaned in SHORT_REPLY_REWRITES:
        return False
    if any(pattern in cleaned for pattern in QUESTION_PATTERNS + PROJECT_QUERY_PATTERNS):
        return False
    if any(pattern in cleaned for pattern in BUSINESS_QUERY_PATTERNS):
        return False

    words = cleaned.split()
    return len(words) >= 4


def _has_extracted_business_signal(normalized: NormalizedUserInput | None) -> bool:
    if not normalized:
        return False
    return any(
        [
            normalized.lead_disposition,
            normalized.interest_level,
            normalized.objection_type,
            normalized.non_interest_reason,
            normalized.next_action,
            normalized.bhk,
            normalized.timeline,
            normalized.visited is not None,
            normalized.budget,
            normalized.location,
            normalized.intent,
            normalized.wants_end,
            normalized.not_interested,
            normalized.hard_stop,
            normalized.business_query,
        ]
    )


def is_repair_or_permission_turn(normalized: NormalizedUserInput | None) -> bool:
    if not normalized:
        return False
    if _has_extracted_business_signal(normalized):
        return False
    return any(
        [
            normalized.hello_repair,
            normalized.confusion,
            normalized.identity_question,
            normalized.ai_identity_question,
            normalized.call_purpose_question,
            normalized.short_ack,
            normalized.language_request,
            normalized.audio_confusion,
            normalized.stt_noise,
        ]
    )


def normalize_user_input(text: str) -> NormalizedUserInput:
    normalized = _normalize_state_text(text)
    compact = normalized.replace(" ", "")
    result = NormalizedUserInput(text=text.strip())

    result.identity_question = any(pattern in normalized for pattern in IDENTITY_QUESTION_PATTERNS)
    result.ai_identity_question = any(pattern in normalized for pattern in AI_IDENTITY_PATTERNS)
    result.call_purpose_question = any(pattern in normalized for pattern in CALL_PURPOSE_QUESTION_PATTERNS)
    result.language_request = any(pattern in normalized for pattern in LANGUAGE_REQUEST_PATTERNS)
    result.audio_confusion = any(pattern in normalized for pattern in AUDIO_CONFUSION_PATTERNS)
    result.stt_noise = (
        normalized in STT_NOISE_PATTERNS
        or compact in {pattern.replace(" ", "") for pattern in STT_NOISE_PATTERNS}
    )
    result.hard_stop = (
        normalized == "stop"
        or any(pattern in normalized for pattern in HARD_STOP_PATTERNS)
    )
    result.hello_repair = normalized in HELLO_REPAIR_PATTERNS or _has_any(
        normalized,
        ("hello hello", "hello who", "hello who's", "hello kaun", "hello kon"),
    )
    result.confusion = result.audio_confusion or any(pattern in normalized for pattern in CONFUSION_PATTERNS)
    result.short_ack = normalized in SHORT_REPLY_REWRITES

    if result.hard_stop:
        result.lead_disposition = "not_interested"
        result.non_interest_reason = "hard stop or explicit rejection"
        result.next_action = "close"
        result.not_interested = True
        result.wants_end = True
        result.intent = "low"
    elif any(pattern in normalized for pattern in WRONG_LEAD_PATTERNS):
        result.lead_disposition = "wrong_lead"
        result.non_interest_reason = "wrong number or no enquiry"
        result.next_action = "close"
    elif any(pattern in normalized for pattern in CALLBACK_PATTERNS):
        result.lead_disposition = "callback_later"
        result.next_action = "callback"
    elif any(pattern in normalized for pattern in HARD_NON_INTERESTED_PATTERNS):
        result.lead_disposition = "not_interested"
        result.non_interest_reason = "not looking or no requirement"
        result.next_action = "close"
    elif any(pattern in normalized for pattern in SOFT_REJECTION_PATTERNS):
        result.soft_rejection = True
        result.interest_level = "cold"
        result.objection_type = "not_interested_reflex"
        result.non_interest_reason = "soft rejection, needs one clarification"
        result.next_action = "handle_objection"
    elif any(pattern in normalized for pattern in INTERESTED_PATTERNS):
        result.lead_disposition = "interested"
        result.next_action = "capture_requirement"

    if any(pattern in normalized for pattern in PRICE_QUERY_PATTERNS):
        result.business_query = "price"
    elif any(pattern in normalized for pattern in LOCATION_QUERY_PATTERNS + PROJECT_QUERY_PATTERNS):
        result.business_query = "location"
    elif any(pattern in normalized for pattern in DETAIL_QUERY_PATTERNS):
        result.business_query = "project_detail"

    if result.business_query and not result.lead_disposition:
        result.lead_disposition = "interested"
        result.interest_level = "warm"
        result.objection_type = result.business_query
        result.next_action = "handle_objection"

    if _has_any(
        normalized,
        (
            "2 bhk",
            "two bhk",
            "to bhk",
            "two b h k",
            "2 b h k",
            "two beech ke",
            "to beech ke",
            "do bhk",
            "do beech ke",
            "do b h k",
        ),
    ) or "2bhk" in compact:
        result.bhk = "2 BHK"
    elif _has_any(
        normalized,
        (
            "3 bhk",
            "three bhk",
            "3 b h k",
            "three b h k",
            "three beech ke",
            "teen bhk",
            "teen beech ke",
        ),
    ) or "3bhk" in compact:
        result.bhk = "3 BHK"
    elif _has_any(
        normalized,
        (
            "1 bhk",
            "one bhk",
            "1 b h k",
            "one b h k",
            "one beech ke",
            "ek bhk",
            "ek beech ke",
        ),
    ) or "1bhk" in compact:
        result.bhk = "1 BHK"

    noisy_bhk = _extract_bhk_from_noisy_stt(text)
    if noisy_bhk and not result.bhk:
        result.bhk = noisy_bhk

    if result.bhk:
        result.lead_disposition = result.lead_disposition or "interested"
        result.next_action = result.next_action or "capture_timeline"

    month_timeline, month_intent = _extract_month_timeline(normalized)
    if month_timeline:
        result.timeline = month_timeline
        result.intent = month_intent
        result.lead_disposition = result.lead_disposition or "interested"
        result.next_action = result.next_action or (
            "nurture" if month_intent == "low" else "capture_requirement"
        )
    elif _has_any(normalized, ("this month", "current month", "planning soon", "soon", "immediate", "urgent")):
        result.timeline = "soon"
        result.intent = "high"
        result.lead_disposition = result.lead_disposition or "interested"
        result.next_action = result.next_action or "capture_requirement"
    elif _has_any(normalized, ("next month", "in one month", "within month")):
        result.timeline = "next month"
        result.intent = "high"
        result.lead_disposition = result.lead_disposition or "interested"
        result.next_action = result.next_action or "capture_requirement"
    elif _has_any(
        normalized,
        (
            "one year",
            "1 year",
            "in a year",
            "after year",
            "next year",
            "ek saal",
            "एक साल",
            "later",
            "after some time",
            "not now",
            "future",
        ),
    ):
        result.timeline = "later"
        result.intent = result.intent or "low"
        result.lead_disposition = result.lead_disposition or "interested"
        result.next_action = result.next_action or "nurture"
    elif _has_any(normalized, ("just looking", "exploring", "checking", "browsing")):
        result.timeline = "exploring"
        result.intent = "low"
        result.lead_disposition = result.lead_disposition or "interested"
        result.next_action = result.next_action or "nurture"

    if _has_any(normalized, ("already visited", "already seen", "visited", "seen project", "site dekha")):
        result.visited = True
    elif _has_any(normalized, ("not visited", "not seen", "havent visited", "have not visited", "did not visit")):
        result.visited = False

    budget_signal = _extract_budget_signal(normalized)
    if budget_signal:
        result.budget = budget_signal
        result.objection_type = result.objection_type or "budget"

    location_signal = _extract_location_signal(normalized)
    if location_signal:
        result.location = location_signal

    if any(pattern in normalized for pattern in DEAL_OR_TIMING_OBJECTION_PATTERNS):
        result.objection_type = result.objection_type or "timing_or_deal"
        result.next_action = result.next_action or "nurture"

    if any(pattern in normalized for pattern in END_CALL_PATTERNS) and not result.soft_rejection:
        result.wants_end = True
    if _has_any(normalized, ("nahi chahiye", "mat call")):
        result.not_interested = True
        result.intent = "low"
        result.lead_disposition = "not_interested"
        result.non_interest_reason = result.non_interest_reason or "not interested"
        result.next_action = "close"

    return result


def update_state_from_user_input(text: str, call_state: CallState | None) -> NormalizedUserInput:
    normalized = normalize_user_input(text)
    if not call_state:
        return normalized

    if text.strip():
        call_state.user_turn_count += 1

    if normalized.language_request:
        call_state.language_mode = "english"

    if normalized.audio_confusion:
        call_state.language_mode = "english"
        call_state.confusion_count += 1
        call_state.last_audio_repair = "english_simplify"

    if normalized.stt_noise or (normalized.hello_repair and not _has_extracted_business_signal(normalized)):
        call_state.noise_turn_count += 1
        if call_state.noise_turn_count >= 2:
            call_state.language_mode = "english"
            call_state.last_audio_repair = "noise_repair"

    if (
        _normalize_state_text(text) in {"okay thank you", "ok thank you", "thank you"}
        and call_state.confusion_count > 0
    ):
        normalized.hard_stop = True
        normalized.wants_end = True
        normalized.not_interested = True
        normalized.lead_disposition = "not_interested"
        normalized.non_interest_reason = "customer ended after confusion"
        normalized.next_action = "close"

    if normalized.soft_rejection and call_state.objection_type == "not_interested_reflex":
        normalized.not_interested = True
        normalized.lead_disposition = "not_interested"
        normalized.non_interest_reason = "repeated soft rejection"
        normalized.next_action = "close"

    if (
        not normalized.lead_disposition
        and call_state.current_step == "interest"
        and call_state.last_question == "interest"
        and _is_contextual_positive_answer(text)
    ):
        normalized.lead_disposition = "interested"
        normalized.next_action = "capture_requirement"

    if normalized.lead_disposition:
        call_state.lead_disposition = normalized.lead_disposition
    if normalized.interest_level:
        call_state.interest_level = normalized.interest_level
    if normalized.objection_type:
        call_state.objection_type = normalized.objection_type
    if normalized.non_interest_reason:
        call_state.non_interest_reason = normalized.non_interest_reason
    if normalized.next_action:
        call_state.next_action = normalized.next_action
    if normalized.bhk:
        call_state.bhk = normalized.bhk
    if normalized.timeline:
        call_state.timeline = normalized.timeline
    if normalized.visited is not None:
        call_state.visited = normalized.visited
    if normalized.budget:
        call_state.budget = normalized.budget
    if normalized.location:
        call_state.location = normalized.location
    if normalized.intent:
        call_state.intent = normalized.intent
    if normalized.wants_end:
        call_state.customer_requested_end = True
    if normalized.not_interested:
        call_state.not_interested = True
        call_state.lead_disposition = "not_interested"
        call_state.next_action = "close"

    return normalized


def _normalize_analysis_value(value: str | None) -> str:
    return re.sub(r"\s+", "_", str(value or "").strip().lower())


def _semantic_confidence(analysis: SalesTurnAnalysis | None) -> float:
    if not analysis:
        return 0.0
    try:
        return max(0.0, min(1.0, float(analysis.confidence or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _is_low_confidence_semantic(analysis: SalesTurnAnalysis | None) -> bool:
    if not analysis:
        return False
    return _semantic_confidence(analysis) < LOW_CONFIDENCE_SEMANTIC_THRESHOLD


def _is_valid_done_reason(reason: str | None) -> bool:
    return str(reason or "").strip() in VALID_DONE_REASONS


def _action_has_invalid_done(action: dict | None) -> bool:
    if not action:
        return False
    return action.get("current_step") == "done" and not _is_valid_done_reason(
        action.get("done_reason")
    )


def _is_engaged_non_answer_turn(
    call_state: CallState | None,
    normalized: NormalizedUserInput | None,
) -> bool:
    if not call_state:
        return False
    if normalized and (
        normalized.ai_identity_question
        or normalized.identity_question
        or normalized.call_purpose_question
        or normalized.hello_repair
        or normalized.confusion
    ):
        return True
    if normalized and normalized.business_query and call_state.last_turn_type != "answer":
        return True
    if normalized and any(
        pattern in _normalize_state_text(normalized.text)
        for pattern in DEAL_OR_TIMING_OBJECTION_PATTERNS
    ):
        return True
    return call_state.last_turn_type in {
        "business_question",
        "objection",
        "off_topic",
        "identity_question",
        "call_purpose_question",
        "confusion",
        "repair",
    }


def _safe_direct_answer(value: str | None) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "").strip())
    if not cleaned:
        return ""
    cleaned = re.sub(
        r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\U00002700-\U000027BF]",
        "",
        cleaned,
    ).strip()
    if len(cleaned.split()) > 16:
        cleaned = " ".join(cleaned.split()[:16]).rstrip(".,;:!?")
    return cleaned


def _analysis_has_explicit_wrong_lead_signal(analysis: SalesTurnAnalysis) -> bool:
    text = _normalize_state_text(
        " ".join(
            [
                analysis.reason or "",
                analysis.semantic_summary or "",
                analysis.direct_answer or "",
            ]
        )
    )
    explicit_signals = (
        "wrong number",
        "galat number",
        "no enquiry",
        "did not enquire",
        "never enquired",
        "not my number",
        "invalid number",
        "number galat",
        "maine enquiry nahi",
        "denies making enquiry",
        "denies making any enquiry",
        "denies enquiry",
        "claims no enquiry",
    )
    weak_noise = (
        "invalid input",
        "not related",
        "unrelated",
        "unclear",
        "unknown",
        "gibberish",
        "foreign",
    )
    if any(signal in text for signal in explicit_signals):
        return True
    if any(noise in text for noise in weak_noise):
        return False
    return False


def _analysis_has_hard_rejection_signal(analysis: SalesTurnAnalysis) -> bool:
    text = _normalize_state_text(
        " ".join(
            [
                analysis.reason or "",
                analysis.semantic_summary or "",
                analysis.direct_answer or "",
            ]
        )
    )
    hard_signals = (
        "do not call",
        "dont call",
        "don t call",
        "do not want to talk",
        "dont want to talk",
        "don't want to talk",
        "don t want to talk",
        "stop calling",
        "stop the call",
        "stop this call",
        "remove my number",
        "mat call",
        "nahi chahiye",
        "nahin chahiye",
        "no requirement",
        "no need",
        "not looking",
        "already bought",
        "already purchased",
        "purchased already",
        "bought already",
    )
    return any(signal in text for signal in hard_signals)


def _analysis_is_whatsapp_details_request(analysis: SalesTurnAnalysis) -> bool:
    text = _normalize_state_text(
        " ".join(
            [
                analysis.reason or "",
                analysis.semantic_summary or "",
                analysis.direct_answer or "",
                analysis.next_action or "",
            ]
        )
    )
    return any(token in text for token in ("whatsapp", "brochure", "details", "send_details", "send brochure"))


def _controlled_direct_answer(analysis: SalesTurnAnalysis) -> str:
    turn_type = _normalize_analysis_value(analysis.turn_type)
    question_type = _normalize_analysis_value(analysis.question_type)
    objection_type = _normalize_analysis_value(analysis.objection_type)
    direct_answer = _safe_direct_answer(analysis.direct_answer)
    unsafe_phrases = (
        "best offer",
        "best offers",
        "best price",
        "discount",
        "lowest price",
        "guarantee",
        "available hai",
        "availability hai",
        "book now",
        "limited time",
        "market down",
        "market abhi down",
    )
    direct_answer_is_safe = bool(direct_answer) and not any(
        phrase in direct_answer.lower() for phrase in unsafe_phrases
    )

    if question_type == "call_purpose" or turn_type == "call_purpose_question":
        return "This is about your property enquiry"
    if question_type == "identity" or turn_type == "identity_question":
        return "This is Riya from Proviso Group"
    if question_type == "personal":
        return "Let's stay with the property enquiry"
    if question_type == "price" or objection_type in {"price", "price_sensitive", "budget"}:
        return "I understand, budget is important"
    if objection_type in {"family", "family_concern", "parents", "parent_concern"}:
        return "I understand the family concern"
    if _analysis_is_whatsapp_details_request(analysis):
        return "I can send details on WhatsApp"
    if "market" in _normalize_state_text(
        " ".join([analysis.semantic_summary or "", analysis.direct_answer or "", analysis.reason or ""])
    ):
        return "Market conditions keep changing"
    if objection_type == "not_interested_reflex":
        return "I understand, just confirming one thing"
    if question_type == "location":
        return "I can share location details"
    if question_type in {"project", "availability"}:
        return "I can share details after your requirement"
    if turn_type in {"objection", "business_question", "off_topic"} and direct_answer_is_safe:
        return direct_answer
    if turn_type == "callback_request":
        return "Sure, I will call later"
    if turn_type == "off_topic":
        return "Let's stay with the property enquiry"
    if turn_type == "confusion":
        return "Sorry, I will keep it simple"

    if direct_answer_is_safe:
        return direct_answer
    return ""


def apply_sales_turn_analysis(call_state: CallState | None, analysis: SalesTurnAnalysis | None) -> None:
    if not call_state:
        return
    if not analysis:
        call_state.last_direct_answer = None
        call_state.semantic_confidence = 0.0
        return

    turn_type = _normalize_analysis_value(analysis.turn_type)
    question_type = _normalize_analysis_value(analysis.question_type)
    if question_type == "personal":
        turn_type = "off_topic"

    if turn_type == "wrong_lead" and not _analysis_has_explicit_wrong_lead_signal(analysis):
        turn_type = "confusion"
    if turn_type == "rejection" and not _analysis_has_hard_rejection_signal(analysis):
        turn_type = "objection"
        if not analysis.objection_type:
            analysis.objection_type = "not_interested_reflex"
        if not analysis.next_action:
            analysis.next_action = "handle_objection"
    if turn_type == "callback_request" and _analysis_is_whatsapp_details_request(analysis):
        turn_type = "business_question"
        question_type = "other"
        analysis.disposition = analysis.disposition or "interested"
        analysis.next_action = "send_brochure"

    low_confidence = _is_low_confidence_semantic(analysis)
    explicit_final_signal = (
        _analysis_has_explicit_wrong_lead_signal(analysis)
        or turn_type == "callback_request"
        or (turn_type == "rejection" and _analysis_has_hard_rejection_signal(analysis))
    )
    explicit_behavior_signal = turn_type == "off_topic" and question_type == "personal"
    if low_confidence and not explicit_final_signal and not explicit_behavior_signal:
        # A low-confidence semantic read is useful only as a repair signal.
        # It must not mutate disposition, next_action, BHK, timeline, or close
        # the call; that was the root cause of the post-greeting silence.
        turn_type = "repair"
        question_type = "none"

    if turn_type in {
        "answer",
        "permission",
        "repair",
        "identity_question",
        "call_purpose_question",
        "business_question",
        "objection",
        "off_topic",
        "rejection",
        "callback_request",
        "wrong_lead",
        "confusion",
        "unclear",
    }:
        call_state.last_turn_type = turn_type

    if question_type in {
        "none",
        "identity",
        "call_purpose",
        "price",
        "location",
        "project",
        "availability",
        "callback",
        "personal",
        "other",
    }:
        call_state.last_question_type = question_type

    direct_answer = (
        ""
        if low_confidence and not explicit_final_signal and not explicit_behavior_signal
        else _controlled_direct_answer(analysis)
    )
    if direct_answer:
        call_state.last_direct_answer = direct_answer

    semantic_summary = _safe_direct_answer(analysis.semantic_summary)
    if semantic_summary:
        call_state.last_semantic_summary = semantic_summary

    call_state.semantic_confidence = _semantic_confidence(analysis)

    if low_confidence and not explicit_final_signal and not explicit_behavior_signal:
        return

    disposition = _normalize_analysis_value(analysis.disposition)
    if disposition == "wrong_lead" and not _analysis_has_explicit_wrong_lead_signal(analysis):
        disposition = ""
    if disposition == "not_interested" and turn_type == "objection" and not _analysis_has_hard_rejection_signal(analysis):
        disposition = ""
    # "unclear" from the semantic layer means "ask again / repair"; it is not
    # a final lead disposition until the deterministic controller exhausts
    # real attempts. Otherwise one noisy STT turn can prematurely end a call.
    if disposition in {"interested", "not_interested", "callback_later", "wrong_lead"}:
        call_state.lead_disposition = disposition
    if turn_type == "callback_request":
        call_state.lead_disposition = "callback_later"
        call_state.next_action = "callback"
    elif turn_type == "wrong_lead" and _analysis_has_explicit_wrong_lead_signal(analysis):
        call_state.lead_disposition = "wrong_lead"
        call_state.next_action = "close"
    elif turn_type == "rejection":
        call_state.lead_disposition = disposition if disposition == "not_interested" else "not_interested"
        call_state.next_action = "close"

    interest_level = _normalize_analysis_value(analysis.interest_level)
    if interest_level in {"hot", "warm", "nurture", "cold", "unknown"}:
        call_state.interest_level = interest_level
        if not call_state.intent:
            call_state.intent = {
                "hot": "high",
                "warm": "medium",
                "nurture": "low",
                "cold": "low",
            }.get(interest_level)

    objection_type = _normalize_analysis_value(analysis.objection_type)
    if objection_type and objection_type != "none":
        call_state.objection_type = objection_type

    reason = _normalize_field(analysis.reason)
    if reason and disposition in {"not_interested", "wrong_lead", "unclear"}:
        call_state.non_interest_reason = reason

    next_action = _normalize_analysis_value(analysis.next_action)
    if next_action:
        call_state.next_action = next_action

    bhk = _normalize_field(analysis.bhk).upper().replace(" ", "")
    if bhk in {"1", "2", "3"}:
        bhk = f"{bhk}BHK"
    if bhk in {"1BHK", "2BHK", "3BHK"}:
        call_state.bhk = bhk.replace("BHK", " BHK")
        call_state.lead_disposition = call_state.lead_disposition or "interested"

    timeline = _normalize_field(analysis.timeline)
    if timeline:
        call_state.timeline = timeline
        call_state.lead_disposition = call_state.lead_disposition or "interested"

    visited = _normalize_analysis_value(analysis.visited)
    if visited in {"yes", "true", "visited", "already_visited"}:
        call_state.visited = True
    elif visited in {"no", "false", "not_visited"}:
        call_state.visited = False

    budget = _normalize_field(analysis.budget)
    if budget:
        call_state.budget = budget

    location = _normalize_field(analysis.location)
    if location:
        existing_locations = {
            part.strip().casefold()
            for part in (call_state.location or "").split(",")
            if part.strip()
        }
        new_locations = [
            part.strip()
            for part in re.split(r"[,/]| and ", location)
            if part.strip()
        ]
        merged = [part.strip() for part in (call_state.location or "").split(",") if part.strip()]
        for loc in new_locations:
            if loc.casefold() not in existing_locations:
                merged.append(loc)
                existing_locations.add(loc.casefold())
        call_state.location = ", ".join(merged)

    if call_state.lead_disposition in {"not_interested", "wrong_lead"}:
        call_state.not_interested = True
        call_state.next_action = call_state.next_action or "close"


def decide_next_action(call_state: CallState, normalized: NormalizedUserInput | None = None) -> dict:
    flow = ("interest", "bhk", "timeline")
    max_step_attempts = int(os.getenv("CONTROLLER_MAX_STEP_ATTEMPTS", "2"))
    max_step_repair_attempts = int(os.getenv("CONTROLLER_MAX_STEP_REPAIR_ATTEMPTS", "2"))
    semantic_repair_turn = call_state.last_turn_type in {
        "permission",
        "repair",
        "identity_question",
        "call_purpose_question",
        "confusion",
    }
    repair_or_permission_turn = is_repair_or_permission_turn(normalized) or semantic_repair_turn
    unclear_repair_turn = bool(
        normalized
        and (
            normalized.stt_noise
            or normalized.audio_confusion
            or normalized.confusion
            or normalized.hello_repair
        )
    ) or call_state.last_turn_type in {"repair", "confusion"}
    engaged_non_answer_turn = _is_engaged_non_answer_turn(call_state, normalized)
    should_count_step_attempt = not (repair_or_permission_turn or engaged_non_answer_turn)

    if call_state.customer_requested_end:
        call_state.current_step = "done"
        call_state.last_question = None
        call_state.done_reason = call_state.lead_disposition or "customer_exit"
        return {
            "current_step": "done",
            "next_question_intent": "Close politely because the customer wants to end the call.",
            "should_ask": False,
            "already_asked": False,
            "done_reason": call_state.done_reason,
        }

    if call_state.lead_disposition in {"not_interested", "wrong_lead", "callback_later"} or call_state.not_interested:
        call_state.current_step = "done"
        call_state.last_question = None
        call_state.done_reason = call_state.lead_disposition or "not_interested"
        if call_state.lead_disposition == "callback_later":
            intent = "Acknowledge that this lead should be called later. Close politely."
        elif call_state.lead_disposition == "wrong_lead":
            intent = "Acknowledge the wrong lead or no-enquiry signal. Close politely."
        else:
            intent = "Acknowledge that the customer is not interested. Close politely."
        return {
            "current_step": "done",
            "next_question_intent": intent,
            "should_ask": False,
            "already_asked": False,
            "done_reason": call_state.done_reason,
        }

    if call_state.confusion_count >= 2 or call_state.noise_turn_count >= 4:
        call_state.current_step = "done"
        call_state.last_question = None
        call_state.done_reason = "unclear_interest"
        call_state.next_action = "close"
        return {
            "current_step": "done",
            "next_question_intent": (
                "Close politely because the customer cannot understand the call audio. "
                "Do not ask another sales question."
            ),
            "should_ask": False,
            "already_asked": False,
            "done_reason": "unclear_interest",
        }

    for step in flow:
        if step == "interest":
            if call_state.lead_disposition:
                continue
        elif getattr(call_state, step) is not None:
            continue

        if step in {"bhk", "timeline"} and call_state.lead_disposition != "interested":
            continue

        attempts = call_state.step_attempts.get(step, 0)
        if attempts >= max_step_attempts and should_count_step_attempt:
            call_state.current_step = "done"
            call_state.last_question = None
            if step == "interest":
                call_state.lead_disposition = "unclear"
                call_state.done_reason = "unclear_interest"
                intent = (
                    "Close politely because interest could not be confirmed after two short attempts. "
                    "Do not mark as interested."
                )
            else:
                call_state.done_reason = "interested_missing_detail"
                call_state.next_action = call_state.next_action or "manual_follow_up"
                intent = (
                    "Close warmly as an interested lead with incomplete details. "
                    "Do not ask another qualification question."
                )
            return {
                "current_step": "done",
                "next_question_intent": intent,
                "should_ask": False,
                "already_asked": True,
                "done_reason": call_state.done_reason,
            }

        already_asked = attempts > 0 or step in call_state.asked_steps
        if already_asked and unclear_repair_turn and step in {"bhk", "timeline"}:
            repair_attempts = call_state.step_repair_counts.get(step, 0) + 1
            call_state.step_repair_counts[step] = repair_attempts
            if repair_attempts >= max_step_repair_attempts:
                call_state.current_step = "done"
                call_state.last_question = None
                call_state.done_reason = "interested_missing_detail"
                call_state.next_action = call_state.next_action or "manual_follow_up"
                return {
                    "current_step": "done",
                    "next_question_intent": (
                        "Close warmly as an interested lead with unclear audio. "
                        "Do not repeat the same qualification question."
                    ),
                    "should_ask": False,
                    "already_asked": True,
                    "done_reason": call_state.done_reason,
                }

        call_state.current_step = step
        call_state.last_question = step
        if should_count_step_attempt:
            call_state.asked_steps.add(step)
            call_state.step_attempts[step] = attempts + 1
            call_state.step_repair_counts[step] = 0
        question_intent = QUESTION_INTENTS[step]
        if already_asked and should_count_step_attempt:
            question_intent += " Rephrase it because the earlier answer did not contain this detail."
        return {
            "current_step": step,
            "next_question_intent": question_intent,
            "should_ask": True,
            "already_asked": already_asked,
            "done_reason": None,
        }

    if engaged_non_answer_turn and not (normalized and normalized.wants_end):
        call_state.current_step = "engagement"
        call_state.last_question = "engagement"
        call_state.done_reason = None
        return {
            "current_step": "engagement",
            "next_question_intent": (
                "Answer the customer's latest real-estate question safely, keep the lead warm, "
                "and ask one light next-step question only if useful."
            ),
            "should_ask": True,
            "already_asked": False,
            "done_reason": None,
        }

    call_state.current_step = "done"
    call_state.last_question = None
    call_state.done_reason = call_state.lead_disposition or "interested"
    call_state.next_action = call_state.next_action or "send_details_or_follow_up"
    return {
        "current_step": "done",
        "next_question_intent": QUESTION_INTENTS["done"],
        "should_ask": False,
        "already_asked": False,
        "done_reason": call_state.done_reason,
    }


def build_controller_context(
    call_state: CallState,
    action: dict,
    normalized: NormalizedUserInput,
    latest_user_text: str = "",
    sales_analysis: SalesTurnAnalysis | None = None,
    off_topic: bool = False,
) -> str:
    question_intent = action["next_question_intent"]
    user_turn_type = "answer"
    response_directive = (
        "Use the semantic analysis as the customer's meaning. "
        "Acknowledge briefly in simple Indian English, then follow next_question_intent."
    )
    semantic_turn_type = call_state.last_turn_type or ""

    if normalized.hard_stop:
        user_turn_type = "hard_stop"
        response_directive = "Respect the rejection. Close politely in simple English. Do not ask another question."
    elif normalized.language_request:
        user_turn_type = "language_request"
        response_directive = "Switch to simple English immediately. Ask only the controller's current_step."
    elif normalized.audio_confusion or normalized.stt_noise:
        user_turn_type = "audio_confusion"
        response_directive = (
            "Apologize once, use simple English, and avoid repeating the same phrase. "
            "If done, close politely."
        )
    elif normalized.identity_question:
        user_turn_type = "identity_question"
        response_directive = (
            "Answer identity first: you are calling from Proviso Group. "
            "Then ask the controller's current_step in simple English."
        )
    elif normalized.ai_identity_question:
        user_turn_type = "ai_identity_question"
        response_directive = (
            "Answer transparently: you are Proviso Group's virtual assistant. "
            "Then ask the controller's current_step in the same short response."
        )
    elif normalized.call_purpose_question:
        user_turn_type = "call_purpose_question"
        response_directive = (
            "Answer the call reason first: this is regarding their property enquiry. "
            "Then ask only the controller's current_step."
        )
    elif normalized.hello_repair:
        user_turn_type = "hello_after_silence"
        response_directive = (
            "Re-engage immediately: confirm you are on the line, then ask the controller's current_step."
        )
    elif normalized.confusion:
        user_turn_type = "confusion"
        response_directive = (
            "Apologize briefly, switch to simple English, then ask only the controller's current_step."
        )
    elif normalized.business_query:
        user_turn_type = f"business_query:{normalized.business_query}"
        if normalized.business_query == "price":
            response_directive = (
                "Acknowledge budget concern. Say exact price depends on requirement. "
                "Then ask only the controller's current_step."
            )
        elif normalized.business_query == "location":
            response_directive = (
                "Answer safely: say location details depend on the project/context; do not invent areas. "
                "Then ask only the controller's current_step."
            )
        else:
            response_directive = (
                "Answer safely: details can be shared after basic requirement is clear. "
                "Then ask only the controller's current_step."
            )
    elif normalized.short_ack:
        user_turn_type = "short_ack"
        response_directive = "Treat this as listening/permission to continue. Ask the controller's current_step."
    elif semantic_turn_type == "objection":
        user_turn_type = "objection"
        response_directive = (
            "Do not argue. Acknowledge the real concern from analysis, give one safe reframe, "
            "then ask only the controller's current_step."
        )
    elif semantic_turn_type == "business_question":
        user_turn_type = "business_question"
        response_directive = (
            "Answer the business question safely without inventing facts, then ask only the controller's current_step."
        )
    elif semantic_turn_type == "off_topic":
        user_turn_type = "off_topic"
        response_directive = (
            "Set a friendly professional boundary in simple English; do not flirt back. "
            "Redirect to the controller's current_step."
        )

    if off_topic and action["should_ask"]:
        user_turn_type = "off_topic"
        question_intent = (
            "Lightly acknowledge the off-topic comment, then immediately redirect to this step: "
            f"{question_intent}"
        )
        response_directive = (
            "Use a light professional boundary, no flirting back, and redirect to the controller's current_step."
        )

    if action["current_step"] == "engagement":
        response_directive = (
            "Answer the latest buyer question safely using known state only. "
            "Do not invent price, possession, availability, broker status, or market claims. "
            "End with one useful next-step question such as WhatsApp details or site visit."
        )

    if not action["should_ask"]:
        if action.get("done_reason") in {"interested", "interested_missing_detail"}:
            response_directive = "Mark as interested, close warmly, and use end_call. Do not ask more."
        elif action.get("done_reason") == "callback_later":
            response_directive = "Mark as callback later, acknowledge briefly, and use end_call."
        elif action.get("done_reason") == "not_interested":
            response_directive = "Mark as not interested, acknowledge politely, and use end_call."
        elif action.get("done_reason") == "wrong_lead":
            response_directive = "Mark as wrong lead, apologize briefly, and use end_call."
        elif action.get("done_reason") == "unclear_interest":
            response_directive = "Mark as unclear/contacted, close politely, and use end_call."
        else:
            response_directive = "Close politely and use end_call. Do not ask another question."

    compact_state = {
        key: value
        for key, value in {
            "disposition": call_state.lead_disposition,
            "interest": call_state.interest_level or call_state.intent,
            "objection": call_state.objection_type,
            "next_action": call_state.next_action,
            "turn_type": call_state.last_turn_type,
            "question_type": call_state.last_question_type,
            "direct_answer": call_state.last_direct_answer,
            "bhk": call_state.bhk,
            "timeline": call_state.timeline,
            "visited": call_state.visited,
            "budget": call_state.budget,
            "location": call_state.location,
            "step": call_state.current_step,
            "turns": call_state.user_turn_count,
            "done": call_state.done_reason,
            "language": call_state.language_mode,
            "confusions": call_state.confusion_count,
            "noise": call_state.noise_turn_count,
        }.items()
        if value not in (None, "", {}, [])
    }
    compact_analysis = {}
    if sales_analysis:
        compact_analysis = {
            key: value
            for key, value in sales_analysis.model_dump().items()
            if value not in (None, "", {}, [])
        }
        if call_state.last_direct_answer:
            compact_analysis["direct_answer"] = call_state.last_direct_answer
    latest = latest_user_text.strip()
    if len(latest) > 180:
        latest = latest[:180].rsplit(" ", 1)[0].strip()

    return (
        "Controller block. Phrase only this action; do not choose flow.\n"
        f"step={action['current_step']}\n"
        f"state={json.dumps(compact_state, ensure_ascii=False, separators=(',', ':'))}\n"
        f"last_user={json.dumps(latest, ensure_ascii=False)}\n"
        f"analysis={json.dumps(compact_analysis, ensure_ascii=False, separators=(',', ':'))}\n"
        f"type={user_turn_type}\n"
        f"directive={response_directive}\n"
        f"next={question_intent}\n"
        "Rules: <=18 words, one question max, simple Indian English, no facts you do not know. "
        "No Hindi script. No repeated fallback. Use one clear sentence when possible."
    )


def render_controller_fallback_response(
    call_state: CallState,
    action: dict | None,
    normalized: NormalizedUserInput | None,
    off_topic: bool = False,
) -> str:
    step = (action or {}).get("current_step") or call_state.current_step
    done_reason = (action or {}).get("done_reason") or call_state.done_reason

    if step == "done" and not _is_valid_done_reason(done_reason):
        step = "interest"
        done_reason = None

    if step == "done" or done_reason:
        if done_reason == "callback_later":
            return "Sure, I will call later. Thank you."
        if done_reason == "wrong_lead":
            return "Sorry, I will update the number. Thank you."
        if done_reason == "not_interested":
            return "No problem, I will not continue. Thank you."
        if done_reason == "unclear_interest":
            return "Sorry, I will not continue. Thank you."
        if done_reason in {"interested", "interested_missing_detail"}:
            return "Thanks, I will send the details on WhatsApp."
        return "No problem, I will update this. Thank you."

    if normalized and normalized.hard_stop:
        return "No problem, I will not continue. Thank you."

    if normalized and normalized.language_request:
        if step == "bhk":
            return "Sure, I will speak in English. Which BHK are you looking for?"
        if step == "timeline":
            return "Sure, I will speak in English. When are you planning to buy?"
        return "Sure, I will speak in English. Are you looking for a property?"

    if normalized and (normalized.audio_confusion or normalized.stt_noise):
        if call_state.confusion_count >= 2 or call_state.noise_turn_count >= 4:
            return "Sorry, I will not continue. Thank you."
        if step == "bhk":
            return "Sorry, one, two, or three BHK?"
        if step == "timeline":
            return "Sorry, when are you planning to buy?"
        return "Sorry, I will keep it simple. Are you looking for a property?"

    if normalized and normalized.ai_identity_question:
        if step == "bhk":
            return "Yes, I am Proviso Group's virtual assistant. Which BHK are you looking for?"
        if step == "timeline":
            return "Yes, I am Proviso Group's virtual assistant. When are you planning to buy?"
        return "Yes, I am Proviso Group's virtual assistant. Are you looking for a property?"

    semantic_answer = _safe_direct_answer(call_state.last_direct_answer)
    if semantic_answer and call_state.last_turn_type in {
        "identity_question",
        "call_purpose_question",
        "business_question",
        "objection",
        "off_topic",
        "confusion",
    }:
        separator = " " if semantic_answer.endswith((".", "?", "।")) else ". "
        if (
            step == "engagement"
            and call_state.next_action in {"send_brochure", "send_details"}
            and any(token in _normalize_state_text(semantic_answer) for token in ("brochure", "whatsapp"))
        ):
            return f"{semantic_answer}{separator}Is that okay?"
        if step == "bhk":
            return "I understand. Which BHK are you looking for?"
        if step == "timeline":
            return "I understand. When are you planning to buy?"
        if step == "interest":
            return "I understand. Are you looking for a property?"
        if step == "engagement":
            return "I understand. Should I send the details on WhatsApp?"
        return semantic_answer

    if normalized and normalized.identity_question:
        return "This is Riya from Proviso Group. Are you looking for a property?"

    if normalized and normalized.call_purpose_question:
        return "This is about your property enquiry. Are you looking for a property?"

    if normalized and normalized.confusion:
        if step == "bhk":
            return "Sorry, I will keep it simple. Which BHK are you looking for?"
        if step == "timeline":
            return "Sorry, I will keep it simple. When are you planning to buy?"
        return "Sorry, I will keep it simple. Are you looking for a property?"

    if normalized and normalized.business_query == "price":
        if step == "bhk":
            return "Exact price depends on requirement. Which BHK are you looking for?"
        return "Exact price depends on requirement. Are you looking for a property?"

    if normalized and normalized.business_query == "location":
        return "I can share location details. Are you looking for a property?"

    if off_topic:
        if step == "bhk":
            return "Let's stay with property. Which BHK are you looking for?"
        if step == "timeline":
            return "Let's stay with property. When are you planning to buy?"
        return "Let's stay with property. Are you looking for a property?"

    if step == "interest":
        if normalized and (normalized.hello_repair or normalized.short_ack):
            if call_state.noise_turn_count >= 2:
                return "Yes, I am here. Are you looking for a property?"
            return "Yes, I am on the line. Are you looking for a property?"
        return "Are you still looking for a property?"

    if step == "bhk":
        if (action or {}).get("already_asked"):
            return "Just to confirm, one, two, or three BHK?"
        return "Which BHK are you looking for?"

    if step == "timeline":
        return "Got it. When are you planning to buy?"

    if step == "engagement":
        semantic_answer = _safe_direct_answer(call_state.last_direct_answer)
        if normalized and normalized.hello_repair:
            return "Yes, I am here. Should I send details on WhatsApp?"
        if semantic_answer:
            return "I understand. Should I send details on WhatsApp?"
        return "I understand. Should I send details on WhatsApp?"

    return "No problem, I will update this. Thank you."


def _next_required_step(call_state: CallState | None) -> str:
    if not call_state:
        return ""
    return call_state.current_step


def _update_state_from_user_text(text: str, call_state: CallState | None) -> None:
    update_state_from_user_input(text, call_state)


def _update_state_from_agent_text(text: str, call_state: CallState | None) -> None:
    return


def _guard_spoken_text(cleaned: str, call_state: CallState | None) -> str:
    if not call_state or not cleaned:
        return cleaned

    lowered = _normalize_state_text(cleaned)
    if re.search(
        r"(whatsapp|phone|mobile|number).*(number|mil|de|bhej)|number.*(mil|de|bhej|whatsapp)",
        lowered,
        flags=re.IGNORECASE,
    ):
        logger.warning("Blocked request for phone/WhatsApp number in spoken text: %s", cleaned)
        return "I can share details on this WhatsApp number. Is that okay?"

    if call_state.timeline is None and any(
        phrase in lowered
        for phrase in (
            "one year",
            "1 year",
            "in a year",
            "after year",
            "next year",
            "ek saal",
            "एक साल",
        )
    ):
        logger.warning("Blocked unsupported timeline claim in spoken text: %s", cleaned)
        return STEP_EXAMPLE_RESPONSES.get(call_state.current_step, "Planning soon or just exploring?")

    return cleaned


def validate_llm_response_for_voice(text: str, max_words: int = 18) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""

    cleaned = re.sub(
        r"[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\U00002700-\U000027BF]",
        "",
        cleaned,
    ).strip()

    first_question = cleaned.find("?")
    if first_question != -1:
        cleaned = cleaned[: first_question + 1]
        cleaned = cleaned.replace("?", "", cleaned.count("?") - 1)
    else:
        sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?।])\s+", cleaned)
            if sentence.strip()
        ]
        if sentences:
            cleaned = " ".join(sentences[:2]).strip()

    words = cleaned.split()
    if len(words) > max_words:
        keep_question = cleaned.endswith("?")
        cleaned = " ".join(words[:max_words]).rstrip(".,;:!?")
        if keep_question:
            cleaned += "?"

    return cleaned


def should_force_deterministic_response(
    call_state: CallState,
    action: dict | None,
    normalized: NormalizedUserInput | None,
) -> bool:
    """Use fixed speech only for turn-safety cases, not real sales reasoning."""
    if not action:
        return False
    if _action_has_invalid_done(action):
        return True
    if normalized and normalized.hard_stop:
        return True
    if action.get("current_step") == "done" and action.get("done_reason") in {
        "not_interested",
        "wrong_lead",
        "callback_later",
        "unclear_interest",
    }:
        return True
    if not action.get("should_ask"):
        return False
    if normalized and (
        normalized.language_request
        or normalized.audio_confusion
        or normalized.stt_noise
    ):
        return True
    if is_repair_or_permission_turn(normalized):
        return True
    return call_state.last_turn_type in {
        "permission",
        "repair",
        "identity_question",
        "call_purpose_question",
        "confusion",
    } or bool(normalized and normalized.call_purpose_question)


def should_use_fast_semantic_response(
    call_state: CallState,
    action: dict | None,
    normalized: NormalizedUserInput | None,
) -> bool:
    if os.getenv("DISABLE_FAST_SEMANTIC_RESPONSE", "").lower() in {"1", "true", "yes"}:
        return False
    if not action or not action.get("should_ask"):
        return False
    if should_force_deterministic_response(call_state, action, normalized):
        return False
    try:
        confidence = float(call_state.semantic_confidence or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.65:
        return False
    if action.get("current_step") == "engagement":
        return True
    return bool(call_state.last_direct_answer) and call_state.last_turn_type in {
        "business_question",
        "objection",
        "off_topic",
    }


class _CostSafeFailoverTTSStream:
    def __init__(self, owner, primary, fallback, conn_options, fallback_label: str) -> None:
        self._owner = owner
        self._primary = primary
        self._fallback = fallback
        self._conn_options = conn_options
        self._fallback_label = fallback_label
        self._chunks: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._closed = False
        self._sentinel = object()

    def push_text(self, token: str) -> None:
        if token and not self._closed:
            self._chunks.append(token)

    def end_input(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="cost_safe_tts_failover")

    async def _run(self) -> None:
        text = "".join(self._chunks).strip()
        if not text:
            await self._queue.put(self._sentinel)
            return

        emitted_audio = False
        fallback_conn_options = APIConnectOptions(
            max_retry=int(os.getenv("TTS_FALLBACK_MAX_RETRY", "0")),
            retry_interval=float(os.getenv("TTS_FALLBACK_RETRY_INTERVAL", "0.1")),
            timeout=float(os.getenv("TTS_FALLBACK_TIMEOUT", "7.0")),
        )
        if self._owner.primary_unhealthy:
            logger.info("Primary TTS is marked unhealthy for this call; using fallback %s.", self._fallback_label)
            try:
                fallback_stream = self._fallback.synthesize(text, conn_options=fallback_conn_options)
                async for event in fallback_stream:
                    await self._queue.put(event)
            finally:
                await self._queue.put(self._sentinel)
            return

        try:
            primary_conn_options = APIConnectOptions(
                max_retry=int(os.getenv("TTS_PRIMARY_MAX_RETRY", "0")),
                retry_interval=float(os.getenv("TTS_PRIMARY_RETRY_INTERVAL", "0.1")),
                timeout=float(os.getenv("TTS_PRIMARY_TIMEOUT", "3.0")),
            )
            primary_stream = self._primary.synthesize(text, conn_options=primary_conn_options)
            async for event in primary_stream:
                emitted_audio = True
                await self._queue.put(event)
        except Exception as exc:
            if emitted_audio:
                logger.warning("Primary TTS failed after partial audio; not replaying full text: %s", exc)
            else:
                logger.warning("Primary TTS failed before audio; using fallback %s: %s", self._fallback_label, exc)
                self._owner.mark_primary_unhealthy()
                try:
                    fallback_stream = self._fallback.synthesize(text, conn_options=fallback_conn_options)
                    async for event in fallback_stream:
                        await self._queue.put(event)
                except Exception as fallback_exc:
                    logger.error("Fallback TTS also failed: %s", fallback_exc)
                    raise
        finally:
            await self._queue.put(self._sentinel)

    async def aclose(self) -> None:
        self._closed = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, exc_tb) -> None:
        await self.aclose()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._task is None:
            self.end_input()
        item = await self._queue.get()
        if item is self._sentinel:
            if self._task and not self._task.cancelled() and self._task.exception():
                raise self._task.exception()
            raise StopAsyncIteration
        return item


class CostSafeFailoverTTS(agents_tts.TTS):
    def __init__(self, primary, fallback, *, primary_label: str, fallback_label: str) -> None:
        super().__init__(
            capabilities=agents_tts.TTSCapabilities(streaming=True),
            sample_rate=primary.sample_rate,
            num_channels=primary.num_channels,
        )
        self._primary = primary
        self._fallback = fallback
        self._primary_label = primary_label
        self._fallback_label = fallback_label
        self._primary_unhealthy = False
        self._label = f"cost-safe-failover({primary_label}->{fallback_label})"

    @property
    def model(self) -> str:
        return self._primary_label

    @property
    def provider(self) -> str:
        return "sarvam"

    def synthesize(self, text: str, *, conn_options=None):
        options = conn_options or APIConnectOptions(
            max_retry=0,
            retry_interval=0.1,
            timeout=float(os.getenv("TTS_PRIMARY_TIMEOUT", "3.0")),
        )
        return self._synthesize_with_stream(text, conn_options=options)

    def stream(self, *, conn_options=None):
        return _CostSafeFailoverTTSStream(
            self,
            self._primary,
            self._fallback,
            conn_options,
            self._fallback_label,
        )

    @property
    def primary_unhealthy(self) -> bool:
        return self._primary_unhealthy

    def mark_primary_unhealthy(self) -> None:
        self._primary_unhealthy = True

    async def aclose(self) -> None:
        await asyncio.gather(
            self._primary.aclose(),
            self._fallback.aclose(),
            return_exceptions=True,
        )


def _sarvam_voice_for_model(model: str, voice: str) -> str:
    if model in {"bulbul:v3", "bulbul:v3-beta"} and voice in {"anushka", "manisha", "vidya", "arya"}:
        return os.getenv("SARVAM_FALLBACK_VOICE") or ("ritu" if _uses_feminine_self_reference() else "shubh")
    if model == "bulbul:v2" and voice in {
        "ritu",
        "pooja",
        "simran",
        "kavya",
        "ishita",
        "shreya",
        "priya",
        "neha",
        "roopa",
        "amelia",
        "sophia",
    }:
        return "anushka"
    return voice


def _build_outbound_greeting() -> str:
    return "Hi, this is Riya from Proviso Group. You had a property enquiry. Is this a good time?"


def _build_fallback_greeting() -> str:
    return "Hi, this is Riya from Proviso Group. How can I help with your property enquiry?"


_RETRYABLE_SIP_STATUS_CODES = {"408", "480", "500", "502", "503", "504"}


class OutboundDialError(RuntimeError):
    def __init__(self, message: str, errors: list[str]):
        super().__init__(message)
        self.errors = errors


def _safe_status_filename(room_name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", room_name)
    return os.path.join(_CALL_STATUS_DIR, f"{safe}.json")


def _write_call_status(room_name: str, status: str, **fields) -> None:
    payload = {
        "room": room_name,
        "status": status,
        **fields,
    }
    try:
        with open(_safe_status_filename(room_name), "w", encoding="utf-8") as status_file:
            json.dump(payload, status_file, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("Could not write local call status for room %s: %s", room_name, exc)


def _split_env_csv(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _add_unique(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def _trunk_matches_config(trunk) -> bool:
    domain = (getattr(config, "SIP_DOMAIN", "") or "").strip()
    outbound_number = (getattr(config, "SIP_OUTBOUND_NUMBER", "") or "").strip()
    trunk_address = (getattr(trunk, "address", "") or "").strip()
    trunk_numbers = list(getattr(trunk, "numbers", []) or [])

    if outbound_number and outbound_number in trunk_numbers:
        return True
    if domain and trunk_address == domain:
        return True
    return False


async def _outbound_trunk_candidates(ctx) -> list[str]:
    candidates: list[str] = []
    primary = (getattr(config, "SIP_TRUNK_ID", "") or "").strip()
    _add_unique(candidates, primary)

    for trunk_id in _split_env_csv("VOBIZ_FALLBACK_SIP_TRUNK_IDS"):
        _add_unique(candidates, trunk_id)

    try:
        response = await ctx.api.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
        available = list(response.items or [])
    except Exception as exc:
        logger.warning("Could not preflight outbound SIP trunks; using configured IDs only: %s", exc)
        available = []

    if available:
        available_ids = [getattr(trunk, "sip_trunk_id", "") for trunk in available]
        if primary and primary not in available_ids:
            logger.error("Configured SIP trunk %s is not present in LiveKit outbound trunks.", primary)

        for trunk in available:
            trunk_id = getattr(trunk, "sip_trunk_id", "")
            if trunk_id and _trunk_matches_config(trunk):
                _add_unique(candidates, trunk_id)

    return candidates


def _extract_sip_failure(exc: Exception) -> tuple[str | None, str | None]:
    metadata = getattr(exc, "metadata", None) or {}
    code = None
    status = None

    if isinstance(metadata, dict):
        raw_code = metadata.get("sip_status_code")
        raw_status = metadata.get("sip_status")
        code = str(raw_code) if raw_code is not None else None
        status = str(raw_status) if raw_status is not None else None

    message = str(exc)
    if not code:
        code_match = re.search(r"sip[_ ]status(?:[_ ]code)?['\"]?\s*[:=]\s*['\"]?(\d{3})", message, re.I)
        if not code_match:
            code_match = re.search(r"sip status:\s*(\d{3})", message, re.I)
        if code_match:
            code = code_match.group(1)

    if not status:
        status_match = re.search(r"sip status:\s*\d{3}:\s*([^,\)]+)", message, re.I)
        if status_match:
            status = status_match.group(1).strip()

    return code, status


def _is_retryable_sip_failure(code: str | None) -> bool:
    return code is None or code in _RETRYABLE_SIP_STATUS_CODES


def _ringing_timeout_duration() -> Duration | None:
    seconds = int(getattr(config, "OUTBOUND_RINGING_TIMEOUT_SECONDS", 0) or 0)
    if seconds <= 0:
        return None
    return Duration(seconds=seconds)


async def _create_sip_participant(ctx, phone_number: str, trunk_id: str, attempt_number: int):
    request_kwargs = {
        "room_name": ctx.room.name,
        "sip_trunk_id": trunk_id,
        "sip_call_to": phone_number,
        "participant_identity": f"sip_{phone_number}",
        "participant_attributes": {
            "dial_attempt": str(attempt_number),
            "dialed_number": phone_number,
        },
        "wait_until_answered": True,
    }

    outbound_number = (getattr(config, "SIP_OUTBOUND_NUMBER", "") or "").strip()
    if outbound_number:
        request_kwargs["sip_number"] = outbound_number
        request_kwargs["participant_attributes"]["caller_id"] = outbound_number

    ringing_timeout = _ringing_timeout_duration()
    if ringing_timeout is not None:
        request_kwargs["ringing_timeout"] = ringing_timeout

    return await ctx.api.sip.create_sip_participant(
        api.CreateSIPParticipantRequest(**request_kwargs)
    )


async def _dial_outbound_call(ctx, phone_number: str) -> str:
    candidates = await _outbound_trunk_candidates(ctx)
    if not candidates:
        _write_call_status(
            ctx.room.name,
            "failed",
            phone_number=phone_number,
            error="No outbound SIP trunk is configured.",
        )
        raise OutboundDialError("No outbound SIP trunk is configured.", [])

    _write_call_status(
        ctx.room.name,
        "dialing",
        phone_number=phone_number,
        candidate_trunks=candidates,
    )

    errors: list[str] = []
    total = len(candidates)
    for index, trunk_id in enumerate(candidates, start=1):
        logger.info("Outbound SIP attempt %s/%s using trunk %s.", index, total, trunk_id)
        _write_call_status(
            ctx.room.name,
            "dialing",
            phone_number=phone_number,
            current_trunk=trunk_id,
            attempt=index,
            total_attempts=total,
            candidate_trunks=candidates,
        )
        try:
            await _create_sip_participant(ctx, phone_number, trunk_id, index)
            logger.info("Outbound SIP call answered on trunk %s.", trunk_id)
            _write_call_status(
                ctx.room.name,
                "answered",
                phone_number=phone_number,
                answered_trunk=trunk_id,
                attempt=index,
            )
            return trunk_id
        except Exception as exc:
            code, status = _extract_sip_failure(exc)
            detail = f"trunk={trunk_id}, sip_status_code={code or 'unknown'}, sip_status={status or 'unknown'}"
            errors.append(detail)

            if index >= total or not _is_retryable_sip_failure(code):
                _write_call_status(
                    ctx.room.name,
                    "failed",
                    phone_number=phone_number,
                    failed_trunk=trunk_id,
                    sip_status_code=code,
                    sip_status=status,
                    errors=errors,
                )
                raise OutboundDialError(f"Outbound SIP call failed: {detail}", errors) from exc

            logger.warning(
                "Outbound SIP attempt failed with retryable status; trying next trunk. %s",
                detail,
            )
            _write_call_status(
                ctx.room.name,
                "retrying",
                phone_number=phone_number,
                failed_trunk=trunk_id,
                next_trunk=candidates[index],
                sip_status_code=code,
                sip_status=status,
                errors=errors,
            )
            await asyncio.sleep(1.5)

    _write_call_status(
        ctx.room.name,
        "failed",
        phone_number=phone_number,
        error="Outbound SIP call failed on all candidate trunks.",
        errors=errors,
    )
    raise OutboundDialError("Outbound SIP call failed on all candidate trunks.", errors)


def _build_stt(config_language: str | None = None):
    language = (
        config_language
        or os.getenv("STT_LANGUAGE")
        or getattr(config, "STT_LANGUAGE", "multi")
    ).strip()

    # Keep multilingual STT for common Indian acknowledgments like
    # "haan", "boliye", and "ji" while speaking back in clear English.
    if language.lower() == "en":
        language = "multi"

    endpointing_ms = int(os.getenv("DEEPGRAM_ENDPOINTING_MS", "160"))
    stt_kwargs = {
        "model": config.STT_MODEL,
        "language": language,
        "interim_results": True,
        "punctuate": True,
        "smart_format": True,
        "no_delay": True,
        "endpointing_ms": endpointing_ms,
        "filler_words": True,
    }
    if str(config.STT_MODEL).startswith("nova-3"):
        stt_kwargs["keyterm"] = [
            "one BHK",
            "two BHK",
            "three BHK",
            "ek BHK",
            "do BHK",
            "teen BHK",
            "property enquiry",
            "Proviso Group",
        ]
    else:
        stt_kwargs["keywords"] = DEEPGRAM_HINT_KEYWORDS
    return deepgram.STT(**stt_kwargs)


def _normalize_user_reply(text: str, call_state: CallState | None = None) -> str:
    update_state_from_user_input(text, call_state)
    return text


def _sanitize_spoken_text(text: str, call_state: CallState | None = None) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""

    cleaned = re.sub(r"\b(\w+)(?:\s+\1\b)+", r"\1", cleaned, flags=re.IGNORECASE)

    for pattern, replacement in SPOKEN_TEXT_REPLACEMENTS:
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)

    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?।])\s+", cleaned)
        if sentence.strip()
    ]
    if sentences:
        selected_sentences = sentences[:2]
        if (
            len(sentences) >= 3
            and sentences[2].endswith("?")
            and len(" ".join(sentences[:3]).split()) <= 18
        ):
            selected_sentences = sentences[:3]
        cleaned = " ".join(selected_sentences).strip()

    if len(cleaned) > 160:
        cleaned = cleaned[:160].rsplit(" ", 1)[0].strip()

    if "�" in cleaned or re.search(r"[\u0370-\u03FF\u0400-\u04FF\u2500-\u257F]", cleaned):
        logger.warning("Blocked garbled spoken text: %s", cleaned)
        return "Sorry, I will keep it simple. Are you looking for a property?"

    if re.search(r"\bend_call\b", cleaned, flags=re.IGNORECASE):
        logger.warning("Blocked leaked end_call syntax in spoken text: %s", cleaned)
        return ""

    for pattern in UNSAFE_SPOKEN_PATTERNS:
        if re.search(pattern, cleaned, flags=re.IGNORECASE):
            logger.warning("Blocked unsafe spoken text: %s", cleaned)
            return ""

    cleaned = _guard_spoken_text(cleaned, call_state)
    if not cleaned:
        return ""

    _update_state_from_agent_text(cleaned, call_state)
    return cleaned


def _build_tts(config_provider: str = None, config_voice: str = None):
    """Configure the Text-to-Speech provider based on env vars or dynamic config."""
    # Priority: Config > Env Var > Default
    provider = (config_provider or os.getenv("TTS_PROVIDER", config.DEFAULT_TTS_PROVIDER)).lower()
    
    # If using Sarvam Voice names (Anushka/Aravind), force Sarvam provider
    if config_voice in ["anushka", "aravind", "amartya", "dhruv"]:
        provider = "sarvam"

    if provider == "cartesia":
        logger.info("Using Cartesia TTS")
        model = os.getenv("CARTESIA_TTS_MODEL", config.CARTESIA_MODEL)
        voice = os.getenv("CARTESIA_TTS_VOICE", config.CARTESIA_VOICE)
        return cartesia.TTS(model=model, voice=voice)
    
    if provider == "sarvam":
        model = os.getenv("SARVAM_TTS_MODEL", config.SARVAM_MODEL)
        # Use dynamic voice or env var or default
        voice = config_voice or os.getenv("SARVAM_VOICE", "anushka")
        language = os.getenv("SARVAM_LANGUAGE", config.SARVAM_LANGUAGE)
        codec = os.getenv("SARVAM_OUTPUT_AUDIO_CODEC", "wav")
        pace = float(os.getenv("SARVAM_PACE", str(getattr(config, "SARVAM_PACE", 0.78))))
        speech_sample_rate = int(os.getenv("SARVAM_SPEECH_SAMPLE_RATE", "22050"))
        min_buffer_size = int(os.getenv("SARVAM_MIN_BUFFER_SIZE", "120"))
        max_chunk_length = int(os.getenv("SARVAM_MAX_CHUNK_LENGTH", "220"))
        loudness = float(os.getenv("SARVAM_LOUDNESS", "0.9"))
        temperature = float(os.getenv("SARVAM_TEMPERATURE", "0.35"))
        enable_preprocessing = os.getenv("SARVAM_ENABLE_PREPROCESSING", "true").lower() in {
            "1",
            "true",
            "yes",
        }
        primary_voice = _sarvam_voice_for_model(model, voice)
        logger.info(
            f"Using Sarvam TTS (Model: {model}, Voice: {primary_voice}, Language: {language}, Pace: {pace})"
        )
        sarvam_kwargs = {
            "model": model,
            "speaker": primary_voice,
            "target_language_code": language,
            "output_audio_codec": codec,
            "pace": pace,
            "speech_sample_rate": speech_sample_rate,
            "min_buffer_size": min_buffer_size,
            "max_chunk_length": max_chunk_length,
            "loudness": loudness,
            "temperature": temperature,
            "enable_preprocessing": enable_preprocessing,
        }

        primary_tts = sarvam.TTS(**sarvam_kwargs)

        fallback_model = os.getenv("SARVAM_FALLBACK_TTS_MODEL", "").strip()
        if fallback_model and fallback_model != model:
            fallback_voice = _sarvam_voice_for_model(
                fallback_model,
                os.getenv("SARVAM_FALLBACK_VOICE", voice),
            )
            fallback_kwargs = {
                **sarvam_kwargs,
                "model": fallback_model,
                "speaker": fallback_voice,
            }
            logger.info(
                "Cost-safe Sarvam failover enabled: primary %s/%s, fallback %s/%s",
                model,
                primary_voice,
                fallback_model,
                fallback_voice,
            )
            fallback_tts = sarvam.TTS(**fallback_kwargs)
            return CostSafeFailoverTTS(
                primary_tts,
                fallback_tts,
                primary_label=f"{model}/{primary_voice}",
                fallback_label=f"{fallback_model}/{fallback_voice}",
            )

        return primary_tts

    if provider == "deepgram":
        logger.info("Using Deepgram TTS")
        model = os.getenv("DEEPGRAM_TTS_MODEL", "aura-asteria-en")
        return deepgram.TTS(model=model)

    # Default to OpenAI
    logger.info(f"Using OpenAI TTS (Voice: {config_voice})")
    model = os.getenv("OPENAI_TTS_MODEL", "tts-1")
    voice = config_voice or os.getenv("OPENAI_TTS_VOICE", config.DEFAULT_TTS_VOICE)
    return openai.TTS(model=model, voice=voice)


def _build_llm(config_provider: str = None, max_completion_tokens: int | None = None):
    """Configure the LLM provider based on config or env vars."""
    provider = (config_provider or os.getenv("LLM_PROVIDER", config.DEFAULT_LLM_PROVIDER)).lower()
    if max_completion_tokens is not None:
        max_tokens = max(32, min(512, int(max_completion_tokens)))
    else:
        try:
            max_tokens = max(32, min(256, int(os.getenv("LLM_MAX_COMPLETION_TOKENS", "96"))))
        except ValueError:
            max_tokens = 96
    try:
        provider_retries = max(0, min(2, int(os.getenv("LLM_PROVIDER_MAX_RETRIES", "0"))))
    except ValueError:
        provider_retries = 0

    if provider == "groq":
        logger.info("Using Groq LLM")
        return openai.LLM(
            base_url="https://api.groq.com/openai/v1",
            api_key=os.getenv("GROQ_API_KEY"),
            model=os.getenv("GROQ_MODEL", config.GROQ_MODEL),
            temperature=float(os.getenv("GROQ_TEMPERATURE", str(config.GROQ_TEMPERATURE))),
            max_completion_tokens=max_tokens,
            max_retries=provider_retries,
            parallel_tool_calls=False,
        )
    
    # Default to OpenAI
    logger.info("Using OpenAI LLM")
    return openai.LLM(
        model=os.getenv("OPENAI_LLM_MODEL", config.DEFAULT_LLM_MODEL),
        temperature=float(
            os.getenv(
                "OPENAI_LLM_TEMPERATURE",
                str(getattr(config, "DEFAULT_LLM_TEMPERATURE", 0.35)),
            )
        ),
        max_completion_tokens=max_tokens,
        max_retries=provider_retries,
        parallel_tool_calls=False,
    )


def _resolve_llm_provider(config_provider: str | None = None) -> str:
    return (config_provider or os.getenv("LLM_PROVIDER", config.DEFAULT_LLM_PROVIDER)).lower()


class LeadExtraction(BaseModel):
    disposition: str = Field(default="")
    interest_level: str = Field(default="")
    objection_type: str = Field(default="")
    reason: str = Field(default="")
    next_action: str = Field(default="")
    bhk: str = Field(default="")
    timeline: str = Field(default="")
    visited: str = Field(default="")
    budget: str = Field(default="")
    location: str = Field(default="")
    summary: str = Field(default="")
    customer_intent: str = Field(default="unknown")


def _build_agent_instructions(config_dict: dict) -> str:
    compact_context: list[str] = [
        "After each customer turn, a Controller block gives step, state, directive, and next. Follow it exactly.",
        "You phrase the next action only; never choose flow, invent facts, or ask multiple questions.",
        "Goal: classify lead as interested, not_interested, callback_later, wrong_lead, or unclear.",
        "For interested leads, collect only BHK and timeline unless the Controller says close.",
        "Answer the latest customer question briefly, then ask the Controller's one next question.",
        "If there is an objection, acknowledge it, answer safely, and advance one step.",
        "Use simple Indian English, <=18 words, one question max, no emoji, no internal/tool/debug text.",
        "Avoid Hindi script and long mixed-language phrases; clarity on phone audio is more important than style.",
        "Never ask for the customer's phone number; you are already speaking on it. Say you can WhatsApp this number.",
        "Use end_call only when the Controller says step=done and directive says close, or the customer clearly asks to stop.",
    ]

    if _uses_feminine_self_reference():
        compact_context.append("You are Riya; keep first-person phrasing short and professional.")

    if config_dict.get("lead_name"):
        compact_context.append(f"Lead name: {config_dict['lead_name']}. Use it only if natural.")

    if config_dict.get("user_prompt"):
        compact_context.append(f"Campaign context: {config_dict['user_prompt']}")

    return f"{config.SYSTEM_PROMPT.strip()}\n" + "\n".join(compact_context)

def _normalize_field(value: str | None) -> str:
    if not value:
        return ""
    cleaned = str(value).strip()
    return "" if cleaned.lower() in {"unknown", "n/a", "none", "null"} else cleaned


def _clean_json_payload(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    cleaned = cleaned.strip()
    if not cleaned.startswith("{"):
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            cleaned = match.group(0)
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    return cleaned.strip()


async def _analyze_sales_turn(
    text: str,
    call_state: CallState,
    llm_provider: str | None,
) -> SalesTurnAnalysis | None:
    if not text.strip() or os.getenv("DISABLE_SALES_TURN_ANALYSIS", "").lower() in {"1", "true", "yes"}:
        return None

    quick = normalize_user_input(text)
    if (
        quick.hello_repair
        or quick.short_ack
        or quick.language_request
        or quick.audio_confusion
        or quick.stt_noise
        or quick.hard_stop
    ) and not _has_extracted_business_signal(quick):
        return None
    if quick.stt_noise or quick.audio_confusion or quick.language_request or quick.hard_stop:
        return None
    if (
        (call_state.current_step == "interest" and quick.lead_disposition in {"interested", "not_interested", "callback_later", "wrong_lead"})
        or (call_state.current_step == "bhk" and quick.bhk)
        or (call_state.current_step == "timeline" and quick.timeline)
    ):
        return None

    prompt = """
Infer the latest Indian real-estate buyer turn. Return compact valid JSON only with:
turn_type,question_type,disposition,interest_level,objection_type,reason,next_action,bhk,timeline,visited,budget,location,direct_answer,semantic_summary,buyer_signal,sales_move,response_strategy,confidence.
Allowed turn_type: answer,permission,repair,identity_question,call_purpose_question,business_question,objection,off_topic,rejection,callback_request,wrong_lead,confusion,unclear.
Allowed question_type: none,identity,call_purpose,price,location,project,availability,callback,personal,other.
Rules: generic first "not interested"=objection/not_interested_reflex, disposition empty. "I do not want to talk", stop, do not call, already bought/no need => not_interested. No enquiry/wrong number => wrong_lead. Noisy/unclear => no disposition. Price/location/project/family/budget/WhatsApp/brochure are engagement, not rejection. "call later" => callback_request; "send brochure/WhatsApp" => business_question, next_action send_brochure. AI/bot question => identity_question. Flirting/personal => off_topic/personal. Extract BHK,timeline,visited,budget,location. direct_answer max 10 simple English words; never invent price, possession, market claim, broker/builder status, offer, availability, or ask phone number. confidence 0-1; if <0.45 leave disposition,next_action,bhk,timeline,visited empty.
Examples: not interested=>objection; pehle price batao=>objection price; I love you=>off_topic personal; maine koi enquiry nahi ki=>wrong_lead close; 2bhk Panvel 3 mahine=>interested 2BHK 3 months Panvel.
"""
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content=prompt.strip())
    chat_ctx.add_message(
        role="user",
        content=json.dumps(
            {
                "latest_customer_message": text.strip(),
                "current_state": {
                    "lead_disposition": call_state.lead_disposition,
                    "interest_level": call_state.interest_level,
                    "bhk": call_state.bhk,
                    "timeline": call_state.timeline,
                    "budget": call_state.budget,
                    "location": call_state.location,
                    "current_step": call_state.current_step,
                    "last_turn_type": call_state.last_turn_type,
                    "last_objection_type": call_state.objection_type,
                    "user_turn_count": call_state.user_turn_count,
                },
            },
            ensure_ascii=False,
        ),
    )

    async def _run() -> SalesTurnAnalysis | None:
        resolved_provider = _resolve_llm_provider(llm_provider)
        try:
            semantic_max_tokens = int(os.getenv("SALES_TURN_ANALYSIS_MAX_TOKENS", "220"))
        except ValueError:
            semantic_max_tokens = 220
        classifier_llm = _build_llm(llm_provider, max_completion_tokens=semantic_max_tokens)
        if resolved_provider == "openai":
            response = await classifier_llm.chat(
                chat_ctx=chat_ctx,
                response_format=SalesTurnAnalysis,
            ).collect()
        else:
            response = await classifier_llm.chat(chat_ctx=chat_ctx).collect()

        payload = response.text.strip()
        if not payload:
            return None
        data = json.loads(_clean_json_payload(payload))
        for field_name, field_info in SalesTurnAnalysis.model_fields.items():
            if data.get(field_name) is None:
                data[field_name] = 0.0 if field_name == "confidence" else ""
        return SalesTurnAnalysis.model_validate(data)

    try:
        return await asyncio.wait_for(
            _run(),
            timeout=float(os.getenv("SALES_TURN_ANALYSIS_TIMEOUT", "2.8")),
        )
    except Exception as exc:
        logger.warning("Sales turn analysis skipped; using deterministic signals only: %s", exc)
        return None


def _normalize_transcript_for_extraction(transcript: str) -> str:
    normalized = transcript
    replacements = {
        r"\btwenty h k\b": "2BHK",
        r"\btwo h k\b": "2BHK",
        r"\b2 h k\b": "2BHK",
        r"\bone h k\b": "1BHK",
        r"\b1 h k\b": "1BHK",
        r"\bthree h k\b": "3BHK",
        r"\b3 h k\b": "3BHK",
        r"\bpanwale\b": "Panvel",
        r"\bpanvel\b": "Panvel",
        r"\bkharghar\b": "Kharghar",
        r"\bsite dekha\b": "site visited",
        r"\balready seen\b": "already visited",
        r"\bek saal\b": "later",
    }

    for pattern, replacement in replacements.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    return normalized


def _build_transcript(conversation_log: list[str], session: AgentSession) -> str:
    if conversation_log:
        deduped_lines: list[str] = []
        for line in conversation_log:
            cleaned = line.strip()
            if not cleaned:
                continue
            if deduped_lines and deduped_lines[-1] == cleaned:
                continue
            deduped_lines.append(cleaned)
        return "\n".join(deduped_lines).strip()

    messages = []
    for message in session.history.messages():
        text = message.text_content
        if not text:
            continue
        messages.append(f"{message.role.title()}: {text.strip()}")
    return "\n".join(messages).strip()


def _derive_status(extracted: LeadExtraction, transcript: str) -> str:
    disposition = extracted.disposition.strip().lower()
    intent = extracted.customer_intent.strip().lower()
    has_core_details = bool(extracted.bhk and (extracted.timeline or extracted.visited))
    has_user_input = any(
        line.strip().startswith("User:")
        for line in transcript.splitlines()
    )

    if disposition == "not_interested" or intent == "not_interested":
        return "Not Interested"
    if disposition == "callback_later" or intent == "callback_later":
        return "Callback Later"
    if disposition == "wrong_lead":
        return "Wrong Lead"
    if disposition == "interested" and has_core_details:
        return "Interested - Qualified"
    if disposition == "interested" or intent in {"interested", "high"} or any(
        [extracted.bhk, extracted.timeline, extracted.visited, extracted.budget, extracted.location]
    ):
        return "Interested"
    if has_user_input:
        return "Contacted"
    return "Pending"


async def _extract_lead_data(transcript: str, llm_provider: str | None) -> LeadExtraction:
    if not transcript:
        return LeadExtraction(
            summary="No usable conversation transcript was captured for this lead.",
            customer_intent="unknown",
        )

    normalized_transcript = _normalize_transcript_for_extraction(transcript)

    prompt = """
You extract structured real-estate lead data from a phone-call transcript.
The transcript may contain noisy phone-call STT errors, so normalize obvious real-estate phrases before deciding.

Examples:
- "Twenty h k" usually means "2BHK"
- "One h k" usually means "1BHK"
- "Panwale" usually means "Panvel"
- One-word answers like "Saturday" usually mean the customer accepted that option
- If a field is not clearly present, keep it as an empty string

Return JSON only.
Rules:
- disposition: interested, not_interested, callback_later, wrong_lead, unclear, or empty string
- interest_level: hot, warm, nurture, cold, unknown, or empty string
- objection_type: price, timing, trust, need, already_bought, wrong_number, busy, confusion, off_topic, none, or empty string
- reason: short reason for the disposition
- next_action: send_details, callback, nurture, close, manual_follow_up, or empty string
- bhk: use values like 1BHK, 2BHK, 3BHK, or empty string if unknown
- timeline: buying or visit timeline such as soon, this month, next month, later, or empty string
- visited: yes, no, or empty string if unknown
- budget: preserve the customer's range or number in natural form, or empty string
- location: preferred area or project, or empty string
- customer_intent: one of high, interested, low, not_interested, callback_later, unknown
- summary: one short sentence describing the lead disposition and reason
"""

    resolved_provider = _resolve_llm_provider(llm_provider)
    try:
        extraction_max_tokens = int(os.getenv("LEAD_EXTRACTION_MAX_TOKENS", "320"))
    except ValueError:
        extraction_max_tokens = 320
    extraction_llm = _build_llm(llm_provider, max_completion_tokens=extraction_max_tokens)
    chat_ctx = llm.ChatContext.empty()
    chat_ctx.add_message(role="system", content=prompt.strip())
    chat_ctx.add_message(role="user", content=normalized_transcript)

    use_json_schema = resolved_provider == "openai"

    if use_json_schema:
        response = await extraction_llm.chat(
            chat_ctx=chat_ctx,
            response_format=LeadExtraction,
        ).collect()
    else:
        response = await extraction_llm.chat(chat_ctx=chat_ctx).collect()

    payload = response.text.strip()
    if not payload:
        return LeadExtraction(
            summary="The call ended without enough information to summarize the lead.",
            customer_intent="unknown",
        )

    data = json.loads(_clean_json_payload(payload))
    extracted = LeadExtraction.model_validate(data)
    extracted.disposition = _normalize_field(extracted.disposition).lower()
    extracted.interest_level = _normalize_field(extracted.interest_level).lower()
    extracted.objection_type = _normalize_field(extracted.objection_type).lower()
    extracted.reason = _normalize_field(extracted.reason)
    extracted.next_action = _normalize_field(extracted.next_action).lower()
    extracted.bhk = _normalize_field(extracted.bhk)
    extracted.timeline = _normalize_field(extracted.timeline)
    extracted.visited = _normalize_field(extracted.visited)
    extracted.budget = _normalize_field(extracted.budget)
    extracted.location = _normalize_field(extracted.location)
    extracted.summary = _normalize_field(extracted.summary)
    extracted.customer_intent = _normalize_field(extracted.customer_intent).lower() or "unknown"
    return extracted


async def _update_sheet_after_call(config_dict: dict, session: AgentSession, conversation_log: list[str]) -> None:
    raw_row = config_dict.get("sheet_row")
    if raw_row in (None, ""):
        return

    try:
        row_number = int(raw_row)
    except (TypeError, ValueError):
        logger.warning("Skipping sheet update because sheet_row is invalid: %s", raw_row)
        return

    transcript = _build_transcript(conversation_log, session)
    llm_provider = config_dict.get("llm_provider") or config_dict.get("model_provider")

    try:
        extracted = await _extract_lead_data(transcript, llm_provider)
    except Exception as exc:
        logger.exception("Lead extraction failed: %s", exc)
        extracted = LeadExtraction(
            summary="Call completed, but structured extraction failed. Please review the transcript manually.",
            customer_intent="unknown",
        )

    spreadsheet_name = config_dict.get("spreadsheet_name") or config_dict.get("sheet_name")
    worksheet_name = config_dict.get("worksheet_name")

    updates = {
        "BHK": extracted.bhk,
        "Budget": extracted.budget,
        "Location": extracted.location,
        "Summary": extracted.summary or "Call completed but no structured summary was generated.",
        "Status": _derive_status(extracted, transcript),
    }

    try:
        headers = await asyncio.to_thread(
            sheet_service.get_headers,
            spreadsheet_name,
            worksheet_name,
        )
        header_set = set(headers)
        optional_updates = {
            "Disposition": extracted.disposition,
            "Interest Level": extracted.interest_level,
            "Objection": extracted.objection_type,
            "Reason": extracted.reason,
            "Next Action": extracted.next_action,
            "Timeline": extracted.timeline,
            "Visited": extracted.visited,
            "Intent": extracted.customer_intent,
        }
        for key, value in optional_updates.items():
            if key in header_set:
                updates[key] = value
    except Exception as exc:
        logger.warning("Could not read optional sheet headers; updating core fields only: %s", exc)

    await asyncio.to_thread(
        sheet_service.update_lead_row,
        row_number,
        updates,
        spreadsheet_name,
        worksheet_name,
    )



class TransferFunctions(llm.ToolContext):
    def __init__(
        self,
        ctx: agents.JobContext,
        phone_number: str = None,
        session: Optional[AgentSession] = None,
        call_state: CallState | None = None,
    ):
        super().__init__(tools=[])
        self.ctx = ctx
        self.phone_number = phone_number
        self.session = session
        self.call_state = call_state
        self.call_end_requested = False
        self.call_end_completed = False

    def _resolve_participant_identity(self) -> Optional[str]:
        if self.phone_number:
            return f"sip_{self.phone_number}"

        for participant in self.ctx.room.remote_participants.values():
            if participant.identity != self.ctx.local_participant_identity:
                return participant.identity

        return None

    @llm.function_tool(description="Disabled internal CRM lookup. Do not use for this campaign.")
    async def lookup_user(self, phone: str):
        """
        Mock function to look up user details.

        Args:
            phone: The phone number to look up
        """
        logger.info(f"Looking up user: {phone}")
        return "CRM lookup is disabled for this campaign. Continue the conversation without using this tool."

    @llm.function_tool(description="Transfer the call to a human support agent or another phone number.")
    async def transfer_call(self, destination: Optional[str] = None):
        """
        Transfer the call.
        """
        if destination is None:
            destination = config.DEFAULT_TRANSFER_NUMBER
            if not destination:
                 return "Error: No default transfer number configured."
        if "@" not in destination:
            # If no domain is provided, append the SIP domain
            if config.SIP_DOMAIN:
                # Ensure clean number (strip tel: or sip: prefix if present but no domain)
                clean_dest = destination.replace("tel:", "").replace("sip:", "")
                destination = f"sip:{clean_dest}@{config.SIP_DOMAIN}"
            else:
                # Fallback to tel URI if no domain configured
                if not destination.startswith("tel:") and not destination.startswith("sip:"):
                     destination = f"tel:{destination}"
        elif not destination.startswith("sip:"):
             destination = f"sip:{destination}"
        
        logger.info(f"Transferring call to {destination}")
        
        # Determine the participant identity
        # For outbound calls initiated by this agent, the participant identity is typically "sip_<phone_number>"
        # For inbound, we might need to find the remote participant.
        participant_identity = self._resolve_participant_identity()
        if not farewell_message:
            farewell_message = "No problem, I will update this. Thank you."

        if not participant_identity:
            logger.error("Could not determine participant identity for transfer")
            return "Failed to transfer: could not identify the caller."

        try:
            logger.info(f"Transferring participant {participant_identity} to {destination}")
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination,
                    play_dialtone=False
                )
            )
            return "Transfer initiated successfully."
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            return f"Error executing transfer: {e}"

    @llm.function_tool(
        description=(
            "Politely end the current phone call after the conversation is complete. "
            "Use this when the customer says bye, asks to end, wants a callback, "
            "or the conversation has naturally finished. Pass one short English goodbye "
            "in farewell_message."
        )
    )
    async def end_call(self, farewell_message: Optional[str] = None):
        if self.call_end_completed:
            return "Call already ended. Do not call end_call again."

        allowed_done_reasons = {
            "not_interested",
            "callback_later",
            "wrong_lead",
            "customer_exit",
            "unclear_interest",
        }
        natural_completion = self.call_state and (
            self.call_state.done_reason in {"interested", "interested_missing_detail"}
            and (
                self.call_state.bhk is not None
                or self.call_state.timeline is not None
                or self.call_state.user_turn_count >= 3
            )
        )
        unclear_after_real_attempts = self.call_state and (
            self.call_state.done_reason == "unclear_interest"
            and self.call_state.user_turn_count >= 4
        )
        can_end_call = (
            self.call_state is None
            or self.call_state.customer_requested_end
            or self.call_state.not_interested
            or self.call_state.done_reason in allowed_done_reasons
            or natural_completion
            or unclear_after_real_attempts
        )

        if self.call_state and not can_end_call:
            logger.warning(
                "Blocked premature end_call. state=%s",
                json.dumps(self.call_state.public_state(), ensure_ascii=False),
            )
            return (
                "Do not end the call yet. Continue from the controller's current_step, "
                "or wait for the customer to explicitly say bye or ask to stop."
            )

        participant_identity = self._resolve_participant_identity()
        spoken_farewell = _sanitize_spoken_text(
            farewell_message or "ठीक है जी, मैं डिटेल्स व्हाट्सऐप कर दूंगी। थैंक यू, बाय।",
            self.call_state,
        )
        self.call_end_requested = True

        try:
            if self.session and spoken_farewell and not self.call_end_completed:
                speech = await self.session.say(
                    spoken_farewell,
                    allow_interruptions=False,
                    add_to_chat_ctx=True,
                )
                await speech.wait_for_playout()

            if participant_identity:
                try:
                    await self.ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=self.ctx.room.name,
                            identity=participant_identity,
                        )
                    )
                    logger.info("Ended call for participant %s", participant_identity)
                except Exception as exc:
                    if "participant does not exist" in str(exc).lower():
                        logger.info("Participant %s was already gone while ending the call.", participant_identity)
                    else:
                        raise
            else:
                logger.warning("No remote participant found while ending call.")

            self.call_end_completed = True
            return "Call ended successfully. The call will close now."
        except Exception as e:
            logger.error(f"Failed to end call cleanly: {e}")
            return f"Call end requested, but cleanup reported an error: {e}"


class OutboundAssistant(Agent):
    """
    An AI agent tailored for outbound calls.
    Attempts to be helpful and concise.
    """
    def __init__(
        self,
        instructions: str,
        tools: list,
        call_state: CallState,
        llm_provider: str | None = None,
        session: AgentSession | None = None,
        ctx: agents.JobContext | None = None,
        phone_number: str | None = None,
    ) -> None:
        super().__init__(
            instructions=instructions,
            tools=tools,
        )
        self.call_state = call_state
        self.llm_provider = llm_provider
        self._voice_session = session
        self._ctx = ctx
        self._phone_number = phone_number
        self.last_action: dict | None = None
        self.last_normalized: NormalizedUserInput | None = None
        self.last_off_topic: bool = False
        self._turn_id = 0
        self._turn_output_started: dict[int, bool] = {}
        self._turn_spoken: dict[int, bool] = {}
        self._turn_started_at: dict[int, float] = {}
        self._watchdog_turns: set[int] = set()
        self._watchdog_task: asyncio.Task | None = None
        self._call_close_task: asyncio.Task | None = None
        self._watchdog_delay = float(os.getenv("RESPONSE_WATCHDOG_SECONDS", "1.2"))

    def _current_fallback(self) -> str:
        return validate_llm_response_for_voice(
            render_controller_fallback_response(
                self.call_state,
                self.last_action,
                self.last_normalized,
                self.last_off_topic,
            )
        )

    def _mark_output_started(self, turn_id: int, source: str, text: str | None = None) -> None:
        self._turn_output_started[turn_id] = True
        started_at = self._turn_started_at.get(turn_id)
        latency_ms = (time.monotonic() - started_at) * 1000 if started_at else None
        if text:
            if latency_ms is not None:
                logger.info(
                    "Assistant response started via %s for turn %s after %.1f ms: %s",
                    source,
                    turn_id,
                    latency_ms,
                    text,
                )
            else:
                logger.info("Assistant response started via %s for turn %s: %s", source, turn_id, text)
        else:
            if latency_ms is not None:
                logger.info(
                    "Assistant response started via %s for turn %s after %.1f ms.",
                    source,
                    turn_id,
                    latency_ms,
                )
            else:
                logger.info("Assistant response started via %s for turn %s.", source, turn_id)

    def _mark_spoken(self, turn_id: int, text: str) -> None:
        self._turn_output_started[turn_id] = True
        self._turn_spoken[turn_id] = True
        started_at = self._turn_started_at.get(turn_id)
        if started_at:
            logger.info(
                "Final assistant speech before TTS for turn %s after %.1f ms: %s",
                turn_id,
                (time.monotonic() - started_at) * 1000,
                text,
            )
        else:
            logger.info("Final assistant speech before TTS for turn %s: %s", turn_id, text)

    def _should_watchdog_turn(self, action: dict | None, fallback: str) -> bool:
        if not self._voice_session or not fallback or not action:
            return False
        if action.get("should_ask"):
            return True
        return _action_has_invalid_done(action)

    def _schedule_response_watchdog(self, turn_id: int) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        fallback = self._current_fallback()
        if not self._should_watchdog_turn(self.last_action, fallback):
            return

        async def _watchdog() -> None:
            try:
                await asyncio.sleep(self._watchdog_delay)
                if turn_id != self._turn_id:
                    return
                if self._turn_output_started.get(turn_id) or self._turn_spoken.get(turn_id):
                    return
                fallback_text = self._current_fallback()
                if not fallback_text or not self._voice_session:
                    return
                self._watchdog_turns.add(turn_id)
                self._mark_output_started(turn_id, "watchdog", fallback_text)
                logger.warning(
                    "Response watchdog fired for turn %s; saying deterministic fallback.",
                    turn_id,
                )
                await self._voice_session.say(
                    fallback_text,
                    allow_interruptions=True,
                    add_to_chat_ctx=True,
                )
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Response watchdog failed for turn %s: %s", turn_id, exc)

        self._watchdog_task = asyncio.create_task(_watchdog())

    def _model_can_use_call_tools(self) -> bool:
        if not self.call_state:
            return False
        if self.call_state.customer_requested_end or self.call_state.not_interested:
            return True
        return self.call_state.current_step == "done" and self.call_state.done_reason in {
            "interested",
            "interested_missing_detail",
            "not_interested",
            "callback_later",
            "wrong_lead",
            "customer_exit",
        }

    def _should_close_after_response(self) -> bool:
        action = self.last_action or {}
        done_reason = action.get("done_reason") or self.call_state.done_reason
        return action.get("current_step") == "done" and done_reason in {
            "interested",
            "interested_missing_detail",
            "not_interested",
            "callback_later",
            "wrong_lead",
            "customer_exit",
            "unclear_interest",
        }

    def _resolve_remote_participant_identity(self) -> str | None:
        if self._phone_number:
            return f"sip_{self._phone_number}"
        if not self._ctx:
            return None
        for participant in self._ctx.room.remote_participants.values():
            if participant.identity != self._ctx.local_participant_identity:
                return participant.identity
        return None

    def _schedule_call_close_after_response(self, text: str) -> None:
        if not self._ctx or not self._should_close_after_response():
            return
        if self._call_close_task and not self._call_close_task.done():
            return

        delay = max(1.8, min(5.5, len(text.split()) * 0.32 + 0.8))

        async def _close_after_playout() -> None:
            try:
                await asyncio.sleep(delay)
                participant_identity = self._resolve_remote_participant_identity()
                if participant_identity:
                    await self._ctx.api.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=self._ctx.room.name,
                            identity=participant_identity,
                        )
                    )
                    logger.info(
                        "Deterministic close removed participant %s after %.1fs.",
                        participant_identity,
                        delay,
                    )
                else:
                    logger.warning("Deterministic close could not find remote participant.")
                self._ctx.shutdown("Deterministic call close completed.")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("Deterministic call close failed: %s", exc)

        self._call_close_task = asyncio.create_task(
            _close_after_playout(),
            name="deterministic_call_close",
        )

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        text = new_message.text_content
        if not text:
            return

        turn_started_at = time.monotonic()
        normalized = update_state_from_user_input(text, self.call_state)
        sales_analysis = await _analyze_sales_turn(text, self.call_state, self.llm_provider)
        apply_sales_turn_analysis(self.call_state, sales_analysis)
        action = decide_next_action(self.call_state, normalized)
        semantic_non_off_topic = self.call_state.last_turn_type in {
            "answer",
            "permission",
            "repair",
            "identity_question",
            "call_purpose_question",
            "business_question",
            "objection",
            "rejection",
            "callback_request",
            "wrong_lead",
            "confusion",
        }
        off_topic = self.call_state.last_turn_type == "off_topic" or (
            is_off_topic_user_input(text, normalized) and not semantic_non_off_topic
        )
        self.last_action = action
        self.last_normalized = normalized
        self.last_off_topic = off_topic
        self._turn_id += 1
        current_turn_id = self._turn_id
        self._turn_output_started[current_turn_id] = False
        self._turn_spoken[current_turn_id] = False
        self._turn_started_at[current_turn_id] = turn_started_at
        logger.info(
            "Controller turn: text=%r action=%s state=%s",
            text.strip(),
            json.dumps(action, ensure_ascii=False),
            json.dumps(self.call_state.public_state(), ensure_ascii=False),
        )
        logger.info(
            "Turn %s controller decision latency: %.1f ms",
            current_turn_id,
            (time.monotonic() - turn_started_at) * 1000,
        )
        turn_ctx.add_message(
            role="system",
            content=build_controller_context(
                self.call_state,
                action,
                normalized,
                latest_user_text=text,
                sales_analysis=sales_analysis,
                off_topic=off_topic,
            ),
        )
        turn_ctx.truncate(max_items=4)
        self._schedule_response_watchdog(current_turn_id)

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings):
        effective_tools = tools if self._model_can_use_call_tools() else []
        produced_output = False
        turn_id = self._turn_id
        fallback = self._current_fallback()
        force_deterministic = bool(fallback) and should_force_deterministic_response(
            self.call_state,
            self.last_action,
            self.last_normalized,
        )

        if force_deterministic:
            logger.info("Using immediate controller speech fallback: %s", fallback)
            self._mark_output_started(turn_id, "deterministic_fallback", fallback)
            yield fallback
            return

        if fallback and should_use_fast_semantic_response(
            self.call_state,
            self.last_action,
            self.last_normalized,
        ):
            logger.info("Using fast semantic response: %s", fallback)
            self._mark_output_started(turn_id, "fast_semantic_response", fallback)
            yield fallback
            return

        try:
            async for chunk in Agent.default.llm_node(self, chat_ctx, effective_tools, model_settings):
                if turn_id in self._watchdog_turns:
                    logger.info("Suppressing late LLM output for turn %s after watchdog fallback.", turn_id)
                    return
                if isinstance(chunk, str):
                    produced_output = produced_output or bool(chunk.strip())
                    if chunk.strip() and not self._turn_output_started.get(turn_id):
                        self._mark_output_started(turn_id, "llm")
                elif getattr(chunk, "delta", None):
                    delta = chunk.delta
                    chunk_has_output = bool(
                        (delta.content or "").strip() or delta.tool_calls
                    )
                    produced_output = produced_output or chunk_has_output
                    if chunk_has_output and not self._turn_output_started.get(turn_id):
                        self._mark_output_started(turn_id, "llm")
                yield chunk
        except Exception as exc:
            logger.warning("LLM generation failed; using controller fallback: %s", exc)
            if fallback:
                self._mark_output_started(turn_id, "llm_exception_fallback", fallback)
                yield fallback
            return

        if not produced_output and fallback:
            logger.warning("LLM produced no output; using controller fallback.")
            self._mark_output_started(turn_id, "empty_llm_fallback", fallback)
            yield fallback

    def tts_node(
        self, text, model_settings
    ):
        turn_id = self._turn_id

        async def sanitized_text():
            chunks: list[str] = []
            async for chunk in text:
                if chunk:
                    chunks.append(str(chunk))

            cleaned = _sanitize_spoken_text("".join(chunks), self.call_state)
            validated = validate_llm_response_for_voice(cleaned)
            if (
                validated
                and self.last_action
                and self.last_action.get("should_ask")
                and "?" not in validated
            ):
                fallback = self._current_fallback()
                if fallback:
                    logger.warning(
                        "LLM response missed required controller question; using fallback: %s",
                        fallback,
                    )
                    validated = fallback
            if validated:
                self._mark_spoken(turn_id, validated)
                self._schedule_call_close_after_response(validated)
                yield validated

        return Agent.default.tts_node(self, sanitized_text(), model_settings)




async def entrypoint(ctx: agents.JobContext):
    """
    Main entrypoint for the agent.
    
    For outbound calls:
    1. Checks for 'phone_number' in the job metadata.
    2. Connects to the room.
    3. Initiates the SIP call to the phone number.
    4. Waits for answer before speaking.
    """
    logger.info(f"Connecting to room: {ctx.room.name}")
    
    # parse the phone number AND config from the metadata
    phone_number = None
    config_dict = {}
    conversation_log: list[str] = []
    seen_message_ids: set[str] = set()
    session_closed = asyncio.Event()
    call_state = CallState()
    
    # Check Job Metadata (Legacy/Dispatch)
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
            config_dict = data
    except Exception:
        pass
        
    # Check Room Metadata (Dashboard/Route.ts) - Overrides Job Metadata if present
    try:
        if ctx.room.metadata:
            data = json.loads(ctx.room.metadata)
            if data.get("phone_number"):
                phone_number = data.get("phone_number")
            config_dict.update(data) # Merge configs
    except Exception:
        logger.warning("No valid JSON metadata found in Room.")

    await ctx.connect()

    # Initialize function context
    # Initialize the Agent Session with plugins
    session = AgentSession(
        vad=silero.VAD.load(min_silence_duration=0.22),
        stt=_build_stt(config_dict.get("stt_language")),
        llm=_build_llm(config_dict.get("llm_provider") or config_dict.get("model_provider")),
        tts=_build_tts(config_dict.get("tts_provider"), config_dict.get("voice_id")),
        min_endpointing_delay=0.12,
        max_endpointing_delay=0.75,
        min_interruption_duration=0.18,
        min_interruption_words=1,
        allow_interruptions=True,
        false_interruption_timeout=0.7,
        resume_false_interruption=False,
        discard_audio_if_uninterruptible=True,
        user_away_timeout=35.0,
        conn_options=SessionConnectOptions(
            stt_conn_options=APIConnectOptions(max_retry=2, retry_interval=0.4, timeout=8.0),
            llm_conn_options=APIConnectOptions(
                max_retry=int(os.getenv("LLM_SESSION_MAX_RETRY", "0")),
                retry_interval=float(os.getenv("LLM_SESSION_RETRY_INTERVAL", "0.2")),
                timeout=float(os.getenv("LLM_SESSION_TIMEOUT", "4.0")),
            ),
            tts_conn_options=APIConnectOptions(max_retry=1, retry_interval=0.2, timeout=5.0),
            max_unrecoverable_errors=5,
        ),
        preemptive_generation=False,
    )
    fnc_ctx = TransferFunctions(ctx, phone_number, session, call_state)

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event: ConversationItemAddedEvent) -> None:
        if not isinstance(event.item, llm.ChatMessage):
            return

        if event.item.id in seen_message_ids:
            return

        seen_message_ids.add(event.item.id)
        text = event.item.text_content
        if not text:
            return

        conversation_log.append(f"{event.item.role.title()}: {text.strip()}")

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event: UserInputTranscribedEvent) -> None:
        if not event.is_final or not event.transcript.strip():
            return

        # Capture the final raw STT turn directly; this is more reliable than
        # relying only on committed chat messages for short phone-call answers.
        logger.info("Final user transcript: %s", event.transcript.strip())
        conversation_log.append(f"User: {event.transcript.strip()}")

    @session.on("close")
    def _on_close(_: CloseEvent) -> None:
        session_closed.set()

    # Start the session
    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(
            instructions=_build_agent_instructions(config_dict),
            tools=[
                tool
                for name, tool in fnc_ctx.function_tools.items()
                if name in {"end_call"}
            ],
            call_state=call_state,
            llm_provider=config_dict.get("llm_provider") or config_dict.get("model_provider"),
            session=session,
            ctx=ctx,
            phone_number=phone_number,
        ),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True, # Close room when agent disconnects
        ),
    )

    # Logic to dial out:
    # 1. If 'phone_number' is present, we MIGHT need to dial.
    # 2. Check if a SIP participant is already in the room (Dashboard dispatch case).
    
    should_dial = False
    if phone_number:
        # Check if any remote participant looks like our user (sip_PHONE)
        user_already_here = False
        for p in ctx.room.remote_participants.values():
            if f"sip_{phone_number}" in p.identity or "sip_" in p.identity:
                user_already_here = True
                break
        
        if not user_already_here:
            should_dial = True
            logger.info("User not in room. Agent will initiate dial-out.")
        else:
            logger.info("User already in room (Dashboard dispatched). output Only generated greeting.")

    if should_dial:
        logger.info(f"Initiating outbound SIP call to {phone_number}...")
        try:
            answered_trunk = await _dial_outbound_call(ctx, phone_number)
            logger.info("Call answered on trunk %s. Agent is now listening.", answered_trunk)
            
            await session.say(
                _build_outbound_greeting(),
                allow_interruptions=True,
                add_to_chat_ctx=True,
            )
            
        except OutboundDialError as e:
            logger.error("Failed to place outbound call: %s", e)
            if e.errors:
                logger.error("Outbound SIP failure history: %s", " | ".join(e.errors))
            session_closed.set()
            ctx.shutdown()
        except Exception as e:
            logger.error("Failed to place outbound call: %s", e)
            # Ensure we clean up if the call fails
            session_closed.set()
            ctx.shutdown()
    else:
        # Fallback for inbound calls (if this agent is used for that) OR Dashboard calls where user is already there
        logger.info("Detecting if we should greet...")
        await session.say(
            _build_fallback_greeting(),
            allow_interruptions=True,
            add_to_chat_ctx=True,
        )

    await session_closed.wait()
    logger.info(
        "Session closed for room %s. Preparing sheet update for row %s.",
        ctx.room.name,
        config_dict.get("sheet_row"),
    )

    try:
        await _update_sheet_after_call(config_dict, session, conversation_log)
        logger.info(
            "Sheet update finished for row %s in room %s.",
            config_dict.get("sheet_row"),
            ctx.room.name,
        )
    except Exception:
        logger.exception(
            "Sheet update failed for row %s in room %s.",
            config_dict.get("sheet_row"),
            ctx.room.name,
        )
    finally:
        if fnc_ctx.call_end_requested:
            await asyncio.sleep(0.2)
        ctx.shutdown("Call processing finished.")


if __name__ == "__main__":
    # The agent name "outbound-caller" is used by the dispatch script to find this worker
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller", 
            load_threshold=1.0,
        )
    )
