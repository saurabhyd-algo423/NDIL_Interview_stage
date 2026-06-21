"""
routes_interview.py
===================
Flask Blueprint for all interview lifecycle routes:

  POST /api/startInterview           → start session, return greeting
  POST /api/userResponse             → handle candidate reply (blocking)
  POST /api/userResponseStream       → handle candidate reply (SSE stream)
  POST /api/notifyInterrupt          → record avatar interrupt position
  POST /api/resumeAfterInterrupt     → resume after interrupt (blocking)
  POST /api/resumeAfterInterruptStream → resume after interrupt (SSE stream)
  POST /api/semanticVad              → server-side VAD check
  GET  /api/sessionStatus            → current phase / progress
  POST /api/saveTranscription        → background / final transcript save
  POST /api/uploadRecording          → upload webm recording chunk
  POST /api/finalizeInterview        → trigger background AI evaluation
"""

import re
import json
import uuid
import datetime
import threading
import tempfile
import os
import traceback as _tb

from flask import Blueprint, request, jsonify, Response

from config import (
    interview_sessions, sessions_lock,
)
from cosmos_db_connector import fetch_resume_with_linked_jd
from blob_storage import (
    blob_storage_configured,
    upload_recording,
    check_candidate_folder_exists,
    delete_candidate_folder,
    get_latest_transcription_blob,
)
from evaluator import evaluate_candidate
from interview_session import (
    PHASES,
    semantic_vad_check,
    analyze_candidate_speech,
    generate_nudge,
    stream_nudge,
    is_role_confirmation_yes,
    is_role_confirmation_no,
    has_role_confusion,
    create_session,
    get_ai_response,
    get_remaining_text,
    stream_gpt_sentences,
    auto_save_session,
    save_turn_async,
    build_transcription_payload,
    _phase_ctx,
    _should_advance,
    _cover,
)

interview_bp = Blueprint("interview", __name__)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS — local to this module
# ══════════════════════════════════════════════════════════════════════════════

def _get_candidate_data(resume_id: str) -> dict:
    """Fetch resume + linked JD from Cosmos DB."""
    print(f"[DB] Fetching resume_id='{resume_id}' (jd_id read from resume document)")
    return fetch_resume_with_linked_jd(resume_id)


def _process_background_evaluation(resume_id: str) -> None:
    """
    Downloads the latest transcript from Blob Storage, runs the AI evaluator,
    and writes results back to Cosmos DB.  Runs in a daemon thread.
    """
    try:
        import time
        time.sleep(3)   # give the final transcription upload a moment to complete

        transcript_data = get_latest_transcription_blob(resume_id)
        if not transcript_data:
            print(f"[Eval] No transcript found in Blob for {resume_id}")
            return

        with tempfile.NamedTemporaryFile(
                delete=False, suffix=".json", mode="w", encoding="utf-8") as tf:
            json.dump(transcript_data, tf)
            temp_path = tf.name

        print(f"[Eval] Starting AI Evaluation for Resume: {resume_id}")
        success, _results, error = evaluate_candidate(resume_id, temp_path)

        if success:
            print(f"[Eval] Successfully updated Cosmos DB for {resume_id}")
        else:
            print(f"[Eval] Evaluation failed: {error}")

        if os.path.exists(temp_path):
            os.remove(temp_path)

    except Exception as e:
        print(f"[Eval] Critical background error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — SEMANTIC VAD
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/semanticVad", methods=["POST"])
def api_semantic_vad():
    data     = request.get_json(silent=True) or {}
    text     = (data.get("text") or "").strip()
    is_final = bool(data.get("is_final", False))
    if not text:
        return jsonify({"is_speech": False, "is_complete": False, "reason": "empty"})
    return jsonify(semantic_vad_check(text, is_final=is_final))



# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — NUDGE CANDIDATE (silence / stuck handler)
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/nudgeCandidate", methods=["POST"])
def api_nudge_candidate():
    """
    Called by the frontend when the silence-watch system determines the
    candidate is stuck, silent, or has trailed off.

    POST body:
      {
        "session_id":     "...",
        "nudge_type":     "rephrase" | "move_on" | "post_silence",
        "candidate_text": "partial text candidate said this turn (may be empty)"
      }

    nudge_type semantics:
      rephrase     → candidate was silent after the question;
                     AI gently rephrases / offers a hint; stays on same question.
      move_on      → candidate remained silent even after rephrase nudge;
                     AI acknowledges gracefully and moves to next question.
      post_silence → candidate spoke some words then trailed off;
                     AI acknowledges what was said, asks if they want to continue
                     or pivots naturally to the next question.

    SSE response:
      data: {"type":"sentence","text":"..."}   (one or more)
      data: {"type":"done","interview_ended":bool}
    """
    data           = request.get_json(silent=True) or {}
    sid            = data.get("session_id", "")
    nudge_type     = data.get("nudge_type", "rephrase")
    candidate_text = (data.get("candidate_text") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    if not state["active"]:
        return Response(
            f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n",
            content_type="text/event-stream",
        )

    if nudge_type not in ("rephrase", "move_on", "post_silence"):
        nudge_type = "rephrase"

    print(
        f"[Nudge] sid={sid[:8]} type={nudge_type} "
        f"candidate_words={len(candidate_text.split()) if candidate_text else 0}"
    )

    return Response(
        stream_nudge(state, sid, nudge_type, candidate_text),
        content_type="text/event-stream",
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — ANALYZE CANDIDATE SPEECH (AI interrupt decision)
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/analyzeCandidate", methods=["POST"])
def api_analyze_candidate():
    """
    Called by the frontend every N words while the candidate is speaking.
    Returns whether the AI should interrupt and the phrase to use.

    POST body: { "session_id": "...", "partial_text": "accumulated candidate speech" }
    Response:  { "should_interrupt": bool, "reason": str, "interrupt_phrase": str }
    """
    data         = request.get_json(silent=True) or {}
    sid          = data.get("session_id", "")
    partial_text = (data.get("partial_text") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    if not partial_text:
        return jsonify({"should_interrupt": False, "reason": "empty", "interrupt_phrase": ""})

    result = analyze_candidate_speech(state, partial_text)
    print(
        f"[AIInterrupt] sid={sid[:8]} words={len(partial_text.split())} "
        f"decision={'INTERRUPT' if result['should_interrupt'] else 'CONTINUE'} "
        f"reason={result['reason']}"
    )
    return jsonify(result)


@interview_bp.route("/api/aiInterruptStream", methods=["POST"])
def api_ai_interrupt_stream():
    """
    Called when the AI has decided to interrupt the candidate.
    Records the partial candidate speech, then streams the next AI question.

    POST body:
      {
        "session_id":      "...",
        "partial_text":    "what candidate said before being cut off",
        "interrupt_phrase": "the short phrase the avatar will speak first"
      }

    SSE stream: same format as /api/userResponseStream
      data: {"type":"sentence","text":"..."}
      data: {"type":"done","interview_ended":bool}
    """
    data          = request.get_json(silent=True) or {}
    sid           = data.get("session_id", "")
    partial_text  = (data.get("partial_text")  or "").strip()
    int_phrase    = (data.get("interrupt_phrase") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    if not state["active"]:
        return Response(
            f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n",
            content_type="text/event-stream",
        )

    # Record the partial candidate answer we have so far
    if partial_text:
        state["transcription"].append({
            "speaker":   "Candidate",
            "text":      partial_text,
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
            "phase":     PHASES[state["current_phase"]],
            "note":      "ai_interrupted",
        })

    # Build context: tell GPT it interrupted and what was partially said
    ctx = (
        f"[You interrupted the candidate who was saying: '{partial_text[:200]}'. "
        f"Acknowledge briefly and ask your next question.]"
        if partial_text
        else "[Redirect the candidate and ask your next question.]"
    )
    state["conversation"].append({"role": "user", "content": ctx})
    _cover(state, partial_text)

    if _should_advance(state) and state["current_phase"] < len(PHASES) - 1:
        state["current_phase"] += 1
        state["qa_count"]      = 0
        state["phase_start"]   = datetime.datetime.now()

    if PHASES[state["current_phase"]] == "CLOSING" and state["qa_count"] >= 1:
        state["active"] = False
        save_turn_async(sid, state)
        return Response(
            f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n",
            content_type="text/event-stream",
        )

    msgs = state["conversation"].copy()
    msgs.append({"role": "system", "content": _phase_ctx(state)})

    # The interrupt_phrase is spoken by the frontend directly before the stream;
    # we pass it as extra_prefix so it's recorded in the AI's conversation turn.
    return Response(
        stream_gpt_sentences(msgs, state, sid, extra_prefix=int_phrase or None),
        content_type="text/event-stream",
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — START INTERVIEW
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/startInterview", methods=["POST"])
def api_start_interview():
    """
    POST body: { "resume_id": "<Name_UUID>" }

    Fetches resume + JD from Cosmos DB, creates a session, generates the
    greeting, initialises the transcription blob, and returns:
      { session_id, greeting, candidate_name, job_title, avatar }
    """
    data      = request.get_json(silent=True) or {}
    resume_id = (data.get("resume_id") or "").strip()
    if not resume_id:
        return jsonify({"error": "resume_id is required"}), 400

    try:
        cd = _get_candidate_data(resume_id)
        
        # Check if the candidate's interview is scheduled
        resume_state = cd.get("resume_data", {}).get("raw", {}).get("state", "").lower()
        if resume_state != "scheduled":
            return jsonify({
                "error": "This interview is not scheduled or has already been completed. Please contact HR for further information."
            }), 403
            
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except EnvironmentError as e:
        return jsonify({"error": str(e),
                        "hint": "Your COSMOS_ENDPOINT or COSMOS_KEY in .env is missing or wrong."}), 500
    except ConnectionError as e:
        return jsonify({
            "error": str(e),
            "hint": (
                "Cannot reach Cosmos DB. Fix COSMOS_ENDPOINT in .env:\n"
                "  1. portal.azure.com → your Cosmos account → Keys\n"
                "  2. Copy the URI field exactly\n"
                "  3. Paste as COSMOS_ENDPOINT=https://... in .env\n"
                "  4. Restart Flask"
            ),
        }), 503
    except PermissionError as e:
        return jsonify({"error": str(e),
                        "hint": "Fix COSMOS_KEY in .env — copy PRIMARY KEY from Azure Portal → Keys"}), 401
    except KeyError as e:
        return jsonify({
            "error":     str(e),
            "hint":      "Visit /api/debug/resumes to see real Resume IDs. "
                         "Make sure the resume document has a 'jd_id' field.",
            "resume_id": resume_id,
        }), 404
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500

    sid   = str(uuid.uuid4())
    # Build skills lists from Cosmos metadata — same pattern as recruiter_comment_id,
    # data is already fetched from Cosmos inside cd, no extra DB call needed.
    _jd_meta        = cd.get("jd_data", {})
    _jd_skills_meta = (
        _jd_meta.get("required_skills", []) +
        _jd_meta.get("nice_to_have_skills", [])
    )
    _resume_skills_meta = cd.get("resume_data", {}).get("skills", [])
    state = create_session(
        cd["resume"], cd["job_description"], cd["candidate_name"],
        job_title=cd.get("job_title", ""),
        recruiter_comment_id=cd.get("recruiter_comment_id"),
        jd_skills_meta=_jd_skills_meta,
        resume_skills_meta=_resume_skills_meta,
    )
    state["resume_id"] = resume_id
    state["jd_id"]     = cd.get("jd_id", cd["jd_data"]["id"])

    # Fixed blob name for background checkpoint saves
    _safe_rid = re.sub(r"[^A-Za-z0-9_\-.]", "_", resume_id)
    _tx_ts    = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    state["transcription_blob_name"] = f"{_safe_rid}/transcription_{_tx_ts}.json"

    with sessions_lock:
        interview_sessions[sid] = state

    greeting = get_ai_response(state)
    print(f"[Interview] Started sid={sid[:8]} candidate='{cd['candidate_name']}' "
          f"job='{cd['job_title']}' jd_id='{state['jd_id']}' "
          f"skills={len(state['resume_skills'])} projects={len(state['projects'])}")

    # Initialise the transcription blob asynchronously
    if blob_storage_configured():
        def _init_blob():
            try:
                # Never delete existing blobs — previous recordings and
                # transcriptions are preserved across sessions.
                auto_save_session(sid, state, save_reason="background")
                print(f"[Interview] Initial transcription created for sid={sid[:8]}")
            except Exception as e:
                print(f"[Interview] Initial blob setup failed: {e}")
        threading.Thread(target=_init_blob, daemon=True).start()

    avatar_data = cd.get("avatar", {})
    if avatar_data.get("avatar_character", "").lower() == "lisa":
        avatar_data["avatar_style"] = "casual-sitting"

    return jsonify({
        "session_id":     sid,
        "greeting":       greeting,
        "candidate_name": cd["candidate_name"],
        "job_title":      cd["job_title"],
        "avatar":         avatar_data,
    })


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — USER RESPONSE (blocking)
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/userResponse", methods=["POST"])
def api_user_response():
    data  = request.get_json(silent=True) or {}
    sid   = data.get("session_id", "")
    text  = (data.get("text") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    if not state["active"]:
        return jsonify({"ai_response": "", "interview_ended": True})

    state["transcription"].append({
        "speaker":   "Candidate",
        "text":      text,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        "phase":     PHASES[state["current_phase"]],
    })

    # # ── Identity check (GREETING phase) ──────────────────────────────────────
    # if PHASES[state["current_phase"]] == "GREETING":
    #     expected_name = state["candidate_name"].lower()
    #     if expected_name not in text.lower():
    #         if state.get("name_mismatch_confirmed", False):
    #             farewell = ("It seems there has been a misunderstanding. "
    #                         "Our HR will contact you soon. Thank you for your time")
    #             state["active"] = False
    #             return jsonify({"ai_response": farewell, "interview_ended": True})
    #         state["name_mismatch_confirmed"] = True
    #         confirm_q = (f"Could you please confirm if I am interviewing "
    #                      f"{state['candidate_name']} as listed in the resume?")
    #         return jsonify({"ai_response": confirm_q, "interview_ended": False})

    # ── Role confirmation (ROLE_CONFIRMATION phase) ───────────────────────────
    if PHASES[state["current_phase"]] == "ROLE_CONFIRMATION":
        expected_role = state.get("job_title", "")
        spoken_text   = text.strip()

        if state.get("awaiting_role_confirmation", False):
            if is_role_confirmation_no(spoken_text):
                farewell = "I understand. Our HR will contact you soon. Thank you for your time."
                state["active"] = False
                state["awaiting_role_confirmation"] = False
                return jsonify({"ai_response": farewell, "interview_ended": True})
            if is_role_confirmation_yes(spoken_text):
                state["awaiting_role_confirmation"] = False
                state["role_mismatch_confirmed"]    = False
                state["current_phase"] += 1
                state["qa_count"]      = 0
                state["phase_start"]   = datetime.datetime.now()
                ai_text = get_ai_response(state, text)
                return jsonify({"ai_response": ai_text, "interview_ended": not state["active"]})
            confirm_q = (f"This interview is for the role of {expected_role}. "
                         "Would you like to continue with this interview?")
            return jsonify({"ai_response": confirm_q, "interview_ended": False})

        if is_role_confirmation_no(spoken_text):
            state["active"] = False
            farewell = "I understand. Our HR will contact you soon. Thank you for your time."
            return jsonify({"ai_response": farewell, "interview_ended": True})

        if has_role_confusion(spoken_text, expected_role):
            state["awaiting_role_confirmation"] = True
            state["role_mismatch_confirmed"]    = True
            confirm_q = (f"I understand there might be some confusion. "
                         f"This interview is for the role of {expected_role}. "
                         "Would you like to proceed with the interview for this role?")
            return jsonify({"ai_response": confirm_q, "interview_ended": False})

        if is_role_confirmation_yes(spoken_text):
            state["awaiting_role_confirmation"] = False
            state["role_mismatch_confirmed"]    = False
            state["current_phase"] += 1
            state["qa_count"]      = 0
            state["phase_start"]   = datetime.datetime.now()
            ai_text = get_ai_response(state, text)
            return jsonify({"ai_response": ai_text, "interview_ended": not state["active"]})

        state["awaiting_role_confirmation"] = True
        confirm_q = (f"This interview is for the role of {expected_role}. "
                     "Would you like to continue with this interview?")
        return jsonify({"ai_response": confirm_q, "interview_ended": False})

    # ── Explicit end-interview command ────────────────────────────────────────
    if any(p in text.lower() for p in ["end interview", "stop interview", "finish interview"]):
        farewell = ("Thank you so much for your time today. It was a real pleasure. "
                    "We will be in touch about next steps. Have a wonderful day!")
        state["active"] = False
        _save_turn_async(sid, state)
        return jsonify({"ai_response": farewell, "interview_ended": True})

    # ── Normal interview turn ─────────────────────────────────────────────────
    ai_text = get_ai_response(state, text)
    save_turn_async(sid, state)
    return jsonify({"ai_response": ai_text, "interview_ended": not state["active"]})


# ── Alias used internally above (avoids naming conflict with imported fn) ─────
_save_turn_async = save_turn_async


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — USER RESPONSE (SSE streaming)
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/userResponseStream", methods=["POST"])
def api_user_response_stream():
    """Stream AI reply sentence-by-sentence as Server-Sent Events."""
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    text = (data.get("text") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    if not state["active"]:
        return Response(
            f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n",
            content_type="text/event-stream",
        )

    state["transcription"].append({
        "speaker":   "Candidate",
        "text":      text,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        "phase":     PHASES[state["current_phase"]],
    })

    # Explicit end-interview shortcut
    if any(p in text.lower() for p in ["end interview", "stop interview", "finish interview"]):
        farewell = ("Thank you so much for your time today. It was a real pleasure. "
                    "We will be in touch about next steps. Have a wonderful day!")
        state["active"] = False
        state["transcription"].append({
            "speaker":   "AI",
            "text":      farewell,
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
            "phase":     PHASES[state["current_phase"]],
        })
        save_turn_async(sid, state)

        def _farewell_stream():
            yield f"data: {json.dumps({'type': 'sentence', 'text': farewell})}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n"
        return Response(_farewell_stream(), content_type="text/event-stream")

    # Normal GPT turn
    state["conversation"].append({"role": "user", "content": text})
    _cover(state, text)
    if _should_advance(state) and state["current_phase"] < len(PHASES) - 1:
        state["current_phase"] += 1
        state["qa_count"]      = 0
        state["phase_start"]   = datetime.datetime.now()
    if PHASES[state["current_phase"]] == "CLOSING" and state["qa_count"] >= 1:
        state["active"] = False
        save_turn_async(sid, state)
        return Response(
            f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n",
            content_type="text/event-stream",
        )

    msgs = state["conversation"].copy()
    msgs.append({"role": "system", "content": _phase_ctx(state)})
    return Response(stream_gpt_sentences(msgs, state, sid), content_type="text/event-stream")


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — INTERRUPT HANDLING
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/notifyInterrupt", methods=["POST"])
def api_notify_interrupt():
    """Record the word index at which the candidate interrupted the avatar."""
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    wi   = int(data.get("word_index", -1))

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404

    state["interrupt_word_idx"] = wi
    rem = get_remaining_text(state["last_spoken_text"], wi)
    state["remaining_text"] = rem
    return jsonify({"ok": True, "remaining_preview": rem[:80]})


@interview_bp.route("/api/resumeAfterInterrupt", methods=["POST"])
def api_resume_after_interrupt():
    """Blocking: acknowledge the interrupt then generate the next question."""
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    text = (data.get("text") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    if not state["active"]:
        return jsonify({"ai_response": "", "interview_ended": True})

    rem = state.get("remaining_text", "").strip()
    state["transcription"].append({
        "speaker":   "Candidate",
        "text":      text,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        "phase":     PHASES[state["current_phase"]],
        "note":      "after_interrupt",
    })
    state["remaining_text"]     = ""
    state["interrupt_word_idx"] = -1

    ctx    = (f"[Context: you were saying '…{rem}' when interrupted. "
              f"Acknowledge briefly then ask your next question.]\nCandidate said: {text}"
              if rem else text)
    next_q = get_ai_response(state, ctx)
    full_r = (rem.rstrip(".?!") + ". " + next_q) if rem and next_q else (rem or next_q)
    save_turn_async(sid, state)
    return jsonify({
        "ai_response":    full_r,
        "remaining_text": rem,
        "next_question":  next_q,
        "interview_ended": not state["active"],
    })


@interview_bp.route("/api/resumeAfterInterruptStream", methods=["POST"])
def api_resume_after_interrupt_stream():
    """SSE stream: emit remaining text first, then GPT next question."""
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")
    text = (data.get("text") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    if not state["active"]:
        return Response(
            f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n",
            content_type="text/event-stream",
        )

    rem = state.get("remaining_text", "").strip()
    state["transcription"].append({
        "speaker":   "Candidate",
        "text":      text,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        "phase":     PHASES[state["current_phase"]],
        "note":      "after_interrupt",
    })
    state["remaining_text"]     = ""
    state["interrupt_word_idx"] = -1

    ctx = (f"[Context: you were saying '…{rem}' when interrupted. "
           f"Acknowledge briefly then ask your next question.]\nCandidate said: {text}"
           if rem else text)
    state["conversation"].append({"role": "user", "content": ctx})
    _cover(state, text)

    if _should_advance(state) and state["current_phase"] < len(PHASES) - 1:
        state["current_phase"] += 1
        state["qa_count"]      = 0
        state["phase_start"]   = datetime.datetime.now()
    if PHASES[state["current_phase"]] == "CLOSING" and state["qa_count"] >= 1:
        state["active"] = False
        save_turn_async(sid, state)
        return Response(
            f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n",
            content_type="text/event-stream",
        )

    msgs = state["conversation"].copy()
    msgs.append({"role": "system", "content": _phase_ctx(state)})
    return Response(
        stream_gpt_sentences(msgs, state, sid, extra_prefix=rem or None),
        content_type="text/event-stream",
    )


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — SESSION STATUS
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/sessionStatus")
def api_session_status():
    sid = request.args.get("session_id", "")
    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404
    elapsed = (datetime.datetime.now() - state["interview_start"]).seconds
    return jsonify({
        "active":           state["active"],
        "phase":            PHASES[state["current_phase"]],
        "phase_index":      state["current_phase"],
        "elapsed_seconds":  elapsed,
        "skills_covered":   len(state["covered_skills"]),
        "total_skills":     len(state["resume_skills"]),
        "jd_covered":       len(state["covered_jd"]),
        "total_jd":         len(state["jd_skills"]),
        "projects_covered": len(state["covered_projects"]),
        "total_projects":   len(state["projects"]),
    })


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — SAVE TRANSCRIPTION
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/saveTranscription", methods=["POST"])
def api_save_transcription():
    """
    Called by the frontend:
      • Every 30 s  → save_reason = "background"
      • On finish   → save_reason = "final"
      • On tab close (sendBeacon) → save_reason = "emergency"
    """
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id", "")

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404

    save_reason = data.get("save_reason", "background")
    resume_id   = state.get("resume_id", "unknown")

    if not blob_storage_configured():
        return jsonify({
            "error": "Blob Storage not configured",
            "hint":  "Add AZURE_BLOB_CONNECTION_STRING to .env",
        }), 503

    ok = auto_save_session(sid, state, save_reason=save_reason)
    if not ok:
        return jsonify({"error": "Upload failed — check server logs"}), 500

    safe_rid = re.sub(r"[^A-Za-z0-9_\-.]", "_", resume_id)
    resp = {
        "ok":          True,
        "resume_id":   resume_id,
        "turns_saved": len(state["transcription"]),
        "save_reason": save_reason,
        "folder":      f"{safe_rid}/",
    }
    if save_reason == "final":
        resp["note"] = ("Previous interview files deleted. "
                        "Folder now contains only the current session's final files.")
    return jsonify(resp)


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — UPLOAD RECORDING
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/uploadRecording", methods=["POST"])
def api_upload_recording():
    """
    Receives a WebM recording blob from the browser MediaRecorder.
    Blob path: {resume_id}/recording_{timestamp}.webm
    The timestamp is shared with the transcription file (set by auto_save_session
    on final save) so both files are co-located with the same timestamp.
    """
    sid = (request.form.get("session_id") or "").strip()

    with sessions_lock:
        state = interview_sessions.get(sid)
    if not state:
        return jsonify({"error": "Session not found"}), 404

    recording_file = request.files.get("recording")
    if not recording_file:
        return jsonify({"error": "No 'recording' file in request"}), 400

    if not blob_storage_configured():
        return jsonify({
            "error": "Azure Blob Storage is not configured",
            "hint":  "Add AZURE_BLOB_CONNECTION_STRING to .env",
        }), 503

    resume_id    = state.get("resume_id", "unknown")
    content_type = recording_file.content_type or "video/webm"
    timestamp    = (state.get("final_folder_ts") or
                    datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))

    try:
        recording_bytes = recording_file.read()
        blob_info = upload_recording(resume_id, recording_bytes, content_type, timestamp)
        print(f"[Interview] Recording saved -> {blob_info['blob_url']}")
        return jsonify({"ok": True, "resume_id": resume_id, "blob": blob_info})
    except Exception as e:
        _tb.print_exc()
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE — FINALIZE INTERVIEW (trigger AI evaluation)
# ══════════════════════════════════════════════════════════════════════════════

@interview_bp.route("/api/finalizeInterview", methods=["POST"])
def api_finalize_interview():
    """
    Called by the frontend when the interview ends.
    Launches the evaluation in a background thread so the UI is never blocked.
    """
    data = request.get_json(silent=True) or {}
    sid  = data.get("session_id")

    with sessions_lock:
        state = interview_sessions.get(sid)
        if not state:
            return jsonify({"error": "Session not found"}), 404
        resume_id = state.get("resume_id")

    threading.Thread(
        target=_process_background_evaluation, args=(resume_id,), daemon=True
    ).start()
    return jsonify({"ok": True, "message": "Evaluation started in background"})