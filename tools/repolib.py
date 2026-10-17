"""Helpers para tocar addons.xml sin romper el repositorio de Kodi.

Regla de oro (esto es lo que rompio el repo en 1.1.4/1.1.5):
  dentro de addons.xml cada bloque <addon>...</addon> va PELADO: sin BOM de
  UTF-8 y sin declaracion <?xml ...?>. Solo puede haber UNA declaracion, y
  solo si esta en la primera linea del fichero.

Aqui todo eso se hace solo y ademas se valida el XML antes de escribir.
"""

import hashlib
import os
import re
import xml.etree.ElementTree as ET

DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*\n?")
XML_DECL_RE = re.compile(r"<\?xml")


def strip_declaration(text):
    """Quita BOM(s) y TODAS las declaraciones <?xml ...?> iniciales."""
    text = text.replace("\ufeff", "")
    while True:
        match = DECL_RE.match(text)
        if not match:
            break
        text = text[match.end():]
    return text


def addon_block(addon_xml_text):
    """Devuelve el bloque <addon>...</addon> listo para addons.xml."""
    return strip_declaration(addon_xml_text).strip("\r\n") + "\n"


def set_block(content, addon_id, block):
    """Sustituye el bloque del addon en addons.xml (o lo anade al final)."""
    content = content.replace("\ufeff", "")
    marker = '<addon id="%s"' % addon_id
    if marker in content:
        start = content.index(marker)
        if "\n</addon>\n" in content[:start]:
            keep_from = content.rindex("\n</addon>\n", 0, start) + len("\n</addon>\n")
        else:
            keep_from = content.index("<addons>") + len("<addons>") + 1
        keep_to = content.index("\n</addon>", start) + len("\n</addon>\n")
        return content[:keep_from] + block + content[keep_to:]
    return content.replace("</addons>", block + "</addons>")


def validate_content(content):
    """Lista de problemas (vacia = addons.xml OK para Kodi)."""
    problems = []
    if "\ufeff" in content:
        problems.append("hay un BOM de UTF-8 dentro del fichero (Kodi puede no parsearlo)")
    positions = [m.start() for m in XML_DECL_RE.finditer(content)]
    stray = [p for p in positions if p != 0]
    if stray:
        problems.append(
            "declaracion <?xml ...?> en mitad del fichero (posiciones %s): "
            "esto es exactamente lo que hace que Kodi diga "
            "'no se pudo conectar al repositorio'" % stray
        )
    root = None
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        problems.append("no es XML valido: %s" % exc)
    if root is not None:
        if root.tag != "addons":
            problems.append("la raiz es <%s>, deberia ser <addons>" % root.tag)
        addons = root.findall("addon")
        if not addons:
            problems.append("no hay ningun <addon> dentro")
        for addon in addons:
            if not addon.get("id") or not addon.get("version"):
                problems.append("<addon> sin id/version: %r" % addon.attrib)
    if "\r\n" in content:
        problems.append("el fichero tiene CRLF; en el repo va con LF")
    return problems


def write(content, repo):
    """Valida y escribe addons.xml + addons.xml.md5. Aborta si algo esta mal."""
    if not content.endswith("\n"):
        content += "\n"
    problems = validate_content(content)
    if problems:
        raise SystemExit(
            "ABORTADO: no se escribe addons.xml (el repositorio quedaria roto para Kodi):\n  - "
            + "\n  - ".join(problems)
        )
    path = os.path.join(repo, "addons.xml")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    digest = hashlib.md5(open(path, "rb").read()).hexdigest()
    with open(os.path.join(repo, "addons.xml.md5"), "w", encoding="utf-8", newline="") as handle:
        handle.write(digest + "\n")
    return digest
