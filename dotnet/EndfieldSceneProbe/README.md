# EndfieldSceneProbe

Small, read-only scene inventory/export helper built on the local AnimeStudio checkout.

It accepts one already-extracted Endfield AssetBundle or a small folder of bundles. It writes:

- `scene_manifest.json`: GameObject/Transform hierarchy, renderer-to-mesh and renderer-to-material links, material properties, texture slots, external-file references and AssetBundle containers.
- `meshes/*.obj`: meshes referenced by renderers.
- `textures/*.png`: textures referenced by selected materials when the decoder supports the format and the streamed data is available.

The manifest also includes `DecalProjectors` with HGDecalProjector parameters and exact material references. Shader names are read from the serialized type tree to support the current Endfield shader layout. Runtime decal instances still require the separate InitChunkData/StreamingChunkData join performed by `extract_projected_decals.py`; prefab parameters alone do not identify every runtime material variant.

The output directory must not already exist; the tool refuses to overwrite it. It does not read process memory, start the game, or modify the game installation.

Build:

```powershell
dotnet build .\work\EndfieldSceneProbe\EndfieldSceneProbe.csproj -c Release
```

Run on a small extracted bundle set:

```powershell
.\work\EndfieldSceneProbe\bin\Release\net9.0-windows\EndfieldSceneProbe.exe `
  .\work\small_scene_bundle_set `
  .\work\scene_probe_output_01
```

For diagnostic bundles containing standalone meshes/textures but no GameObjects, append `--include-unreferenced`.

For a broader dependency/inventory pass without writing OBJ or PNG files, append `--manifest-only`.

Material records retain serialized disabled passes, valid keywords and render queues. Resolved shader references also include the original pass names. These describe source render state; they do not reconstruct HGRP execution. For focused pipeline-resource research, `--dump-metadata` writes original type-tree dumps for MonoBehaviour, TextAsset, Shader and ComputeShader objects and records them in `MetadataExports`. Use a small selected bundle set; missing type trees fail explicitly.

`AssetMap` and `CABMap` in AnimeStudio solve a different layer: AssetMap indexes objects/containers/source files, while CABMap maps Unity CAB names to physical bundle paths so PPtr dependencies can be loaded. This probe consumes the resulting small physical bundle set and records the scene relationships needed by a later Blender builder.
