from __future__ import annotations

import enum
import json
import re
import stat
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NamedTuple, NotRequired, Self, TypeAlias, TypedDict

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
BUNDLE_TAG = "bundle"
INPUT_NAME = "input"
BODY_PREFIX = "__llajinja_body_"
DEFAULT_FILTERS: frozenset[str] = frozenset({"default", "d"})
QUESTION_TYPES: frozenset[str] = frozenset({"choice", "score", "noul"})
FILE_READ_LIMIT = 1 << 20
FIND_FILES_LIMIT = 1000
SKILL_NAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
# Never readable, whatever the permissions allow: hidden files and directories, keys,
# and dependency trees.
DENIED_GLOBS = (
    "**/.*",
    "**/.*/**",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/id_*",
    "**/node_modules/**",
    "**/__pycache__/**",
)
TEMP_FILE_RE = "template_[A-Za-z0-9_-]+"

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
    DECISION = "decision"


class TemplateLoadError(Exception):
    @classmethod
    def from_jinja_error(cls, exc: jinja2.TemplateError) -> TemplateLoadError:
        if isinstance(exc, jinja2.TemplateSyntaxError):
            skill = exc.name or exc.filename or "<skill>"
            return cls(f"{skill}:{exc.lineno}: {exc.message}")
        return cls(str(exc))


class SkillNotFound(TemplateLoadError):
    pass


class ReadPolicyMissing(TemplateLoadError):
    """A skill reads files, and the run has no read permissions for its working directory."""


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


# ---------------------------------------------------------------------------
# Decision agents
# ---------------------------------------------------------------------------


class DecisionSpec(TypedDict):
    """One question, as the decision model's API takes it."""

    type: Literal["choice", "score", "noul"]
    instructions: str
    criteria: NotRequired[Mapping[str, str | None] | Sequence[str]]


def choice(
    question: str, options: Mapping[str, str | None] | Sequence[str]
) -> DecisionSpec:
    if isinstance(options, str) or not isinstance(options, (Mapping, Sequence)):
        raise TemplateLoadError(f"choice() takes labels, not {options!r}")
    if not all(isinstance(label, str) and label.strip() for label in options):
        raise TemplateLoadError(
            f"choice() takes non-empty strings as labels: {options!r}"
        )
    criteria = dict(options) if isinstance(options, Mapping) else dict.fromkeys(options)
    if len(criteria) < 2 or len(criteria) < len(options):
        raise TemplateLoadError(
            f"choice() takes at least 2 distinct labels: {options!r}"
        )
    return {"type": "choice", "instructions": question, "criteria": criteria}


def score(question: str, levels: Sequence[str]) -> DecisionSpec:
    if isinstance(levels, str) or not isinstance(levels, Sequence):
        raise TemplateLoadError(f"score() takes levels, not {levels!r}")
    if not all(isinstance(level, str) and level.strip() for level in levels):
        raise TemplateLoadError(
            f"score() takes non-empty strings as levels: {levels!r}"
        )
    if len(levels) < 2 or len(set(levels)) < len(levels):
        raise TemplateLoadError(f"score() takes at least 2 distinct levels: {levels!r}")
    return {"type": "score", "instructions": question, "criteria": list(levels)}


def yesno(question: str, yes: str | None = None, no: str | None = None) -> DecisionSpec:
    spec: DecisionSpec = {"type": "noul", "instructions": question}
    if criteria := {key: text for key, text in (("true", yes), ("false", no)) if text}:
        spec["criteria"] = criteria
    return spec


@dataclass
class AgentQuestions:
    agent: str
    questions: dict[str, DecisionSpec] = field(default_factory=dict)
    single: bool = False
    reads_state: bool = False  # the body called decision_state_from_file()


@jinja2.pass_environment
def decision(
    environment: SkillEnvironment,
    unnamed: DecisionSpec | None = None,
    /,
    **named: DecisionSpec,
) -> str:
    run: Run | None = environment.llajinja_run
    if run is None or not run.enclosing_agents:
        raise TemplateLoadError("decision() is only for the body of an {% agent %}")
    asked = run.enclosing_agents[-1]
    if unnamed is not None:
        named[asked.agent] = unnamed
        asked.single = True
    if not named or (asked.single and len(asked.questions) + len(named) > 1):
        raise TemplateLoadError(
            f"{{% agent {asked.agent} %}}: decision() takes one unnamed question, "
            "or any number of named ones"
        )
    for name, spec in named.items():
        if not isinstance(spec, Mapping) or spec.get("type") not in QUESTION_TYPES:
            raise TemplateLoadError(
                f"decision({name}=...) takes choice(), score() or yesno(): {spec!s}"
            )
        if name in asked.questions:
            raise TemplateLoadError(f"decision() asks {name!r} twice")
        asked.questions[name] = spec
    return ""


class DecisionItem(NamedTuple):
    agent: str
    key: str | int | None
    questions: dict[str, DecisionSpec]
    single: bool  # one unnamed question, whose answer binds to the agent's name
    state: str | dict[str, object]


class BundleMember:
    """What an agent's name holds inside a {% bundle %}: its answer is not there yet."""

    def __init__(self, agent: str) -> None:
        self.agent = agent

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise TemplateLoadError(
            f"{self.agent!r} is answered at {{% endbundle %}}: read it after the bundle, "
            f"as BUNDLE.{self.agent} or BUNDLE.{self.agent}[key]"
        )

    __getitem__ = __getattr__


@dataclass
class Bundle:
    """Decision agents asked in one step, a `{% bundle %}`'s or a single one. Recorded
    as `{"answers": {item: {question: answer record}}}`."""

    items: dict[str, DecisionItem] = field(default_factory=dict)
    asked: dict[str, set[str | int | None]] = field(default_factory=dict)

    def add(self, item: DecisionItem) -> None:
        item_id = item.agent if item.key is None else f"{item.agent}[{item.key}]"
        keys = self.asked.setdefault(item.agent, set())
        if item_id in self.items or (keys and None in keys | {item.key}):
            raise TemplateLoadError(
                f"{item.agent!r} is asked twice in one {{% bundle %}}; "
                "give each a distinct key="
            )
        keys.add(item.key)
        self.items[item_id] = item

    def decode(self, values: Any) -> dict[str, Any]:
        """By agent name, then key if any: its one answer, or its answers by question
        name. An answer is the model's record, its value also under `value`."""
        result: dict[str, Any] = {}
        try:
            for item_id, item in self.items.items():
                answers = {}
                for name, question in item.questions.items():
                    answer = dict(values["answers"][item_id][name])
                    kind, criteria = question["type"], question.get("criteria")
                    if kind == "score":
                        p = answer["probabilities"]
                        level = int(max(p, key=p.get))
                        answer.update(level=level, label=criteria[level])
                    answers[name] = dict(answer, value=answer[kind])
                bound = answers[item.agent] if item.single else answers
                if item.key is None:
                    result[item.agent] = bound
                else:
                    result.setdefault(item.agent, {})[item.key] = bound
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise TemplateLoadError(f"malformed decision answer: {exc!r}") from exc
        return result

    def ask(self, run: Run, step_name: str, source_reference: str) -> dict[str, Any]:
        if not self.items:
            return {}
        step = Step(
            StepKind.DECISION,
            step_name,
            source_reference,
            "",
            {"answers": "item -> question -> answer record"},
            request=self,
        )
        return self.decode(run.dispatch(step))


@dataclass
class Step:
    kind: StepKind
    step_name: str
    source_reference: TemplateReference
    prompt: str
    outputs_spec: dict[str, str] = field(default_factory=dict)
    nested: NestedSkill | None = None
    request: Bundle | None = None  # what a decision step asks
    last: bool = False  # nothing can come after it: the run ends with this step


@dataclass
class RunResult:
    answered: list[Step]
    pending: Step | None
    transcript: str
    find_files_results: dict[str, list[str]]


def get_default_skills_dir() -> Path | None:
    parent = Path(__file__).resolve().parent.parent.parent
    return parent if parent.name == "skills" else None


def resolved_within_skill_dir(base_path: Path, relative_path: Path) -> Path:
    resolved = relative_path.resolve()
    if not resolved.is_relative_to(base_path.resolve()):
        raise SkillNotFound(f"Can't look for skills outside of {base_path}")
    return resolved


def read_without_links(root: Path, relative: str) -> str:
    """The text of `root/relative` if no part of the path is a link and it is a regular
    file with a single link, of at most FILE_READ_LIMIT bytes. Checked, then read: a
    link swapped in between is not caught."""
    root = root.resolve()
    path = root / relative
    try:
        info = path.lstat()
        if path.resolve(strict=True) != path or not path.is_relative_to(root):
            reason = f"a symbolic link in the path, or not under {root}"
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            reason = "not a regular file with a single link"
        elif info.st_size > FILE_READ_LIMIT:
            reason = f"larger than {FILE_READ_LIMIT} bytes"
        else:
            return path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        reason = str(exc)
    raise TemplateLoadError(
        f"decision_state_from_file(): can't read {relative}: {reason}"
    )


@jinja2.pass_environment
def decision_state_from_file(environment: SkillEnvironment, path: str) -> str:
    """The text of a file temp_file() made, or of one under the run's working
    directory that its read permissions allow. It is only for a decision agent's
    body: the text is sent to the decision model, never into the orchestrator's
    instructions. The environment is injected by Jinja at call time (like
    find_files), so nothing template-visible holds a reference to it."""
    run = environment.llajinja_run
    if run is None or not run.enclosing_agents:
        raise TemplateLoadError(
            "decision_state_from_file() is only for the body of an {% agent %}"
        )
    # Marks the enclosing agent; _dispatch_agent rejects it unless it is a
    # decision agent (one whose body also calls decision()).
    run.enclosing_agents[-1].reads_state = True
    if not isinstance(path, str):
        raise TemplateLoadError(f"decision_state_from_file(): {path!r} is not a path")
    resolved = Path(path).resolve()
    if run.temp_dir is not None and resolved.is_relative_to(run.temp_dir):
        name = resolved.relative_to(run.temp_dir).as_posix()
        if not re.fullmatch(TEMP_FILE_RE, name):
            raise TemplateLoadError(
                f"decision_state_from_file({path!r}): only temp_file() paths of this "
                f"run are readable under {run.temp_dir}"
            )
        text = read_without_links(run.temp_dir, name)
    else:
        if run.policy is None:
            raise ReadPolicyMissing(
                f"no read permissions for the working directory {run.workdir}"
            )
        relative = PurePosixPath(path)
        if not any(relative.full_match(glob) for glob in run.policy) or any(
            relative.full_match(glob) for glob in DENIED_GLOBS
        ):
            raise TemplateLoadError(
                f"decision_state_from_file({path!r}) is refused: the read permissions "
                f"allow {run.policy}, never hidden files, keys or dependency trees"
            )
        text = read_without_links(run.workdir, path)
    return text


@jinja2.pass_environment
def find_files(environment: SkillEnvironment, pattern: str) -> list[str]:
    """The sorted paths, relative to the run's working directory, of its regular files
    that the glob `pattern` and the run's read permissions allow. The driver records
    each list by the steps done before it and the pattern, so replays get the same list.
    """
    run: Run = environment.llajinja_run
    key = f"{len(run.answered)}:{pattern}"
    if key in run.find_files_results:
        return run.find_files_results[key]
    if run.workdir is None or not run.workdir.is_absolute():
        raise TemplateLoadError(f"find_files(): {run.workdir} is not an absolute path")
    if run.policy is None:
        raise ReadPolicyMissing(
            f"no read permissions for the working directory {run.workdir}"
        )
    if not isinstance(pattern, str) or ".." in PurePosixPath(pattern).parts:
        raise TemplateLoadError(
            f"find_files({pattern!r}): use a relative glob without '..'"
        )
    root = run.workdir.resolve()
    paths: list[str] = []
    try:
        for path in run.workdir.glob(pattern):
            relative = path.relative_to(run.workdir).as_posix()
            info = path.lstat()
            if (
                any(PurePosixPath(relative).full_match(glob) for glob in run.policy)
                and not any(
                    PurePosixPath(relative).full_match(glob) for glob in DENIED_GLOBS
                )
                and path.resolve() == root / relative
                and stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
            ):
                paths.append(relative)
                if len(paths) > FIND_FILES_LIMIT:
                    raise TemplateLoadError(
                        f"find_files({pattern!r}) matches more than {FIND_FILES_LIMIT} "
                        "files: narrow the pattern or the read permissions"
                    )
    except (NotImplementedError, ValueError) as exc:
        raise TemplateLoadError(f"find_files({pattern!r}): {exc}") from exc
    run.find_files_results[key] = sorted(paths)
    return run.find_files_results[key]


@jinja2.pass_environment
def temp_file(environment: SkillEnvironment, name: str) -> str:
    """The path `template_<name>` in the run's directory, the same on every render of
    the run. A name takes only letters, digits, `_` and `-`."""
    run: Run = environment.llajinja_run
    file_name = f"template_{name}"
    if not isinstance(name, str) or not re.fullmatch(TEMP_FILE_RE, file_name):
        raise TemplateLoadError(
            f"temp_file({name!r}): use letters, digits, _ and - only"
        )
    path = run.temp_dir / file_name
    if path.resolve() != path:
        raise TemplateLoadError(f"temp_file({name!r}): {path} is a symbolic link")
    return str(path)


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
        normalised = reference.strip().rstrip("/\\").strip()
        if not normalised:
            raise SkillNotFound("Skill name is not defined")
        if len(normalised.split()) != 1:
            raise SkillNotFound(f"a skill name has no whitespace in it: {reference!r}")
        return normalised

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
    tags = {AGENT_TAG, STAGE_TAG, BUNDLE_TAG}

    def __init__(self, environment: jinja2.Environment) -> None:
        super().__init__(environment)
        self.depth = 0
        self.captures = 0
        environment.globals.update(decision=decision, choice=choice, score=score)
        environment.globals.update(yesno=yesno, temp_file=temp_file)
        environment.globals.update(
            find_files=find_files, decision_state_from_file=decision_state_from_file
        )
        blocked = jinja2.StrictUndefined(
            "random and lipsum() are blocked: every driver call renders the skill "
            "again, so a random value would change from one call to the next"
        )
        environment.globals["lipsum"] = environment.filters["random"] = blocked

    def preprocess(
        self, source: str, name: str | None, filename: str | None = None
    ) -> str:
        source = source.rstrip() + "\n"
        lines = source.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            return source
        for index in range(1, len(lines)):
            if lines[index].strip() in {"---", "..."}:
                return "\n" * (index + 1) + "".join(lines[index + 1 :])
        return source

    def parse(self, parser: Parser) -> list[nodes.Node] | nodes.Node:
        token = next(parser.stream)
        parser_func = {
            STAGE_TAG: self.parse_stage,
            AGENT_TAG: self.parse_agent,
            BUNDLE_TAG: self.parse_bundle,
        }
        if parser_func := parser_func.get(token.value):
            return parser_func(parser, token.lineno)

        raise RuntimeError("Parser not found for unknown token")

    def parse_stage(self, parser: Parser, lineno: int) -> nodes.Node:
        target = parser.parse_assign_target(name_only=True)
        body = parser.parse_statements((f"name:end{STAGE_TAG}",), drop_needle=True)
        # The end of the entry skill's source follows: no step can come after it.
        last = (
            parser.name == self.environment.llajinja_entry_skill_name
            and parser.stream.look().test("eof")
        )
        call = self.call_method(
            "_dispatch_stage",
            [
                nodes.Const(target.name, lineno=lineno),
                nodes.Const(parser.name or "<skill>", lineno=lineno),
                nodes.Const(last, lineno=lineno),
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
            self.call_method("_capture_prompt", [nodes.Const(target.name)]),
            [],
            [],
            body,
            lineno=lineno,
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

    def parse_bundle(self, parser: Parser, lineno: int) -> list[nodes.Node]:
        """`{% bundle NAME %}`: opens a bundle that every decision agent run in its body
        joins, then `NAME = _ask_bundle(text)` asks them all in one step."""
        if self.depth:
            parser.fail("a {% bundle %} cannot be inside an agent or a bundle", lineno)
        target = parser.parse_assign_target(name_only=True)
        body = parser.parse_statements((f"name:end{BUNDLE_TAG}",), drop_needle=True)
        self.captures += 1
        text = nodes.Name(f"{BODY_PREFIX}{self.captures}", "store", lineno=lineno)
        ask = self.call_method("_ask_bundle", [nodes.Name(text.name, "load")])
        return [
            nodes.ExprStmt(self.call_method("_open_bundle"), lineno=lineno),
            nodes.AssignBlock(text, None, body, lineno=lineno),
            nodes.Assign(target, ask, lineno=lineno),
        ]

    def _dispatch_stage(
        self,
        step_name: str,
        source_reference: TemplateReference,
        last: bool,
        caller: Callable[[], str],
    ) -> str:
        text = caller()
        run: Run | None = getattr(self.environment, "llajinja_run", None)
        if run is None or run.capturing:
            return text
        if run.bundle is not None:
            raise TemplateLoadError("a {% bundle %} holds decision agents, not stages")
        run.dispatch(
            Step(
                kind=StepKind.STAGE,
                step_name=step_name,
                source_reference=source_reference,
                prompt=textwrap.dedent(text).strip(),
                last=last,
            )
        )
        return ""

    def _capture_prompt(self, name: str, caller: Callable[[], str]) -> str:
        run: Run | None = getattr(self.environment, "llajinja_run", None)
        if run is None:
            return caller()
        run.capturing += 1
        run.enclosing_agents.append(AgentQuestions(name))
        try:
            return caller()
        finally:
            run.capturing -= 1
            run.captured = run.enclosing_agents.pop()

    def _open_bundle(self) -> None:
        run: Run = self.environment.llajinja_run
        if run.bundle is not None or run.capturing:
            raise TemplateLoadError(
                "a {% bundle %} cannot be inside an agent or a bundle"
            )
        run.bundle = []

    @jinja2.pass_context
    def _dispatch_agent(
        self,
        context: Context,
        step_name: str,
        body: str,
        outputs: Any = None,
        nested: Any = None,
        input: Any = None,
        key: str | int | None = None,
        **unknown: Any,
    ) -> Any:
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
        asked, run.captured = run.captured, None
        if key is not None and run.bundle is None:
            raise TemplateLoadError("key= is for decision agents inside a {% bundle %}")
        if asked and asked.questions:
            # A decision agent: its body is the state, asked now or at {% endbundle %}.
            if outputs is not None or nested is not None:
                raise TemplateLoadError(
                    f"{{% agent {step_name} %}} asks decisions: outputs= and nested= "
                    "are for Task agents"
                )
            state: str | dict[str, object] = textwrap.dedent(body).strip()
            if input is not None:
                state = {"text": state, "input": input}
                try:
                    json.dumps(state)
                except TypeError as exc:
                    raise TemplateLoadError(
                        f"{{% agent {step_name} %}}: input= must be JSON data: {exc}"
                    ) from exc
            if not state:
                raise TemplateLoadError(f"{{% agent {step_name} %}}: its body is empty")
            item = DecisionItem(step_name, key, asked.questions, asked.single, state)
            if run.bundle is not None:
                run.bundle.append(item)
                return BundleMember(step_name)
            source = context.name or run.entry_skill_name
            return Bundle({step_name: item}).ask(run, step_name, source)[step_name]
        if asked and asked.reads_state:
            # Reached only when the agent asked no decision: its body is a Task
            # prompt (or nested input), so a file read into it would go to the
            # orchestrator, which is exactly what decision_state_from_file() forbids.
            raise TemplateLoadError(
                f"{{% agent {step_name} %}}: decision_state_from_file() is only for a "
                "decision agent — one whose body calls decision(). This agent asks "
                "no decision, so its body is sent to the orchestrator, not the "
                "decision model."
            )
        if run.bundle is not None:
            raise TemplateLoadError(
                f"a {{% bundle %}} holds decision agents only; {step_name!r} asks none"
            )
        if outputs is not None and not isinstance(outputs, Mapping):
            raise TemplateLoadError(f"outputs= must be a mapping, got {outputs!r}")
        if isinstance(input, jinja2.Undefined) or input is None:
            input = {}
        if nested is not None:
            nested_name = SkillLoader.get_normalised_reference(str(nested))
            if not SKILL_NAME_PATTERN.fullmatch(nested_name):
                raise TemplateLoadError(
                    f"{{% agent {step_name} %}} nested= must be a skill name made of "
                    f"letters, digits, '_', '.' or '-', got {nested_name!r}"
                )
        nested_run = (
            None
            if nested is None
            else NestedSkill(
                skill_name=nested_name,
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

    @jinja2.pass_context
    def _ask_bundle(self, context: Context, text: str) -> dict[str, Any]:
        run: Run = context.environment.llajinja_run
        items, run.bundle = run.bundle or [], None
        if text.strip():
            raise TemplateLoadError("a {% bundle %} holds decision agents, not text")
        bundle = Bundle()
        for item in items:
            bundle.add(item)
        step_name = f"{BUNDLE_TAG}:{','.join(dict.fromkeys(i.agent for i in items))}"
        return bundle.ask(run, step_name, context.name or run.entry_skill_name)


@dataclass
class Run:
    entry_skill_name: SkillName
    answers: list[dict[str, Any]] = field(default_factory=list)
    answered: list[Step] = field(default_factory=list)
    capturing: int = 0
    prose: list[str] = field(default_factory=list)
    temp_dir: Path | None = None
    workdir: Path | None = None
    enclosing_agents: list[AgentQuestions] = field(default_factory=list)
    captured: AgentQuestions | None = None
    bundle: list[DecisionItem] | None = None
    find_files_results: dict[str, list[str]] = field(default_factory=dict)
    policy: list[str] | None = None

    def take_prose(self) -> str:
        text = textwrap.dedent("".join(self.prose)).strip()
        self.prose.clear()
        return text

    def dispatch_prose(self, last: bool = False) -> None:
        if prose := self.take_prose():
            self.dispatch(
                Step(
                    kind=StepKind.STAGE,
                    step_name=self.entry_skill_name,
                    source_reference=self.entry_skill_name,
                    prompt=prose,
                    last=last,
                )
            )

    def dispatch(self, step: Step) -> dict[str, Any]:
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

    @staticmethod
    def needs_read_permissions(
        skill_name: SkillName, *, skills_dir: SkillsDir = UNSET
    ) -> bool:
        """Whether the skill, or a skill it references, names decision_state_from_file
        or find_files anywhere in its source. Textual: a mention in prose counts too,
        and only demands the read permissions needlessly."""
        entry_skill_name = SkillLoader.get_normalised_reference(skill_name)
        root = get_default_skills_dir() if skills_dir is UNSET else skills_dir
        loader = SkillLoader(root, entry_skill_name=entry_skill_name)
        environment = SkillEnvironment(loader, entry_skill_name=entry_skill_name)
        sources: list[str] = []

        def collect(
            ast: nodes.Template, declared: TemplateReference
        ) -> list[Reference]:
            sources.append(loader.get_source(environment, declared)[0])
            return Reference.get_references(ast, declared)

        environment.walk_skills(collect)
        return any(
            "decision_state_from_file" in text or "find_files" in text
            for text in sources
        )

    def run(
        self,
        answers: Sequence[Mapping[str, Any]] = (),
        temp_dir: Path | None = None,
        workdir: str | None = None,
        find_files_results: Mapping[str, list[str]] | None = None,
        policy: Sequence[str] | None = None,
    ) -> RunResult:
        run = Run(
            self.entry_skill_name,
            [dict(answer) for answer in answers],
            temp_dir=temp_dir.resolve() if temp_dir else None,
            workdir=Path(workdir) if workdir else None,
            find_files_results=dict(find_files_results or {}),
            policy=None if policy is None else list(policy),
        )
        self.environment.llajinja_run = run
        rendered: list[str] = []
        try:
            for chunk in self.template.generate({INPUT_NAME: dict(self.variables)}):
                rendered.append(chunk)
                run.prose.append(chunk)
            # Rendered to the end: what prose is left is the last step.
            run.dispatch_prose(last=True)
        except Suspend as suspended:
            return RunResult(
                run.answered, suspended.step, "".join(rendered), run.find_files_results
            )
        except jinja2.TemplateError as exc:
            raise TemplateLoadError.from_jinja_error(exc) from exc
        finally:
            run.bundle = None
            self.environment.llajinja_run = None
        return RunResult(run.answered, None, "".join(rendered), run.find_files_results)
