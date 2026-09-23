---
name: llajinja
description: >
    Execute a jinja-templated skill that the user points you at. In the current session you
    run scripts/get_next_instruction.py and precisely follow the instructions in its output
    (stdout). The template is the plan and the driver replays it step by step.
---

# LLAJinja — execute a jinja-templated skill

The user names a jinja skill; `scripts/get_next_instruction.py` — a resumable state machine,
called the driver — renders it, decides what runs next, and prints instructions. 

This skill is executed in three roles:

**ORCHESTRATOR** — the session where the user asked to run this skill. You run the driver and
perform exactly what it prints, then run the driver again, until it prints that execution is
finished. Some steps can be executed in this session (a **stage**), other you dispatch
as a Task/Agent and nothing more. The driver tells you which kind each step is; you never
decide that.

**SUB-AGENT** — an Agent dispatched as an Agent/Task the orchestrator started for a `sub_agent` step.
As sub-agent you do one step of the real work and write ONE result JSON file. Then you exit.
You never run `scripts/get_next_instruction.py`, and you never load this skill.

**NESTED ORCHESTRATOR** — an Agent/Task the orchestrator started for a nested jinja skill. You drive
one nested llajinja run — with the session id that run prints for itself, never one handed to
you from outside — write ONE result JSON file, and exit.


## Arguments and environment

- `--skill <name>` — the jinja skill to execute, by **name**. LLAJinja looks for it beside
  itself, among the other skills of this project, and nowhere else; anything that leads
  outside that directory is refused. Required on the very first call of a run and never
  passed again; the recorded value is the skill that runs.
- `--llajinja-session <id>` — identifies this run; every file and every log record is keyed by
  it. You type the id the driver gave you on **every** execution after the first:
  `python scripts/get_next_instruction.py --llajinja-session <id>`.
- `--var name=value` — repeatable, values are strings. Supplies an input variable the skill
  declares. Only ever used when the driver asks for one, and only with a value the user gave.
- a positional path — the working directory for the deliverables, asked for once at setup.
- `LLAJINJA_WORKFLOW_PATH` — base directory for run workspaces, supplied by the environment
  (default `/tmp`). Leave it as you found it; setting it moves the whole run.


## Execution protocol for a sub-agent or a nested orchestrator

If you are a SUB-AGENT, do the scope of work your dispatch named,
write the one JSON result file it named, and exit. You never perform orchestrator duty for the
run that dispatched you, and finding this skill in your own list of available skills is not an
invitation to load it — under friction, report the friction and exit.


## Execution protocol for the orchestrator

These phases are the ORCHESTRATOR's protocol. A sub-agent never performs them.

Run `scripts/get_next_instruction.py` (supplying the needed arguments), then follow the
printed instructions literally and re-run the driver. Never decide the next step yourself —
the driver decides, from the template. In case of failure or misbehaviour, re-run the driver.
**The phases below describe what the driver *may* print — not a checklist to run
top-to-bottom.** Always do exactly what the latest execution printed, then re-run the driver.
Phases can repeat, branch, or arrive in a different order.

**Phase 1 — setup.** Once, and only once per run, call the driver with the skill the user
named and no id: `python scripts/get_next_instruction.py --skill "<name>"`. It prints the
`--llajinja-session` id to use. That form is the first call of the run and never appears
again; every later call carries the id. A call without the id never continues a run — it
starts a new one and abandons yours. If setup asks for anything else (normally the working
directory for deliverables), answer in the exact form it printed.

If the user did not name a path, ask for one.

**Phase 2 — variables.** The skill's input variables that have neither a value provided nor a default
are printed with the template that declares each. Ask the user for them using available "Ask" or "Question"
tools (if none such tools available ask in writing form and stop execution until user answers), then re-run
the driver with one `--var name=value` per answer in a single call. Values come from the user, unless
otherwise instructed: do not guess, derive or default one, and do not read the skill to find a value the
driver already reported missing.

**Phase 3 — execute.** The driver replays the log, re-renders the template, and prints the one
step that comes next, in one of three kinds:

- **stage** — a `{% stage %}` of the template, rendered. **You carry it out yourself, in this
  session.** This is the one and only place where the orchestrator does the work. There is no
  Task/Agent, no result file and no output value: the outcome lives in this session's context, which
  is exactly why the author made it a stage. Then re-run the driver — that is what records it
  as done.
- **sub_agent** — you start ONE Agent as a Task with the printed prompt copied verbatim, and it
  writes the printed JSON result file. You do not do that work, do not author or edit any value in that
  result, and do not read it back to see how it went. The driver reads it and decides.
- **nested jinja skill** — you start ONE Agent/Task that drives llajinja over another jinja skill,
  in its own run with its own session id, and reports back one JSON result. You do not run
  that nested skill in this session.

After every step, whichever kind it was, re-run
`python scripts/get_next_instruction.py --llajinja-session <id>` and follow what it prints,
until it prints that execution is finished.

If the driver reports that the skill could not be loaded, the template is broken. There is no
worker to regenerate it and re-running prints the same error: report the error verbatim and
stop, without editing the skill and without doing its work by hand.


## Core rules you must always follow

1. As the orchestrator, you do the work of a **stage**, in this session, and of nothing
   else. A `sub_agent` step and a nested jinja skill are always dispatched via the Agent, sub-Agent
   or Task tools, as standalone units of work. A step whose prompt tells the Agent/Task to use some
   skill of this project is still a Agent/Task you dispatch, never one you run here.
2. As the orchestrator, do exactly what the latest execution printed; never skip, reorder or
   anticipate steps, and never run a step the driver has not printed yet.
3. Every driver call except the very first carries `--llajinja-session <id>`. Dropping it does
   not continue your run — it starts a new one. Copy the id from the driver output character
   for character; never retype it from memory.
4. As the orchestrator, stop only when the driver prints a line stating `Execution finished`.
   Every turn you take must contain a call for the next instruction; deciding to stop, or
   saying that you will re-run the driver without actually calling it, is not permitted.
5. Paths and prompts in the driver's output are copied exactly as printed. Text the driver
   prints inside a fenced block for a Task is passed on **verbatim** — not paraphrased, not
   summarised, not shortened. Paraphrasing a dispatch changes what the Task is obliged to do.
6. `scripts/get_next_instruction.py` is the only program you run as the orchestrator, apart
   from what a stage or a skill step explicitly asks for. No builds, no tests, and never extra
   or improper arguments. The driver creates and owns its own directories.
7. **Result files belong to the sub-agents.** If one is missing, wrong or rejected, never
   repair it — re-run the driver and let it re-dispatch. Never open a write or edit tool on a
   result JSON, on the run's log, or on the jinja skill's templates; never `mkdir`, `rm` or
   `touch` a path the driver printed.
8. **Never invent an output.** A result value must have been measured by the sub-agent that
   ran — not guessed, not carried over from a Task's prose reply, not decided by you.
9. A variable value comes from the user; a result value comes from a sub-agent; the next step
   comes from the driver. None of the three is ever yours to supply.
10. The jinja skill being executed and this skill's own scripts are not yours to repair —
    never edit anything under either directory, however small the fix looks.
11. A Python traceback from `scripts/get_next_instruction.py` is not an instruction. Re-running
    prints the same traceback. Do not repair the run, do not produce the deliverable yourself,
    and do not keep looping: report the traceback and stop. This overrides rule 4.
12. Availability of the `python` executable depends on the project setup. The driver prints its
    commands with the word `python` as a stand-in — substitute the interpreter that works in
    this project (from `uv`, the venv, or one the user named), replacing that one word rather
    than prepending anything to it, and keep using the same one on every call. Rule 5 governs
    paths, not the interpreter.
13. When the driver prints a step, act on it in the same turn: dispatch it, or — for a
    stage — do it. A step acknowledged but not performed is a run abandoned.
