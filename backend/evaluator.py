"""evaluator.py — 
Post-interview AI evaluation: scores the candidate using Azure OpenAI, stores results in Cosmos DB, and generates a PDF evaluation report via reportlab."""


import os
import json
import time
from collections import OrderedDict
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

from azure.cosmos import CosmosClient, exceptions
from openai import AzureOpenAI
from dotenv import load_dotenv
from blob_storage import upload_pdf, blob_storage_configured

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable,
)

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================
COSMOS_ENDPOINT              = os.getenv("COSMOS_ENDPOINT")
COSMOS_KEY                   = os.getenv("COSMOS_KEY")
COSMOS_DATABASE_NAME         = os.getenv("COSMOS_DATABASE_NAME")
COSMOS_RESUME_CONTAINER      = os.getenv("COSMOS_RESUME_CONTAINER", "resumes")
COSMOS_JD_CONTAINER          = os.getenv("COSMOS_JD_CONTAINER", "jobdescriptions")
COSMOS_EVALUATIONS_CONTAINER = os.getenv("COSMOS_EVALUATIONS_CONTAINER", "evaluations")

AZURE_OPENAI_CHAT_ENDPOINT    = os.getenv("AZURE_OPENAI_CHAT_ENDPOINT")
AZURE_OPENAI_CHAT_KEY         = os.getenv("AZURE_OPENAI_CHAT_KEY")
AZURE_OPENAI_CHAT_DEPLOYMENT  = os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT")
AZURE_OPENAI_CHAT_API_VERSION = os.getenv("AZURE_OPENAI_CHAT_API_VERSION")
TEMPERATURE = float(os.getenv("TEMPERATURE", 0.7))
MAX_TOKENS  = int(os.getenv("MAX_TOKENS", 2000))

PDF_OUTPUT_DIR = os.getenv("PDF_OUTPUT_DIR", "reports")

# =============================================================================
# CLIENTS
# =============================================================================
cosmos_client    = CosmosClient(COSMOS_ENDPOINT, credential=COSMOS_KEY)
database         = cosmos_client.get_database_client(COSMOS_DATABASE_NAME)
resume_container      = database.get_container_client(COSMOS_RESUME_CONTAINER)
jd_container          = database.get_container_client(COSMOS_JD_CONTAINER)
evaluations_container = database.get_container_client(COSMOS_EVALUATIONS_CONTAINER)

openai_client = AzureOpenAI(
    api_key=AZURE_OPENAI_CHAT_KEY,
    api_version=AZURE_OPENAI_CHAT_API_VERSION,
    azure_endpoint=AZURE_OPENAI_CHAT_ENDPOINT,
)

# =============================================================================
# PHASE CONSTANTS
# =============================================================================
PHASE_ORDER = [
    "GREETING", "ROLE_CONFIRMATION", "EXPERIENCE_DEEP_DIVE", "PROJECTS_DISCUSSION",
    "SKILLS_COVERAGE", "JD_ALIGNMENT", "BEHAVIORAL", "CANDIDATE_QUESTIONS", "CLOSING",
]

PHASE_LABELS = {
    "GREETING":             "Greeting",
    "ROLE_CONFIRMATION":    "Role Confirmation",
    "EXPERIENCE_DEEP_DIVE": "Experience Deep Dive",
    "PROJECTS_DISCUSSION":  "Projects Discussion",
    "SKILLS_COVERAGE":      "Skills Coverage",
    "JD_ALIGNMENT":         "JD Alignment",
    "BEHAVIORAL":           "Behavioural",
    "CANDIDATE_QUESTIONS":  "Candidate Questions",
    "CLOSING":              "Closing",
}

# =============================================================================
# TRANSCRIPT HELPERS
# =============================================================================
def extract_interview_duration(transcript_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract interview duration from transcript JSON.
    Strategy 1: top-level start_time / end_time.
    Strategy 2: first / last utterance timestamp.
    """
    result = {
        "interview_start": None, "interview_end": None,
        "duration_seconds": None, "duration_formatted": None,
        "duration_source": "unavailable",
    }

    def _fmt(s):
        h, r = divmod(s, 3600); m, s = divmod(r, 60)
        return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")

    raw_start = transcript_data.get("start_time")
    raw_end   = transcript_data.get("end_time")
    if raw_start and raw_end:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                diff = int((datetime.strptime(raw_end, fmt) - datetime.strptime(raw_start, fmt)).total_seconds())
                if diff >= 0:
                    result.update({"interview_start": raw_start, "interview_end": raw_end,
                                   "duration_seconds": diff, "duration_formatted": _fmt(diff),
                                   "duration_source": "start_time/end_time fields"})
                    return result
            except ValueError:
                continue

    utterances = transcript_data.get("transcription", [])
    if isinstance(utterances, list) and len(utterances) >= 2:
        first_ts, last_ts = utterances[0].get("timestamp"), utterances[-1].get("timestamp")
        if first_ts and last_ts:
            for fmt in ("%H:%M:%S", "%H:%M:%S.%f", "%M:%S"):
                try:
                    diff = int((datetime.strptime(last_ts, fmt) - datetime.strptime(first_ts, fmt)).total_seconds())
                    if diff < 0: diff += 86400
                    result.update({"interview_start": first_ts, "interview_end": last_ts,
                                   "duration_seconds": diff, "duration_formatted": _fmt(diff),
                                   "duration_source": "first/last utterance timestamps"})
                    return result
                except ValueError:
                    continue
    return result


def extract_qa_by_phase(transcript_data: Dict[str, Any]) -> Dict[str, list]:
    """
    Parse utterance list into Q&A pairs grouped and ordered by phase.
    Pairs consecutive AI -> Candidate turns. AI turns without a reply get answer=None.
    """
    utterances = transcript_data.get("transcription", [])
    if not isinstance(utterances, list) or not utterances:
        return {}

    phase_utterances: Dict[str, list] = OrderedDict()
    for u in utterances:
        if isinstance(u, dict):
            phase_utterances.setdefault(u.get("phase", "UNKNOWN").upper(), []).append(u)

    qa_by_phase: Dict[str, list] = {}
    for phase, turns in phase_utterances.items():
        pairs, i = [], 0
        while i < len(turns):
            turn = turns[i]
            if (turn.get("speaker") or "").strip().upper() == "AI":
                answer = None
                if i + 1 < len(turns) and (turns[i+1].get("speaker") or "").strip().upper() == "CANDIDATE":
                    answer = turns[i+1].get("text", "").strip()
                    i += 1
                pairs.append({"question": turn.get("text","").strip(),
                               "answer": answer, "timestamp": turn.get("timestamp","")})
            i += 1
        if pairs:
            qa_by_phase[phase] = pairs

    ordered: Dict[str, list] = {}
    for phase in PHASE_ORDER:
        if phase in qa_by_phase: ordered[phase] = qa_by_phase[phase]
    for phase in qa_by_phase:
        if phase not in ordered: ordered[phase] = qa_by_phase[phase]
    return ordered


def flatten_transcript(transcript_data: Dict[str, Any]) -> str:
    """Flatten transcript JSON to plain text for LLM input."""
    text = transcript_data.get("transcript") or transcript_data.get("text") or transcript_data.get("content")
    if text: return text
    utterances = transcript_data.get("transcription", [])
    if isinstance(utterances, list) and utterances:
        return " ".join(u.get("text", "") for u in utterances if isinstance(u, dict))
    return json.dumps(transcript_data)


def transform_resume_to_text(resume_doc: Dict[str, Any]) -> str:
    """Flatten a resume Cosmos document into plain text for LLM input."""
    try:
        data, contact = resume_doc.get("metadata",{}).get("extracted_data",{}), {}
        contact = data.get("contact", {})
        lines = [f"Candidate Name: {contact.get('name','Unknown')}"]
        if contact.get("email"):    lines.append(f"Email: {contact['email']}")
        if contact.get("location"): lines.append(f"Location: {contact['location']}")
        lines.append("")
        if data.get("summary"): lines += ["### SUMMARY", str(data["summary"]), ""]
        skills = data.get("skills", [])
        if skills: lines += ["### SKILLS", ", ".join(skills) if isinstance(skills, list) else str(skills), ""]
        experience = data.get("experience", [])
        if experience:
            lines.append("### EXPERIENCE")
            for exp in experience:
                if not isinstance(exp, dict): continue
                lines += [f"Title: {exp.get('title','N/A')}", f"Company: {exp.get('company','N/A')}",
                          f"Duration: {exp.get('duration_display','N/A')}"]
                bullets = exp.get("description", [])
                if isinstance(bullets, list): [lines.append(f"  - {b}") for b in bullets]
                elif isinstance(bullets, str): lines.append(f"  - {bullets}")
                tech = exp.get("technologies", [])
                if tech and isinstance(tech, list): lines.append(f"  Technologies: {', '.join(tech)}")
                lines.append("")
        projects = data.get("projects", [])
        if projects:
            lines.append("### PROJECTS")
            for proj in projects:
                if not isinstance(proj, dict): continue
                lines += [f"Name: {proj.get('name','N/A')}", f"Description: {proj.get('description','N/A')}"]
                tech = proj.get("technologies", [])
                if tech and isinstance(tech, list): lines.append(f"Technologies: {', '.join(tech)}")
                lines.append("")
        education = data.get("education", [])
        if education:
            lines.append("### EDUCATION")
            for edu in education:
                if isinstance(edu, dict):
                    lines.append(f"- {edu.get('degree','N/A')} from {edu.get('institution','N/A')} ({edu.get('graduation_year','N/A')})")
                else: lines.append(f"- {edu}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        print(f"Error transforming resume: {e}"); return ""


def transform_jd_to_text(jd_doc: Dict[str, Any]) -> str:
    """Flatten a JD Cosmos document into plain text for LLM input."""
    try:
        metadata = jd_doc.get("metadata", {})
        lines = [f"Role Title: {metadata.get('title','Unknown Role')}", ""]
        req = metadata.get("required_skills", [])
        if req: lines += ["### REQUIRED SKILLS", ", ".join(req), ""]
        nice = metadata.get("nice_to_have_skills", [])
        if nice: lines += ["### NICE TO HAVE SKILLS", ", ".join(nice), ""]
        resp = metadata.get("responsibilities", [])
        if resp:
            lines.append("### RESPONSIBILITIES")
            for r in resp: lines.append(f"- {r}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        print(f"Error transforming JD: {e}"); return ""

# =============================================================================
# PDF REPORT — COLOURS & FLOWABLES
# =============================================================================
_DARK_NAVY   = colors.HexColor("#0D1B2A")
_ACCENT_BLUE = colors.HexColor("#1B6CA8")
_LIGHT_BLUE  = colors.HexColor("#E8F4FD")
_SUCCESS     = colors.HexColor("#27AE60")
_WARNING     = colors.HexColor("#E67E22")
_DANGER      = colors.HexColor("#C0392B")
_NEUTRAL     = colors.HexColor("#7F8C8D")
_LIGHT_GRAY  = colors.HexColor("#F5F6FA")
_MID_GRAY    = colors.HexColor("#BDC3C7")
_TEXT_DARK   = colors.HexColor("#2C3E50")
_WHITE       = colors.white


class _ScoreBar(Flowable):
    def __init__(self, score, width=310, height=10):
        super().__init__()
        self.score = score; self.width = width; self.height = height

    def draw(self):
        self.canv.setFillColor(_MID_GRAY)
        self.canv.roundRect(0, 0, self.width, self.height, 4, fill=1, stroke=0)
        bar_color = _SUCCESS if self.score >= 75 else (_WARNING if self.score >= 55 else _DANGER)
        self.canv.setFillColor(bar_color)
        self.canv.roundRect(0, 0, (self.score / 100) * self.width, self.height, 4, fill=1, stroke=0)

    def wrap(self, aW, aH): return self.width, self.height


class _SectionHeader(Flowable):
    def __init__(self, text, doc_width, bg=None, fg=None):
        super().__init__()
        self.text = text; self.doc_width = doc_width
        self.bg = bg or _ACCENT_BLUE; self.fg = fg or _WHITE; self.height = 22

    def draw(self):
        self.canv.setFillColor(self.bg)
        self.canv.roundRect(0, 0, self.doc_width, self.height, 4, fill=1, stroke=0)
        self.canv.setFillColor(self.fg)
        self.canv.setFont("Helvetica-Bold", 10)
        self.canv.drawString(8, 6, self.text.upper())

    def wrap(self, aW, aH): return self.doc_width, self.height

# =============================================================================
# PDF REPORT — BUILDER HELPERS
# =============================================================================
def _pdf_styles():
    return {
        "bullet":      ParagraphStyle("bullet",      fontName="Helvetica", fontSize=9,   leading=13, leftIndent=10, textColor=_TEXT_DARK),
        "narrative":   ParagraphStyle("narrative",   fontName="Helvetica", fontSize=9,   leading=15, textColor=_TEXT_DARK, spaceAfter=6),
        "score_label": ParagraphStyle("score_label", fontName="Helvetica", fontSize=8.5, leading=11, textColor=_TEXT_DARK),
    }

def _hex(score):
    if score >= 75: return "#27AE60"  # Strong
    if score >= 55: return "#E67E22"  # Moderate
    return "#C0392B"                  # Needs Work

def _score_card(label, score, width):
    c = _hex(score)
    return Table([[Paragraph(
        f'<font color="#7F8C8D" size="8">{label}</font><br/>'
        f'<font color="{c}" size="14"><b>{score:.1f}</b></font>'
        f'<font color="#7F8C8D" size="9">/100</font>',
        ParagraphStyle("card", fontName="Helvetica", fontSize=9, leading=14, alignment=TA_CENTER)
    )]], colWidths=[width], style=[
        ("BACKGROUND",(0,0),(-1,-1),_LIGHT_GRAY), ("BOX",(0,0),(-1,-1),0.5,_MID_GRAY),
        ("TOPPADDING",(0,0),(-1,-1),10), ("BOTTOMPADDING",(0,0),(-1,-1),10), ("ALIGN",(0,0),(-1,-1),"CENTER"),
    ])

def _duration_card(duration, time_range, width):
    return Table([[Paragraph(
        f'<font color="#7F8C8D" size="8">INTERVIEW DURATION</font><br/>'
        f'<font color="#1B6CA8" size="13"><b>{duration}</b></font>'
        + (f'<br/><font color="#7F8C8D" size="7">{time_range}</font>' if time_range else ''),
        ParagraphStyle("dcrd", fontName="Helvetica", fontSize=9, leading=14, alignment=TA_CENTER)
    )]], colWidths=[width], style=[
        ("BACKGROUND",(0,0),(-1,-1),_LIGHT_BLUE), ("BOX",(0,0),(-1,-1),0.5,_ACCENT_BLUE),
        ("TOPPADDING",(0,0),(-1,-1),10), ("BOTTOMPADDING",(0,0),(-1,-1),10), ("ALIGN",(0,0),(-1,-1),"CENTER"),
    ])

def _swi_block(strengths, weaknesses, improvements, content_w, S):
    """3-column Strengths / Weaknesses / Scope of Improvement table."""
    COL = (content_w - 8) / 3
    def hdr(text, bg):
        return Paragraph(f'<font color="#FFFFFF"><b>{text}</b></font>',
                         ParagraphStyle("ch", fontName="Helvetica-Bold", fontSize=8, textColor=_WHITE, alignment=TA_CENTER))
    def items(lst, color):
        cell = []
        for item in lst:
            cell.append(Paragraph(f'<font color="{color}"><b>&#8226;</b></font>  {item}',
                                  ParagraphStyle("ci", fontName="Helvetica", fontSize=8, leading=12, textColor=_TEXT_DARK, leftIndent=4)))
            cell.append(Spacer(1, 2))
        return cell or [Paragraph("None noted.", ParagraphStyle("none", fontName="Helvetica", fontSize=8, textColor=_NEUTRAL))]

    tbl = Table(
        [[hdr("Strengths", _SUCCESS), hdr("Weaknesses", _DANGER), hdr("Scope of Improvement", _ACCENT_BLUE)],
         [items(strengths,"#27AE60"), items(weaknesses,"#C0392B"), items(improvements,"#1B6CA8")]],
        colWidths=[COL, COL, COL]
    )
    tbl.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(0,0),_SUCCESS), ("BACKGROUND",(1,0),(1,0),_DANGER), ("BACKGROUND",(2,0),(2,0),_ACCENT_BLUE),
        ("TOPPADDING",(0,0),(-1,0),5), ("BOTTOMPADDING",(0,0),(-1,0),5), ("ALIGN",(0,0),(-1,0),"CENTER"),
        ("BACKGROUND",(0,1),(0,1),colors.HexColor("#EAF9EF")),
        ("BACKGROUND",(1,1),(1,1),colors.HexColor("#FDEDEC")),
        ("BACKGROUND",(2,1),(2,1),colors.HexColor("#EBF5FB")),
        ("VALIGN",(0,1),(-1,1),"TOP"),
        ("TOPPADDING",(0,1),(-1,-1),6), ("BOTTOMPADDING",(0,1),(-1,-1),6),
        ("LEFTPADDING",(0,1),(-1,-1),6), ("RIGHTPADDING",(0,1),(-1,-1),6),
        ("BOX",(0,0),(-1,-1),0.5,_MID_GRAY), ("INNERGRID",(0,0),(-1,-1),0.3,_MID_GRAY),
    ]))
    return tbl

def _score_label_pill(score: int) -> str:
    """Return (text, hex_bg) for the coloured label pill beside a skill score."""
    if score >= 75: return "Strong",   "#27AE60"
    if score >= 55: return "Moderate", "#E67E22"
    return "Needs Work",               "#C0392B"


def _skill_table(skills, bar_w, name_w, score_w, style_name, S):
    """
    Enhanced skill table:
      Col 0  — skill name + 75-word summary below
      Col 1  — score bar with colour label pill (Strong / Moderate / Needs Work)
      Col 2  — numeric score
    """
    SUMMARY_STYLE = ParagraphStyle(
        "sk_summary", fontName="Helvetica", fontSize=7.5, leading=11,
        textColor=_NEUTRAL, spaceAfter=0, spaceBefore=3
    )
    PILL_STYLE = ParagraphStyle(
        "pill", fontName="Helvetica-Bold", fontSize=7, leading=9, alignment=TA_CENTER
    )
    NAME_STYLE = ParagraphStyle(
        "sk_name", fontName="Helvetica-Bold", fontSize=9, leading=12, textColor=_TEXT_DARK
    )

    rows = []
    for sk in skills:
        sc      = sk["score"]
        summary = sk.get("summary", "")
        label, pill_bg = _score_label_pill(sc)
        hex_c   = _hex(sc)

        # Left cell: name + summary
        name_cell = [Paragraph(sk["name"], NAME_STYLE)]
        if summary:
            name_cell.append(Paragraph(summary, SUMMARY_STYLE))

        # Middle cell: bar on top row, colour pill on bottom row
        bar_cell = [
            _ScoreBar(sc, width=int(bar_w)),
            Spacer(1, 3),
            Table([[Paragraph(
                f'<font color="#FFFFFF"><b>{label}</b></font>',
                PILL_STYLE
            )]], colWidths=[bar_w],
                style=[
                    ("BACKGROUND",    (0,0),(-1,-1), colors.HexColor(pill_bg)),
                    ("TOPPADDING",    (0,0),(-1,-1), 2),
                    ("BOTTOMPADDING", (0,0),(-1,-1), 2),
                    ("LEFTPADDING",   (0,0),(-1,-1), 4),
                    ("RIGHTPADDING",  (0,0),(-1,-1), 4),
                    ("ALIGN",         (0,0),(-1,-1), "CENTER"),
                ]),
        ]

        # Right cell: numeric score
        score_cell = [Paragraph(
            f'<font color="{hex_c}"><b>{sc}</b></font>',
            ParagraphStyle(style_name, fontName="Helvetica-Bold", fontSize=11,
                           alignment=TA_RIGHT, textColor=_TEXT_DARK)
        )]

        rows.append([name_cell, bar_cell, score_cell])

    tbl = Table(rows, colWidths=[name_w, bar_w, score_w])
    tbl.setStyle(TableStyle([
        ("VALIGN",        (0,0),(-1,-1), "TOP"),
        ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ("RIGHTPADDING",  (0,0),(-1,-1), 4),
        ("TOPPADDING",    (0,0),(-1,-1), 6),
        ("BOTTOMPADDING", (0,0),(-1,-1), 6),
        ("ROWBACKGROUNDS",(0,0),(-1,-1), [_WHITE, _LIGHT_GRAY]),
        ("LINEBELOW",     (0,0),(-1,-1), 0.3, _MID_GRAY),
    ]))
    return tbl

def _recommendation(score):
    if score >= 85: return "Strong Hire",             _SUCCESS
    if score >= 70: return "Hire",                    _SUCCESS
    if score >= 45: return "Consider with Reservations", _WARNING
    return "Do Not Hire",                             _DANGER

# =============================================================================
# PDF REPORT — MAIN BUILDER
# =============================================================================
def build_pdf(data: dict, output_path: str) -> str:
    """Render the evaluation dict to a PDF. Returns output_path."""
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    L = R = 18 * mm; T = B = 15 * mm
    CONTENT_W = A4[0] - L - R
    doc   = SimpleDocTemplate(output_path, pagesize=A4, leftMargin=L, rightMargin=R, topMargin=T, bottomMargin=B)
    S     = _pdf_styles()
    story = []
    cd    = data["candidateData"]
    date  = data.get("evaluation_date", "")[:10]

    dur_fmt = data.get("interview_duration_formatted") or "N/A"
    def _hhmm(ts):
        if not ts: return ""
        p = str(ts).split(" "); return p[-1][:5] if len(p) > 1 else str(ts)[:5]
    time_range = (f"{_hhmm(data.get('interview_start'))} \u2013 {_hhmm(data.get('interview_end'))}"
                  if data.get("interview_start") else "")

    # Header banner
    story.append(Table([[
        Paragraph('<font color="#FFFFFF"><b>INTERVIEW EVALUATION REPORT</b></font>',
                  ParagraphStyle("hdr", fontName="Helvetica-Bold", fontSize=16, textColor=_WHITE, leading=20)),
        Paragraph(
            f'<font color="#E8F4FD"><b>{cd["name"]}</b></font><br/>'
            f'<font color="#BDC3C7">{cd["jobRole"]}</font><br/>'
            f'<font color="#BDC3C7">{cd.get("email","")}'
            + (f'  |  {cd.get("location","")}' if cd.get("location") else '') + '</font><br/>'
            f'<font color="#BDC3C7">Date: {date}'
            + (f'  |  Duration: {dur_fmt}' if dur_fmt != "N/A" else '') + '</font>',
            ParagraphStyle("hdr2", fontName="Helvetica", fontSize=9, textColor=_WHITE, leading=13, alignment=TA_RIGHT)
        ),
    ]], colWidths=[CONTENT_W*0.45, CONTENT_W*0.55], style=[
        ("BACKGROUND",(0,0),(-1,-1),_DARK_NAVY), ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ("LEFTPADDING",(0,0),(-1,-1),12), ("RIGHTPADDING",(0,0),(-1,-1),12),
        ("TOPPADDING",(0,0),(-1,-1),14), ("BOTTOMPADDING",(0,0),(-1,-1),14),
    ]))
    story.append(Spacer(1, 10))

    # Score cards
    cw = (CONTENT_W - 12) / 4
    story.append(Table([[
        _score_card("OVERALL SCORE",  data["overall_score"],       cw),
        _score_card("TECHNICAL",      data["technical_average"],   cw),
        _score_card("SOFT SKILLS",    data["soft_skills_average"], cw),
        _duration_card(dur_fmt,       time_range,                  cw),
    ]], colWidths=[cw]*4, style=[("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4)]))
    story.append(Spacer(1, 12))

    # Technical skills
    BAR_W = CONTENT_W * 0.52; SCORE_W = 28; NAME_W = CONTENT_W - BAR_W - SCORE_W - 8
    story += [_SectionHeader("Technical Skills Assessment", CONTENT_W), Spacer(1,6),
              _skill_table(data["technicalSkills"], BAR_W, NAME_W, SCORE_W, "sc1", S), Spacer(1,10),
              _swi_block(data["technicalStrengths"], data["technicalWeaknesses"], data["technicalImprovements"], CONTENT_W, S),
              Spacer(1,12)]

    # Soft skills
    story += [_SectionHeader("Soft Skills Assessment", CONTENT_W), Spacer(1,6),
              _skill_table(data["softSkills"], BAR_W, NAME_W, SCORE_W, "sc2", S), Spacer(1,10),
              _swi_block(data["softStrengths"], data["softWeaknesses"], data["softImprovements"], CONTENT_W, S),
              Spacer(1,12)]

    # Positive observations (strengths from both blocks combined)
    positives = (
        data.get("technicalStrengths", []) +
        data.get("softStrengths", [])
    )
    if positives:
        story += [
            _SectionHeader("Positive Observations", CONTENT_W, bg=_SUCCESS),
            Spacer(1, 6),
            Table(
                [[Paragraph(f'<font color="#27AE60"><b>&#10003;</b></font>  {obs}', S["bullet"])]
                 for obs in positives],
                colWidths=[CONTENT_W],
                style=[
                    ("BACKGROUND",    (0,0),(-1,-1), colors.HexColor("#EAF9EF")),
                    ("BOX",           (0,0),(-1,-1), 0.5, colors.HexColor("#A9DFBF")),
                    ("TOPPADDING",    (0,0),(-1,-1), 5),
                    ("BOTTOMPADDING", (0,0),(-1,-1), 5),
                    ("LEFTPADDING",   (0,0),(-1,-1), 10),
                ]
            ),
            Spacer(1, 12),
        ]

    # Negative observations (no "Red Flags" in heading)
    negatives = data.get("negativeObservations", [])
    if negatives:
        story += [
            _SectionHeader("Negative Observations", CONTENT_W, bg=_DANGER),
            Spacer(1, 6),
            Table(
                [[Paragraph(f'<font color="#C0392B"><b>&#9888;</b></font>  {obs}', S["bullet"])]
                 for obs in negatives],
                colWidths=[CONTENT_W],
                style=[
                    ("BACKGROUND",    (0,0),(-1,-1), colors.HexColor("#FDF2F2")),
                    ("BOX",           (0,0),(-1,-1), 0.5, colors.HexColor("#E8A0A0")),
                    ("TOPPADDING",    (0,0),(-1,-1), 5),
                    ("BOTTOMPADDING", (0,0),(-1,-1), 5),
                    ("LEFTPADDING",   (0,0),(-1,-1), 10),
                ]
            ),
            Spacer(1, 12),
        ]

    # Hiring recommendation badge + recommendation sentence directly below
    rec_text, rec_color = _recommendation(data["overall_score"])
    story.append(Table([[Paragraph(
        f'<font color="#FFFFFF"><b>HIRING RECOMMENDATION:  {rec_text}</b></font>',
        ParagraphStyle("badge", fontName="Helvetica-Bold", fontSize=10,
                       textColor=_WHITE, alignment=TA_CENTER)
    )]], colWidths=[CONTENT_W], style=[
        ("BACKGROUND",    (0,0),(-1,-1), rec_color),
        ("TOPPADDING",    (0,0),(-1,-1), 10),
        ("BOTTOMPADDING", (0,0),(-1,-1), 10),
        ("ALIGN",         (0,0),(-1,-1), "CENTER"),
    ]))

    # Split overallResult: last paragraph (the Recommendation sentence) sits
    # directly under the badge; remaining paragraphs go under the section title.
    overall_paras = [p.strip() for p in data.get("overallResult","").split("\n\n") if p.strip()]
    rec_para   = data.get("hiringRecommendation") or (overall_paras[-1] if overall_paras else "")
    body_paras = overall_paras

    if rec_para:
        story.append(Spacer(1, 6))
        story.append(Paragraph(rec_para, S["narrative"]))

    story.append(Spacer(1, 10))

    # Overall Evaluation Summary — remaining paragraphs
    story.append(_SectionHeader("Overall Evaluation Summary", CONTENT_W))
    story.append(Spacer(1, 6))
    for para in body_paras:
        story.append(Paragraph(para, S["narrative"]))
    story.append(Spacer(1, 16))

    # Q&A transcript
    qa_by_phase = data.get("qa_by_phase", {})
    if qa_by_phase:
        story += [_SectionHeader("Interview Q&A Transcript", CONTENT_W, bg=colors.HexColor("#2C3E50")), Spacer(1,8)]
        Q_ST  = ParagraphStyle("qs", fontName="Helvetica-Bold", fontSize=8.5, leading=13, textColor=_DARK_NAVY)
        A_ST  = ParagraphStyle("as", fontName="Helvetica",      fontSize=8.5, leading=13, textColor=_TEXT_DARK, leftIndent=10)
        TS_ST = ParagraphStyle("ts", fontName="Helvetica",      fontSize=7,   textColor=_NEUTRAL)
        PH_ST = ParagraphStyle("ph", fontName="Helvetica-Bold", fontSize=9,   textColor=_WHITE)
        NEG_KW = ["don't know", "i don't know", "sorry", "never faced", "no idea"]

        for phase_key, pairs in qa_by_phase.items():
            story.append(Table([[Paragraph(PHASE_LABELS.get(phase_key, phase_key.replace("_"," ").title()), PH_ST)]],
                               colWidths=[CONTENT_W], style=[
                                   ("BACKGROUND",(0,0),(-1,-1),_ACCENT_BLUE),
                                   ("TOPPADDING",(0,0),(-1,-1),4), ("BOTTOMPADDING",(0,0),(-1,-1),4),
                                   ("LEFTPADDING",(0,0),(-1,-1),8),
                               ]))
            story.append(Spacer(1, 4))
            for idx, pair in enumerate(pairs):
                q_text  = pair.get("question", "")
                a_text  = pair.get("answer") or "\u2014"
                ts_text = pair.get("timestamp", "")
                is_neg  = any(kw in a_text.lower() for kw in NEG_KW)
                row_bg  = colors.HexColor("#FDF2F2") if is_neg else (_WHITE if idx % 2 == 0 else _LIGHT_GRAY)
                a_color = "#C0392B" if is_neg else "#2C3E50"
                q_cell  = [Paragraph(f'<font color="#1B6CA8"><b>Q:</b></font>  {q_text}', Q_ST)]
                if ts_text: q_cell.append(Paragraph(ts_text, TS_ST))
                story.append(Table([[
                    q_cell,
                    [Paragraph(f'<font color="{a_color}"><b>A:</b></font>  <font color="{a_color}">{a_text}</font>', A_ST)]
                ]], colWidths=[CONTENT_W*0.46, CONTENT_W*0.54], style=[
                    ("BACKGROUND",(0,0),(-1,-1),row_bg), ("VALIGN",(0,0),(-1,-1),"TOP"),
                    ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5),
                    ("LEFTPADDING",(0,0),(-1,-1),6), ("RIGHTPADDING",(0,0),(-1,-1),6),
                    ("LINEBELOW",(0,0),(-1,-1),0.3,_MID_GRAY),
                ]))
            story.append(Spacer(1, 10))
        story.append(Spacer(1, 6))

    # Footer
    story.append(Table([[Paragraph(
        f'<font color="#7F8C8D" size="7">Generated by AI Evaluation Pipeline  |  '
        f'Candidate: {cd["name"]}  |  Role: {cd["jobRole"]}  |  {date}</font>',
        ParagraphStyle("footer", fontName="Helvetica", fontSize=7, textColor=_NEUTRAL, alignment=TA_CENTER)
    )]], colWidths=[CONTENT_W], style=[
        ("TOPPADDING",(0,0),(-1,-1),8), ("LINEABOVE",(0,0),(-1,0),0.5,_MID_GRAY),
    ]))

    doc.build(story)
    print(f"PDF saved: {output_path}")
    return output_path

# =============================================================================
# COSMOS DB HELPERS
# =============================================================================
def get_resume(resume_id: str) -> Optional[Dict[str, Any]]:
    try:
        item = resume_container.read_item(item=resume_id, partition_key=resume_id)
        if isinstance(item.get("metadata"), str):
            item["metadata"] = json.loads(item["metadata"])
        return item
    except exceptions.CosmosResourceNotFoundError:
        return None
    except Exception as e:
        print(f"Error getting resume: {e}"); return None


def get_jd(jd_id: str) -> Optional[Dict[str, Any]]:
    try:
        item = jd_container.read_item(item=jd_id, partition_key=jd_id)
        if isinstance(item.get("metadata"), str):
            item["metadata"] = json.loads(item["metadata"])
        return item
    except exceptions.CosmosResourceNotFoundError:
        return None
    except Exception as e:
        print(f"Error getting JD: {e}"); return None


def save_evaluation(resume_id: str, evaluation: Dict[str, Any]) -> bool:
    """
    Write the evaluation document to the dedicated 'evaluations' Cosmos container.
    Document id = resume_id so it is easy to look up by candidate.
    The resume container is NOT touched here — evaluation data lives in evaluations only.
    """
    try:
        doc = {"id": resume_id, **evaluation}
        evaluations_container.upsert_item(doc)
        print(f"[CosmosDB] Evaluation saved to 'evaluations' container for '{resume_id}'")
        return True
    except Exception as e:
        print(f"Error saving evaluation: {e}"); return False


def update_resume_evaluation(resume_id: str, evaluation: Dict[str, Any]) -> bool:
    """
    DEPRECATED — kept for backward compatibility only.
    Use save_evaluation() instead.  This now delegates to save_evaluation()
    and no longer writes the evaluation payload into the resume document.
    """
    return save_evaluation(resume_id, evaluation)


def update_resume_state(resume_id: str, new_state: str, interview_link: str = None) -> bool:
    try:
        resume = get_resume(resume_id)
        if not resume: return False
        # evaluation is now stored in the 'evaluations' container — not in the resume doc
        doc = {"id": resume_id, "metadata": resume.get("metadata"), "state": new_state,
               "jd_id": resume.get("jd_id")}
        doc["interview_link"] = ((interview_link if interview_link.strip() else None)
                                 if interview_link is not None else resume.get("interview_link"))
        resume_container.upsert_item(doc)
        return True
    except Exception as e:
        print(f"Error updating state: {e}"); return False

# ========== TRANSFORM FUNCTIONS ==========
def transform_resume_to_text(resume_doc: Dict[str, Any]) -> str:
    try:
        metadata = resume_doc.get("metadata", {})
        data = metadata.get("extracted_data", {})
        contact = data.get("contact", {})
        lines = []
        lines.append(f"Candidate Name: {contact.get('name', 'Unknown')}")
        if contact.get("email"):
            lines.append(f"Email: {contact.get('email')}")
        if contact.get("location"):
            lines.append(f"Location: {contact.get('location')}")
        lines.append("")
        if data.get("summary"):
            lines.append("### SUMMARY")
            lines.append(str(data.get("summary")))
            lines.append("")
        skills = data.get("skills", [])
        if skills:
            lines.append("### SKILLS")
            lines.append(", ".join(skills) if isinstance(skills, list) else str(skills))
            lines.append("")
        experience = data.get("experience", [])
        if experience:
            lines.append("### EXPERIENCE")
            for exp in experience:
                if not isinstance(exp, dict):
                    continue
                lines.append(f"Title: {exp.get('title', 'N/A')}")
                lines.append(f"Company: {exp.get('company', 'N/A')}")
                lines.append(f"Duration: {exp.get('duration_display', 'N/A')}")
                bullets = exp.get("description", [])
                if isinstance(bullets, list):
                    for b in bullets:
                        lines.append(f"  - {b}")
                elif isinstance(bullets, str):
                    lines.append(f"  - {bullets}")
                tech = exp.get("technologies", [])
                if tech and isinstance(tech, list):
                    lines.append(f"  Technologies: {', '.join(tech)}")
                lines.append("")
        projects = data.get("projects", [])
        if projects:
            lines.append("### PROJECTS")
            for proj in projects:
                if not isinstance(proj, dict):
                    continue
                lines.append(f"Name: {proj.get('name', 'N/A')}")
                lines.append(f"Description: {proj.get('description', 'N/A')}")
                tech = proj.get("technologies", [])
                if tech and isinstance(tech, list):
                    lines.append(f"Technologies: {', '.join(tech)}")
                lines.append("")
        education = data.get("education", [])
        if education:
            lines.append("### EDUCATION")
            for edu in education:
                if isinstance(edu, dict):
                    degree = edu.get("degree", "N/A")
                    institution = edu.get("institution", "N/A")
                    year = edu.get("graduation_year", "N/A")
                    lines.append(f"- {degree} from {institution} ({year})")
                else:
                    lines.append(f"- {str(edu)}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        print(f"Error transforming resume: {e}")
        return ""

def transform_jd_to_text(jd_doc: Dict[str, Any]) -> str:
    try:
        metadata = jd_doc.get("metadata", {})
        lines = []
        title = metadata.get("title", "Unknown Role")
        lines.append(f"Role Title: {title}")
        lines.append("")
        req_skills = metadata.get("required_skills", [])
        if req_skills:
            lines.append("### REQUIRED SKILLS")
            lines.append(", ".join(req_skills))
            lines.append("")
        nice_skills = metadata.get("nice_to_have_skills", [])
        if nice_skills:
            lines.append("### NICE TO HAVE SKILLS")
            lines.append(", ".join(nice_skills))
            lines.append("")
        responsibilities = metadata.get("responsibilities", [])
        if responsibilities:
            lines.append("### RESPONSIBILITIES")
            for resp in responsibilities:
                lines.append(f"- {resp}")
            lines.append("")
        return "\n".join(lines).strip()
    except Exception as e:
        print(f"Error transforming JD: {e}")
        return ""

# ========== INTERVIEW QUALITY VALIDATION ==========
def count_meaningful_answers(transcript_data: Dict[str, Any]) -> int:
    """
    Count candidate responses that have meaningful technical/substantive content.
    Filters out:
    - Responses that are ONLY short non-answers: "yes", "no", "okay", "sure", "thanks"
    - Responses with explicit "I don't know" / "no knowledge" phrases
    - Responses that are way too short (< 5 words) UNLESS they contain substantive content
    """
    utterances = transcript_data.get("transcription", [])
    if not isinstance(utterances, list):
        return 0
    
    # Phrases that indicate lack of knowledge or disengagement
    blocker_phrases = [
        "i don't know", "don't know", "no idea", "no knowledge", "not sure",
        "i'm not sure", "unsure", "can't say", "no clue", "beats me",
        "end interview", "end session", "that's all", "i'm done"
    ]
    
    # Responses that are ONLY one of these are not meaningful
    single_word_nonresponses = [
        "yes", "no", "ok", "okay", "sure", "thanks", "thank you",
        "right", "got it", "understood"
    ]
    
    meaningful_count = 0
    
    for utterance in utterances:
        if not isinstance(utterance, dict):
            continue
        if (utterance.get("speaker") or "").strip().upper() != "CANDIDATE":
            continue
        
        text = (utterance.get("text") or "").strip()
        if not text:
            continue
        
        text_lower = text.lower()
        
        # Filter 1: Has blocker phrase = not meaningful
        if any(phrase in text_lower for phrase in blocker_phrases):
            continue
        
        # Filter 2: Is ONLY a single-word non-response = not meaningful
        if text_lower in single_word_nonresponses or text_lower.rstrip('.!?,') in single_word_nonresponses:
            continue
        
        # Filter 3: Too short (< 4 words) = not meaningful
        if len(text.split()) < 4:
            continue
        
        meaningful_count += 1
    
    return meaningful_count


def extract_phase_content(transcript_data: Dict[str, Any], target_phases: list) -> str:
    """Extract utterances from specific phases to identify skills discussed."""
    utterances = transcript_data.get("transcription", [])
    if not isinstance(utterances, list):
        return ""
    
    phase_text = []
    for u in utterances:
        if isinstance(u, dict) and u.get("phase") in target_phases:
            speaker = u.get("speaker", "Unknown")
            text = u.get("text", "")
            if text.strip():
                phase_text.append(f"{speaker}: {text}")
    
    return "\n".join(phase_text).strip()


def validate_interview_quality(transcript_data: Dict[str, Any], min_duration_seconds: int = 900) -> Tuple[bool, str]:
    """
    Validate interview quality before evaluation.
    Returns (is_valid, reason_if_invalid)
    
    Args:
        transcript_data: Interview transcript JSON
        min_duration_seconds: Minimum required duration (default 15 min = 900 sec)
    """
    # Check duration
    duration_info = extract_interview_duration(transcript_data)
    duration_seconds = duration_info.get("duration_seconds") or 0
    
    if duration_seconds < min_duration_seconds:
        minutes = duration_seconds // 60
        return False, f"Interview too short: {minutes}m {duration_seconds % 60}s (minimum {min_duration_seconds // 60}m required)"
    
    # Check meaningful answer count
    meaningful_answers = count_meaningful_answers(transcript_data)
    if meaningful_answers < 3:
        utterances = transcript_data.get("transcription", [])
        total_turns = len([u for u in utterances if isinstance(u, dict)]) // 2 if isinstance(utterances, list) else 0
        return False, f"Insufficient meaningful responses: {meaningful_answers} substantive answers in {total_turns} total turns"
    
    return True, ""


# ========== EVALUATION FUNCTIONS ==========
def get_grouped_technical_skills(jd_metadata: Dict[str, Any], recruiter_comments: Optional[str] = None) -> list:
    """
    Extract and group technical skills from JD metadata.
    Returns a list of up to 10 umbrella categories.
    """
    required = jd_metadata.get("required_skills", [])
    nice_to_have = jd_metadata.get("nice_to_have_skills", [])
    
    # Combine all potential technical content
    skills_list = required + nice_to_have
    
    prompt = f"""You are an expert technical recruiter.
JOB TITLE: {jd_metadata.get('title', 'N/A')}
REQUIRED SKILLS: {', '.join(required)}
NICE TO HAVE: {', '.join(nice_to_have)}
RECRUITER'S COMMENTS: {recruiter_comments or 'None'}

TASK:
1. Identify all technical skills mentioned in the JD and the Recruiter's comments.
2. Group these skills into high-level, distinct technical evaluation categories (umbrella terms like 'Data Engineering', 'Frontend Development', 'Cloud Infrastructure', etc.).
3. If there are more than 10 grouped categories, use your reasoning to select the top 10 most critical ones. You MUST include at least one category that captures the recruiter's specific comments/priorities.
4. If there are 10 or fewer categories, include all of them.

Return ONLY a JSON object with a 'categories' key containing the list of strings.
Example: {{"categories": ["Python Development", "AWS Setup", "Database Design"]}}
"""
    try:
        response = openai_client.chat.completions.create(
            model=AZURE_OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": "Return strictly valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500,
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content.strip()
        data = json.loads(content)
        return data.get("categories", [])
    except Exception as e:
        print(f"Error grouping JD skills: {e}")
        # Fallback: just return the first few required skills if grouping fails
        return (required[:10] if required else ["General Technical Proficiency"])


def evaluate_technical(resume_text: str, interview_transcript: str, job_description: str, candidate_name: str, quality_flag: str = "", ai_questions: str = "", evaluation_categories: list = None) -> list:
    quality_context = ""
    if quality_flag == "SHORT_INTERVIEW":
        quality_context = "\n⚠️ NOTE: This is a SHORT interview (< 15 minutes). Score conservatively. Cap most scores at 50-60 range unless candidate demonstrated exceptional expertise."
    elif quality_flag == "LOW_ENGAGEMENT":
        quality_context = "\n⚠️ NOTE: Candidate had LOW meaningful responses. Score conservatively in the 10-40 range unless exceptional expertise is demonstrated."
    
    # Use AI questions to identify skills discussed
    skills_hint = ""
    if ai_questions:
        skills_hint = f"\nQUESTIONS ASKED BY THE INTERVIEWER:\n{ai_questions}\n"
    
    prompt = f"""You are an expert technical interviewer evaluating candidate {candidate_name}.

JOB DESCRIPTION:
{job_description}

CANDIDATE RESUME:
{resume_text}

INTERVIEW TRANSCRIPT:
{interview_transcript}
{skills_hint}

TASK:
1. Analyze the technical questions asked by the interviewer throughout the entire interview.
"""

    if evaluation_categories:
        cat_list_text = "\n".join([f"   - {c}" for c in evaluation_categories])
        prompt += f"""2. Evaluate the candidate on the following {len(evaluation_categories)} technical categories, which have been derived from the Job Description and recruiter priorities:
{cat_list_text}
3. Evaluate the candidate on these categories based ONLY on what they demonstrated in the INTERVIEW TRANSCRIPT.
"""
    else:
        prompt += """2. Based on these questions, identify exactly 10 relevant technical evaluation categories (umbrella terms) using self-reasoning. 
   - These categories should represent the core competencies being tested (e.g., 'Cloud Infrastructure', 'Data Processing', 'Backend Architecture', etc.).
3. Evaluate the candidate on these 10 categories based ONLY on what they demonstrated in the INTERVIEW TRANSCRIPT.
"""

    cat_count_text = f"exactly {len(evaluation_categories)}" if evaluation_categories else "exactly 10"
    prompt += f"""
CRITICAL SCORING RULES — READ CAREFULLY AND APPLY STRICTLY:{quality_context}

1. FIRST: Assess interview transcript QUALITY:
    - Count answers with actual technical substance (not filler, "I don't know", single words, or evasion)
    - If FEWER than 4 SUBSTANTIAL technical answers → cap ALL scores at 12-30 range
    - If FEWER than 6 total meaningful responses → cap ALL scores at 20-40 range
    - If answers are vague, incomplete, or the candidate says "I don't know" for most questions → score 0-15
    - Short interviews or lack of depth → score conservatively (0-30 range)

2. TRANSCRIPT IS PRIMARY: Score ONLY what the candidate demonstrated in the interview
    - Resume is background context only — do NOT infer skills if transcript doesn't show them
    - Incomplete explanations = low scores (no credit for "we got 85% accuracy" without details)
    - "I don't have knowledge of this" → 0-10 points for that skill area

3. CONSISTENCY CHECK: If most answers are evasive, all scores should be consistently LOW (10-25 range)
    - Do not give high scores in one area if overall transcript shows weak engagement

Return ONLY valid JSON in this format:
{{
    "skills": [
        {{"name": "Category Name", "score": 0}},
        ... ({cat_count_text} categories)
    ]
}}
"""

    try:
        response = openai_client.chat.completions.create(
            model=AZURE_OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": "Return strictly valid JSON. Group individual skills into meaningful categories for evaluation."},
                {"role": "user", "content": prompt}
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"): content = content[4:]
        data = json.loads(content.strip())
        return data.get("skills", [])
    except Exception as e:
        print(f"Technical evaluation error: {e}")
        # Default fallback if LLM fails
        return [
            {"name": "Programming Languages", "score": 50}, 
            {"name": "System Design", "score": 50}, 
            {"name": "Database Knowledge", "score": 50}, 
            {"name": "Cloud Platforms", "score": 50}, 
            {"name": "DevOps Practices", "score": 50}, 
            {"name": "API Development", "score": 50}, 
            {"name": "Testing Methodologies", "score": 50}, 
            {"name": "Code Quality", "score": 50}, 
            {"name": "Problem Solving", "score": 50}, 
            {"name": "Architecture Understanding", "score": 50}
        ]


def evaluate_soft_skills(interview_transcript: str, candidate_name: str, quality_flag: str = "") -> list:
    quality_context = ""
    if quality_flag == "SHORT_INTERVIEW":
        quality_context = "\n⚠️ NOTE: This is a SHORT interview (< 15 minutes). Score conservatively. Cap most scores at 50-60 range unless exceptional soft skills demonstrated."
    elif quality_flag == "LOW_ENGAGEMENT":
        quality_context = "\n⚠️ NOTE: Candidate had LOW meaningful responses. Score conservatively in the 10-40 range unless exceptional soft skills demonstrated."
    
    prompt = f"""You are an expert in evaluating soft skills and communication. Evaluate candidate {candidate_name}.

INTERVIEW TRANSCRIPT:
{interview_transcript}

CRITICAL SCORING RULES — APPLY STRICTLY:{quality_context}
 
 1. ASSESS TRANSCRIPT QUALITY FIRST:
     - If candidate has fewer than 3 meaningful turns (not "yes", "ok", "I don't know") → cap ALL scores at 15-30
     - If candidate answers are consistently evasive or short → scores should be 10-30 range
     - If interview is unusually short or candidate disengages → score conservatively
     - If answers show weak communication → score below 50
 
 2. FOCUS ON WHAT YOU OBSERVE:
     - Score ONLY based on this interview's actual dialogue
     - Do NOT assume soft skills from a resume
     - Weak communication patterns (short replies, evasion) = low Communication score
     - Candidates saying "I don't have knowledge" or "I'm not sure" repeatedly = low Problem Solving and Leadership
 
 3. CONSISTENCY RULE:
     - If overall engagement is poor, ALL soft skill scores should be similarly LOW
     - Do not give high Communication score if candidate gives one-word answers

Evaluate on these 5 soft skills (score 0-100):
1. Communication (clarity, articulation, listening)
2. Problem Solving (approach, creativity, analytical thinking)
3. Leadership (initiative, influence, decision-making)
4. Team Collaboration (teamwork, cooperation, openness)
5. Adaptability (flexibility, learning ability, handling ambiguity)

Return ONLY valid JSON:
{{
    "skills": [
        {{"name": "Communication", "score": 0}},
        {{"name": "Problem Solving", "score": 0}},
        {{"name": "Leadership", "score": 0}},
        {{"name": "Team Collaboration", "score": 0}},
        {{"name": "Adaptability", "score": 0}}
    ]
}}
"""
    try:
        response = openai_client.chat.completions.create(
            model=AZURE_OPENAI_CHAT_DEPLOYMENT,
            messages=[
                {"role": "system", "content": "Return strictly valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            response_format={"type": "json_object"}
        )
        content = response.choices[0].message.content
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"): content = content[4:]
        data = json.loads(content.strip())
        return data.get("skills", [])
    except Exception as e:
        print(f"Soft skills evaluation error: {e}")
        return [{"name": "Communication", "score": 50}, {"name": "Problem Solving", "score": 50}, {"name": "Leadership", "score": 50}, {"name": "Team Collaboration", "score": 50}, {"name": "Adaptability", "score": 50}]

# ========== MAIN FUNCTION ==========
def evaluate_candidate(resume_id: str, transcript_json_path: str) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Evaluate candidate using transcription JSON file.
    
    Args:
        resume_id: ID of the candidate in Cosmos DB
        transcript_json_path: Path to JSON file containing interview transcript
    
    Returns:
        (success, evaluation_dict, error_message)
    """
    try:
        with open(transcript_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        transcription = data.get("transcript") or data.get("text") or data.get("content")
        
        if not transcription:
            transcription = json.dumps(data)
        
    except FileNotFoundError:
        return False, None, f"File not found: {transcript_json_path}"
    except json.JSONDecodeError as e:
        return False, None, f"Invalid JSON file: {e}"
    except Exception as e:
        return False, None, f"Error reading file: {e}"
    
    start_time = time.time()
    try:
        print(f"Fetching resume: {resume_id}")
        resume = get_resume(resume_id)
        if not resume:
            return False, None, f"Candidate record not found: {resume_id}"
        
        if resume.get("state") != "scheduled":
            return False, None, f"Resume is in '{resume.get('state')}' state, not scheduled for interview"

        jd_id = resume.get("jd_id")
        if not jd_id:
            return False, None, f"No JD associated with resume {resume_id}"
        
        jd = get_jd(jd_id)
        if not jd:
            return False, None, f"Job description not found: {jd_id}"
        
        if not transcription or not transcription.strip():
            return False, None, "Transcription text is empty"
        
        # ===== INTERVIEW QUALITY ASSESSMENT (for scoring context, not rejection) =====
        duration_info = extract_interview_duration(data)
        meaningful_answers = count_meaningful_answers(data)
        interview_quality_flag = ""
        
        if duration_info.get("duration_seconds", 0) < 900:  # < 15 minutes
            interview_quality_flag = "SHORT_INTERVIEW"
            print(f"[Quality Alert] Short interview: {duration_info.get('duration_formatted')} (< 15m)")
        
        if meaningful_answers < 4:
            interview_quality_flag = "LOW_ENGAGEMENT"
            print(f"[Quality Alert] Low meaningful responses: {meaningful_answers}/4")

        resume_text = transform_resume_to_text(resume)
        jd_text     = transform_jd_to_text(jd)

        metadata       = resume.get("metadata", {})
        extracted_data = metadata.get("extracted_data", {})
        contact        = extracted_data.get("contact", {})
        jd_metadata    = jd.get("metadata", {})

        candidate_name     = contact.get("name") or extracted_data.get("candidate_name", "Unknown")
        candidate_email    = contact.get("email")
        candidate_location = contact.get("location")
        job_role           = jd_metadata.get("title", "Unknown Role")

        print(f"Evaluating {candidate_name} for {job_role}")
        
        # Extract all AI questions to identify the technical skill categories being tested
        ai_questions = []
        utterances = data.get("transcription", [])
        if isinstance(utterances, list):
            for u in utterances:
                if isinstance(u, dict) and u.get("speaker") == "AI":
                    ai_questions.append(u.get("text", ""))
        ai_questions_text = "\n".join(ai_questions)

        # Extract technical evaluation categories from JD metadata and recruiter comments
        recruiter_comments = jd.get("recruiter_comments")
        evaluation_categories = get_grouped_technical_skills(jd_metadata, recruiter_comments)
        print(f"Grouped skills from JD: {evaluation_categories}")

        # Pass quality flag, AI questions, and JD-based categories to technical evaluation
        technical_result = evaluate_technical(resume_text, transcription, jd_text, candidate_name, interview_quality_flag, ai_questions_text, evaluation_categories)
        soft_skills_result = evaluate_soft_skills(transcription, candidate_name, interview_quality_flag)
        
        tech_avg = sum(s["score"] for s in technical_result) / len(technical_result) if technical_result else 0
        soft_avg = sum(s["score"] for s in soft_skills_result) / len(soft_skills_result) if soft_skills_result else 0
        overall_score = (tech_avg * 0.6 + soft_avg * 0.4)
        
        # Determine rating and specific recommendation text based on score
        rating, _ = _recommendation(overall_score)
        
        # Generate qualitative strengths/weaknesses for the recommendation prompt
        tech_strengths = [s["name"] for s in technical_result if s["score"] >= 80]
        tech_weaknesses = [s["name"] for s in technical_result if s["score"] < 40]
        soft_strengths = [s["name"] for s in soft_skills_result if s["score"] >= 80]
        soft_weaknesses = [s["name"] for s in soft_skills_result if s["score"] < 40]

        # Generate AI-based Hiring Recommendation
        rec_prompt = f"""You are an expert hiring manager providing a final decision justification for {candidate_name} for the {job_role} role.

DECISION: {rating}

TECHNICAL STRENGTHS: {', '.join(tech_strengths) if tech_strengths else 'No significant strengths'}
TECHNICAL WEAKNESSES: {', '.join(tech_weaknesses) if tech_weaknesses else 'No significant weaknesses'}
SOFT SKILLS STRENGTHS: {', '.join(soft_strengths) if soft_strengths else 'No significant strengths'}
SOFT SKILLS WEAKNESSES: {', '.join(soft_weaknesses) if soft_weaknesses else 'No significant weaknesses'}

INTERVIEW HIGHLIGHTS:
{transcription}

TASK: Write a 2-3 sentence hiring recommendation paragraph.
RULES:
1. DO NOT mention any numeric scores or percentages.
2. Provide specific insights based on the strengths and weaknesses above.
3. Explain WHY the candidate is or is not a fit based on their interview responses and overall engagement.
4. The tone must match the decision: {rating}.

Return ONLY the justification text, no extra formatting."""

        try:
            rec_response = openai_client.chat.completions.create(
                model=AZURE_OPENAI_CHAT_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": "You are a professional hiring manager."},
                    {"role": "user", "content": rec_prompt}
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS
            )
            hiring_recommendation = rec_response.choices[0].message.content.strip()
        except Exception:
            # Fallback if AI call fails
            if overall_score >= 85:
                hiring_recommendation = f"Candidate {candidate_name} is highly recommended for the {job_role} position, demonstrating exceptional technical mastery and professional soft skills throughout the interview."
            elif overall_score >= 70:
                hiring_recommendation = f"Candidate {candidate_name} is recommended for hire, showing solid competency and professional alignment with the requirements of the {job_role} role."
            elif overall_score >= 45:
                hiring_recommendation = f"Candidate {candidate_name} should be considered with reservations. While showing potential, specific technical or soft skill gaps were identified that may require additional support."
            else:
                hiring_recommendation = f"Candidate {candidate_name} is not recommended for the {job_role} position at this time as their performance did not meet the expected proficiency levels for the role."
        
        top_technical = sorted(technical_result, key=lambda x: x["score"], reverse=True)[:3]
        top_technical_text = ', '.join([f'{s["name"]} ({s["score"]})' for s in top_technical])
        soft_skills_text = ', '.join([f'{s["name"]} ({s["score"]})' for s in soft_skills_result])
        
        prompt = f"""You are an expert technical recruiter creating an executive summary of interview results.

CANDIDATE: {candidate_name}
POSITION: {job_role}
AVERAGE TECHNICAL SCORE: {tech_avg:.1f}/100
AVERAGE SOFT SKILLS SCORE: {soft_avg:.1f}/100

TOP TECHNICAL SKILLS:
{top_technical_text}

SOFT SKILLS:
{soft_skills_text}

INTERVIEW HIGHLIGHTS:
{transcription}

Write a compelling 2-3 sentence summary (max 200 words) about the candidate's interview performance for the {job_role} position. Be specific about strengths and fit for the role. Focus on hiring value and potential.

Return ONLY the summary text, no JSON or additional formatting."""
        
        try:
            response = openai_client.chat.completions.create(
                model=AZURE_OPENAI_CHAT_DEPLOYMENT,
                messages=[
                    {"role": "system", "content": "You are an expert technical recruiter."},
                    {"role": "user", "content": prompt}
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS
            )
            overall_result = response.choices[0].message.content.strip()
        except Exception:
            overall_result = f"Candidate {candidate_name} scored {tech_avg:.1f}% in technical skills and {soft_avg:.1f}% in soft skills. Overall recommendation is {rating} for the {job_role} position."
        
        strengths = [s for s in technical_result if s["score"] >= 70][:3]
        weaknesses = [s for s in technical_result if s["score"] < 40][:2]
        tech_lines = []
        if strengths:
            tech_lines.append(f"{candidate_name} demonstrated strong proficiency in {', '.join([s['name'] for s in strengths[:2]])}.")
        if weaknesses:
            tech_lines.append(f"Areas for improvement include {', '.join([s['name'] for s in weaknesses])} where deeper understanding would be beneficial.")
        if not strengths and not weaknesses:
            tech_lines.append(f"{candidate_name} showed basic technical competence for the {job_role} position.")
        tech_lines.append(f"Overall technical assessment: {candidate_name} has the potential to succeed with appropriate technical guidance.")
        technical_qualitative = " ".join(tech_lines)
        
        comm_score = next((s["score"] for s in soft_skills_result if s["name"] == "Communication"), 0)
        problem_score = next((s["score"] for s in soft_skills_result if s["name"] == "Problem Solving"), 0)
        soft_lines = []
        if comm_score >= 70:
            soft_lines.append(f"{candidate_name} communicates clearly and articulates thoughts effectively.")
        else:
            soft_lines.append(f"{candidate_name}'s communication could be more structured and concise.")
        if problem_score >= 70:
            soft_lines.append(f"Problem-solving approach shows analytical thinking and logical reasoning.")
        else:
            soft_lines.append(f"Problem-solving skills would benefit from more structured approach.")
        if not soft_lines:
            soft_lines.append(f"{candidate_name} displays adequate interpersonal skills for the {job_role} role.")
        soft_skills_qualitative = " ".join(soft_lines)
        
        # Extract interview duration and Q&A by phase from transcript
        duration_info = extract_interview_duration(data)
        qa_by_phase = extract_qa_by_phase(data)
        
        tech_improvements = [s["name"] for s in technical_result if 40 <= s["score"] < 60]
        soft_improvements = [s["name"] for s in soft_skills_result if 40 <= s["score"] < 60]
        
        # Generate negative observations based on low scores or missing answers
        negative_obs = []
        if tech_avg < 40:
            negative_obs.append(f"Technical competency is below expected level for {job_role} position")
        if soft_avg < 40:
            negative_obs.append("Soft skills assessment indicates challenges in communication or problem-solving")
        if any(s["score"] < 30 for s in technical_result):
            weak_skills = [s["name"] for s in technical_result if s["score"] < 30]
            negative_obs.append(f"Significant gaps in {', '.join(weak_skills[:2])}")
        
        evaluation_dict = {
            "id": resume_id,
            "jd_id": jd_id,
            "evaluation": {
                "candidateData": {
                    "id": resume_id, "name": candidate_name, "email": candidate_email,
                    "location": candidate_location, "jobRole": job_role, "resumeUrl": None,
                },
                "overallResult": overall_result,
                "hiringRecommendation": hiring_recommendation,
                "technicalSkills": technical_result,
                "softSkills": soft_skills_result,
                "technicalQualitative": technical_qualitative,
                "softSkillsQualitative": soft_skills_qualitative,
                "technical_average": round(tech_avg, 2),
                "soft_skills_average": round(soft_avg, 2),
                "overall_score": round(overall_score, 2),
                "evaluation_date": datetime.now().isoformat(),
                "interview_duration_formatted": duration_info.get("duration_formatted", "N/A"),
                "interview_start": duration_info.get("interview_start"),
                "interview_end": duration_info.get("interview_end"),
                "technicalStrengths": tech_strengths,
                "technicalWeaknesses": tech_weaknesses,
                "technicalImprovements": tech_improvements,
                "softStrengths": soft_strengths,
                "softWeaknesses": soft_weaknesses,
                "softImprovements": soft_improvements,
                "negativeObservations": negative_obs,
                "qa_by_phase": qa_by_phase,
                "pdf_blob_url": None
            }
        }
          
        # Generate PDF report and upload to Blob Storage
        try:
            ts           = datetime.now().strftime('%Y%m%d_%H%M%S')
            pdf_filename = f"{resume_id}_evaluation_{ts}.pdf"
            pdf_path     = os.path.join(PDF_OUTPUT_DIR, pdf_filename)
            build_pdf(evaluation_dict["evaluation"], pdf_path)
            print(f"PDF report generated: {pdf_path}")
            # Upload PDF to the same candidate folder as transcription/recording
            if blob_storage_configured():
                blob_info = upload_pdf(resume_id, pdf_path, timestamp=ts)
                evaluation_dict["evaluation"]["pdf_blob_url"] = blob_info["blob_url"]
                evaluation_dict["evaluation"]["pdf_blob_name"] = blob_info["blob_name"]
                print(f"[Eval] PDF uploaded to Blob -> {blob_info['blob_url']}")
                save_evaluation(resume_id, evaluation_dict)  # ← persist the URL back to Cosmos
                os.remove(pdf_path)
            else:
                print("[Eval] Blob storage not configured — PDF saved locally only")
        except Exception as e:
            print(f"Warning: PDF generation/upload failed: {e}")
        
        if not save_evaluation(resume_id, evaluation_dict):
            return False, None, "Failed to save evaluation to evaluations container"
        
        if not update_resume_state(resume_id, "completed", resume.get("interview_link")):
            return False, None, "Failed to update resume state"
        
        print(f"Evaluation completed in {time.time() - start_time:.2f}s")
        print(f"Scores - Technical: {tech_avg:.1f}%, Soft Skills: {soft_avg:.1f}%, Overall: {overall_score:.1f}%")
        
        return True, evaluation_dict, None

    except Exception as e:
        print(f"Evaluation failed: {e}")
        import traceback; traceback.print_exc()
        return False, None, f"Evaluation failed: {str(e)}"