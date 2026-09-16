"""The task builder's voice: the instructions each agent task kind leads with.

These are **data, not a runtime**. A task kind's prompt is stored here as text,
next to the kind it belongs to, so the seam
(:mod:`clear_record.service.agent`) and its runners stay runtime-agnostic:
nothing here names a vendor, a model or an endpoint. The seam's
:func:`~clear_record.service.agent_tasks.render_prompt` leads each request with
one of these instruction blocks, then appends the packaged context as JSON and
the machine-checkable output contract the answer must satisfy.

The instruction text is written for a language model of any provenance and
describes the conventional input sections (``transcript``, ``glossary``,
``context``) the task builders package. It is deliberately not translated: a
prompt is a machine surface, like the JSON API and the logs
(``docs/i18n.md``), not user-interface text.
"""

from __future__ import annotations

#: The output-language rule every task carries. A prompt is otherwise
#: language-neutral, so a Chinese meeting came back with English minutes; the
#: model must answer in the language of its inputs, never its own default.
_OUTPUT_LANGUAGE = (
    "Write every field of your answer in the dominant language of the inputs "
    "(the `transcript` is the meeting's own language); never translate the "
    "meeting into another language. Verbatim quotations, and the spellings of "
    "names and terms, stay exactly as they appear.\n"
)

#: The glossary-collection task: a transcript plus the current glossary → a list
#: of candidate terms with evidence. The model is told to prefer a few
#: well-evidenced candidates over speculation, and to treat the glossary as
#: already known except where the transcript shows it wrong.
GLOSSARY_COLLECTION_INSTRUCTIONS = (
    "You collect glossary candidates from one meeting's transcript for a "
    "project.\n"
    "\n"
    "Inputs: `transcript` is the meeting's transcript, one segment per line as "
    "`HH:MM:SS.mmm [speaker] text`. `glossary` is the project's current "
    "glossary, one term per line, and may be empty.\n"
    "\n"
    "Propose the names, jargon, acronyms, product names and domain terms that "
    "the transcript spells wrongly, hears inconsistently, or uses without ever "
    "defining. For each candidate give: `term` (the correct spelling), "
    "`reading` (how it is pronounced, or null), `aliases` (the wrong or "
    "inconsistent spellings seen in the transcript), `definition` (a concise "
    "meaning, or null), and `evidence` (the transcript line that shows the "
    "problem, quoted).\n"
    "\n"
    "Do not propose a term that is already in `glossary` unless the transcript "
    "shows it spelled or heard wrongly; in that case make the correct spelling "
    "the `term` and the wrong spelling an alias. Prefer a few well-evidenced "
    "candidates to speculation: every candidate must carry transcript evidence, "
    "and a term the transcript never shows is not a candidate.\n"
    "\n"
    "Answer only with the JSON object the output contract requires.\n"
    "\n" + _OUTPUT_LANGUAGE
)

#: The transcript-check task: a transcript, the glossary snapshot and the
#: meeting context → a corrected revision plus the change list that justifies
#: every edit. The model is told to correct only what is clearly wrong and to
#: preserve the rest of the transcript exactly.
TRANSCRIPT_CHECK_INSTRUCTIONS = (
    "You check one meeting's transcript against the project glossary.\n"
    "\n"
    "Inputs: `transcript` is the meeting's transcript, one segment per line as "
    "`HH:MM:SS.mmm [speaker] text`. `glossary` is the project's glossary "
    "snapshot, one term per line. `context` is the meeting and project "
    "metadata and any notes, and may be empty.\n"
    "\n"
    "Correct only what the glossary and the transcript's own context make "
    "clearly wrong: misheard names, acronyms and jargon. Preserve everything "
    "else - the timestamps, the speaker labels, the wording, the punctuation "
    "and the line structure - exactly as it is.\n"
    "\n"
    "Return `revision`: the complete corrected transcript text, the same lines "
    "in the same order with only the corrections applied (never a summary, "
    "never an excerpt). Return `changes`: one entry per correction, with "
    "`before` (the original wording), `after` (the corrected wording) and "
    "`reason` (why the change is justified).\n"
    "\n"
    "Never invent content, never drop or merge lines and never reorder "
    "segments. A change you cannot justify from the glossary or the "
    "transcript's own context is not a change.\n"
    "\n"
    "Answer only with the JSON object the output contract requires.\n"
    "\n" + _OUTPUT_LANGUAGE
)

#: The minutes task: the transcript, the glossary and the meeting/project
#: context → a Markdown minutes document with structured front-matter. The model
#: is told to stay faithful to the record and not to invent attendees,
#: decisions or actions.
MINUTES_INSTRUCTIONS = (
    "You write the minutes for one meeting in a project.\n"
    "\n"
    "Inputs: `transcript` is the meeting's transcript, one segment per line as "
    "`HH:MM:SS.mmm [speaker] text`; `glossary` is the project's glossary, one "
    "term per line, and tells you how names and terms are spelled; `context` is "
    "the meeting and project metadata and any notes, and may be empty.\n"
    "\n"
    "Return the structured front-matter and the document: `meeting` (the "
    "meeting title), `project` (the project name), `attendees` (the speakers "
    "and participants you can identify), `decisions` (what was decided), "
    "`actions` (what was assigned, naming the owner when the transcript names "
    "one) and `body` (the Markdown minutes document).\n"
    "\n"
    "Be faithful to the transcript: state only what was discussed, decided or "
    "assigned, and mark anything uncertain as uncertain rather than guessing. "
    "Do not invent attendees, decisions or actions; an empty list is correct "
    "when the record contains none. Spell names and terms as `glossary` does. "
    "`body` must be a complete Markdown document that stands on its own, under "
    "a heading, and must not repeat the structured fields as a data block.\n"
    "\n"
    "Answer only with the JSON object the output contract requires.\n"
    "\n" + _OUTPUT_LANGUAGE
)

#: The instructions one task kind leads with, keyed by kind — the one place a
#: kind and its prompt meet.
INSTRUCTIONS: dict[str, str] = {
    "glossary_collection": GLOSSARY_COLLECTION_INSTRUCTIONS,
    "transcript_check": TRANSCRIPT_CHECK_INSTRUCTIONS,
    "minutes": MINUTES_INSTRUCTIONS,
}


__all__ = [
    "GLOSSARY_COLLECTION_INSTRUCTIONS",
    "INSTRUCTIONS",
    "MINUTES_INSTRUCTIONS",
    "TRANSCRIPT_CHECK_INSTRUCTIONS",
]
