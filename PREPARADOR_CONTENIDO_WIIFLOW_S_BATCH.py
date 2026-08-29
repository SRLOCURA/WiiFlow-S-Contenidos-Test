#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PREPARADOR_CONTENIDO_WIIFLOW_S_BATCH.py
=======================================

Preparador interactivo + por lotes para el repositorio de contenidos F14.

Contrato de publicación conservado:
    index.ini mutable
        -> catalog_<hash8>.ini inmutable
            -> assets hash-named inmutables

No modifica el repositorio. Genera fuera del repo:
    PREPARADO_WIIFLOW_S_<fecha>/
        PASO_A_PUBLICAR_INMUTABLES/
        PASO_B_CAMBIAR_INDEX/
        MANIFEST_LOCAL.txt

Modos:
    - Fondo HOME individual
    - Avatar + Pointer individual
    - Carpeta completa de fondos
    - Carpetas completas de avatars + pointers
    - Lote completo (fondos + avatars + pointers)
    - Crear plantilla de carpetas para un lote

Estructura de lote recomendada:
    LOTE/
        fondos/
            Mi Fondo.png
            Otro Fondo.png
        previews_fondos/            (opcional)
            Mi Fondo.jpg
            Otro Fondo.png
        avatars/
            WS Orbit.png
            Creepy Ghost.png
        pointers/
            WS Orbit.png
            Creepy Ghost.png

Reglas:
    - Fondos: cada PNG = una entrada.
    - Preview: se busca por el mismo nombre base en previews_fondos/.
      Si no existe, se reutiliza el PNG del fondo.
    - Avatar y Pointer: se emparejan por el MISMO nombre base.
      Ej.: avatars/WS Orbit.png <-> pointers/WS Orbit.png
    - Si hay un avatar o pointer sin pareja, el lote se bloquea.
    - Por lote se puede usar un autor y versión comunes.
    - Los nombres visibles se derivan del nombre del archivo.
    - Los IDs se sanitizan automáticamente.
    - Se puede elegir política de duplicados: actualizar, omitir o bloquear.

Dependencias:
    Solo Python 3 estándar.
"""

import configparser
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from io import StringIO
from pathlib import Path


INDEX_NAME = "index.ini"
CATALOG_SECTION = "content"
SCHEMA_VERSION_DEFAULT = "1"

BACKGROUND_EXTS = {".png"}
PREVIEW_EXTS = {".png", ".jpg", ".jpeg"}
AVATAR_POINTER_EXTS = {".png"}

BATCH_BACKGROUNDS_DIR = "fondos"
BATCH_PREVIEWS_DIR = "previews_fondos"
BATCH_AVATARS_DIR = "avatars"
BATCH_POINTERS_DIR = "pointers"


# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------

def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def clean_dragged_path(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def ask(prompt, default=None):
    if default is not None:
        raw = input("{} [{}]: ".format(prompt, default)).strip()
        return raw if raw else str(default)
    return input(prompt + ": ").strip()


def ask_yes_no(prompt, default=False):
    suffix = " [S/n]" if default else " [s/N]"
    raw = input(prompt + suffix + ": ").strip().lower()
    if not raw:
        return default
    return raw in ("s", "si", "sí", "y", "yes")


def ask_int(prompt, default=1, minimum=1):
    while True:
        raw = ask(prompt, str(default))
        try:
            value = int(raw)
        except ValueError:
            print("  Valor inválido.")
            continue
        if value < minimum:
            print("  Debe ser >= {}.".format(minimum))
            continue
        return value


def ask_existing_file(prompt, exts):
    while True:
        raw = clean_dragged_path(input(prompt + ": "))
        p = Path(raw).expanduser()
        if not p.is_file():
            print("  No existe ese archivo.")
            continue
        if p.suffix.lower() not in exts:
            print("  Extensión no permitida: {}".format(", ".join(sorted(exts))))
            continue
        return p.resolve()


def ask_existing_dir(prompt):
    while True:
        raw = clean_dragged_path(input(prompt + ": "))
        p = Path(raw).expanduser()
        if p.is_dir():
            return p.resolve()
        print("  No existe esa carpeta.")


def slug(value):
    value = value.strip().lower()
    for a, b in (
        ("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"),
        ("ü", "u"), ("ñ", "n")
    ):
        value = value.replace(a, b)
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "contenido"


def pretty_name(stem):
    value = stem.replace("_", " ").replace("-", " ")
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return "Contenido"
    # Conserva siglas ya escritas en mayúsculas.
    words = []
    for word in value.split(" "):
        if len(word) > 1 and word.isupper():
            words.append(word)
        else:
            words.append(word[:1].upper() + word[1:])
    return " ".join(words)


def safe_asset_name(prefix, logical_id, digest, ext):
    return "{}_{}_{}{}".format(
        slug(prefix), slug(logical_id), digest[:8], ext.lower()
    )


def read_ini(path):
    cfg = configparser.ConfigParser(interpolation=None, strict=True)
    cfg.optionxform = str
    with path.open("r", encoding="utf-8-sig") as f:
        cfg.read_file(f)
    return cfg


def render_catalog(cfg):
    s = StringIO()
    cfg.write(s, space_around_delimiters=False)
    return s.getvalue().replace("\r\n", "\n").encode("utf-8")


def render_index(schema_version, catalog_name, catalog_size, catalog_sha256):
    return (
        "[content]\n"
        "version={}\n"
        "catalog={}\n"
        "catalog_size={}\n"
        "catalog_sha256={}\n"
    ).format(
        schema_version, catalog_name, catalog_size, catalog_sha256
    ).encode("utf-8")


def file_metadata(path):
    return path.stat().st_size, sha256_file(path)


def immediate_files(folder, exts):
    if not folder.is_dir():
        return []
    return sorted(
        [p for p in folder.iterdir()
         if p.is_file() and p.suffix.lower() in exts],
        key=lambda p: p.name.lower()
    )


def stem_map(folder, exts):
    result = {}
    for p in immediate_files(folder, exts):
        key = p.stem.casefold()
        if key in result:
            raise RuntimeError(
                "Dos archivos comparten el mismo nombre base en {}: {} / {}".format(
                    folder, result[key].name, p.name
                )
            )
        result[key] = p
    return result


def choose_duplicate_policy():
    print()
    print("Política si una entrada ya existe en el catálogo:")
    print("  1) Actualizarla")
    print("  2) Omitirla")
    print("  3) Bloquear el lote")
    while True:
        choice = input("> ").strip()
        if choice == "1":
            return "update"
        if choice == "2":
            return "skip"
        if choice == "3":
            return "error"
        print("Opción inválida.")


# ---------------------------------------------------------------------------
# Repo state
# ---------------------------------------------------------------------------

class RepoState(object):
    def __init__(self, root):
        self.root = root
        self.index_path = root / INDEX_NAME
        self.schema_version = SCHEMA_VERSION_DEFAULT
        self.current_catalog_name = ""
        self.current_catalog_path = None
        self.catalog = configparser.ConfigParser(interpolation=None, strict=True)
        self.catalog.optionxform = str

    def load(self):
        if not self.index_path.is_file():
            print()
            print("No existe index.ini en:")
            print(" ", self.root)
            if not ask_yes_no(
                "¿Inicializar un repositorio de contenidos nuevo?", False
            ):
                raise SystemExit("Cancelado.")
            return

        idx = read_ini(self.index_path)
        if not idx.has_section(CATALOG_SECTION):
            raise RuntimeError("index.ini no contiene [content].")

        self.schema_version = idx.get(
            CATALOG_SECTION, "version", fallback=SCHEMA_VERSION_DEFAULT
        ).strip()

        self.current_catalog_name = idx.get(
            CATALOG_SECTION, "catalog", fallback=""
        ).strip()

        if not self.current_catalog_name:
            raise RuntimeError("index.ini no contiene content.catalog.")

        self.current_catalog_path = self.root / self.current_catalog_name
        if not self.current_catalog_path.is_file():
            raise RuntimeError(
                "El catálogo actual no existe: {}".format(self.current_catalog_path)
            )

        actual_size = self.current_catalog_path.stat().st_size
        actual_sha = sha256_file(self.current_catalog_path)

        expected_size = idx.get(
            CATALOG_SECTION, "catalog_size", fallback=""
        ).strip()
        expected_sha = idx.get(
            CATALOG_SECTION, "catalog_sha256", fallback=""
        ).strip()

        if expected_size and int(expected_size) != actual_size:
            raise RuntimeError(
                "catalog_size no coincide: index={}, real={}".format(
                    expected_size, actual_size
                )
            )

        if expected_sha and expected_sha.lower() != actual_sha.lower():
            raise RuntimeError(
                "catalog_sha256 no coincide.\n  index: {}\n  real:  {}".format(
                    expected_sha, actual_sha
                )
            )

        self.catalog = read_ini(self.current_catalog_path)

        print()
        print("Estado actual validado:")
        print("  index:    {}".format(self.index_path.name))
        print("  catalog:  {}".format(self.current_catalog_name))
        print("  entradas: {}".format(len(self.catalog.sections())))
        print("  sha256:   {}".format(actual_sha))


# ---------------------------------------------------------------------------
# Asset staging
# ---------------------------------------------------------------------------

class AssetStage(object):
    def __init__(self, repo_root):
        self.repo_root = repo_root
        self.new_assets = {}

    def stage(self, source, desired_name):
        size, digest = file_metadata(source)

        existing = self.repo_root / desired_name
        if existing.is_file():
            if (
                existing.stat().st_size != size
                or sha256_file(existing) != digest
            ):
                raise RuntimeError(
                    "Colisión: {} ya existe con bytes distintos.".format(desired_name)
                )
            return desired_name, size, digest

        previous = self.new_assets.get(desired_name)
        if previous is not None:
            if sha256_file(previous) != digest:
                raise RuntimeError(
                    "Colisión interna: {} apunta a bytes distintos.".format(
                        desired_name
                    )
                )
        else:
            self.new_assets[desired_name] = source

        return desired_name, size, digest


def upsert_section(cfg, section, values, duplicate_policy=None):
    exists = cfg.has_section(section)

    if exists:
        if duplicate_policy is None:
            if not ask_yes_no(
                "La entrada [{}] ya existe. ¿Actualizarla?".format(section), False
            ):
                return False
        elif duplicate_policy == "skip":
            return False
        elif duplicate_policy == "error":
            raise RuntimeError(
                "La entrada [{}] ya existe y la política es BLOQUEAR.".format(section)
            )
        elif duplicate_policy != "update":
            raise RuntimeError("Política de duplicados desconocida.")

        cfg.remove_section(section)

    cfg.add_section(section)
    for key, value in values:
        cfg.set(section, key, str(value))
    return True


def stage_background(cfg, stage, image, preview, section, display,
                     author, version, duplicate_policy=None):
    img_size, img_sha = file_metadata(image)
    img_name = safe_asset_name(
        "background", section, img_sha, image.suffix
    )
    img_name, img_size, img_sha = stage.stage(
        image, img_name
    )

    if preview == image:
        preview_name = img_name
        preview_size = img_size
        preview_sha = img_sha
    else:
        preview_size, preview_sha = file_metadata(preview)
        preview_name = safe_asset_name(
            "preview", section, preview_sha, preview.suffix
        )
        preview_name, preview_size, preview_sha = stage.stage(
            preview, preview_name
        )

    return upsert_section(cfg, section, [
        ("type", "background"),
        ("name", display),
        ("author", author),
        ("version", version),
        ("preview", preview_name),
        ("preview_size", preview_size),
        ("preview_sha256", preview_sha),
        ("file", img_name),
        ("size", img_size),
        ("sha256", img_sha),
    ], duplicate_policy)


def stage_avatar_pointer(cfg, stage, avatar, pointer, suffix, display,
                         pointer_display, author, version,
                         duplicate_policy=None):
    avatar_id = "avatar_" + slug(suffix)
    pointer_id = "pointer_" + slug(suffix)

    # Primero decide si ambas entradas pueden agregarse. Si una se omite y otra
    # no, romperíamos el bundle; tratamos el par como unidad.
    avatar_exists = cfg.has_section(avatar_id)
    pointer_exists = cfg.has_section(pointer_id)

    if avatar_exists or pointer_exists:
        if duplicate_policy is None:
            if not ask_yes_no(
                "El bundle [{}] ya existe parcial o totalmente. ¿Actualizar ambos?".format(
                    suffix
                ),
                False
            ):
                return False
            duplicate_policy_effective = "update"
        elif duplicate_policy == "skip":
            return False
        elif duplicate_policy == "error":
            raise RuntimeError(
                "El bundle [{}] ya existe y la política es BLOQUEAR.".format(suffix)
            )
        else:
            duplicate_policy_effective = "update"
    else:
        duplicate_policy_effective = "update"

    av_size, av_sha = file_metadata(avatar)
    av_name = safe_asset_name("avatar", suffix, av_sha, ".png")
    av_name, av_size, av_sha = stage.stage(avatar, av_name)

    pt_size, pt_sha = file_metadata(pointer)
    pt_name = safe_asset_name("pointer", suffix, pt_sha, ".png")
    pt_name, pt_size, pt_sha = stage.stage(pointer, pt_name)

    upsert_section(cfg, avatar_id, [
        ("type", "avatar"),
        ("name", display),
        ("author", author),
        ("version", version),
        ("preview", av_name),
        ("preview_size", av_size),
        ("preview_sha256", av_sha),
        ("file", av_name),
        ("size", av_size),
        ("sha256", av_sha),
    ], duplicate_policy_effective)

    upsert_section(cfg, pointer_id, [
        ("type", "pointer"),
        ("name", pointer_display),
        ("author", author),
        ("version", version),
        ("preview", pt_name),
        ("preview_size", pt_size),
        ("preview_sha256", pt_sha),
        ("file", pt_name),
        ("size", pt_size),
        ("sha256", pt_sha),
    ], duplicate_policy_effective)

    return True


# ---------------------------------------------------------------------------
# Individual modes
# ---------------------------------------------------------------------------

def add_background_individual(cfg, stage):
    print()
    print("=== HOME BACKGROUND INDIVIDUAL ===")
    image = ask_existing_file(
        "Ruta del PNG de fondo", BACKGROUND_EXTS
    )
    display = ask("Nombre visible", pretty_name(image.stem))
    author = ask("Autor", "WiiFlow-S")
    version = ask_int("Versión", 1)
    section = slug(ask("ID interno", slug(image.stem)))

    raw_preview = clean_dragged_path(
        input("Ruta preview opcional (ENTER = mismo fondo): ")
    )
    if raw_preview:
        preview = Path(raw_preview).expanduser().resolve()
        if (
            not preview.is_file()
            or preview.suffix.lower() not in PREVIEW_EXTS
        ):
            raise RuntimeError("Preview inválido.")
    else:
        preview = image

    added = stage_background(
        cfg, stage, image, preview, section, display,
        author, version, duplicate_policy=None
    )
    print("  " + ("OK" if added else "OMITIDO"))


def add_avatar_pointer_individual(cfg, stage):
    print()
    print("=== AVATAR + POINTER INDIVIDUAL ===")
    avatar = ask_existing_file("Avatar PNG", AVATAR_POINTER_EXTS)
    pointer = ask_existing_file("Pointer PNG", AVATAR_POINTER_EXTS)
    display = ask("Nombre visible", pretty_name(avatar.stem))
    pointer_display = ask(
        "Nombre visible del pointer", "{} Pointer".format(display)
    )
    author = ask("Autor", "WiiFlow-S")
    version = ask_int("Versión", 1)
    suffix = slug(ask("Sufijo interno", slug(avatar.stem)))

    added = stage_avatar_pointer(
        cfg, stage, avatar, pointer, suffix, display,
        pointer_display, author, version, duplicate_policy=None
    )
    print("  " + ("OK" if added else "OMITIDO"))


# ---------------------------------------------------------------------------
# Batch modes
# ---------------------------------------------------------------------------

def find_preview_for_background(previews_dir, image):
    if not previews_dir.is_dir():
        return image

    stem_cf = image.stem.casefold()
    matches = [
        p for p in immediate_files(previews_dir, PREVIEW_EXTS)
        if p.stem.casefold() == stem_cf
    ]

    if len(matches) > 1:
        raise RuntimeError(
            "Más de un preview coincide con '{}': {}".format(
                image.stem,
                ", ".join(p.name for p in matches)
            )
        )
    return matches[0] if matches else image


def batch_backgrounds(cfg, stage, backgrounds_dir, previews_dir=None,
                      author="WiiFlow-S", version=1,
                      duplicate_policy="update"):
    images = immediate_files(backgrounds_dir, BACKGROUND_EXTS)
    if not images:
        print("  No hay fondos PNG en: {}".format(backgrounds_dir))
        return {"found": 0, "added": 0, "skipped": 0}

    result = {"found": len(images), "added": 0, "skipped": 0}

    for image in images:
        section = slug(image.stem)
        display = pretty_name(image.stem)
        preview = (
            find_preview_for_background(previews_dir, image)
            if previews_dir is not None else image
        )

        added = stage_background(
            cfg, stage, image, preview, section, display,
            author, version, duplicate_policy
        )
        if added:
            result["added"] += 1
            print("  + Fondo: {}".format(display))
        else:
            result["skipped"] += 1
            print("  = Omitido: {}".format(display))

    return result


def batch_avatar_pointer(cfg, stage, avatars_dir, pointers_dir,
                         author="WiiFlow-S", version=1,
                         duplicate_policy="update"):
    avatars = stem_map(avatars_dir, AVATAR_POINTER_EXTS)
    pointers = stem_map(pointers_dir, AVATAR_POINTER_EXTS)

    avatar_keys = set(avatars.keys())
    pointer_keys = set(pointers.keys())

    missing_pointer = sorted(avatar_keys - pointer_keys)
    missing_avatar = sorted(pointer_keys - avatar_keys)

    if missing_pointer or missing_avatar:
        lines = ["El lote Avatar+Pointer está incompleto."]
        if missing_pointer:
            lines.append(
                "Sin pointer: " + ", ".join(avatars[k].name for k in missing_pointer)
            )
        if missing_avatar:
            lines.append(
                "Sin avatar: " + ", ".join(pointers[k].name for k in missing_avatar)
            )
        raise RuntimeError("\n".join(lines))

    keys = sorted(avatar_keys)
    if not keys:
        print("  No hay bundles Avatar+Pointer.")
        return {"found": 0, "added": 0, "skipped": 0}

    result = {"found": len(keys), "added": 0, "skipped": 0}

    for key in keys:
        avatar = avatars[key]
        pointer = pointers[key]
        suffix = slug(avatar.stem)
        display = pretty_name(avatar.stem)
        pointer_display = "{} Pointer".format(display)

        added = stage_avatar_pointer(
            cfg, stage, avatar, pointer, suffix, display,
            pointer_display, author, version, duplicate_policy
        )
        if added:
            result["added"] += 1
            print("  + Bundle: {}".format(display))
        else:
            result["skipped"] += 1
            print("  = Omitido: {}".format(display))

    return result


def prompt_batch_defaults():
    author = ask("Autor común del lote", "WiiFlow-S")
    version = ask_int("Versión común del lote", 1)
    duplicate_policy = choose_duplicate_policy()
    return author, version, duplicate_policy


def import_complete_batch(cfg, stage, root):
    backgrounds_dir = root / BATCH_BACKGROUNDS_DIR
    previews_dir = root / BATCH_PREVIEWS_DIR
    avatars_dir = root / BATCH_AVATARS_DIR
    pointers_dir = root / BATCH_POINTERS_DIR

    print()
    print("Lote detectado:")
    print("  fondos:           {}".format(backgrounds_dir))
    print("  previews_fondos:  {}{}".format(
        previews_dir, "" if previews_dir.is_dir() else " (no existe / opcional)"
    ))
    print("  avatars:          {}".format(avatars_dir))
    print("  pointers:         {}".format(pointers_dir))

    author, version, duplicate_policy = prompt_batch_defaults()

    summary = {}

    if backgrounds_dir.is_dir():
        print()
        print("=== IMPORTANDO FONDOS ===")
        summary["backgrounds"] = batch_backgrounds(
            cfg, stage, backgrounds_dir,
            previews_dir if previews_dir.is_dir() else None,
            author, version, duplicate_policy
        )
    else:
        summary["backgrounds"] = {"found": 0, "added": 0, "skipped": 0}

    if avatars_dir.is_dir() or pointers_dir.is_dir():
        if not avatars_dir.is_dir() or not pointers_dir.is_dir():
            raise RuntimeError(
                "Para bundles deben existir ambas carpetas: avatars/ y pointers/."
            )
        print()
        print("=== IMPORTANDO AVATAR + POINTER ===")
        summary["bundles"] = batch_avatar_pointer(
            cfg, stage, avatars_dir, pointers_dir,
            author, version, duplicate_policy
        )
    else:
        summary["bundles"] = {"found": 0, "added": 0, "skipped": 0}

    return summary


def create_batch_template():
    raw = clean_dragged_path(
        input("Carpeta donde crear la plantilla: ")
    )
    root = Path(raw).expanduser().resolve() / "LOTE_WIIFLOW_S"

    if root.exists():
        raise RuntimeError("Ya existe: {}".format(root))

    for name in (
        BATCH_BACKGROUNDS_DIR,
        BATCH_PREVIEWS_DIR,
        BATCH_AVATARS_DIR,
        BATCH_POINTERS_DIR,
    ):
        (root / name).mkdir(parents=True, exist_ok=True)

    readme = """LOTE WiiFlow-S

1. Coloca fondos PNG en:
   fondos/

2. Opcional: previews JPG/PNG con el MISMO nombre base en:
   previews_fondos/

3. Coloca avatars PNG en:
   avatars/

4. Coloca pointers PNG con el MISMO nombre base que su avatar en:
   pointers/

Ejemplo:
   avatars/WS Orbit.png
   pointers/WS Orbit.png

Luego ejecuta el preparador y elige:
   "Importar lote completo"
"""
    (root / "LEEME.txt").write_text(readme, encoding="utf-8", newline="\n")
    print()
    print("Plantilla creada:")
    print(" ", root)


# ---------------------------------------------------------------------------
# Validation + output
# ---------------------------------------------------------------------------

def validate_catalog_assets(cfg, repo_root, stage):
    for section in cfg.sections():
        typ = cfg.get(section, "type", fallback="").strip().lower()
        if typ not in ("background", "avatar", "pointer"):
            # Conserva futuras/otras entradas que el catálogo ya tenga sin
            # intentar reinterpretarlas.
            continue

        for prefix in ("preview", "file"):
            name = cfg.get(section, prefix, fallback="").strip()
            if not name:
                raise RuntimeError(
                    "[{}] no contiene {}=.".format(section, prefix)
                )

            expected_size_key = (
                "preview_size" if prefix == "preview" else "size"
            )
            expected_sha_key = (
                "preview_sha256" if prefix == "preview" else "sha256"
            )

            if not cfg.has_option(section, expected_size_key):
                raise RuntimeError(
                    "[{}] no contiene {}.".format(section, expected_size_key)
                )
            if not cfg.has_option(section, expected_sha_key):
                raise RuntimeError(
                    "[{}] no contiene {}.".format(section, expected_sha_key)
                )

            expected_size = int(cfg.get(section, expected_size_key))
            expected_sha = cfg.get(section, expected_sha_key).strip().lower()

            source = stage.new_assets.get(name)
            if source is None:
                source = repo_root / name

            if not source.is_file():
                raise RuntimeError(
                    "[{}] referencia asset inexistente: {}".format(section, name)
                )

            actual_size = source.stat().st_size
            actual_sha = sha256_file(source)

            if actual_size != expected_size:
                raise RuntimeError(
                    "[{}] {}: size esperado {}, real {}".format(
                        section, name, expected_size, actual_size
                    )
                )
            if actual_sha.lower() != expected_sha:
                raise RuntimeError(
                    "[{}] {}: sha256 no coincide".format(section, name)
                )

    # Bundle contract
    section_map = {s.lower(): s for s in cfg.sections()}
    for section in cfg.sections():
        if cfg.get(section, "type", fallback="").strip().lower() != "avatar":
            continue
        sid = section.lower()
        if not sid.startswith("avatar_"):
            raise RuntimeError(
                "[{}] type=avatar pero ID no empieza por avatar_.".format(section)
            )
        suffix = sid[len("avatar_"):]
        partner = "pointer_" + suffix
        if partner not in section_map:
            raise RuntimeError(
                "[{}] no tiene compañero [{}].".format(section, partner)
            )


def build_package(state, stage, session_summary):
    validate_catalog_assets(state.catalog, state.root, stage)

    catalog_bytes = render_catalog(state.catalog)
    catalog_sha = sha256_bytes(catalog_bytes)
    catalog_name = "catalog_{}.ini".format(catalog_sha[:8])

    index_bytes = render_index(
        state.schema_version,
        catalog_name,
        len(catalog_bytes),
        catalog_sha
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    package_root = (
        state.root.parent / "PREPARADO_WIIFLOW_S_{}".format(stamp)
    )
    step_a = package_root / "PASO_A_PUBLICAR_INMUTABLES"
    step_b = package_root / "PASO_B_CAMBIAR_INDEX"

    step_a.mkdir(parents=True)
    step_b.mkdir(parents=True)

    (step_a / catalog_name).write_bytes(catalog_bytes)

    copied_assets = []
    for name, source in sorted(stage.new_assets.items()):
        destination = step_a / name
        shutil.copy2(source, destination)
        copied_assets.append(name)

    (step_b / INDEX_NAME).write_bytes(index_bytes)

    manifest = [
        "WiiFlow-S Content Publisher BATCH",
        "=" * 72,
        "",
        "Repositorio detectado: {}".format(state.root),
        "Catalogo anterior: {}".format(
            state.current_catalog_name or "(nuevo)"
        ),
        "Catalogo nuevo:    {}".format(catalog_name),
        "SHA-256 catalogo:  {}".format(catalog_sha),
        "Size catalogo:     {} bytes".format(len(catalog_bytes)),
        "Entradas totales:  {}".format(len(state.catalog.sections())),
        "Assets nuevos:     {}".format(len(copied_assets)),
        "",
        "RESUMEN DE ESTA SESION",
    ]

    for line in session_summary:
        manifest.append("  " + line)

    manifest += [
        "",
        "PASO A - PUBLICAR PRIMERO",
        "Copiar el CONTENIDO de:",
        "  {}".format(step_a),
        "a la RAIZ del repositorio.",
        "NO modificar index.ini todavía.",
        "",
        "Archivos Paso A:",
        "  " + catalog_name,
    ]
    manifest += ["  " + x for x in copied_assets]

    manifest += [
        "",
        "PASO B - SOLO DESPUES DE PUBLICAR/VERIFICAR PASO A",
        "Copiar el CONTENIDO de:",
        "  {}".format(step_b),
        "a la RAIZ del repositorio, reemplazando index.ini.",
        "",
        "No borrar generaciones antiguas.",
        "",
        "Nuevo index.ini:",
        index_bytes.decode("utf-8").rstrip(),
        "",
    ]

    (package_root / "MANIFEST_LOCAL.txt").write_text(
        "\n".join(manifest),
        encoding="utf-8",
        newline="\n"
    )

    return package_root


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 72)
    print("WiiFlow-S - PREPARADOR DE CONTENIDO F14 / BATCH")
    print("=" * 72)
    print()
    print("El script NO modifica el repositorio.")
    print("Puede importar imágenes individuales o carpetas completas.")

    script_root = Path(__file__).resolve().parent

    if (script_root / INDEX_NAME).is_file():
        repo_root = script_root
    else:
        raw = clean_dragged_path(
            input(
                "\nNo veo index.ini junto al script.\n"
                "Ruta de la raíz del repositorio de contenidos: "
            )
        )
        repo_root = Path(raw).expanduser().resolve()

    if not repo_root.is_dir():
        raise RuntimeError("No existe: {}".format(repo_root))

    state = RepoState(repo_root)
    state.load()
    stage = AssetStage(repo_root)
    session_summary = []

    while True:
        print()
        print("-" * 72)
        print("¿Qué deseas hacer?")
        print("  1) Agregar un Fondo HOME")
        print("  2) Agregar un Avatar + Pointer")
        print("  3) Importar CARPETA completa de fondos")
        print("  4) Importar CARPETAS completas de avatars + pointers")
        print("  5) Importar LOTE completo (fondos + avatars + pointers)")
        print("  6) Crear plantilla de carpetas para un lote")
        print("  7) Terminar y generar paquete de publicación")
        print("  0) Salir sin generar")
        choice = input("> ").strip()

        try:
            if choice == "1":
                add_background_individual(state.catalog, stage)
                session_summary.append("Fondo individual procesado")

            elif choice == "2":
                add_avatar_pointer_individual(state.catalog, stage)
                session_summary.append("Bundle individual procesado")

            elif choice == "3":
                folder = ask_existing_dir("Carpeta de fondos PNG")
                preview_raw = clean_dragged_path(
                    input(
                        "Carpeta de previews opcional "
                        "(ENTER = reutilizar cada fondo): "
                    )
                )
                previews = (
                    Path(preview_raw).expanduser().resolve()
                    if preview_raw else None
                )
                if previews is not None and not previews.is_dir():
                    raise RuntimeError("La carpeta de previews no existe.")

                author, version, policy = prompt_batch_defaults()
                result = batch_backgrounds(
                    state.catalog, stage, folder, previews,
                    author, version, policy
                )
                session_summary.append(
                    "Fondos: encontrados={found}, agregados={added}, "
                    "omitidos={skipped}".format(**result)
                )

            elif choice == "4":
                avatars = ask_existing_dir("Carpeta de avatars PNG")
                pointers = ask_existing_dir("Carpeta de pointers PNG")
                author, version, policy = prompt_batch_defaults()
                result = batch_avatar_pointer(
                    state.catalog, stage, avatars, pointers,
                    author, version, policy
                )
                session_summary.append(
                    "Bundles: encontrados={found}, agregados={added}, "
                    "omitidos={skipped}".format(**result)
                )

            elif choice == "5":
                root = ask_existing_dir("Carpeta raíz del lote")
                summary = import_complete_batch(
                    state.catalog, stage, root
                )
                bg = summary["backgrounds"]
                bu = summary["bundles"]
                session_summary.append(
                    "Lote fondos: encontrados={found}, agregados={added}, "
                    "omitidos={skipped}".format(**bg)
                )
                session_summary.append(
                    "Lote bundles: encontrados={found}, agregados={added}, "
                    "omitidos={skipped}".format(**bu)
                )

            elif choice == "6":
                create_batch_template()

            elif choice == "7":
                if not state.catalog.sections():
                    print("No hay entradas para publicar.")
                    continue
                package = build_package(
                    state, stage, session_summary
                )
                print()
                print("=" * 72)
                print("PAQUETE PREPARADO")
                print("=" * 72)
                print(package)
                print()
                print("1) Publica PASO_A_PUBLICAR_INMUTABLES")
                print("2) Verifica que ya esté disponible")
                print("3) Después publica PASO_B_CAMBIAR_INDEX/index.ini")
                print()
                print("Lee MANIFEST_LOCAL.txt.")
                return 0

            elif choice == "0":
                print("Cancelado. El repositorio no fue modificado.")
                return 0

            else:
                print("Opción inválida.")

        except RuntimeError as exc:
            print()
            print("ERROR:", exc)
            print("Ese intento no se agregó al paquete.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
        raise SystemExit(130)
    except Exception as exc:
        print("\nFALLO:", exc)
        raise SystemExit(1)
