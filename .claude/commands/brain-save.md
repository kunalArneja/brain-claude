---
description: Force-capture this session into Brain-Claude memory now
---

Capture what we've done so far into Brain-Claude memory.

Review the conversation and emit **exactly one** `<brain-update>{…}</brain-update>`
block (per the format in CLAUDE.md) summarizing this session. The `Stop` hook will
persist it automatically.

Guidance:
- Fill `context`, `actions_taken`, `lessons_learned`, and `pending_items` from what
  actually happened — be concrete.
- Quote real `user_feedback` where the user reacted; don't invent it.
- Only include `metrics` you can state honestly. Metrics you author here are recorded
  as **self-reported** — do not fabricate numbers to look successful. Prefer metrics
  that are objectively true (e.g. tests passed, files changed), and set each metric's
  `dir` correctly (`min` = lower is better, `max` = higher is better).
- Link `related_nodes` to any node ids that were recalled this session.

After the block, confirm in one line what you captured.
