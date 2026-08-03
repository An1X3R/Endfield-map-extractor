# EndfieldBundleScanner

只读扫描外层 VFS 中的 `.ab` 包，不落地复制全部资源。扫描器只解析每个包的
`AssetBundle.m_Container`，并把路径含 `map01/lv001` 或 `map01_lv001` 的命中项
追加写入 JSONL。输入 TSV 和输出 JSONL 均要求是新文件，程序拒绝覆盖。

输出中 `progress` 是检查点，`match` 是地图资源命中，`error` 保留单包解析失败信息，
`complete` 表示扫描自然结束。

加 `--cab-only` 时不解析对象，只输出所有物理包到 Unity CAB 名的映射；这用于解析
材质、贴图及场景的跨包依赖。


