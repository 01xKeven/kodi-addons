#!/usr/bin/env python3
"""Publica un addon en el repositorio 01xKeven (Kodi) de forma segura.

Es la UNICA forma correcta de publicar: copia el addon, construye el zip,
actualiza addons.xml (sin BOM ni declaraciones sueltas), regenera el md5,
valida todo y solo entonces hace commit/push.

Ejemplos:
  python tools/publish_addon.py --id service.guiaparental
  python tools/publish_addon.py --id service.guiaparental --message "arregla el HUD de 1 s"
  python tools/publish_addon.py --id service.guiaparental --no-push     # deja el commit local
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import repolib  # noqa: E402
import validate_repo  # noqa: E402

SKIP_DIRS = {
    "__pycache__", ".git", ".github", ".vscode", ".idea",
    ".pytest_cache", ".mypy_cache", ".tox", "venv", ".venv", "env"
}

SKIP_EXTENSIONS = (
    # Python bytecode y caches
    ".pyc", ".pyo", ".pyd",
    # Backups y temporales de editores
    ".bak", ".backup", ".old", ".orig", ".bkp", ".copy",
    ".tmp", ".temp", ".swp", ".swo",
    # Logs, dumps y debug
    ".log", ".pdb", ".dmp",
    # Archivos comprimidos (evitar meter zips dentro de zips)
    ".zip", ".tar", ".gz", ".rar", ".7z"
)

SKIP_EXACT_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini",
    ".gitignore", ".gitattributes", ".gitmodules"
}


def should_skip_file(name):
    low = name.lower()
    if low in SKIP_EXACT_NAMES:
        return True
    if low.startswith((".", "#")) or low.endswith("~"):
        return True
    if low.endswith(SKIP_EXTENSIONS):
        return True
    return False


def clean_folders(folders):
    """Filtra directorios no deseados in-place para os.walk."""
    folders[:] = [f for f in folders if f not in SKIP_DIRS and not f.startswith(".")]


def git(repo, *args, check=True):
    result = subprocess.run(["git"] + list(args), cwd=repo, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit("git %s fallo:\n%s%s" % (" ".join(args), result.stdout, result.stderr))
    return result.stdout.strip()


def default_source(addon_id):
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "Kodi", "addons", addon_id)


def source_files(source):
    """Ficheros que se publicarian, por ruta relativa (sin basura de python ni backups)."""
    found = {}
    for root, folders, names in os.walk(source):
        clean_folders(folders)
        for name in names:
            if should_skip_file(name):
                continue
            path = os.path.join(root, name)
            found[os.path.relpath(path, source).replace("\\", "/")] = path
    return found


def ensure_identity(repo):
    """Sin identidad de git el commit falla; la pongo local a este clon."""
    name = git(repo, "config", "user.name", check=False)
    email = git(repo, "config", "user.email", check=False)
    if name and email and "@" in email and "(none)" not in email:
        return
    owner = "01xKeven"
    url = git(repo, "remote", "get-url", "origin", check=False)
    match = re.search(r"github\.com[:/]([^/]+)/", url or "")
    if match:
        owner = match.group(1)
    name, email = owner, "%s@users.noreply.github.com" % owner
    print("este clon no tiene identidad de git valida: la configuro como %s <%s> (solo en este clon)" % (name, email))
    git(repo, "config", "user.name", name)
    git(repo, "config", "user.email", email)


def check_version_bump(target, addon_id, version, source):
    """Contenido distinto con la MISMA version = Kodi no actualiza. Se corta aqui."""
    zip_path = os.path.join(target, "%s-%s.zip" % (addon_id, version))
    if not os.path.isfile(zip_path):
        return
    files = source_files(source)
    changed = []
    with zipfile.ZipFile(zip_path) as archive:
        published = set(archive.namelist())
        for rel, path in sorted(files.items()):
            entry = "%s/%s" % (addon_id, rel)
            if entry not in published:
                changed.append("%s (nuevo)" % rel)
            elif archive.read(entry) != open(path, "rb").read():
                changed.append(rel)
        for entry in sorted(published):
            if entry.endswith("/"):
                continue
            rel = entry[len(addon_id) + 1:] if entry.startswith(addon_id + "/") else entry
            if rel not in files:
                changed.append("%s (borrado)" % rel)
    if changed:
        raise SystemExit(
            "el contenido del addon ha cambiado pero la version sigue siendo %s:\n  - %s\n"
            "Sube la version en addon.xml antes de publicar (si no, Kodi cree que ya tiene "
            "la ultima y no actualiza)." % (version, "\n  - ".join(changed[:10]))
        )
    print("contenido identico al ya publicado para la version %s (no hace falta subir version)" % version)


def mirror_addon(source, target, addon_id):
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target)
    for root, folders, names in os.walk(source):
        clean_folders(folders)
        for name in names:
            if should_skip_file(name):
                continue
            src = os.path.join(root, name)
            rel = os.path.relpath(src, source)
            dst = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
    print("copiado %s -> %s" % (source, os.path.relpath(target, os.path.dirname(target))))


def build_zip(target, addon_id, version):
    files = []
    for root, folders, names in os.walk(target):
        clean_folders(folders)
        for name in names:
            if should_skip_file(name):
                continue
            files.append(os.path.join(root, name))
    files.sort()
    zip_path = os.path.join(target, "%s-%s.zip" % (addon_id, version))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            arcname = "%s/%s" % (addon_id, os.path.relpath(path, target).replace("\\", "/"))
            archive.write(path, arcname)
    print("creado %s (%d entradas)" % (os.path.basename(zip_path), len(files)))
    return zip_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Publica un addon en el repositorio (con validacion previa).")
    parser.add_argument("--id", required=True, help="id del addon, ej. service.guiaparental")
    parser.add_argument("--source", help="carpeta del addon (por defecto la de Kodi instalada)")
    parser.add_argument("--root", help="raiz del repo (por defecto la del script)")
    parser.add_argument("--version", help="version esperada; si no coincide con addon.xml, aborta")
    parser.add_argument("--message", help="detalle extra para el mensaje de commit")
    parser.add_argument("--no-push", action="store_true", help="solo commit local")
    parser.add_argument("--no-net", action="store_true", help="no comprobar la URL de Kodi tras el push")
    parser.add_argument("--retries", type=int, default=10, help="reintentos de la comprobacion de la CDN (~5 min)")
    args = parser.parse_args(argv)

    repo = validate_repo.find_repo_root(args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ensure_identity(repo)
    source = args.source or default_source(args.id)
    if not os.path.isfile(os.path.join(source, "addon.xml")):
        raise SystemExit("no encuentro %s/addon.xml (usa --source)" % source)

    with open(os.path.join(source, "addon.xml"), "r", encoding="utf-8-sig", errors="replace") as handle:
        addon_xml_text = handle.read()
    root = ET.fromstring(repolib.strip_declaration(addon_xml_text))
    version = root.get("version")
    if root.get("id") != args.id:
        raise SystemExit("addon.xml de %s dice id=%r" % (source, root.get("id")))
    if args.version and args.version != version:
        raise SystemExit(
            "addon.xml dice version=%s pero pediste %s: sube la version en addon.xml antes de publicar"
            % (version, args.version)
        )
    print("addon %s version %s" % (args.id, version))

    target = os.path.join(repo, "zips", args.id)
    if not os.path.isdir(target):
        raise SystemExit("el addon %s no vive en este repo (%s no existe)" % (args.id, target))

    check_version_bump(target, args.id, version, source)
    mirror_addon(source, target, args.id)
    build_zip(target, args.id, version)

    xml_path = os.path.join(repo, "addons.xml")
    with open(xml_path, "r", encoding="utf-8", newline="") as handle:
        content = handle.read()
    content = repolib.set_block(content, args.id, repolib.addon_block(addon_xml_text))
    digest = repolib.write(content, repo)
    print("addons.xml + md5 regenerados | md5 %s" % digest)

    problems, warnings = [], []
    validate_repo.check_local(repo, problems, warnings)
    for warning in warnings:
        print("aviso: %s" % warning)
    if problems:
        print("FALLO: lo publicado no es valido para Kodi:")
        for problem in problems:
            print("  - %s" % problem)
        raise SystemExit("commit abortado (arregla lo anterior y vuelve a ejecutar)")

    prefix = "Update %s to %s" % (args.id, version)
    message = "%s: %s" % (prefix, args.message) if args.message else prefix
    relative = [os.path.relpath(target, repo), "addons.xml", "addons.xml.md5"]
    git(repo, "add", "--", *relative)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "--"] + relative, cwd=repo)
    if staged.returncode == 0:
        print("nada que publicar: addons.xml, el md5 y el zip ya estan exactamente igual (sin commit)")
        return 0
    git(repo, "commit", "-m", message)
    print("commit: %s" % git(repo, "log", "-1", "--oneline"))

    if args.no_push:
        print("sin push (--no-push). Recuerda: git push origin main")
        return 0

    git(repo, "push", "origin", git(repo, "branch", "--show-current"))
    print("push hecho")

    if args.no_net:
        return 0
    print("comprobando lo que Kodi descargara (con reintentos por la cache de la CDN)...")
    return validate_repo.main(["--root", repo, "--net", "--retries", str(args.retries)])


if __name__ == "__main__":
    sys.exit(main())
