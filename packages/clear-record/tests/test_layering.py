"""Import-boundary guard: the ``clear_record`` layer DAG and the vendor-free core.

The single ``clear-record`` distribution ships nine internal layers as
subpackages:
``clear_record.{core,engine,providers,pipeline,cli,service,web,tray,mcp}``. One
distribution means there is no per-dist dependency graph left to enforce the
layering or the vendor-free core, so this test enforces both with two independent
passes (no third-party imports):

- **Static** — parse every layer source with the stdlib :mod:`ast` module and
  resolve every import to an absolute target, *including relative imports*:
  ``from ..engine import X`` inside ``clear_record.core`` resolves to
  ``clear_record.engine`` and is judged by the same edge rules. Constant
  ``import_module`` / ``__import__`` calls are folded in (aliases included).
- **Runtime isolation** — in a fresh subprocess, pre-poison ``sys.modules``
  with ``None`` for the modules a layer must not reach, then import the layer
  *and every submodule* (:func:`pkgutil.walk_packages`). A real import that
  evades the static pass makes the subprocess fail.

Rules:

- ``core`` imports **no** third-party package (notably not ``numpy``,
  ``soundfile``, ``torch``, ``tensorflow``, ``onnxruntime`` or any ``whisper*``)
  and **no** sibling layer: it is the vendor-free, dependency-free core.
- ``engine`` may import ``core`` (and third-party audio libraries).
- ``providers`` may import ``core``.
- ``pipeline`` may import ``core``, ``engine`` and ``providers``; it is the
  stage wiring and the machinery that runs it, and must not reach the command
  surface.
- ``cli`` may import any layer below it (``core``, ``engine``, ``providers``,
  ``pipeline``).
- ``service`` may import ``core`` and ``pipeline``; it must not reach the
  command surface, ``web``, ``tray`` or ``mcp``.
- ``web`` may import ``core`` and ``service``; it is the browser surface.
- ``tray`` may import ``core``, ``service`` and ``web``; it supervises the console
  and is the native desktop entry point.
- ``mcp`` may import ``core`` and ``service``; it is the agent boundary, a thin
  adapter over the service (ADR-0017), and must not reach the browser or native
  surfaces.
- ``core``/``engine``/``providers`` never import ``pipeline``, ``cli``,
  ``service``, ``web``, ``tray`` or ``mcp``; the CLI reaches the optional
  surfaces only through entry points, never an import (ADR-0013).

If a real edge does not fit this DAG, that is a deliberate design change: update
the layer DAG here and in the ADRs (ADR-0004 / ADR-0012) — do not silently widen
the rule.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src" / "clear_record"

# Internal layers, leaf-first: each layer may only import layers to its left
# (plus its own submodules).
LAYERS = (
    "core",
    "engine",
    "providers",
    "pipeline",
    "cli",
    "service",
    "web",
    "tray",
    "mcp",
)

# The exact internal edges derived from the code. ``layer -> layers it imports``.
ALLOWED_INTERNAL: dict[str, frozenset[str]] = {
    "core": frozenset(),
    "engine": frozenset({"core"}),
    "providers": frozenset({"core"}),
    "pipeline": frozenset({"core", "engine", "providers"}),
    "cli": frozenset({"core", "engine", "providers", "pipeline"}),
    "service": frozenset({"core", "pipeline"}),
    "web": frozenset({"core", "service"}),
    "tray": frozenset({"core", "service", "web"}),
    "mcp": frozenset({"core", "service"}),
}

_ROOT = "clear_record"

# The third-party stacks ``core`` must never reach. ``whisper*`` is matched by
# prefix in the runtime pass because an ASR package root may be named anything.
BLOCKED_THIRD_PARTY = ("numpy", "soundfile", "torch", "tensorflow", "onnxruntime")

# (layer under test, internal layers it must not reach, block third-party?) —
# each runs in its own subprocess so a failure names the offending layer.
#
# A case may only poison layers the layer does **not** need transitively: the
# upper layers legitimately pull the lower ones in (``web → service → pipeline
# → engine → core``, and ``cli → pipeline → core``), so poisoning ``engine``
# while importing ``web`` would break a legal chain. The static pass above is
# the real edge check; this pass catches a *dynamic* import of a layer the layer
# must never reach.
ISOLATION_CASES = (
    (
        "core",
        ("engine", "providers", "pipeline", "cli", "service", "web", "tray", "mcp"),
        True,
    ),
    (
        "engine",
        ("providers", "pipeline", "cli", "service", "web", "tray", "mcp"),
        False,
    ),
    ("providers", ("pipeline", "cli", "service", "web", "tray", "mcp"), False),
    ("pipeline", ("cli", "service", "web", "tray", "mcp"), False),
    ("cli", ("service", "web", "tray", "mcp"), False),
    ("service", ("cli", "web", "tray", "mcp"), False),
    ("web", ("tray", "mcp"), False),
    ("tray", ("mcp",), False),
    ("mcp", ("web", "tray"), False),
)


def _python_files(layer: str) -> list[Path]:
    return sorted((PACKAGE_SRC / layer).rglob("*.py"))


def _module_name(path: Path) -> str:
    """The dotted module name of a source file within the ``clear_record`` package.

    ``clear_record/core/model.py`` -> ``clear_record.core.model``;
    ``clear_record/core/__init__.py`` -> ``clear_record.core``.
    """
    parts = list(path.relative_to(PACKAGE_SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([_ROOT, *parts])


def _package_of(module_name: str, path: Path) -> str:
    """The ``__package__`` a module's relative imports resolve against."""
    if path.name == "__init__.py":
        return module_name
    return module_name.rsplit(".", 1)[0]


def _resolve_relative(
    current_package: str, level: int, module: str | None
) -> str | None:
    """Resolve ``from <dots>module import ...`` to an absolute dotted target.

    ``level`` is the count of leading dots (``.`` = 1). Returns ``None`` when the
    dots climb above the ``clear_record`` root.
    """
    parts = current_package.split(".")
    if level > len(parts):
        return None
    base = parts[: len(parts) - level + 1]
    if module:
        base.extend(module.split("."))
    return ".".join(base)


def _dynamic_imports(tree: ast.AST) -> list[tuple[str, int]]:
    """Catch constant-name ``__import__`` / ``import_module`` calls.

    Tracks aliases (``from importlib import import_module as im``) and the
    attribute form (``importlib.import_module``). A name built at runtime (e.g.
    concatenation) is undecidable statically; the runtime isolation pass below
    is what closes that hole.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    aliases.add(alias.asname or alias.name)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name not in {"__import__", "import_module"} and name not in aliases:
            continue
        if not node.args:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found.append((arg.value, node.lineno))
    return found


def _imports(path: Path) -> list[tuple[str, int]]:
    """Every module ``path`` imports, resolved to an absolute target.

    Relative imports are resolved against the file's package (so
    ``from ..engine import X`` in ``clear_record.core`` yields
    ``clear_record.engine``); ``from pkg import name`` also yields the candidate
    submodule ``pkg.name`` so ``from clear_record import engine`` is seen as an
    internal edge. Constant dynamic imports are included.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_of(_module_name(path), path)
    results: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            results.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module
            else:
                base = _resolve_relative(package, node.level, node.module)
            if base is None:
                continue
            results.append((base, node.lineno))
            for alias in node.names:
                if alias.name != "*":
                    results.append((f"{base}.{alias.name}", node.lineno))
    results.extend(_dynamic_imports(tree))
    return results


def _classify(module: str) -> tuple[str, str | None]:
    """Return ``(kind, layer)`` for a module path.

    ``kind`` is ``"stdlib"``, ``"internal"`` or ``"third_party"``; ``layer`` is
    the imported layer for an internal import (``None`` for a bare
    ``clear_record`` import).
    """
    root = module.split(".")[0]
    if root == _ROOT:
        parts = module.split(".")
        return "internal", parts[1] if len(parts) > 1 else None
    if root.startswith("whisper"):
        return "third_party", None
    if root == "__future__" or root in sys.stdlib_module_names:
        return "stdlib", None
    return "third_party", None


def test_every_layer_has_source_files() -> None:
    """Guard against a silent no-op scan (a renamed/missing layer)."""
    missing = [layer for layer in LAYERS if not _python_files(layer)]
    assert not missing, f"no source files found for layer(s): {missing}"


@pytest.mark.parametrize("layer", LAYERS)
def test_internal_import_edges(layer: str) -> None:
    """A layer imports only the internal layers its DAG edge allows.

    ``core`` has no internal edges; ``engine``/``providers`` may import
    ``core``; ``pipeline`` may import ``core``/``engine``/``providers``; ``cli``
    may import those plus ``pipeline``; ``service`` may import ``core`` and
    ``pipeline``. Importing a sibling outside that set — including the CLI,
    which no layer outside it may import — fails, whether the import is
    absolute or relative.
    """
    allowed = ALLOWED_INTERNAL[layer]
    violations: list[str] = []
    for path in _python_files(layer):
        for module, lineno in _imports(path):
            kind, imported_layer = _classify(module)
            if (
                kind != "internal"
                or imported_layer is None
                or imported_layer == layer
                or imported_layer in allowed
            ):
                continue
            rel = path.relative_to(PACKAGE_SRC)
            violations.append(f"{rel}:{lineno}: imports {module!r}")
    assert not violations, (
        f"layer {layer!r} may import only {sorted(allowed) or 'no internal layer'}, "
        "but:\n" + "\n".join(violations)
    )


def test_core_imports_no_third_party() -> None:
    """``core`` is dependency-free: no third-party import may appear.

    This is the guarantee the old ``cr-core`` packaging supplied (an empty
    dependency list). A single dist has no such packaging fact, so this test is
    the boundary — it must genuinely fail on e.g. ``import numpy`` in ``core``.
    """
    violations: list[str] = []
    for path in _python_files("core"):
        for module, lineno in _imports(path):
            kind, _ = _classify(module)
            if kind == "third_party":
                rel = path.relative_to(PACKAGE_SRC)
                violations.append(f"{rel}:{lineno}: imports third-party {module!r}")
    assert not violations, (
        "clear_record.core must not import third-party code (vendor-free core):\n"
        + "\n".join(violations)
    )


def test_no_layer_imports_the_cli() -> None:
    """No layer outside the command surface imports it; four are checked here.

    Keeping ``core``/``engine``/``providers`` free of ``clear_record.cli`` stops
    the vendor-free/domain layers depending on the command surface (they sit
    below it), and ``service`` joins them: it used to drive the stage wiring
    while that wiring lived in the CLI package, and may no longer, because the
    pipeline moved out into ``clear_record.pipeline`` (ADR-0012's clause (c)
    restated, ADR-0030's ``C3``). ``ALLOWED_INTERNAL``
    now carries no edge into ``cli`` at all, so the layers above ``service``
    (``web``/``tray``/``mcp``, ADR-0013 added ``web``, ADR-0016 ``tray``,
    ADR-0017 ``mcp``) are covered by :func:`test_internal_import_edges`; the
    four named here are the ones this test checks directly.
    """
    violations: list[str] = []
    for layer in ("core", "engine", "providers", "service"):
        for path in _python_files(layer):
            for module, lineno in _imports(path):
                kind, imported_layer = _classify(module)
                if kind == "internal" and imported_layer == "cli":
                    rel = path.relative_to(PACKAGE_SRC)
                    violations.append(f"{rel}:{lineno}: imports {module!r}")
    assert not violations, (
        "no layer outside clear_record.cli may import it, but:\n"
        + "\n".join(violations)
    )


def _isolation_script(
    layer: str, blocked_internal: tuple[str, ...], block_third_party: bool
) -> str:
    """Build the subprocess body that imports *layer* with its edges poisoned."""
    blocked = [*(_ROOT + "." + name for name in blocked_internal)]
    if block_third_party:
        blocked.extend(BLOCKED_THIRD_PARTY)
    return (
        "import importlib\n"
        "import pkgutil\n"
        "import sys\n"
        f"for _name in {blocked!r}:\n"
        "    sys.modules[_name] = None\n"
        "class _WhisperBlocker:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname.split('.')[0].startswith('whisper'):\n"
        "            raise ImportError(f'blocked third-party module {fullname!r}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _WhisperBlocker())\n"
        f"import clear_record.{layer} as _pkg\n"
        "for _info in pkgutil.walk_packages(_pkg.__path__, _pkg.__name__ + '.'):\n"
        "    importlib.import_module(_info.name)\n"
    )


@pytest.mark.parametrize(
    "layer, blocked_internal, block_third_party",
    ISOLATION_CASES,
    ids=[case[0] for case in ISOLATION_CASES],
)
def test_layer_imports_in_isolation(
    layer: str, blocked_internal: tuple[str, ...], block_third_party: bool
) -> None:
    """Import ``layer`` and all its submodules with forbidden edges poisoned.

    The strongest evidence for the boundary: a real ``from ..engine import ...``
    (or a dynamic import the static pass cannot see) makes this subprocess fail.
    Run in a fresh interpreter so the poison is real and a failure names one
    layer.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _isolation_script(layer, blocked_internal, block_third_party),
        ],
        capture_output=True,
        text=True,
    )
    poisoned = [(_ROOT + "." + name) for name in blocked_internal]
    if block_third_party:
        poisoned.extend([*BLOCKED_THIRD_PARTY, "whisper*"])
    assert result.returncode == 0, (
        f"clear_record.{layer} must import with {poisoned} poisoned, but failed:\n"
        f"{result.stderr}"
    )
