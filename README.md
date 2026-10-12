# LLAJinja

LLAJinja is a Skill that runs other Skills written as Jinja templates. No extensions or plugins required. Works in any Harness or AI dev tool that supports skill loading (Claude Code, OpenCode, Codex, Pi ...) 

![image](https://github.com/user-attachments/assets/c34e3391-60d7-4a78-8415-48f335fe3e35)


⚠️ Work in progress: this library is under active development. Feedback is appreciated.

## Features:

### Variables

Declare the variables your Skill needs with `input.<name>`. If a value isn't provided when the skill runs, LLAJinja asks the harness to prompt the user for it. 

![Variables demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/variables.svg)

### Stages

Split a multi-step skill into smaller, sequential steps. Use them when one step depends on the result of another and you want the agent to work through them one at a time in one session:

```jinja
{% stage name %} stage text {% endstage %}
```

![Stages demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/stages.svg)

### Loops
![Loops demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/loops.svg)

### Sub-agents
An `agent` block asks the harness to spawn a new agent: 
```jinja
{% agent name input={} outputs={} nested="optional-nested-skill" %} 
Agent instructions ... 
{% endagent %}
```
Parameters: 
- `input`: a mapping of variables passed to the sub-agent (or to its nested skill). 
- `outputs`: a mapping of output keys to human-readable descriptions. LLAJinja turns this into the JSON schema the agent must return, and each key can then be referenced later in the template as `name.output_key`. 
- `nested` *(optional)*: a Jinja-templated skill that the sub-agent runs in its own LLAJinja session.

![Sub-agents demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/sub-agents.svg)

### Nested Jinja skills
![Nested Jinja skills demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/nested.svg)

### Workflows
Sub-agents can be chained together and combined in control flow to build workflows. The state of each agent is saved to the runtime log, so a workflow can be resumed or replayed as many times as needed. 

![Workflows demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/workflow.svg)

### Composing skill from multiple sources
Jinja's built-in `extends`and `include` tags are supported, so you can assemble a skill from multiple template files.

![Composing demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/composing.svg)

### Decision model

![Decision model demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/tech-debt.svg)

When the template reaches a decision agent, the driver sends that agent's body and its
questions straight to the decision model and continues rendering with the answers.

An `{% agent %}` becomes a decision agent when its body calls `decision()`. The rest of
the body is what the questions are about:

```jinja
{% agent triage %}
  {{ decision(choice("Which team owns this ticket?", ["billing", "auth", "other"])) }}
  {{ input.ticket }}
{% endagent %}

{% if triage.value == "billing" %} ... {% endif %}
```

With one question, the answer is bound to the agent's name (`triage.value`). With named
questions, `decision(level=score(...), kind=choice(...))`, each answer has its own name
(`audit.level.value`, `audit.kind.value`).

#### Question types

- `choice("Which team?", ["billing", "auth", "other"])`: picks one of the labels.
- `score("How urgent?", ["can wait", "today", "now"])`: rates on a scale from first to last level.
- `yesno("Is it spam?")`: answers yes or no.

To ask many agents at once, for example one per file, wrap them in
`{% bundle NAME %}…{% endbundle %}` and give each one a distinct `key=`.

#### Working with files

- `find_files("src/**/*.py")`: lists the files in the working directory that match.
- `decision_state_from_file(path)`: gives a file's text to a decision agent.
- `temp_file("notes")`: a scratch file of this run, for passing text to a later decision agent.

File contents can be read only inside a decision agent. They go to the decision model and
never load into the harness session.

Reading files is only possible when the working directory has a `llajinja.toml` that
allows them:

```toml
[read]
allow = ["src/**/*.py", "docs/*.md"]
```

Decision agents need the optional `typesafe-sdk` package and its API key in the driver's
Python environment.

Decision agents can also be used in the middle of a harness session: the decision model
reads the files instead of the session, which can save tokens, time and cost.

![Decision model benchmark](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/bench_comparison.svg)


## Get started

1. **Install Jinja2.** LLAJinja requires the `Jinja2` Python library. Either make sure it's available on your `PATH`, or point to a Python interpreter that has it installed in your prompt, `AGENTS.md`, or `SKILL.md`.
2. **Add LLAJinja to your skills folder.** Copy it manually or install it via npx: <br>
`npx skills add https://github.com/electronick1/LLAJinja --skill llajinja`
3. **Run a templated skill.** Ask your agent: `llajinja run your-skill`

### Decision models (optional)

Skills with decision agents also need the `typesafe-sdk` package in the same Python environment as Jinja2.
Set `TYPESAFE_API_KEY` in the environment the harness runs in, so the driver can reach the decision model.

### Creating jinja skills

The `llajinja-creator` skill writes jinja skills that `llajinja` executes, for example:
`/llajinja-creator write a skill that researches each planet of the solar system with a sub-agent and compares them`

☝️ Jinja's `ImmutableSandboxedEnvironment` is not a full security boundary, so treat templates, especially AI-written ones, as untrusted: verify them before running and run your harness in a sandbox.

> **Note:** LLAJinja currently discovers skills only in the same `skills` folder it lives in. This means you can't mix global and project-level templated skills yet.

## How it works
The idea of driving an agent harness through a skill comes from LLAssemblyCLI and is described in [this article](https://olegivye.com/#/article/llassembly-pydantic-monty).
LLAJinja applies this to Jinja templates: it keeps a runtime log of each template's current state and replays it whenever the harness asks for the next step.
Execution state is stored in `/tmp/llajinja` by default. To use a different location, set the `LLAJINJA_WORKFLOW_PATH` environment variable.
