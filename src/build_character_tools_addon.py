"""Package the shared Blender controllers as a dependency-free extension ZIP."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import tomllib
import zipfile


RUNTIME_FILES = ('character_rig_ik.py', 'character_rig_panel.py', 'character_weather_panel.py',
                 'character_postprocess.py', 'character_postprocess_panel.py',
                 'character_outline.py', 'character_appearance_panel.py',
                 'character_shader_uniforms.py')


def build_addon(repository: Path, output_directory: Path) -> Path:
    """Copy an explicit code-only closure, preserving a single source of truth."""
    package = repository / 'src' / 'blender_character_tools'
    manifest_bytes = (package / 'blender_manifest.toml').read_bytes()
    manifest = tomllib.loads(manifest_bytes.decode('utf-8'))
    identifier, version = manifest['id'], manifest['version']
    if not isinstance(identifier, str) or not identifier.isidentifier():
        raise ValueError(f'Invalid Blender extension identifier: {identifier!r}')
    if not isinstance(version, str) or len(version.split('.')) != 3 or not all(
        part.isdigit() for part in version.split('.')
    ):
        raise ValueError(f'Invalid Blender extension version: {version!r}')
    inputs = {'__init__.py': package / '__init__.py', 'LICENSE': repository / 'LICENSE',
              **{name: repository / 'src' / 'character_runtime' / name for name in RUNTIME_FILES}}
    payload = {name: path.read_bytes() for name, path in inputs.items()}
    payload['blender_manifest.toml'] = manifest_bytes
    for name, data in payload.items():
        if name.endswith('.py'):
            ast.parse(data.decode('utf-8'), filename=name)
    output_directory.mkdir(parents=True, exist_ok=True)
    target = output_directory / f'{identifier}-{version}.zip'
    temporary = target.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(payload.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    temporary.replace(target)
    return target


def main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    args = parser.parse_args(arguments)
    print(build_addon(Path(__file__).resolve().parents[1], args.output_dir.resolve()))


if __name__ == '__main__':
    import sys
    main(sys.argv[1:])
