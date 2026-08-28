#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PREPARADOR_CONTENIDO_WIIFLOW_S.py
=================================

Asistente interactivo para publicar contenido pasivo de WiiFlow-S en el
repositorio de contenidos F14.

Contrato soportado:
  mutable index.ini
      -> immutable catalog_<hash8>.ini
          -> immutable content-addressed assets

Tipos soportados actualmente:
  1) HOME background
  2) Avatar + Pointer bundle

El script NO modifica el repositorio.
Genera, fuera del repo, dos carpetas:

  PASO_A_PUBLICAR_INMUTABLES/
      catalog_<hash8>.ini
      assets nuevos...

  PASO_B_CAMBIAR_INDEX/
      index.ini

Esto conserva el orden seguro de publicación:
  A) publicar primero catálogo/assets inmutables
  B) cambiar index.ini después

Dependencias:
  Solo Python 3 estándar.
"""

from __future__ import annotations

import configparser
import hashlib
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


INDEX_NAME = "index.ini"
CATALOG_SECTION = "content"
SCHEMA_VERSION_DEFAULT = "1"

SUPPORTED_BACKGROUND_EXTS = {".png"}
SUPPORTED_PREVIEW_EXTS = {".png", ".jpg", ".jpeg"}
SUPPORTED_BUNDLE_EXTS = {".png"}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_dragged_path(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value.strip()


def ask(prompt: str, default: str | None = None) -> str:
    if default is not None:
        raw = input(f"{prompt} [{default}]: ").strip()
        return raw if raw else default
    return input(f"{prompt}: ").strip()


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = " [S/n]" if default else " [s/N]"
    raw = input(prompt + suffix + ": ").strip().lower()
    if not raw:
        return default
    return raw in ("s", "si", "sí", "y", "yes")


def ask_int(prompt: str, default: int = 1, minimum: int = 1) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            value = int(raw)
        except ValueError:
            print("  Valor inválido.")
            continue
        if value < minimum:
            print(f"  Debe ser >= {minimum}.")
            continue
        return value


def ask_existing_file(prompt: str, exts: set[str]) -> Path:
    while True:
        raw = clean_dragged_path(input(prompt + ": "))
        p = Path(raw).expanduser()
        if not p.is_file():
            print("  No existe ese archivo.")
            continue
        if p.suffix.lower() not in exts:
            print("  Extensión no permitida:", ", ".join(sorted(exts)))
            continue
        return p.resolve()


def slug(value: str) -> str:
    # IDs/catalog filenames deben mantenerse simples y deterministas.
    value = value.strip().lower()
    value = (
        value.replace("á", "a")
             .replace("é", "e")
             .replace("í", "i")
             .replace("ó", "o")
             .replace("ú", "u")
             .replace("ü", "u")
             .replace("ñ", "n")
    )
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "contenido"


def safe_asset_name(prefix: str, logical_id: str, digest: str, ext: str) -> str:
    return f"{slug(prefix)}_{slug(logical_id)}_{digest[:8]}{ext.lower()}"


def read_ini(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None, strict=True)
    cfg.optionxform = str
    with path.open("r", encoding="utf-8-sig") as f:
        cfg.read_file(f)
    return cfg


def render_catalog(cfg: configparser.ConfigParser) -> bytes:
    # ConfigParser.write() agrega una línea vacía entre dominios; el parser Wii
    # acepta el mismo contrato INI. Sin espacios alrededor de "=" para conservar
    # el estilo del catálogo F14.
    from io import StringIO
    s = StringIO()
    cfg.write(s, space_around_delimiters=False)
    text = s.getvalue().replace("\r\n", "\n")
    return text.encode("utf-8")


def render_index(schema_version: str, catalog_name: str,
                 catalog_size: int, catalog_sha256: str) -> bytes:
    text = (
        "[content]\n"
        f"version={schema_version}\n"
        f"catalog={catalog_name}\n"
        f"catalog_size={catalog_size}\n"
        f"catalog_sha256={catalog_sha256}\n"
    )
    return text.encode("utf-8")


def file_metadata(path: Path) -> tuple[int, str]:
    return path.stat().st_size, sha256_file(path)


# ---------------------------------------------------------------------------
# Carga del estado actual
# ---------------------------------------------------------------------------

class RepoState:
    def __init__(self, root: Path):
        self.root = root
        self.index_path = root / INDEX_NAME
        self.schema_version = SCHEMA_VERSION_DEFAULT
        self.current_catalog_name = ""
        self.current_catalog_path: Path | None = None
        self.catalog = configparser.ConfigParser(interpolation=None, strict=True)
        self.catalog.optionxform = str

    def load(self) -> None:
        if not self.index_path.is_file():
            print()
            print("No existe index.ini en:")
            print(" ", self.root)
            if not ask_yes_no("¿Inicializar un repositorio de contenidos nuevo?", False):
                raise SystemExit("Cancelado.")
            self.schema_version = SCHEMA_VERSION_DEFAULT
            return

        idx = read_ini(self.index_path)
        if not idx.has_section(CATALOG_SECTION):
            raise RuntimeError("index.ini no contiene [content].")

        self.schema_version = idx.get(
            CATALOG_SECTION, "version",
            fallback=SCHEMA_VERSION_DEFAULT
        ).strip()

        self.current_catalog_name = idx.get(
            CATALOG_SECTION, "catalog", fallback=""
        ).strip()

        if not self.current_catalog_name:
            raise RuntimeError("index.ini no contiene content.catalog.")

        self.current_catalog_path = self.root / self.current_catalog_name
        if not self.current_catalog_path.is_file():
            raise RuntimeError(
                f"El catálogo actual no existe localmente: "
                f"{self.current_catalog_path}"
            )

        actual_size = self.current_catalog_path.stat().st_size
        actual_sha = sha256_file(self.current_catalog_path)

        expected_size = idx.get(CATALOG_SECTION, "catalog_size", fallback="").strip()
        expected_sha = idx.get(CATALOG_SECTION, "catalog_sha256", fallback="").strip()

        if expected_size and int(expected_size) != actual_size:
            raise RuntimeError(
                f"catalog_size no coincide: index={expected_size}, real={actual_size}"
            )

        if expected_sha and expected_sha.lower() != actual_sha.lower():
            raise RuntimeError(
                "catalog_sha256 no coincide.\n"
                f"  index: {expected_sha}\n"
                f"  real:  {actual_sha}"
            )

        self.catalog = read_ini(self.current_catalog_path)

        print()
        print("Estado actual validado:")
        print(f"  index:    {self.index_path.name}")
        print(f"  catalog:  {self.current_catalog_name}")
        print(f"  entradas: {len(self.catalog.sections())}")
        print(f"  sha256:   {actual_sha}")


# ---------------------------------------------------------------------------
# Staging de assets
# ---------------------------------------------------------------------------

class AssetStage:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        # filename -> source path
        self.new_assets: dict[str, Path] = {}
        # filename -> (size, sha)
        self.meta: dict[str, tuple[int, str]] = {}

    def stage(self, source: Path, desired_name: str) -> tuple[str, int, str]:
        size, digest = file_metadata(source)

        existing = self.repo_root / desired_name
        if existing.is_file():
            if existing.stat().st_size != size or sha256_file(existing) != digest:
                raise RuntimeError(
                    f"Colisión: {desired_name} ya existe con bytes distintos."
                )
            self.meta[desired_name] = (size, digest)
            return desired_name, size, digest

        previous = self.new_assets.get(desired_name)
        if previous is not None:
            if sha256_file(previous) != digest:
                raise RuntimeError(
                    f"Colisión interna: {desired_name} apunta a bytes distintos."
                )
        else:
            self.new_assets[desired_name] = source

        self.meta[desired_name] = (size, digest)
        return desired_name, size, digest


def upsert_section(cfg: configparser.ConfigParser,
                   section: str,
                   values: list[tuple[str, str]]) -> None:
    if cfg.has_section(section):
        if not ask_yes_no(
            f"La entrada [{section}] ya existe. ¿Actualizarla en la nueva generación?",
            False
        ):
            raise RuntimeError("Entrada duplicada cancelada por el usuario.")
        cfg.remove_section(section)

    cfg.add_section(section)
    for key, value in values:
        cfg.set(section, key, value)


def add_background(cfg: configparser.ConfigParser, stage: AssetStage) -> None:
    print()
    print("=== NUEVO HOME BACKGROUND ===")
    image = ask_existing_file(
        "Arrastra/escribe la ruta del PNG de fondo",
        SUPPORTED_BACKGROUND_EXTS
    )

    display = ask("Nombre visible")
    author = ask("Autor", "WiiFlow-S")
    version = ask_int("Versión de esta entrada", 1)

    default_id = slug(display)
    section = slug(ask("ID interno", default_id))

    print()
    print("Preview:")
    print("  Recomendado: JPG/PNG ligero y separado.")
    print("  Si dejas vacío, se reutiliza el mismo PNG del fondo.")
    raw_preview = clean_dragged_path(input("Ruta preview (opcional): "))

    if raw_preview:
        preview = Path(raw_preview).expanduser().resolve()
        if not preview.is_file() or preview.suffix.lower() not in SUPPORTED_PREVIEW_EXTS:
            raise RuntimeError("Preview inválido.")
    else:
        preview = image

    img_size, img_sha = file_metadata(image)
    img_name = safe_asset_name("background", section, img_sha, image.suffix)
    img_name, img_size, img_sha = stage.stage(image, img_name)

    if preview == image:
        preview_name = img_name
        preview_size = img_size
        preview_sha = img_sha
    else:
        preview_size, preview_sha = file_metadata(preview)
        preview_name = safe_asset_name("preview", section, preview_sha, preview.suffix)
        preview_name, preview_size, preview_sha = stage.stage(preview, preview_name)

    upsert_section(cfg, section, [
        ("type", "background"),
        ("name", display),
        ("author", author),
        ("version", str(version)),
        ("preview", preview_name),
        ("preview_size", str(preview_size)),
        ("preview_sha256", preview_sha),
        ("file", img_name),
        ("size", str(img_size)),
        ("sha256", img_sha),
    ])

    print(f"  OK: [{section}]")


def add_avatar_pointer(cfg: configparser.ConfigParser, stage: AssetStage) -> None:
    print()
    print("=== NUEVO AVATAR + POINTER BUNDLE ===")
    avatar = ask_existing_file(
        "Arrastra/escribe la ruta del avatar PNG",
        SUPPORTED_BUNDLE_EXTS
    )
    pointer = ask_existing_file(
        "Arrastra/escribe la ruta del pointer PNG",
        SUPPORTED_BUNDLE_EXTS
    )

    display = ask("Nombre visible del bundle")
    pointer_display = ask("Nombre visible del pointer", f"{display} Pointer")
    author = ask("Autor", "WiiFlow-S")
    version = ask_int("Versión del bundle", 1)

    suffix = slug(ask("Sufijo interno del bundle", slug(display)))
    avatar_id = f"avatar_{suffix}"
    pointer_id = f"pointer_{suffix}"

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
        ("version", str(version)),
        ("preview", av_name),
        ("preview_size", str(av_size)),
        ("preview_sha256", av_sha),
        ("file", av_name),
        ("size", str(av_size)),
        ("sha256", av_sha),
    ])

    upsert_section(cfg, pointer_id, [
        ("type", "pointer"),
        ("name", pointer_display),
        ("author", author),
        ("version", str(version)),
        ("preview", pt_name),
        ("preview_size", str(pt_size)),
        ("preview_sha256", pt_sha),
        ("file", pt_name),
        ("size", str(pt_size)),
        ("sha256", pt_sha),
    ])

    print(f"  OK: [{avatar_id}] + [{pointer_id}]")


# ---------------------------------------------------------------------------
# Validación final
# ---------------------------------------------------------------------------

def validate_catalog_assets(cfg: configparser.ConfigParser,
                            repo_root: Path,
                            stage: AssetStage) -> None:
    checked: set[str] = set()

    for section in cfg.sections():
        for prefix in ("preview", "file"):
            name = cfg.get(section, prefix, fallback="").strip()
            if not name:
                raise RuntimeError(f"[{section}] no contiene {prefix}=.")

            if name in checked:
                continue

            expected_size_key = "preview_size" if prefix == "preview" else "size"
            expected_sha_key = "preview_sha256" if prefix == "preview" else "sha256"

            # Si preview y file son el mismo asset, puede que solo una de las
            # parejas de metadatos se haya comprobado antes. Aun así ambas deben
            # existir en el INI.
            expected_size = int(cfg.get(section, expected_size_key))
            expected_sha = cfg.get(section, expected_sha_key).strip().lower()

            source = stage.new_assets.get(name)
            if source is None:
                source = repo_root / name

            if not source.is_file():
                raise RuntimeError(
                    f"[{section}] referencia asset inexistente: {name}"
                )

            actual_size = source.stat().st_size
            actual_sha = sha256_file(source)

            if actual_size != expected_size:
                raise RuntimeError(
                    f"[{section}] {name}: size esperado {expected_size}, "
                    f"real {actual_size}"
                )

            if actual_sha.lower() != expected_sha:
                raise RuntimeError(
                    f"[{section}] {name}: sha256 no coincide"
                )

            checked.add(name)

    # Contrato A2: avatar_<suffix> requiere pointer_<suffix>.
    sections_lower = {s.lower() for s in cfg.sections()}
    for section in cfg.sections():
        if cfg.get(section, "type", fallback="").strip().lower() != "avatar":
            continue

        sid = section.lower()
        if not sid.startswith("avatar_"):
            raise RuntimeError(
                f"[{section}] type=avatar pero el ID no empieza por avatar_."
            )

        suffix = sid[len("avatar_"):]
        partner = f"pointer_{suffix}"
        if partner not in sections_lower:
            raise RuntimeError(
                f"[{section}] no tiene compañero [{partner}]."
            )


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def build_package(state: RepoState, stage: AssetStage) -> Path:
    validate_catalog_assets(state.catalog, state.root, stage)

    catalog_bytes = render_catalog(state.catalog)
    catalog_sha = sha256_bytes(catalog_bytes)
    catalog_name = f"catalog_{catalog_sha[:8]}.ini"

    index_bytes = render_index(
        state.schema_version,
        catalog_name,
        len(catalog_bytes),
        catalog_sha
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    package_root = state.root.parent / f"PREPARADO_WIIFLOW_S_{stamp}"
    step_a = package_root / "PASO_A_PUBLICAR_INMUTABLES"
    step_b = package_root / "PASO_B_CAMBIAR_INDEX"

    if package_root.exists():
        raise RuntimeError(f"Ya existe: {package_root}")

    step_a.mkdir(parents=True)
    step_b.mkdir(parents=True)

    # Catálogo nuevo: siempre se publica en A.
    (step_a / catalog_name).write_bytes(catalog_bytes)

    # Assets nuevos: solo los que aún no existen en la raíz actual.
    copied_assets: list[str] = []
    for name, source in sorted(stage.new_assets.items()):
        destination = step_a / name
        shutil.copy2(source, destination)
        copied_assets.append(name)

    # Índice mutable: exclusivamente en B.
    (step_b / INDEX_NAME).write_bytes(index_bytes)

    manifest_lines = [
        "WiiFlow-S Content Publisher",
        "=" * 72,
        "",
        f"Repositorio detectado: {state.root}",
        f"Catalogo anterior: {state.current_catalog_name or '(nuevo)'}",
        f"Catalogo nuevo:    {catalog_name}",
        f"SHA-256 catalogo:  {catalog_sha}",
        f"Size catalogo:     {len(catalog_bytes)} bytes",
        f"Entradas totales:  {len(state.catalog.sections())}",
        f"Assets nuevos:     {len(copied_assets)}",
        "",
        "PASO A - PRIMER COMMIT/PUBLICACION",
        "Copiar el CONTENIDO de:",
        f"  {step_a}",
        "a la RAIZ del repositorio de contenidos.",
        "NO modificar index.ini en este paso.",
        "",
        "Archivos Paso A:",
        f"  {catalog_name}",
    ]

    manifest_lines += [f"  {name}" for name in copied_assets]

    manifest_lines += [
        "",
        "PASO B - SOLO DESPUES DE PUBLICAR/VERIFICAR PASO A",
        "Copiar el CONTENIDO de:",
        f"  {step_b}",
        "a la RAIZ del repositorio, reemplazando index.ini.",
        "",
        "No borrar catalogos ni assets hash-named de generaciones anteriores.",
        "La retencion de generaciones antiguas es parte del contrato F14D3T2.",
        "",
        "Nuevo index.ini:",
        index_bytes.decode("utf-8").rstrip(),
        "",
    ]

    (package_root / "MANIFEST_LOCAL.txt").write_text(
        "\n".join(manifest_lines),
        encoding="utf-8",
        newline="\n"
    )

    return package_root


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 72)
    print("WiiFlow-S - PREPARADOR DE CONTENIDO F14")
    print("=" * 72)
    print()
    print("Ejecuta este .py desde la RAIZ del repositorio de contenidos.")
    print("El script NO modifica el repo; prepara dos carpetas fuera de él.")

    script_root = Path(__file__).resolve().parent

    if (script_root / INDEX_NAME).is_file():
        repo_root = script_root
    else:
        raw = clean_dragged_path(
            input("\nNo veo index.ini junto al script.\n"
                  "Ruta de la raiz del repositorio: ")
        )
        repo_root = Path(raw).expanduser().resolve()

    if not repo_root.is_dir():
        raise RuntimeError(f"No existe el directorio: {repo_root}")

    state = RepoState(repo_root)
    state.load()

    stage = AssetStage(repo_root)

    while True:
        print()
        print("-" * 72)
        print("¿Qué deseas agregar a la nueva generación?")
        print("  1) HOME background")
        print("  2) Avatar + Pointer bundle")
        print("  3) Terminar y generar paquete de publicación")
        print("  0) Salir sin generar")
        choice = input("> ").strip()

        try:
            if choice == "1":
                add_background(state.catalog, stage)
            elif choice == "2":
                add_avatar_pointer(state.catalog, stage)
            elif choice == "3":
                if not state.catalog.sections():
                    print("No hay entradas para publicar.")
                    continue

                package = build_package(state, stage)
                print()
                print("=" * 72)
                print("PAQUETE PREPARADO")
                print("=" * 72)
                print(package)
                print()
                print("1) Publica primero PASO_A_PUBLICAR_INMUTABLES")
                print("2) Verifica que esos archivos ya estén disponibles")
                print("3) Después publica PASO_B_CAMBIAR_INDEX/index.ini")
                print()
                print("Lee MANIFEST_LOCAL.txt antes de subir.")
                return 0
            elif choice == "0":
                print("Cancelado. El repositorio no fue modificado.")
                return 0
            else:
                print("Opción inválida.")
        except RuntimeError as e:
            print()
            print("ERROR:", e)
            print("No se generó ninguna publicación con ese intento.")

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelado por el usuario.")
        raise SystemExit(130)
    except Exception as e:
        print("\nFALLO:", e)
        raise SystemExit(1)
