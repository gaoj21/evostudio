---
name: code-review
description: Review source code for correctness, readability and security issues. Use when the user asks to review, audit or improve a piece of code.
---

# Code Review Skill

When asked to review code, follow these steps:

1. **Understand the intent** — read the code and restate what it is supposed to do in one or two sentences.
2. **Check correctness** — look for logic errors, off-by-one mistakes, unhandled edge cases and exception paths.
3. **Check readability** — naming, dead code, overly complex conditionals, missing docstrings on public APIs.
4. **Check security** — injection risks, hard-coded secrets, unsafe deserialization, missing input validation.
5. **Report** — group findings by severity (critical / major / minor). For each finding, cite the file and line, explain the impact, and provide a concrete fix.

See `references/checklist.md` for the detailed review checklist.
