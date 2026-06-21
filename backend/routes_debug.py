"""
routes_debug.py
===============
Flask Blueprint for diagnostic and healthcheck routes.
These are safe to call at any time without affecting live sessions.

  GET /api/debug/resumes          → list all resume IDs in Cosmos
  GET /api/debug/jds              → list all JD IDs in Cosmos
  GET /api/debug/users            → list all user IDs in Cosmos
  GET /api/debug/cosmos/<ctr>     → diagnose a Cosmos container
  GET /api/debug/ice              → test ICE token endpoint
  GET /api/healthcheck            → full health report (keys, Cosmos, Speech)
"""

import traceback as _tb

from flask import Blueprint, request, jsonify

from config import (
    SPEECH_KEY, SPEECH_REGION,
    AZURE_OAI_KEY, AZURE_OAI_ENDPOINT,
    COSMOS_ENDPOINT, COSMOS_DATABASE,
    COSMOS_USERS_CONTAINER, COSMOS_RESUME_CONTAINER, COSMOS_JD_CONTAINER,
)
from cosmos_db_connector import (
    list_all_resumes, list_all_jds, list_all_users,
    diagnose_cosmos, check_endpoint_format,
)
from speech_service import get_speech_token, get_ice_token

debug_bp = Blueprint("debug", __name__)


# ── Cosmos data explorers ─────────────────────────────────────────────────────

@debug_bp.route("/api/debug/resumes")
def debug_resumes():
    """List up to 50 resume documents.  Use this to find valid resume IDs."""
    try:
        return jsonify(list_all_resumes(50))
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


@debug_bp.route("/api/debug/jds")
def debug_jds():
    """List up to 50 JD documents."""
    try:
        return jsonify(list_all_jds(50))
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


@debug_bp.route("/api/debug/users")
def debug_users():
    try:
        return jsonify(list_all_users(50))
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


@debug_bp.route("/api/debug/cosmos/<ctr>")
def debug_cosmos(ctr):
    """Diagnose a Cosmos container.  Add ?id=<doc_id> to test a specific lookup."""
    try:
        return jsonify(diagnose_cosmos(ctr, request.args.get("id")))
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


@debug_bp.route("/api/debug/ice")
def debug_ice():
    """Test the ICE token endpoint and return key names + first URL."""
    try:
        d = get_ice_token()
        return jsonify({
            "region": SPEECH_REGION,
            "keys":   list(d.keys()),
            "url":    (d.get("Urls") or d.get("urls") or ["none"])[0],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Full healthcheck ──────────────────────────────────────────────────────────

@debug_bp.route("/api/healthcheck")
def api_healthcheck():
    """
    Check all required Azure service connections and return a status report.
    Look at 'overall' for a quick pass/fail, then individual fields for detail.
    """
    def _fmt(k):
        return f"SET ({len(k)} chars)" if k else "MISSING ✗"

    ep_valid, ep_msg = check_endpoint_format()

    result = {
        "SPEECH_KEY":          _fmt(SPEECH_KEY),
        "SPEECH_REGION":       SPEECH_REGION or "MISSING ✗",
        "OPENAI_KEY":          _fmt(AZURE_OAI_KEY),
        "OPENAI_ENDPOINT":     AZURE_OAI_ENDPOINT or "MISSING ✗",
        "COSMOS_ENDPOINT":     COSMOS_ENDPOINT    or "MISSING ✗",
        "COSMOS_ENDPOINT_OK":  ep_valid,
        "COSMOS_ENDPOINT_MSG": ep_msg,
        "COSMOS_DATABASE":     COSMOS_DATABASE    or "MISSING ✗",
        "COSMOS_RESUME_CTR":   COSMOS_RESUME_CONTAINER,
        "COSMOS_JD_CTR":       COSMOS_JD_CONTAINER,
        "COSMOS_USERS_CTR":    COSMOS_USERS_CONTAINER,
        "speech_token_test":   None,
        "ice_token_test":      None,
        "cosmos_resumes_test": None,
        "cosmos_jds_test":     None,
    }

    try:
        tok = get_speech_token()
        result["speech_token_test"] = f"OK ({len(tok)} chars)"
    except Exception as e:
        result["speech_token_test"] = f"FAILED — {e}"

    try:
        ice = get_ice_token()
        result["ice_token_test"] = f"OK keys={list(ice.keys())}"
    except Exception as e:
        result["ice_token_test"] = f"FAILED — {e}"

    for key, ctr in [("cosmos_resumes_test", COSMOS_RESUME_CONTAINER),
                     ("cosmos_jds_test",     COSMOS_JD_CONTAINER)]:
        try:
            r = diagnose_cosmos(ctr)
            if r["errors"]:
                result[key] = f"ERRORS: {r['errors']}"
            else:
                ids = [d["id"] for d in r["sample_docs"]]
                result[key] = f"OK — {len(ids)} sample ids: {ids}"
        except Exception as e:
            result[key] = f"FAILED — {e}"

    ok = (
        bool(SPEECH_KEY) and ep_valid
        and "OK" in str(result["cosmos_resumes_test"])
        and "OK" in str(result["cosmos_jds_test"])
    )
    result["overall"] = "ALL OK ✓" if ok else "PROBLEMS FOUND — read fields above"
    return jsonify(result)
