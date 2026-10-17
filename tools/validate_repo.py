#!/usr/bin/env python3
"""Valida el repositorio de Kodi antes de publicar (o en CI).

Se usa en tres sitios:
  * hook pre-commit  -> impide un commit que rompa el repositorio
  * GitHub Actions   -> impide que quede roto en GitHub
  * a mano           -> python tools/validate_repo.py --net

Comprueba justo lo que provoco el error "no se pudo conectar al repositorio":
addons.xml con una declaracion <?xml ...?> en mitad del fichero, XML invalido,
md5 que no cuadra o zips que no son la version publicada.
"""

import argparse
import hashlib
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from repolib import validate_content  # noqa: E402

ADDON_XML = "addons.xml"
ADDON_XML_MD5 = "addons.xml.md5"


def addons_settings_path(addon_id):
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    path = os.path.join(appdata, "Kodi", "userdata", "addon_data", addon_id, "settings.xml")
    return path if os.path.isfile(path) else None


def load_key(setting_id):
    """Valor de una setting del usuario, para comprobar que NO se publica."""
    path = addons_settings_path("service.guiaparental")
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    match = re.search(r'id="%s"[^>]*?value="([^"]*)"' % re.escape(setting_id), text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return None


def read_zip_texts(zip_path, addon_id):
    """Textos publicados de un addon (addon.xml, .py, .xml, .po, .txt...)."""
    texts = []
    with zipfile.ZipFile(zip_path) as archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            if not name.lower().endswith((".xml", ".py", ".po", ".txt", ".json", ".cfg", ".ini", ".log")):
                continue
            try:
                texts.append((name, archive.read(name).decode("utf-8", errors="replace")))
            except (OSError, zipfile.BadZipFile) as exc:
                texts.append((name, "<no legible: %s>" % exc))
    return texts


def check_zip(zip_path, addon_id, version, problems, warnings):
    if not os.path.isfile(zip_path):
        problems.append(
            "falta el zip %s para <addon id=%r version=%r>" % (zip_path, addon_id, version)
        )
        return None
    try:
        with zipfile.ZipFile(zip_path) as archive:
            bad = archive.testzip()
            if bad:
                problems.append("%s esta corrupto (%s)" % (zip_path, bad))
                return None
            names = archive.namelist()
            entry = "%s/addon.xml" % addon_id
            if entry not in names:
                problems.append("%s no contiene %s" % (zip_path, entry))
                return archive
            inner = archive.read(entry).decode("utf-8-sig", errors="replace")
    except (zipfile.BadZipFile, OSError) as exc:
        problems.append("%s no se puede abrir: %s" % (zip_path, exc))
        return None

    try:
        root = ET.fromstring(inner)
    except ET.ParseError as exc:
        problems.append("%s -> addon.xml interno no es XML valido: %s" % (zip_path, exc))
        return None
    if root.get("id") != addon_id:
        problems.append("%s -> addon.xml interno es de %r" % (zip_path, root.get("id")))
    if root.get("version") != version:
        problems.append(
            "%s -> addon.xml interno es la version %r pero addons.xml publica %r "
            "(Kodi instalaria la version equivocada)"
            % (zip_path, root.get("version"), version)
        )
    dirty = [n for n in names if "__pycache__" in n or n.endswith(".pyc") or n.endswith(".pyo")]
    if dirty:
        warnings.append("%s contiene basura de python: %s" % (zip_path, ", ".join(dirty[:4])))
    if not any(n.startswith("%s/" % addon_id) for n in names):
        problems.append("%s no tiene las entradas bajo %s/ (Kodi lo rechaza)" % (zip_path, addon_id))
    return None


def check_local(repo, problems, warnings):
    xml_path = os.path.join(repo, ADDON_XML)
    md5_path = os.path.join(repo, ADDON_XML_MD5)
    if not os.path.isfile(xml_path):
        problems.append("no existe %s" % xml_path)
        return None, None

    with open(xml_path, "rb") as handle:
        raw = handle.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        warnings.append("addons.xml empieza con BOM de UTF-8 (mejor sin BOM)")
    content = raw.decode("utf-8-sig", errors="replace")
    problems.extend(validate_content(content))

    digest = hashlib.md5(raw).hexdigest()
    if not os.path.isfile(md5_path):
        problems.append("no existe %s" % md5_path)
    else:
        published = open(md5_path, "r", encoding="utf-8", errors="replace").read().strip()
        if published != digest:
            problems.append(
                "addons.xml.md5 no coincide: fichero=%s publicado=%s "
                "(Kodi se queda con la lista vieja)" % (digest, published)
            )

    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None, digest

    forbidden = {}
    for setting in ("tmdb_api_key", "api_key", "apikey", "token", "password"):
        value = load_key(setting)
        if value and len(value) >= 8:
            forbidden[setting] = value
    if forbidden:
        print("  (comprobando %d secreto(s) del usuario contra lo publicado)" % len(forbidden))

    for addon in root.findall("addon"):
        addon_id = addon.get("id")
        version = addon.get("version")
        if not addon_id or not version:
            continue
        zip_path = os.path.join(repo, "zips", addon_id, "%s-%s.zip" % (addon_id, version))
        check_zip(zip_path, addon_id, version, problems, warnings)

        if os.path.isfile(zip_path):
            for name, text in read_zip_texts(zip_path, addon_id):
                for setting, value in forbidden.items():
                    if value in text:
                        problems.append(
                            "SECRETO PUBLICADO: %s de %s aparece en %s:%s"
                            % (setting, addon_id, os.path.basename(zip_path), name)
                        )
        for setting, value in forbidden.items():
            if value in content:
                problems.append("SECRETO PUBLICADO: %s aparece en addons.xml" % setting)
    return content, digest


def fetch(url, timeout=30):
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "repo-validator"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def pin_url(url, pin):
    """Cambia la rama de las URLs de nuestro repo por el commit indicado (inmutable).

    Sin esto, la comprobacion de red de un commit antiguo compara su addons.xml con
    el que ya sirve main (mas nuevo) y falla en falso cuando se encadenan pushes.
    """
    if not pin or not url:
        return url
    prefix = "https://raw.githubusercontent.com/01xKeven/kodi-addons/"
    if not url.startswith(prefix):
        return url
    rest = url[len(prefix):]
    if "/" not in rest:
        return url
    return prefix + pin + "/" + rest.split("/", 1)[1]


def check_net(repo, content, digest, problems, warnings, retries, wait, pin=None):
    """Comprueba lo que Kodi descarga de verdad (y la cache de la CDN)."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return
    if pin:
        print("  (URLs de nuestro repo comprobadas en el commit %s)" % pin[:7])
    targets = []
    for addon in root.findall("addon"):
        for extension in addon.findall("extension"):
            if extension.get("point") != "xbmc.addon.repository":
                continue
            for folder in extension.findall("dir"):
                info = folder.findtext("info")
                checksum = folder.findtext("checksum")
                datadir = folder.findtext("datadir")
                if info:
                    targets.append((addon.get("id"), info, checksum, datadir))

    for addon_id, info, checksum, datadir in targets:
        if "01xKeven/kodi-addons" not in info:
            print("  (omito repo ajeno: %s -> %s)" % (addon_id, info))
            continue
        info = pin_url(info, pin)
        checksum = pin_url(checksum, pin)
        print("  comprobando %s" % info)
        try:
            served = fetch(info)
        except (urllib.error.URLError, OSError) as exc:
            problems.append("no se pudo descargar %s: %s" % (info, exc))
            continue
        served_digest = hashlib.md5(served).hexdigest()
        if served_digest != digest:
            retry = 0
            while retry < retries and served_digest != digest:
                retry += 1
                print("    cache/CDN: servido=%s publicado=%s -> reintento %d/%d en %ds"
                      % (served_digest[:8], digest[:8], retry, retries, wait))
                time.sleep(wait)
                try:
                    served = fetch(info)
                except (urllib.error.URLError, OSError):
                    continue
                served_digest = hashlib.md5(served).hexdigest()
        if served_digest != digest:
            problems.append(
                "lo que sirve la URL no es lo publicado (servido=%s publicado=%s); "
                "suele ser cache de la CDN, Kodi tambien la vera" % (served_digest, digest)
            )
            continue
        try:
            ET.fromstring(served.decode("utf-8-sig", errors="replace"))
        except ET.ParseError as exc:
            problems.append("el addons.xml servido por la URL no parsea: %s" % exc)
        if checksum:
            try:
                online_md5 = fetch(checksum).decode("utf-8", errors="replace").strip()
            except (urllib.error.URLError, OSError) as exc:
                problems.append("no se pudo descargar %s: %s" % (checksum, exc))
                online_md5 = None
            if online_md5 and online_md5 != digest:
                problems.append("%s dice %s y el fichero es %s" % (checksum, online_md5, digest))
        if datadir and "01xKeven/kodi-addons" in datadir:
            base = datadir if datadir.endswith("/") else datadir + "/"
            base = pin_url(base, pin)
            for addon in root.findall("addon"):
                addon_id, version = addon.get("id"), addon.get("version")
                url = "%s%s/%s-%s.zip" % (base, addon_id, addon_id, version)
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as response:
                        if response.status != 200:
                            problems.append("%s -> HTTP %s" % (url, response.status))
                except (urllib.error.URLError, OSError) as exc:
                    problems.append("el zip que Kodi descargaria no esta: %s (%s)" % (url, exc))


def find_repo_root(start):
    folder = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(folder, ADDON_XML)):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            raise SystemExit("no encuentro addons.xml partiendo de %s" % start)
        folder = parent


def main(argv=None):
    parser = argparse.ArgumentParser(description="Valida el repositorio de Kodi (addons.xml, md5 y zips).")
    parser.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--net", action="store_true", help="comprueba tambien lo que sirve la URL de Kodi")
    parser.add_argument("--retries", type=int, default=10, help="reintentos si la CDN sirve version vieja (10 = ~5 min)")
    parser.add_argument("--wait", type=int, default=30, help="segundos entre reintentos")
    parser.add_argument("--pin", default=None,
                        help="comprueba las URLs de nuestro repo en ese commit/rama "
                             "(inmutable; en CI se pasa el SHA del propio commit)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    repo = find_repo_root(args.root)
    if not args.quiet:
        print("Validando repositorio: %s" % repo)

    problems, warnings = [], []
    content, digest = check_local(repo, problems, warnings)
    if args.net and content:
        check_net(repo, content, digest, problems, warnings, args.retries, args.wait, args.pin)

    for warning in warnings:
        print("  aviso: %s" % warning)
    if problems:
        print("")
        print("FALLO (%d problema(s)):" % len(problems))
        for problem in problems:
            print("  - %s" % problem)
        print("")
        print("El repositorio NO es valido para Kodi. No publiques/commitees asi.")
        return 1

    print("OK: addons.xml valido (%d addons), md5 %s y zips coherentes."
          % (len(ET.fromstring(content).findall("addon")), digest))
    return 0


if __name__ == "__main__":
    sys.exit(main())
