#!/usr/bin/env bash
# Verifies the relative markdown links in the hand-written docs: every linked
# file exists, every `#Lnn` / `#Lnn-Lmm` anchor is within the file's line
# count, and a link whose text is a single backticked name — `foo`,
# `module.foo`, `Type::foo`, or a call such as `Type.foo(arg)` — has that
# name's last segment on the anchored line. Plain-file labels (`open.rs`,
# `store/mod.rs`) and `file.rs:A-B` range labels are only checked for
# existence and range.
#
# Prints one `doc:line: message` per failure and exits 1 if there is any.
# Run directly (`scripts/check-doc-anchors.sh`) or through scripts/ci-check.sh.
#
# `--fix` re-points the broken links of named labels instead of reporting
# them, and reports only the ones it cannot place. A link goes where the line
# it anchored at HEAD is now — that line's text, found once, with the name on
# it — or, failing that, to the one definition of the name (`def`, `class` or
# an assignment). Either is looked for in the linked file first, then in the
# other files of its directory tree, so a link follows its name into another
# module (and out of a file that no longer exists). Run it after moving code,
# before committing: HEAD holds the lines the links were written against.
#
# Needs only bash, git and python3.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

fix=0
if [ "${1:-}" = "--fix" ]; then
  fix=1
fi

docs=(README.md docs/*.md)

existing=()
for doc in "${docs[@]}"; do
  [ -f "$doc" ] && existing+=("$doc")
done

FIX="$fix" python3 - "${existing[@]}" <<'PY'
import os
import re
import subprocess
import sys

LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ANCHOR = re.compile(r"^L(\d+)(?:-L(\d+))?$")
IDENT = re.compile(
    r"^`([A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)*)(?:\([^`]*\))?`$"
)
FILE_LABEL = re.compile(r"\.(?:rs|py|md|toml|ts|js|json|sh|ya?ml|html)$")
FIX = os.environ.get("FIX") == "1"

failures = 0
fixed = 0
file_lines = {}
head_lines = {}


def lines_of(path):
    if path not in file_lines:
        with open(path, encoding="utf-8", errors="replace") as f:
            file_lines[path] = f.read().splitlines()
    return file_lines[path]


def head_line(path, n):
    """Line ``n`` of ``path`` as HEAD has it, or ``None``."""
    if path not in head_lines:
        done = subprocess.run(
            ["git", "show", f"HEAD:{path}"], capture_output=True, text=True, check=False
        )
        head_lines[path] = done.stdout.splitlines() if done.returncode == 0 else []
    lines = head_lines[path]
    return lines[n - 1] if 0 < n <= len(lines) else None


def search_order(path):
    """``path`` if it exists — or, for a module that became a package of the
    same name (``x.py`` -> ``x/``), that package's files — then every other
    file of the nearest existing directory's tree, sorted, with the same
    extension."""
    root = os.path.dirname(path)
    while root and not os.path.isdir(root):
        root = os.path.dirname(root)
    stem, ext = os.path.splitext(path)

    def files(top_dir):
        return sorted(
            os.path.normpath(os.path.join(top, name))
            for top, dirs, names in os.walk(top_dir)
            if "__pycache__" not in top
            for name in names
            if name.endswith(ext)
        )

    if os.path.isfile(path):
        first = [path]
    elif os.path.isdir(stem):
        first = files(stem)
    else:
        first = []
    return first + [p for p in files(root or ".") if p not in first]


def place(path, n, name):
    """Where the link to line ``n`` of ``path``, labelled ``name``, belongs
    now: ``(file, line)``, or ``None``."""
    old = head_line(path, n)
    definition = re.compile(
        rf"^\s*(?:(?:async\s+)?def|class)\s+{re.escape(name)}\b"
        rf"|^\s*{re.escape(name)}\s*(?::[^=\n]+)?=(?!=)"
    )
    for wanted in (
        None if old is None or name not in old else (lambda text: text == old),
        lambda text: definition.match(text) is not None,
    ):
        if wanted is None:
            continue
        for candidate in search_order(path):
            hits = [i for i, text in enumerate(lines_of(candidate), 1) if wanted(text)]
            if len(hits) == 1:
                return candidate, hits[0]
            if hits:
                break  # several in the nearest file: ambiguous, try the next rule
    return None


def fail(doc, lineno, msg):
    global failures
    failures += 1
    print(f"{doc}:{lineno}: {msg}")


def check(doc, lineno, label, target):
    """``target`` as it should be: itself when the link holds, a re-pointed
    one under ``--fix``, ``None`` after reporting a broken link."""
    base = os.path.dirname(doc)
    if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
        return target  # external URL or same-document heading
    path, _, frag = target.partition("#")
    resolved = os.path.normpath(os.path.join(base, path))
    a = ANCHOR.match(frag)
    ident = IDENT.match(label.strip())
    name = None
    if a and ident and not FILE_LABEL.search(ident.group(1)):
        name = re.split(r"::|\.", ident.group(1))[-1]
    problem = None
    if not os.path.isfile(resolved):
        problem = f"{resolved} does not exist"
    elif a:
        start = int(a.group(1))
        end = int(a.group(2)) if a.group(2) else start
        lines = lines_of(resolved)
        if start < 1 or end < start or end > len(lines):
            problem = f"#{frag} outside 1-{len(lines)}"
        elif name is not None and not any(name in lines[n - 1] for n in range(start, end + 1)):
            problem = f"`{name}` not on {path} line {frag}"
    if problem is None:
        return target
    if FIX and name is not None:
        start = int(a.group(1))
        end = int(a.group(2)) if a.group(2) else start
        spot = place(resolved, start, name)
        if spot is not None:
            new_path, line = spot
            relative = os.path.relpath(new_path, base or ".")
            anchor = f"L{line}" if end == start else f"L{line}-L{line + end - start}"
            return f"{relative}#{anchor}"
    fail(doc, lineno, f"[{label}]({target}): {problem}")
    return None


for doc in sys.argv[1:]:
    with open(doc, encoding="utf-8") as f:
        text = f.read()
    out = []
    in_fence = False
    for lineno, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:

            def repoint(m, lineno=lineno):
                global fixed
                label, target = m.group(1), m.group(2)
                new = check(doc, lineno, label, target)
                if new is None or new == target:
                    return m.group(0)
                fixed += 1
                start, end = m.span(2)
                whole = m.group(0)
                return whole[: start - m.start()] + new + whole[end - m.start() :]

            line = LINK.sub(repoint, line)
        out.append(line)
    if FIX and "\n".join(out) != text:
        with open(doc, "w", encoding="utf-8") as f:
            f.write("\n".join(out))

if fixed:
    print(f"re-pointed {fixed} doc link(s)")
if failures:
    print(f"{failures} broken doc link(s)")
    sys.exit(1)
PY
