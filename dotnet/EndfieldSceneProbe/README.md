# EndfieldSceneProbe

Small, read-only scene inventory/export helper built on the local AnimeStudio checkout.

It accepts one already-extracted Endfield AssetBundle or a small folder of bundles. It writes:

- `scene_manifest.json`: GameObject/Transform hierarchy, renderer-to-mesh and renderer-to-material links, material properties, texture slots, external-file references and AssetBundle containers.
- `meshes/*.obj`: meshes referenced by renderers.
- `textures/*.png`: textures referenced by selected materials when the decoder supports the format and the streamed data is available.

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

`AssetMap` and `CABMap` in AnimeStudio solve a different layer: AssetMap indexes objects/containers/source files, while CABMap maps Unity CAB names to physical bundle paths so PPtr dependencies can be loaded. This probe consumes the resulting small physical bundle set and records the scene relationships needed by a later Blender builder.


