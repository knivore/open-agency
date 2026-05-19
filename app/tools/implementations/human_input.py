from __future__ import annotations

import threading
import time
from pydantic import BaseModel, Field
from typing import Any

from app.runtime.channels import agent_output_channel, create_sync_redis_client, human_reply_channel


class HumanInputRequest(BaseModel):
    query: str = Field(..., description="The question to ask the human operator")
    timeout_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="Maximum seconds to wait for a human reply before returning timeout.",
    )
    process_id: str | int | None = Field(
        default=None,
        description="Optional execution or process id used to route the human prompt/reply channel.",
    )


redis_client = create_sync_redis_client()


def request_human_input(query: str, process_id: str | int | None = None, timeout: int = 60) -> dict[str, Any]:
    response_holder = {"response": ""}
    process_key = process_id or 0
    redis_client.publish(agent_output_channel(process_key), query)

    def wait_for_response() -> None:
        pubsub = redis_client.pubsub()
        pubsub.subscribe(human_reply_channel(process_key))
        start_time = time.time()
        try:
            while time.time() - start_time < timeout:
                message = pubsub.get_message()
                if message and message["type"] == "message":
                    response_holder["response"] = str(message["data"])
                    return
                time.sleep(1)
        finally:
            pubsub.close()

    response_thread = threading.Thread(target=wait_for_response)
    response_thread.start()
    response_thread.join()

    if response_holder["response"]:
        return {"status": "received", "response": response_holder["response"]}
    return {"status": "timeout", "response": ""}
