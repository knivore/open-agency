from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Callable


def log_file_path_for(process_id: str) -> str:
    return f"logs/{process_id}.json"


def initialize_log_file(process_id: str) -> str:
    log_file_path = log_file_path_for(process_id)
    os.makedirs("logs", exist_ok=True)
    with open(log_file_path, "w", encoding="utf-8") as handle:
        json.dump([], handle)
        handle.flush()
        os.fsync(handle.fileno())
    return log_file_path


async def stream_log_file(process_id: str, *, is_process_alive: Callable[[str], bool]) -> AsyncGenerator[str, None]:
    file_path = log_file_path_for(process_id)
    sent_items: set[str] = set()
    last_modified = None

    while True:
        try:
            if not is_process_alive(process_id):
                with open(file_path, encoding="utf-8") as handle:
                    data = json.load(handle)
                    if isinstance(data, list):
                        for item in data:
                            item_str = json.dumps(item)
                            if item_str not in sent_items:
                                sent_items.add(item_str)
                                yield f"data: {item_str}\n\n"
                break

            current_modified_time = os.path.getmtime(file_path)
            if current_modified_time == last_modified:
                await asyncio.sleep(1)
                continue
            last_modified = current_modified_time

            with open(file_path, encoding="utf-8") as handle:
                data = json.load(handle)
                if not isinstance(data, list):
                    continue
                for item in data:
                    item_str = json.dumps(item)
                    if item_str not in sent_items:
                        sent_items.add(item_str)
                        yield f"data: {item_str}\n\n"
        except json.JSONDecodeError:
            await asyncio.sleep(1)
        except Exception:
            await asyncio.sleep(1)

    yield "event: close\ndata: Connection closed\n\n"


__all__ = ["initialize_log_file", "log_file_path_for", "stream_log_file"]
