def echo_tool(text: str):
    return {"echo": text}


def failing_tool(text: str):
    raise RuntimeError(f"tool failure: {text}")


def artifact_tool(name: str):
    return {
        "artifact_uri": f"s3://bucket/{name}.txt",
        "artifact_name": f"{name}.txt",
        "artifact_type": "text",
        "result": "stored",
    }


def token_tool(text: str):
    return {"status": "ok", "token": text, "nested": {"api_key": text}}


def computer_use_click(x: float, y: float, token: str | None = None):
    return {"status": "ok", "x": x, "y": y, "token_seen": token is not None}
