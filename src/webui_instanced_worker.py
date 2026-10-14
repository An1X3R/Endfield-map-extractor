"""Durable independent-scene jobs over the shared portable scene builder."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import tempfile
import threading
import time
from typing import TypedDict, cast
from uuid import uuid4

from webui_instanced_contract import (
    SceneRequest, SceneRequestError, canonical_scene_request, json_object,
    required_text, request_uuid, scene_build_request,
)


_LOGGER = logging.getLogger(__name__)
TERMINAL = frozenset({"completed", "partial", "failed", "cancelled"})


class InstancedJob(TypedDict):
    format: str
    job_id: str
    state: str
    phase: str
    progress: float
    message: str
    cancel_requested: bool
    request: SceneRequest
    result: dict[str, object] | None
    error: dict[str, object] | None


class SceneJobCancelled(RuntimeError):
    """The user requested cooperative cancellation of this scene job."""


class SceneJobNotFound(FileNotFoundError):
    """The requested job has no durable record in the current preparation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict[str, object]:
    return json_object(json.loads(path.read_text(encoding="utf-8-sig")), str(path))


def _write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=path.stem, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _no_cancel() -> None:
    return None


class InstancedSceneCoordinator:
    """Connect HTTP requests, durable external job files and extraction workers."""

    def __init__(self, first_run_status: Callable[[], dict[str, object]]) -> None:
        self._first_run_status = first_run_status
        self._lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._paths: dict[str, Path] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._session = str(uuid4())
        self._closing = False

    def shutdown(self) -> None:
        """Cancel owned jobs and join outside the record lock within ten seconds."""
        with self._lock:
            self._closing = True
            threads = tuple(self._threads.items())
            for job_id, _ in threads:
                self.cancel(job_id)
        deadline = time.monotonic() + 10.0
        for _, thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        alive = [job_id for job_id, thread in threads if thread.is_alive()]
        if alive:
            raise RuntimeError(f"Independent scene workers did not stop after cancellation: job_ids={alive}")

    def _context(self) -> tuple[Path, str]:
        status = self._first_run_status()
        if status.get("ready") is not True or status.get("state") in {"queued", "running"}:
            raise SceneRequestError("请先完成当前游戏路径的首次准备，再打开独立场景目录")
        result = json_object(status.get("result"), "first-run.result")
        path = Path(required_text(result.get("runtime_config"), "first-run.runtime_config")).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"首次准备配置不存在，请恢复准备: {path}")
        identity = json_object(status.get("request_identity"), "first-run.request_identity")
        return path, required_text(identity.get("sha256"), "first-run identity")

    def catalog(self) -> dict[str, object]:
        from endfield_instanced_catalog import read_instanced_catalog
        try:
            runtime, identity = self._context()
        except SceneRequestError as error:
            return {"format": "InstancedSceneCatalog/1", "catalog_id": "unavailable",
                    "first_run_identity": None, "availability": {"ready": False, "reason": str(error)},
                    "capabilities": {"export_modes": [], "requires_first_run": True},
                    "groups": [], "scenes": []}
        source = dict(read_instanced_catalog(runtime, _no_cancel))
        digest = hashlib.sha256((str(source["catalog_id"]) + ":" + identity).encode()).hexdigest()
        # Only publish fields the browser needs, never internal evidence file paths.
        return {"format": "InstancedSceneCatalog/1", "catalog_id": digest,
                "first_run_identity": identity, "availability": source["availability"],
                "capabilities": {"export_modes": ["blend"], "requires_first_run": True},
                "groups": [{"group_id": row["group_id"], "name": row["name"]}
                           for row in cast(list[dict[str, object]], source["groups"])],
                "scenes": [{name: row[name] for name in
                            ("scene_id", "name", "group_id", "form", "export_capability")}
                           for row in cast(list[dict[str, object]], source["scenes"])]}

    def _scene(self, scene_id: str, catalog_id: str | None) -> tuple[dict[str, object], dict[str, object]]:
        from endfield_instanced_catalog import read_instanced_catalog
        current = self.catalog()
        if catalog_id is not None and catalog_id != current["catalog_id"]:
            raise SceneRequestError("目录已更新，请刷新后重新选择场景")
        runtime, _ = self._context()
        source = read_instanced_catalog(runtime, _no_cancel)
        rows = cast(list[dict[str, object]], source["scenes"])
        for row in rows:
            if row["scene_id"] == scene_id:
                return current, row
        raise FileNotFoundError(f"当前目录未找到物理场景: {scene_id}")

    def detail(self, scene_id: str, catalog_id: str | None) -> dict[str, object]:
        current, row = self._scene(scene_id, catalog_id)
        limitations = list(cast(list[str], row.get("limitations", [])))
        limitations.append("按物理场景整体导出，可能包含关卡名所指区域之外的内容；局部样本验收不代表整个场景已验收。构件数量未测量时显示未知，首次导出会按需准备资源。")
        return {"format": "InstancedSceneDetail/1", "catalog_id": current["catalog_id"],
                "first_run_identity": current["first_run_identity"], "scene_id": scene_id,
                "name": row["name"], "group_id": row["group_id"],
                "source_label": "当前游戏包体中的物理场景：" + scene_id,
                "dimensions": row.get("dimensions"), "component_counts": row["component_counts"],
                "export_capability": row["export_capability"], "limitations": limitations}

    def preview(self, scene_id: str, catalog_id: str | None) -> dict[str, object]:
        current, _ = self._scene(scene_id, catalog_id)
        return {"format": "InstancedScenePreview/1", "scene_id": scene_id,
                "catalog_id": current["catalog_id"], "first_run_identity": current["first_run_identity"],
                "status": "missing", "scope": None, "reason": "当前目录没有该物理场景的完整预览图。", "asset": None}

    def _canonical(self, payload: Mapping[str, object]) -> SceneRequest:
        runtime, identity = self._context()
        catalog = self.catalog()
        if json_object(catalog["availability"], "availability").get("ready") is not True:
            raise SceneRequestError("独立场景目录尚不可用，请检查首次准备诊断")
        rows = cast(list[dict[str, object]], catalog["scenes"])
        available = frozenset(str(row["scene_id"]) for row in rows
                              if json_object(row["export_capability"], "capability").get("ready") is True)
        return canonical_scene_request(payload, str(catalog["catalog_id"]), identity,
                                       json_object(_read(runtime)["paths"], "runtime.paths"), runtime.parent, available)

    def validate(self, payload: Mapping[str, object]) -> dict[str, object]:
        request = self._canonical(payload)
        return {"format": "InstancedSceneExportValidation/1", "status": "valid",
                "canonical_request": request, "issues": [],
                "warnings": ["独立场景按物理场景整体导出；特效与运行时光照可能仍有待还原项。"]}

    def _job_path(self, job_id: str) -> Path:
        request_uuid(job_id)
        if job_id in self._paths:
            return self._paths[job_id]
        runtime, _ = self._context()
        path = runtime.parent / "instanced_jobs" / (job_id + ".json")
        self._paths[job_id] = path
        return path

    def _save(self, path: Path, job: InstancedJob, events: list[dict[str, object]]) -> None:
        event = {"sequence": len(events) + 1, "timestamp": _utc_now(),
                 **{key: job[key] for key in ("state", "phase", "progress", "message", "cancel_requested")}}
        _write(path, {"session": self._session, "job": job, "events": [*events, event]})

    def _load(self, job_id: str) -> tuple[Path, InstancedJob, list[dict[str, object]]]:
        path = self._job_path(job_id)
        if not path.is_file():
            raise SceneJobNotFound(f"当前准备目录没有该任务记录: {job_id}")
        record = _read(path)
        job = cast(InstancedJob, json_object(record.get("job"), "stored job"))
        events = cast(list[dict[str, object]], record["events"])
        if job["job_id"] != job_id or job["request"]["request_id"] != job_id:
            raise SceneRequestError("保存的任务身份不一致，不能读取该结果")
        if record.get("session") != self._session and job["state"] not in TERMINAL:
            job = {**job, "state": "failed", "phase": "failed", "message": "后端进程曾中断，已保留现有产物，请新建任务重试。",
                   "error": {"code": "worker_interrupted", "message": "任务所属后端进程已结束，不能冒报仍在运行。",
                             "retryable": True, "stage": job["phase"]}}
            self._save(path, job, events)
            events = cast(list[dict[str, object]], _read(path)["events"])
        return path, job, events

    def get(self, job_id: str) -> InstancedJob:
        with self._lock:
            return self._load(job_id)[1]

    def events(self, job_id: str, after: int) -> dict[str, object]:
        if after < 0:
            raise SceneRequestError("after: 事件序号不能为负数")
        with self._lock:
            _, _, events = self._load(job_id)
            latest = len(events)
            if after > latest:
                raise SceneRequestError("事件游标超出当前任务范围，请重新连接任务")
            return {"format": "InstancedSceneJobEvents/1", "job_id": job_id,
                    "events": events[after:], "next_after": latest}

    def submit(self, payload: Mapping[str, object]) -> InstancedJob:
        with self._lock:
            if self._closing:
                raise SceneRequestError("Independent scene worker is closing; restart the launcher before submitting")
            request = self._canonical(payload)
            job_id = request["request_id"]
            runtime, _ = self._context()
            path = self._job_path(job_id)
            if path.exists():
                current = self._load(job_id)[1]
                if current["request"] != request:
                    raise SceneRequestError("同一个 request_id 不能提交不同参数；请创建新任务")
                return current
            job = InstancedJob(format="InstancedSceneJob/1", job_id=job_id, state="queued", phase="queued",
                               progress=0.0, message="独立场景任务已排队", cancel_requested=False,
                               request=request, result=None, error=None)
            self._save(path, job, [])
            thread = threading.Thread(target=self._run, args=(job_id, runtime), daemon=True,
                                      name="endfield-instanced-" + job_id)
            self._threads[job_id] = thread
            try:
                thread.start()
            except RuntimeError as error:
                self._update(job_id, {"state": "failed", "phase": "failed", "message": "无法启动独立场景任务",
                                    "error": {"code": "worker_start_failed", "message": str(error),
                                              "stage": "queued", "retryable": True}})
                raise
            return job

    def _update(self, job_id: str, changes: Mapping[str, object]) -> None:
        with self._lock:
            path, job, events = self._load(job_id)
            if job["state"] in TERMINAL:
                return
            updated = cast(InstancedJob, {**job, **changes})
            if updated["state"] in {"completed", "partial"} and job["cancel_requested"]:
                updated = {**updated, "state": "cancelled", "phase": "cancelled", "result": None,
                           "message": "任务已取消，已生成的文件保留"}
            self._save(path, updated, events)

    def cancel(self, job_id: str) -> dict[str, object]:
        with self._lock:
            _, job, _ = self._load(job_id)
            terminal = job["state"] in TERMINAL
            if not terminal:
                changes: dict[str, object] = {"cancel_requested": True, "message": "已请求取消，等待工作进程退出"}
                if job["state"] == "queued":
                    changes.update(state="cancelled", phase="cancelled", message="已取消尚未开始的任务")
                self._update(job_id, changes)
            return {"format": "InstancedSceneJobCancelAck/1", "job_id": job_id,
                    "accepted": True, "state": "already_terminal" if terminal else "accepted"}

    def _check(self, job_id: str) -> None:
        job = self.get(job_id)
        if job["cancel_requested"] or job["state"] == "cancelled":
            raise SceneJobCancelled(job_id)

    def _progress(self, job_id: str, phase: str, progress: float, message: str) -> None:
        self._check(job_id)
        current = self.get(job_id)
        self._update(job_id, {"state": phase, "phase": phase,
                             "progress": max(current["progress"], min(0.99, progress)), "message": message})

    def _run(self, job_id: str, runtime: Path) -> None:
        from endfield_instanced_catalog import prepare_instanced_scene
        from endfield_scene_export import build_scene_exports
        from webui_instanced_results import build_instanced_scene_export_result
        try:
            with self._execution_lock:
                self._check(job_id)
                request = self.get(job_id)["request"]
                self._canonical(request)
                self._progress(job_id, "preparing", 0.01, "按当前物理场景准备资源")
                prepared = prepare_instanced_scene(runtime, request["scene_id"], lambda: self._check(job_id),
                    lambda value, message: self._progress(job_id, "preparing", 0.02 + value * 0.18, message))
                self._check(job_id)
                output = Path(request["output"]["root"]) / (request["output"]["label"] + "_" + job_id)
                output.mkdir(parents=True, exist_ok=False)
                _write(output / "request.json", request)
                (output / "INCOMPLETE").write_text(job_id, encoding="ascii")
                artifacts = build_scene_exports(prepared.runtime_path, scene_build_request(request, prepared.bounds), output,
                    lambda: self._check(job_id),
                    lambda value, message: self._progress(job_id, "building", 0.2 + value * 0.73, message))
                self._progress(job_id, "auditing", 0.94, "核对真实文件、构件统计和待处理项")
                result = dict(build_instanced_scene_export_result(output, artifacts))
                self._check(job_id)
                _write(output / "instanced_result.json", result)
                (output / "INCOMPLETE").unlink()
                state = str(result["status"])
                self._update(job_id, {"state": state, "phase": state, "progress": 1.0,
                                    "message": "导出完成，存在待处理项" if state == "partial" else "导出完成",
                                    "result": result})
        except SceneJobCancelled:
            self._update(job_id, {"state": "cancelled", "phase": "cancelled", "message": "任务已取消，已生成的部分文件保留"})
        except Exception as error:
            _LOGGER.exception("instanced_scene_job_failed", extra={"job_id": job_id, "error_type": type(error).__name__})
            current = self.get(job_id)
            self._update(job_id, {"state": "failed", "phase": "failed", "message": "独立场景导出失败，请展开诊断查看原因",
                                "error": {"code": type(error).__name__, "message": str(error),
                                          "stage": current["phase"], "retryable": True}})
        finally:
            with self._lock:
                self._threads.pop(job_id, None)
