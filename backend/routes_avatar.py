"""
routes_avatar.py
================
Flask Blueprint for avatar-related and speech-token routes:

  GET  /                                           → serve index.html (Jinja2)
  GET  /api/config                                 → runtime config for frontend
  GET  /api/getSpeechToken                         → Azure Speech token
  GET  /api/getIceToken                            → ICE relay token for WebRTC
  POST /api/connectAvatar                          → register avatar client
  POST /api/releaseClient                          → release avatar client
  GET  /favicon.ico
  GET  /.well-known/appspecific/com.chrome.devtools.json
"""

import uuid
import datetime
import traceback as _tb

from flask import Blueprint, render_template, request, jsonify, Response

from config import (
    SPEECH_REGION, AVATAR_CHARACTER, AVATAR_STYLE, TTS_VOICE,
    AVATAR_TURN_END_DELAY_MS, INITIAL_SILENCE_TIMEOUT_MS,
    END_OF_UTTERANCE_SILENCE_MS, INTERRUPT_MIN_PARTIAL_MS,
    TURN_SUBMIT_DELAY_MS, SEMANTIC_VAD_MIN_WORDS, SEMANTIC_VAD_TIMEOUT_MS,
    clients, clients_lock,
)
from speech_service import get_speech_token, get_ice_token

avatar_bp = Blueprint("avatar", __name__)


# ── Serve the main interview UI ───────────────────────────────────────────────

@avatar_bp.route("/")
def index():
    return render_template(
        "index.html",
        avatar_character=AVATAR_CHARACTER,
        avatar_style=AVATAR_STYLE,
        tts_voice=TTS_VOICE,
        speech_region=SPEECH_REGION,
        avatar_turn_end_delay_ms=AVATAR_TURN_END_DELAY_MS,
        initial_silence_timeout_ms=INITIAL_SILENCE_TIMEOUT_MS,
        end_of_utterance_silence_ms=END_OF_UTTERANCE_SILENCE_MS,
        interrupt_min_partial_ms=INTERRUPT_MIN_PARTIAL_MS,
        turn_submit_delay_ms=TURN_SUBMIT_DELAY_MS,
        semantic_vad_min_words=SEMANTIC_VAD_MIN_WORDS,
        semantic_vad_timeout_ms=SEMANTIC_VAD_TIMEOUT_MS,
    )


# ── Runtime config endpoint (consumed by frontend JS if needed) ───────────────

@avatar_bp.route("/api/config")
def api_config():
    return jsonify({
        "speech_region":              SPEECH_REGION,
        "avatar_character":           AVATAR_CHARACTER,
        "avatar_style":               AVATAR_STYLE,
        "tts_voice":                  TTS_VOICE,
        "avatar_turn_end_delay_ms":   AVATAR_TURN_END_DELAY_MS,
        "initial_silence_timeout_ms": INITIAL_SILENCE_TIMEOUT_MS,
        "end_of_utterance_silence_ms": END_OF_UTTERANCE_SILENCE_MS,
        "interrupt_min_partial_ms":   INTERRUPT_MIN_PARTIAL_MS,
        "turn_submit_delay_ms":       TURN_SUBMIT_DELAY_MS,
        "semantic_vad_min_words":     SEMANTIC_VAD_MIN_WORDS,
        "semantic_vad_timeout_ms":    SEMANTIC_VAD_TIMEOUT_MS,
    })


# ── Speech token (pre-fetched by interview.js on page load) ───────────────────

@avatar_bp.route("/api/getSpeechToken")
def api_speech_token():
    """
    Return an Azure Speech token.

    Query params:
      ?force=1  — bypass the 9-minute backend cache and fetch a brand-new token
                  from Azure.  Used by the frontend after a reactive STT rebuild
                  (StatusCode 1006 auth failure) to guarantee a fully fresh token.

    Response JSON:
      { "token": "...", "region": "...", "expiresAt": <unix-milliseconds> }
    """
    try:
        force = request.args.get("force", "0") == "1"
        result = get_speech_token(force=force)
        return jsonify(result)
    except PermissionError as e:
        return jsonify({"error": str(e)}), 401
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


# ── ICE relay token (needed to establish WebRTC peer connection for avatar) ───

@avatar_bp.route("/api/getIceToken")
def api_ice_token():
    try:
        return jsonify(get_ice_token())
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Avatar client registration ────────────────────────────────────────────────

@avatar_bp.route("/api/connectAvatar", methods=["POST"])
def api_connect_avatar():
    """
    Register an avatar client.  If the POST body carries candidate-specific
    avatar fields (sourced from Cosmos DB resume metadata), those values take
    precedence over the .env defaults so every interview uses the character
    assigned to that candidate.

    Expected body (all fields optional):
      {
        "client_id":        "<uuid>",
        "avatar_character": "<character>",  # overrides AVATAR_CHARACTER env var
        "avatar_style":     "<style>",      # overrides AVATAR_STYLE env var
        "tts_voice":        "<voice>",      # overrides TTS_VOICE env var
      }
    """
    data      = request.get_json(silent=True) or {}
    client_id = data.get("client_id") or str(uuid.uuid4())

    # Prefer candidate-specific values from Cosmos DB; fall back to .env defaults
    character = data.get("avatar_character") or AVATAR_CHARACTER
    style     = data.get("avatar_style")     or AVATAR_STYLE
    voice     = data.get("tts_voice")        or TTS_VOICE

    if character and character.lower() == "lisa":
        style = "casual-sitting"

    with clients_lock:
        clients[client_id] = {
            "created":          datetime.datetime.now().isoformat(),
            "avatar_character": character,
            "avatar_style":     style,
            "tts_voice":        voice,
        }
    return jsonify({
        "client_id":        client_id,
        "avatar_character": character,
        "avatar_style":     style,
        "tts_voice":        voice,
        "speech_region":    SPEECH_REGION,
    })


@avatar_bp.route("/api/releaseClient", methods=["POST"])
def api_release_client():
    data = request.get_json(silent=True) or {}
    with clients_lock:
        clients.pop(data.get("client_id", ""), None)
    return jsonify({"ok": True})


# ── Browser noise suppression ─────────────────────────────────────────────────

@avatar_bp.route("/favicon.ico")
def favicon():
    return Response(status=204)


@avatar_bp.route("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools():
    return Response(status=204)