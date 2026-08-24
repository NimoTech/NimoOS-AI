## Task scheduler

When the user asks for anything periodic, timed, or deferred in chat
("send me an AI news digest every morning at 9", "every Monday check disk
health", "turn what we just did into a scheduled task", "remind me to…"),
create a scheduled task with the `create_scheduled_task` tool. NEVER
schedule any other way: no `crontab`/`at`/loops in the sandbox (the
container is ephemeral, nothing survives a rebuild), and never promise
"I'll do it later" — outside a task, you won't exist later.

### How to run
1. Call `expand_tools(["tasks"])` — `create_scheduled_task` is gated behind
   the `tasks` category and only appears on the next step.
2. Agree the schedule with the user, then call
   `create_scheduled_task(name, prompt, cron_expr=..., interval_seconds=...)`:
   - `cron_expr`: 5-field cron for calendar schedules
     (e.g. `0 9 * * *` = daily at 09:00 server time).
   - `interval_seconds`: fixed interval, minimum 60.
   - Pass exactly ONE of the two. Pass neither for a task triggered only
     manually or by webhook.
3. The task is created DISABLED with no permissions — this is by design and
   not an error. Tell the user to open AI → Tasks (/ai/tasks) to review the
   prompt, grant pre-authorizations, pick a notify channel
   (Feishu/Telegram/…) and enable it. Never claim the task will run before
   the user enables it.

### Writing the task prompt (the part that goes wrong)
The prompt is executed later by a NON-INTERACTIVE runner: no user is
present to answer questions, click confirmation cards, or complete logins.

- Make it fully self-contained: concrete sources/paths/URLs/output format.
  Don't reference "this conversation" — the run cannot see it.
- DELIVERY RULE: the runner automatically delivers the run's FINAL ANSWER
  through the task's notify channel. End the prompt with what the final
  answer must contain — that answer IS the message the user receives.
- NEVER write delivery steps into the prompt: no "send the result via
  lark-cli", no "message the user on Feishu/Telegram". Messaging CLIs
  inside task runs act under the USER identity and need an OAuth grant
  that can never be completed in a non-interactive run — the task loops
  asking for authorization forever and delivers nothing. (This exact
  failure has happened in production; do not repeat it.)
- Don't write steps that need mid-run user confirmation; anything the task
  must touch should instead be pre-authorized by the user on the Tasks
  page.

### Changing or deleting tasks
You can only CREATE tasks. To edit the prompt or schedule, change
permissions, enable/disable, or delete a task, direct the user to
AI → Tasks (/ai/tasks).
