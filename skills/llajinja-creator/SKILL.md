---
name: llajinja-creator
description: >
    Creates jinja skills — skills written in the Jinja template language, with tags and
    functions that llajinja adds on top of it to manage agents and the harness. Use when
    the user wants an jinja templated skill that will be executed with llajinja driver.
---

# Creating jinja based skills

llajinja is a python driver that executes skills  written in the Jinja template language, plus
tags and functions that llajinja adds on top of it to manage agents and the harness running the
skill. It has the usual frontmatter (`name`, `description`) and is run with the `llajinja` .

This skill defines convention that is used to write the `SKILL.md` for llajinja driver.
The content of this skill is only source you use to build the jinja
SKILL for llajina driver, do NOT check and look in any other skills for patterns,
examples, style or structure, only this skill gives a source and definitions of how to write
jinja skill for llajinja driver.

Start from understanding user request, what is needed, and then how to convert that into
jinja template executed by LLAJinja using constrains and defition below.

## How an llajinja driver works

The body of the skill is a Jinja template. What it renders is the instructions that the
orchestrator — the agent running the skill — follows. llajinja adds:

- `input.NAME` — a value the user gives for the run;
- `{% agent %}` — work for a sub-agent, which reports values the rest of the skill can use; only
  when the user asks for sub-agents;
- `{% agent %}` with `decision()` — a question about raw text, answered by a decision model; only
  when the user asks for decision agents;
- `{% bundle %}` — many decision agents asked together; only when the user asks for decision
  agents;
- `{% stage %}` — a separate part of the execution;
- `find_files()` — files of the working directory;
- `temp_file()` — a file for data the run creates, in a temporary folder;
- `decision_state_from_file()` — a file's text, for a decision agent; only when the user asks for files
  to be read.

The skill runs from top to bottom. A result can be used by everything after it — in
instructions, conditions, loops and later agents. Values inserted with `{{ }}` become part of
the instructions exactly as they are.

Everything the template renders outside agents reaches the orchestrator as instructions, so
notes meant only for people reading the skill go in Jinja comments. Instructions after the last
agent or stage come last in the run — the place for the report to the user.

## Jinja in llajinja driver

Standard Jinja works, with these differences:

- `random` and `lipsum()` are not available.
- `{% do %}`, `{% break %}` and `{% continue %}` are not available.
- Lists, mappings and sets cannot be changed in place: `append`, `extend`, `insert`, `pop`,
  `remove`, `sort`, `update`, `setdefault` and similar methods fail. New values are built
  instead — `items + [item]` — and a value that has to survive loop iterations lives in a
  `namespace()`.
- Attributes whose names start with `_` cannot be read.
- `range()` produces at most 100,000 items.
- `{% include … without context %}` cannot be used inside a stage, an agent, a macro, or a
  call, filter or set block: Jinja mishandles it there, and the run stops or the text comes
  out wrong.
- Anything undefined — a variable, an attribute, a key — stops the run. `is defined` tests for
  it; `| default(…)` replaces it.

The content of every block is indented two spaces deeper than its opening and closing tags, so
the nesting of the skill is visible at a glance.

## Inputs

An input is a value that needs to be providen by the user for a run. Writing `input.NAME`
anywhere in the template makes `NAME` an input; nothing else declares it. Before the run starts,
llajinja collects the inputs of the skill — including those of the skills it includes, extends
or imports — and asks the user for every required input that has no value yet.

- `{{ input.NAME }}` and `{{ input["NAME"] }}` are a required input.
- `{{ input.NAME | default("VALUE") }}` is an optional input; `VALUE` is used when the user gives
  none. The input is optional only if every use of `input.NAME` has a default.

Values the user gives are strings, so `| int` converts them before arithmetic. They keep their
value for the whole run, and they are exactly what the user typed; nothing checks them.

## Sub-agents

Sub-agents — nested skills too — are used only when the user asks for them. Without such a
request, the work is written as instructions for the orchestrator.

```jinja
{% agent NAME outputs={"KEY": "description of the value", …} %}
PROMPT
{% endagent %}
```

A sub-agent is a separate agent that carries out PROMPT in a fresh context and reports a value
for each key in `outputs`. After the agent, `NAME.KEY` holds the reported value — always a
string — for the rest of the skill. The sub-agent sees nothing but its prompt, so the prompt
contains everything it needs; inputs and earlier results go in with `{{ }}`.

A sub-agent is costly — it starts from an empty context, and inside a `{% for %}` it runs once
per item — so each one is carefully considered. Work whose result nothing else in the skill
uses needs no sub-agent: as instructions, the orchestrator does it with the context it
already has.

A prompt may name one of the project's regular skills; that skill then runs inside the
sub-agent, and only `outputs` come back. Only the keys listed in `outputs` exist; reading any
other stops the run. Inside a `{% for %}`, the sub-agent runs once per iteration and `NAME`
holds the current iteration's result. A prompt cannot contain another agent, neither written in
it nor brought in with `{% include %}`.

## Nested skills

```jinja
{% agent NAME nested="SKILL", input={"INPUT": "value", …}, outputs={"KEY": "description", …} %}
PROMPT
{% endagent %}
```

A sub-agent that runs another llajinja driver from start to finish, as a separate run with the
inputs given in `input`, and reports `outputs` like any sub-agent. It suits a long piece of work
that needs a context of its own when only a few of its results are needed. `{% include %}` makes
the other skill's instructions part of the same run instead.

## Decision agents

Decision agents — and bundles of them — are used only when the user asks for them: by naming
decision agents, or a decision model such as Jev, Clef, TypeSafe, Laya or Nimble. 

```jinja
{% agent NAME %}
{{ decision(QUESTION) }}
TEXT
{% endagent %}
```

An agent whose content calls `decision()` is a decision agent. TEXT — is the decision agent's content
excluding `decision()` call — is sent to the decision model together with the question, and
the answer is stored in `NAME`. 

**The decision model reads only raw text.** It gets TEXT exactly as written, and nothing else: it
cannot open files, run commands or see the conversation. A file path written in TEXT stays a
path; the file itself is never read. When the user asks for files to be judged, the file's
content goes into TEXT with `decision_state_from_file(PATH)`. TEXT and the questions are sent to
the decision model, outside this machine.

A decision agent fits classifying, rating or accepting a TEXT. Work that needs commands or files
belongs to a sub-agent.

A decision agent can judge data that the orchestrator collects without that data entering the
orchestrator's context. A stage asks the orchestrator to run a command that writes all the data
into one `temp_file()`, so the orchestrator never sees it. A decision agent after the stage
passes the file's path to `decision_state_from_file()` and reads the data from the text with
Jinja, in its body. For Jinja to read the data, the command has to write it in a clear format:
newline-separated text, one item per line, or JSON Lines (JSONL), one JSON value per line.
Jinja splits both into lines; it does not parse the JSON, which reaches the decision model as
text.

### Questions

- `choice("QUESTION", LABELS)` — which label fits. `LABELS` is a list of labels, or a mapping
  from each label to a short description.
- `score("QUESTION", LEVELS)` — where the text sits on a scale. `LEVELS` is a list of level
  names, lowest first; the levels are numbered from 0.
- `yesno("QUESTION", yes="…", no="…")` — yes or no. `yes` and `no` optionally describe each
  answer.

`LABELS` and `LEVELS` are at least two distinct, non-empty strings; `LEVELS` is a list, never a
mapping. Anything else stops the run when the question is built.

Several questions about the same text are given names — `decision(A=QUESTION, B=QUESTION)`,
possibly over several `decision()` calls — and the answers are then `NAME.A` and `NAME.B`.

### Answers

Every answer has a `value`:

- **choice** — the label chosen, a string. It is not guaranteed to be one of `LABELS`, so a
  condition on it needs an `{% else %}`.
- **score** — the expected level: the average of the level numbers weighted by their
  probabilities, a number from 0 to the highest level. The answer also has `level`, the number
  of the most likely level, and `label`, its name. `value` and `level` can differ — a `value` of
  1.4 can come with `level` 1 — so results are sorted by `value` and grouped by `label`.
- **yesno** — the probability of yes, from 0 to 1. The skill decides from which probability on
  it counts as yes.

`choice` and `score` answers also have `confidence`, from 0 to 1, and `probabilities` — the
probability of each label, or of each level keyed by its number as a string (`"0"`, `"1"`, …).

### Limits

`decision()` works only inside an agent. An agent asks either one unnamed question or named
ones — never both, and never the same name twice. A decision agent needs TEXT and has no
`outputs` or `nested`. A variable named `decision`, `choice`, `score` or `yesno` hides the
function of that name. If the decision model cannot be reached, the run stops with the reason.

## Bundles

```jinja
{% bundle NAME %}
{% for path in find_files("PATTERN") %}
  {% agent AGENT key=path %}{{ decision(QUESTION) }}{{ decision_state_from_file(path) }}{% endagent %}
{% endfor %}
{% endbundle %}
```

Decision agents in a loop are asked one after another; inside a bundle, all their questions are
asked at once. A bundle is how one judgement is made about many items — most often one
decision agent per something. Like decision agents, bundles are used only when the user asks for
them.

The answers are read through the bundle: `NAME.AGENT[KEY]` for an agent with a `key`,
`NAME.AGENT` for one without. An agent that occurs more than once in a bundle needs a different
`key` each time. If no agent was reached — `find_files()` found nothing, for example — `NAME` is
empty, and `NAME.AGENT | default({})` reads it without stopping the run.

A bundle contains only decision agents — no instructions, stages or sub-agents — and is never
inside an agent or another bundle. Its size is not limited: every agent's text is sent to the
decision model.

## Stages

```jinja
{% stage NAME %}
INSTRUCTIONS
{% endstage %}
```

A stage is a separate part of the execution: it is carried out after everything above it, and
nothing below it is evaluated until it is finished. Most instructions need no stage; one is
needed where that separation matters:

- **A file for a later agent.** Instructions that write a temp file for an agent or a bundle
  below go in a stage. Without the stage, the agent reads the file before it is written, and the
  run stops.
- **One part per item.** The instructions a `{% for %}` produces for its items, with no agent or
  stage between them, are carried out together. A stage inside the loop makes each item a
  separate part, finished before the next.
- **Replaceable instructions.** A skill that extends another can replace only the other's
  `{% block %}`s; a stage that a variant may replace sits inside a `{% block %}`.

An agent or bundle placed inside a stage is carried out before the stage itself, so agents and
bundles go between stages, not inside them.

## Files

Files are those of the run's working directory — the directory the user names when starting
the run — and paths are relative to it. Which files may be listed or read is set by the user
for the run, outside the skill. A file that is not allowed, a missing file, an absolute path, a
path leading out of the working directory, a symbolic link, or a file with several hard links
stops the run. `decision_state_from_file()` is used only when the user explicitly asked for it: the
special case of a file that must not enter the harness's context and is judged outside its
session, by the decision model.

Data the run keeps for its own later steps goes into a `temp_file()`, in a temporary folder, not
into the project. Its path is the one exception to the rules above.

- `find_files(PATTERN)` returns the sorted paths of the files the glob `PATTERN`. Its list stays
  the same for the rest of the run, and it never lists temp files. `find_files(PATTERN)[:N]`
  keeps the first N paths.
- `temp_file(NAME)` returns the path of a file reserved for the run, in a temporary folder, the
  same throughout the run; `NAME` uses letters, `_` and `-`. A stage saves a result of the
  orchestrator's work there — a command's output, for example — for an agent after the stage.
  Only a few temp files are created for one agent: many items go into one file, one item per
  line, not a file each.
- `decision_state_from_file(PATH)` inserts the text of a file as it is, up to 1 MiB of UTF-8; Jinja in
  the file is not evaluated. It works only inside an `{% agent %}` and is for a decision
  agent's TEXT — so the text reaches the decision model, never the orchestrator. Without the
  user's request, instructions and sub-agent prompts name the file, and the orchestrator or sub-agent
  reads it with its own tools. A temp file can always be read this way; the file has to stay
  in place until the run ends.

## Loops and retries

A retry is a loop over a fixed number of attempts that stops doing work once a `namespace()`
flag is set, followed by a branch for the case where no attempt succeeded:

```jinja
{% set state = namespace(done=false) %}
{% for attempt in range(N) %}{% if not state.done %}
  {% agent NAME outputs={"ok": "yes or no"} %}PROMPT{% endagent %}
  {% set state.done = NAME.ok == "yes" %}
{% endif %}{% endfor %}
{% if not state.done %}…{% endif %}
```

