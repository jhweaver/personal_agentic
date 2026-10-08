---
description: "Every 15 min in the workday, PRs from watched authors that are 3h+ old and not mentioned in Slack go on my plate."
schedule: {every: 15m, window: workday}
context: isolated
thinking: false
report: changes
notify: [desktop]
tools: ["gh", "slack__*", "glean__*", "working_context_*", "state_get", "state_set", "current_time"]
vars:
  repo: "your-org/X"                 # the X project
  authors: ["h1", "h2", "h3"]        # GitHub handles to watch
  min_open_hours: 3
enabled: false   # turn on once the vars above are filled in and Slack (or Glean) search works
max_steps: 25
---
Watch for pull requests that {{user_name}} should review.

1. Call `state_get` with key `handled` to get the PR numbers you've already dealt with (a list; empty if none).
2. List the **open** pull requests in `{{repo}}` from each of these authors: {{authors}}. Call the `gh`
   tool once per author with args like:
   `pr list --repo {{repo}} --state open --author HANDLE --json number,title,url,createdAt,author`
3. Keep only PRs that have been open for at least {{min_open_hours}} hours (compare their creation time
   with `current_time`) and aren't in `handled`.
4. For each remaining PR, search Slack for any mention of it: its URL, `{{repo}}#<number>`, `#<number>`
   near the repo name, or its title. Use the Slack tools if you have them; otherwise use Glean search,
   limited to Slack if it lets you choose a source. If neither can search Slack, say so and stop.
   - If it's mentioned anywhere, someone has picked it up. Just mark it handled.
   - If it isn't mentioned, use `working_context_update` (section `Plate`, action `append`) to add:
     `- [ ] Review PR #<number> "<title>" by @<author> — <url> (pr-watch, {{today}})`
     and mark it handled.
5. Save the updated list with `state_set` key `handled`. Drop numbers of PRs that are no longer open
   so the list doesn't grow forever.
6. If you added anything to the Plate, reply with one line per PR added. Otherwise reply with exactly NO_UPDATE.
