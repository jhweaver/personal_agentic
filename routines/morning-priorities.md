---
description: "8:30am: prioritize what's still open, using the working context and fresh updates from work tools."
schedule: {at: "08:30", days: workdays}
context: main
thinking: true
notify: [desktop]
catch_up: 3h       # still run if the Mac wakes up within 3h of the scheduled time
enabled: false   # turn on once the Slack / Gmail / Docs MCP servers are set up
---
Good morning. It's {{weekday}} {{today}}. Help {{user_name}} decide what to work on today.

1. Read the working context: Plate, In progress, Waiting on others, and the latest Daily log entry.
2. Look for updates since the last Daily log entry. Use `spawn_agent` per source so raw results stay
   out of this conversation:
   - Slack and email: replies on anything in "Waiting on others", new requests for {{user_name}},
     and anything urgent or time-bound.
   - Google Docs: new comments or mentions on docs they're working on.
   - Company tools you have access to: changes to their tickets, PRs and reviews.
   - Calendar, if you have it: today's meetings that need preparation.
3. Update the working context: tick or remove resolved items, and add new items to Plate in the form
   `- [ ] what (source, {{today}})`.
4. Reply with a prioritized list:
   - **Top 3 for today**: each with why it matters now and the concrete next step.
   - **Also open**: the rest, most important first, one line each.
   - **Waiting on others**: anything worth a nudge today.

Don't send messages to anyone as part of this routine.
