#!/bin/bash
# AI Recall System — one-time setup.
# Creates venv, installs dependencies, configures hooks, and imports historical transcripts.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$SCRIPT_DIR"

echo "=== AI Recall System Setup ==="
echo ""

# ── Add-on detection ──
# One determination drives both transcript directory and hook handling.
# Add-on = project already has the AI Project Architect archiver installed.
ADD_ON=false
if [ -f "$PROJECT_DIR/.claude/hooks/archive-transcripts.py" ]; then
    ADD_ON=true
fi

# ── 1. Config ──
if [ -f config.json ]; then
    echo "config.json already exists — skipping configuration."
    # Resolve through the archiver so setup and the hook always agree on the directory.
    TRANSCRIPTS_DIR=$(python3 - "$PROJECT_DIR" 2>/dev/null <<'PYEOF'
import sys, os, importlib.util
spec = importlib.util.spec_from_file_location("archive_transcripts", "archive-transcripts.py")
archiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archiver)
print(archiver.resolve_transcripts_dir(archiver.load_config("config.json"), sys.argv[1]))
PYEOF
    ) || TRANSCRIPTS_DIR="recall/sessions"
else
    if [ "$ADD_ON" = true ]; then
        # Add-on install: use the existing transcript directory.
        if [ -d "$PROJECT_DIR/Project/Sessions" ]; then
            TRANSCRIPTS_DIR="Project/Sessions"
        else
            # Fallback for a non-Architect add-on that has an archiver
            # but not the Project/Sessions/ structure.
            TRANSCRIPTS_DIR="Sessions"
        fi
        echo "Detected existing transcript directory: $TRANSCRIPTS_DIR/"
    else
        # Standalone install: transcripts live inside recall/.
        TRANSCRIPTS_DIR="recall/sessions"
        echo "Will create transcript directory: recall/sessions/"
    fi

    PROJECT_NAME=$(basename "$PROJECT_DIR")
    cat > config.json <<CONF
{
  "transcripts_dir": "$TRANSCRIPTS_DIR",
  "project_name": "$PROJECT_NAME"
}
CONF
    echo "Wrote config.json (project: $PROJECT_NAME)"
fi

# Ensure transcript directory exists
mkdir -p "$PROJECT_DIR/$TRANSCRIPTS_DIR"
mkdir -p "$SCRIPT_DIR/summaries"
echo ""

# ── 2. Dependencies ──
echo "Creating Python environment..."
python3 -m venv .venv

echo "Installing dependencies (this may take a minute)..."
if command -v uv &>/dev/null; then
    uv pip install --python .venv/bin/python3 -r requirements.txt 2>&1 | tail -3
else
    .venv/bin/pip install -r requirements.txt 2>&1 | tail -3
fi
echo "  Dependencies installed."

echo "Downloading embedding model (~80MB on first run, may take a few minutes)..."
.venv/bin/python3 -c "import sys; sys.path.insert(0, '.'); from embed import get_model; get_model()"
echo "  Model downloaded."
echo ""

# ── 3. Hooks ──
SETTINGS_FILE="$PROJECT_DIR/.claude/settings.json"
mkdir -p "$PROJECT_DIR/.claude/hooks"

# Copy hook scripts to .claude/hooks/.
# Uses the ADD_ON flag from the top-level determination.
if [ "$ADD_ON" = true ]; then
    echo "Add-on install — updating renderer only (existing archiver left in place)."
    cp "$SCRIPT_DIR/transcript_to_md.py" "$PROJECT_DIR/.claude/hooks/transcript_to_md.py"
else
    echo "Installing archive hook and renderer to .claude/hooks/..."
    cp "$SCRIPT_DIR/archive-transcripts.py" "$PROJECT_DIR/.claude/hooks/archive-transcripts.py"
    cp "$SCRIPT_DIR/transcript_to_md.py" "$PROJECT_DIR/.claude/hooks/transcript_to_md.py"
fi

echo "Configuring Claude Code hooks..."
.venv/bin/python3 - "$SETTINGS_FILE" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault("hooks", {})

# Archive hook (SessionStart) — bare python3, stdlib only
archive_cmd = 'python3 "${CLAUDE_PROJECT_DIR:-$PWD}/.claude/hooks/archive-transcripts.py"'
archive_hook = {"type": "command", "command": archive_cmd}

ss_hooks = hooks.setdefault("SessionStart", [])
archive_exists = any(
    "archive-transcripts" in h.get("command", "")
    for group in ss_hooks
    for h in group.get("hooks", [])
)
if not archive_exists:
    ss_hooks.append({"hooks": [archive_hook]})

# Recall hook (UserPromptSubmit) — venv python, needs fastembed
recall_cmd = ('"${CLAUDE_PROJECT_DIR:-$PWD}/recall/.venv/bin/python" '
              '"${CLAUDE_PROJECT_DIR:-$PWD}/recall/hook.py"')
recall_hook = {"type": "command", "command": recall_cmd}

ups_hooks = hooks.setdefault("UserPromptSubmit", [])
recall_exists = any(
    "recall" in h.get("command", "") and "hook.py" in h.get("command", "")
    for group in ups_hooks
    for h in group.get("hooks", [])
)
if not recall_exists:
    ups_hooks.append({"hooks": [recall_hook]})

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print("  Hooks configured in .claude/settings.json")
PYEOF
echo ""

# ── 4. Agent ──
AGENTS_DIR="$PROJECT_DIR/.claude/agents"
mkdir -p "$AGENTS_DIR"
if [ ! -f "$AGENTS_DIR/session-summarizer.md" ]; then
    AGENT_SOURCE="$SCRIPT_DIR/agents/session-summarizer.md"
    if [ -f "$AGENT_SOURCE" ]; then
        cp "$AGENT_SOURCE" "$AGENTS_DIR/session-summarizer.md"
        echo "Copied session-summarizer agent to .claude/agents/"
    else
        echo "Warning: agents/session-summarizer.md not found. Copy it manually to .claude/agents/"
    fi
else
    echo "session-summarizer.md already exists in .claude/agents/ — skipping."
fi
echo ""

# ── 5. Historical transcripts ──
DEST="$PROJECT_DIR/$TRANSCRIPTS_DIR"
EXISTING_COUNT=$(find "$DEST" -name "*.jsonl" 2>/dev/null | wc -l | tr -d ' ')
if [ "$EXISTING_COUNT" -eq 0 ]; then
    echo "Scanning for existing Claude Code sessions..."
    SLUG=$(echo "$PROJECT_DIR" | sed 's/[^A-Za-z0-9]/-/g')
    SRC_DIR="$HOME/.claude/projects/$SLUG"
    if [ -d "$SRC_DIR" ]; then
        FOUND=$(find "$SRC_DIR" -name "*.jsonl" | wc -l | tr -d ' ')
        echo "Found $FOUND transcript(s). Copying and rendering..."
        .venv/bin/python3 - "$SRC_DIR" "$DEST" "$SCRIPT_DIR" "$PROJECT_DIR" <<'PYEOF'
import sys, os, shutil, glob, importlib.util
src_dir, dest, script_dir, project_dir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

sys.path.insert(0, script_dir)
import transcript_to_md

spec = importlib.util.spec_from_file_location("archive_transcripts", os.path.join(script_dir, "archive-transcripts.py"))
archiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archiver)

config = archiver.load_config(os.path.join(project_dir, "recall", "config.json"))
project_name, project_name_tier = archiver.resolve_project_name(config, project_dir)

all_jsonl = sorted(glob.glob(os.path.join(src_dir, "*.jsonl")))
newest = max(all_jsonl, key=os.path.getmtime) if all_jsonl else None

for src in all_jsonl:
    if src == newest:
        # The most recent transcript may be the active session — skip it.
        # It is archived automatically at the next session start.
        print(f"  skipped (most recent, may be an active session): {os.path.basename(src)}")
        continue
    base = os.path.basename(src)
    stem = archiver.resolve_filename(src, project_name, project_name_tier)
    if stem:
        dst_name = stem + ".jsonl"
        md_name = stem + ".md"
    else:
        dst_name = base
        md_name = base[:-6] + ".md"
    dst = os.path.join(dest, dst_name)
    if os.path.exists(dst):
        src_sid = archiver.transcript_session_id(src)
        dst_sid = archiver.transcript_session_id(dst)
        if src_sid and dst_sid and src_sid != dst_sid:
            print(f"  NAMING CONFLICT (not copied — a different session already has this name): {dst_name}")
            continue
        if os.path.getsize(dst) >= os.path.getsize(src):
            continue
    shutil.copy2(src, dst)
    md_path = os.path.join(dest, md_name)
    try:
        transcript_to_md.render(dst, md_path)
    except Exception:
        pass
    print(f"  {dst_name}")
PYEOF
        echo "Done. Transcripts are ready for summarization."
    else
        echo "No existing sessions found for this project."
    fi
fi
echo ""

# ── 5b. Re-render transcripts ──
# Ensures all .md files have timestamps (required for per-unit dates in the index).
# Renders from the canonical .jsonl — safe to re-run; overwrites stale dateless renders.
echo "Ensuring all transcripts have timestamps..."
.venv/bin/python3 - "$PROJECT_DIR/$TRANSCRIPTS_DIR" "$SCRIPT_DIR" <<'PYEOF'
import sys, os, glob
dest, script_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, script_dir)
import transcript_to_md

count = 0
for jp in sorted(glob.glob(os.path.join(dest, "*.jsonl"))):
    mp = jp[:-6] + ".md"
    try:
        transcript_to_md.render(jp, mp)
        count += 1
    except Exception:
        pass
if count:
    print(f"  Rendered {count} transcript(s).")
else:
    print("  No transcripts to render.")
PYEOF
echo ""

# ── 6. Initial index ──
echo "Running initial index..."
.venv/bin/python3 indexer.py 2>/dev/null || true

echo ""
echo "=== Setup complete ==="
echo ""
echo "To finish the install:"
echo ""
echo "1. This project needs the following startup step in its CLAUDE.md."
echo "   If the project does not have a CLAUDE.md, create one with this step."
echo "   If a CLAUDE.md already exists, do not write to it — show the user"
echo "   this text and ask them to add it:"
echo ""
echo "     Dispatch the session-summarizer agent on the most recent .md"
echo "     transcript in $TRANSCRIPTS_DIR/, writing output to"
echo "     recall/summaries/<transcript-basename>.txt. Always run the"
echo "     summarizer even if a summary file already exists. Run the"
echo "     agent in the background. When it reports done, run:"
echo "     recall/.venv/bin/python recall/indexer.py --incremental"
echo ""
echo "2. The recall index is empty. Recall will not work on past sessions"
echo "   until the session-summarizer agent has been run on each .md"
echo "   transcript in $TRANSCRIPTS_DIR/ (writing output to"
echo "   recall/summaries/<transcript-basename>.txt) and the indexer has"
echo "   been run. Ask the user if they would like to summarize their"
echo "   existing transcripts now. The session-summarizer agent is not"
echo "   available until the next session (agents load at session start)."
echo "   To summarize now, read the agent file at"
echo "   .claude/agents/session-summarizer.md for the prompt. For each .md"
echo "   transcript in $TRANSCRIPTS_DIR/, dispatch a general-purpose agent"
echo "   (model claude-opus-4-6 or higher) with the summarizer prompt,"
echo "   writing output to recall/summaries/<transcript-basename>.txt."
echo "   Run the agents in parallel. After all complete, run:"
echo "   recall/.venv/bin/python recall/indexer.py"
echo ""
echo "Start a new session so the hooks take effect. Use +recall in any prompt"
echo "to trigger recall."

# Version 2.1
