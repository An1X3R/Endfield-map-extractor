# EndfieldSceneProbe

Small, read-only scene inventory/export helper built on the local AnimeStudio checkout.

It accepts one already-extracted Endfield AssetBundle or a small folder of bundles. It writes:

- `scene_manifest.json`: GameObject/Transform hierarchy, renderer-to-mesh and renderer-to-material links, material properties, texture slots, external-file references and AssetBundle containers.
- `meshes/*.obj`: meshes referenced by renderers.
- `textures/*.png`: textures referenced by selected materials when the decoder supports the format and the streamed data is available.

The manifest also includes `DecalProjectors` with HGDecalProjector parameters and exact material references. Shader names are read from the serialized type tree to support the current Endfield shader layout. Runtime decal instances still require the separate InitChunkData/StreamingChunkData join performed by `extract_projected_decals.py`; prefab parameters alone do not identify every runtime material variant.

`Water` contains typed global settings, individual configurations and sector records read from their serialized type trees. Pointer records retain source identity and resolution status. These records do not identify water instances by themselves: the water ECS decoder joins type-30 Init entities with their same-entity Streaming resource references. Collision meshes are not water renderer bindings.

`TextureArrayExports` keeps array layers separate from ordinary `TextureExports`, with the original asset identity, layer index, mip offsets and payload hashes. BC7 UNorm arrays are decoded into individual PNG layers; unsupported formats report explicit errors. Texture availability and decoded water parameters do not imply that their shader semantics or Blender integration have been verified.

`Particles` pairs systems and renderers through their source-qualified GameObject ownership and reverse component references. Parameter schema v4 retains Initial/Emission, Shape, Size over Lifetime, Color over Lifetime, Trail, CustomData and UV modules, including source gravity multipliers, rotation, curve keys, gradients and bursts. Direct Shape mesh/texture references join the existing export sets; renderer/sprite indirect dependencies carry an explicit unverified status when present. Curve slopes preserve stepped tangents as JSON strings `"Infinity"` or `"-Infinity"`; NaN is rejected and other numeric fields must be finite. The helper exports source parameters; it does not simulate particles. The Blender consumer supports a bounded static snapshot subset and reports unsupported modules explicitly.

The output directory must not already exist; the tool refuses to overwrite it. It does not read process memory, start the game, or modify the game installation.

Build:

```powershell
dotnet build .\dotnet\EndfieldSceneProbe\EndfieldSceneProbe.csproj -c Release -p:AnimeStudioRoot='<pinned AnimeStudio checkout>'
```

Run on a small extracted bundle set:

```powershell
.\dotnet\EndfieldSceneProbe\bin\Release\net9.0-windows\EndfieldSceneProbe.exe `
  .\work\small_scene_bundle_set `
  .\work\scene_probe_output_01
```

For diagnostic bundles containing standalone meshes/textures but no GameObjects, append `--include-unreferenced`.

For a broader dependency/inventory pass without writing OBJ or PNG files, append `--manifest-only`.

Material records retain serialized disabled passes, valid keywords and render queues. Resolved shader references also include the original pass names. These describe source render state; they do not reconstruct HGRP execution. For focused pipeline-resource research, `--dump-metadata` writes original type-tree dumps for MonoBehaviour, TextAsset, Shader and ComputeShader objects and records them in `MetadataExports`. Use a small selected bundle set; missing type trees fail explicitly.

`AssetMap` and `CABMap` in AnimeStudio solve a different layer: AssetMap indexes objects/containers/source files, while CABMap maps Unity CAB names to physical bundle paths so PPtr dependencies can be loaded. This probe consumes the resulting small physical bundle set and records the scene relationships needed by a later Blender builder.
