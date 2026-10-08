---
description: "4pm summary of open items and what I worked on; I review it, then it goes in the daily log."
schedule: {at: "16:00", days: workdays}
context: main
thinking: true
await_reply: true
escalate:
  - {after: 30m, via: desktop}
  - {after: 2h, via: imessage}
catch_up: 2h       # still run if the Mac wakes up within 2h of the scheduled time
enabled: false   # turn on once the Slack / Gmail / Docs MCP servers are set up
---
It's the end of {{user_name}}'s workday ({{weekday}} {{today}}). Put together a brief end-of-day summary.

1. Gather today's activity from {{user_name}}'s common work resources. Use `spawn_agent` once per
   source so the raw results stay out of this conversation, and ask each one to return a short list
   of items with links:
   - Slack: threads and DMs {{user_name}} took part in today, and anything still asking them for something.
   - Email: what they sent today, and unanswered mail that needs them.
   - Google Docs: docs they edited, or were mentioned or commented on, today.
   - Any company resources you have tools for: tickets, PRs, or reviews they touched or that are assigned to them.
2. Compare that with the working context (Plate, In progress, Waiting on others).
3. Reply with the summary, under these headings:
   **Worked on**, **Still open**, **New today**, **Waiting on others**.
   Use one line per item, with a link where there is one. Keep it short enough to read in a minute.
4. End by asking: "OK as is, or anything to add or expand on?"

Then wait. {{user_name}} will either OK it or ask for changes, so revise until they're happy. Once the
conversation about it is done:
- Prepend the final summary to the **Daily log** section of the working context, under a `### {{today}}` heading.
- Update **Plate**, **In progress** and **Waiting on others** to match: tick finished items, add new ones.
- Confirm in one line.
