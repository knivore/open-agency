from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from multiprocessing import Process, Queue
from threading import Lock
from typing import Any, Callable

from app.core.logging import get_logger
from app.core.storage import upload_to_s3
from app.runtime.channels import create_async_redis_client
from app.runtime.process_logs import initialize_log_file, log_file_path_for, stream_log_file

logger = get_logger(__name__)


@dataclass(slots=True)
class ExecutionProcessManager:
    redis_client: Any = field(default_factory=create_async_redis_client)
    process_lock: Lock = field(default_factory=Lock)
    processes: dict[str, dict[str, Any]] = field(default_factory=dict)

    def initialize_log_file(self, process_id: str) -> str:
        return initialize_log_file(process_id)

    async def start_process(
            self,
            *,
            process_id: str,
            target: Callable[..., Any],
            args: tuple[Any, ...] = (),
            metadata: dict[str, Any] | None = None,
    ) -> None:
        queue = Queue()
        process = Process(target=target,
                          args=(*args, queue, process_id, metadata.get("run_by", "system") if metadata else "system"))
        process.start()
        with self.process_lock:
            self.processes[process_id] = {
                "process": process,
                "queue": queue,
                "start_time": datetime.now(),
                **(metadata or {}),
            }

    async def start_crewai_process(
            self,
            workflow_id: str,
            workflow_payload: dict[str, Any],
            workflow_inputs: dict[str, str],
            process_id: str,
            run_by: str,
            log_file_path: str,
            target: Callable[..., Any],
    ) -> None:
        queue = Queue()
        process = Process(target=target, args=(workflow_payload, workflow_inputs, queue, process_id, run_by))
        process.start()
        with self.process_lock:
            self.processes[process_id] = {
                "process": process,
                "queue": queue,
                "start_time": datetime.now(),
                "workflow_id": workflow_id,
                "file_path": log_file_path,
                "run_by": run_by,
            }

    def is_process_alive(self, process_id: str) -> bool:
        with self.process_lock:
            process_info = self.processes.get(process_id)
            if not process_info:
                return False
            process: Process = process_info["process"]
            return bool(process.is_alive())

    def stop_process(self, process_id: str) -> None:
        with self.process_lock:
            process_info = self.processes.get(process_id)
            if not process_info:
                return
            process: Process = process_info["process"]
            if process.is_alive():
                process.terminate()

    def handle_process_completion(self, process_id: str) -> Any:
        with self.process_lock:
            process_info = self.processes.pop(process_id, None)
            result = None
            if process_info:
                try:
                    if process_info.get("queue") and not process_info["queue"].empty():
                        result = process_info["queue"].get_nowait()
                    file_path = log_file_path_for(process_id)
                    if os.path.exists(file_path) and process_info.get("workflow_id") and process_info.get("run_by"):
                        upload_to_s3(str(process_info["workflow_id"]), process_id, str(process_info["run_by"]),
                                     [file_path])
                except Exception as exc:
                    logger.warning("Failed to finalize process %s: %s", process_id, exc)
            return result

    async def stream_logs(self, process_id: str):
        async for chunk in stream_log_file(process_id, is_process_alive=self.is_process_alive):
            yield chunk

    def process_status(self, process_id: str) -> dict[str, Any]:
        with self.process_lock:
            process_info = self.processes.get(process_id)
            process_exists = process_info is not None
            process_alive = bool(process_info and process_info["process"].is_alive())
        if process_exists:
            if process_alive:
                return {"status": "Process is still running"}
            result = self.handle_process_completion(process_id)
            return {"status": "completed", "result": result}
        return {"status": "No active process"}


execution_process_manager = ExecutionProcessManager()

__all__ = ["ExecutionProcessManager", "execution_process_manager"]
