"""
app.py  —  Entry point for the NDIL AI Interviewer backend
===========================================================

Responsibilities of this file (and ONLY this file):
  1. Create the Flask application instance
  2. Point Flask at the frontend template and static folders
  3. Register all route Blueprints
  4. Print startup diagnostics
  5. Run the dev server when executed directly

Everything else lives in dedicated modules:
  config.py            — env vars, shared state, openai_client
  speech_service.py    — Azure Speech / ICE token helpers
  interview_session.py — all interview business logic
  routes_avatar.py     — Blueprint: /, /api/config, /api/getSpeechToken, etc.
  routes_interview.py  — Blueprint: /api/startInterview, /api/userResponse, etc.
  routes_debug.py      — Blueprint: /api/debug/*, /api/healthcheck
  blob_storage.py      — Azure Blob Storage helpers  (unchanged)
  cosmos_db_connector.py — Cosmos DB connector       (unchanged)
  evaluator.py         — Post-interview AI evaluation (unchanged)

Run:
    cd backend
    python app.py
"""

import os

from flask import Flask

# ── Config must be imported first (triggers load_dotenv) ─────────────────────
from config import (
    FLASK_SECRET,
    SPEECH_KEY, SPEECH_REGION,
    AZURE_OAI_KEY, AZURE_OAI_ENDPOINT,
    COSMOS_ENDPOINT, COSMOS_DATABASE,
    COSMOS_USERS_CONTAINER, COSMOS_RESUME_CONTAINER, COSMOS_JD_CONTAINER,
)
from cosmos_db_connector import check_endpoint_format

# ── Blueprints ────────────────────────────────────────────────────────────────
from routes_avatar    import avatar_bp
from routes_interview import interview_bp
from routes_debug     import debug_bp

# ══════════════════════════════════════════════════════════════════════════════
# FLASK APPLICATION FACTORY
# ══════════════════════════════════════════════════════════════════════════════

def create_app() -> Flask:
    """
    Create and configure the Flask application.
    Frontend assets are served directly from the sibling `frontend/` folder
    so no separate static-file server is needed during development.
    """
    _base = os.path.dirname(__file__)   # → .../project/backend

    app = Flask(
        __name__,
        template_folder=os.path.join(_base, "..", "frontend", "templates"),
        static_folder=os.path.join(_base,   "..", "frontend", "static"),
    )
    app.secret_key = FLASK_SECRET

    # ── Register blueprints ───────────────────────────────────────────────────
    app.register_blueprint(avatar_bp)      # /, /api/config, /api/getSpeechToken …
    app.register_blueprint(interview_bp)   # /api/startInterview, /api/userResponse …
    app.register_blueprint(debug_bp)       # /api/debug/*, /api/healthcheck

    return app


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP DIAGNOSTICS  (printed only when running directly)
# ══════════════════════════════════════════════════════════════════════════════

def _print_startup_diagnostics() -> None:
    ep_valid, ep_msg = check_endpoint_format()
    w = 68
    print("\n" + "=" * w)
    print("  STARTUP DIAGNOSTICS")
    print("=" * w)
    print(f"  SPEECH_KEY          : {'SET ✓' if SPEECH_KEY else 'MISSING ✗ ← add to .env'}")
    print(f"  SPEECH_REGION       : {SPEECH_REGION}")
    print(f"  OPENAI_KEY          : {'SET ✓' if AZURE_OAI_KEY else 'MISSING ✗ ← add to .env'}")
    print(f"  COSMOS_ENDPOINT     : {COSMOS_ENDPOINT or 'MISSING ✗ ← add to .env'}")
    if not ep_valid:
        print(f"  ⚠  ENDPOINT PROBLEM : {ep_msg}")
        print(f"     HOW TO FIX: portal.azure.com → Cosmos DB account → Keys → URI")
    print(f"  COSMOS_DATABASE     : {COSMOS_DATABASE or 'MISSING ✗ ← add COSMOS_DATABASE_NAME'}")
    print(f"  COSMOS_RESUME_CTR   : {COSMOS_RESUME_CONTAINER}")
    print(f"  COSMOS_JD_CTR       : {COSMOS_JD_CONTAINER}")
    print(f"  COSMOS_USERS_CTR    : {COSMOS_USERS_CONTAINER}")
    print(f"\n  Healthcheck         : http://localhost:5000/api/healthcheck")
    print(f"  List resume IDs     : http://localhost:5000/api/debug/resumes")
    print(f"  List JD IDs         : http://localhost:5000/api/debug/jds")
    print(f"  Interview UI        : http://localhost:5000/")
    print(f"  Start interview     : POST /api/startInterview")
    print(f'    body: {{"resume_id":"AJAY_BONGANE_9eeff9c6-720c-4e6e-ac29-f2f81e24f9ad"}}')
    print("=" * w + "\n")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

app = create_app()

if __name__ == "__main__":
    _print_startup_diagnostics()
    app.run(host="0.0.0.0", port=5000, debug=True)
