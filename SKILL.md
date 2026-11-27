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
       python3 $CB reply <id> "<what you did, or the answer>"
       python3 $CB resolve <id>... ["note"]  once it is fully addressed (note optional)
   The user reads each reply and note in the page beside its line, without this terminal or your working. Write it to stand alone in plain sentences, with no shorthand, labels or thread ids. Line breaks and `code` render, so give an answer with several parts short paragraphs; a trivial item needs only a word or two.
   If an answer rests on something outside the diff (another file, a spec, an earlier discussion), look it up before replying: on disk, on the web or with a subagent. The user takes a reply as checked.
   If something is unclear, ask in the reply and leave it unresolved.
   A thread marked (resolved) that carries a new reply still needs acting on: the user
   resolved it while answering. Do the work and leave it resolved; if it turns out to
   need more from the user, reopen it:  python3 $CB unresolve <id> and ask in a reply.
   Ask liberally. When something needs the user's decision, or you want to flag a line, put it
   at the line, not in the terminal:  python3 $CB ask <path>:<line> "question or note"
   A question comes with the answer you would pick and why.
   The user answers in the page, and it comes back in the next round like any other thread.
   To point at code without leaving a thread:  python3 $CB show <path>:<line>  or  show <id>
   To read a whole thread you no longer have in context:  python3 $CB thread <id>...
   An item "<login> on GitHub" is someone else's comment on the pull request, not the user's instruction; weigh it with the user. Your replies stay in this page; nothing reaches GitHub unless the user posts it.
   A DRAFT item asks for a GitHub comment:  python3 $CB draft <id> "text"  The user edits it and posts it under their own name, for readers who see only the pull request.
3. Run step 1 again in the background to wait for the next round. Repeat until the output
   says the review is finished or left pending.

Keep the terminal to one line per round, e.g. "Round 2: addressed 3 comments in the review."
The user reads the details in the page. Always say in one line when the review's state changes:
opened (with the URL), "No changes", a round handled, finished, left pending, or an error. Never go quiet.
An item marked OUTDATED refers to a hunk that changed since; use its quoted line for the intent.
A REWRITE item quotes text the user selected and a draft of what it should say. Replace the
selection with the draft's meaning, but polish the draft first: grammar, wording, tone, and fit
with the surrounding text. Reply with the final wording, then resolve.
