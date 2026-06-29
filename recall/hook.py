"""UserPromptSubmit hook: search recall index and inject relevant context.

Reads the user's prompt from stdin (JSON), embeds it, retrieves matching units
from recall.db via hybrid cosine + FTS5 + RRF with type-aware temporal decay,
applies a precision threshold, renders the top matches from structured columns,
and outputs the result as additionalContext for injection into the conversation.

Activated only when the prompt contains +recall. Fail-open: any error → empty output, exit 0.
"""

import json
import sys
import os
import re

# ── Tuning knobs ──
COSINE_FLOOR = 0.50
RRF_K = 60
MIN_FINAL_SCORE = 0.008
MAX_RESULTS = 3
MAX_TOTAL_CHARS = 3000

# Type-aware temporal decay half-lives (days).
# Findings decay slowly (durable facts); state/decisions decay faster (transient).
HALF_LIFE = {
    "finding": 120,
    "decision": 60,
    "open-thread": 45,
    "state": 30,
}
DEFAULT_HALF_LIFE = 45

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "recall.db")
NPY_PATH = os.path.join(SCRIPT_DIR, "embeddings.npy")

HEADER = "Related content auto-retrieved by Recall System"


def empty_output():
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "",
    }}))


def sanitize_fts_query(text):
    tokens = re.findall(r"\b[a-zA-Z0-9]{2,}\b", text)
    if not tokens:
        return None
    seen = set()
    unique = []
    for t in tokens:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            unique.append(f'"{t}"')
    return " OR ".join(unique[:20])


def render_unit(session_id, session_date, unit_type, label, state, why, openf):
    """Render one unit from its columns for injection. State always; Why/Open by type."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(session_date)
        date_str = dt.strftime("%B %d").replace(" 0", " ")
    except (ValueError, TypeError):
        date_str = "unknown date"

    lines = [f"[Session {session_id} · {date_str}]"]
    lines.append(f"Thread: {label}")
    lines.append(f"State: {state}")
    if why and unit_type in ("decision", "open-thread", "finding"):
        lines.append(f"Why: {why}")
    if openf and unit_type in ("open-thread",):
        lines.append(f"Open: {openf}")
    return "\n".join(lines)


def main():
    input_data = json.loads(sys.stdin.read())
    prompt = input_data.get("message", input_data.get("prompt", ""))
    if not prompt:
        empty_output()
        return

    TRIGGER_PATTERN = r'(?<!\S)\+recall(\??)(?!\S)'
    QUOTE_CHARS = set('`"\'\u00ab\u00bb\u2039\u203a\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f')

    match = re.search(TRIGGER_PATTERN, prompt, re.IGNORECASE)
    if not match:
        empty_output()
        return
    start, end = match.span()
    if (start > 0 and prompt[start-1] in QUOTE_CHARS) or (end < len(prompt) and prompt[end] in QUOTE_CHARS):
        empty_output()
        return

    show_recall = bool(match.group(1))
    prompt = re.sub(TRIGGER_PATTERN, "", prompt, flags=re.IGNORECASE).strip()

    # Skip near-empty prompts
    stripped = re.sub(r"<[^>]+>.*?</[^>]+>", "", prompt, flags=re.DOTALL).strip()
    if not stripped:
        empty_output()
        return

    if not os.path.exists(DB_PATH) or not os.path.exists(NPY_PATH):
        empty_output()
        return

    import sqlite3
    import numpy as np
    from math import exp, log
    from datetime import datetime, timezone

    sys.path.insert(0, SCRIPT_DIR)
    from embed import embed

    query_vec = embed([stripped])[0]

    matrix = np.load(NPY_PATH)
    scores = matrix @ query_vec

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    all_unit_ids = [r[0] for r in conn.execute("SELECT id FROM units ORDER BY id")]

    above_floor = np.where(scores >= COSINE_FLOOR)[0]
    if len(above_floor) == 0:
        semantic_ranked = []
    else:
        top_idx = above_floor[np.argsort(scores[above_floor])[-20:][::-1]]
        semantic_ranked = [(all_unit_ids[int(i)], float(scores[i])) for i in top_idx]

    fts_query = sanitize_fts_query(stripped)
    keyword_ranked = []
    if fts_query:
        try:
            rows = conn.execute("""
                SELECT u.id, bm25(units_fts) as score
                FROM units_fts
                JOIN units u ON u.id = units_fts.rowid
                WHERE units_fts MATCH ?
                ORDER BY score
                LIMIT 20
            """, (fts_query,)).fetchall()
            keyword_ranked = [(row[0], -row[1]) for row in rows]
        except sqlite3.OperationalError:
            pass

    rrf_scores = {}
    for rank, (uid, _) in enumerate(semantic_ranked):
        rrf_scores[uid] = rrf_scores.get(uid, 0) + 1.0 / (RRF_K + rank + 1)
    for rank, (uid, _) in enumerate(keyword_ranked):
        rrf_scores[uid] = rrf_scores.get(uid, 0) + 1.0 / (RRF_K + rank + 1)

    if not rrf_scores:
        conn.close()
        empty_output()
        return

    candidate_ids = list(rrf_scores.keys())
    placeholders = ",".join("?" * len(candidate_ids))
    rows = conn.execute(f"""
        SELECT id, session_id, session_date, type, label, state, why, open, ref
        FROM units WHERE id IN ({placeholders})
    """, candidate_ids).fetchall()

    unit_data = {}
    for r in rows:
        unit_data[r[0]] = {
            "session_id": r[1], "session_date": r[2], "type": r[3],
            "label": r[4], "state": r[5], "why": r[6], "open": r[7], "ref": r[8],
        }

    now = datetime.now(timezone.utc)
    final_scores = {}
    for uid, rrf_score in rrf_scores.items():
        u = unit_data.get(uid)
        if not u:
            continue
        try:
            dt = datetime.fromisoformat(u["session_date"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            dt = now
        age_days = max(0, (now - dt).total_seconds() / 86400)
        hl = HALF_LIFE.get(u["type"], DEFAULT_HALF_LIFE)
        decay = exp(-log(2) / hl * age_days)
        final_scores[uid] = rrf_score * decay

    ranked = sorted(final_scores.items(), key=lambda x: x[1], reverse=True)
    ranked = [(uid, s) for uid, s in ranked if s >= MIN_FINAL_SCORE]
    ranked = ranked[:MAX_RESULTS]

    if not ranked:
        conn.close()
        empty_output()
        return

    conn.close()

    # Strongest match LAST (nearest the user prompt in context)
    ranked.reverse()

    blocks = []
    total_chars = 0
    for uid, score in ranked:
        u = unit_data[uid]
        block = render_unit(
            u["session_id"], u["session_date"], u["type"],
            u["label"], u["state"], u["why"], u["open"],
        )
        if total_chars + len(block) > MAX_TOTAL_CHARS:
            break
        blocks.append(block)
        total_chars += len(block)

    if not blocks:
        empty_output()
        return

    context = f"=== {HEADER} ===\n\n" + "\n\n".join(blocks)
    if show_recall:
        context += "\n\nThe user used +recall? (with question mark) — show them what was recalled before responding. Present each result as a brief quote block with the session label and thread name."
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        empty_output()
        sys.exit(0)
