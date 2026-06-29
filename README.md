# AI Recall System

Type +recall in any prompt to automatically give Claude a bit of relevant context from past sessions.

[Download the repo.](https://github.com/vbiroshak/ai-recall-system/tags)

## What it does

This isn't memory — it's recall, a way to pull up relevant past discussions on demand.

Append `+recall` to any prompt and Claude gets relevant context from past sessions injected alongside your message.

Like saying "hey, remember when we talked about this?" — Claude gets the recollection, but only when you trigger it.

Claude's built-in auto-memory and a [workspace architecture](https://github.com/vbiroshak/ai-project-architect) can provide continuity across sessions. This system can be used on top of those or alone.

## What you need

- Claude Code (CLI or Desktop app)
- Python 3.10+
- Any directory where you use Claude Code

## Setup

Copy the `recall/` directory into your project root and tell Claude to install it. Claude will read `setup.sh`, examine the files, run it, and walk you through the post-install steps. If you don't have a `CLAUDE.md` yet it will create one; if you already have one, it will show you the startup step to add. It will also offer to summarize your existing transcripts to build the initial index.

When Claude runs the setup script it will:
- Install dependencies into a local venv (~80MB for the embedding model)
- Copy the transcript archiver and renderer to `.claude/hooks/`
- Wire the hooks into your `.claude/settings.json`
- Copy the session-summarizer agent to `.claude/agents/`
- Scan for existing Claude Code sessions from this project and import them
- Create human-readable `.md` versions of each transcript while preserving the original `.jsonl`, giving you a backup of your transcripts
- Note: if you already use the [AI Project Architect](https://github.com/vbiroshak/ai-project-architect) system, it will re-render all existing `.md` transcript files from the `.jsonl` files to add timestamps, which are used here for dating each indexed unit and for temporal decay in retrieval

If your project already uses [AI Project Architect](https://github.com/vbiroshak/ai-project-architect), setup detects its existing transcript archiver and leaves it in place — only the recall hook is added. `transcript_to_md.py` is always updated to the version that adds timestamps (needed for dating indexed units).

After setup, start a new session so the hooks take effect.

## How to use it

To keep the index current, your `CLAUDE.md` needs a startup step that summarizes the latest transcript each session. If Claude ran the install and you didn't have a `CLAUDE.md` already, it will have created this for you. If you already had a `CLAUDE.md` or you installed manually, add this to your `CLAUDE.md`:

```
Dispatch the session-summarizer agent on the most recent .md transcript in recall/sessions/ (or wherever your transcripts live), writing output to recall/summaries/<transcript-basename>.txt. Always run the summarizer even if a summary file already exists. Run the agent in the background. When it reports done, run:
recall/.venv/bin/python recall/indexer.py --incremental
```

If Claude runs the install, it will offer to run the session-summarizer agent on all the `.md` versions of your transcripts that the setup script created in your transcript directory. This is necessary in order for the indexer to populate the database. If you do the installation yourself, or want to do it later (or choose only certain transcripts), you will need to dispatch the session-summarizer agent on each `.md` transcript in your transcript directory (writing output to `recall/summaries/<transcript-basename>.txt`), then run `recall/.venv/bin/python recall/indexer.py`.

Once the setup, batch summarizer run, and index is complete, just append `+recall` to any prompt:

```
What was the approach we settled on for the auth system? +recall
```

Claude gets relevant past context injected automatically. The injected context is invisible to you by default.

**To see what was recalled**, use `+recall?` instead — Claude will show you what was retrieved before responding:

```
What was the approach we settled on for the auth system? +recall?
```

## How it works

The system has three stages:

1. **Archive** — A SessionStart hook copies and renders each completed session transcript to your transcript directory as a readable `.md` file. If you name your sessions (via `/rename` in Claude Code), the archived files use that name. Sessions without a name keep their UUID filenames — the system works either way.

2. **Summarize and index** — A session-summarizer agent decomposes each transcript into typed topic-units (findings, decisions, open threads, state changes). The indexer embeds these units locally using [all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) and stores them in a SQLite database.

3. **Retrieve** — When you include `+recall` in a prompt, the hook embeds your query, searches the index using hybrid semantic + keyword matching, applies temporal decay (recent findings rank higher), and injects the top matches into your conversation context.

**Note on naming:** The archiver names transcripts using three tiers. If you `/rename` a session with a name and number (e.g. "MyProject 5"), the file is formatted as `MyProject_0005.jsonl` (zero-padded for consistent sort order). If your opening message declares "This is ProjectName N" (the [AI Project Architect](https://github.com/vbiroshak/ai-project-architect) naming convention), that is used as a fallback. Sessions with neither keep their UUID filenames. The Claude Code Desktop app shows session names in its UI that are not written to the actual transcript — only `/rename` does that.

Embedding, indexing, and retrieval all run locally — no external services, no data leaves your machine. Summarization uses a Claude agent (the same way any Claude Code agent interaction works), so transcript content is sent to Anthropic's API during that step.

## What's in this repo

| Path | What it is |
|------|-----------|
| `recall/hook.py` | The `+recall` trigger — searches the index and injects context (UserPromptSubmit hook) |
| `recall/indexer.py` | Parses summaries, embeds topic-units, stores in SQLite |
| `recall/embed.py` | Local embedding (fastembed / all-MiniLM-L6-v2, 384-dim) |
| `recall/archive-transcripts.py` | Archives and renders session transcripts (SessionStart hook) |
| `recall/transcript_to_md.py` | Converts `.jsonl` transcripts to readable `.md` |
| `recall/setup.sh` | One-time setup: config, dependencies, hooks, agent |
| `recall/requirements.txt` | Python dependencies (fastembed, numpy) |
| `recall/config.json.example` | Example configuration |
| `recall/agents/session-summarizer.md` | Agent that decomposes transcripts into topic-units. Pinned to `claude-opus-4-6` — the lowest model that produced reliable results in testing. Change the `model:` frontmatter if needed. |

## Contributing

This is a project I maintain for my own work. Hopefully you find it useful and can adapt it to yours. If you run into problems or have suggestions, open an issue on the repo.

## License

[MIT](LICENSE)

---
*Part of [AI Recall System](https://github.com/vbiroshak/ai-recall-system) — Version 2.0*
