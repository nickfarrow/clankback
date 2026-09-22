---
name: clankback
description: Review a local diff in the browser like a GitHub pull request and iterate on the comments from here. Usage /clankback [--staged] [<ref> | <PR#> | <file-a> <file-b>] [<path>...] (outside a repo: <path>... reviewed as new files) [--resume] [--discard]
disable-model-invocation: false
allowed-tools: Bash(python3 ~/.claude/skills/clankback/clankback.py *)
---

The review is a loop between the user's browser and this terminal. `CB=~/.claude/skills/clankback/clankback.py`

1. Run in the background: `python3 $CB $ARGUMENTS`. It blocks until the user clicks
   "Send to clanker", then prints the new comments and replies, each with an id like `[k3f9a]`.
2. For each item: do what it asks, then answer in the page, not in the terminal:
       python3 $CB reply <id> "<one short line: what you did, or the answer>"
       python3 $CB resolve <id>... ["note"]  once it is fully addressed (note optional)
   If something is unclear, ask in the reply and leave it unresolved.
   Ask liberally. When something needs the user's decision, or you want to flag a line, put it
   at the line, not in the terminal:  python3 $CB ask <path>:<line> "question or note"
   The user answers in the page, and it comes back in the next round like any other thread.
   To point at code without leaving a thread:  python3 $CB show <path>:<line>  or  show <id>
3. Run step 1 again in the background to wait for the next round. Repeat until the output
   says the review is finished or left pending.

Fan out when a round has 3+ items across more than one file: one background subagent per file
(same file, same agent), each given its items verbatim, the repo path and the reply/resolve/ask
commands above, posting its own answers. Keep decisions and cross-file items yourself, and go
straight back to waiting; answers land in the page as each agent finishes. If a later round
follows up on a thread whose agent is still running, pass it to that agent instead of editing.

Keep the terminal to one line per round, e.g. "Round 2: addressed 3 comments in the review."
The user reads the details in the page. Always say in one line when the review's state changes:
opened (with the URL), "No changes", a round handled, finished, left pending, or an error. Never go quiet.
An item marked OUTDATED refers to a hunk that changed since; use its quoted line for the intent.
