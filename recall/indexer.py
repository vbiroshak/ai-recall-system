"""Index per-session summary-unit files into a searchable SQLite database with embeddings.

Reads summaries/*.txt files produced by the session-summarizer agent.
Each file has a PHASE 1 inventory (ignored) and a PHASE 2 section of typed units. Each unit:

    ### Thread: <label>
    Type: finding | decision | open-thread | state
    Date: YYYY-MM-DD
    State: <...>
    Why: <...>      (optional)
    Open: <...>     (optional)
    Ref: Session N

Only PHASE 2 is parsed. One row per unit. The embedded/matchable text is the composite
label + state + why (Type, Date, and Ref are excluded from the vector).
Session id comes from the summary filename; date comes from each unit's Date field.
"""

import sys
import os
import re
import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SUMMARIES_DIR = SCRIPT_DIR / "summaries"
NPY_PATH = SCRIPT_DIR / "embeddings.npy"
DB_PATH = SCRIPT_DIR / "recall.db"

VALID_TYPES = {"finding", "decision", "open-thread", "state"}
FIELD_RE = re.compile(r"^(Type|Date|State|Why|Open|Ref)\s*:\s*(.*)$")


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def phase2_region(text):
    """Return the text from the PHASE 2 marker onward (the units)."""
    m = re.search(r"^.*PHASE\s*2.*$", text, re.MULTILINE)
    return text[m.end():] if m else text


def parse_units(summary_path, session_id):
    """Parse the PHASE 2 units of a summary file into unit dicts.
    Each unit carries its own Date field. Falls back to summary file mtime for old summaries."""
    with open(summary_path, "r", encoding="utf-8") as f:
        text = f.read()

    fallback_date = datetime.fromtimestamp(
        summary_path.stat().st_mtime, tz=timezone.utc
    )

    region = phase2_region(text)
    blocks = re.split(r"(?m)^###\s*Thread:\s*", region)[1:]

    units = []
    for position, block in enumerate(blocks):
        lines = block.splitlines()
        if not lines:
            continue
        label = lines[0].strip()
        fields = {"Type": "", "Date": "", "State": "", "Why": "", "Open": "", "Ref": ""}
        current = None
        for line in lines[1:]:
            fm = FIELD_RE.match(line)
            if fm:
                current = fm.group(1)
                fields[current] = fm.group(2).strip()
            elif current and line.strip():
                fields[current] = (fields[current] + " " + line.strip()).strip()
        if not label and not fields["State"]:
            continue

        utype = fields["Type"].strip().lower()
        if utype not in VALID_TYPES:
            utype = "state"

        if fields["Date"]:
            try:
                unit_date = datetime.fromisoformat(fields["Date"]).replace(tzinfo=timezone.utc)
            except ValueError:
                unit_date = fallback_date
        else:
            unit_date = fallback_date

        matchable = " ".join(p for p in (label, fields["State"], fields["Why"]) if p).strip()

        units.append({
            "session_id": session_id,
            "session_date": unit_date.isoformat(),
            "unit_position": position,
            "label": label,
            "type": utype,
            "state": fields["State"],
            "why": fields["Why"],
            "open": fields["Open"],
            "ref": fields["Ref"],
            "matchable_text": matchable,
        })
    return units


def init_db(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            file_path TEXT UNIQUE,
            content_hash TEXT,
            session_id TEXT,
            session_date TEXT
        );
        CREATE TABLE IF NOT EXISTS units (
            id INTEGER PRIMARY KEY,
            file_id INTEGER REFERENCES files(id),
            session_id TEXT,
            session_date TEXT,
            unit_position INTEGER,
            label TEXT,
            type TEXT,
            state TEXT,
            why TEXT,
            open TEXT,
            ref TEXT,
            matchable_text TEXT,
            embedding BLOB
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(
            matchable_text,
            content=units,
            content_rowid=id
        );
        CREATE TRIGGER IF NOT EXISTS units_ai AFTER INSERT ON units BEGIN
            INSERT INTO units_fts(rowid, matchable_text) VALUES (new.id, new.matchable_text);
        END;
        CREATE TRIGGER IF NOT EXISTS units_ad AFTER DELETE ON units BEGIN
            INSERT INTO units_fts(units_fts, rowid, matchable_text) VALUES('delete', old.id, old.matchable_text);
        END;
        CREATE TRIGGER IF NOT EXISTS units_au AFTER UPDATE ON units BEGIN
            INSERT INTO units_fts(units_fts, rowid, matchable_text) VALUES('delete', old.id, old.matchable_text);
            INSERT INTO units_fts(rowid, matchable_text) VALUES (new.id, new.matchable_text);
        END;
    """)
    return conn


def check_model_consistency(conn):
    """Reset the index if the embedding model changed."""
    from embed import MODEL_NAME, EMBEDDING_DIM
    cur = conn.execute("SELECT value FROM metadata WHERE key='model_name'")
    row = cur.fetchone()
    if row is None:
        conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('model_name', ?)", (MODEL_NAME,))
        conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('embedding_dim', ?)", (str(EMBEDDING_DIM),))
        conn.commit()
        return
    if row[0] != MODEL_NAME:
        print(f"Model mismatch: db has {row[0]}, current model is {MODEL_NAME}. Full re-index.")
        conn.execute("DELETE FROM units")
        conn.execute("DELETE FROM files")
        conn.execute("INSERT INTO units_fts(units_fts) VALUES('rebuild')")
        conn.execute("UPDATE metadata SET value=? WHERE key='model_name'", (MODEL_NAME,))
        conn.execute("UPDATE metadata SET value=? WHERE key='embedding_dim'", (str(EMBEDDING_DIM),))
        conn.commit()


def rebuild_npy(conn, npy_path):
    """Rebuild the contiguous embeddings.npy from BLOB data in the units table."""
    import numpy as np
    cur = conn.execute("SELECT embedding FROM units ORDER BY id")
    blobs = [row[0] for row in cur if row[0] is not None]
    if not blobs:
        if npy_path.exists():
            os.remove(npy_path)
        return
    from embed import EMBEDDING_DIM
    raw = b"".join(blobs)
    expected_per_unit = EMBEDDING_DIM * 4
    if len(raw) % expected_per_unit != 0:
        raise ValueError(f"Embedding data is corrupted: {len(raw)} bytes is not divisible by {expected_per_unit} (dim={EMBEDDING_DIM}). Re-index to fix.")
    matrix = np.frombuffer(raw, dtype=np.float32).reshape(-1, EMBEDDING_DIM)
    # Write to a temp file and rename so a concurrent reader never sees a partial file.
    tmp_path = str(npy_path) + ".tmp"
    with open(tmp_path, "wb") as f:
        np.save(f, matrix)
    os.replace(tmp_path, str(npy_path))
    print(f"Saved embeddings.npy: {matrix.shape}")


def main():
    incremental = "--incremental" in sys.argv

    if not SUMMARIES_DIR.exists():
        SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    conn = init_db(DB_PATH)

    # Model check runs before change detection: a model change wipes the index,
    # which empties `existing` below so every summary re-embeds with the new model.
    check_model_consistency(conn)

    # Prune entries whose summary file no longer exists (deleted, or the project moved).
    pruned = 0
    for file_id, file_path in conn.execute("SELECT id, file_path FROM files").fetchall():
        if not os.path.exists(file_path):
            conn.execute("DELETE FROM units WHERE file_id=?", (file_id,))
            conn.execute("DELETE FROM files WHERE id=?", (file_id,))
            pruned += 1
    if pruned:
        print(f"Pruned {pruned} summary file(s) no longer on disk.")

    summary_files = sorted(SUMMARIES_DIR.glob("*.txt"))

    existing = {}
    for row in conn.execute("SELECT file_path, content_hash FROM files"):
        existing[row[0]] = row[1]

    to_index = []
    for sp in summary_files:
        path_str = str(sp)
        current_hash = file_hash(sp)
        if existing.get(path_str) == current_hash:
            continue
        to_index.append((sp, current_hash))

    if not to_index:
        if pruned:
            rebuild_npy(conn, NPY_PATH)
            conn.commit()
            conn.close()
            return
        if incremental:
            return
        if not summary_files:
            print("No summary files found.")
            return
        print("All summaries already indexed. Nothing to do.")
        return

    print(f"Indexing {len(to_index)} summary file(s)...")

    from embed import embed
    import numpy as np

    total_units = 0
    for sp, content_hash in to_index:
        path_str = str(sp)
        session_id = sp.stem

        cur = conn.execute("SELECT id FROM files WHERE file_path=?", (path_str,))
        old = cur.fetchone()
        if old:
            conn.execute("DELETE FROM units WHERE file_id=?", (old[0],))
            conn.execute("DELETE FROM files WHERE id=?", (old[0],))

        units = parse_units(sp, session_id)
        if not units:
            print(f"  {sp.name}: no units parsed (skipped)")
            continue

        file_date = units[0]["session_date"] if units else datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO files (file_path, content_hash, session_id, session_date) VALUES (?, ?, ?, ?)",
            (path_str, content_hash, session_id, file_date)
        )
        file_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        vectors = embed([u["matchable_text"] for u in units])
        for i, u in enumerate(units):
            conn.execute(
                "INSERT INTO units (file_id, session_id, session_date, unit_position, label, type, state, why, open, ref, matchable_text, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (file_id, u["session_id"], u["session_date"], u["unit_position"], u["label"], u["type"], u["state"],
                 u["why"], u["open"], u["ref"], u["matchable_text"], vectors[i].tobytes())
            )
        total_units += len(units)
        print(f"  {sp.name}: {len(units)} units")

    rebuild_npy(conn, NPY_PATH)
    conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('indexed_at', ?)",
                 (datetime.now(timezone.utc).isoformat(),))
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    conn.close()
    print(f"Done. Indexed {total_units} new units. Total in database: {total}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if "--incremental" in sys.argv:
            sys.exit(0)
        raise

# Version 2.1
