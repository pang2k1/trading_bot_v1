"""
playbook.py
───────────
Parse, validate, and edit the trading playbook (playbook.md).

Each lesson is a structured block in markdown with HTML-comment delimiters.
The reflection loop (Phase 2) proposes edits; this module validates caps
and applies them safely.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

PLAYBOOK_FILE = Path("playbook.md")
PLAYBOOK_EXAMPLE = Path("playbook.example.md")


@dataclass
class Lesson:
    id: str
    text: str
    created: str
    evidence: list[str] = field(default_factory=list)
    hits: int = 0


def get_playbook_path() -> Path:
    return PLAYBOOK_FILE if PLAYBOOK_FILE.exists() else PLAYBOOK_EXAMPLE


def count_words(text: str) -> int:
    return len(text.split())


def next_lesson_id(lessons: list[Lesson]) -> str:
    max_num = 0
    for lesson in lessons:
        m = re.match(r"L(\d+)", lesson.id)
        if m:
            max_num = max(max_num, int(m.group(1)))
    return f"L{max_num + 1:03d}"


def load_lessons(path: Path | None = None) -> list[Lesson]:
    """Parse playbook file into a list of Lesson objects."""
    path = path or get_playbook_path()
    if not path.exists():
        return []

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return []

    lessons = []
    pattern = re.compile(
        r"<!--\s*lesson:\s*(L\d+)\s*-->\s*\n"
        r"(.*?)\n"
        r"(?=<!--\s*(?:lesson:\s*L\d+|/lesson:\s*L\d+)\s*-->|$)",
        re.DOTALL,
    )
    for m in pattern.finditer(content):
        lesson_id = m.group(1)
        block = m.group(2).strip()
        lessons.append(_parse_block(lesson_id, block))
    return lessons


def save_lessons(lessons: list[Lesson], path: Path | None = None) -> None:
    """Write lessons to playbook.md atomically."""
    path = path or PLAYBOOK_FILE
    lines = ["# Trading Playbook\n"]
    for lesson in lessons:
        lines.append(format_lesson_block(lesson))
        lines.append("")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)


def format_lesson_block(lesson: Lesson) -> str:
    evidence_str = ", ".join(lesson.evidence) if lesson.evidence else "none"
    return (
        f"<!-- lesson: {lesson.id} -->\n"
        f"## {lesson.id}: {_title_from_text(lesson.text)}\n\n"
        f"**Rule:** {lesson.text}\n\n"
        f"**Created:** {lesson.created}  \n"
        f"**Evidence:** {evidence_str}  \n"
        f"**Hits:** {lesson.hits}\n"
        f"<!-- /lesson: {lesson.id} -->"
    )


def validate_edit(edit: dict, current_lessons: list[Lesson]) -> dict | None:
    """Validate a single proposed edit. Returns the edit or None if rejected."""
    op = edit.get("op")
    if op not in ("add", "update", "remove"):
        log.warning(f"Rejecting edit: invalid op '{op}'")
        return None

    evidence = edit.get("evidence", [])
    if not isinstance(evidence, list):
        evidence = []

    # Anti-overfit guard: add/update need >= 3 evidence trade IDs
    if op in ("add", "update") and len(evidence) < 3:
        log.warning(f"Rejecting edit ({op}): {len(evidence)} evidence items, minimum 3")
        return None

    # Word count cap
    text = edit.get("text", "")
    if op in ("add", "update") and text and count_words(text) > config.PLAYBOOK_MAX_WORDS:
        log.warning(
            f"Rejecting edit ({op}): {count_words(text)} words exceeds "
            f"{config.PLAYBOOK_MAX_WORDS}"
        )
        return None

    # Lesson cap for 'add'
    if op == "add" and len(current_lessons) >= config.PLAYBOOK_MAX_LESSONS:
        log.warning(
            f"Rejecting add: {len(current_lessons)} lessons at cap "
            f"{config.PLAYBOOK_MAX_LESSONS}"
        )
        return None

    # update/remove require existing lesson
    lesson_id = edit.get("lesson_id", "")
    if op in ("update", "remove"):
        existing_ids = {l.id for l in current_lessons}
        if lesson_id not in existing_ids:
            log.warning(f"Rejecting edit ({op}): lesson {lesson_id} not found")
            return None

    return edit


def apply_edits(
    lessons: list[Lesson],
    edits: list[dict],
    max_edits: int | None = None,
) -> tuple[list[Lesson], list[dict], list[dict]]:
    """Apply validated edits. Returns (new_lessons, applied, rejected)."""
    max_edits = max_edits if max_edits is not None else config.PLAYBOOK_MAX_EDITS_DAY
    applied: list[dict] = []
    rejected: list[dict] = []
    current = list(lessons)

    for edit in edits:
        validated = validate_edit(edit, current)
        if validated is None:
            rejected.append(edit)
            continue
        if len(applied) >= max_edits:
            rejected.append(edit)
            continue

        op = validated["op"]
        lid = validated.get("lesson_id", "")

        if op == "add":
            new_id = lid if lid else next_lesson_id(current)
            lesson = Lesson(
                id=new_id,
                text=validated.get("text", ""),
                created=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                evidence=[str(e) for e in validated.get("evidence", [])],
                hits=0,
            )
            current.append(lesson)
            applied.append(validated)

        elif op == "update":
            for i, l in enumerate(current):
                if l.id == lid:
                    current[i] = Lesson(
                        id=lid,
                        text=validated.get("text", l.text),
                        created=l.created,
                        evidence=[str(e) for e in validated.get("evidence", l.evidence)],
                        hits=l.hits,
                    )
                    break
            applied.append(validated)

        elif op == "remove":
            current = [l for l in current if l.id != lid]
            applied.append(validated)

    return current, applied, rejected


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_block(lesson_id: str, block: str) -> Lesson:
    text = ""
    created = ""
    evidence: list[str] = []
    hits = 0

    rule_m = re.search(r"\*\*Rule:\*\*\s*(.+?)(?:\n\n|\n\*\*)", block, re.DOTALL)
    if rule_m:
        text = rule_m.group(1).strip()

    created_m = re.search(r"\*\*Created:\*\*\s*(.+)", block)
    if created_m:
        created = created_m.group(1).strip()

    ev_m = re.search(r"\*\*Evidence:\*\*\s*(.+)", block)
    if ev_m:
        ev_raw = ev_m.group(1).strip()
        if ev_raw.lower() != "none" and ev_raw:
            evidence = [e.strip() for e in ev_raw.split(",")]
        else:
            evidence = []

    hits_m = re.search(r"\*\*Hits:\*\*\s*(\d+)", block)
    if hits_m:
        hits = int(hits_m.group(1))

    return Lesson(id=lesson_id, text=text, created=created, evidence=evidence, hits=hits)


def _title_from_text(text: str) -> str:
    first_sentence = text.split(".")[0]
    title = first_sentence[:80]
    return title.strip()
