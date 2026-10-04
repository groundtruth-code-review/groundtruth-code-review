"""Findings the parser can prove, with no model involved.

The usual way a claim reaches a pull request is: a model proposes it, a
second model agrees. For one common class of claim that is the weaker
route, because the answer is mechanical. "This function now needs another
argument, and that caller still passes one" is not a matter of opinion: it
is true or false of the two syntax trees. So when a function's signature
changed, this module takes each place that calls it and asks the one
question a language answers exactly -- could this call bind to the new
parameter list? If not, and it *could* have bound to the old one, this
change broke it, and that is a finding with its evidence attached.

Nothing is executed. The repository's code is only ever parsed, never run,
which matters here: a review runs inside your CI with your secrets, and
running a pull request's own tooling there means running whatever the pull
request put in it.

What keeps this honest is how much it declines to say. Name search finds
text that looks like a call, not the function it calls, so a proof is only
attempted when the call can be tied to the function:

  * the repository defines the name exactly once (two same-named functions
    and a call that is wrong for one may be right for the other);
  * a call from another file must import that name from the changed file's
    module -- a bare `loads(x)` that came from `json` is not our `loads`;
  * a method is only checked against `self.name(...)` in its own file,
    since `obj.name(...)` could be any object's method (and because the name
    is defined once, a `self.name(...)` there can only be reaching this one,
    as a subclass does);
  * a call using `*args` or `**kwargs` is skipped (its shape is unknown);
  * a call that was already incompatible with the old signature is skipped,
    because this change didn't break it.

Python only, for now. Anything else produces no proof, which is the same as
today: the model still proposes, and the second model still decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .context_engine import DiffMap, SignatureChange, count_definitions, find_callers
from .quality_gate import Finding, Severity

try:
    import tree_sitter_language_pack as tslp
except ImportError:  # pragma: no cover - the dependency is required in practice
    tslp = None

logger = logging.getLogger(__name__)

_CALLERS_TO_CHECK = 20
_SITES_LISTED = 3
# Not 1.0. The parse is exact, but tying a call to the function it names is
# still done by name and import, and a number that admits that is the honest one.
_CONFIDENCE = 0.95

_parser = None


def _python_parser():
    global _parser
    if _parser is None and tslp is not None:
        try:
            if not tslp.has_language("python"):
                tslp.download(["python"])
            _parser = tslp.get_parser("python")
        except Exception:
            logger.debug("proof_parser_unavailable language=python")
            return None
    return _parser


def _parse(source: str):
    parser = _python_parser()
    if parser is None:
        return None
    try:
        return parser.parse(source.encode("utf-8")).root_node
    except Exception:
        return None


def _text(node) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


# ----------------------------------------------------------------- parameters


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # "posonly" | "pos" | "kwonly" | "varargs" | "varkw"
    has_default: bool


def _params(fn) -> list[Param] | None:
    """The parameter list of a `function_definition`, or None when it has a
    shape this module does not recognise -- saying nothing beats guessing.
    """
    node = fn.child_by_field_name("parameters")
    if node is None:
        return None
    out: list[Param] = []
    after_star = False
    for child in node.named_children:
        kind = child.type
        plain = "kwonly" if after_star else "pos"
        if kind == "comment":
            continue
        if kind == "identifier":
            out.append(Param(_text(child), plain, False))
        elif kind in ("default_parameter", "typed_default_parameter"):
            out.append(Param(_text(child.child_by_field_name("name")), plain, True))
        elif kind == "typed_parameter":
            inner = next(
                (
                    c
                    for c in child.named_children
                    if c.type in ("identifier", "list_splat_pattern", "dictionary_splat_pattern")
                ),
                None,
            )
            if inner is None:
                return None
            if inner.type == "identifier":
                out.append(Param(_text(inner), plain, False))
            elif inner.type == "list_splat_pattern":
                out.append(Param(_text(inner).lstrip("*").strip(), "varargs", False))
                after_star = True
            else:
                out.append(Param(_text(inner).lstrip("*").strip(), "varkw", False))
        elif kind == "list_splat_pattern":
            out.append(Param(_text(child).lstrip("*").strip(), "varargs", False))
            after_star = True
        elif kind == "dictionary_splat_pattern":
            out.append(Param(_text(child).lstrip("*").strip(), "varkw", False))
        elif kind == "keyword_separator":
            after_star = True
        elif kind == "positional_separator":
            out = [Param(p.name, "posonly" if p.kind == "pos" else p.kind, p.has_default) for p in out]
        else:
            return None
    return out


def _drop_receiver(params: list[Param]) -> list[Param]:
    """`self` is bound by the call itself, so it is not something a caller passes."""
    if params and params[0].kind in ("pos", "posonly"):
        return params[1:]
    return params


# ---------------------------------------------------------------- binding


@dataclass(frozen=True)
class CallShape:
    positional: int
    keywords: tuple[str, ...]


def _call_shape(call) -> CallShape | None:
    """How a call passes its arguments, or None when that is not knowable
    from the text (`*args`, `**kwargs`).
    """
    args = call.child_by_field_name("arguments")
    if args is None:
        return None
    if args.type == "generator_expression":  # f(x for x in y): one argument
        return CallShape(1, ())
    positional = 0
    keywords: list[str] = []
    for arg in args.named_children:
        if arg.type == "comment":
            continue
        if arg.type in ("list_splat", "dictionary_splat"):
            return None
        if arg.type == "keyword_argument":
            keywords.append(_text(arg.child_by_field_name("name")))
        else:
            positional += 1
    return CallShape(positional, tuple(keywords))


def _bind(name: str, params: list[Param], shape: CallShape) -> str | None:
    """Why this call cannot bind to these parameters, in a clause that
    follows "the call" -- or None when it binds.
    """
    positional_slots = [p for p in params if p.kind in ("posonly", "pos")]
    has_varargs = any(p.kind == "varargs" for p in params)
    has_varkw = any(p.kind == "varkw" for p in params)

    if shape.positional > len(positional_slots) and not has_varargs:
        limit = len(positional_slots)
        bound = "at most " if any(p.has_default for p in positional_slots) else ""
        return (
            f"passes {shape.positional} positional argument{'s' if shape.positional != 1 else ''}, "
            f"but {name} takes {bound}{limit}"
        )

    bound_names = {p.name for p in positional_slots[: shape.positional]}
    by_name = {p.name: p for p in params if p.kind in ("pos", "kwonly")}
    for keyword in shape.keywords:
        target = by_name.get(keyword)
        if target is None:
            if has_varkw:
                continue
            return f"passes unknown keyword '{keyword}'"
        if target.name in bound_names:
            return f"passes '{keyword}' twice, positionally and by keyword"
        bound_names.add(target.name)

    missing = [
        p.name
        for p in params
        if p.kind in ("posonly", "pos", "kwonly") and not p.has_default and p.name not in bound_names
    ]
    if missing:
        listed = ", ".join(f"'{m}'" for m in missing)
        return f"does not pass required argument{'s' if len(missing) != 1 else ''} {listed}"
    return None


# --------------------------------------------------------------- syntax walks


def _walk(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.children))


def _function_defs(root, name: str) -> list:
    return [
        n
        for n in _walk(root)
        if n.type == "function_definition" and _text(n.child_by_field_name("name")) == name
    ]


def _enclosing_class(node) -> str | None:
    """Name of the innermost class a node sits in, or None for module level."""
    parent = node.parent
    while parent is not None:
        if parent.type == "class_definition":
            return _text(parent.child_by_field_name("name"))
        parent = parent.parent
    return None


def _decorated(fn) -> bool:
    return fn.parent is not None and fn.parent.type == "decorated_definition"


def _calls_on_line(root, line: int, name: str) -> list[tuple]:
    """Calls to `name` that cover a 1-indexed line, as (node, form, receiver):
    form is "bare" for `name(...)` and "attr" for `<expr>.name(...)`.
    """
    found = []
    for node in _walk(root):
        if node.type != "call":
            continue
        if not (node.start_point.row <= line - 1 <= node.end_point.row):
            continue
        callee = node.child_by_field_name("function")
        if callee is None:
            continue
        if callee.type == "identifier" and _text(callee) == name:
            found.append((node, "bare", ""))
        elif callee.type == "attribute" and _text(callee.child_by_field_name("attribute")) == name:
            found.append((node, "attr", _text(callee.child_by_field_name("object"))))
    return found


def _imports_name(root, name: str, target_path: str) -> bool:
    """Does this file bring `name` in from the changed file's own module?"""
    target = Path(target_path)
    stem = target.parent.name if target.stem == "__init__" else target.stem
    for node in _walk(root):
        if node.type != "import_from_statement":
            continue
        module = node.child_by_field_name("module_name")
        if module is None or _text(module).rsplit(".", 1)[-1] != stem:
            continue
        for imported in node.children_by_field_name("name"):
            if imported.type == "dotted_name" and _text(imported) == name:
                return True
    return False


def _anchor(fn, source_lines: list[str], added: set[int]) -> tuple[int, str]:
    """Where the finding sits: the first changed line of the signature, so it
    lands inside the diff even when the caller it concerns is in another file.
    """
    params = fn.child_by_field_name("parameters")
    first = fn.start_point.row + 1
    last = (params.end_point.row if params is not None else fn.start_point.row) + 1
    for line in range(first, last + 1):
        if line in added:
            return line, source_lines[line - 1].strip()
    return first, source_lines[first - 1].strip()


def _one_line(text: str, limit: int = 90) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ------------------------------------------------------------------- the check


def _break_sites(
    repo_root: Path,
    change: SignatureChange,
    new_params: list[Param],
    old_params: list[Param],
    is_method: bool,
    head_root,
) -> list[tuple[str, int, str, str]]:
    """(path, line, call text, reason) for each call this change broke."""
    sites: list[tuple[str, int, str, str]] = []
    seen: set[tuple[str, int]] = set()
    trees: dict[str, object] = {change.path: head_root}

    for hit in find_callers(repo_root, change.name, from_path=change.path, limit=_CALLERS_TO_CHECK):
        if not hit.path.endswith(".py"):
            continue
        if hit.path not in trees:
            try:
                trees[hit.path] = _parse((repo_root / hit.path).read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                trees[hit.path] = None
        root = trees[hit.path]
        if root is None:
            continue

        for call, form, receiver in _calls_on_line(root, hit.line, change.name):
            if is_method:
                if form != "attr" or receiver not in ("self", "cls") or hit.path != change.path:
                    continue
            else:
                if form != "bare":
                    continue
                if hit.path != change.path and not _imports_name(root, change.name, change.path):
                    continue

            key = (hit.path, call.start_byte)
            if key in seen:
                continue
            seen.add(key)

            shape = _call_shape(call)
            if shape is None:
                continue
            reason = _bind(change.name, new_params, shape)
            if reason is None:
                continue
            if _bind(change.name, old_params, shape) is not None:
                continue  # already broken before this change; not this change's doing
            sites.append((hit.path, call.start_point.row + 1, _one_line(_text(call)), reason))
    return sites


def prove_signature_breaks(
    repo_root: Path | str,
    changes: list[SignatureChange],
    diffmap: DiffMap,
    head_sources: dict[str, str],
    base_sources: dict[str, str],
) -> list[Finding]:
    """One finding per changed function that now has a call it can't accept.

    Never raises: a parse that fails or a search that errors means no proof
    for that function, which leaves the review exactly as it was.
    """
    root = Path(repo_root)
    findings: list[Finding] = []
    for change in changes:
        if not change.path.endswith(".py") or change.name.startswith("__"):
            continue
        try:
            finding = _prove_one(root, change, diffmap, head_sources, base_sources)
        except Exception as exc:  # a proof is a bonus; it must never cost the review
            logger.warning("proof_failed file=%s name=%s error=%s", change.path, change.name, exc)
            continue
        if finding is not None:
            findings.append(finding)
    return findings


def _prove_one(
    root: Path,
    change: SignatureChange,
    diffmap: DiffMap,
    head_sources: dict[str, str],
    base_sources: dict[str, str],
) -> Finding | None:
    head_src, base_src = head_sources.get(change.path), base_sources.get(change.path)
    if not head_src or not base_src:
        return None
    head_root, base_root = _parse(head_src), _parse(base_src)
    if head_root is None or base_root is None:
        return None

    head_fns = [
        f
        for f in _function_defs(head_root, change.name)
        if change.start_line - 1 <= f.start_point.row <= change.end_line - 1
    ]
    if len(head_fns) != 1:
        return None
    head_fn = head_fns[0]
    head_class = _enclosing_class(head_fn)
    base_fns = [f for f in _function_defs(base_root, change.name) if _enclosing_class(f) == head_class]
    if len(base_fns) != 1:
        return None
    base_fn = base_fns[0]

    if _decorated(head_fn) or _decorated(base_fn):
        return None  # staticmethod / classmethod / property / wrappers change what a call means
    is_method = head_class is not None
    new_params, old_params = _params(head_fn), _params(base_fn)
    if new_params is None or old_params is None:
        return None
    if is_method:
        new_params, old_params = _drop_receiver(new_params), _drop_receiver(old_params)

    if count_definitions(root, change.name) != 1:
        return None

    sites = _break_sites(root, change, new_params, old_params, is_method, head_root)
    if not sites:
        return None

    added = diffmap[change.path].added if change.path in diffmap else set()
    line, quoted = _anchor(head_fn, head_src.splitlines(), added)

    path, at, call_text, reason = sites[0]
    more = len(sites) - 1
    title = (
        f"{change.name} changed its signature, but {path}:{at} still calls it as "
        f"`{call_text}`, which {reason}"
    )
    if more:
        title += f" (and {more} more call site{'s' if more != 1 else ''})"
    shown = "; ".join(f"{p}:{n} `{c}` {r}" for p, n, c, r in sites[:_SITES_LISTED])
    proof = f"Checked by parsing, not by a model: {shown}"
    if len(sites) > _SITES_LISTED:
        proof += f"; and {len(sites) - _SITES_LISTED} more"

    return Finding(
        file=change.path,
        line=line,
        category="correctness",
        severity=Severity.HIGH,
        confidence=_CONFIDENCE,
        title=title,
        quoted_code=quoted,
        proof=proof,
    )
