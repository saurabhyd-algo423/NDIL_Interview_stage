"""
interview_session.py
====================
All interview business logic:
  - Phase definitions, timing constants, skill keywords
  - Role-confirmation / identity-check helpers
  - Session creation (create_session)
  - Phase context builder (_phase_ctx) and phase-advance logic
  - AI response (blocking: get_ai_response)
  - Streaming AI response generator (_stream_gpt_sentences)
  - Semantic VAD (semantic_vad_check)
  - Transcription payload building + blob auto-save
  - Background cleanup daemon

None of this module knows anything about Flask routes or HTTP.
It is imported by routes_interview.py and routes_avatar.py.
"""

import re
import json
import datetime
import threading
import time
import traceback as _tb

from config import (
    openai_client, AZURE_OAI_DEPLOYMENT,
    SEMANTIC_VAD_MIN_WORDS,
    interview_sessions, sessions_lock,
)
from blob_storage import (
    blob_storage_configured,
    upload_transcription,
    check_candidate_folder_exists,
    delete_candidate_folder,
)

# ══════════════════════════════════════════════════════════════════════════════
# INTERVIEW PHASES & TIMING
# ══════════════════════════════════════════════════════════════════════════════

PHASES = [
    "GREETING",
    "ROLE_CONFIRMATION",
    "EXPERIENCE_DEEP_DIVE",
    "PROJECTS_DISCUSSION",
    "SKILLS_COVERAGE",
    "JD_ALIGNMENT",
    "BEHAVIORAL",
    "CANDIDATE_QUESTIONS",
    "CLOSING",
]

PHASE_MINS = {
    "GREETING":             1,
    "ROLE_CONFIRMATION":    1,
    "EXPERIENCE_DEEP_DIVE": 5,
    "PROJECTS_DISCUSSION":  8,
    "SKILLS_COVERAGE":      5,
    "JD_ALIGNMENT":         4,
    "BEHAVIORAL":           3,
    "CANDIDATE_QUESTIONS":  2,
    "CLOSING":              1,
}
TOTAL_MINS = 30

# ══════════════════════════════════════════════════════════════════════════════
# ROLE CONFIRMATION KEYWORD LISTS
# ══════════════════════════════════════════════════════════════════════════════

ROLE_CONFIRM_YES = [
    "yes", "yeah", "yep", "sure", "okay", "ok", "correct", "right",
    "i am ready", "i'm ready", "ready to proceed", "yes i want to continue",
    "i want to continue", "continue", "proceed", "let's continue",
    "lets continue", "go ahead",
]

ROLE_CONFIRM_NO = [
    "no", "nope", "not ready", "do not proceed", "don't proceed",
    "dont proceed", "do not want to proceed", "don't want to proceed",
    "dont want to proceed", "i do not want to proceed", "i don't want to proceed",
    "i dont want to proceed", "not interested", "not this role", "wrong role",
    "wrong interview", "i decline", "i refuse", "stop", "end interview",
]

ROLE_CONFUSION_HINTS = [
    "different role", "another role", "other role", "wrong role",
    "wrong interview", "thought this was", "i thought this was",
    "i was expecting", "i applied for", "for a different role",
    "for another role", "for some other role",
]

# ══════════════════════════════════════════════════════════════════════════════
# SYSTEM PROMPT TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

SYS_PROMPT = """You are an expert AI interviewer conducting a professional job interview via video call.
Your words are spoken aloud by a photorealistic avatar — keep responses short and conversational.

You are an AI avatar interviewer. At the beginning of the conversation, introduce yourself as an AI avatar and inform the candidate that you may take a few seconds to process or think before responding.
Always give this Guidelines to candidate at the beginning of interview:
1. Keep response short and conversational.
2. You may face some lag in audio/video, so please be patient.
3. Do not close the interview window or tab.
4. Do not turn off the camera.

CANDIDATE: {candidate_name}
JOB TITLE: {job_title}

RESUME:
{resume}

JOB DESCRIPTION:
{job_description}

SKILLS FROM RESUME: {resume_skills}
JD REQUIREMENTS: {jd_skills}
TOTAL PROJECTS: {total_projects} | Projects to discuss: {projects_to_discuss}
{recruiter_comment_block}
INTERVIEW STRUCTURE (30 minutes):
Phase 1 GREETING (1 min): Warm welcome,
Introduce yourself as an AI avatar.
Inform the candidate that you may take a few seconds to process or think before responding.
Ask for brief introduction.
Phase 2 ROLE CONFIRMATION (1 min): Remind the candidate that the interview being conducted is for the role of {job_title} and ask them to confirm if they are ready to give the interview.
- If the candidate says that they got confused and thought this interview was for a role other than {job_title}:
    1. Politely respond: "I understand there might be some confusion. This interview is for the role of {job_title}. Would you like to proceed with the interview for this role?"
    2. If the candidate agrees to proceed with the interview for the role of {job_title}:
        - Proceed to Phase 3
    3. If the candidate refuses to proceed with the interview for the role of {job_title}:
        - Respond: "I understand. HR will contact you soon. Thank you for your time."
        - End the interview immediately.
Phase 3 EXPERIENCE_DEEP_DIVE (5 min): Career journey, areas of expertise. Pace your questions so the candidate can cover their background fully within the 5-minute window — ask follow-ups only if time permits.
Phase 4 PROJECTS_DISCUSSION (8 min): Deep-dive on {projects_to_discuss} resume projects. Spread the 8 minutes across all projects — move to the next project as soon as the current one is covered adequately. If time is short, reduce follow-up depth rather than skipping projects.
Phase 5 SKILLS_COVERAGE (5 min): Each resume skill in depth. Distribute the 5 minutes across all uncovered skills. If many skills remain and time is short, ask one concise question per skill and move on quickly.
Phase 6 JD_ALIGNMENT (4 min): Map candidate experience to job requirements. Cover as many JD requirements as possible within 4 minutes — prioritise the most critical gaps.
Phase 7 BEHAVIORAL (3 min): STAR method questions. Keep answers focused; one well-answered behavioral question is better than two rushed ones.
Phase 8 CANDIDATE_QUESTIONS (2 min): Let candidate ask questions
Phase 9 CLOSING (1 min): Thank and explain next steps

STRICT RULES:
1. EXACTLY ONE question per response.
2. Under 60 words — spoken aloud by avatar.
3. Plain conversational English — no markdown, no bullet points.
4. Never start with "Certainly", "Of course", "Absolutely", or "Great".
5. Natural and warm — like a real human interviewer on a video call.
6. Reference specific resume details when asking questions.
7. STAY IN EACH PHASE FOR ITS FULL ALLOCATED TIME AS MENTIONED ["GREETING": 1,
    "ROLE_CONFIRMATION":    1,
    "EXPERIENCE_DEEP_DIVE": 5,
    "PROJECTS_DISCUSSION":  8,
    "SKILLS_COVERAGE":      5,
    "JD_ALIGNMENT":         4,
    "BEHAVIORAL":           3,
    "CANDIDATE_QUESTIONS":  2,
    "CLOSING":              1]. Do not move to the next phase until the system tells you the phase time has run out. If a candidate answers a question quickly, ask a follow-up or a related question on the same topic — do not jump to the next phase early.
8. AI or Avatar should be able to interrupt whenever it feels candidate is talking too much on the question asked and ask the next question. It should keep the answer brief and fully.
   - For GREETING and ROLE_CONFIRMATION phases: these are simple confirmation exchanges — interrupt and move on as soon as the candidate has given a clear answer (name, confirmation, yes/no).
   - For EXPERIENCE_DEEP_DIVE, PROJECTS_DISCUSSION, SKILLS_COVERAGE, JD_ALIGNMENT, and BEHAVIORAL phases: internally evaluate whether the candidate's answer has covered the expected key points for the question asked. If the candidate has provided a complete and satisfactory answer and continues speaking beyond that, interrupt politely and ask the next question within the SAME phase. If the candidate gives a brief answer, ask a follow-up question to go deeper — do NOT move to the next phase just because one answer was short. Only advance to the next phase when the system instructs you that the phase time is up.
"""

# ══════════════════════════════════════════════════════════════════════════════
# SEMANTIC VAD
# ══════════════════════════════════════════════════════════════════════════════

_VAD_SYS = (
    "You are a voice activity detector for a live job interview.\n"
    "Classify the candidate's transcript with EXACTLY two words:\n"
    "Word 1: SPEECH or NOISE\n"
    "  SPEECH = genuine human speech directed at the interviewer\n"
    "  NOISE = background sounds, coughs, filler only, off-topic fragments\n"
    "Word 2: COMPLETE or INCOMPLETE\n"
    "  COMPLETE = at least one coherent thought expressed, enough to respond to\n"
    "  INCOMPLETE = filler or too short to be a real answer\n"
    "Reply with EXACTLY two words. Examples: SPEECH COMPLETE, NOISE INCOMPLETE"
)


# ══════════════════════════════════════════════════════════════════════════════
# AI INTERRUPT — CANDIDATE ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

# Phases where we SKIP AI interrupt analysis (short exchanges, confirmations)
_NO_INTERRUPT_PHASES = {"GREETING", "ROLE_CONFIRMATION", "CANDIDATE_QUESTIONS", "CLOSING"}

_CANDIDATE_ANALYSIS_SYS = (
    "You are monitoring a live job interview. The AI interviewer asked a question "
    "and the candidate is responding. Decide if the AI should politely interrupt now.\n\n"
    "INTERRUPT if ANY of these are clearly true:\n"
    "  1. The candidate has fully answered the question and is now repeating the same points.\n"
    "  2. The candidate has answered the question and is rambling without adding new relevant information.\n"
    "  3. The candidate is going significantly off-topic from the question asked.\n\n"
    "DO NOT interrupt if:\n"
    "  - The candidate is still mid-explanation and adding new, relevant information.\n"
    "  - The candidate has given a thorough, on-topic answer but it is long (depth is fine).\n"
    "  - The candidate has only spoken briefly and is still developing their answer.\n\n"
    "Respond ONLY with a valid JSON object on one line. No markdown, no explanation.\n"
    "Format: {\"decision\": \"INTERRUPT\" or \"CONTINUE\", \"reason\": \"one short phrase\", "
    "\"interrupt_phrase\": \"under 12 words — a polite redirection, only include this key if decision is INTERRUPT\"}"
)


def analyze_candidate_speech(state: dict, partial_text: str) -> dict:
    """
    Content-quality based analysis to determine if the AI should interrupt
    the candidate. NOT time-based — purely evaluates what the candidate said.

    Returns: {should_interrupt: bool, reason: str, interrupt_phrase: str}
    """
    current_phase = PHASES[state.get("current_phase", 0)]
    if current_phase in _NO_INTERRUPT_PHASES:
        return {"should_interrupt": False, "reason": "phase_excluded", "interrupt_phrase": ""}

    # Find the last question the AI asked
    last_question = ""
    for msg in reversed(state.get("conversation", [])):
        if msg["role"] == "assistant":
            last_question = msg["content"]
            break

    if not last_question:
        return {"should_interrupt": False, "reason": "no_question_context", "interrupt_phrase": ""}

    word_count = len(partial_text.strip().split())
    analysis_prompt = (
        f"Phase: {current_phase}\n"
        f"AI question: \"{last_question}\"\n"
        f"Candidate response so far ({word_count} words):\n\"{partial_text}\""
    )

    try:
        r = openai_client.chat.completions.create(
            model=AZURE_OAI_DEPLOYMENT, max_tokens=80, temperature=0,
            messages=[
                {"role": "system", "content": _CANDIDATE_ANALYSIS_SYS},
                {"role": "user",   "content": analysis_prompt},
            ],
        )
        raw = r.choices[0].message.content.strip()
        # Strip markdown fences if model includes them despite instructions
        raw = re.sub(r"```(?:json)?|```", "", raw).strip()
        result = json.loads(raw)
        should_interrupt = result.get("decision") == "INTERRUPT"
        return {
            "should_interrupt": should_interrupt,
            "reason":           result.get("reason", ""),
            "interrupt_phrase": result.get("interrupt_phrase", "Thank you — let me ask you something else.") if should_interrupt else "",
        }
    except Exception as e:
        print(f"[AnalyzeCandidate] Error: {e}")
        return {"should_interrupt": False, "reason": "error", "interrupt_phrase": ""}


# ══════════════════════════════════════════════════════════════════════════════
# NUDGE GENERATION — silence / stuck candidate handler
# ══════════════════════════════════════════════════════════════════════════════

_NUDGE_PROMPTS = {
    "rephrase": (
        "The candidate has been silent for several seconds after you asked a question. "
        "They may be confused, nervous, or uncertain how to begin. "
        "Your job: in ONE short sentence (under 15 words), gently acknowledge the pause "
        "and either rephrase the question more simply, offer a hint, or invite them to "
        "start wherever they feel comfortable. Be warm, not robotic. "
        "Do NOT ask a brand new question — stay on the same topic. "
        "Do NOT say 'Take your time' as the only response."
    ),
    "move_on": (
        "The candidate has been silent for an extended period even after you gave them "
        "a prompt. Gracefully move on. In ONE sentence (under 20 words), acknowledge "
        "that it is completely fine, and pivot naturally to the next question or topic. "
        "Be encouraging, not apologetic. Do not dwell on the silence."
    ),
    "post_silence": (
        "The candidate started speaking but then trailed off. You have their partial "
        "response (provided below). In ONE short sentence (under 15 words), acknowledge "
        "what they said warmly and gently invite them to continue or confirm if they "
        "have finished. Keep the same topic."
    ),
}


def generate_nudge(state: dict, nudge_type: str, candidate_text: str = "") -> str:
    """
    Generate a single contextual nudge sentence for a silent or stuck candidate.
    Returns plain text (no markdown, no question mark enforced).

    nudge_type: 'rephrase' | 'move_on' | 'post_silence'
    candidate_text: optional partial speech the candidate produced this turn.
    """
    phase       = PHASES[state.get("current_phase", 0)]
    instruction = _NUDGE_PROMPTS.get(nudge_type, _NUDGE_PROMPTS["rephrase"])

    # Find the last AI question
    last_q = ""
    for msg in reversed(state.get("conversation", [])):
        if msg["role"] == "assistant":
            last_q = msg["content"]
            break

    user_content = f"Phase: {phase}\nYour last question: \"{last_q}\""
    if candidate_text:
        user_content += f"\nCandidate's partial response: \"{candidate_text[:300]}\""

    try:
        r = openai_client.chat.completions.create(
            model=AZURE_OAI_DEPLOYMENT, max_tokens=60, temperature=0.7,
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user",   "content": user_content},
            ],
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        print(f"[Nudge] Error: {e}")
        # Safe fallbacks
        fallbacks = {
            "rephrase":    "No problem — feel free to start wherever you are most comfortable.",
            "move_on":     "That's completely fine — let's move on to the next topic.",
            "post_silence": "Great, thank you — feel free to continue or we can move on.",
        }
        return fallbacks.get(nudge_type, "Let's continue.")


def stream_nudge(state: dict, sid: str, nudge_type: str, candidate_text: str = ""):
    """
    Generator that streams a nudge as SSE, then (for move_on / post_silence)
    follows up with the next GPT question.

    Yields same SSE format as stream_gpt_sentences:
      data: {"type":"sentence","text":"..."}
      data: {"type":"done","interview_ended":bool}
    """
    # 1. Generate and emit the nudge phrase immediately
    nudge_phrase = generate_nudge(state, nudge_type, candidate_text)
    if nudge_phrase:
        yield f"data: {json.dumps({'type': 'sentence', 'text': nudge_phrase})}\n\n"

    # 2. For move_on and post_silence, also stream the next AI question
    if nudge_type in ("move_on", "post_silence"):
        # Record partial candidate text if we have it
        if candidate_text:
            state["transcription"].append({
                "speaker":   "Candidate",
                "text":      candidate_text,
                "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
                "phase":     PHASES[state["current_phase"]],
                "note":      f"nudge_{nudge_type}_partial",
            })
            state["conversation"].append({"role": "user", "content": candidate_text})
            _cover_text(state, candidate_text)

        ctx = (
            f"[Nudge: candidate was silent / trailing off. "
            f"You just said: '{nudge_phrase}'. Now ask your next question naturally.]"
        )
        state["conversation"].append({"role": "user", "content": ctx})

        if _should_advance(state) and state["current_phase"] < len(PHASES) - 1:
            state["current_phase"] += 1
            state["qa_count"]      = 0
            state["phase_start"]   = datetime.datetime.now()

        if PHASES[state["current_phase"]] == "CLOSING" and state["qa_count"] >= 1:
            state["active"] = False
            save_turn_async(sid, state)
            yield f"data: {json.dumps({'type': 'done', 'interview_ended': True})}\n\n"
            return

        msgs = state["conversation"].copy()
        msgs.append({"role": "system", "content": _phase_ctx(state)})

        # Stream GPT next question (re-using core stream logic inline)
        collected     = []
        question_sent = False
        try:
            stream = openai_client.chat.completions.create(
                model=AZURE_OAI_DEPLOYMENT, max_tokens=150,
                temperature=0.7, messages=msgs, stream=True,
            )
            for chunk in stream:
                if not chunk.choices: continue
                delta = chunk.choices[0].delta
                if not delta or not delta.content: continue
                token = delta.content
                collected.append(token)
                if question_sent: continue
                ch = token.rstrip()
                if ch and ch[-1] in _SENTENCE_ENDS:
                    sentence = "".join(collected).strip()
                    if sentence:
                        yield f"data: {json.dumps({'type': 'sentence', 'text': sentence})}\n\n"
                        if "?" in sentence:
                            question_sent = True
                        collected = []
            # flush
            if not question_sent:
                remainder = "".join(collected).strip()
                if remainder:
                    yield f"data: {json.dumps({'type': 'sentence', 'text': remainder})}\n\n"
        except Exception as e:
            print(f"[Nudge stream] GPT error: {e}")

        full_ai = nudge_phrase + " " + "".join(collected)
        state["conversation"].append({"role": "assistant", "content": full_ai.strip()})
        state["qa_count"] += 1
        state["last_spoken_text"]   = full_ai.strip()
        state["interrupt_word_idx"] = -1
        state["remaining_text"]     = ""
        _cover_text(state, full_ai)
        state["transcription"].append({
            "speaker":   "AI",
            "text":      full_ai.strip(),
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
            "phase":     PHASES[state["current_phase"]],
            "note":      f"nudge_{nudge_type}",
        })
        save_turn_async(sid, state)

    elif nudge_type == "rephrase":
        # Just record the nudge phrase itself in conversation/transcription
        state["conversation"].append({"role": "assistant", "content": nudge_phrase})
        state["transcription"].append({
            "speaker":   "AI",
            "text":      nudge_phrase,
            "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
            "phase":     PHASES[state["current_phase"]],
            "note":      "nudge_rephrase",
        })
        save_turn_async(sid, state)

    yield f"data: {json.dumps({'type': 'done', 'interview_ended': not state['active']})}\n\n"


def _cover_text(state: dict, text: str) -> None:
    """Alias used internally in stream_nudge (avoids circular ref with _cover)."""
    _cover(state, text)


# ══════════════════════════════════════════════════════════════════════════════
# SEMANTIC VAD
# ══════════════════════════════════════════════════════════════════════════════

def semantic_vad_check(text: str, is_final: bool = False) -> dict:
    """
    Single GPT call that combines speech-detection + completeness check.
    Returns dict: {is_speech, is_complete, reason}
    """
    text  = text.strip()
    words = len(text.split())
    if words < SEMANTIC_VAD_MIN_WORDS:
        return {"is_speech": False, "is_complete": False, "reason": "too_short"}
    try:
        r = openai_client.chat.completions.create(
            model=AZURE_OAI_DEPLOYMENT, max_tokens=5, temperature=0,
            messages=[
                {"role": "system", "content": _VAD_SYS},
                {"role": "user",   "content": f'Transcript: "{text}"'},
            ],
        )
        label       = r.choices[0].message.content.strip().upper()
        parts       = label.split()
        is_speech   = parts[0].startswith("SPEECH") if parts else True
        is_complete = (
            parts[1].startswith("COMPLETE") if len(parts) > 1 else False
        ) if is_final else False
    except Exception as e:
        print(f"[VAD] error: {e}")
        is_speech, is_complete = True, is_final
        label = "ERROR"
    if not is_speech:
        return {"is_speech": False, "is_complete": False, "reason": label}
    return {"is_speech": True, "is_complete": is_complete, "reason": label}


# ══════════════════════════════════════════════════════════════════════════════
# TEXT / ROLE-CONFIRMATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalize_text(text: str) -> str:
    """Collapse whitespace, strip, lowercase."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _contains_any(text: str, patterns: list) -> bool:
    return any(p in text for p in patterns)


def is_role_confirmation_yes(text: str) -> bool:
    return _contains_any(_normalize_text(text), ROLE_CONFIRM_YES)


def is_role_confirmation_no(text: str) -> bool:
    return _contains_any(_normalize_text(text), ROLE_CONFIRM_NO)


def has_role_confusion(text: str, expected_role: str) -> bool:
    t        = _normalize_text(text)
    expected = _normalize_text(expected_role)
    if _contains_any(t, ROLE_CONFUSION_HINTS):
        return True
    if (any(token in t for token in ["role", "position", "interview", "job"])
            and expected and expected not in t):
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
# SKILL / PROJECT EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def _extract(resume: str, jd: str,
             jd_skills_meta: list = None,
             resume_skills_meta: list = None):
    """
    Extract matching skills from resume and JD, and detect project bullets
    from the resume.  Returns (resume_skills, jd_skills, projects).

    jd_skills_meta     — list from Cosmos JD metadata (required_skills +
                         nice_to_have_skills).  Used as-is for jd_skills.
    resume_skills_meta — list from Cosmos resume metadata (extracted_data.skills).
                         Used as-is for resume_skills.

    When both are provided, Cosmos metadata is the authoritative source —
    no substring scanning
    """
    rs, js, projs = [], [], []

    if jd_skills_meta is not None and resume_skills_meta is not None:
        # Use Cosmos metadata directly — no text scanning needed
        js = [s.strip().lower() for s in jd_skills_meta    if s and s.strip()]
        rs = [s.strip().lower() for s in resume_skills_meta if s and s.strip()]

    in_p, cur = False, []
    for line in resume.split("\n"):
        ll = line.lower()
        if any(x in ll for x in ["projects:", "project experience:", "key projects"]):
            in_p = True
            continue
        if in_p and line.strip():
            if line.strip()[0] in "-•*" or re.match(r"^\d+\.", line.strip()):
                if cur: projs.append(" ".join(cur))
                cur = [line.strip()]
            elif cur:
                cur.append(line.strip())
        elif in_p and not line.strip():
            in_p = False
            if cur: projs.append(" ".join(cur)); cur = []
    if cur: projs.append(" ".join(cur))

    # Fallback: grab any bullet lines from the whole resume
    if not projs:
        for line in resume.split("\n"):
            s = line.strip()
            if s and (s[0] in "-•*" or re.match(r"^\d+\.", s)):
                projs.append(s)
    return rs, js, projs


# ══════════════════════════════════════════════════════════════════════════════
# SESSION CREATION
# ══════════════════════════════════════════════════════════════════════════════

def create_session(resume: str, jd: str, candidate_name: str = "",
                   job_title: str = "", recruiter_comment_id=None,
                   jd_skills_meta: list = None,
                   resume_skills_meta: list = None) -> dict:
    """
    Build a fresh interview session state dict.
    The dict is stored in config.interview_sessions[sid].

    jd_skills_meta     — skills from Cosmos JD metadata (required_skills +
                         nice_to_have_skills). Used directly, no GPT call.
    resume_skills_meta — skills from Cosmos resume metadata (extracted_data.skills).
                         Used directly, no text scanning.
    Both follow the same pattern as recruiter_comment_id — sourced from
    the already-fetched Cosmos documents, no extra DB call needed.
    """
    rs, js, projs = _extract(resume, jd,
                             jd_skills_meta=jd_skills_meta,
                             resume_skills_meta=resume_skills_meta)
    ptd = min(3, max(1, len(projs))) if projs else 0

    recruiter_comment_block = (
        "RECRUITER'S NOTES FOR THIS INTERVIEW:\n"
        f"{recruiter_comment_id}\n"
        "Use these notes to guide your questions — make sure to probe the candidate\n"
        "on the areas or concerns the recruiter has highlighted above.\n\n"
    ) if recruiter_comment_id else ""

    sp = SYS_PROMPT.format(
        candidate_name=candidate_name or "the candidate",
        job_title=job_title or "the applied role",
        resume=resume, job_description=jd,
        resume_skills=", ".join(rs) or "not specified",
        jd_skills=", ".join(js) or "not specified",
        total_projects=len(projs), projects_to_discuss=ptd,
        recruiter_comment_block=recruiter_comment_block,
    )
    now = datetime.datetime.now()
    return {
        "candidate_name": candidate_name, "job_title": job_title,
        "resume": resume, "job_description": jd,
        "resume_skills": rs, "jd_skills": js, "projects": projs,
        "projects_to_discuss": ptd, "current_phase": 0, "qa_count": 0,
        "covered_skills": set(), "covered_jd": set(), "covered_projects": set(),
        "interview_start": now, "phase_start": now, "active": True,
        "conversation": [{"role": "system", "content": sp}],
        "transcription": [],
        "recruiter_comment_id": recruiter_comment_id,
        "transcription_blob_name": None,
        "final_folder_ts": None,
        "awaiting_role_confirmation": False,
        "last_spoken_text": "", "interrupt_word_idx": -1, "remaining_text": "",
    }


# ══════════════════════════════════════════════════════════════════════════════
# PHASE CONTEXT & ADVANCE LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def _phase_ctx(state: dict) -> str:
    """
    Build the per-turn system message injected at the end of each GPT call.
    Tells the model which phase it's in, per-phase time remaining, total time
    remaining, and what's uncovered — so the AI paces questions within each phase.
    """
    ph            = PHASES[state["current_phase"]]
    ph_idx        = state["current_phase"]
    now           = datetime.datetime.now()

    # Per-phase elapsed and remaining
    phase_elapsed = (now - state["phase_start"]).seconds / 60
    phase_limit   = list(PHASE_MINS.values())[ph_idx]
    phase_rem     = max(0, phase_limit - phase_elapsed)

    # Total interview remaining
    total_elapsed = (now - state["interview_start"]).seconds / 60
    total_rem     = max(0, TOTAL_MINS - total_elapsed)

    usk = [s for s in state["resume_skills"] if s not in state["covered_skills"]]
    ujd = [s for s in state["jd_skills"]     if s not in state["covered_jd"]]
    upr = [p for p in state["projects"]       if p not in state["covered_projects"]]

    ctx  = f"\n\nCURRENT PHASE: {ph}."
    ctx += f" Phase time remaining: {phase_rem:.1f} min (allocated: {phase_limit} min, used: {phase_elapsed:.1f} min)."
    ctx += f" Total interview time remaining: {total_rem:.1f} min."
    ctx += f"\nQuestions asked this phase: {state['qa_count']}."
    ctx += f"\nSkills uncovered: {', '.join(usk[:6]) or 'all done'}."
    ctx += f"\nJD uncovered: {', '.join(ujd[:4]) or 'all done'}."
    ctx += f"\nProjects covered: {len(state['covered_projects'])}/{len(state['projects'])}."
    if upr:
        ctx += f" Next project: {upr[0][:80]}..."

    # Pacing instructions based on how much phase time remains
    if phase_rem <= 0:
        ctx += (
            "\nPHASE TIME EXPIRED: Finish your current thought in one sentence, "
            "then transition immediately to the next phase. Do NOT ask another question in this phase."
        )
    elif phase_rem <= 0.75:
        ctx += (
            f"\nURGENT: Only {phase_rem:.1f} min left in this phase. "
            "Ask ONE final question for this phase, then move on."
        )
    elif phase_rem <= 1.5:
        ctx += (
            f"\nNOTE: {phase_rem:.1f} min remaining in this phase. "
            "Wrap up with one or two more targeted questions — do not start a new sub-topic."
        )
    else:
        ctx += (
            f"\nPACING: You have {phase_rem:.1f} min left in this phase. "
            "Use the full time — keep asking questions on this phase's topic. "
            "Do NOT move to the next phase early."
        )

    if total_rem < 4:
        ctx += "\nURGENT: Under 4 minutes total remaining — move to CLOSING now."

    rc = state.get("recruiter_comment_id")
    if rc and ph == "JD_ALIGNMENT":
        ctx += (
            f"\nRECRUITER FOCUS: The recruiter noted — \"{rc}\"."
            "\nAsk at least one question directly related to this comment."
            " Connect it to the candidate's experience or the JD requirements."
        )
    ctx += "\nRemember: ONE question only, under 60 words, no markdown."
    return ctx


def _should_advance(state: dict) -> bool:
    """
    Return True if the interview should move to the next phase.

    Rules (in priority order):
      1. Hard cap  — total interview time >= TOTAL_MINS.
      2. Per-phase time limit reached AND at least 1 question was asked.
         (The minimum-1-question floor prevents advancing on the very first
         turn of a phase before the AI has even spoken.)
      3. Content fully covered for content-driven phases — but ONLY after
         the phase has run for at least half its allocated time, so a fast
         candidate doesn't skip through phases in seconds.
    """
    ph, name      = state["current_phase"], PHASES[state["current_phase"]]
    now           = datetime.datetime.now()

    # Per-phase elapsed time (resets each time a phase starts via phase_start)
    phase_elapsed = (now - state["phase_start"]).seconds / 60
    phase_limit   = list(PHASE_MINS.values())[ph]
    half_limit    = phase_limit / 2.0

    # Total interview elapsed (hard overall cap)
    total_elapsed = (now - state["interview_start"]).seconds / 60

    # 1. Hard cap: total interview time exceeded
    if total_elapsed >= TOTAL_MINS and ph < len(PHASES) - 1:
        return True

    # 2. Per-phase time limit reached — require at least 1 AI question asked
    #    so we never advance on the very first turn of a new phase.
    if phase_elapsed >= phase_limit and state["qa_count"] >= 1:
        return True

    # 3. Content-completion — only after at least half the phase time has run,
    #    so a brief answer doesn't skip the phase prematurely.
    if phase_elapsed >= half_limit:
        if (name == "PROJECTS_DISCUSSION"
                and state["projects_to_discuss"] > 0
                and len(state["covered_projects"]) >= state["projects_to_discuss"]):
            return True
        if (name == "SKILLS_COVERAGE"
                and state["resume_skills"]
                and len(state["covered_skills"]) >= len(state["resume_skills"])):
            return True
        if (name == "JD_ALIGNMENT"
                and state["jd_skills"]
                and len(state["covered_jd"]) >= len(state["jd_skills"])):
            return True

    return False


def _cover(state: dict, text: str) -> None:
    """Mark which skills and projects were mentioned in *text*."""
    tl = text.lower()
    for s in state["resume_skills"]:
        if s in tl: state["covered_skills"].add(s)
    for s in state["jd_skills"]:
        if s in tl: state["covered_jd"].add(s)
    for p in state["projects"]:
        kw = [w for w in p.lower().split() if len(w) > 3][:3]
        if any(k in tl for k in kw): state["covered_projects"].add(p)


# ══════════════════════════════════════════════════════════════════════════════
# AI RESPONSE — BLOCKING
# ══════════════════════════════════════════════════════════════════════════════

def get_ai_response(state: dict, user_input: str = None) -> str:
    """
    Append user_input to the conversation, advance phase if needed,
    call GPT, record the reply in transcription, return the text.
    """
    if user_input:
        state["conversation"].append({"role": "user", "content": user_input})
        _cover(state, user_input)

    if _should_advance(state) and state["current_phase"] < len(PHASES) - 1:
        state["current_phase"] += 1
        state["qa_count"]       = 0
        state["phase_start"]    = datetime.datetime.now()

    if PHASES[state["current_phase"]] == "CLOSING" and state["qa_count"] >= 1:
        state["active"] = False
        return ""

    msgs = state["conversation"].copy()
    msgs.append({"role": "system", "content": _phase_ctx(state)})

    r       = openai_client.chat.completions.create(
        model=AZURE_OAI_DEPLOYMENT, max_tokens=150, temperature=0.7, messages=msgs)
    ai_text = r.choices[0].message.content.strip()

    # Enforce single-question rule
    if ai_text.count("?") > 1:
        ai_text = ai_text.split("?")[0] + "?"

    state["conversation"].append({"role": "assistant", "content": ai_text})
    state["qa_count"]          += 1
    state["last_spoken_text"]   = ai_text
    state["interrupt_word_idx"] = -1
    state["remaining_text"]     = ""
    _cover(state, ai_text)
    state["transcription"].append({
        "speaker":   "AI",
        "text":      ai_text,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        "phase":     PHASES[state["current_phase"]],
    })
    return ai_text


def get_remaining_text(full_text: str, word_idx: int) -> str:
    """Return the portion of full_text starting at word_idx (used for interrupts)."""
    if not full_text or word_idx < 0:
        return ""
    words = full_text.split()
    return " ".join(words[max(0, word_idx - 1):]) if word_idx < len(words) else ""


# ══════════════════════════════════════════════════════════════════════════════
# STREAMING AI RESPONSE — SSE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

_SENTENCE_ENDS = frozenset("。！？；.!?;")


def stream_gpt_sentences(msgs, state: dict, sid: str, extra_prefix: str = None):
    """
    Generator that streams the GPT response as Server-Sent Events,
    one sentence at a time.

    Yields:
      data: {"type":"sentence","text":"..."}\\n\\n   for each sentence
      data: {"type":"done","interview_ended":bool}\\n\\n when finished
      data: {"type":"error","message":"..."}\\n\\n on GPT failure

    Enforces the single-question rule: stops emitting after the first '?'.
    Updates state (conversation, transcription, covered_*) when done.
    """
    all_sent      = []
    collected     = []
    question_sent = False

    if extra_prefix:
        all_sent.append(extra_prefix)
        yield f"data: {json.dumps({'type': 'sentence', 'text': extra_prefix})}\n\n"

    try:
        stream = openai_client.chat.completions.create(
            model=AZURE_OAI_DEPLOYMENT, max_tokens=150,
            temperature=0.7, messages=msgs, stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if not delta or not delta.content:
                continue
            token = delta.content
            collected.append(token)
            if question_sent:
                continue
            ch = token.rstrip()
            if ch and ch[-1] in _SENTENCE_ENDS:
                sentence = "".join(collected).strip()
                if sentence:
                    all_sent.append(sentence)
                    yield f"data: {json.dumps({'type': 'sentence', 'text': sentence})}\n\n"
                    if "?" in sentence:
                        question_sent = True
                    collected = []

        # Flush any remaining tokens
        if not question_sent:
            remainder = "".join(collected).strip()
            if remainder:
                all_sent.append(remainder)
                yield f"data: {json.dumps({'type': 'sentence', 'text': remainder})}\n\n"

    except Exception as e:
        print(f"[Stream] GPT error: {e}")
        _tb.print_exc()
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    # ── Update session state ─────────────────────────────────────────────
    full_text = " ".join(all_sent)
    ai_only   = " ".join(all_sent[1:]) if extra_prefix and len(all_sent) > 1 else full_text

    state["conversation"].append({"role": "assistant", "content": ai_only})
    state["qa_count"]          += 1
    state["last_spoken_text"]   = full_text
    state["interrupt_word_idx"] = -1
    state["remaining_text"]     = ""
    _cover(state, full_text)
    state["transcription"].append({
        "speaker":   "AI",
        "text":      full_text,
        "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
        "phase":     PHASES[state["current_phase"]],
    })
    save_turn_async(sid, state)

    yield f"data: {json.dumps({'type': 'done', 'interview_ended': not state['active']})}\n\n"


# ══════════════════════════════════════════════════════════════════════════════
# TRANSCRIPTION SAVE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def build_transcription_payload(sid: str, state: dict, save_reason: str = "background") -> dict:
    """
    Build the structured JSON payload that gets uploaded to Blob Storage.

    Schema:
      session_id, resume_id, jd_id, recruiter_comment_id, candidate_name,
      start_time, end_time, save_reason, total_turns,
      resume_skills, jd_skills, covered_skills, covered_jd, covered_projects,
      transcription: [{turn, speaker, text, timestamp, phase}, ...]
    """
    numbered_turns = [
        {**turn, "turn": idx + 1}
        for idx, turn in enumerate(state["transcription"])
    ]
    return {
        "session_id":           sid,
        "resume_id":            state.get("resume_id", "unknown"),
        "jd_id":                state.get("jd_id", ""),
        "recruiter_comment_id": state.get("recruiter_comment_id"),
        "candidate_name":       state.get("candidate_name", ""),
        "start_time":           state["interview_start"].strftime("%Y-%m-%d %H:%M:%S"),
        "end_time":             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "save_reason":          save_reason,
        "total_turns":          len(numbered_turns),
        "resume_skills":        state["resume_skills"],
        "jd_skills":            state["jd_skills"],
        "covered_skills":       list(state["covered_skills"]),
        "covered_jd":           list(state["covered_jd"]),
        "covered_projects":     list(state["covered_projects"]),
        "transcription":        numbered_turns,
    }


def auto_save_session(sid: str, state: dict, save_reason: str = "background") -> bool:
    """
    Upload the transcription JSON to Blob Storage.

    'background' / 'emergency':  overwrite the same fixed-name blob in-place.
    'final':
      1. Check if the candidate folder already exists.
      2. Delete ALL blobs in that folder (wipes old checkpoint files).
      3. Generate a fresh timestamp shared by transcription + recording.
      4. Store timestamp in state['final_folder_ts'] so the recording
         upload (api_upload_recording) lands in the same folder with the
         same timestamp.
      5. Upload the transcription.

    Returns True on success, False on any failure.
    """
    if not blob_storage_configured():
        return False
    if not state.get("transcription"):
        return False

    resume_id = state.get("resume_id", "unknown")

    if save_reason == "final":
        # Delete ALL existing blobs in the candidate folder first.
        # This wipes old background-checkpoint transcription files and any
        # stale recordings so the folder is clean before the definitive
        # final files are written.
        try:
            if check_candidate_folder_exists(resume_id):
                result = delete_candidate_folder(resume_id)
                print(f"[AutoSave] Folder cleaned — {result['deleted']} old blob(s) removed")
        except Exception as e:
            print(f"[AutoSave] Folder cleanup warning: {e}")

        fresh_ts        = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_rid        = re.sub(r"[^A-Za-z0-9_\-.]", "_", resume_id)
        new_blob        = f"{safe_rid}/transcription_{fresh_ts}.json"
        state["transcription_blob_name"] = new_blob
        state["final_folder_ts"]         = fresh_ts
        fixed_blob_name = new_blob
        print(f"[AutoSave] Final save — new path: {new_blob}")
    else:
        # Skip background saves if the session is already finalized
        if state.get("final_folder_ts"):
            print(f"[AutoSave] Skipping background save — session already finalized")
            return True
        fixed_blob_name = state.get("transcription_blob_name")

    payload = build_transcription_payload(sid, state, save_reason)
    try:
        blob_info = upload_transcription(resume_id, payload, fixed_blob_name=fixed_blob_name)
        print(f"[AutoSave] {save_reason} — {len(payload['transcription'])} turns -> {blob_info['blob_url']}")
        return True
    except Exception as e:
        print(f"[AutoSave] FAILED ({save_reason}): {e}")
        return False


def save_turn_async(sid: str, state: dict) -> None:
    """
    Fire-and-forget background transcription save triggered after every turn.
    Runs in a daemon thread so it never blocks the HTTP response.
    Skipped entirely if the session is already inactive or finalized,
    preventing race conditions with the final save.
    """
    if not state.get("active", True) or state.get("final_folder_ts"):
        return   # interview ended — final save will handle it
    def _do():
        try:
            auto_save_session(sid, state, save_reason="background")
        except Exception as e:
            print(f"[TurnSave] Failed for sid={sid[:8]}: {e}")
    threading.Thread(target=_do, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND SESSION CLEANUP DAEMON
# ══════════════════════════════════════════════════════════════════════════════

def _cleanup_sessions_task():
    """
    Runs every 10 minutes.  Saves + removes sessions that are either:
      • Marked inactive (interview ended normally server-side), OR
      • Older than 2 hours (abandoned / power cut / network drop).
    """
    while True:
        time.sleep(600)
        now       = datetime.datetime.now()
        to_delete = []

        with sessions_lock:
            for sid, state in interview_sessions.items():
                start_time = state.get("interview_start")
                stale      = (now - start_time).total_seconds() > 7200  # 2 h
                if not state.get("active") or stale:
                    to_delete.append((sid, dict(state)))   # snapshot for thread-safety
            for sid, _ in to_delete:
                del interview_sessions[sid]

        for sid, state in to_delete:
            if state.get("transcription") and not state.get("final_folder_ts"):
                # Session ended without a proper final save (abandoned / power cut).
                # Upload whatever transcription we have as a background save so
                # it is not lost.  Never delete blobs — recordings stay untouched.
                print(f"[Cleanup] Session {sid[:8]} has no final save — uploading transcript…")
                auto_save_session(sid, state, save_reason="background")
            else:
                print(f"[Cleanup] Session {sid[:8]} already finalized — no upload needed")
            print(f"[Cleanup] Removed session {sid[:8]} from memory")


# Start the cleanup daemon once when this module is imported
_cleanup_thread = threading.Thread(target=_cleanup_sessions_task, daemon=True)
_cleanup_thread.start()