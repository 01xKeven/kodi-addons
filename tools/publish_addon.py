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

SKIP_FILES = ("__pycache__", ".pyc", ".pyo", ".log", ".zip", ".pdb")


def git(repo, *args, check=True):
    result = subprocess.run(["git"] + list(args), cwd=repo, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit("git %s fallo:\n%s%s" % (" ".join(args), result.stdout, result.stderr))
    return result.stdout.strip()


def default_source(addon_id):
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "Kodi", "addons", addon_id)


def mirror_addon(source, target, addon_id):
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(target)
    for root, folders, names in os.walk(source):
        folders[:] = [f for f in folders if f != "__pycache__"]
        for name in names:
            if name.lower().endswith(SKIP_FILES) or name.endswith(".pyc"):
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
        folders[:] = [f for f in folders if f != "__pycache__"]
        for name in names:
            if name.lower().endswith(".zip") or name.lower().endswith(SKIP_FILES[1:]):
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
