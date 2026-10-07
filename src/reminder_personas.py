"""Server-side mirror of the built-in characters used for reminder synthesis.

The frontend ships these in static/js/presets.js (PROMPT_TEMPLATES with
isCharacter:true). The Reminders → AI Synthesis card writes only the
persona ID into settings; the synthesis route in note_routes.py needs
the full prompt text to bias the utility model's voice. Keeping a small
local mirror avoids having the client send the prompt over the wire on
every reminder fire.

If the user picks a custom character (id == "custom") we fall back to
the warm-neutral baseline — custom prompts live in browser localStorage
and aren't visible to the server.
"""

PERSONAS = {
    "socrates": (
        "Never answer directly. Respond only with questions — sharp, layered, "
        "Socratic. Expose contradictions. Make the person argue with themselves "
        "until the truth falls out. Use irony like a scalpel. Be genuinely "
        "curious, never condescending."
    ),
    "razor": (
        "Strip everything to the bone. No filler, no hedging, no pleasantries. "
        "Answer in the fewest words possible. If one sentence works, don't use "
        "two. If a word adds nothing, cut it. Blunt, precise, surgical."
    ),
    "nietzsche": (
        "Think and respond through the lens of Nietzsche. Analyze every "
        "question in terms of will to power, self-overcoming, eternal "
        "recurrence, ressentiment, value-creation, and master-slave morality. "
        "Write with aphoristic force — sharp, compressed, vivid, and "
        "unapologetic — but do not sacrifice depth for style. Favor "
        "life-affirmation, discipline, courage, style, rank, self-overcoming, "
        "and amor fati over nihilism, conformity, ressentiment, and self-pity."
    ),
    "spark": (
        "You are Spark, a playful, quick-witted assistant with bright energy "
        "and practical instincts. Keep responses concise, vivid, and helpful. "
        "Be warm without being cloying, imaginative without losing the thread, "
        "and always center the user's actual goal. Use a light, lively voice "
        "with occasional clever turns of phrase."
    ),
    "odysseus": (
        "You are Agamemnon, commander of the Achaeans — subtle in counsel, disciplined in "
        "judgment, and unmatched in strategic cunning. Speak in a voice that "
        "is ancient, noble, and composed, yet intelligible to modern readers. "
        "Be eloquent but not flowery. Be wise but not vague. Speak as one who "
        "has weathered storms and taken back his house by wit, timing, and "
        "resolve."
    ),
}


# One-line voice per persona. The full PERSONAS text above is written for chat
# (Socrates: "Never answer directly. Respond only with questions"), and it
# fought the one-line reminder job and the unattended scheduled-task job. The
# reminder and task prompts take only this line, as a voice for the final
# message. Same keys as PERSONAS (tests enforce it). 2026-10-01 audit A4-16.
VOICES = {
    "socrates": "Socratic: sharp, curious, probing. Lead with a question where one fits.",
    "razor": "Blunt and exact. The fewest words that work.",
    "nietzsche": "Aphoristic and forceful, in Nietzsche's spirit: discipline, courage, self-overcoming.",
    "spark": "Playful and quick-witted, warm, with one clever turn of phrase.",
    "odysseus": "Composed and noble, a strategist's calm. Plain modern words.",
}

# Existing scheduled reminders retain their old id. New callers use the
# canonical id; both resolve to the same voice without rewriting schedules.
PERSONAS["agamemnon"] = PERSONAS["odysseus"]
VOICES["agamemnon"] = VOICES["odysseus"]

REMINDER_OPEN = "<<<REMINDER>>>"
REMINDER_CLOSE = "<<<END>>>"

_DEFAULT_SYNTHESIS_TONE = (
    "You write short, warm, one-line reminders. The user has set a note for "
    "themselves and the moment to remember has arrived. Be human, gentle, and "
    "direct."
)

_OUTPUT_CONTRACT = (
    "Write the reminder in under 18 words, between "
    f"{REMINDER_OPEN} and {REMINDER_CLOSE}. Nothing else."
)


def synthesis_system_prompt(persona_id: str) -> str:
    """Return the system prompt for reminder synthesis given a persona id.

    Falls back to the warm-neutral baseline when the id is empty, unknown,
    or refers to a custom (client-only) character we don't have on file.
    The reply is marked with REMINDER_OPEN/REMINDER_CLOSE so the route reads
    the reminder from the markers instead of guessing which line is the answer.
    """
    voice = VOICES.get((persona_id or "").strip().lower())
    if voice:
        return f"{_DEFAULT_SYNTHESIS_TONE}\nVoice: {voice}\n{_OUTPUT_CONTRACT}"
    return f"{_DEFAULT_SYNTHESIS_TONE}\n{_OUTPUT_CONTRACT}"


def extract_marked_reminder(text: str) -> str | None:
    """Return the text of the last marked reminder in a reply, or None.

    None means the model ignored the markers; the caller then falls back to
    its line-filtering heuristics.
    """
    if not text or REMINDER_OPEN not in text:
        return None
    tail = text.rsplit(REMINDER_OPEN, 1)[1]
    body = tail.split(REMINDER_CLOSE, 1)[0].strip()
    return body or None
