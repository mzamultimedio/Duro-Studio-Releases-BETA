#!/usr/bin/env python3
"""
Promueve y sincroniza releases de borrador / versionados (ej: beta-windows-v3.0.0)
hacia los tags canónicos fijos (ej: beta-windows) que consultan los auto-updaters
de Duro Studio (Tauri v2 y Legacy NW.js) y la página web oficial.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

TARGET_REPO = os.environ.get("TARGET_REPO", "mzamultimedio/Duro-Studio-Releases-BETA")
GH_TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
FILTER_VERSION = os.environ.get("FILTER_VERSION", "").strip().lstrip("v")
FILTER_TAG = os.environ.get("FILTER_TAG", "").strip()
INCLUDE_DRAFTS = os.environ.get("INCLUDE_DRAFTS", "true").strip().lower() == "true"

# Tags que no deben eliminarse por compatibilidad con enlaces externos existentes
PROTECTED_SOURCE_TAGS = {
    "beta-windows-xp-v3.0.0",
}

PLATFORMS: dict[str, dict[str, Any]] = {
    "beta-windows": {
        "title": lambda v: f"{v}",
        "default_body": "Actualización automática de la versión Beta para Windows.",
        "make_latest": "true",
    },
    "beta-windows-x86": {
        "title": lambda v: f"{v} (Windows 32-bit)",
        "default_body": "Actualización automática de la versión Beta para Windows de 32 bits (x86).",
        "make_latest": "false",
    },
    "beta-windows-xp": {
        "title": lambda v: f"{v} (Windows XP / 7 / 8 Legacy)",
        "default_body": "Nueva versión de Duro Studio.",
        "make_latest": "false",
    },
    "beta-linux": {
        "title": lambda v: f"{v}",
        "default_body": "Actualización automática de la versión Beta para Linux.",
        "make_latest": "false",
    },
    "beta-pi": {
        "title": lambda v: f"{v}",
        "default_body": "Actualización automática de la versión Beta para Raspberry Pi 4.",
        "make_latest": "false",
    },
    "beta-macos": {
        "title": lambda v: f"{v}",
        "default_body": "Actualización automática de la versión Beta para macOS.",
        "make_latest": "false",
    },
    "beta-macos-intel": {
        "title": lambda v: f"{v} (Intel)",
        "default_body": "Actualización automática de la versión Beta para macOS.",
        "make_latest": "false",
    },
}

TAG_RE = re.compile(
    r"^(beta-windows-x86|beta-windows-xp|beta-windows|beta-linux|beta-macos-intel|beta-macos|beta-pi)-v(.+)$"
)

RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now_rfc3339() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_semver_key(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else (0,)


def gh_api_json(
    method: str,
    endpoint: str,
    payload: dict[str, Any] | None = None,
    allow_404: bool = False,
) -> Any:
    url = (
        endpoint
        if endpoint.startswith("https://")
        else f"https://api.github.com{endpoint}"
    )
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "DuroStudio-ReleasePromoter",
    }
    if data is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        if allow_404 and e.code == 404:
            return None
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {endpoint} falló ({e.code}): {err_body}") from e


def list_all_releases() -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = gh_api_json(
            "GET",
            f"/repos/{TARGET_REPO}/releases?per_page=100&page={page}",
        )
        if not batch:
            break
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return releases


def download_asset(asset: dict[str, Any], dest_path: Path) -> None:
    asset_id = asset["id"]
    expected_size = asset.get("size", 0)
    api_url = f"https://api.github.com/repos/{TARGET_REPO}/releases/assets/{asset_id}"

    opener = urllib.request.build_opener(_NoRedirectHandler)
    req = urllib.request.Request(
        api_url,
        headers={
            "Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/octet-stream",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DuroStudio-ReleasePromoter",
        },
        method="GET",
    )

    download_url: str | None = None
    try:
        with opener.open(req, timeout=60) as resp:
            with dest_path.open("wb") as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            download_url = e.headers.get("Location")
        else:
            err_body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Error descargando asset {asset['name']} ({e.code}): {err_body}"
            ) from e

    if download_url:
        s3_req = urllib.request.Request(
            download_url,
            headers={"User-Agent": "DuroStudio-ReleasePromoter"},
            method="GET",
        )
        with urllib.request.urlopen(s3_req, timeout=300) as resp:
            with dest_path.open("wb") as f:
                shutil.copyfileobj(resp, f, length=1024 * 1024)

    actual_size = dest_path.stat().st_size
    if expected_size and actual_size != expected_size:
        raise RuntimeError(
            f"Tamaño incorrecto en {asset['name']}: esperado {expected_size}, descargado {actual_size}"
        )


def run_gh(args: list[str]) -> None:
    env = os.environ.copy()
    env["GH_TOKEN"] = GH_TOKEN
    res = subprocess.run(
        ["gh", *args],
        env=env,
        text=True,
        capture_output=True,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Comando gh {' '.join(args)} falló ({res.returncode}):\nSTDOUT: {res.stdout}\nSTDERR: {res.stderr}"
        )


def sanitize_and_repair_workdir(
    canonical_tag: str,
    version: str,
    source_release: dict[str, Any],
    work_dir: Path,
) -> None:
    fallback_date = (
        source_release.get("published_at")
        or source_release.get("created_at")
        or now_rfc3339()
    )

    if canonical_tag in ("beta-macos", "beta-macos-intel"):
        # Eliminar paquetes sin GStreamer si existieran de builds anteriores
        for bad_name in ("Duro-Studio-Free.app.tar.gz", "Duro-Studio-Free.app.tar.gz.sig"):
            bad_file = work_dir / bad_name
            if bad_file.exists():
                print(f"  🧹 Descartando artefacto sin GStreamer: {bad_name}")
                bad_file.unlink()

        arch = "aarch64" if canonical_tag == "beta-macos" else "x86_64"
        plat_key = "darwin-aarch64" if canonical_tag == "beta-macos" else "darwin-x86_64"

        sig_candidates = sorted(work_dir.glob(f"*{arch}*.app.tar.gz.sig"))
        if not sig_candidates:
            sig_candidates = sorted(work_dir.glob("*.app.tar.gz.sig"))
        if not sig_candidates:
            raise RuntimeError(
                f"No se encontró archivo .app.tar.gz.sig válido en {canonical_tag} v{version}"
            )

        sig_path = sig_candidates[-1]
        archive_name = sig_path.name[:-4]  # quitar .sig
        archive_path = work_dir / archive_name
        if not archive_path.exists():
            raise RuntimeError(f"No existe el archivo {archive_name} junto a su firma")

        sig_content = sig_path.read_text(encoding="utf-8").strip()
        latest_path = work_dir / "latest.json"
        manifest: dict[str, Any] = {}
        if latest_path.exists():
            try:
                manifest = json.loads(latest_path.read_text(encoding="utf-8-sig"))
            except Exception:
                manifest = {}

        pub_date = str(manifest.get("pub_date") or "").strip()
        if not RFC3339_RE.match(pub_date):
            pub_date = fallback_date

        notes = manifest.get("notes") or "Actualización automática para macOS."
        platforms = manifest.get("platforms")
        if not isinstance(platforms, dict):
            platforms = {}

        platforms[plat_key] = {
            "signature": sig_content,
            "url": f"https://github.com/{TARGET_REPO}/releases/download/{canonical_tag}/{archive_name}",
        }

        repaired = {
            "version": version,
            "notes": notes,
            "pub_date": pub_date,
            "platforms": platforms,
        }
        latest_path.write_text(
            json.dumps(repaired, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  ✅ latest.json validado/reparado para {canonical_tag} ({plat_key} -> {archive_name})")
        return

    if canonical_tag == "beta-windows-xp":
        legacy_path = work_dir / "latest-legacy.json"
        if legacy_path.exists():
            manifest = json.loads(legacy_path.read_text(encoding="utf-8-sig"))
            manifest["version"] = version
            manifest["url"] = (
                f"https://github.com/{TARGET_REPO}/releases/download/beta-windows-xp/"
                "DuroStudio_Setup_Legacy-XP-Vista-7-8.exe"
            )
            if not str(manifest.get("pub_date") or "").strip():
                manifest["pub_date"] = fallback_date
            legacy_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print("  ✅ latest-legacy.json validado para beta-windows-xp")
        return

    # Resto de plataformas Tauri: beta-windows, beta-windows-x86, beta-linux, beta-pi
    latest_path = work_dir / "latest.json"
    if not latest_path.exists():
        raise RuntimeError(f"Falta latest.json en {canonical_tag} v{version}")

    manifest = json.loads(latest_path.read_text(encoding="utf-8-sig"))
    manifest["version"] = version

    pub_date = str(manifest.get("pub_date") or "").strip()
    if not RFC3339_RE.match(pub_date):
        manifest["pub_date"] = fallback_date

    sig_files = list(work_dir.glob("*.sig"))
    sig_override = sig_files[0].read_text(encoding="utf-8").strip() if len(sig_files) == 1 else None

    platforms = manifest.get("platforms", {})
    for plat_key, plat_data in platforms.items():
        if isinstance(plat_data, dict):
            url = str(plat_data.get("url") or "")
            asset_filename = url.rsplit("/", 1)[-1]
            plat_data["url"] = (
                f"https://github.com/{TARGET_REPO}/releases/download/{canonical_tag}/{asset_filename}"
            )
            if sig_override:
                plat_data["signature"] = sig_override

    latest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"  ✅ latest.json validado para {canonical_tag} v{version}")


def promote_single_release(
    source_release: dict[str, Any],
    canonical_tag: str,
    version: str,
    canonical_by_tag: dict[str, dict[str, Any]],
) -> None:
    source_tag = source_release["tag_name"]
    source_id = source_release["id"]
    cfg = PLATFORMS[canonical_tag]
    clean_title = cfg["title"](version)
    raw_body = (source_release.get("body") or "").strip()
    clean_body = raw_body if raw_body else cfg["default_body"]
    make_latest = cfg["make_latest"]

    assets = source_release.get("assets") or []
    if not assets:
        print(f"⚠️ El release {source_tag} no tiene assets; se omite.")
        return

    print(f"\n🚀 Promoviendo {source_tag} (draft={source_release.get('draft')}) -> {canonical_tag} [{clean_title}]")

    with tempfile.TemporaryDirectory(prefix=f"promote_{canonical_tag}_") as tmp:
        work_dir = Path(tmp)
        for asset in assets:
            dest = work_dir / asset["name"]
            print(f"  ⬇️ Descargando {asset['name']} ({asset.get('size', 0):,} bytes)...")
            download_asset(asset, dest)

        sanitize_and_repair_workdir(canonical_tag, version, source_release, work_dir)

        files_to_upload = sorted(str(p) for p in work_dir.iterdir() if p.is_file())
        existing_canonical = canonical_by_tag.get(canonical_tag)

        if existing_canonical is None:
            print(f"  📦 Creando release canónico nuevo: {canonical_tag}")
            run_gh(
                [
                    "release",
                    "create",
                    canonical_tag,
                    *files_to_upload,
                    "-R",
                    TARGET_REPO,
                    "--title",
                    clean_title,
                    "--notes",
                    clean_body,
                    f"--latest={make_latest}",
                ]
            )
        else:
            canonical_id = existing_canonical["id"]
            # Limpiar artefactos viejos sin GStreamer en macOS si quedaron en el release canónico
            if canonical_tag in ("beta-macos", "beta-macos-intel"):
                for old_asset in existing_canonical.get("assets") or []:
                    if old_asset["name"] in (
                        "Duro-Studio-Free.app.tar.gz",
                        "Duro-Studio-Free.app.tar.gz.sig",
                    ):
                        print(f"  🧹 Eliminando asset antiguo inválido en {canonical_tag}: {old_asset['name']}")
                        gh_api_json(
                            "DELETE",
                            f"/repos/{TARGET_REPO}/releases/assets/{old_asset['id']}",
                            allow_404=True,
                        )

            print(f"  ⬆️ Subiendo {len(files_to_upload)} archivos a {canonical_tag} (--clobber)...")
            run_gh(
                [
                    "release",
                    "upload",
                    canonical_tag,
                    *files_to_upload,
                    "-R",
                    TARGET_REPO,
                    "--clobber",
                ]
            )

            print(f"  ✏️ Actualizando metadatos de {canonical_tag} -> '{clean_title}'")
            gh_api_json(
                "PATCH",
                f"/repos/{TARGET_REPO}/releases/{canonical_id}",
                payload={
                    "name": clean_title,
                    "body": clean_body,
                    "draft": False,
                    "prerelease": False,
                    "make_latest": make_latest,
                },
            )

    if source_tag in PROTECTED_SOURCE_TAGS:
        print(f"  🛡️ Se conserva {source_tag} por compatibilidad con enlaces directos externos.")
    else:
        print(f"  🗑️ Eliminando release temporal {source_tag} (id={source_id})...")
        gh_api_json("DELETE", f"/repos/{TARGET_REPO}/releases/{source_id}", allow_404=True)
        encoded_tag = urllib.parse.quote(source_tag, safe="")
        try:
            gh_api_json(
                "DELETE",
                f"/repos/{TARGET_REPO}/git/refs/tags/{encoded_tag}",
                allow_404=True,
            )
        except Exception as e:
            print(f"  ℹ️ Nota al limpiar ref de tag {source_tag}: {e}")


def cross_sync_macos_manifests() -> None:
    """
    Asegura que beta-macos/latest.json incluya tanto darwin-aarch64 como darwin-x86_64
    cuando ambos están en la misma versión, manteniendo compatibilidad con instalaciones
    macOS Intel <= 3.0.0 cuyo endpoint apunta a beta-macos/latest.json.
    """
    macos_rel = gh_api_json("GET", f"/repos/{TARGET_REPO}/releases/tags/beta-macos", allow_404=True)
    intel_rel = gh_api_json("GET", f"/repos/{TARGET_REPO}/releases/tags/beta-macos-intel", allow_404=True)
    if not macos_rel or not intel_rel:
        return

    macos_asset = next((a for a in macos_rel.get("assets", []) if a["name"] == "latest.json"), None)
    intel_asset = next((a for a in intel_rel.get("assets", []) if a["name"] == "latest.json"), None)
    if not macos_asset or not intel_asset:
        return

    with tempfile.TemporaryDirectory(prefix="macos_sync_") as tmp:
        tmp_dir = Path(tmp)
        macos_path = tmp_dir / "macos_latest.json"
        intel_path = tmp_dir / "intel_latest.json"
        download_asset(macos_asset, macos_path)
        download_asset(intel_asset, intel_path)

        macos_json = json.loads(macos_path.read_text(encoding="utf-8-sig"))
        intel_json = json.loads(intel_path.read_text(encoding="utf-8-sig"))

        if macos_json.get("version") != intel_json.get("version"):
            return

        x86_entry = (intel_json.get("platforms") or {}).get("darwin-x86_64")
        if not x86_entry:
            return

        macos_platforms = macos_json.setdefault("platforms", {})
        if macos_platforms.get("darwin-x86_64") == x86_entry:
            print("\n✅ beta-macos/latest.json ya contiene darwin-x86_64 sincronizado.")
            return

        macos_platforms["darwin-x86_64"] = x86_entry
        merged_path = tmp_dir / "latest.json"
        merged_path.write_text(
            json.dumps(macos_json, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        run_gh(
            [
                "release",
                "upload",
                "beta-macos",
                str(merged_path),
                "-R",
                TARGET_REPO,
                "--clobber",
            ]
        )
        print(
            f"\n✅ darwin-x86_64 fusionado en beta-macos/latest.json (v{macos_json.get('version')})."
        )


WEBHOOK_WORKFLOW_YML = """name: "Sync Canonical Beta Release"

on:
  release:
    types: [published]
  workflow_dispatch:
    inputs:
      version:
        description: "Versión específica a promover (vacío = todas)"
        required: false
        default: ""

permissions:
  contents: write

concurrency:
  group: ${{ startsWith(github.event.release.tag_name, 'beta-macos') && 'sync-macos' || format('sync-{0}', github.event.release.tag_name || 'manual') }}
  cancel-in-progress: false

jobs:
  sync-release:
    if: github.event_name == 'workflow_dispatch' || contains(github.event.release.tag_name, '-v')
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Promote versioned release to canonical updater tag
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          TARGET_REPO: ${{ github.repository }}
          FILTER_TAG: ${{ github.event_name == 'release' && github.event.release.tag_name || '' }}
          FILTER_VERSION: ${{ inputs.version || '' }}
          INCLUDE_DRAFTS: ${{ github.event_name == 'workflow_dispatch' && 'true' || 'false' }}
        run: python3 .github/scripts/promote_beta_releases.py
"""


def upsert_repo_file(repo_path: str, content_bytes: bytes, message: str) -> None:
    existing = gh_api_json(
        "GET",
        f"/repos/{TARGET_REPO}/contents/{repo_path}",
        allow_404=True,
    )
    b64_new = base64.b64encode(content_bytes).decode("ascii")
    if existing and existing.get("content"):
        existing_bytes = base64.b64decode(existing["content"])
        if existing_bytes == content_bytes:
            print(f"  ✓ {repo_path} ya está actualizado en {TARGET_REPO}.")
            return

    payload: dict[str, Any] = {
        "message": message,
        "content": b64_new,
    }
    if existing and existing.get("sha"):
        payload["sha"] = existing["sha"]

    gh_api_json("PUT", f"/repos/{TARGET_REPO}/contents/{repo_path}", payload=payload)
    print(f"  ✅ {repo_path} instalado/actualizado en {TARGET_REPO}.")


def install_webhook_workflow() -> None:
    print(f"\n🔧 Verificando workflow automático en {TARGET_REPO}...")
    script_bytes = Path(__file__).read_bytes()
    try:
        upsert_repo_file(
            ".github/scripts/promote_beta_releases.py",
            script_bytes,
            "ci(updater): sync release promotion script",
        )
        upsert_repo_file(
            ".github/workflows/sync-canonical-release.yml",
            WEBHOOK_WORKFLOW_YML.encode("utf-8"),
            "ci(updater): auto-promote published draft releases to canonical tags",
        )
    except Exception as e:
        print(
            f"⚠️ No se pudo instalar el workflow automático directamente en {TARGET_REPO}: {e}\n"
            "   (Puedes publicar borradores usando el workflow 'Publicar Releases (Drafts)' en Duro-Studio "
            "o agregar permiso 'workflow' a BETA_REPO_TOKEN)."
        )


def main() -> int:
    if not GH_TOKEN:
        print("❌ Error: GH_TOKEN / GITHUB_TOKEN no está configurado.", file=sys.stderr)
        return 1

    install_hook = "--install-webhook-workflow" in sys.argv

    releases = list_all_releases()
    canonical_by_tag: dict[str, dict[str, Any]] = {}
    candidates: list[tuple[tuple[int, ...], str, str, dict[str, Any]]] = []

    for rel in releases:
        tag = rel.get("tag_name") or ""
        is_draft = bool(rel.get("draft"))

        if tag in PLATFORMS and not is_draft:
            canonical_by_tag[tag] = rel
            continue

        m = TAG_RE.match(tag)
        if not m:
            continue

        canonical_tag, ver = m.group(1), m.group(2)
        if is_draft and not INCLUDE_DRAFTS:
            continue
        if FILTER_TAG and tag != FILTER_TAG:
            continue
        if FILTER_VERSION and ver != FILTER_VERSION:
            continue

        candidates.append((parse_semver_key(ver), canonical_tag, ver, rel))

    # Filtrar tags protegidos que ya fueron promovidos previamente
    filtered_candidates: list[tuple[tuple[int, ...], str, str, dict[str, Any]]] = []
    for sem_key, canonical_tag, ver, rel in candidates:
        tag = rel.get("tag_name") or ""
        if tag in PROTECTED_SOURCE_TAGS and canonical_tag in canonical_by_tag:
            existing_name = (canonical_by_tag[canonical_tag].get("name") or "").split(" ")[0].lstrip("v")
            if parse_semver_key(existing_name) >= sem_key:
                print(
                    f"⏭️ {canonical_tag} ya está en v{existing_name} (>= v{ver}); se omite tag protegido {tag}."
                )
                continue
        filtered_candidates.append((sem_key, canonical_tag, ver, rel))

    candidates = filtered_candidates
    candidates.sort(key=lambda item: (item[0], item[1]))

    if not candidates:
        print("ℹ️ No se encontraron releases versionados/borradores pendientes para promover.")
    else:
        print(f"📋 Se encontraron {len(candidates)} release(s) para promover a tags canónicos.")
        for _, canonical_tag, ver, rel in candidates:
            promote_single_release(rel, canonical_tag, ver, canonical_by_tag)
            # Refrescar referencia canónica por si hubiera más de una versión
            updated_rel = gh_api_json(
                "GET",
                f"/repos/{TARGET_REPO}/releases/tags/{canonical_tag}",
                allow_404=True,
            )
            if updated_rel:
                canonical_by_tag[canonical_tag] = updated_rel

    cross_sync_macos_manifests()

    if install_hook:
        install_webhook_workflow()

    print("\n🎉 Sincronización de releases finalizada con éxito.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
