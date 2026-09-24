# First-run bootstrap contract

The public source tree does not contain extracted game data. A user supplies only:

- the read-only directory that directly contains `Endfield.exe`;
- an external writable export root;
- an optional external cache root;
- a local Blender 4.4 or newer executable for scene export (optional for data packages).

`launch_webui.py` now starts the WebUI first by default. When these values are not configured, choose them in the WebUI setup panel, validate them, and click the required-files extraction action. The launcher does not run `endfield_first_run.py` before the page opens. The same process remains available as a standalone CLI and through the legacy `--prepare-before-webui` developer mode.

```powershell
.\launch_first_run.ps1 `
  --game-root 'X:\Games\Endfield Game' `
  --export-root 'X:\Endfield_Map_Exports' `
  --cache-root 'X:\Endfield_Map_Cache' `
  --run-name bootstrap_v1
```

The coordinator automatically creates, outside the game installation:

- extracted VFS bootstrap metadata;
- a searchable resource SQLite database;
- a BundleScanner TSV index derived from that database;
- complete Map01 and Map02 instance SQLite databases;
- automatic Map01 and Map02 asset-resolution tables derived from the new bundle scan, with unresolved rows retained honestly;
- a full Map01 terrain surface and four bounded Map02 quadrant terrain surfaces, including height SQLite and baked albedo/normal atlases;
- an audited bootstrap map-layer dataset with a dynamic fingerprint;
- a local Blender profile registry placeholder tied to that fingerprint and explicitly marked `scan.status=not_ready`;
- pinned SceneProbe and BundleScanner binaries when Git, the locked .NET SDK, and network/package access are available;
- a full CAB-to-bundle inventory, per-map dependency bundle closures, and SceneProbe mesh/texture payloads when helper bootstrap succeeds;
- an `EndfieldRuntimeConfig/1` file and a per-user active runtime configuration.
- map overview PNGs and an `EndfieldMapOverviews/1` manifest served through the active runtime, without changing sector ownership.

The game directory is only opened for reading. Every derived SQLite, JSONL, bundle copy, log, report, and cache remains under the configured external roots. Existing run/output directories are never overwritten; `--resume` reuses only completed state entries and preserves partial files.

## Local first-run backend gate

`launch_webui.py` starts the token-protected local backend while keeping map extraction jobs locked until required first-run data is ready. `--defer-first-run-to-webui` explicitly selects the same default behavior. This mode does not change the extraction coordinator or publish generated data.

The local bridge exposes:

- `POST /first-run/validate` for an `EndfieldWebUIFirstRunRequest/1` path check;
- `POST /first-run` to start the complete coordinator or resume the same run;
- `GET /first-run` for the current `EndfieldWebUIFirstRunStatus/1` snapshot;
- `GET /first-run/events?after=<sequence>&limit=<1..500>` for paged monotonic progress;
- `POST /first-run/cancel` for cooperative cancellation with partial-output preservation.

The request schema is `webui_first_run_job_v1.schema.json`. The backend does not expose helper/SceneProbe skip switches, always passes `--no-activate-runtime-config`, and rejects map `/jobs` with `first_run_required` until status is `ready: true`. On success it hot-swaps the local export coordinator to the generated Stage/runtime config, and the WebUI reloads the active overview.

The baseline WebUI dataset is usable after its full audit passes. Terrain, instance, asset-resolution and scene-probe inputs are extracted during the same first run. The historical Blender profile registry remains separate from the portable scene path; users do not need historical research files. Preparation readiness does not guarantee that every selected asset or material is present in the cache.

The public `/jobs` contract accepts `export_mode=data_package` or `blend`. Data packages do not require Blender; scene mode uses the selected executable and the repository's portable scene builder, producing one scene per requested batch with packed images and reopen checks. Missing source assets and unsupported components remain in the scene plan and audit, and the result is `partial` when pending items remain. Neither mode invokes the historical `endfield_blender_selection_worker_v1.py`. See the root README for current known defects and release status.

Detailed logs are written to `<export-root>\log\first_run_<run-name>`. Cancellation uses the `CANCEL_REQUESTED` marker in that directory.

Published source includes the coordinator, extraction primitives, helper projects, patches, lockfiles, schemas, tests, and synthetic fixtures. It excludes all real SQLite, JSONL, bundle copies, PNG, Blend, Stage cache, logs, local runtime config, and machine-specific profile data.
