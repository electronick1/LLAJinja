# Dispatching a sub-agent

The step instruction that sent you here carries the prompt text for the Task and the driver
command. This is what to do with them.

Launch the named sub-agent with the Task or Agent tool, as a separate standalone unit of
work. You dispatch it, and that is all you do.

## How to run the Agent/Task tool

1) Define the agent/task description that will be sent.

2) Include the prompt text for the Task/Agent, copied VERBATIM — not paraphrased, not summarised,
not shortened, not reordered. It is intended for the sub-agent, not for you, and nothing in
it is a placeholder for you to fill.

3) Beyond that block, tell the Task/Agent every limit the user placed on the work — what must not
be run, executed, installed or accessed. The sub-agent cannot see the user's request, so
restate those limits.

## What you must not do on this step

- Do not do this work yourself, in whole or in part, however small it looks next to the cost
of a Task/Agent. That prompt is the sub-agent's, not yours to act on.
- Do not write the result file it names, and do not supply any value in it — not with an edit
tool, not from a shell, not from what the Task/Agent said in its reply. Those values are the
sub-agent's to measure, and one you author is invented.
- Do not read that result file, and do not `ls`, `cat` or otherwise inspect anything under the
run directory to check the outcome. The driver reads it and decides; an outcome you checked
yourself is an outcome you will be tempted to act on.
 
## When the Agent/Task tool is done

Immediately re-run the driver command the instruction printed, seeking the next instruction
and following what it says, whatever state the Task/Agent left behind — the driver decides what is needed next:

`python scripts/get_next_instruction.py --llajinja-session {session_id}`
