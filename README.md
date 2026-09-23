# LLAJinja

LLAJinja is a skill that runs other skills written as Jinja templates. No extensions or plugins required. Works in any Harness or AI dev tool that supports skill loading (Claude Code, OpenCode, Codex, Pi ...) 

![image](https://github.com/user-attachments/assets/c34e3391-60d7-4a78-8415-48f335fe3e35)


⚠️ Work in progress: this library is under active development. Feedback is appreciated.

## Features:

### Variables

Declare the variables your skill needs with `input.<name>`. If a value isn't provided when the skill runs, LLAJinja asks the harness to prompt the user for it. 

![Variables demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/variables.svg)

### Stages

Split a multi-step skill into smaller, sequential steps. Use them when one step depends on the result of another and you want the agent to work through them one at a time in one session. 

Stages add a new block type to your Jinja templates: 
```jinja
{% stage name %} stage text {% endstage %}
```

![Stages demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/stages.svg)

### Loops
![Loops demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/loops.svg)

### Sub-agents
An `agent` block asks the harness to spawn a new agent: 
```jinja
{% agent name input={} output={} nested="optional-nested-skill" %} 
Agent instructions ... 
{% endagent %}
```
Parameters: 
- `input`: a mapping of variables passed to the sub-agent (or to its nested skill). 
- `output`: a mapping of output keys to human-readable descriptions. LLAJinja turns this into the JSON schema the agent must return, and each key can then be referenced later in the template as `name.output_key`. 
- `nested` *(optional)*: a Jinja-templated skill that the sub-agent runs in its own LLAJinja session.

![Sub-agents demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/sub-agents.svg)

### Nested Jinja skills
![Sub-agents demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/nested.svg)

### Workflows
Sub-agents can be chained together and combined in control flow to build workflows. The state of each agent is saved to the runtime log, so a workflow can be resumed or replayed as many times as needed. 

![Sub-agents demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/workflow.svg)

### Composing skill from multiple sources
Jinja's built-in `extends`and `include` tags are supported, so you can assemble a skill from multiple template files.

![Sub-agents demo](https://github.com/electronick1/LLAJinja/blob/dev/assets/readme/composing.svg)


## Get started

1. **Install Jinja2.** LLAJinja requires the `Jinja2` Python library. Either make sure it's available on your `PATH`, or point to a Python interpreter that has it installed in your prompt, `AGENTS.md`, or `SKILL.md`.
2. **Add LLAJinja to your skills folder.** Copy it manually or install it via npx.
3. **Run a templated skill.** Ask your agent: `llajinja run your-skill`

> **Note:** LLAJinja currently discovers skills only in the same `skills` folder it lives in. This means you can't mix global and project-level templated skills yet.

## How it works
The idea of driving an agent harness through a skill comes from LLAssemblyCLI and is described in [this article](https://olegivye.com/#/article/llassembly-pydantic-monty).
LLAJinja applies this to Jinja templates: it keeps a runtime log of each template's current state and replays it whenever the harness asks for the next step.
Execution state is stored in `/tmp/llajinja` by default. To use a different location, set the `LLAJINJA_WORKFLOW_PATH` environment variable.
