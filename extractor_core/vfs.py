from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable


DEFAULT_VFS_KEY_HEX = (
    "e95b317ac4f828569d23a86bf271dcb5"
    "3e846fa75c924d671dba8e38f4ca52e1"
)
GROUP_HASH = "7064D8E2"
BLOCK_HEAD_LEN = 12
COPY_CHUNK = 8 * 1024 * 1024


class ProbeError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, object] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics


class Reader:
    def __init__(self, data: bytes, offset: int = 0):
        self.data = data
        self.pos = offset

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise ProbeError(
                f"BLC ended unexpectedly at 0x{self.pos:X}; requested {size} bytes"
            )
        value = self.data[self.pos : self.pos + size]
        self.pos += size
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def i16(self) -> int:
        return struct.unpack("<h", self.take(2))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def utf8(self) -> str:
        size = self.i16()
        if size < 0 or size > 32767:
            raise ProbeError(f"Invalid UTF-8 field length {size} at 0x{self.pos - 2:X}")
        return self.take(size).decode("utf-8", errors="strict")


@dataclass
class VfsFile:
    file_name: str
    file_name_hash: str
    file_chunk_md5_name: str
    file_data_md5: str
    offset: int
    length: int
    block_type: int
    use_encrypt: bool
    iv_seed: int | None


@dataclass
class VfsChunk:
    md5_name: str
    content_md5: str
    length: int
    block_type: int
    files: list[VfsFile] = field(default_factory=list)


@dataclass
class VfsGroup:
    version: int
    group_name: str
    group_hash: str
    file_count: int
    chunks_length: int
    block_type: int
    chunks: list[VfsChunk] = field(default_factory=list)

    def iter_files(self) -> Iterable[VfsFile]:
        for chunk in self.chunks:
            yield from chunk.files


def hex_be(value: bytes) -> str:
    return value.hex().upper()


def make_cipher(key: bytes, nonce: bytes):
    try:
        from Crypto.Cipher import ChaCha20
    except ImportError as exc:
        raise ProbeError(
            "Missing pycryptodome. Run: python -m pip install -r requirements.txt"
        ) from exc
    cipher = ChaCha20.new(key=key, nonce=nonce)
    cipher.seek(64)
    return cipher


def decrypt_blc(raw: bytes, key: bytes) -> bytes:
    if len(raw) <= BLOCK_HEAD_LEN:
        raise ProbeError("BLC is too short")
    if len(key) != 32:
        raise ProbeError("VFS ChaCha20 key must be exactly 32 bytes")
    version = struct.unpack("<I", raw[:4])[0]
    if version <= 0 or version > 100:
        raise ProbeError(f"Unexpected VFS version {version}")
    cipher = make_cipher(key, raw[:BLOCK_HEAD_LEN])
    return raw[:BLOCK_HEAD_LEN] + cipher.decrypt(raw[BLOCK_HEAD_LEN:])


def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = collections.Counter(data)
    length = len(data)
    return -sum(
        (count / length) * math.log2(count / length) for count in counts.values()
    )


def analyze_decrypted_head(decrypted: bytes, key: bytes) -> dict[str, object]:
    sample = decrypted[BLOCK_HEAD_LEN : BLOCK_HEAD_LEN + 1024]
    preview = sample[:256]
    printable = sum(1 for value in sample if value in (9, 10, 13) or 32 <= value < 127)
    first_utf8: str | None = None
    first_utf8_error: str | None = None
    try:
        first_utf8 = Reader(decrypted, BLOCK_HEAD_LEN).utf8()
    except Exception as exc:
        first_utf8_error = f"{type(exc).__name__}: {exc}"
    markers = {}
    for marker in (b"Bundle", b"Data/", b"Windows", b"UnityFS", b"7064D8E2"):
        position = decrypted.find(marker, BLOCK_HEAD_LEN, BLOCK_HEAD_LEN + 8192)
        markers[marker.decode("ascii")] = position if position >= 0 else None
    return {
        "key_sha256": hashlib.sha256(key).hexdigest(),
        "version": struct.unpack("<I", decrypted[:4])[0],
        "first_utf8": first_utf8,
        "first_utf8_error": first_utf8_error,
        "sample_size": len(sample),
        "printable_ratio": round(printable / len(sample), 6) if sample else 0.0,
        "shannon_entropy": round(shannon_entropy(sample), 6),
        "known_marker_offsets": markers,
        "preview_hex": preview.hex(" ").upper(),
        "preview_ascii": "".join(
            chr(value) if 32 <= value < 127 else "." for value in preview
        ),
    }


def locate_group_header(
    data: bytes,
    search_start: int,
    expected_chunk_count: int,
    expected_chunks_length: int,
    expected_block_type: int,
) -> tuple[int, int, int, int]:
    search_end = min(len(data) - 5, search_start + 8192)
    candidates: list[tuple[int, int, int, int, int]] = []
    nearby_patterns: list[str] = []
    for block_pos in range(search_start + 12, search_end):
        if data[block_pos] != expected_block_type:
            continue
        chunk_count = struct.unpack_from("<i", data, block_pos + 1)[0]
        if 0 <= chunk_count <= 4096 and len(nearby_patterns) < 12:
            nearby_patterns.append(f"offset=0x{block_pos:X}, chunk_count={chunk_count}")
        if chunk_count != expected_chunk_count:
            continue
        file_count = struct.unpack_from("<i", data, block_pos - 12)[0]
        chunks_length = struct.unpack_from("<q", data, block_pos - 8)[0]
        if file_count < chunk_count or file_count > 10_000_000:
            continue
        if chunks_length <= 0:
            continue
        difference = abs(chunks_length - expected_chunks_length)
        candidates.append(
            (block_pos, difference, file_count, chunks_length, chunk_count)
        )
    if not candidates:
        detail = "; ".join(nearby_patterns) if nearby_patterns else "none"
        raise ProbeError(
            "Could not locate the formal-release group header using the Bundle type "
            f"and CHK count (expected chunks={expected_chunk_count}); nearby type-"
            f"{expected_block_type} patterns: {detail}"
        )
    candidates.sort()
    # A chunk header can coincidentally have the same type/count pattern. The group
    # header is the earliest exact-size candidate after the group name.
    block_pos, _, file_count, chunks_length, chunk_count = candidates[0]
    return block_pos, file_count, chunks_length, chunk_count


def parse_blc(
    decrypted: bytes,
    *,
    expected_group_hash: str | None = None,
    expected_chunk_count: int | None = None,
    expected_chunks_length: int | None = None,
    expected_block_type: int | None = None,
) -> VfsGroup:
    version = struct.unpack("<I", decrypted[:4])[0]
    # Formal-release catalogs share an eight-byte preamble before the UTF-8
    # group name.  The old probe recognized only the literal ``Bundle`` group,
    # but the same layout is used by Terrain, Streaming, BundleManifest, etc.
    formal_release_layout = False
    if len(decrypted) > BLOCK_HEAD_LEN + 12:
        marker = struct.unpack_from("<I", decrypted, BLOCK_HEAD_LEN)[0]
        name_length = struct.unpack_from("<H", decrypted, BLOCK_HEAD_LEN + 8)[0]
        name_start = BLOCK_HEAD_LEN + 10
        name_end = name_start + name_length
        if marker == 4 and 0 < name_length <= 128 and name_end <= len(decrypted):
            try:
                decoded_name = decrypted[name_start:name_end].decode("utf-8")
                formal_release_layout = bool(decoded_name and decoded_name.isprintable())
            except UnicodeDecodeError:
                formal_release_layout = False
    r = Reader(decrypted, BLOCK_HEAD_LEN + 8 if formal_release_layout else BLOCK_HEAD_LEN)
    group_name = r.utf8()
    if not group_name or len(group_name) > 128:
        raise ProbeError(f"Implausible group name: {group_name!r}")

    adaptive = not formal_release_layout and all(
        value is not None
        for value in (
            expected_group_hash,
            expected_chunk_count,
            expected_chunks_length,
            expected_block_type,
        )
    )
    if formal_release_layout:
        group_hash_raw = r.take(8)
        file_count = r.i32()
        chunks_length = r.i64()
        block_type = r.u8()
        chunk_count = r.i32()
        group_hash = hex_be(group_hash_raw[:4])
    elif adaptive:
        block_pos, file_count, chunks_length, chunk_count = locate_group_header(
            decrypted,
            r.pos,
            int(expected_chunk_count),
            int(expected_chunks_length),
            int(expected_block_type),
        )
        block_type = decrypted[block_pos]
        group_hash = str(expected_group_hash).upper()
        r.pos = block_pos + 5
    else:
        group_hash_raw = r.take(8)
        file_count = r.i32()
        chunks_length = r.i64()
        block_type = r.u8()
        chunk_count = r.i32()
        group_hash = hex_be(group_hash_raw[:4])

    if file_count < 0 or file_count > 10_000_000:
        raise ProbeError(f"Implausible file count: {file_count}")
    if chunk_count < 0 or chunk_count > 100_000:
        raise ProbeError(f"Implausible chunk count: {chunk_count}")

    chunks: list[VfsChunk] = []
    parsed_files = 0
    for _ in range(chunk_count):
        md5_name = hex_be(r.take(16))
        content_md5 = hex_be(r.take(16))
        chunk_length = r.i64()
        chunk_block_type = r.u8()
        if formal_release_layout:
            r.take(4)
        item_count = r.i32()
        if item_count < 0 or item_count > 10_000_000:
            raise ProbeError(f"Implausible file count in chunk: {item_count}")
        items: list[VfsFile] = []
        for _ in range(item_count):
            file_name = r.utf8()
            file_name_hash = hex_be(r.take(8))
            file_chunk_md5_name = hex_be(r.take(16))
            file_data_md5 = hex_be(r.take(16))
            offset = r.i64()
            length = r.i64()
            item_block_type = r.u8()
            use_encrypt = bool(r.u8())
            iv_seed = r.i64() if use_encrypt else None
            # Formal release records append four reserved bytes after the
            # optional IV.  Reading them before the IV keeps record alignment
            # intact but silently produces a bogus nonce (commonly 2**32).
            if formal_release_layout:
                r.take(4)
            if offset < 0 or length < 0 or offset + length > chunk_length:
                raise ProbeError(
                    f"Invalid range for {file_name!r}: offset={offset}, length={length}, "
                    f"chunk_length={chunk_length}"
                )
            items.append(
                VfsFile(
                    file_name=file_name,
                    file_name_hash=file_name_hash,
                    file_chunk_md5_name=file_chunk_md5_name,
                    file_data_md5=file_data_md5,
                    offset=offset,
                    length=length,
                    block_type=item_block_type,
                    use_encrypt=use_encrypt,
                    iv_seed=iv_seed,
                )
            )
        parsed_files += len(items)
        chunks.append(
            VfsChunk(
                md5_name=md5_name,
                content_md5=content_md5,
                length=chunk_length,
                block_type=chunk_block_type,
                files=items,
            )
        )

    if parsed_files != file_count:
        raise ProbeError(
            f"BLC count mismatch: header={file_count}, parsed={parsed_files}"
        )
    if r.pos != len(decrypted):
        tail = len(decrypted) - r.pos
        formal_footer = formal_release_layout and 0 < tail <= 16
        if not formal_footer and (tail > 16 or any(decrypted[r.pos:])):
            raise ProbeError(f"BLC has {tail} unexpected trailing bytes")

    return VfsGroup(
        version=version,
        group_name=group_name,
        group_hash=group_hash,
        file_count=file_count,
        chunks_length=chunks_length,
        block_type=block_type,
        chunks=chunks,
    )


def load_candidate_keys(args: argparse.Namespace) -> list[bytes]:
    candidates: list[str] = []
    if args.vfs_key_hex:
        candidates.append(args.vfs_key_hex)
    if args.vfs_key_file:
        for line in args.vfs_key_file.read_text(encoding="utf-8-sig").splitlines():
            value = line.split("#", 1)[0].strip()
            if value:
                candidates.append(value)
    if not candidates:
        candidates.append(DEFAULT_VFS_KEY_HEX)
    keys: list[bytes] = []
    for value in candidates:
        compact = re.sub(r"[^0-9a-fA-F]", "", value)
        if len(compact) != 64:
            raise ProbeError(
                f"Invalid VFS key {value!r}; expected 64 hexadecimal characters"
            )
        key = bytes.fromhex(compact)
        if key not in keys:
            keys.append(key)
    return keys


def decrypt_and_parse_blc(path: Path, keys: list[bytes]) -> tuple[VfsGroup, bytes]:
    raw = path.read_bytes()
    chunk_paths = list(path.parent.glob("*.chk"))
    expected_chunk_count = len(chunk_paths)
    expected_chunks_length = sum(item.stat().st_size for item in chunk_paths)
    failures: list[str] = []
    diagnostics: list[dict[str, object]] = []
    for key in keys:
        decrypted = decrypt_blc(raw, key)
        diagnostics.append(analyze_decrypted_head(decrypted, key))
        try:
            group = parse_blc(
                decrypted,
                expected_group_hash=path.parent.name,
                expected_chunk_count=expected_chunk_count,
                expected_chunks_length=expected_chunks_length,
                expected_block_type=11,
            )
            if group.group_hash.upper() != path.parent.name.upper():
                raise ProbeError(
                    f"Group hash mismatch: {group.group_hash} != {path.parent.name}"
                )
            parsed_chunk_names = {chunk.md5_name.upper() for chunk in group.chunks}
            disk_chunk_names = {item.stem.upper() for item in chunk_paths}
            if parsed_chunk_names != disk_chunk_names:
                missing = sorted(disk_chunk_names - parsed_chunk_names)[:3]
                extra = sorted(parsed_chunk_names - disk_chunk_names)[:3]
                raise ProbeError(
                    "Parsed CHK names do not match the files on disk; "
                    f"missing={missing}, extra={extra}"
                )
            parsed_chunks_length = sum(chunk.length for chunk in group.chunks)
            if parsed_chunks_length != group.chunks_length:
                raise ProbeError(
                    "Parsed chunk lengths do not match the BLC group total: "
                    f"parsed={parsed_chunks_length}, header={group.chunks_length}"
                )
            return group, key
        except Exception as exc:
            failures.append(f"{key.hex()[:12]}...: {exc}")
    raise ProbeError(
        "None of the supplied VFS keys produced a valid BLC.\n  "
        + "\n  ".join(failures),
        diagnostics={
            "vfs_key_probes": diagnostics,
            "expected_chunk_count": expected_chunk_count,
            "disk_chunks_length": expected_chunks_length,
        },
    )


def choose_file(
    group: VfsGroup,
    pattern: re.Pattern[str],
    max_size: int,
) -> tuple[VfsChunk, VfsFile]:
    preferred: list[tuple[int, int, VfsChunk, VfsFile]] = []
    fallback: list[tuple[int, VfsChunk, VfsFile]] = []
    negative = re.compile(r"shader|character|avatar|ui/|audio|video", re.I)
    for chunk in group.chunks:
        for item in chunk.files:
            if item.length <= 0 or item.length > max_size:
                continue
            name = item.file_name.replace("\\", "/")
            fallback.append((item.length, chunk, item))
            match = pattern.search(name)
            if match and not negative.search(name):
                preferred.append((match.start(), item.length, chunk, item))
    if preferred:
        preferred.sort(key=lambda row: (row[0], row[1], row[3].file_name.lower()))
        return preferred[0][2], preferred[0][3]
    if fallback:
        fallback.sort(key=lambda row: (row[0], row[2].file_name.lower()))
        return fallback[0][1], fallback[0][2]
    raise ProbeError(
        "No virtual file fits the requested size limit. Increase --max-bundle-mb."
    )


def safe_name(name: str, fallback: str) -> str:
    base = Path(name.replace("\\", "/")).name or fallback
    base = re.sub(r"[^0-9A-Za-z._-]+", "_", base).strip("._")
    return base[:160] or fallback


def ensure_output_is_safe(game_root: Path, output_root: Path) -> None:
    game = game_root.resolve()
    output = output_root.resolve()
    if output == game or game in output.parents:
        raise ProbeError("Output directory must not be inside the game installation")
    output.mkdir(parents=True, exist_ok=True)


def copy_virtual_file(
    source: BinaryIO,
    target: BinaryIO,
    item: VfsFile,
    version: int,
    vfs_key: bytes,
) -> str:
    source.seek(item.offset)
    remaining = item.length
    digest = hashlib.md5()
    cipher = None
    if item.use_encrypt:
        if item.iv_seed is None:
            raise ProbeError("Encrypted item is missing iv_seed")
        nonce = struct.pack("<I", version) + struct.pack("<q", item.iv_seed)
        cipher = make_cipher(vfs_key, nonce)
    while remaining:
        block = source.read(min(COPY_CHUNK, remaining))
        if not block:
            raise ProbeError("CHK ended before the virtual file was fully read")
        if cipher:
            block = cipher.decrypt(block)
        target.write(block)
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest().upper()


def extract_one_bundle(
    vfs_dir: Path,
    chunk: VfsChunk,
    item: VfsFile,
    output_dir: Path,
    version: int,
    vfs_key: bytes,
) -> tuple[Path, str]:
    chunk_name = item.file_chunk_md5_name or chunk.md5_name
    chunk_path = vfs_dir / f"{chunk_name}.chk"
    if not chunk_path.is_file():
        alternate = vfs_dir / f"{chunk.md5_name}.chk"
        if alternate.is_file():
            chunk_path = alternate
        else:
            raise ProbeError(f"CHK file not found: {chunk_path}")
    bundle_dir = output_dir / "bundle"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_name(item.file_name, f"{item.file_data_md5}.ab")
    if not Path(filename).suffix:
        filename += ".ab"
    target = bundle_dir / filename
    with chunk_path.open("rb") as source, target.open("wb") as dest:
        digest = copy_virtual_file(source, dest, item, version, vfs_key)
    return target, digest


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def inventory_object(obj) -> dict[str, object]:
    try:
        name = obj.peek_name()
    except Exception:
        name = ""
    return {
        "path_id": int(getattr(obj, "path_id", 0)),
        "type": getattr(getattr(obj, "type", None), "name", "Unknown"),
        "name": name,
        "byte_size": int(getattr(obj, "byte_size", 0)),
    }


def export_unity_objects(
    bundle_path: Path,
    output_dir: Path,
    max_meshes: int,
    max_textures: int,
    bundle_key: str | None,
) -> dict[str, object]:
    try:
        import UnityPy
        import UnityPy.config
    except ImportError as exc:
        raise ProbeError(
            "Missing UnityPy. Run: python -m pip install -r requirements.txt"
        ) from exc

    if bundle_key:
        UnityPy.set_assetbundle_decrypt_key(bundle_key)
    UnityPy.config.FALLBACK_UNITY_VERSION = "2021.3.34f5"
    env = UnityPy.load(str(bundle_path))
    objects = list(env.objects)
    inventory = [inventory_object(obj) for obj in objects]
    write_json(output_dir / "object_inventory.json", inventory)

    mesh_dir = output_dir / "meshes"
    texture_dir = output_dir / "textures"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    texture_dir.mkdir(parents=True, exist_ok=True)
    mesh_count = 0
    texture_count = 0
    errors: list[str] = []
    used_names: dict[str, int] = {}

    def unique(base: str, suffix: str) -> Path:
        clean = safe_name(base, "unnamed")
        key = clean.lower()
        number = used_names.get(key, 0)
        used_names[key] = number + 1
        if number:
            clean = f"{clean}_{number}"
        return Path(clean + suffix)

    for obj in objects:
        type_name = getattr(getattr(obj, "type", None), "name", "")
        if type_name == "Mesh" and mesh_count < max_meshes:
            try:
                data = obj.parse_as_object()
                name = getattr(data, "m_Name", "") or f"mesh_{obj.path_id}"
                target = mesh_dir / unique(name, ".obj")
                target.write_text(data.export(), encoding="utf-8", newline="")
                mesh_count += 1
            except Exception as exc:
                errors.append(f"Mesh path_id={obj.path_id}: {exc}")
        elif type_name == "Texture2D" and texture_count < max_textures:
            try:
                data = obj.parse_as_object()
                name = getattr(data, "m_Name", "") or f"texture_{obj.path_id}"
                target = texture_dir / unique(name, ".png")
                data.image.save(target)
                texture_count += 1
            except Exception as exc:
                errors.append(f"Texture2D path_id={obj.path_id}: {exc}")
        if mesh_count >= max_meshes and texture_count >= max_textures:
            break

    return {
        "object_count": len(objects),
        "mesh_count": mesh_count,
        "texture_count": texture_count,
        "errors": errors,
    }


def run_blender(blender: Path, package_dir: Path, output_dir: Path) -> Path:
    script = Path(__file__).with_name("blender_build_probe.py")
    target = output_dir / "Endfield_Small_Probe.blend"
    command = [
        str(blender),
        "--background",
        "--factory-startup",
        "--python",
        str(script),
        "--",
        "--input-dir",
        str(package_dir / "meshes"),
        "--output",
        str(target),
    ]
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0 or not target.is_file():
        raise ProbeError(f"Blender failed with exit code {completed.returncode}")
    return target


def bundle_header(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        head = handle.read(256)
    printable = "".join(chr(x) if 32 <= x < 127 else "." for x in head)
    return {"hex": head.hex(" ").upper(), "ascii": printable}


def run(args: argparse.Namespace) -> int:
    game_root = args.game_root.resolve()
    output_root = args.output.resolve()
    ensure_output_is_safe(game_root, output_root)
    report: dict[str, object] = {
        "status": "started",
        "game_root": str(game_root),
        "output": str(output_root),
        "read_only_game_access": True,
    }
    report_path = output_root / "probe_report.json"
    try:
        data_root = game_root / "Endfield_Data" / "StreamingAssets"
        vfs_dir = data_root / "VFS" / GROUP_HASH
        blc_path = vfs_dir / f"{GROUP_HASH}.blc"
        required = [game_root / "Endfield.exe", data_root / "VFS", blc_path]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ProbeError("Required paths are missing:\n  " + "\n  ".join(missing))

        keys = load_candidate_keys(args)
        group, active_key = decrypt_and_parse_blc(blc_path, keys)
        report["vfs"] = {
            "group": group.group_name,
            "hash": group.group_hash,
            "version": group.version,
            "file_count": group.file_count,
            "chunk_count": len(group.chunks),
            "accepted_key_sha256": hashlib.sha256(active_key).hexdigest(),
        }
        write_json(output_root / "vfs_7064D8E2_index.json", asdict(group))

        pattern = re.compile(args.match, re.I)
        chunk, item = choose_file(
            group, pattern, int(args.max_bundle_mb * 1024 * 1024)
        )
        report["selected"] = asdict(item)
        bundle_path, digest = extract_one_bundle(
            vfs_dir, chunk, item, output_root, group.version, active_key
        )
        report["bundle"] = {
            "path": str(bundle_path),
            "size": bundle_path.stat().st_size,
            "computed_md5": digest,
            "expected_md5": item.file_data_md5,
            "md5_matches": digest == item.file_data_md5,
            "header": bundle_header(bundle_path),
        }

        if args.vfs_only:
            report["status"] = "vfs_extracted"
            write_json(report_path, report)
            print(f"VFS extraction complete: {bundle_path}")
            return 0

        unity_dir = output_root / "unity_export"
        unity_dir.mkdir(parents=True, exist_ok=True)
        try:
            unity_result = export_unity_objects(
                bundle_path,
                unity_dir,
                args.max_meshes,
                args.max_textures,
                args.bundle_key,
            )
            report["unity"] = unity_result
        except Exception as exc:
            report["status"] = "bundle_needs_adapter"
            report["unity_error"] = f"{type(exc).__name__}: {exc}"
            report["unity_traceback"] = traceback.format_exc()
            write_json(report_path, report)
            print(
                "The VFS layer was extracted, but the customized AssetBundle could not "
                f"yet be parsed. See {report_path}",
                file=sys.stderr,
            )
            return 2

        if args.blender:
            blend_path = run_blender(args.blender.resolve(), unity_dir, output_root)
            report["blend_file"] = str(blend_path)
        report["status"] = "complete"
        write_json(report_path, report)
        print(f"Small extraction experiment complete. Report: {report_path}")
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        diagnostics = getattr(exc, "diagnostics", None)
        if diagnostics is not None:
            report["diagnostics"] = diagnostics
        report["traceback"] = traceback.format_exc()
        write_json(report_path, report)
        print(f"Experiment stopped safely: {exc}\nReport: {report_path}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only, one-bundle Endfield extraction experiment"
    )
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--match",
        default=r"wuling|wu.?ling|武陵|valley.?iv|valley|四号谷地|map|scene|level",
        help="Regular expression used to prefer a small map-like virtual file",
    )
    parser.add_argument("--max-bundle-mb", type=float, default=128.0)
    parser.add_argument("--max-meshes", type=int, default=20)
    parser.add_argument("--max-textures", type=int, default=20)
    parser.add_argument("--vfs-key-hex")
    parser.add_argument("--vfs-key-file", type=Path)
    parser.add_argument(
        "--bundle-key",
        help="Optional value passed to UnityPy.set_assetbundle_decrypt_key",
    )
    parser.add_argument("--blender", type=Path)
    parser.add_argument(
        "--vfs-only",
        action="store_true",
        help="Stop after extracting one virtual AssetBundle",
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))

