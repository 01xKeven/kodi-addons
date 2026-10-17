#!/usr/bin/env python3
"""Instala el hook pre-commit en este clon (los hooks no se versionan en .git).

Uso:
  python tools/install_hooks.py

Deja el hook apuntando al python actual y comprueba que funciona.
"""

import os
import shutil
import stat
import subprocess
import sys


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = os.path.join(repo, "tools", "hooks", "pre-commit")
    hooks_dir = os.path.join(repo, ".git", "hooks")
    if not os.path.isdir(hooks_dir):
        raise SystemExit("%s no es un clon de git" % repo)

    with open(source, "r", encoding="utf-8") as handle:
        content = handle.read()
    if content.startswith("#!"):
        first, rest = content.split("\n", 1)
        content = "%s\nREPO_PYTHON=\"${REPO_PYTHON:-%s}\"\n%s" % (
            first,
            sys.executable.replace("\\", "/"),
            rest,
        )

    target = os.path.join(hooks_dir, "pre-commit")
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)
    os.chmod(target, os.stat(target).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print("hook instalado: %s" % target)
    print("python del hook: %s" % sys.executable)

    result = subprocess.run(
        ["git", "-C", repo, "config", "core.hooksPath"],
        capture_output=True,
        text=True,
    )
    custom = result.stdout.strip()
    if custom and custom != ".git/hooks":
        print("OJO: core.hooksPath=%r; los hooks se leen de ahi, no de .git/hooks" % custom)

    check = subprocess.run(
        [sys.executable, os.path.join(repo, "tools", "validate_repo.py"), "--root", repo, "--quiet"],
        capture_output=True,
        text=True,
    )
    if check.returncode == 0:
        print("el validador pasa con el estado actual del repo: cualquier commit de addons.xml se revisara")
    else:
        print("el validador FALLA ahora mismo con el repo actual:")
        print(check.stdout.strip() or check.stderr.strip())


if __name__ == "__main__":
    main()
