from __future__ import annotations

import enum
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple, Self, TypeAlias

import jinja2
from jinja2 import nodes
from jinja2.ext import Extension
from jinja2.parser import Parser
from jinja2.runtime import Context
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.visitor import NodeVisitor

SKILL_MD_NAME = "SKILL.md"
AGENT_TAG = "agent"
STAGE_TAG = "stage"
INPUT_NAME = "input"
BODY_PREFIX = "__llajinja_body_"
DEFAULT_FILTERS: frozenset[str] = frozenset({"default", "d"})

SkillName: TypeAlias = str
TemplateReference: TypeAlias = str

ReferenceNode: TypeAlias = (
    nodes.Extends | nodes.Include | nodes.Import | nodes.FromImport
)
REFERENCE_NODES: tuple[type[ReferenceNode], ...] = (
    nodes.Extends,
    nodes.Include,
    nodes.Import,
    nodes.FromImport,
)


class Reference(NamedTuple):
    template_reference: TemplateReference
    origin: str

    @staticmethod
    def get_skill_names(node: nodes.Node) -> list[str]:
        if not isinstance(node, REFERENCE_NODES):
            return []
        target = node.template
        value = target.value if isinstance(target, nodes.Const) else None
        if isinstance(value, str):
            return [value]
        if isinstance(value, (list, tuple)):
            return [item for item in value if isinstance(item, str)]
        return []

    @classmethod
    def get_references(
        cls, ast: nodes.Template, declared: TemplateReference
    ) -> list[Reference]:
        return [
            cls(template_reference, f"{declared}:{node.lineno}")
            for node in ast.find_all(REFERENCE_NODES)
            for template_reference in cls.get_skill_names(node)
        ]


ReferenceCollector: TypeAlias = Callable[[nodes.Template, str], Sequence[Reference]]


class Unset(enum.Enum):
    TOKEN = enum.auto()


UNSET = Unset.TOKEN
SkillsDir: TypeAlias = Path | None | Literal[Unset.TOKEN]


class StepKind(enum.StrEnum):
    STAGE = "stage"
    SUB_AGENT = "sub_agent"
    NESTED_SKILL = "nested_skill"


class TemplateLoadError(Exception):
    @classmethod
    def from_jinja_error(cls, exc: jinja2.TemplateError) -> TemplateLoadError:
        if isinstance(exc, jinja2.TemplateSyntaxError):
            skill = exc.name or exc.filename or "<skill>"
            return cls(f"{skill}:{exc.lineno}: {exc.message}")
        return cls(str(exc))


class SkillNotFound(TemplateLoadError):
    pass


class Suspend(BaseException):
    def __init__(self, step: Step) -> None:
        super().__init__(step.step_name)
        self.step = step


@dataclass
class InputVariable:
    name: str
    required: bool
    default: str | None
    declared_in_reference: TemplateReference


@dataclass
class NestedSkill:
    skill_name: SkillName
    input: dict[str, str]


@dataclass
class Step:
    kind: StepKind
    step_name: str
    source_reference: TemplateReference
    prompt: str
    outputs_spec: dict[str, str] = field(default_factory=dict)
    nested: NestedSkill | None = None


@dataclass
class RunResult:
    answered: list[Step]
    pending: Step | None
    transcript: str


def get_default_skills_dir() -> Path | None:
    parent = Path(__file__).resolve().parent.parent.parent
    return parent if parent.name == "skills" else None


def resolved_within_skill_dir(base_path: Path, relative_path: Path) -> Path:
    resolved = relative_path.resolve()
    if not resolved.is_relative_to(base_path.resolve()):
        raise SkillNotFound(f"Can't look for skills outside of {base_path}")
    return resolved


class SkillLoader(jinja2.BaseLoader):
    def __init__(
        self,
        root: Path | None = None,
        *,
        entry_skill_name: SkillName | None = None,
    ) -> None:
        if root is None:
            raise SkillNotFound("Skill directory is not set or not found.")
        if entry_skill_name is None:
            raise SkillNotFound("No skill provided to be run under LLAJinja")
        self.root = root
        self.entry_skill_name = entry_skill_name

    @staticmethod
    def get_normalised_reference(reference: str) -> TemplateReference:
        normalised = reference.strip().rstrip("/\\")
        if not normalised:
            raise SkillNotFound("Skill name is not defined")
        return normalised.split()[0]

    @staticmethod
    def is_skill_name(reference: TemplateReference) -> bool:
        return "/" not in reference

    def get_skill_path(self, reference: TemplateReference) -> Path | None:
        reference = self.get_normalised_reference(reference)
        bases = (
            (self.root,)
            if self.is_skill_name(reference)
            else (self.root / self.entry_skill_name, self.root)
        )
        for base in bases:
            candidate = resolved_within_skill_dir(self.root, base / reference)
            if candidate.is_dir():
                candidate = resolved_within_skill_dir(
                    self.root, candidate / SKILL_MD_NAME
                )
            if candidate.is_file():
                return candidate
        return None

    def get_source(
        self, environment: jinja2.Environment, template: str
    ) -> tuple[str, str, Callable[[], bool]]:
        reference = self.get_normalised_reference(template)
        path = self.get_skill_path(reference)
        if path is None:
            raise jinja2.TemplateNotFound(
                reference, self.get_not_found_message(reference)
            )
        return path.read_text(), reference, lambda: True

    def get_not_found_message(self, reference: TemplateReference) -> str:
        return f"nothing named {reference!r} under {self.root}."


class Optionality(NamedTuple):
    default: str | None
    required: bool


class InputScan(NodeVisitor):
    @staticmethod
    def get_input_variable_name(node: nodes.Node) -> str | None:
        name: object
        if isinstance(node, nodes.Getattr):
            base, name = node.node, node.attr
        elif isinstance(node, nodes.Getitem) and isinstance(node.arg, nodes.Const):
            base, name = node.node, node.arg.value
        else:
            return None
        if isinstance(base, nodes.Name) and base.name == INPUT_NAME:
            return name if isinstance(name, str) else None
        return None

    @staticmethod
    def get_optionality(parent: nodes.Node | None, node: nodes.Node) -> Optionality:
        filtered = isinstance(parent, nodes.Filter) and parent.name in DEFAULT_FILTERS
        if not filtered or parent.node is not node:
            return Optionality(default=None, required=True)
        given = parent.args[0] if parent.args else None
        default = str(given.value) if isinstance(given, nodes.Const) else None
        return Optionality(default=default, required=False)

    def __init__(
        self, reference: TemplateReference, found: dict[str, InputVariable]
    ) -> None:
        self.reference = reference
        self.found = found
        self.references: list[Reference] = []

    def record_variable(self, name: str, optionality: Optionality) -> None:
        existing = self.found.get(name)
        if existing is None:
            self.found[name] = InputVariable(
                name=name,
                required=optionality.required,
                default=optionality.default,
                declared_in_reference=self.reference,
            )
            return
        existing.required = existing.required or optionality.required
        if existing.default is None:
            existing.default = optionality.default

    def visit_With(self, node: nodes.With, parent: nodes.Node | None = None) -> None:
        if not any(
            isinstance(target, nodes.Name) and target.name == INPUT_NAME
            for target in node.targets
        ):
            self.generic_visit(node)
            return
        for value in node.values:
            self.visit(value, node)

    def generic_visit(self, node: nodes.Node, parent: nodes.Node | None = None) -> None:
        if (name := self.get_input_variable_name(node)) is not None:
            self.record_variable(name, self.get_optionality(parent, node))
        for template_reference in Reference.get_skill_names(node):
            self.references.append(
                Reference(template_reference, f"{self.reference}:{node.lineno}")
            )
        for child in node.iter_child_nodes():
            self.visit(child, node)


class SkillEnvironment(ImmutableSandboxedEnvironment):
    llajinja_entry_skill_name: SkillName
    llajinja_run: Run | None

    def __init__(
        self, loader: SkillLoader, *, entry_skill_name: SkillName = "<skill>"
    ) -> None:
        super().__init__(
            loader=loader,
            extensions=[LLAJinjaExtension],
            undefined=jinja2.StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
        self.skills = loader
        self.llajinja_entry_skill_name = entry_skill_name
        self.llajinja_run = None

    def walk_skills(self, collect: ReferenceCollector) -> None:
        entry_skill_name = self.llajinja_entry_skill_name
        visited: set[TemplateReference] = set()
        pending = [Reference(entry_skill_name, entry_skill_name)]
        while pending:
            reference = pending.pop(0)
            declared = SkillLoader.get_normalised_reference(
                reference.template_reference
            )
            if declared in visited:
                continue
            visited.add(declared)
            try:
                source, _, _ = self.skills.get_source(self, declared)
                ast = self.parse(source, declared, declared)
            except jinja2.TemplateNotFound as exc:
                raise SkillNotFound(f"{reference.origin}: {exc.message}") from exc
            except SkillNotFound as exc:
                raise SkillNotFound(f"{reference.origin}: {exc}") from exc
            except jinja2.TemplateSyntaxError as exc:
                raise TemplateLoadError.from_jinja_error(exc) from exc
            pending.extend(collect(ast, declared))


class LLAJinjaExtension(Extension):
    tags = {AGENT_TAG, STAGE_TAG}

    def __init__(self, environment: jinja2.Environment) -> None:
        super().__init__(environment)
        self.depth = 0
        self.captures = 0

    def preprocess(
        self, source: str, name: str | None, filename: str | None = None
    ) -> str:
        lines = source.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            return source
        for index in range(1, len(lines)):
            if lines[index].strip() in {"---", "..."}:
                return "\n" * (index + 1) + "".join(lines[index + 1 :])
        return source

    def parse(self, parser: Parser) -> list[nodes.Node] | nodes.Node:
        token = next(parser.stream)
        parser_func = {STAGE_TAG: self.parse_stage, AGENT_TAG: self.parse_agent}
        if parser_func := parser_func.get(token.value):
            return parser_func(parser, token.lineno)

        raise RuntimeError("Parser not found for unknown token")

    def parse_stage(self, parser: Parser, lineno: int) -> nodes.Node:
        target = parser.parse_assign_target(name_only=True)
        body = parser.parse_statements((f"name:end{STAGE_TAG}",), drop_needle=True)
        call = self.call_method(
            "_dispatch_stage",
            [
                nodes.Const(target.name, lineno=lineno),
                nodes.Const(parser.name or "<skill>", lineno=lineno),
            ],
        )
        return nodes.CallBlock(call, [], [], body, lineno=lineno)

    def parse_agent(self, parser: Parser, lineno: int) -> list[nodes.Node]:
        if self.depth:
            parser.fail(
                "an {% agent %} block cannot be nested inside another one: the inner "
                "block is part of the outer block's prompt, so it would never be "
                "dispatched. Give it an {% agent %} of its own outside this one.",
                lineno,
            )
        target = parser.parse_assign_target(name_only=True)
        kwargs: list[nodes.Keyword] = []
        while parser.stream.current.type != "block_end":
            parser.stream.skip_if("comma")
            key = parser.stream.expect("name").value
            parser.stream.expect("assign")
            kwargs.append(nodes.Keyword(key, parser.parse_expression(), lineno=lineno))
        self.depth += 1
        try:
            body = parser.parse_statements((f"name:end{AGENT_TAG}",), drop_needle=True)
        finally:
            self.depth -= 1
        self.captures += 1
        held = nodes.Name(f"{BODY_PREFIX}{self.captures}", "store", lineno=lineno)
        capture = nodes.CallBlock(
            self.call_method("_capture_prompt"), [], [], body, lineno=lineno
        )
        dispatch = self.call_method(
            "_dispatch_agent",
            [
                nodes.Const(target.name, lineno=lineno),
                nodes.Name(held.name, "load", lineno=lineno),
            ],
            kwargs,
        )
        return [
            nodes.AssignBlock(held, None, [capture], lineno=lineno),
            nodes.Assign(target, dispatch, lineno=lineno),
        ]

    def _dispatch_stage(
        self,
        step_name: str,
        source_reference: TemplateReference,
        caller: Callable[[], str],
    ) -> str:
        text = caller()
        run: Run | None = getattr(self.environment, "llajinja_run", None)
        if run is None or run.capturing:
            return text
        run.dispatch(
            Step(
                kind=StepKind.STAGE,
                step_name=step_name,
                source_reference=source_reference,
                prompt=textwrap.dedent(text).strip(),
            )
        )
        return ""

    def _capture_prompt(self, caller: Callable[[], str]) -> str:
        run: Run | None = getattr(self.environment, "llajinja_run", None)
        if run is None:
            return caller()
        run.capturing += 1
        try:
            return caller()
        finally:
            run.capturing -= 1

    @jinja2.pass_context
    def _dispatch_agent(
        self,
        context: Context,
        step_name: str,
        body: str,
        outputs: Any = None,
        nested: Any = None,
        input: Any = None,
        **unknown: Any,
    ) -> dict[str, str]:
        if unknown:
            given = ", ".join(f"{key}=" for key in sorted(unknown))
            raise TemplateLoadError(
                f"{{% agent {step_name} %}} got no such keyword: {given}"
            )
        run: Run | None = getattr(context.environment, "llajinja_run", None)
        if run is None:
            raise TemplateLoadError(f"the agent block {step_name!r} ran outside a run")
        if run.capturing:
            raise TemplateLoadError(
                f"the agent block {step_name!r} was reached while another agent's "
                "prompt was being rendered, which would silently drop it."
            )
        if outputs is not None and not isinstance(outputs, Mapping):
            raise TemplateLoadError(f"outputs= must be a mapping, got {outputs!r}")
        if isinstance(input, jinja2.Undefined) or input is None:
            input = {}
        nested_run = (
            None
            if nested is None
            else NestedSkill(
                skill_name=SkillLoader.get_normalised_reference(nested),
                input={str(key): str(item) for key, item in input.items()},
            )
        )
        return run.dispatch(
            Step(
                kind=(
                    StepKind.SUB_AGENT if nested_run is None else StepKind.NESTED_SKILL
                ),
                step_name=step_name,
                source_reference=context.name or run.entry_skill_name,
                prompt=textwrap.dedent(body).strip(),
                outputs_spec={
                    str(key): str(text) for key, text in (outputs or {}).items()
                },
                nested=nested_run,
            )
        )


@dataclass
class Run:
    entry_skill_name: SkillName
    answers: list[dict[str, str]] = field(default_factory=list)
    answered: list[Step] = field(default_factory=list)
    capturing: int = 0
    prose: list[str] = field(default_factory=list)

    def take_prose(self) -> str:
        text = textwrap.dedent("".join(self.prose)).strip()
        self.prose.clear()
        return text

    def dispatch_prose(self) -> None:
        if prose := self.take_prose():
            self.dispatch(
                Step(
                    kind=StepKind.STAGE,
                    step_name=self.entry_skill_name,
                    source_reference=self.entry_skill_name,
                    prompt=prose,
                )
            )

    def dispatch(self, step: Step) -> dict[str, str]:
        self.dispatch_prose()
        index = len(self.answered)
        if index >= len(self.answers):
            raise Suspend(step)
        self.answered.append(step)
        return dict(self.answers[index])


class Emulator:
    def __init__(
        self, environment: SkillEnvironment, variables: Mapping[str, Any]
    ) -> None:
        self.environment = environment
        self.entry_skill_name = environment.llajinja_entry_skill_name
        self.variables = dict(variables)
        environment.walk_skills(Reference.get_references)
        try:
            self.template = environment.get_template(self.entry_skill_name)
        except jinja2.TemplateSyntaxError as exc:
            raise TemplateLoadError.from_jinja_error(exc) from exc

    @classmethod
    def load(
        cls,
        skill_name: SkillName,
        *,
        skills_dir: SkillsDir = UNSET,
        variables: Mapping[str, Any] | None = None,
    ) -> Self:
        entry_skill_name = SkillLoader.get_normalised_reference(skill_name)
        root = get_default_skills_dir() if skills_dir is UNSET else skills_dir
        loader = SkillLoader(root, entry_skill_name=entry_skill_name)
        environment = SkillEnvironment(loader, entry_skill_name=entry_skill_name)
        return cls(environment, variables or {})

    @staticmethod
    def get_skills_dir() -> Path | None:
        return get_default_skills_dir()

    @staticmethod
    def get_input_variables(
        skill_name: SkillName,
        *,
        skills_dir: SkillsDir = UNSET,
    ) -> dict[str, InputVariable]:
        found: dict[str, InputVariable] = {}

        def collect_variables(
            ast: nodes.Template, declared: TemplateReference
        ) -> list[Reference]:
            scan = InputScan(declared, found)
            scan.visit(ast)
            return scan.references

        entry_skill_name = SkillLoader.get_normalised_reference(skill_name)
        root = get_default_skills_dir() if skills_dir is UNSET else skills_dir
        loader = SkillLoader(root, entry_skill_name=entry_skill_name)
        environment = SkillEnvironment(loader, entry_skill_name=entry_skill_name)
        environment.walk_skills(collect_variables)
        return found

    def run(self, answers: Sequence[Mapping[str, str]] = ()) -> RunResult:
        run = Run(self.entry_skill_name, [dict(answer) for answer in answers])
        self.environment.llajinja_run = run
        rendered: list[str] = []
        try:
            for chunk in self.template.generate({INPUT_NAME: dict(self.variables)}):
                rendered.append(chunk)
                run.prose.append(chunk)
            run.dispatch_prose()
        except Suspend as suspended:
            return RunResult(run.answered, suspended.step, "".join(rendered))
        except jinja2.TemplateError as exc:
            raise TemplateLoadError.from_jinja_error(exc) from exc
        finally:
            self.environment.llajinja_run = None
        return RunResult(run.answered, None, "".join(rendered))
