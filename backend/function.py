"""function.py — Helper functions that build AI evaluation prompts (technical skills scoring) used by evaluator.py during post-interview assessment."""


def evaluate_technical(resume_text: str, interview_transcript: str, job_description: str, candidate_name: str) -> list:
    prompt = f"""You are an expert technical interviewer evaluating candidate {candidate_name}.

JOB DESCRIPTION:
{job_description[:2000]}

CANDIDATE RESUME:
{resume_text[:2000]}

INTERVIEW TRANSCRIPT:
{interview_transcript[:2000]}

CRITICAL SCORING RULES — READ BEFORE SCORING:
- First, assess the interview transcript quality:
    * Count answers that contain actual technical content (not filler, noise, or gibberish)
    * If fewer than 3 meaningful answers exist → cap ALL scores at 15
    * If answers are incoherent, off-topic, or nonsensical → score 0-15
    * If the candidate's identity could not be confirmed → set all scores to 0
- The TRANSCRIPT is the PRIMARY evidence. The resume is background context only.
- Do NOT infer or assume skills from the resume if the transcript does not support them.
- A candidate who is silent, confused, or incoherent scores LOW regardless of their resume.

Evaluate the candidate on these technical skills (score 0-100):
1. Programming Languages (proficiency in required languages)
2. System Design (architecture, scalability patterns)
3. Database Knowledge (SQL, NoSQL, optimization)
4. Cloud Platforms (AWS, Azure, GCP)
5. DevOps Practices (CI/CD, monitoring, automation)
6. API Development (REST, GraphQL, microservices)
7. Testing Methodologies (unit, integration, e2e)
8. Code Quality (clean code, patterns, best practices)
9. Problem Solving (algorithmic thinking, debugging)
10. Architecture Understanding (design patterns, trade-offs)

Return ONLY valid JSON with this format(example for 2 skills):
{{
    "skills": [
        {{"name": "Programming Languages", "score": 85}},
        {{"name": "System Design", "score": 75}}
    ]
}}

Note: Strictly do not consider any resume information/content for scoring. Score solely based on the content and quality of the interview transcript answers.
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
        print(f"Technical evaluation error: {e}")
        return [{"name": "Programming Languages", "score": 50}, {"name": "System Design", "score": 50}, {"name": "Database Knowledge", "score": 50}, {"name": "Cloud Platforms", "score": 50}, {"name": "DevOps Practices", "score": 50}, {"name": "API Development", "score": 50}, {"name": "Testing Methodologies", "score": 50}, {"name": "Code Quality", "score": 50}, {"name": "Problem Solving", "score": 50}, {"name": "Architecture Understanding", "score": 50}]

def evaluate_soft_skills(interview_transcript: str, candidate_name: str) -> list:
    prompt = f"""You are an expert in evaluating soft skills and communication. Evaluate candidate {candidate_name}.

INTERVIEW TRANSCRIPT:
{interview_transcript[:3000]}

CRITICAL SCORING RULES:
 - Assess transcript quality first:
     * If answers are incoherent or nonsensical → score 0-15
     * If fewer than 2 meaningful responses exist → cap ALL scores at 15-20
     * If identity could not be verified → set all scores to 0
 - Score ONLY what you observe in the transcript.
 - Do NOT assume communication quality from a well-written resume.

Evaluate the candidate on these exact 5 soft skills (score 0-100):
1. Communication (clarity, articulation, listening)
2. Problem Solving (approach, creativity, analytical thinking)
3. Leadership (initiative, influence, decision-making)
4. Team Collaboration (teamwork, cooperation, openness)
5. Adaptability (flexibility, learning ability, handling ambiguity)

Return ONLY valid JSON with this format (for example):
{{
    "skills": [
        {{"name": "Communication", "score": 85}}, {{"name": "Problem Solving", "score": 78}},
        {{"name": "Leadership", "score": 72}}, {{"name": "Team Collaboration", "score": 80}},
        {{"name": "Adaptability", "score": 75}}
    ]
}}
Note: Strictly do not consider any resume information/content for scoring. Score solely based on the content and quality of the interview transcript answers.
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
