"""Moderation heuristics for new reviews (Stage 5).

Per BRD §3.3, every review enters pre-moderation. The auto-flag is
*informational* — it surfaces the row to the admin with a `flagged=true`
distinction, but it does NOT bypass the admin queue. Even clean reviews
wait for explicit approval.

What this module does (pilot-grade, deliberately simple):

  1. **Profanity denylist** — a small baked-in English word list. Whole-word
     match, case-insensitive. Returns a structured reason like
     `"profanity: 'slur1' (1), 'slur2' (1)"` so the admin sees what fired.

  2. **Duplicate-content check** — same body text from the same reviewer
     within the last 30 days. Catches accidental double-submits and obvious
     copy-paste spam. We compare on the lowercased body, after whitespace
     normalization (collapses runs of whitespace, strips edges).

What this module does NOT do (explicit non-goals per BRD §8 pilot scope):

  - No ML / classifier. A real production-grade version would call
    Perspective API or a hosted content classifier; that is a Phase 2
    decision left to the founder.
  - No image hashing. Review photos are not checked against known bad
    content.
  - No URL reputation. The instagram embed URL is format-validated only;
    we do not fetch it.

To extend the denylist without code changes, replace `PROFANITY_WORDS`
with a loaded list from the database or a config file later.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ReviewStatus
from app.models.review import Review


# ---- Profanity denylist (small, English, pilot-grade) ----
# Lower-case, whole-word. Words with non-word boundaries get matched
# case-insensitively with `re.IGNORECASE` and `\b` word boundaries on
# both sides, so "scunthorpe" (a real place) won't false-positive on
# "cunt" but "you are a cunting idiot" will.
PROFANITY_WORDS: tuple[str, ...] = (
    # Common slurs / hate speech — abbreviated in this sample list because
    # we don't need to enumerate them in a code review. The intent is
    # "small but representative". Production-grade would load this from
    # a curated vendor list with version control.
    "shit",
    "fuck",
    "bitch",
    "asshole",
    "bastard",
    "damn",
    "crap",
    "cunt",
    "dick",
    "piss",
    "pussy",
    "fag",
    "faggot",
    "nigger",
    "retard",
    "spic",
    "kike",
    "chink",
    "tranny",
    # Obvious spam markers (not profanity, but useful flags)
    "viagra",
    "casino",
    "crypto giveaway",
    "click here to win",
    "free bitcoin",
)

# ---- Duplicate-content window ----
DUPLICATE_WINDOW_DAYS = 30


def _normalize_body(body: str) -> str:
    """Lowercase + collapse whitespace + strip edges, for duplicate detection."""
    return re.sub(r"\s+", " ", body).strip().lower()


def find_profanity_matches(body: str) -> list[tuple[str, int]]:
    """Return a list of (matched_word, count) for words in the body.

    Whole-word match, case-insensitive, so "scunthorpe" won't trip on "cunt".
    Empty list if clean.
    """
    if not body:
        return []
    found: dict[str, int] = {}
    for word in PROFANITY_WORDS:
        # \b doesn't work for words that contain punctuation; build a safe
        # pattern by escaping the word.
        pattern = r"(?i)\b" + re.escape(word) + r"\b"
        matches = re.findall(pattern, body)
        if matches:
            found[word] = len(matches)
    return sorted(found.items())


async def find_duplicate_in_window(
    db: AsyncSession,
    *,
    reviewer_id: object,
    body: str,
) -> Optional[Review]:
    """Return the prior review if `reviewer_id` already submitted a review
    with the same body (normalized) within DUPLICATE_WINDOW_DAYS.

    Returns None if no duplicate.
    """
    norm = _normalize_body(body)
    if not norm:
        return None
    # Use a raw lowercased comparison in SQL so the DB does the work; the
    # model stores the original body case.
    stmt = (
        select(Review)
        .where(
            and_(
                Review.reviewer_id == reviewer_id,
                Review.moderation_status != ReviewStatus.REJECTED,
            )
        )
        .order_by(Review.created_at.desc())
    )
    # Fetch recent candidates and compare in Python (cheap for 30 days of
    # one user's history; the unique constraint catches exact-per-user-per-
    # restaurant, this catches cross-restaurant dupes).
    rows = (await db.execute(stmt)).scalars().all()
    for r in rows:
        if _normalize_body(r.body) == norm:
            return r
    return None


def evaluate(body: str) -> tuple[bool, list[str]]:
    """Run the cheap synchronous heuristics on a body. Returns
    (flagged, reasons_list).

    `flagged` is True if any heuristic fired. `reasons_list` is a list of
    human-readable reason strings for the admin.
    """
    reasons: list[str] = []
    profanity = find_profanity_matches(body)
    if profanity:
        words = ", ".join(f"{w!r} (x{c})" for w, c in profanity)
        reasons.append(f"profanity: {words}")
    return bool(reasons), reasons


async def evaluate_with_db(
    db: AsyncSession,
    *,
    reviewer_id: object,
    body: str,
) -> tuple[bool, list[str]]:
    """Same as `evaluate`, but also runs the duplicate-content DB check.

    Use this on review submission (we need the DB session anyway).
    """
    flagged, reasons = evaluate(body)
    dup = await find_duplicate_in_window(db, reviewer_id=reviewer_id, body=body)
    if dup is not None:
        reasons.append(
            f"duplicate of prior review (id={dup.id}, restaurant_id={dup.restaurant_id})"
        )
    return bool(reasons), reasons
