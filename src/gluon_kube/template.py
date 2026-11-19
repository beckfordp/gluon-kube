# SPDX-License-Identifier: MIT

"""The Go-template subset gluon's chart uses (plan GK-3.4), rendered as Helm renders it.

Supported: text and `{{ }}` actions with `{{-`/`-}}` trimming and `{{/* */}}` comments;
`.`, `.Values.…`, `.Release.…`, `.Chart.…` and `.field` chains on the current dot;
string, integer and boolean literals; pipelines into `quote`, `toYaml`, `nindent N` and
`indent N`; `if`/`with`/`range` … `else` … `end` with Go's notion of empty. A missing
field renders as nothing (Helm replaces Go's `<no value>`).

Anything else (`include`, `define`, `tpl`, `default`, variables, comparisons, …) is
refused with a TemplateError naming the template, the line and the construct: gluon-kube
renders what the chart uses exactly, or says it can't (ADR 0030 decision 2).

Sources: Go's text/template documentation (pkg.go.dev/text/template), Helm's
template guide and its sprig functions (`quote`, `indent`, `nindent`) and `toYaml`.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import yaml

FUNCTIONS = ("quote", "toYaml", "nindent", "indent")
KEYWORDS = ("if", "with", "range", "else", "end")


class TemplateError(Exception):
    """A template gluon-kube can't render; the message names the template and line."""


# --- Parsing ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Field:
    """A field chain on the current dot (`.` itself is the empty chain)."""

    names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Literal:
    """A string, integer or boolean literal."""

    value: str | int | bool


type Operand = Field | Literal


@dataclass(frozen=True, slots=True)
class Command:
    """A pipeline stage: an operand, or a function applied to its arguments (the
    previous stage's value is passed as the last argument, as Go does)."""

    function: str | None
    args: tuple[Operand, ...]


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Commands joined by `|`."""

    commands: tuple[Command, ...]
    line: int


@dataclass(frozen=True, slots=True)
class Text:
    """Literal text."""

    text: str


@dataclass(frozen=True, slots=True)
class Action:
    """`{{ pipeline }}`: its value printed."""

    pipeline: Pipeline


@dataclass(frozen=True, slots=True)
class Block:
    """`if`, `with` or `range`, its body and its `else` body."""

    keyword: str
    pipeline: Pipeline
    body: tuple["Node", ...]
    otherwise: tuple["Node", ...]


type Node = Text | Action | Block


@dataclass(frozen=True, slots=True)
class Token:
    """A lexed piece: text, or an action's inside with the line it starts on."""

    text: str
    is_action: bool
    line: int


ACTION = re.compile(r"\{\{(-\s)?(.*?)(\s-)?\}\}", re.DOTALL)
WORD = re.compile(
    r'\s*(?:(?P<string>"(?:[^"\\]|\\.)*")|(?P<raw>`[^`]*`)|(?P<pipe>\|)'
    r"|(?P<paren>[()])|(?P<word>[^\s|()]+))"
)
FIELD = re.compile(r"^(\.[A-Za-z_][A-Za-z0-9_]*)+$|^\.$")
INTEGER = re.compile(r"^-?[0-9]+$")


def lex(source: str, name: str) -> list[Token]:
    """Text and actions, with `{{-`/`-}}` trimming applied to the text around them."""
    tokens: list[Token] = []
    position = 0
    trim_next = False
    for match in ACTION.finditer(source):
        text = source[position : match.start()]
        if trim_next:
            text = text.lstrip()
        if match.group(1):
            text = text.rstrip()
        if text:
            tokens.append(Token(text, False, 0))
        line = source.count("\n", 0, match.start()) + 1
        inner = match.group(2).strip()
        if inner.startswith("/*"):
            if not inner.endswith("*/"):
                raise TemplateError(f"{name}: line {line}: unclosed comment")
        else:
            tokens.append(Token(inner, True, line))
        trim_next = bool(match.group(3))
        position = match.end()
    rest = source[position:]
    if trim_next:
        rest = rest.lstrip()
    if "{{" in rest:
        line = source.count("\n", 0, position + rest.index("{{")) + 1
        raise TemplateError(f"{name}: line {line}: unclosed action")
    if rest:
        tokens.append(Token(rest, False, 0))
    return tokens


def unsupported(name: str, line: int, construct: str) -> TemplateError:
    """The error for a construct outside the subset."""
    return TemplateError(
        f"{name}: line {line}: {construct!r} is not supported by gluon-kube's template renderer "
        f"(the subset gluon's chart uses: fields, {', '.join(FUNCTIONS)}, "
        "if/with/range/else/end)"
    )


def operand(word: str, name: str, line: int) -> Operand:
    """A field chain or a literal; anything else is unsupported."""
    if FIELD.match(word):
        return Field(tuple(part for part in word.split(".") if part))
    if INTEGER.match(word):
        return Literal(int(word))
    if word in ("true", "false"):
        return Literal(word == "true")
    raise unsupported(name, line, word)


def pipeline(text: str, name: str, line: int) -> Pipeline:
    """`operand`, `function args…` or those joined by `|`."""
    stages: list[list[Operand | str]] = [[]]
    for match in WORD.finditer(text):
        if match.group("pipe"):
            stages.append([])
        elif match.group("paren"):
            raise unsupported(name, line, "(")
        elif (quoted := match.group("string")) is not None:
            stages[-1].append(Literal(go_unquote(quoted)))
        elif (raw := match.group("raw")) is not None:
            stages[-1].append(Literal(raw[1:-1]))
        else:
            word = match.group("word")
            if word.startswith("$"):
                raise unsupported(name, line, word)
            if not stages[-1] and word in FUNCTIONS:
                stages[-1].append(word)
            else:
                stages[-1].append(operand(word, name, line))
    commands = []
    for index, stage in enumerate(stages):
        if not stage:
            raise TemplateError(f"{name}: line {line}: empty command in {text!r}")
        head, *rest = stage
        if isinstance(head, str):
            args = tuple(arg for arg in rest if not isinstance(arg, str))
            commands.append(Command(head, args))
        else:
            if rest or index > 0:
                raise TemplateError(
                    f"{name}: line {line}: can't give arguments to a value: {text!r}"
                )
            commands.append(Command(None, (head,)))
    return Pipeline(tuple(commands), line)


def go_unquote(quoted: str) -> str:
    """A Go interpreted string literal's value (the common escapes)."""
    body = quoted[1:-1]
    escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
    return re.sub(r"\\(.)", lambda m: escapes.get(m.group(1), m.group(0)), body)


def parse(source: str, name: str) -> tuple[Node, ...]:
    """The template's tree; unbalanced or unsupported actions raise TemplateError."""
    tokens = lex(source, name)
    nodes, position, ender = parse_nodes(tokens, 0, name)
    if ender is not None:
        raise TemplateError(f"{name}: line {ender.line}: unexpected {{{{{ender.text}}}}}")
    assert position == len(tokens)
    return nodes


def parse_nodes(
    tokens: Sequence[Token], position: int, name: str
) -> tuple[tuple[Node, ...], int, Token | None]:
    """Nodes until `else`/`end` (returned, unconsumed beyond it) or the end."""
    nodes: list[Node] = []
    while position < len(tokens):
        token = tokens[position]
        position += 1
        if not token.is_action:
            nodes.append(Text(token.text))
            continue
        keyword, _, rest = token.text.partition(" ")
        if keyword in ("else", "end"):
            return tuple(nodes), position, token
        if keyword in ("if", "with", "range"):
            if not rest.strip():
                raise TemplateError(f"{name}: line {token.line}: {keyword} needs a pipeline")
            condition = pipeline(rest, name, token.line)
            body, position, ender = parse_nodes(tokens, position, name)
            otherwise: tuple[Node, ...] = ()
            if ender is not None and ender.text != "else" and ender.text.startswith("else"):
                raise unsupported(name, ender.line, ender.text)
            if ender is not None and ender.text == "else":
                otherwise, position, ender = parse_nodes(tokens, position, name)
            if ender is None or ender.text != "end":
                raise TemplateError(f"{name}: line {token.line}: {keyword} has no end")
            nodes.append(Block(keyword, condition, body, otherwise))
            continue
        nodes.append(Action(pipeline(token.text, name, token.line)))
    return tuple(nodes), position, None


# --- Rendering -------------------------------------------------------------------------


def empty(value: object) -> bool:
    """Go's truth: false, 0, nil and empty strings, lists and maps are empty."""
    if value is None or value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    if isinstance(value, (str, list, tuple, dict, Mapping)):
        return len(value) == 0
    return False


def lookup(dot: object, names: Sequence[str]) -> object:
    """A field chain from the dot; a missing field is None (Helm renders it as nothing)."""
    value: object = dot
    for part in names:
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def go_string(value: object, name: str, line: int) -> str:
    """A value as Go's template prints it: strings as they are, numbers, booleans;
    nothing for a missing value. Maps and lists aren't printed by the chart."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    raise TemplateError(
        f"{name}: line {line}: printing a {type(value).__name__} directly is not supported "
        "(use toYaml)"
    )


def go_quote(text: str) -> str:
    """Go's %q for a string: double quotes, backslash escapes."""
    out = ['"']
    for char in text:
        if char in ('"', "\\"):
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif not char.isprintable():
            code = ord(char)
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def to_yaml(value: object) -> str:
    """Helm's toYaml: block YAML with sorted keys, no trailing newline; nothing for nil."""
    if value is None:
        return ""
    dumped: str = yaml.safe_dump(value, default_flow_style=False, sort_keys=True, width=1 << 20)
    if dumped.endswith("\n...\n"):
        dumped = dumped[: -len("...\n")]
    return dumped.rstrip("\n")


def indent(width: int, text: str) -> str:
    """sprig's indent: every line prefixed with `width` spaces."""
    pad = " " * width
    return pad + text.replace("\n", "\n" + pad)


def call(function: str, args: Sequence[object], name: str, line: int) -> object:
    """One of the supported functions."""
    match function, args:
        case "quote", _:
            return " ".join(go_quote(go_string(arg, name, line)) for arg in args if arg is not None)
        case "toYaml", [value]:
            return to_yaml(value)
        case "indent", [int() as width, text] if not isinstance(width, bool):
            return indent(width, go_string(text, name, line))
        case "nindent", [int() as width, text] if not isinstance(width, bool):
            return "\n" + indent(width, go_string(text, name, line))
    raise TemplateError(f"{name}: line {line}: wrong arguments to {function}: {list(args)!r}")


def evaluate(pipe: Pipeline, dot: object, name: str) -> object:
    """A pipeline's value."""
    value: object = None
    for index, command in enumerate(pipe.commands):
        args = [
            lookup(dot, arg.names) if isinstance(arg, Field) else arg.value for arg in command.args
        ]
        if command.function is None:
            value = args[0]
        else:
            if index > 0:
                args.append(value)
            value = call(command.function, args, name, pipe.line)
    return value


def render_nodes(nodes: Sequence[Node], dot: object, name: str) -> str:
    """The nodes' output with `dot` as the current value."""
    out: list[str] = []
    for node in nodes:
        match node:
            case Text():
                out.append(node.text)
            case Action():
                value = evaluate(node.pipeline, dot, name)
                out.append(go_string(value, name, node.pipeline.line))
            case Block():
                out.append(render_block(node, dot, name))
    return "".join(out)


def render_block(block: Block, dot: object, name: str) -> str:
    """`if`, `with` or `range`."""
    value = evaluate(block.pipeline, dot, name)
    if empty(value):
        return render_nodes(block.otherwise, dot, name)
    if block.keyword == "if":
        return render_nodes(block.body, dot, name)
    if block.keyword == "with":
        return render_nodes(block.body, value, name)
    if isinstance(value, Mapping):
        items: list[object] = [value[key] for key in sorted(value)]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif isinstance(value, int) and not isinstance(value, bool):
        items = list(range(value))
    else:
        raise TemplateError(
            f"{name}: line {block.pipeline.line}: can't range over a {type(value).__name__}"
        )
    return "".join(render_nodes(block.body, item, name) for item in items)


def render(source: str, name: str, context: Mapping[str, object]) -> str:
    """The template `source` (named `name` in errors) rendered with `context` as the dot."""
    return render_nodes(parse(source, name), context, name)
