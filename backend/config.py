"""config.py — Central configuration module: loads .env variables, sets up the Azure OpenAI client, and holds shared in-memory state (sessions/clients locks) used across the whole backend."""
import os
import threading
from dotenv import load_dotenv
from openai import AzureOpenAI

try:
    _ok = load_dotenv(override=True)
    print(f"[dotenv] .env loaded: {_ok}")
except ImportError:
    print("[dotenv] python-dotenv not installed — run: pip install python-dotenv")


# ══════════════════════════════════════════════════════════════════════════════
# AZURE SPEECH / AVATAR
# ══════════════════════════════════════════════════════════════════════════════
SPEECH_KEY      = os.getenv("SPEECH_KEY")
SPEECH_REGION   = os.getenv("SPEECH_REGION", "eastus")
AVATAR_CHARACTER = os.getenv("AVATAR_CHARACTER")
AVATAR_STYLE     = os.getenv("AVATAR_STYLE")
TTS_VOICE        = os.getenv("TTS_VOICE")
COSMOS_EVALUATIONS_CONTAINER = os.getenv("COSMOS_EVALUATIONS_CONTAINER")
# ══════════════════════════════════════════════════════════════════════════════
# AZURE OPENAI
# ══════════════════════════════════════════════════════════════════════════════
AZURE_OAI_ENDPOINT   = os.getenv("AZURE_OPENAI_CHAT_ENDPOINT")
AZURE_OAI_KEY        = os.getenv("AZURE_OPENAI_CHAT_KEY")
AZURE_OAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")

openai_client = AzureOpenAI(
    azure_endpoint=AZURE_OAI_ENDPOINT,
    api_key=AZURE_OAI_KEY,
    api_version="2024-06-01",
    max_retries=1,
    timeout=20,
)

# ══════════════════════════════════════════════════════════════════════════════
# FLASK
# ══════════════════════════════════════════════════════════════════════════════
FLASK_SECRET = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# ══════════════════════════════════════════════════════════════════════════════
# INTERVIEW TIMING CONSTANTS  (sent to frontend via /api/config)
# ══════════════════════════════════════════════════════════════════════════════
SEMANTIC_VAD_MIN_WORDS      = 3
SEMANTIC_VAD_TIMEOUT_MS     = 800
AVATAR_TURN_END_DELAY_MS    = 600
INITIAL_SILENCE_TIMEOUT_MS  = 10000
END_OF_UTTERANCE_SILENCE_MS = 1800
INTERRUPT_MIN_PARTIAL_MS    = 500
TURN_SUBMIT_DELAY_MS        = 150

# ══════════════════════════════════════════════════════════════════════════════
# COSMOS DB  (raw env vars — the full connector logic lives in cosmos_db_connector.py)
# Exposed here so routes_debug.py and routes_interview.py can read them without
# importing the full connector.
# ══════════════════════════════════════════════════════════════════════════════
COSMOS_ENDPOINT          = os.getenv("COSMOS_ENDPOINT", "")
COSMOS_DATABASE          = os.getenv("COSMOS_DATABASE_NAME", "")
COSMOS_USERS_CONTAINER   = os.getenv("COSMOS_CONTAINER", "users")
COSMOS_RESUME_CONTAINER  = os.getenv("COSMOS_RESUME_CONTAINER", "resumes")
COSMOS_JD_CONTAINER      = os.getenv("COSMOS_JD_CONTAINER", "jobdescriptions")

# ══════════════════════════════════════════════════════════════════════════════
# SHARED IN-MEMORY STATE  (avatar clients + interview sessions)
# Both dicts are written/read by multiple threads — always use the locks.
# ══════════════════════════════════════════════════════════════════════════════
clients: dict            = {}
clients_lock             = threading.Lock()
interview_sessions: dict = {}
sessions_lock            = threading.Lock()

