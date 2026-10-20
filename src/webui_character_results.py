"""Verify delivery manifests and project character results into public fields."""
from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
import re
from typing import TypedDict

from character_runtime.character_material_options import parse_character_material_options
from character_runtime.character_appearance_options import (
    parse_character_appearance_options, verify_character_appearance_report,
)
from webui_character_contract import json_object, required_text


class CharacterDeliveryFile(TypedDict):
    relative_path: str
    role: str
    bytes: int
    sha256: str


DELIVERY_EXTENSIONS = {
    "character": frozenset({".blend", ".glb", ".gltf", ".fbx"}),
    "animation": frozenset({".blend", ".fbx", ".bvh"}),
    "expressions": frozenset({".json"}),
    "texture": frozenset({".png", ".jpg", ".jpeg", ".tga", ".exr", ".dds", ".hdr", ".ktx2"}),
    "preview": frozenset({".png", ".jpg", ".jpeg", ".webp"}),
    "report": frozenset({".json", ".txt"}),
}


def build_character_result(output_dir: Path, manifest: dict[str, object], kind: str) -> dict[str, object]:
    """Hash real files inside the owned output and never publish source receipts."""
    root = output_dir.resolve(strict=True)
    status = manifest.get("status")
    if status not in {"completed", "partial"}:
        raise ValueError("Character manifest requires completed or partial status")
    rows = manifest.get("files")
    if not isinstance(rows, list) or (kind == "export" and not rows):
        raise ValueError("Character export manifest requires delivery files")
    files: list[CharacterDeliveryFile] = []
    seen: set[str] = set()
    for index, value in enumerate(rows):
        row = json_object(value, f"files[{index}]")
        relative = required_text(row.get("relative_path"), "relative_path")
        windows = PureWindowsPath(relative)
        path = Path(relative.replace("\\", "/"))
        if path.is_absolute() or windows.drive or windows.root or ".." in path.parts or ":" in relative:
            raise ValueError("Delivery paths must remain relative to the job output")
        resolved = (root / path).resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError("Delivery file escapes the job output or is not a file")
        normalized = path.as_posix()
        if normalized in seen:
            raise ValueError("Duplicate character delivery file")
        seen.add(normalized)
        role = required_text(row.get("role"), "role")
        if role not in DELIVERY_EXTENSIONS:
            raise ValueError("Unsupported character delivery role")
        if resolved.suffix.casefold() not in DELIVERY_EXTENSIONS[role] or normalized in {"request.json", "character_result.json"}:
            raise ValueError("Character manifest lists an internal or unsupported delivery file")
        size = row.get("bytes")
        digest = row.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Delivery files require byte counts and lowercase SHA-256")
        with resolved.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if size != resolved.stat().st_size or actual != digest:
            raise ValueError("Character delivery size or SHA-256 does not match the file")
        files.append(CharacterDeliveryFile(relative_path=normalized, role=role, bytes=size, sha256=digest))
    pending_values = manifest.get("pending", [])
    if not isinstance(pending_values, list):
        raise ValueError("Character pending items must be an array")
    pending: list[dict[str, str]] = []
    for value in pending_values:
        row = json_object(value, "pending")
        pending.append({"code": required_text(row.get("code"), "pending.code"),
                        "message": required_text(row.get("message"), "pending.message")})
    if pending and status == "completed":
        raise ValueError("Character manifest with pending items must report partial status")
    result: dict[str, object] = {
        "format": "CharacterPreparationResult/1" if kind == "preparation" else "CharacterExportResult/1",
        "status": status, "output_directory": str(root), "files": files, "pending": pending,
    }
    if "material_options" in manifest:
        if kind != "export":
            raise ValueError("Material export options are valid only for character export results")
        result["material_options"] = parse_character_material_options(manifest["material_options"]).as_json()
    if "appearance_options" in manifest or "appearance_report" in manifest:
        if kind != "export" or "appearance_options" not in manifest or "appearance_report" not in manifest:
            raise ValueError("Appearance settings and their completed report are valid only together on character exports")
        appearance = parse_character_appearance_options(manifest["appearance_options"])
        result["appearance_options"] = appearance.as_json()
        result["appearance_report"] = verify_character_appearance_report(
            appearance, manifest["appearance_report"])
    return result
