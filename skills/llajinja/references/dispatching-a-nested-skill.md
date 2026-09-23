# Dispatching a nested jinja skill

The step instruction that sent you here carries the prompt text for the Agent/Task, sometimes extra
context the skill author attached to this step, and the driver command. This is what to do
with them.

The jinja skill that prompt names runs as a workflow of its own: inside a Agent/Task, with its own
session id and its own log. You dispatch it. You do not run it here, you do not run its steps,
and you do not touch this run's session id while doing so.

## How to run the Task/Agent tool

1) Define the agent/task description that will be sent.

2) That description must carry the prompt text for the Agent/Task, copied VERBATIM — not
paraphrased, not summarised, not shortened. It is intended for the task/agent, not for you,
and nothing in it is a placeholder for you to fill.

3) When the instruction also carries extra context the skill author attached to this step, put
that in the description as well, copied VERBATIM, after the first block. It is part of what
the Agent/Task is told, not a note for you and not work for you to do.

4) Beyond those blocks, tell the Agent/Task every limit the user placed on the work — what must not
be run, executed, installed or accessed. It cannot see the user's request, so restate them.

## What you must not do on this step

- Do not run the nested run's `--skill` command yourself in this session. Two runs driven from
one session means two session ids to carry and one context holding both; the nested run gets
its own Agent/Task precisely so this one keeps a single id and a single log.
- Do not do the nested skill's work yourself, and do not read that skill to decide what it
would have done.
- Do not write the result file the prompt names or any value in it, and do not read it back to
check what happened. The Agent/Task measures those values; the driver reads them.

## When the Agent/Task tool is done

Immediately re-run the driver command the instruction printed, seeking the next instruction
and following what it says:

`python scripts/get_next_instruction.py --llajinja-session {session_id}`
