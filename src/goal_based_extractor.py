# src/goal_based_extractor.py
"""
Goal-based page extraction prompt for deep research.

2026-10-01 (prompt audit A4-4): the first version, copied from Alibaba Tongyi
DeepResearch, asked for "rational", "evidence" and "summary". The pipeline
reads only "summary" (`DeepResearcher._format_findings` falls back to
"evidence" when the summary is empty), so "rational" was never read and
"evidence" cost three or more paragraphs of output per page. Meanwhile the
final report was told to quote numbers and statistics that the one-paragraph
summaries had dropped. The prompt now asks for the two fields the code uses and
tells the model to keep figures, so they reach the final report.
"""

EXTRACTOR_SYSTEM = """Goal: {goal}

Read the page content in the next message and extract what bears on the goal. Return a JSON object with:
- "relevant": true when the page contains information on the goal, otherwise false.
- "summary": 3 to 8 sentences with the facts, figures, dates, names, and prices that answer the goal, as the page states them. Empty when relevant is false.

Example:
{{"relevant": true, "summary": "<the page's facts, figures, dates, names, and prices that answer the goal>"}}
"""
