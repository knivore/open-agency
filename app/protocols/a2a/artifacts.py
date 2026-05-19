from __future__ import annotations

from app.domain import ExecutionArtifact


def execution_artifact_to_a2a_artifact(artifact: ExecutionArtifact) -> dict:
    return {
        "id": artifact.id,
        "task_id": artifact.execution_id,
        "name": artifact.name,
        "type": artifact.artifact_type,
        "uri": artifact.uri,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "created_at": artifact.created_at.isoformat(),
        "metadata": artifact.metadata,
    }
