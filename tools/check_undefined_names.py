"""Report names that are used but never defined — ruff's F821, without ruff.

The pipeline runs ``ruff check --select E9,F63,F7,F82``. ruff cannot be installed
here (the internal index does not carry it), and the gap showed: a function left
behind by a refactor still referred to a constant that had been deleted with the
module header, and the first thing to notice was CI.

symtable answers exactly the question that matters. It resolves each name to a
scope, so a name that some scope reads as a global while nothing in the module
ever binds it, and which is not a builtin, is a name that will raise NameError
the moment that line runs.

Deliberately narrow: this is not a reimplementation of pyflakes. It catches
leftovers from moving code between modules, which is the mistake this codebase
has actually made.

    python tools/check_undefined_names.py app tools tests
"""
from __future__ import annotations

import builtins
import symtable
import sys
from pathlib import Path

BUILTINS = set(dir(builtins)) | {
    "__file__",
    "__name__",
    "__doc__",
    "__package__",
    "__spec__",
    "__loader__",
    "__builtins__",
    "__debug__",
    "__path__",
}


def _bound_names(table: symtable.SymbolTable) -> set[str]:
    """Every name this scope or any scope inside it binds."""
    names = {
        symbol.get_name()
        for symbol in table.get_symbols()
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_parameter()
    }
    # A nested def or class binds its own name in the enclosing scope, which
    # get_symbols() reports as assigned — but a comprehension or lambda scope
    # holds bindings this scope cannot see, so recurse for completeness rather
    # than for visibility.
    for child in table.get_children():
        names.add(child.get_name())
    return names


def _walk(table: symtable.SymbolTable, module_names: set[str]) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    for symbol in table.get_symbols():
        name = symbol.get_name()
        if name in module_names or name in BUILTINS:
            continue
        if not symbol.is_referenced():
            continue
        # Bound somewhere in this scope, so it resolves locally.
        if symbol.is_assigned() or symbol.is_imported() or symbol.is_parameter():
            continue
        # Free variables resolve in an enclosing function scope, which is not a
        # module-level binding and is not this check's business.
        if symbol.is_free():
            continue
        if symbol.is_global() or table.get_type() == "module":
            problems.append((table.get_lineno(), name))
    for child in table.get_children():
        problems.extend(_walk(child, module_names))
    return problems


def check(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    try:
        top = symtable.symtable(source, str(path), "exec")
    except SyntaxError as exc:  # E9 territory; report it the same way
        return [f"{path}:{exc.lineno}: syntax error: {exc.msg}"]

    module_names = _bound_names(top)
    # A star-import can supply anything, so nothing after one can be judged.
    if "*" in source and "import *" in source:
        return []

    seen: set[tuple[int, str]] = set()
    out = []
    for lineno, name in _walk(top, module_names):
        if (lineno, name) in seen:
            continue
        seen.add((lineno, name))
        out.append(f"{path}: undefined name `{name}` (near line {lineno})")
    return out


def main(argv: list[str]) -> int:
    roots = [Path(a) for a in argv[1:]] or [Path("app")]
    findings: list[str] = []
    files = 0
    for root in roots:
        for file in sorted(root.rglob("*.py")):
            if "__pycache__" in file.parts or ".venv" in file.parts:
                continue
            files += 1
            findings.extend(check(file))
    for line in findings:
        print(line)
    print(f"checked {files} files, {len(findings)} problem(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
