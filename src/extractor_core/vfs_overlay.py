"""Resolve the installed VFS with the game's Persistent update layer."""
from __future__ import annotations

from pathlib import Path


def vfs_source_roots(vfs_root: Path) -> tuple[Path, ...]:
    """Return storage layers in precedence order without modifying either layer."""
    root = vfs_root.expanduser().resolve()
    if root.name.casefold() == 'vfs' and root.parent.name.casefold() == 'streamingassets':
        update = root.parent.parent / 'Persistent' / 'VFS'
        if update.is_dir():
            return update, root
    return (root,)


def resolve_vfs_file(vfs_root: Path, group_hash: str, filename: str) -> Path:
    """Select an update file when present; otherwise use its installed base file."""
    candidates = tuple(root / group_hash / filename for root in vfs_source_roots(vfs_root))
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError(f'VFS file is missing from all installed layers: candidates={candidates}')


def vfs_group_sources(vfs_root: Path, group_hash: str) -> tuple[Path, ...]:
    """List the active catalog and available chunk storage for one group."""
    catalog = resolve_vfs_file(vfs_root, group_hash, f'{group_hash}.blc')
    chunks: dict[str, Path] = {}
    for root in vfs_source_roots(vfs_root):
        for path in (root / group_hash).glob('*.chk'):
            chunks.setdefault(path.name.casefold(), path)
    return (catalog, *(chunks[key] for key in sorted(chunks)))
