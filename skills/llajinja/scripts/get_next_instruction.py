from __future__ import annotations

import abc
import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import secrets
import shlex
import string
import sys
import time
from dataclasses import dataclass
from importlib import util
from pathlib import Path
from typing import Any, NoReturn, Required, Self, TypedDict

# Will be initialized based on the current path of the script
emulator_module: Any = None

SKILL_ROOT = Path(__file__).resolve().parent.parent

LLAJINJA_WORKFLOW_PATH = "LLAJINJA_WORKFLOW_PATH"
LLAJINJA_DEFAULT_RUNTIME = "/tmp/"
LLAJINJA_RUNTIME_LOG_NAME = "runtime.log"
LLAJINJA_RUNTIME_RESULT_REL = Path("runtime_result")

# The command every asset tells the orchestrator to re-run, spelled once.
DRIVER_COMMAND = "python scripts/get_next_instruction.py"


class NextInstructionError(Exception):
    pass


class InferResultArgumentsError(Exception):
    pass


class TemplateLoadError(Exception):
    """The skill could not be loaded, scanned or rendered. Carries an author-facing message."""


class SkillMissingError(Exception):
    """No skill answers to the name this run was started with. Carries the search that failed."""


class EmulatorLoadError(Exception):
    """emulator.py could not be imported at all. Carries the missing dependency's name."""


# ---------------------------------------------------------------------------
# agent_instruction templates
# ---------------------------------------------------------------------------


def _internal_asset(name: str) -> str:
    # Security note: `name` must NOT be from the user or AI input, no path resolution here.
    path = SKILL_ROOT / "assets" / name
    try:
        return path.read_text()
    except OSError as exc:
        raise NextInstructionError(f"Missing skill asset: {path} ({exc})") from exc


PROMPT_REPORT_SESSION_ID = _internal_asset("command_stage_1/session_id.txt")
PROMPT_GET_WORKDIR = _internal_asset("command_stage_1/get_workdir.txt")
PROMPT_ASK_VARIABLES = _internal_asset("command_stage_2/ask_variables.txt")
PROMPT_ASK_VARIABLE_ITEM = _internal_asset("command_stage_2/ask_variable_item.txt")
PROMPT_EXECUTE_STAGE = _internal_asset("command_stage_3/execute_stage.txt")
PROMPT_EXECUTE_SUB_AGENT = _internal_asset("command_stage_3/execute_sub_agent.txt")
PROMPT_EXECUTE_NESTED_SKILL = _internal_asset(
    "command_stage_3/execute_nested_skill.txt"
)
PROMPT_EXECUTE_NESTED_SKILL_CONTEXT = _internal_asset(
    "command_stage_3/execute_nested_skill_context.txt"
)
PROMPT_FINISHED = _internal_asset("command_stage_3/finished.txt")
PROMPT_INCORRECT_SUB_AGENT_RESULTS = _internal_asset(
    "command_stage_3/incorrect_sub_agent_results.txt"
)

# Fallbacks: what the driver prints when it cannot reach a stage at all, because no skill
# answers to the name, the skill will not render, or emulator.py will not import.
PROMPT_FALLBACK_SKILL_MISSING = _internal_asset("fallback_cases/skill_missing.txt")
PROMPT_FALLBACK_TEMPLATE_LOAD_FAILED = _internal_asset(
    "fallback_cases/template_load_failed.txt"
)
PROMPT_FALLBACK_EMULATOR_UNIMPORTABLE = _internal_asset(
    "fallback_cases/emulator_unimportable.txt"
)


# ---------------------------------------------------------------------------
# Runtime log records
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class RuntimeLogRecord:
    msg: str
    time: float = dataclasses.field(default_factory=time.time)
    agent_instruction: str | None = None
    payload: dict | None = None

    @classmethod
    def load(cls, log_path: Path) -> list[RuntimeLogRecord]:
        if not log_path.exists():
            return []
        records = []
        for log_line in log_path.read_text().strip().splitlines():
            if not (log_line := log_line.strip()):
                continue
            try:
                records.append(RuntimeLogRecord(**json.loads(log_line)))
            except (json.JSONDecodeError, TypeError) as exc:
                raise NextInstructionError(
                    f"Corrupt runtime log entry: {exc!r} — line: {log_line!r}"
                ) from exc
        return records

    @classmethod
    def prompt(cls, text: str) -> RuntimeLogRecord:
        return RuntimeLogRecord(msg="prompt", agent_instruction=text)

    @classmethod
    def payloads(cls, records: list[RuntimeLogRecord]) -> list[dict]:
        return [record.payload or {} for record in records if record.msg == cls.msg]

    @classmethod
    def first_payload(cls, records: list[RuntimeLogRecord]) -> dict | None:
        payloads = cls.payloads(records)
        return payloads[0] if payloads else None


@dataclass(kw_only=True)
class RuntimeLogCreated(RuntimeLogRecord):
    class Payload(TypedDict, total=False):
        skill_name: str
        workdir_path: str

    payload: RuntimeLogCreated.Payload
    msg: str = "runtime-log-created"

    @classmethod
    def skill_name(cls, records: list[RuntimeLogRecord]) -> str | None:
        # The first record: the skill a run executes is decided once and never again.
        return (cls.first_payload(records) or {}).get("skill_name")

    @classmethod
    def workdir(cls, records: list[RuntimeLogRecord]) -> str | None:
        # The last one written: a later call may be the one that supplied it.
        for payload in reversed(cls.payloads(records)):
            if payload.get("workdir_path"):
                return payload["workdir_path"]
        return None


@dataclass(kw_only=True)
class RuntimeLogVariables(RuntimeLogRecord):
    class Payload(TypedDict):
        values: Required[dict[str, str]]

    payload: RuntimeLogVariables.Payload
    msg: str = "variables-provided"

    @classmethod
    def merged(cls, records: list[RuntimeLogRecord]) -> dict[str, str]:
        values: dict[str, str] = {}
        for payload in cls.payloads(records):
            values.update(payload.get("values") or {})
        return values


@dataclass(kw_only=True)
class RuntimeLogStepDispatch(RuntimeLogRecord):
    class Payload(TypedDict):
        kind: Required[str]
        step_name: Required[str]
        source_reference: Required[str]
        iteration: Required[int]
        result_id: Required[str]
        expected_output: Required[dict[str, str]]

    payload: RuntimeLogStepDispatch.Payload
    msg: str = "step-dispatch"


@dataclass(kw_only=True)
class RuntimeLogStepResult(RuntimeLogRecord):
    class Payload(TypedDict):
        iteration: Required[int]
        args: Required[dict[str, Any]]
        result_file: str | None

    payload: RuntimeLogStepResult.Payload
    msg: str = "step-result"


@dataclass(kw_only=True)
class RuntimeLogExecutionFinished(RuntimeLogRecord):
    msg: str = "llajinja-executed"


# ---------------------------------------------------------------------------
# Runtime log helpers
# ---------------------------------------------------------------------------


def locked_write_record(ctx: RequestContext, record: dict | RuntimeLogRecord) -> None:
    if isinstance(record, RuntimeLogRecord):
        record = dataclasses.asdict(record)
    ctx.runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ctx.runtime_log_path, "a") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            fd.write(json.dumps(record) + "\n")
            fd.flush()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def record_supplied_variables(ctx: RequestContext) -> None:
    values: dict[str, str] = {}
    for item in ctx.parsed.var or []:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            raise NextInstructionError(
                f"--var expects name=value, got: {item!r}. Nothing was recorded."
            )
        values[name.strip()] = value
    if values:
        locked_write_record(ctx, RuntimeLogVariables(payload={"values": values}))


def output_json_schema(
    name: str, outputs_spec: dict[str, str], indent: str = "    "
) -> str:
    properties = {}
    for key, text in outputs_spec.items():
        text = text.strip("\\'\"")
        properties[key] = {"type": "string", "description": text}
    schema = json.dumps(
        {
            "title": f"{name} outputs",
            "type": "object",
            "properties": properties,
            "required": list(outputs_spec),
            "additionalProperties": False,
        },
        indent=2,
    )
    return "\n".join(indent + line for line in schema.splitlines())


# ---------------------------------------------------------------------------
# Emulator + shared step helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def template_errors(skill: str):
    module = import_emulator()
    try:
        yield
    except module.TemplateLoadError as exc:
        raise TemplateLoadError(str(exc)) from exc
    except Exception as exc:
        raise TemplateLoadError(f"{skill}: {type(exc).__name__}: {exc}") from exc


def import_emulator() -> Any:
    global emulator_module
    if emulator_module is not None:
        return emulator_module
    script_dir = Path(__file__).resolve().parent
    spec = util.spec_from_file_location("emulator", script_dir / "emulator.py")
    assert spec and spec.loader
    emulator: Any = util.module_from_spec(spec)
    sys.modules[spec.name] = emulator
    try:
        spec.loader.exec_module(emulator)
    except ImportError as exc:
        raise EmulatorLoadError(exc.name or str(exc)) from exc
    emulator_module = emulator
    return emulator


def result_is_complete(values: Any, output_keys: list[str]) -> bool:
    if not output_keys:
        return True
    return isinstance(values, dict) and all(key in values for key in output_keys)


def demand_step_result(ctx: RequestContext, payload: dict) -> NoReturn:
    step_name = payload["step_name"]
    print(
        PROMPT_INCORRECT_SUB_AGENT_RESULTS.format(
            driver_command=DRIVER_COMMAND,
            agent_name=step_name,
            result_path=ctx.result_file(payload["result_id"]),
            output_schema=output_json_schema(step_name, payload["expected_output"]),
            session_id=ctx.session_id,
        )
    )
    raise InferResultArgumentsError()


def create_runtime_log(ctx: RequestContext) -> bool:
    ctx.runtime_path.mkdir(parents=True, exist_ok=True)
    ctx.result_dir.mkdir(parents=True, exist_ok=True)

    if ctx.runtime_log_path.exists():
        return False

    ctx.runtime_log_path.touch()

    return True


def resolved_within(base_path: Path, relative_path: str | Path) -> Path:
    full_path = base_path / relative_path
    if not full_path.resolve().is_relative_to(base_path.resolve()):
        raise NextInstructionError(
            f"Refusing the path outside of {base_path}: {relative_path}"
        )
    return full_path.resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Get the next instruction for the LLAJinja run."
    )
    parser.add_argument(
        "args",
        nargs="*",
        help="Optional arguments.",
    )
    parser.add_argument("--llajinja-session", default=None)
    parser.add_argument("--skill", default=None)
    parser.add_argument("--var", action="append", default=None, metavar="NAME=VALUE")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


@dataclass
class RequestContext:
    parsed: argparse.Namespace
    session_id: str
    runtime_base_path: Path
    runtime_path: Path
    runtime_log_path: Path
    # True when the orchestrator carried the id forward via --llajinja-session
    adopted: bool = False
    records: list[RuntimeLogRecord] | None = None

    @classmethod
    def create(cls, parsed: argparse.Namespace) -> Self:
        """Resolve the workspace this invocation addresses, from the command line."""
        session_id = parsed.llajinja_session or cls.generate_session_id()
        base = os.environ.get(LLAJINJA_WORKFLOW_PATH, LLAJINJA_DEFAULT_RUNTIME)
        assert base
        base_path = Path(base).resolve()
        runtime_path = resolved_within(base_path / "llajinja", session_id)
        return cls(
            parsed=parsed,
            session_id=session_id,
            adopted=bool(parsed.llajinja_session),
            runtime_base_path=base_path,
            runtime_path=runtime_path,
            runtime_log_path=resolved_within(runtime_path, LLAJINJA_RUNTIME_LOG_NAME),
        )

    @property
    def result_dir(self) -> Path:
        return self.runtime_path / LLAJINJA_RUNTIME_RESULT_REL

    def result_file(self, result_id: str) -> Path:
        return resolved_within(self.result_dir, f"{result_id}.json")

    @staticmethod
    def generate_session_id() -> str:
        return f"{int(time.time())}_{RequestContext.random_token()}"

    @staticmethod
    def random_token() -> str:
        return "".join(secrets.choice(string.ascii_letters) for _ in range(6))


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------


class StageHandler(abc.ABC):
    # The stage this one hands to once its own work is recorded, None for the stage that
    # ends a run. Handlers are defined successor-first so each can name its own.
    next_stage: type[StageHandler] | None = None

    def __init__(self, ctx: RequestContext):
        self.ctx = ctx

    @abc.abstractmethod
    def handle(self) -> RuntimeLogRecord: ...

    def trigger_next(self) -> RuntimeLogRecord:
        if self.next_stage is None:
            raise NextInstructionError(f"{type(self).__name__} ends a run")
        return self.next_stage(self.ctx).handle()


class ExecuteHandler(StageHandler):
    next_stage = None

    def handle(self) -> RuntimeLogRecord:
        ctx = self.ctx
        records = (
            ctx.records
            if ctx.records is not None
            else RuntimeLogRecord.load(ctx.runtime_log_path)
        )
        skill_name = RuntimeLogCreated.skill_name(records) or ""
        module = import_emulator()

        answers, dispatched, unanswered = self.replay_records(records)
        with template_errors(skill_name):
            emulator = module.Emulator.load(
                skill_name, variables=RuntimeLogVariables.merged(records)
            )
            result = emulator.run(answers)

        self.check_replay(dispatched, result)

        if result.pending is None:
            return RuntimeLogExecutionFinished(agent_instruction=PROMPT_FINISHED)
        if unanswered is not None:
            demand_step_result(ctx, unanswered)
        return self.emit_step_dispatch(
            ctx, result.pending, len(records), RuntimeLogCreated.workdir(records)
        )

    @staticmethod
    def replay_records(
        records: list[RuntimeLogRecord],
    ) -> tuple[list[dict], list[str], dict | None]:
        results = {
            payload.get("iteration"): payload.get("args")
            for payload in RuntimeLogStepResult.payloads(records)
        }
        answers: list[dict] = []
        dispatched: list[str] = []
        for payload in RuntimeLogStepDispatch.payloads(records):
            step_name = payload.get("step_name")
            assert step_name, "Inconsistent runtime log, no step name found"
            dispatched.append(step_name)

            expected = payload.get("expected_output") or {}
            if not expected:
                # A step declaring no outputs is done by the driver running again.
                answers.append({})
                continue
            values = results.get(payload.get("iteration"))
            if not (result_is_complete(values, list(expected)) and values):
                # Stop at a result short of a declared key, so the render suspends there.
                return answers, dispatched, payload
            answers.append(values)
        return answers, dispatched, None

    @staticmethod
    def check_replay(dispatched: list[str], result) -> None:
        for expected, step in zip(dispatched, result.answered):
            if step.step_name != expected:
                raise TemplateLoadError(
                    f"the log says step {expected!r} ran here, but the skill now "
                    f"produces {step.step_name!r}. Nothing of a skill is copied when a "
                    "run starts, so the overwhelmingly likely cause is that it was "
                    "edited since this run began. Start a new run; this one can no "
                    "longer be replayed."
                )

    @staticmethod
    def emit_step_dispatch(
        ctx: RequestContext,
        step,
        iteration: int,
        workdir: str | None,
    ) -> RuntimeLogStepDispatch:
        kind = import_emulator().StepKind
        result_id = RequestContext.random_token()
        result_path = ctx.result_file(result_id)

        if step.kind == kind.STAGE:
            instruction = PROMPT_EXECUTE_STAGE.format(
                driver_command=DRIVER_COMMAND,
                stage_name=step.step_name,
                prompt=step.prompt.strip(),
                session_id=ctx.session_id,
                workdir=workdir,
            )
        elif step.kind == kind.SUB_AGENT:
            instruction = PROMPT_EXECUTE_SUB_AGENT.format(
                driver_command=DRIVER_COMMAND,
                agent_name=step.step_name,
                prompt=step.prompt.strip(),
                output_schema=output_json_schema(step.step_name, step.outputs_spec),
                result_path=result_path,
                session_id=ctx.session_id,
                workdir=workdir,
            )
        elif step.kind == kind.NESTED_SKILL:
            extra_context = step.prompt.strip()
            instruction = PROMPT_EXECUTE_NESTED_SKILL.format(
                driver_command=DRIVER_COMMAND,
                step_name=step.step_name,
                skill_name=shlex.quote(step.nested.skill_name.split()[0]),
                var_args=" ".join(
                    f"--var {shlex.quote(f'{name}={value}')}"
                    for name, value in step.nested.input.items()
                ),
                output_schema=output_json_schema(step.step_name, step.outputs_spec),
                result_path=result_path,
                session_id=ctx.session_id,
                workdir=workdir,
                extra_context=(
                    PROMPT_EXECUTE_NESTED_SKILL_CONTEXT.format(
                        extra_context=extra_context
                    )
                    if extra_context
                    else ""
                ),
            )
        else:
            raise NextInstructionError(
                f"Unknown step kind from the emulator: {step.kind!r}"
            )

        return RuntimeLogStepDispatch(
            agent_instruction=instruction,
            payload={
                "kind": step.kind,
                "step_name": step.step_name,
                "source_reference": step.source_reference,
                "iteration": iteration,
                "result_id": result_id,
                "expected_output": dict(step.outputs_spec),
            },
        )


class InferResultFromPreviousStepHandler(StageHandler):
    next_stage = ExecuteHandler

    def handle(self) -> RuntimeLogRecord:
        ctx = self.ctx
        records = RuntimeLogRecord.load(ctx.runtime_log_path)
        dispatches = RuntimeLogStepDispatch.payloads(records)
        answered = {
            payload.get("iteration")
            for payload in RuntimeLogStepResult.payloads(records)
        }
        for payload in dispatches:
            assert "result_id" in payload
            assert "iteration" in payload
            assert "expected_output" in payload

        for payload in dispatches:
            if payload["iteration"] in answered:
                continue
            if not payload["expected_output"]:
                continue
            path = ctx.result_file(payload["result_id"])
            try:
                values = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                demand_step_result(ctx, payload)
            if not result_is_complete(values, list(payload["expected_output"])):
                demand_step_result(ctx, payload)
            locked_write_record(
                ctx,
                RuntimeLogStepResult(
                    payload={
                        "iteration": payload["iteration"],
                        "args": values,
                        "result_file": path.name,
                    },
                ),
            )
        return self.trigger_next()


class AskVariablesHandler(StageHandler):
    next_stage = InferResultFromPreviousStepHandler

    def handle(self) -> RuntimeLogRecord:
        ctx = self.ctx
        records = RuntimeLogRecord.load(ctx.runtime_log_path)
        known = RuntimeLogVariables.merged(records)
        skill_name = RuntimeLogCreated.skill_name(records) or ""

        module = import_emulator()
        with template_errors(skill_name):
            declared = module.Emulator.get_input_variables(skill_name)

        missing = []
        for variable in declared.values():
            if variable.required and variable.name not in known:
                missing.append(variable)

        if not missing:
            # Every declared input has a value, move on.
            return self.trigger_next()

        variables_block = "\n".join(
            PROMPT_ASK_VARIABLE_ITEM.format(
                name=variable.name,
                template=variable.declared_in_reference,
                requirement="required",
            ).strip()
            for variable in missing
        )
        example_command = " ".join(
            [DRIVER_COMMAND, f"--llajinja-session {ctx.session_id}"]
            + [f'--var {shlex.quote(variable.name)}="<value>"' for variable in missing]
        )
        return RuntimeLogRecord.prompt(
            PROMPT_ASK_VARIABLES.format(
                session_id=ctx.session_id,
                variables_block=variables_block,
                example_command=example_command,
            )
        )


class VerifySkillHandler(StageHandler):
    next_stage = AskVariablesHandler

    def handle(self) -> RuntimeLogRecord:
        records = RuntimeLogRecord.load(self.ctx.runtime_log_path)
        skill_name = RuntimeLogCreated.skill_name(records) or ""
        module = import_emulator()
        try:
            loader = module.SkillLoader(
                module.Emulator.get_skills_dir(), entry_skill_name=skill_name
            )
            found = loader.get_skill_path(skill_name)
        except module.SkillNotFound as exc:
            raise SkillMissingError(str(exc)) from exc
        if found is None:
            raise SkillMissingError(loader.get_not_found_message(skill_name))
        return self.trigger_next()


class AskWorkdirHandler(StageHandler):
    next_stage = VerifySkillHandler

    def handle(self) -> RuntimeLogRecord:
        ctx = self.ctx
        records = RuntimeLogRecord.load(ctx.runtime_log_path)
        # Log-driven: once the workdir has been captured (recorded), move on.
        if RuntimeLogCreated.workdir(records) is not None:
            return self.trigger_next()

        if ctx.parsed.args:
            # This run supplied the workdir path — record it, then move on.
            locked_write_record(
                ctx, RuntimeLogCreated(payload={"workdir_path": ctx.parsed.args[0]})
            )
            return self.trigger_next()

        return RuntimeLogRecord.prompt(
            PROMPT_GET_WORKDIR.format(
                driver_command=DRIVER_COMMAND, session_id=ctx.session_id
            )
        )


class SessionSetupHandler(StageHandler):
    next_stage = AskWorkdirHandler

    def handle(self) -> RuntimeLogRecord:
        ctx = self.ctx
        records = RuntimeLogRecord.load(ctx.runtime_log_path)
        # Resolved before the runtime directory exists, so a call naming no skill at all
        # leaves no half-made run behind.
        recorded = RuntimeLogCreated.skill_name(records)
        skill_name = recorded if recorded is not None else self.require_skill()
        create_runtime_log(ctx)

        if recorded is None:
            payload: RuntimeLogCreated.Payload = {"skill_name": skill_name}
            if ctx.parsed.args:
                payload["workdir_path"] = ctx.parsed.args[0]
            locked_write_record(ctx, RuntimeLogCreated(payload=payload))

        record_supplied_variables(ctx)

        if ctx.adopted:
            return self.trigger_next()
        return RuntimeLogRecord.prompt(
            PROMPT_REPORT_SESSION_ID.format(
                driver_command=DRIVER_COMMAND,
                session_id=ctx.session_id,
                skill_name=skill_name,
            )
        )

    def require_skill(self) -> str:
        given = self.ctx.parsed.skill
        if not given:
            # The asset reads the blank name as "no --skill was passed at all".
            raise SkillMissingError("")
        module = import_emulator()
        try:
            reference = module.SkillLoader.get_normalised_reference(given)
        except module.SkillNotFound as exc:
            raise SkillMissingError(str(exc)) from exc
        if not module.SkillLoader.is_skill_name(reference):
            raise SkillMissingError(
                f"--skill takes the name of an installed skill, not a path: {given!r}"
            )
        return reference


def main():
    parsed = parse_args()
    ctx = RequestContext.create(parsed)
    try:
        record = SessionSetupHandler(ctx).handle()
    except InferResultArgumentsError:
        return
    except SkillMissingError as exc:
        print(
            PROMPT_FALLBACK_SKILL_MISSING.format(
                driver_command=DRIVER_COMMAND, problem=exc
            )
        )
        return
    except TemplateLoadError as exc:
        print(PROMPT_FALLBACK_TEMPLATE_LOAD_FAILED.format(error=exc))
        return
    except EmulatorLoadError as exc:
        print(
            PROMPT_FALLBACK_EMULATOR_UNIMPORTABLE.format(
                module_name=exc,
                executable=sys.executable,
                driver_command=DRIVER_COMMAND,
            )
        )
        return
    locked_write_record(ctx, record)
    print(record.agent_instruction)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
