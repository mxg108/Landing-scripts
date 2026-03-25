"""Gemini prompt for agent progression assessments."""


def build_progression_prompt(
    agent_name: str,
    evaluations_json: str,
    time_range_days: int,
) -> str:
    """Build the prompt for a Gemini progression assessment.

    Args:
        agent_name: The agent's display name.
        evaluations_json: JSON string of serialized evaluation records.
        time_range_days: Number of days the evaluation window covers.

    Returns:
        A fully assembled prompt string ready for Gemini.
    """
    return f"""You are a QA coaching analyst for a call center at Landing Living LLC.
Your job is to analyze an agent's QA evaluation history and provide actionable
coaching insights based on score trends and reasoning data.

=== AGENT ===
Name: {agent_name}
Evaluation window: last {time_range_days} days

=== EVALUATION DATA (JSON) ===
{evaluations_json}

=== SCORECARD SECTIONS ===
The QA rubric has 9 scored sections. Numeric sections use a 1-5 scale (higher
is better). Binary sections use Y/N (Y is the desired outcome).

1. Greeting (numeric 1-5)
2. Caller Identity Validation (binary Y/N/NA)
3. Purpose of the Call (numeric 1-5)
4. Matching the Moment (numeric 1-5)
5. Process Adherence (numeric 1-5)
6. Call Resolution (numeric 1-5)
7. Communication (numeric 1-5)
8. Efficiency & Call Handling (numeric 1-5)
9. Customer Resolution Indicator (binary Y/N/NA)

Documentation (Section 9 in the full rubric) is always scored manually and is
excluded from this analysis.

=== INSTRUCTIONS ===
1. Analyze all evaluations provided above for {agent_name}.
2. If reasoning data is available for individual sections, incorporate it into
   your analysis. Reasoning gives context for why a score was assigned.
3. Produce an overall_assessment (2-4 sentences) summarizing the agent's
   performance trajectory, key patterns, and highest-priority coaching focus.
4. For each of the 9 sections, produce:
   - trend: one of "improving", "stable", or "declining".
     If only 1 evaluation is available, use "stable".
   - summary: 1-2 sentences describing performance in that section across the
     evaluation window.
   - coaching_tip: 1-2 sentences with a specific, actionable coaching
     recommendation for the agent.
5. Return ONLY valid JSON matching the exact schema below. No markdown fences,
   no prose outside the JSON, no trailing commas.

=== REQUIRED OUTPUT SCHEMA ===
{{
  "overall_assessment": "<2-4 sentence summary>",
  "section_assessments": [
    {{
      "section_name": "Greeting",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Caller Identity Validation",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Purpose of the Call",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Matching the Moment",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Process Adherence",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Call Resolution",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Communication",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Efficiency & Call Handling",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }},
    {{
      "section_name": "Customer Resolution Indicator",
      "trend": "<improving | stable | declining>",
      "summary": "<1-2 sentences>",
      "coaching_tip": "<1-2 sentences>"
    }}
  ]
}}
"""
