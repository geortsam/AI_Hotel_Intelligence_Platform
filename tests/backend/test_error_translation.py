"""Static guards on constraint-name-aware error translation.

Stage 4.5.16. A service may only describe a failure it can actually account for.

The defect this closes was found in Stage 4.5.15 and is worth stating precisely, because it is
the kind that reads as correct. Since Stage 4.5.12 six services write an audit event inside the
transaction of the mutation they describe -- deliberately, so the two commit or roll back
together. The consequence nobody had followed through is that an integrity failure in the AUDIT
layer now surfaces inside a DOMAIN service's ``except IntegrityError``, where it is
indistinguishable by SQLSTATE from a real domain conflict. A booking deletion whose audit
INSERT failed on its actor foreign key told the client that payments still referenced the
booking: a 23503 is a 23503.

Two rules follow, and this file pins both:

* the extraction of a constraint name is declared ONCE, as the SQLSTATE vocabulary already is;
* a service that writes an audit event must refuse to attribute an audit-relation failure to
  its own domain.
"""

from __future__ import annotations

import ast
import inspect
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import app
from app.core import errors
from app.core.errors import (
    AUDIT_RELATIONS,
    GENERIC_CONFLICT_MESSAGE,
    constraint_name_of,
    is_audit_integrity_failure,
    relation_of,
)

APP = Path(app.__file__).resolve().parent

#: The canonical audit-writing abstraction. A service writes audit events by holding one of
#: these; there is no other route, and :func:`test_the_abstraction_is_the_only_way_in` checks
#: that claim rather than assuming it.
#:
#: Named by WHERE IT IS DEFINED, which is the one thing about it an importing module cannot
#: change. Stage 4.5.18 keyed on the identifier appearing in an annotation; that was the
#: finding it carried forward, because ``from app.services.audit import AuditTrail as Trail``
#: reads as an ordinary tidy-up and would have dropped a service out of coverage in silence --
#: the same silent-absence failure the derived set exists to prevent.
AUDIT_MODULE = "app.services.audit"
AUDIT_ABSTRACTION = "AuditTrail"
AUDIT_QUALNAME = f"{AUDIT_MODULE}.{AUDIT_ABSTRACTION}"

#: Where the service package lives, so a module's own name -- and therefore its relative
#: imports and its locally defined classes -- can be resolved.
SERVICE_PACKAGE = "app.services"

#: The repository underneath the abstraction. Nothing but the audit module may bind it.
AUDIT_REPOSITORY_QUALNAME = "app.repositories.audit.AuditRepository"

#: The number of audit-writing classes the codebase has today, and the modules holding them.
#:
#: A regression sanity check ONLY. Nothing below reads these to decide what to verify -- the
#: coverage is derived by :func:`audit_writing_classes` -- but a silent change in the shape of
#: the architecture should still be something somebody has to look at.
EXPECTED_WRITER_COUNT = 7
EXPECTED_WRITER_MODULES = frozenset(
    {"auth", "booking", "payment", "membership", "amenity", "finance"}
)

#: Services known NOT to write audit events. Asserted to stay undiscovered, so the rule cannot
#: quietly widen into "everything with a ``_translate`` needs a guard" -- which is the shape
#: that would put unreachable code into four services to satisfy a test.
NON_AUDIT_SERVICES = frozenset({"guest", "review", "room", "room_type", "hotel"})

#: How far a re-export chain may be followed.
#:
#: Finite on purpose. The job is to follow a re-export, not to become a general type
#: resolver, and a bound is what makes "follow it" terminate without having to prove
#: anything about the shape of the graph. A chain longer than this resolves to wherever it
#: had got to, which is not the abstraction -- the fail-closed direction.
MAX_REEXPORT_HOPS = 8


#: The repository root: the application package's grandparent, since ``app`` sits one
#: level inside the import root.
REPO_ROOT = APP.parent.parent


def source_roots() -> list[Path]:
    """The directories the project puts on the import path, read from its own configuration.

    Derived, not assumed. ``pythonpath`` in ``pyproject.toml`` is the single fact that
    says where a production module may be imported from, and hardcoding ``backend``
    here would let the resolver's universe and the project's import path drift apart
    without anything noticing -- which is the shape of the gap this stage closes.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = config["tool"]["pytest"]["ini_options"]["pythonpath"]
    return [REPO_ROOT / entry for entry in declared]


def project_sources() -> dict[str, str]:
    """Every module importable from a declared source root, keyed by dotted name.

    The universe a re-export may be followed through, and the outer bound of this
    analysis. Stage 4.5.20 scoped it to the ``app`` package, which left a hole it
    recorded as NB-4: a module OUTSIDE that package but inside the project would be
    unreadable, so a chain through it would dead-end and the writer would drop out of
    coverage -- silently, and in the false-negative direction.

    Widening it to the import roots closes that hole by argument rather than by
    mitigation. A module that re-exports :class:`AuditTrail` must import it, and to
    import it the module must itself be importable, which means it lives under one of
    these roots and is therefore read here. A chain that still leaves this universe has
    left the project altogether, and a third-party package does not re-export a class
    defined in ``app``.
    """
    found: dict[str, str] = {}
    for root in source_roots():
        for path in sorted(root.rglob("*.py")):
            parts = path.relative_to(root).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            found[".".join(parts)] = path.read_text(encoding="utf-8")
    return found


#: Read once. Everything below resolves against this unless a test supplies its own
#: universe.
PROJECT_SOURCES = project_sources()


@dataclass
class ImportTable:
    """What the local names in one module actually refer to.

    Two mappings, because an annotation reaches a type by two routes: as a bare name bound by
    ``from ... import ...``, or as an attribute of a module bound by ``import ...``. A name that
    is in neither denotes nothing here -- which is the whole point, since it is what separates
    an aliased ``AuditTrail`` from somebody else's ``Trail``.
    """

    types: dict[str, str] = field(default_factory=dict)
    modules: dict[str, str] = field(default_factory=dict)
    #: The modules whose exports may be followed. Empty means: resolve spelling only.
    module_sources: dict[str, str] = field(default_factory=dict, repr=False)

    def resolved(self, annotation: ast.expr | None) -> set[str]:
        """Every fully qualified type *annotation* can denote in this module.

        A set rather than a single name because ``X | None`` and ``Annotated[X, ...]`` are both
        annotations that mention more than one thing, and a parameter typed either way is still
        holding an ``X``.
        """
        if annotation is None:
            return set()
        if isinstance(annotation, ast.Name):
            bound = self.types.get(annotation.id)
            return {bound} if bound else set()
        if isinstance(annotation, ast.Attribute):
            module = self.modules.get(ast.unparse(annotation.value))
            return {f"{module}.{annotation.attr}"} if module else set()
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                quoted = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return set()
            return self.resolved(quoted)
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return self.resolved(annotation.left) | self.resolved(annotation.right)
        if isinstance(annotation, ast.Subscript):
            return self.resolved(annotation.slice)
        if isinstance(annotation, ast.Tuple):
            found: set[str] = set()
            for element in annotation.elts:
                found |= self.resolved(element)
            return found
        return set()

    def identities(self, annotation: ast.expr | None) -> set[str]:
        """What *annotation* ultimately IS, re-exports followed.

        Stage 4.5.20. :meth:`resolved` answers a question about THIS module: what the local
        name is bound to here. That is the right answer to the wrong question the moment a
        module in between re-exports the abstraction, because ``from app.services.helpers
        import AuditTrail`` binds ``app.services.helpers.AuditTrail`` -- a true statement
        about the binding, and not the identity of the class.

        The two coincide for every import in the codebase today, which is why both methods
        exist: keeping them apart is what lets a test say which question it is asking.
        """
        return {follow_reexports(name, self.module_sources) for name in self.resolved(annotation)}


def origin_of(node: ast.ImportFrom, package: str) -> str:
    """The absolute module a ``from ... import ...`` reads from, relative forms included."""
    if not node.level:
        return node.module or ""
    root = package
    for _ in range(node.level - 1):
        root = root.rpartition(".")[0]
    return f"{root}.{node.module}" if node.module else root


def import_table(
    tree: ast.Module, module: str, module_sources: dict[str, str] | None = None
) -> ImportTable:
    """Bind every local name in *module* to the thing it actually refers to.

    *module* is the dotted name of the module being read, which is what makes both relative
    imports and locally defined classes resolvable. *module_sources* is the universe those
    bindings may be followed through; it defaults to the application package.
    """
    package = module.rpartition(".")[0]
    table = ImportTable(
        module_sources=PROJECT_SOURCES if module_sources is None else module_sources
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # ``import a.b.c`` binds the dotted path itself; ``as x`` binds x to it.
                table.modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            origin = origin_of(node, package)
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                # ``from a.b import c`` may bind a type OR a submodule, and the import alone
                # cannot say which. Which reading applies is decided by the annotation that
                # uses the name, so both are recorded and only one can ever match.
                table.types[local] = f"{origin}.{alias.name}"
                table.modules[local] = f"{origin}.{alias.name}"
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            # A class defined here needs no import to be named. ``services/audit.py`` refers to
            # its own ``AuditTrail`` by bare name, and that must resolve to the same type --
            # otherwise the audit module would be excluded by a resolution failure rather than
            # by the predicate, and :func:`test_the_audit_module_is_not_itself_a_writer` would
            # be asserting something it had not actually established.
            table.types[node.name] = f"{module}.{node.name}"
    return table


def export_table(module: str, module_sources: dict[str, str]) -> dict[str, str]:
    """What *module* exposes to an importer, as qualified names.

    TOP-LEVEL statements only, and that restriction is the whole of the safety argument. A
    binding made inside ``try: ... except ImportError:``, under ``if TYPE_CHECKING:``, or in
    a function is not something a static reader can claim the module exports -- so it is not
    followed, and the chain stops there. Nothing in ``app`` binds anything conditionally
    today, so the restriction costs nothing and buys the conditional-import case for free.

    Three kinds of top-level statement can establish an export:

    * an import -- the ordinary re-export, alias or not;
    * a class definition, which ENDS a chain: a class defined here is this module's own, no
      matter what it is called, so a local ``class AuditTrail`` is not the abstraction;
    * an assignment, but only the unambiguous kind -- one target, that target a bare name
      assigned exactly once in the module, and a value that is itself a name or an attribute
      resolving to exactly one thing. ``AuditTrail = something_dynamic()`` is a call, which
      resolves to nothing and fails closed. This is the smallest assignment rule that covers
      ``AuditTrail = audit.AuditTrail`` and admits nothing it cannot prove.
    """
    source = module_sources.get(module)
    if source is None:
        return {}
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - the package parses
        return {}

    package = module.rpartition(".")[0]
    local = ImportTable()
    exports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local.modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            origin = origin_of(node, package)
            for alias in node.names:
                if alias.name == "*":
                    continue
                name = alias.asname or alias.name
                local.types[name] = f"{origin}.{alias.name}"
                local.modules[name] = f"{origin}.{alias.name}"
                exports[name] = f"{origin}.{alias.name}"
        elif isinstance(node, ast.ClassDef):
            local.types[node.name] = f"{module}.{node.name}"
            exports[node.name] = f"{module}.{node.name}"

    rebound = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or rebound.count(target.id) != 1:
            continue
        if not isinstance(node.value, ast.Name | ast.Attribute):
            continue
        candidates = local.resolved(node.value)
        if len(candidates) == 1:
            exports[target.id] = next(iter(candidates))
    return exports


def follow_reexports(
    qualname: str, module_sources: dict[str, str], *, hops: int = MAX_REEXPORT_HOPS
) -> str:
    """*qualname* with any re-export it names followed to the class itself.

    Terminates on all four of the ways a chain can end, returning wherever it had reached in
    each: the defining module (the answer), a module outside the universe, a name the module
    does not export, and a name already seen. That last one is the cycle case -- ``a``
    re-exporting from ``b`` re-exporting from ``a`` -- which stops without recursion and
    without the identity, because a cycle defines nothing.

    *hops* is a parameter so that the cycle check can be shown to be doing the work rather
    than the bound: with it, a cycle reaches the same answer however many hops it is given.
    """
    seen = {qualname}
    for _ in range(hops):
        module, _, name = qualname.rpartition(".")
        target = export_table(module, module_sources).get(name)
        if target is None or target in seen:
            return qualname
        seen.add(target)
        qualname = target
    return qualname


@dataclass(frozen=True)
class ServiceClass:
    """One service class, with the AST that proves what it does and the import table that says
    what the names in it mean.

    Identity is the module and the class name; the AST and the table are carried, not compared.
    """

    module: str
    name: str
    node: ast.ClassDef = field(compare=False, repr=False)
    imports: ImportTable = field(compare=False, repr=False)

    def __str__(self) -> str:
        return f"{self.module}.{self.name}"


def service_classes() -> list[ServiceClass]:
    """Every class defined under ``app/services``, discovered rather than listed."""
    found: list[ServiceClass] = []
    for path in sorted((APP / "services").glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = import_table(tree, f"{SERVICE_PACKAGE}.{path.stem}")
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                found.append(ServiceClass(path.stem, node.name, node, imports))
    return found


def initialiser(node: ast.ClassDef) -> ast.FunctionDef | None:
    return next(
        (n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None
    )


def holds_the_audit_abstraction(service: ServiceClass) -> bool:
    """Whether the class is CONSTRUCTED with an :class:`AuditTrail`.

    This is the reliable half of the signal, and it is why discovery keys on the constructor
    rather than on call sites. ``AuthService`` records through
    ``self._audit.for_actor(user).record(...)`` -- a shape no naive search for
    ``self._audit.record`` would match -- but it cannot record at all without being handed the
    abstraction first. The dependency is the thing that cannot be routed around.

    Stage 4.5.19: the annotation is RESOLVED through the module's import table rather than read
    as text. ``audit: Trail`` counts when ``Trail`` is this abstraction under an alias, and does
    not when it is somebody else's -- and ``audit: FakeAuditTrail``, which the old substring
    test accepted, no longer counts at all.

    Stage 4.5.20: and that resolution follows re-exports, so the answer is the class the
    annotation denotes rather than the module the local name happened to be imported from.
    """
    init = initialiser(service.node)
    if init is None:
        return False
    arguments = [*init.args.posonlyargs, *init.args.args, *init.args.kwonlyargs]
    return any(AUDIT_QUALNAME in service.imports.identities(a.annotation) for a in arguments)


def records_an_audit_event(service: ServiceClass) -> bool:
    """Whether the class actually calls ``record(...)`` somewhere.

    The second half. Holding the abstraction without using it would be a service that could
    write and does not; requiring both means the derived set is what it claims to be.
    """
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "record"
        for call in ast.walk(service.node)
    )


def writes_audit_events(service: ServiceClass) -> bool:
    return holds_the_audit_abstraction(service) and records_an_audit_event(service)


def audit_writing_classes() -> list[ServiceClass]:
    """The derived coverage set. **Nothing enumerates these by name.**

    Note the granularity: CLASSES, not modules. ``finance.py`` defines four services and only
    two of them write audit events, which a module-level list could not express -- it said
    "finance" and was satisfied by a guard anywhere in the file.
    """
    return [service for service in service_classes() if writes_audit_events(service)]


def translator_of(service: ServiceClass) -> ast.FunctionDef | None:
    return next(
        (n for n in service.node.body if isinstance(n, ast.FunctionDef) and n.name == "_translate"),
        None,
    )


def audit_guards(function: ast.FunctionDef) -> list[ast.If]:
    """Every ``if is_audit_integrity_failure(...)`` branch in *function*."""
    return [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If) and "is_audit_integrity_failure" in ast.unparse(node.test)
    ]


def guarded_source(function: ast.FunctionDef) -> str:
    """The BODY of the audit guard, and nothing after it.

    Taken from the AST because a window of lines is wrong: an earlier draft read four lines
    past the guard, ran into the next branch -- which legitimately returns a conflict -- and
    reported the guard as returning a 409 when it does not.
    """
    return "\n".join(
        ast.unparse(ast.Module(body=guard.body, type_ignores=[]))
        for guard in audit_guards(function)
    )


def has_conflict_fallback(function: ast.FunctionDef) -> bool:
    """Whether the translator still returns an ordinary client conflict somewhere OUTSIDE the
    audit guard. The 409s this architecture is built on must survive every change to the 500."""
    guarded = {id(node) for guard in audit_guards(function) for node in ast.walk(guard)}
    return any(
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "ConflictError"
        and id(node) not in guarded
        for node in ast.walk(function)
    )


#: Derived once at import, so pytest can parametrise over it.
AUDIT_WRITERS = audit_writing_classes()
WRITER_IDS = [str(service) for service in AUDIT_WRITERS]


def code_only(source: str) -> str:
    """*source* with docstrings removed, so prose cannot be mistaken for code."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


#: How psycopg's diagnostics object is reached: as a quoted attribute name handed to `getattr`,
#: or as a real attribute access. Either quoting, because `ast.unparse` rewrites them.
_DIAGNOSTICS_ACCESS = re.compile(r"""['"]diag['"]|\.diag\b""")


def sources() -> dict[str, str]:
    return {
        path.relative_to(APP).as_posix(): code_only(path.read_text(encoding="utf-8"))
        for path in APP.rglob("*.py")
    }


class _Diag:
    def __init__(self, constraint_name: str | None = None, table_name: str | None = None) -> None:
        self.constraint_name = constraint_name
        self.table_name = table_name


class _Orig:
    def __init__(self, diag: _Diag, sqlstate: str = "23503") -> None:
        self.diag = diag
        self.sqlstate = sqlstate


class _Error(Exception):
    def __init__(self, **diag: str | None) -> None:
        super().__init__("boom")
        self.orig = _Orig(_Diag(**diag))


# ======================================================================================
# One extraction, declared once
# ======================================================================================


def test_the_constraint_name_extraction_is_declared_once() -> None:
    """Six services had identical private copies of this before Stage 4.5.16.

    The project already forbids a per-service copy of the SQLSTATE vocabulary, for exactly the
    reason that applies here: a rule duplicated six times is a rule that drifts, and the copy
    that drifts is the one nobody is looking at.
    """
    definitions = [
        name
        for name, source in sources().items()
        if "def constraint_name_of" in source or "def _constraint_name" in source
    ]

    assert definitions == ["core/errors.py"]


def test_no_service_reads_the_driver_diagnostics_directly() -> None:
    """The two accessors are the only doorway to ``exc.orig.diag``.

    A service reaching into the diagnostics itself would be one edit away from reading the
    driver MESSAGE, which renders the offending row -- a guest's details, an amount, an email
    address -- and is the thing eleven stages of error hygiene exist to keep out of responses.

    Two corrections to how this is matched, both made when Stage 7.5 tripped it.

    **``.diag`` is matched as a whole attribute, not as a substring.** The bare substring also
    fires on any longer name starting with those four letters -- ``.diagnostics`` on Stage 7.5's
    ``ChatResponse`` is one, ``.diagnose`` and ``.diagram`` would be others -- none of which is
    psycopg's diagnostics object.

    **The quoted form is matched in the quoting ``ast.unparse`` actually produces.** ``sources()``
    runs every file through :func:`code_only`, which re-emits the tree, and ``ast.unparse``
    normalises every string literal to single quotes. The original ``'"diag"'`` clause therefore
    could never match anything, in any file: the real accessor here is
    ``getattr(..., 'diag', None)``. The clause below matches either quoting, so it now does the
    work it was written to do. The positive control that follows is what would have caught this.
    """
    offenders = [
        name
        for name, source in sources().items()
        if name != "core/errors.py" and _DIAGNOSTICS_ACCESS.search(source)
    ]

    assert offenders == [], offenders


def test_the_diagnostics_guard_would_catch_a_real_offender() -> None:
    """Guards the test above: a pattern that matched nothing would pass over every file.

    ``core/errors.py`` is the one module allowed to read the diagnostics, so it is the natural
    positive control -- if the pattern cannot find the access there, it cannot find it anywhere,
    and the exclusion above is guarding nothing.
    """
    assert _DIAGNOSTICS_ACCESS.search(sources()["core/errors.py"])
    assert _DIAGNOSTICS_ACCESS.search("exc.orig.diag.constraint_name")
    assert _DIAGNOSTICS_ACCESS.search("getattr(orig, 'diag', None)")
    assert not _DIAGNOSTICS_ACCESS.search("response.diagnostics")


def test_the_extraction_returns_none_for_a_non_driver_error() -> None:
    """Callers fall back to their generic branch rather than crashing on an unexpected type."""
    assert constraint_name_of(ValueError("not a driver error")) is None
    assert relation_of(ValueError("not a driver error")) is None
    assert is_audit_integrity_failure(ValueError("not a driver error")) is False


def test_the_extraction_reads_the_two_diagnostic_fields() -> None:
    error = _Error(
        constraint_name="fk_payments_booking_id_hotel_id_bookings", table_name="payments"
    )

    assert constraint_name_of(error) == "fk_payments_booking_id_hotel_id_bookings"
    assert relation_of(error) == "payments"


def test_a_not_null_violation_has_a_relation_but_no_constraint_name() -> None:
    """The measured case that decided the design.

    ``revenue`` and ``reviews`` block a booking deletion through a composite SET NULL over a
    NOT NULL ``hotel_id``, and PostgreSQL reports those with no constraint name at all. Any
    attribution built on the constraint name alone would be unable to recognise them.
    """
    error = _Error(constraint_name=None, table_name="revenue")

    assert constraint_name_of(error) is None
    assert relation_of(error) == "revenue"


# ======================================================================================
# The audit relations, and the guard that names them
# ======================================================================================


def test_the_audit_relations_are_the_two_audit_tables() -> None:
    assert set(AUDIT_RELATIONS) == {"audit_events", "audit_events_archive"}


@pytest.mark.parametrize("relation", ["audit_events", "audit_events_archive"])
def test_an_audit_relation_failure_is_recognised(relation: str) -> None:
    assert is_audit_integrity_failure(_Error(table_name=relation)) is True


@pytest.mark.parametrize("relation", ["payments", "revenue", "reviews", "bookings", "guests"])
def test_a_domain_relation_failure_is_not(relation: str) -> None:
    assert is_audit_integrity_failure(_Error(table_name=relation)) is False


def test_the_guard_asks_about_the_relation_not_the_sqlstate() -> None:
    """An audit write can fail as a foreign-key, check or unique violation, and each of those
    already means something specific in every domain that would otherwise claim it."""
    for state in ("23503", "23505", "23514", "23502"):
        error = _Error(table_name="audit_events")
        error.orig.sqlstate = state
        assert is_audit_integrity_failure(error) is True


@pytest.mark.parametrize("service", AUDIT_WRITERS, ids=WRITER_IDS)
def test_every_audit_writing_service_guards_its_translation(service: ServiceClass) -> None:
    """The rule, checked on every class discovery finds -- not on a list somebody maintains.

    Matched on the guard CALL inside the class's own translator, so a service that imported
    the guard and forgot to consult it, or consulted it in a different method, still fails.
    """
    translator = translator_of(service)

    assert translator is not None, f"{service} writes audit events but has no _translate"
    assert audit_guards(translator), f"{service} never consults the audit guard"


@pytest.mark.parametrize("service", AUDIT_WRITERS, ids=WRITER_IDS)
def test_the_guard_runs_before_any_domain_attribution(service: ServiceClass) -> None:
    """Placement is the whole point: a guard after the SQLSTATE branches would never be
    reached, because those branches already returned a domain-specific sentence."""
    checked = 0
    for function in ast.walk(service.node):
        if not isinstance(function, ast.FunctionDef) or function.name != "_translate":
            continue
        guards = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "is_audit_integrity_failure"
        ]
        returns = [
            node.lineno
            for node in ast.walk(function)
            if isinstance(node, ast.Return) and node.lineno > min(guards, default=10**9)
        ]
        if not guards:
            continue
        checked += 1
        first_return = min(
            node.lineno for node in ast.walk(function) if isinstance(node, ast.Return)
        )
        assert min(guards) <= first_return, f"{service}: the guard is not the first decision"
        assert returns, f"{service}: nothing follows the guard"

    assert checked, f"{service} has no guarded _translate"


@pytest.mark.parametrize("name", sorted(NON_AUDIT_SERVICES))
def test_a_service_that_writes_no_audit_event_is_not_discovered(name: str) -> None:
    """Unreachable code is not a safeguard.

    These record nothing, so an audit-relation failure cannot arise inside their transaction
    and a guard there would say otherwise. The check is that DISCOVERY leaves them out -- if
    one of them ever starts writing audit events, it is discovered, and the coverage tests
    above then demand its guard automatically.
    """
    discovered = {service.module for service in AUDIT_WRITERS}

    assert name not in discovered
    source = code_only((APP / "services" / f"{name}.py").read_text(encoding="utf-8"))
    assert "is_audit_integrity_failure" not in source, f"{name} carries a guard it can never reach"


# ======================================================================================
# Booking: the relation decides which dependents are blamed
# ======================================================================================


def test_the_booking_dependent_relations_are_the_three_the_schema_declares() -> None:
    from app.services.booking import BOOKING_DEPENDENT_RELATIONS

    assert set(BOOKING_DEPENDENT_RELATIONS) == {"payments", "revenue", "reviews"}
    assert "audit_events" not in BOOKING_DEPENDENT_RELATIONS


def test_the_booking_service_checks_the_relation_before_blaming_a_dependent() -> None:
    source = code_only((APP / "services" / "booking.py").read_text(encoding="utf-8"))

    assert source.count("relation_of(exc) in BOOKING_DEPENDENT_RELATIONS") == 2, (
        "both the delete path and the shared translation must be relation-aware"
    )


def test_the_dependency_message_is_reachable_only_through_that_check() -> None:
    """The sentence naming payments, revenue and reviews appears once, behind the guard."""
    source = code_only((APP / "services" / "booking.py").read_text(encoding="utf-8"))

    assert source.count("payments, revenue or reviews") == 1


# ======================================================================================
# One sentence for "I cannot account for this"
# ======================================================================================


def test_the_generic_conflict_sentence_is_declared_once() -> None:
    literal = "The request conflicts with the current state of the database."
    offenders = [
        name for name, source in sources().items() if name != "core/errors.py" and literal in source
    ]

    assert offenders == [], offenders


def test_the_generic_sentence_names_nothing_specific() -> None:
    """It is what a service says when it does NOT know what failed, so it must not guess."""
    for banned in ("payment", "booking", "audit", "constraint", "table", "SQL", "foreign key"):
        assert banned.lower() not in GENERIC_CONFLICT_MESSAGE.lower(), banned


def test_the_shared_helpers_are_exported() -> None:
    for symbol in (
        "constraint_name_of",
        "relation_of",
        "is_audit_integrity_failure",
        "AUDIT_RELATIONS",
        "GENERIC_CONFLICT_MESSAGE",
    ):
        assert symbol in errors.__all__, symbol
        assert hasattr(errors, symbol), symbol


def test_no_service_leaks_a_relation_or_constraint_name_into_a_message() -> None:
    """The names are read to DECIDE, never to report. A message naming a table or a constraint
    would hand a client the schema."""
    for name, source in sources().items():
        if not name.startswith("services/"):
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            rendered = ast.unparse(node)
            for banned in ("constraint_name_of", "relation_of", "{constraint}", "{relation}"):
                assert banned not in rendered, f"{name} interpolates {banned}: {rendered}"


def test_the_signature_of_the_shared_helpers_is_the_one_services_use() -> None:
    for helper in (constraint_name_of, relation_of, is_audit_integrity_failure):
        parameters = list(inspect.signature(helper).parameters)
        assert parameters == ["error"], helper.__name__


# ======================================================================================
# Stage 4.5.17 -- the classification the attribution feeds
#
# Stage 4.5.16 decided WHOSE fault a failure is. Stage 4.5.17 decides what that means for the
# caller. The two must stay joined: an audit-relation failure is recognised by the attribution
# rule and classified by this one, and neither is allowed to drift into the other's job.
# ======================================================================================


def test_the_internal_fault_is_a_server_error() -> None:
    from app.core.errors import InternalFaultError

    assert InternalFaultError().status_code == 500


def test_the_internal_fault_is_never_a_conflict() -> None:
    """The regression this stage exists to prevent. A subclass relationship in either
    direction would put a server fault back in the 4xx class."""
    from app.core.errors import ConflictError, InternalFaultError

    assert not issubclass(InternalFaultError, ConflictError)
    assert not issubclass(ConflictError, InternalFaultError)
    assert InternalFaultError().status_code != ConflictError().status_code


def test_no_handler_maps_the_internal_fault_to_anything_but_its_own_status() -> None:
    """It travels the shared ``AppError`` handler, which returns ``exc.status_code``.

    The handler function's own body is inspected, not the rest of the module -- ``__all__``
    names the class further down, and a text split would have read that as a special case.
    """
    from app.core.errors import InternalFaultError

    tree = ast.parse((APP / "core" / "errors.py").read_text(encoding="utf-8"))
    registrar = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "register_exception_handlers"
    )

    assert "InternalFaultError" not in ast.unparse(registrar), (
        "a handler special-cases the internal fault, which is how its status gets rewritten"
    )
    assert InternalFaultError().status_code == 500


@pytest.mark.parametrize("service", AUDIT_WRITERS, ids=WRITER_IDS)
def test_the_audit_guard_returns_the_internal_fault(service: ServiceClass) -> None:
    """The classification, checked in the branch that makes it, on every discovered writer."""
    translator = translator_of(service)
    assert translator is not None

    assert "internal_fault(exc)" in guarded_source(translator), (
        f"{service}: the audit guard does not classify as an internal fault"
    )


@pytest.mark.parametrize("service", AUDIT_WRITERS, ids=WRITER_IDS)
def test_the_audit_guard_returns_no_conflict(service: ServiceClass) -> None:
    """Specifically: the guarded branch must not hand back a 409 of any wording."""
    translator = translator_of(service)
    assert translator is not None

    assert "ConflictError" not in guarded_source(translator), (
        f"{service}: the audit guard still returns a conflict"
    )


@pytest.mark.parametrize("service", AUDIT_WRITERS, ids=WRITER_IDS)
def test_every_audit_writer_keeps_its_client_conflict_fallback(service: ServiceClass) -> None:
    """The 500 was added beside the 409s, not instead of them.

    Checked outside the guard's own branch, so a translator whose ONLY remaining
    ``ConflictError`` was the one the guard used to return would fail here.
    """
    translator = translator_of(service)
    assert translator is not None

    assert has_conflict_fallback(translator), (
        f"{service}: every client conflict became an internal fault"
    )


def test_the_audit_guard_cannot_reach_the_booking_dependency_message() -> None:
    """The Stage 4.5.15 defect, pinned from the other side.

    The sentence naming payments, revenue and reviews sits behind a relation check that
    excludes ``audit_events`` -- so no audit failure can arrive at it however the surrounding
    code is rearranged.
    """
    from app.services.booking import BOOKING_DEPENDENT_RELATIONS

    source = code_only((APP / "services" / "booking.py").read_text(encoding="utf-8"))

    assert "audit_events" not in BOOKING_DEPENDENT_RELATIONS
    assert source.count("payments, revenue or reviews") == 1
    assert source.count("relation_of(exc) in BOOKING_DEPENDENT_RELATIONS") == 2


def test_the_internal_fault_is_constructed_with_no_message_anywhere() -> None:
    """A per-site message is exactly where a constraint name or a table would eventually be
    interpolated. Every construction takes the safe default."""
    for name, source in sources().items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "InternalFaultError":
                continue
            assert not node.args and not node.keywords, f"{name}: {ast.unparse(node)}"


def test_only_the_shared_factory_constructs_the_internal_fault() -> None:
    """One classification site, so the log line and the error cannot be raised apart."""
    builders = [
        name
        for name, source in sources().items()
        if "InternalFaultError()" in source and name != "core/errors.py"
    ]

    assert builders == [], builders


def test_the_factory_logs_without_a_traceback() -> None:
    """``exc_info`` would render the chained ``IntegrityError``, whose message carries the
    offending row. Services are already forbidden from doing this; so is the helper they call.
    """
    source = code_only((APP / "core" / "errors.py").read_text(encoding="utf-8"))
    factory = source[source.index("def internal_fault") : source.index("def error_response")]

    assert "exc_info" not in factory
    assert "logger.exception" not in factory
    assert "logger.error" in factory


def test_the_factory_logs_only_the_two_safe_facts() -> None:
    source = code_only((APP / "core" / "errors.py").read_text(encoding="utf-8"))
    factory = source[source.index("def internal_fault") : source.index("def error_response")]

    assert "sqlstate_of(error)" in factory
    assert "relation_of(error)" in factory
    assert "constraint_name_of" not in factory, "a constraint name in a log line is schema detail"
    assert "str(error)" not in factory, "the driver message renders the offending row"


def test_client_conflict_mappings_were_not_disturbed() -> None:
    """The 409s this stage must not have moved, asserted as classes rather than as messages."""
    from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError

    assert ConflictError().status_code == 409
    assert NotFoundError().status_code == 404
    assert ValidationError().status_code == 422
    assert ForbiddenError().status_code == 403


def test_no_service_returns_an_internal_fault_for_a_client_conflict() -> None:
    """ "every IntegrityError = 500" is the wrong rule, and this is what would catch it.

    Each audit-writing service classifies exactly once -- inside the guard -- and its other
    branches still return conflicts.
    """
    for service in AUDIT_WRITERS:
        source = ast.unparse(service.node)
        assert source.count("internal_fault(exc)") == source.count(
            "is_audit_integrity_failure(exc)"
        ), f"{service}: internal faults are raised somewhere other than the audit guard"
        assert "ConflictError" in source, f"{service}: all conflicts became faults"


# ======================================================================================
# Stage 4.5.18 -- the coverage set is derived, not maintained
#
# Stages 4.5.16 and 4.5.17 both carried the same finding forward: the services needing the
# audit guard were listed by hand in this file. A list is only correct on the day it is
# written. The seventh audit-writing service would not have failed anything -- the tests would
# simply never have looked at it, which is the worst way for a safeguard to be absent.
#
# Everything above now parametrises over ``AUDIT_WRITERS``, derived by walking the AST. This
# section tests the derivation itself.
# ======================================================================================


def parse_service(
    source: str, *, module: str = "app.services.thing", among: dict[str, str] | None = None
) -> ServiceClass:
    """The first class in a synthetic module, for exercising the rule on services that do not
    exist -- so the demonstration never touches a production file.

    The synthetic module carries its own real import statements and is resolved through the same
    :func:`import_table` production modules go through, so a fixture cannot pass by naming a type
    it never imported.
    """
    tree = ast.parse(source)
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    return ServiceClass(
        module.rpartition(".")[2], node.name, node, import_table(tree, module, among)
    )


#: A service that writes audit events and forgets the guard. The regression this stage exists
#: to make impossible: before discovery, a module like this simply went unexamined.
UNGUARDED_WRITER = """
from app.services.audit import AuditTrail


class ThingService:
    def __init__(self, session: Session, repository: ThingRepository, audit: AuditTrail) -> None:
        self._session = session
        self._audit = audit

    def create(self, payload: ThingCreate) -> ThingResponse:
        created = self._repository.add(Thing())
        self._audit.record(AuditAction.THING_CREATED, AuditResourceType.THING, created.code)
        self._session.commit()
        return ThingResponse()

    def _translate(self, exc: IntegrityError) -> Exception:
        state = sqlstate_of(exc)
        if state == SQLSTATE_UNIQUE_VIOLATION:
            return ConflictError("That thing already exists.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)
"""

#: The same service, done correctly.
GUARDED_WRITER = """
from app.services.audit import AuditTrail


class ThingService:
    def __init__(self, session: Session, repository: ThingRepository, audit: AuditTrail) -> None:
        self._session = session
        self._audit = audit

    def create(self, payload: ThingCreate) -> ThingResponse:
        created = self._repository.add(Thing())
        self._audit.record(AuditAction.THING_CREATED, AuditResourceType.THING, created.code)
        self._session.commit()
        return ThingResponse()

    def _translate(self, exc: IntegrityError) -> Exception:
        state = sqlstate_of(exc)
        if is_audit_integrity_failure(exc):
            return internal_fault(exc)
        if state == SQLSTATE_UNIQUE_VIOLATION:
            return ConflictError("That thing already exists.")
        return ConflictError(GENERIC_CONFLICT_MESSAGE)
"""

#: A writer whose guard exists but still returns a 409 -- the Stage 4.5.17 regression.
MISCLASSIFYING_WRITER = GUARDED_WRITER.replace(
    "return internal_fault(exc)", "return ConflictError(GENERIC_CONFLICT_MESSAGE)"
)

#: Translates integrity errors, writes no audit event. Must not be swept in.
NON_WRITER = """
from app.repositories.thing import ThingRepository


class ThingService:
    def __init__(self, session: Session, repository: ThingRepository) -> None:
        self._session = session

    def _translate(self, exc: IntegrityError) -> Exception:
        return ConflictError(GENERIC_CONFLICT_MESSAGE)
"""

#: Holds the abstraction and never uses it. Not a writer either.
INERT_HOLDER = """
from app.services.audit import AuditTrail


class ThingService:
    def __init__(self, session: Session, audit: AuditTrail) -> None:
        self._audit = audit

    def read(self, code: str) -> ThingResponse:
        return ThingResponse()
"""

#: A translator whose only conflict is the guard's own -- it has lost its 409s.
ONLY_GUARDED_CONFLICT = """
from app.services.audit import AuditTrail


class ThingService:
    def __init__(self, session: Session, audit: AuditTrail) -> None:
        self._audit = audit

    def create(self) -> None:
        self._audit.record(1, 2, "3")

    def _translate(self, exc: IntegrityError) -> Exception:
        if is_audit_integrity_failure(exc):
            return ConflictError(GENERIC_CONFLICT_MESSAGE)
        return internal_fault(exc)
"""


# --- the derivation itself ----------------------------------------------------------------


def test_discovery_finds_the_audit_writing_classes() -> None:
    """Non-vacuous, and at CLASS granularity.

    The count and the module set are a sanity check on the current architecture, not the
    mechanism: nothing above reads them to decide what to verify.
    """
    assert AUDIT_WRITERS, "discovery found nothing -- every coverage test above is vacuous"
    assert len(AUDIT_WRITERS) == EXPECTED_WRITER_COUNT, WRITER_IDS
    assert {service.module for service in AUDIT_WRITERS} == EXPECTED_WRITER_MODULES


def test_discovery_distinguishes_classes_within_one_module() -> None:
    """What the old module-level list could not say.

    ``finance.py`` defines four services and only the two catalogue ones write audit events. A
    list naming "finance" was satisfied by a guard anywhere in the file; this is satisfied only
    by a guard on each class that needs one.
    """
    finance = {service.name for service in AUDIT_WRITERS if service.module == "finance"}

    assert finance == {"RevenueCategoryService", "ExpenseCategoryService"}
    everything = {s.name for s in service_classes() if s.module == "finance"}
    assert {"RevenueService", "ExpenseService"} <= everything - finance


def test_every_discovered_writer_really_records() -> None:
    """Both halves of the signal, so the derived set is what it claims to be."""
    for service in AUDIT_WRITERS:
        assert holds_the_audit_abstraction(service), service
        assert records_an_audit_event(service), service


def test_the_abstraction_is_the_only_way_into_the_audit_table() -> None:
    """Discovery keys on ``AuditTrail``, which is only sound if nothing bypasses it.

    ``AuditRepository`` is imported by the audit module alone; a service reaching for it
    directly would write audit events discovery could not see, and this is where that surfaces.
    """
    offenders = [
        name
        for name, source in sources().items()
        if name.startswith("services/")
        and name != "services/audit.py"
        and "AuditRepository" in source
    ]

    assert offenders == [], offenders


def test_discovery_is_not_fooled_by_prose() -> None:
    """A live example, not a hypothetical.

    ``services/retention.py`` names ``AuditTrail`` in its module docstring -- explaining why the
    archival job deliberately does NOT record an event. A text search would classify it as an
    audit writer and demand a guard for a failure it cannot have.
    """
    assert "AuditTrail" in (APP / "services" / "retention.py").read_text(encoding="utf-8")
    assert "retention" not in {service.module for service in AUDIT_WRITERS}


def test_the_audit_module_is_not_itself_a_writer() -> None:
    """It IS the mechanism, and it needs no exclusion list: ``AuditTrail`` takes a repository
    and an actor, not another trail, so the predicate passes over it on its own."""
    assert "audit" not in {service.module for service in AUDIT_WRITERS}


# --- the rule applied to services that do not exist ---------------------------------------


def test_a_new_audit_writer_is_discovered_automatically() -> None:
    """The point of the stage: nobody has to add it to a list."""
    assert writes_audit_events(parse_service(UNGUARDED_WRITER))
    assert writes_audit_events(parse_service(GUARDED_WRITER))


def test_a_new_audit_writer_without_the_guard_fails_coverage() -> None:
    """The regression this stage prevents, demonstrated end to end on a synthetic service.

    Discovery finds it; the coverage rule then rejects it. No production file is touched, and
    the assertions are the ones every real writer is held to above.
    """
    unguarded = parse_service(UNGUARDED_WRITER)
    translator = translator_of(unguarded)

    assert writes_audit_events(unguarded), "discovery missed a writer"
    assert translator is not None
    assert audit_guards(translator) == [], "the fixture is not actually unguarded"
    assert "internal_fault(exc)" not in guarded_source(translator)


def test_a_new_audit_writer_with_a_misclassifying_guard_fails_coverage() -> None:
    """A guard that returns a 409 is the Stage 4.5.17 regression, and it is caught too."""
    misclassifying = parse_service(MISCLASSIFYING_WRITER)
    translator = translator_of(misclassifying)
    assert translator is not None

    assert audit_guards(translator), "the fixture has no guard at all"
    assert "internal_fault(exc)" not in guarded_source(translator)
    assert "ConflictError" in guarded_source(translator)


def test_a_correct_new_audit_writer_passes_coverage() -> None:
    """The positive control. Without it, the two tests above would pass for a rule that
    rejects everything."""
    guarded = parse_service(GUARDED_WRITER)
    translator = translator_of(guarded)
    assert translator is not None

    assert writes_audit_events(guarded)
    assert "internal_fault(exc)" in guarded_source(translator)
    assert "ConflictError" not in guarded_source(translator)
    assert has_conflict_fallback(translator)


def test_a_service_that_only_translates_is_not_discovered() -> None:
    """ "every service with ``_translate`` needs a guard" is the rule this must not become: it
    would put unreachable code into four services to satisfy a test."""
    non_writer = parse_service(NON_WRITER)

    assert translator_of(non_writer) is not None
    assert not writes_audit_events(non_writer)


def test_holding_the_abstraction_without_using_it_is_not_writing() -> None:
    inert = parse_service(INERT_HOLDER)

    assert holds_the_audit_abstraction(inert)
    assert not records_an_audit_event(inert)
    assert not writes_audit_events(inert)


def test_the_conflict_fallback_check_ignores_the_guards_own_branch() -> None:
    """A translator whose only remaining ``ConflictError`` was the guard's would have lost its
    409s. The check looks outside the guard, and this proves it does."""
    stripped = translator_of(parse_service(ONLY_GUARDED_CONFLICT))
    correct = translator_of(parse_service(GUARDED_WRITER))
    assert stripped is not None and correct is not None

    assert not has_conflict_fallback(stripped)
    assert has_conflict_fallback(correct)


# ======================================================================================
# Stage 4.5.19 -- the abstraction is resolved, not spelled
#
# Stage 4.5.18 derived the coverage set instead of listing it, and carried one finding forward:
# the derivation recognised the abstraction by the identifier ``AuditTrail`` appearing in an
# annotation. That is a text test wearing an AST costume. Two things follow from it, and both
# are wrong in the same direction as the list it replaced -- silently:
#
#   * ``from app.services.audit import AuditTrail as Trail`` renames the abstraction locally,
#     and the service drops out of coverage. Nothing fails; the tests simply stop looking.
#   * ``audit: FakeAuditTrail`` contains the identifier, and a service that holds a test double
#     is swept in as a real writer.
#
# The annotation is now resolved through the module's own import table to the one thing an
# importing module cannot rename: where the class is defined.
# ======================================================================================


def service_modules() -> dict[str, ImportTable]:
    """Each service module's import table, keyed by module stem."""
    return {
        path.stem: import_table(
            ast.parse(path.read_text(encoding="utf-8")), f"{SERVICE_PACKAGE}.{path.stem}"
        )
        for path in sorted((APP / "services").glob("*.py"))
        if path.name != "__init__.py"
    }


def writer_with(imports: str, annotation: str) -> str:
    """A synthetic audit-writing service that differs from the others below in NOTHING except
    how it names the abstraction.

    Holding the rest of the service constant is the point: any difference in the verdict is
    attributable to the import and the annotation, and to nothing else.
    """
    return (
        f"{imports}\n\n\n"
        "class ThingService:\n"
        f"    def __init__(self, session: Session, trail: {annotation}) -> None:\n"
        "        self._audit = trail\n"
        "\n"
        "    def create(self) -> None:\n"
        '        self._audit.record(AuditAction.THING_CREATED, AuditResourceType.THING, "x")\n'
    )


#: The form the codebase actually uses. The control: if this stopped being discovered, every
#: verdict below would be measuring a broken resolver rather than an import form.
DIRECT_IMPORT = writer_with("from app.services.audit import AuditTrail", "AuditTrail")

#: The four ways of naming the same class that Stage 4.5.18 would have missed.
ALIASED_IMPORT = writer_with("from app.services.audit import AuditTrail as Trail", "Trail")
ALIASED_MODULE = writer_with("import app.services.audit as audit", "audit.AuditTrail")
PLAIN_MODULE = writer_with("import app.services.audit", "app.services.audit.AuditTrail")
SUBMODULE_IMPORT = writer_with("from app.services import audit", "audit.AuditTrail")

#: Not a form this project uses -- see
#: :func:`test_the_project_names_the_abstraction_absolutely` -- but cheap to resolve correctly
#: and wrong to resolve as ``app.audit``.
RELATIVE_ALIASED = writer_with("from .audit import AuditTrail as Trail", "Trail")

#: Annotation shapes that mention the abstraction alongside something else.
QUOTED_ALIAS = writer_with(
    "from app.services.audit import AuditTrail as Trail", chr(34) + "Trail" + chr(34)
)
OPTIONAL_ALIAS = writer_with("from app.services.audit import AuditTrail as Trail", "Trail | None")

#: Somebody else's ``Trail``. Same local name, same annotation text, different class.
WRONG_ALIAS = writer_with("from app.services.trails import Trail", "Trail")

#: The false positive the old substring rule accepted: a test double whose name CONTAINS the
#: abstraction's, in a module that also imports the real one.
LOOKALIKE_ANNOTATION = writer_with(
    "from app.services.audit import AuditTrail\nfrom tests.doubles import FakeAuditTrail",
    "FakeAuditTrail",
)

#: A dependency with no annotation at all resolves to nothing, and nothing is not the
#: abstraction.
UNANNOTATED_DEPENDENCY = """
from app.services.audit import AuditTrail


class ThingService:
    def __init__(self, session, trail) -> None:
        self._audit = trail

    def create(self) -> None:
        self._audit.record(1, 2, "3")
"""

#: An aliased writer that forgets the guard -- alias resolution feeding the coverage rule, not
#: just the discovery predicate.
ALIASED_UNGUARDED_WRITER = UNGUARDED_WRITER.replace(
    "from app.services.audit import AuditTrail",
    "from app.services.audit import AuditTrail as Trail",
).replace("audit: AuditTrail", "audit: Trail")


# --- the forms that must be discovered ------------------------------------------------------


def test_the_direct_import_is_still_discovered() -> None:
    """The control, and the form every real service uses today."""
    assert writes_audit_events(parse_service(DIRECT_IMPORT))


@pytest.mark.parametrize(
    ("source", "form"),
    [
        (ALIASED_IMPORT, "from app.services.audit import AuditTrail as Trail"),
        (ALIASED_MODULE, "import app.services.audit as audit"),
        (PLAIN_MODULE, "import app.services.audit"),
        (SUBMODULE_IMPORT, "from app.services import audit"),
        (RELATIVE_ALIASED, "from .audit import AuditTrail as Trail"),
        (QUOTED_ALIAS, "a quoted alias"),
        (OPTIONAL_ALIAS, "an optional alias"),
    ],
    ids=["aliased", "aliased-module", "plain-module", "submodule", "relative", "quoted", "union"],
)
def test_the_abstraction_is_discovered_under_any_name(source: str, form: str) -> None:
    """Every one of these is the same class, and Stage 4.5.18 would have found none of them."""
    assert writes_audit_events(parse_service(source)), form


def test_an_aliased_writer_without_the_guard_still_fails_coverage() -> None:
    """The end-to-end point of the stage.

    Discovery is not the deliverable; coverage is. An aliased writer must reach the same
    verdict as a directly imported one -- found, then rejected for having no guard.
    """
    aliased = parse_service(ALIASED_UNGUARDED_WRITER)
    translator = translator_of(aliased)

    assert writes_audit_events(aliased), "the alias hid the writer"
    assert translator is not None
    assert audit_guards(translator) == [], "the fixture is not actually unguarded"


# --- the forms that must NOT be discovered ---------------------------------------------------


def test_a_different_class_under_the_same_alias_is_not_discovered() -> None:
    """``Trail`` is not a type. It is a name, and it means whatever the import says."""
    assert not writes_audit_events(parse_service(WRONG_ALIAS))


def test_a_name_that_merely_contains_the_abstraction_is_not_discovered() -> None:
    """The false positive the old rule had, demonstrated rather than asserted away.

    ``FakeAuditTrail`` contains ``AuditTrail``. The substring rule said yes; resolution says the
    annotation denotes ``tests.doubles.FakeAuditTrail``, which is not the abstraction.
    """
    service = parse_service(LOOKALIKE_ANNOTATION)
    init = initialiser(service.node)
    assert init is not None
    annotation = init.args.args[-1].annotation
    assert annotation is not None

    assert AUDIT_ABSTRACTION in ast.unparse(annotation), "the fixture is not a lookalike"
    assert service.imports.resolved(annotation) == {"tests.doubles.FakeAuditTrail"}
    assert not writes_audit_events(service)


def test_an_unannotated_dependency_is_not_discovered() -> None:
    """Holding something called ``trail`` proves nothing about what it is."""
    assert not writes_audit_events(parse_service(UNANNOTATED_DEPENDENCY))


def test_the_two_halves_of_the_signal_both_survive_resolution() -> None:
    """Stage 4.5.18's rule is unchanged: hold the abstraction AND record. Alias awareness
    widened how the first half is recognised, not what it requires."""
    inert = parse_service(
        writer_with("from app.services.audit import AuditTrail as Trail", "Trail").replace(
            'self._audit.record(AuditAction.THING_CREATED, AuditResourceType.THING, "x")',
            "pass",
        )
    )

    assert holds_the_audit_abstraction(inert)
    assert not records_an_audit_event(inert)
    assert not writes_audit_events(inert)


# --- the resolution machinery itself ---------------------------------------------------------


def test_the_import_table_binds_each_form_it_claims_to() -> None:
    table = import_table(
        ast.parse(
            "import app.services.audit as audit\n"
            "from app.services.audit import AuditTrail as Trail\n"
            "from app.services import audit as package_audit\n"
        ),
        f"{SERVICE_PACKAGE}.thing",
    )

    assert table.types["Trail"] == AUDIT_QUALNAME
    assert table.modules["audit"] == AUDIT_MODULE
    assert table.modules["package_audit"] == AUDIT_MODULE


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("from app.services.audit import AuditTrail", "app.services.audit"),
        ("from .audit import AuditTrail", "app.services.audit"),
        ("from ..services.audit import AuditTrail", "app.services.audit"),
        ("from . import audit", "app.services"),
    ],
    ids=["absolute", "sibling", "parent", "package"],
)
def test_a_relative_import_resolves_against_its_own_package(statement: str, expected: str) -> None:
    """The module being read is what makes a relative import resolvable, which is why
    :func:`import_table` is told its own name rather than inferring one."""
    node = ast.parse(statement).body[0]
    assert isinstance(node, ast.ImportFrom)

    assert origin_of(node, f"{SERVICE_PACKAGE}") == expected


def test_an_unresolvable_annotation_denotes_nothing() -> None:
    """Resolution fails closed. A name with no binding is not quietly treated as a match."""
    table = import_table(ast.parse("x = 1"), f"{SERVICE_PACKAGE}.thing")

    assert table.resolved(ast.parse("AuditTrail", mode="eval").body) == set()
    assert table.resolved(None) == set()


# --- the resolution applied to the real architecture -----------------------------------------


@pytest.mark.parametrize("service", AUDIT_WRITERS, ids=WRITER_IDS)
def test_every_real_writer_resolves_to_the_abstraction_itself(service: ServiceClass) -> None:
    """Not to something spelled like it: to ``app.services.audit.AuditTrail``."""
    init = initialiser(service.node)
    assert init is not None
    resolved = {
        name
        for argument in [*init.args.args, *init.args.kwonlyargs]
        for name in service.imports.identities(argument.annotation)
    }

    assert AUDIT_QUALNAME in resolved


def test_every_module_that_binds_the_abstraction_produces_a_writer() -> None:
    """A cross-check from the opposite direction, and the one that would catch a resolver that
    had silently stopped resolving: a module importing the abstraction and contributing nothing
    to coverage is either a bug here or a service that holds it and never records."""
    binding = {
        module
        for module, table in service_modules().items()
        if AUDIT_QUALNAME in table.types.values() and f"{SERVICE_PACKAGE}.{module}" != AUDIT_MODULE
    }

    assert binding == {service.module for service in AUDIT_WRITERS}


def test_the_audit_module_resolves_its_own_class() -> None:
    """``services/audit.py`` names ``AuditTrail`` without importing it, and it must still mean
    the same class -- otherwise the module would be excluded from coverage by a resolution
    failure, and :func:`test_the_audit_module_is_not_itself_a_writer` would be asserting
    something it had not established."""
    assert service_modules()["audit"].types[AUDIT_ABSTRACTION] == AUDIT_QUALNAME

    trail = next(
        service
        for service in service_classes()
        if service.module == "audit" and service.name == AUDIT_ABSTRACTION
    )
    assert not holds_the_audit_abstraction(trail)


def test_only_the_audit_module_binds_the_audit_repository() -> None:
    """The semantic form of :func:`test_the_abstraction_is_the_only_way_into_the_audit_table`.

    The text version would still catch an aliased import, because the imported NAME survives
    the alias; this one does not depend on that happening to be true.
    """
    offenders = [
        module
        for module, table in service_modules().items()
        if AUDIT_REPOSITORY_QUALNAME in table.types.values()
        and f"{SERVICE_PACKAGE}.{module}" != AUDIT_MODULE
    ]

    assert offenders == [], offenders


def bindings_of_the_abstraction() -> list[tuple[str, ast.ImportFrom]]:
    """Every ``from ... import AuditTrail`` in a service module other than the audit module."""
    found: list[tuple[str, ast.ImportFrom]] = []
    for path in sorted((APP / "services").glob("*.py")):
        if path.name == "__init__.py" or f"{SERVICE_PACKAGE}.{path.stem}" == AUDIT_MODULE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and origin_of(node, SERVICE_PACKAGE) == AUDIT_MODULE
                and any(alias.name == AUDIT_ABSTRACTION for alias in node.names)
            ):
                found.append((path.stem, node))
    return found


def test_the_project_names_the_abstraction_absolutely() -> None:
    """Relative-import support is precautionary, and this records that.

    Every binding today comes from the absolute form, so the relative branch of
    :func:`origin_of` is exercised only by the fixtures above. If that ever changes, the
    resolver already handles it -- but the claim being made here is about the codebase.

    What this deliberately does NOT constrain is the local NAME. An earlier draft of this
    test asserted that every binding was still spelled ``AuditTrail``, and mutating a real
    service to ``import AuditTrail as Trail`` showed what that cost: discovery and coverage
    both held, and this one test failed -- forbidding at the suite level the very thing the
    resolver was built to tolerate. A guard that punishes the safe change is not a guard.
    """
    bindings = bindings_of_the_abstraction()

    assert {module for module, _ in bindings} == {service.module for service in AUDIT_WRITERS}
    relative = [(module, ast.unparse(node)) for module, node in bindings if node.level]
    assert relative == [], relative


# ======================================================================================
# Stage 4.5.20 -- the abstraction is followed, not just resolved
#
# Stage 4.5.19 resolved a local name to the qualified thing it was bound to, which answers
# "what does this name mean HERE". One module between the service and the abstraction turns
# that into the wrong question: given
#
#     app/services/helpers.py   from app.services.audit import AuditTrail
#     app/services/payment.py   from app.services.helpers import AuditTrail
#
# the binding is app.services.helpers.AuditTrail -- a true statement, and not the identity of
# the class. The service drops out of coverage while every name in sight still reads AuditTrail.
#
# The resolver now follows a binding THROUGH a module when that module's own top-level AST
# establishes where the name came from, and refuses to when it does not. Refusing is the
# failure mode that matters here, so the cases below are weighted towards it.
# ======================================================================================


def universe(**modules: str) -> dict[str, str]:
    """``PROJECT_SOURCES`` plus synthetic modules under the service package.

    The synthetic modules exist as SOURCE and nothing else. No file is written into
    ``app/services`` to make a test pass, and nothing is imported at runtime -- which is what
    keeps a fixture from becoming a fake production module.
    """
    return universe_of({f"{SERVICE_PACKAGE}.{name}": source for name, source in modules.items()})


def universe_of(modules: dict[str, str]) -> dict[str, str]:
    """``PROJECT_SOURCES`` plus synthetic modules named in full.

    The general form, for fixtures that live outside the service package -- which is the
    whole subject of Stage 4.5.21 and cannot be expressed by the keyword form above.
    """
    return {**PROJECT_SOURCES, **modules}


#: The re-export forms a chain may legitimately pass through.
REEXPORTS_THE_ABSTRACTION = "from app.services.audit import AuditTrail\n"
REEXPORTS_UNDER_AN_ALIAS = "from app.services.audit import AuditTrail as Trail\n"
REEXPORTS_BY_ASSIGNMENT = "import app.services.audit as audit\n\nAuditTrail = audit.AuditTrail\n"

#: And the forms that establish nothing, each failing closed for its own reason.
DEFINES_ITS_OWN = "class AuditTrail:\n    pass\n"
REEXPORTS_A_DOUBLE = "from tests.doubles import FakeAuditTrail as AuditTrail\n"
ASSIGNS_DYNAMICALLY = "AuditTrail = something_dynamic()\n"
IMPORTS_CONDITIONALLY = (
    "try:\n"
    "    from app.services.audit import AuditTrail\n"
    "except ImportError:\n"
    "    AuditTrail = None\n"
)


def consumer_of(module: str, name: str = AUDIT_ABSTRACTION) -> str:
    """A writer that reaches the abstraction through *module*, and is otherwise identical to
    every other synthetic writer here."""
    return writer_with(f"from {SERVICE_PACKAGE}.{module} import {name}", name)


# --- chains that must be followed -----------------------------------------------------------


def test_a_re_exported_abstraction_is_discovered() -> None:
    """The case the stage exists for: one module in between, nothing else different."""
    service = parse_service(
        consumer_of("helpers"), among=universe(helpers=REEXPORTS_THE_ABSTRACTION)
    )

    assert writes_audit_events(service)


def test_an_aliased_re_export_is_discovered() -> None:
    """The helper renames it, the consumer imports the new name, and it is still the class."""
    service = parse_service(
        consumer_of("helpers", "Trail"), among=universe(helpers=REEXPORTS_UNDER_AN_ALIAS)
    )

    assert writes_audit_events(service)


def test_an_assignment_re_export_is_discovered() -> None:
    """``AuditTrail = audit.AuditTrail`` -- the one assignment shape that proves its own target.

    One name, assigned once, from an attribute of a module this file imported. Anything less
    determinate than that is refused below.
    """
    service = parse_service(consumer_of("helpers"), among=universe(helpers=REEXPORTS_BY_ASSIGNMENT))

    assert writes_audit_events(service)


@pytest.mark.parametrize("hops", [2, 3, MAX_REEXPORT_HOPS - 1], ids=["two", "three", "the-bound"])
def test_a_chain_within_the_bound_is_followed_to_the_end(hops: int) -> None:
    """Length is not what makes a chain resolvable; each link establishing its target is."""
    links = {
        f"hop_{index}": (
            REEXPORTS_THE_ABSTRACTION
            if index == hops - 1
            else f"from {SERVICE_PACKAGE}.hop_{index + 1} import {AUDIT_ABSTRACTION}\n"
        )
        for index in range(hops)
    }

    assert writes_audit_events(parse_service(consumer_of("hop_0"), among=universe(**links)))


# --- chains that must not be followed ---------------------------------------------------------


@pytest.mark.parametrize(
    ("helper", "why"),
    [
        (DEFINES_ITS_OWN, "a class defined in the helper is the helper's own"),
        (REEXPORTS_A_DOUBLE, "a re-exported test double is not the abstraction"),
        (ASSIGNS_DYNAMICALLY, "a call establishes nothing statically"),
        (IMPORTS_CONDITIONALLY, "a binding under try/except is not an export"),
    ],
    ids=["local-class", "test-double", "dynamic", "conditional"],
)
def test_a_helper_that_establishes_nothing_is_not_followed(helper: str, why: str) -> None:
    """Four ways for a chain to fail, and one verdict for all of them.

    Each of these names ``AuditTrail`` at the point the consumer imports it, so spelling cannot
    tell them apart from the real re-export above. Only the helper's own AST can.
    """
    service = parse_service(consumer_of("helpers"), among=universe(helpers=helper))

    assert not writes_audit_events(service), why


def test_a_binding_from_a_module_the_resolver_cannot_read_is_not_discovered() -> None:
    """No source, no claim. The universe is the bound of the analysis, and outside it the chain
    simply ends."""
    service = parse_service(consumer_of("absent"), among=universe())
    init = initialiser(service.node)
    assert init is not None
    annotation = init.args.args[-1].annotation

    assert service.imports.identities(annotation) == {f"{SERVICE_PACKAGE}.absent.AuditTrail"}
    assert not writes_audit_events(service)


def test_a_re_export_cycle_terminates_and_resolves_to_nothing() -> None:
    """``a`` imports from ``b``, ``b`` imports from ``a``.

    The test completing at all is half the assertion -- a naive follower recurses here forever.
    The other half is that a cycle resolves to no identity, because a cycle defines nothing.
    """
    cyclic = universe(
        cycle_a=f"from {SERVICE_PACKAGE}.cycle_b import {AUDIT_ABSTRACTION}\n",
        cycle_b=f"from {SERVICE_PACKAGE}.cycle_a import {AUDIT_ABSTRACTION}\n",
    )

    assert follow_reexports(f"{SERVICE_PACKAGE}.cycle_a.{AUDIT_ABSTRACTION}", cyclic) != (
        AUDIT_QUALNAME
    )
    assert not writes_audit_events(parse_service(consumer_of("cycle_a"), among=cyclic))


def test_a_cycle_is_stopped_by_the_cycle_check_and_not_by_the_bound() -> None:
    """The bound alone would also terminate a cycle, so it would hide a missing cycle
    check. The difference the check makes is that the answer stops depending on how many
    hops were allowed: without it, an even and an odd budget land on opposite ends of the
    cycle.
    """
    cyclic = universe(
        cycle_a=f"from {SERVICE_PACKAGE}.cycle_b import {AUDIT_ABSTRACTION}\n",
        cycle_b=f"from {SERVICE_PACKAGE}.cycle_a import {AUDIT_ABSTRACTION}\n",
    )
    start = f"{SERVICE_PACKAGE}.cycle_a.{AUDIT_ABSTRACTION}"

    assert follow_reexports(start, cyclic, hops=2) == follow_reexports(start, cyclic, hops=3)
    assert follow_reexports(start, cyclic, hops=1000) != AUDIT_QUALNAME


def test_an_assignment_of_anything_but_a_plain_name_establishes_nothing() -> None:
    """``AuditTrail = list[audit.AuditTrail]`` MENTIONS the abstraction and is not it.

    The annotation grammar and the value grammar are different problems.
    :meth:`ImportTable.resolved` exists to answer the first, where reaching inside a
    subscript is exactly right -- ``Annotated[AuditTrail, ...]`` really does hold one. Reusing
    it on the right-hand side of an assignment inherits that reaching, which is wrong there:
    a container of the class is not the class. Hence the value-shape restriction, and hence
    this test, without which removing it changes nothing observable.
    """
    wrapped = "import app.services.audit as audit\n\nAuditTrail = list[audit.AuditTrail]\n"
    exports = export_table(f"{SERVICE_PACKAGE}.helpers", universe(helpers=wrapped))

    assert AUDIT_ABSTRACTION not in exports
    assert not writes_audit_events(
        parse_service(consumer_of("helpers"), among=universe(helpers=wrapped))
    )


def test_a_chain_longer_than_the_bound_is_not_followed_to_the_end() -> None:
    """The bound is what makes termination independent of the graph. Past it the resolver stops
    where it is, which is not the abstraction -- it under-claims rather than looping."""
    length = MAX_REEXPORT_HOPS + 2
    links = {
        f"long_{index}": (
            REEXPORTS_THE_ABSTRACTION
            if index == length - 1
            else f"from {SERVICE_PACKAGE}.long_{index + 1} import {AUDIT_ABSTRACTION}\n"
        )
        for index in range(length)
    }

    assert not writes_audit_events(parse_service(consumer_of("long_0"), among=universe(**links)))


# --- the rule itself is unchanged --------------------------------------------------------------


def test_a_translator_only_class_is_still_not_discovered() -> None:
    """A helper that genuinely re-exports the abstraction does not make a class that never
    receives it into a writer."""
    service = parse_service(NON_WRITER, among=universe(helpers=REEXPORTS_THE_ABSTRACTION))

    assert translator_of(service) is not None
    assert not writes_audit_events(service)


def test_an_inert_holder_of_a_re_exported_abstraction_is_not_discovered() -> None:
    """Both halves survive the new resolution: holding it through a re-export is still only the
    first half."""
    inert = parse_service(
        consumer_of("helpers").replace(
            'self._audit.record(AuditAction.THING_CREATED, AuditResourceType.THING, "x")', "pass"
        ),
        among=universe(helpers=REEXPORTS_THE_ABSTRACTION),
    )

    assert holds_the_audit_abstraction(inert)
    assert not records_an_audit_event(inert)
    assert not writes_audit_events(inert)


def test_a_re_export_module_exposes_the_abstraction_without_writing_anything() -> None:
    """A helper is not a writer for having the abstraction pass through it.

    Both halves of the claim are made here, because only together do they say anything: the
    module really does export the class, AND the class it contains is not a writer.
    """
    helper = REEXPORTS_THE_ABSTRACTION + (
        "\n\nclass HelperService:\n"
        "    def __init__(self, session: Session) -> None:\n"
        "        self._session = session\n"
    )
    exports = export_table(f"{SERVICE_PACKAGE}.helpers", universe(helpers=helper))

    assert exports[AUDIT_ABSTRACTION] == AUDIT_QUALNAME
    assert not writes_audit_events(parse_service(helper, module=f"{SERVICE_PACKAGE}.helpers"))


# --- coverage, not just discovery ---------------------------------------------------------------


def test_a_re_exporting_writer_with_the_guard_is_covered_and_passes() -> None:
    """The positive control for the pair below: found through the re-export, and correct."""
    service = parse_service(
        GUARDED_WRITER.replace(
            f"from {AUDIT_MODULE} import {AUDIT_ABSTRACTION}",
            f"from {SERVICE_PACKAGE}.helpers import {AUDIT_ABSTRACTION}",
        ),
        among=universe(helpers=REEXPORTS_THE_ABSTRACTION),
    )
    translator = translator_of(service)

    assert writes_audit_events(service)
    assert translator is not None
    assert "internal_fault" in guarded_source(translator)
    assert has_conflict_fallback(translator)


def test_a_re_exporting_writer_without_the_guard_still_fails_coverage() -> None:
    """The end-to-end point, and the reason discovery is not the deliverable.

    A writer reached through a re-export must arrive at the same verdict as a directly imported
    one: found, and then rejected for having no guard. If the resolver quietly stopped following
    re-exports, this service would be invisible and the suite would go green on it.
    """
    service = parse_service(
        UNGUARDED_WRITER.replace(
            f"from {AUDIT_MODULE} import {AUDIT_ABSTRACTION}",
            f"from {SERVICE_PACKAGE}.helpers import {AUDIT_ABSTRACTION}",
        ),
        among=universe(helpers=REEXPORTS_THE_ABSTRACTION),
    )
    translator = translator_of(service)

    assert writes_audit_events(service), "the re-export hid the writer"
    assert translator is not None
    assert audit_guards(translator) == [], "the fixture is not actually unguarded"


# --- the machinery itself --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("helper", "name", "expected"),
    [
        (REEXPORTS_THE_ABSTRACTION, AUDIT_ABSTRACTION, AUDIT_QUALNAME),
        (REEXPORTS_UNDER_AN_ALIAS, "Trail", AUDIT_QUALNAME),
        (REEXPORTS_BY_ASSIGNMENT, AUDIT_ABSTRACTION, AUDIT_QUALNAME),
        (DEFINES_ITS_OWN, AUDIT_ABSTRACTION, f"{SERVICE_PACKAGE}.helpers.{AUDIT_ABSTRACTION}"),
        (REEXPORTS_A_DOUBLE, AUDIT_ABSTRACTION, "tests.doubles.FakeAuditTrail"),
    ],
    ids=["direct", "aliased", "assigned", "local-class", "double"],
)
def test_the_export_table_says_what_each_form_exposes(
    helper: str, name: str, expected: str
) -> None:
    exports = export_table(f"{SERVICE_PACKAGE}.helpers", universe(helpers=helper))

    assert exports[name] == expected


@pytest.mark.parametrize(
    "helper",
    [ASSIGNS_DYNAMICALLY, IMPORTS_CONDITIONALLY],
    ids=["dynamic", "conditional"],
)
def test_the_export_table_exposes_nothing_it_cannot_establish(helper: str) -> None:
    """Absent from the table, not present-and-wrong. The difference matters: ``follow`` stops on
    a name a module does not export, and stopping is what fails closed."""
    exports = export_table(f"{SERVICE_PACKAGE}.helpers", universe(helpers=helper))

    assert AUDIT_ABSTRACTION not in exports


def test_the_export_table_reads_only_top_level_statements() -> None:
    """An import inside a function is not something the module exports, whatever it imports."""
    hidden = (
        "def build():\n    from app.services.audit import AuditTrail\n\n    return AuditTrail\n"
    )
    exports = export_table(f"{SERVICE_PACKAGE}.helpers", universe(helpers=hidden))

    assert exports == {}


def test_an_assignment_with_two_targets_establishes_nothing() -> None:
    """A name bound twice at module level has no single answer, so it gets none."""
    ambiguous = (
        "import app.services.audit as audit\n"
        "from tests.doubles import FakeAuditTrail\n"
        "\n"
        "AuditTrail = audit.AuditTrail\n"
        "AuditTrail = FakeAuditTrail\n"
    )
    exports = export_table(f"{SERVICE_PACKAGE}.helpers", universe(helpers=ambiguous))

    assert AUDIT_ABSTRACTION not in exports


def test_following_a_name_the_defining_module_owns_returns_it_unchanged() -> None:
    """The chain ends at the class, not one hop past it."""
    assert follow_reexports(AUDIT_QUALNAME, PROJECT_SOURCES) == AUDIT_QUALNAME


def test_following_stops_outside_the_universe() -> None:
    """``tests.doubles`` is not read, so nothing can be claimed about what it holds."""
    assert (
        follow_reexports("tests.doubles.FakeAuditTrail", PROJECT_SOURCES)
        == "tests.doubles.FakeAuditTrail"
    )


def test_the_binding_and_the_identity_are_different_questions() -> None:
    """Both are asked in this file, and this is the fixture on which they disagree.

    :meth:`ImportTable.resolved` says where the consumer got the name; :meth:`identities` says
    what it is. Keeping the two apart is what let the cross-checks below stay independent of the
    resolution the coverage set is derived from.
    """
    service = parse_service(
        consumer_of("helpers"), among=universe(helpers=REEXPORTS_THE_ABSTRACTION)
    )
    init = initialiser(service.node)
    assert init is not None
    annotation = init.args.args[-1].annotation

    assert service.imports.resolved(annotation) == {f"{SERVICE_PACKAGE}.helpers.AuditTrail"}
    assert service.imports.identities(annotation) == {AUDIT_QUALNAME}


# --- the real architecture ---------------------------------------------------------------------


@pytest.mark.parametrize("service", AUDIT_WRITERS, ids=WRITER_IDS)
def test_no_real_writer_needs_a_re_export_followed(service: ServiceClass) -> None:
    """Re-export support is precautionary, exactly as relative-import support is, and this
    records that rather than assuming it.

    Every writer today imports the abstraction from the module that defines it, so its binding
    and its identity are the same string. That is a claim about the codebase; the tests above
    are the claim about the resolver.
    """
    init = initialiser(service.node)
    assert init is not None
    bound = {
        name
        for argument in [*init.args.args, *init.args.kwonlyargs]
        for name in service.imports.resolved(argument.annotation)
    }

    assert AUDIT_QUALNAME in bound


def test_nothing_reaches_the_abstraction_through_an_intermediate_module() -> None:
    """The companion fact: the fixtures above describe nothing that exists yet.

    Note what is NOT claimed. Every ``from app.services.audit import AuditTrail`` makes
    the name reachable as ``that_module.AuditTrail``, so in Python's terms every
    importer re-exports it; an earlier draft asserted nobody did and was simply wrong
    about the language. The claim that means something is about INDIRECTION: no binding
    in the package arrives at the abstraction via a module in between.

    Like the relative-import record above, this is a statement about the codebase, not
    about the resolver, and it is the one test that will fail when an intermediary is
    introduced. That failure is intended and cheap -- coverage keeps working, because
    the resolver already follows the chain; what changes is that a claim made here has
    stopped being true and should be updated by whoever made it stop.
    """
    direct: list[str] = []
    indirect: list[str] = []
    for module in sorted(PROJECT_SOURCES):
        if module == AUDIT_MODULE:
            continue
        for name, target in sorted(export_table(module, PROJECT_SOURCES).items()):
            if follow_reexports(target, PROJECT_SOURCES) != AUDIT_QUALNAME:
                continue
            (direct if target == AUDIT_QUALNAME else indirect).append(f"{module}.{name}")

    assert indirect == [], indirect
    assert direct != [], "nothing binds the abstraction at all -- the traversal is blind"


def test_the_audit_repository_stays_unreachable_through_a_re_export() -> None:
    """The Stage 4.5.19 cross-check, made proof against the machinery this stage adds.

    That check asked whether a module BOUND ``AuditRepository`` by name. A re-export would have
    walked straight past it, so the same question is now asked of the identity: whatever any
    service module's top-level names ultimately denote, only the audit module may own that one.
    """
    offenders = sorted(
        module
        for module, table in service_modules().items()
        if f"{SERVICE_PACKAGE}.{module}" != AUDIT_MODULE
        and AUDIT_REPOSITORY_QUALNAME
        in {follow_reexports(target, PROJECT_SOURCES) for target in table.types.values()}
    )

    assert offenders == [], offenders


# ======================================================================================
# Stage 4.5.21 -- the universe is the import path, not one package
#
# Stage 4.5.20 followed a re-export through any module in ``app`` and recorded what it could
# not do as NB-4: a re-exporter living OUTSIDE that package would be unreadable, the chain
# would dead-end, and the writer would drop out of coverage. Silently, and in the direction
# that matters -- a missing writer is a missing guard.
#
# The universe is now the import path the project declares, which closes the hole by argument
# rather than by mitigation. To re-export the abstraction a module must import it; to import it
# the module must be importable; to be importable it must live under a declared root; and every
# file under those roots is read here. A chain that still escapes has left the project, and a
# third-party package does not re-export a class defined in ``app``.
#
# Today the two universes contain exactly the same 119 modules, because ``backend/`` holds
# nothing but ``app``. That coincidence is what made the old scoping look correct, and it is
# recorded below rather than relied on.
# ======================================================================================


def app_only(sources: dict[str, str]) -> dict[str, str]:
    """*sources* narrowed to the application package -- the Stage 4.5.20 universe.

    Kept so the difference this stage makes can be measured instead of asserted.
    """
    return {
        module: source
        for module, source in sources.items()
        if module == "app" or module.startswith("app.")
    }


def project_file_for(module: str) -> Path | None:
    """The repository file *module* would name if it lived outside every import root."""
    relative = Path(*module.split("."))
    for candidate in (
        REPO_ROOT / relative.with_suffix(".py"),
        REPO_ROOT / relative / "__init__.py",
    ):
        if candidate.exists():
            return candidate
    return None


def writers_under(sources: dict[str, str]) -> set[str]:
    """The derived coverage set, resolved against an arbitrary universe."""
    found: set[str] = set()
    for path in sorted((APP / "services").glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = import_table(tree, f"{SERVICE_PACKAGE}.{path.stem}", sources)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                service = ServiceClass(path.stem, node.name, node, imports)
                if writes_audit_events(service):
                    found.add(str(service))
    return found


#: A re-exporter that is inside the project and outside the application package. The NB-4
#: case, and the one thing about it that matters is the dotted name: it is not ``app.anything``.
SIBLING_REEXPORTER = {"shared.helpers": REEXPORTS_THE_ABSTRACTION}
SIBLING_CONSUMER = writer_with(f"from shared.helpers import {AUDIT_ABSTRACTION}", AUDIT_ABSTRACTION)


# --- the roots are read, not assumed ---------------------------------------------------------


def test_the_import_roots_come_from_the_project_configuration() -> None:
    """Every root the resolver reads is a real directory the project declares.

    The declaration is the point. A hardcoded ``backend`` would keep working right up until
    somebody added a second root, and then keep working while quietly seeing half the project.
    """
    roots = source_roots()

    assert roots != []
    for root in roots:
        assert root.is_dir(), root
        assert root.parent == REPO_ROOT, root


def test_the_universe_holds_every_module_under_every_root() -> None:
    """No file under a declared root is skipped, so no chain through one can dead-end."""
    expected = {path for root in source_roots() for path in root.rglob("*.py") if path.is_file()}

    assert len(PROJECT_SOURCES) == len(expected)


def test_the_application_package_is_all_there_is_under_the_roots_today() -> None:
    """The coincidence that made the previous scoping look correct, written down.

    ``backend/`` contains ``app`` and nothing else, so the old universe and this one hold the
    same modules. That is a fact about the layout, not a property of the resolver, and if a
    sibling package is ever added this test is what will say the two have come apart.
    """
    outside = sorted(module for module in PROJECT_SOURCES if module.split(".")[0] != "app")

    assert outside == []
    assert app_only(PROJECT_SOURCES) == PROJECT_SOURCES


# --- the case NB-4 described --------------------------------------------------------------------


def test_a_re_export_from_outside_the_application_package_is_followed() -> None:
    """``shared.helpers`` is inside the project, outside ``app``, and re-exports the class."""
    service = parse_service(SIBLING_CONSUMER, among=universe_of(SIBLING_REEXPORTER))

    assert writes_audit_events(service)


def test_the_previous_universe_would_have_missed_it() -> None:
    """The measurement that says this stage changed something.

    One fixture, two universes, opposite verdicts -- and the verdict under the narrow one is
    the silent false negative NB-4 named: not an error, just a writer nobody looks at.
    """
    wide = universe_of(SIBLING_REEXPORTER)

    assert writes_audit_events(parse_service(SIBLING_CONSUMER, among=wide))
    assert not writes_audit_events(parse_service(SIBLING_CONSUMER, among=app_only(wide)))


def test_a_writer_reached_from_outside_the_package_still_needs_its_guard() -> None:
    """Coverage, not discovery, is the deliverable -- through this route as through every other.

    Found, and then rejected for having no guard. If the widening had only made the service
    visible without feeding the coverage rule, this is where that would show.
    """
    service = parse_service(
        UNGUARDED_WRITER.replace(
            f"from {AUDIT_MODULE} import {AUDIT_ABSTRACTION}",
            f"from shared.helpers import {AUDIT_ABSTRACTION}",
        ),
        among=universe_of(SIBLING_REEXPORTER),
    )
    translator = translator_of(service)

    assert writes_audit_events(service), "the sibling package hid the writer"
    assert translator is not None
    assert audit_guards(translator) == [], "the fixture is not actually unguarded"


def test_a_deep_chain_out_of_the_package_and_back_in_is_followed() -> None:
    """The two mechanisms compose: leaving ``app`` is not the same as leaving the project."""
    chain = universe_of(
        {
            "shared.first": f"from shared.second import {AUDIT_ABSTRACTION}\n",
            "shared.second": f"from {SERVICE_PACKAGE}.relay import {AUDIT_ABSTRACTION}\n",
            f"{SERVICE_PACKAGE}.relay": REEXPORTS_THE_ABSTRACTION,
        }
    )
    consumer = writer_with(f"from shared.first import {AUDIT_ABSTRACTION}", AUDIT_ABSTRACTION)

    assert writes_audit_events(parse_service(consumer, among=chain))


# --- and the bound that remains ------------------------------------------------------------------


def test_a_chain_into_a_third_party_module_still_terminates() -> None:
    """The universe is the project, not the environment.

    ``sqlalchemy`` is not read and does not need to be: it cannot re-export a class defined in
    ``app`` without importing it, and it does not import from ``app`` at all.
    """
    assert follow_reexports("sqlalchemy.orm.Session", PROJECT_SOURCES) == "sqlalchemy.orm.Session"


def test_a_module_no_root_declares_cannot_be_followed() -> None:
    """Fail closed is still the behaviour at the edge; what changed is where the edge is."""
    assert (
        follow_reexports(f"nowhere.helpers.{AUDIT_ABSTRACTION}", PROJECT_SOURCES)
        == f"nowhere.helpers.{AUDIT_ABSTRACTION}"
    )


def test_no_service_imports_from_a_project_file_outside_the_import_roots() -> None:
    """The last way the resolver could go blind, and the one the widening does not remove.

    A file in the repository but under no declared root is unreadable here -- and unimportable
    in production too, so a service that reached for one would be broken rather than merely
    unaudited. This asserts that nothing does, which is what lets the universe be the roots.
    """
    assert project_file_for("tests.backend.test_error_translation") is not None, (
        "the lookup cannot find a repository file, so the check below proves nothing"
    )
    assert project_file_for("app") is None, "``app`` is under a root, not at the repository root"

    strays = []
    for module, table in sorted(service_modules().items()):
        origins = {target.rpartition(".")[0] for target in table.types.values()}
        origins |= set(table.modules.values())
        strays += [
            f"{module} -> {origin}"
            for origin in sorted(origins)
            if origin and origin not in PROJECT_SOURCES and project_file_for(origin) is not None
        ]

    assert strays == [], strays


# --- the real architecture is untouched by the widening ------------------------------------


def test_the_wider_universe_discovers_exactly_the_same_writers() -> None:
    """Both directions of the claim.

    The seven are still found; and they were already found under the narrow universe, so no
    real writer depends on the machinery this stage added. Re-export support remains
    precautionary -- the same standing the relative-import branch has had since Stage 4.5.19.
    """
    assert writers_under(PROJECT_SOURCES) == set(WRITER_IDS)
    assert writers_under(app_only(PROJECT_SOURCES)) == set(WRITER_IDS)
    assert len(WRITER_IDS) == EXPECTED_WRITER_COUNT
