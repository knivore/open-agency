from datetime import datetime
from pydantic import BaseModel, Field
from pydantic.functional_validators import BeforeValidator
from typing import Literal, Optional
from typing_extensions import Annotated

from app.domain import Execution as CanonicalExecution
from app.domain import ExecutionStatus

PyObjectId = Annotated[str, BeforeValidator(str)]


class ExecutionRecord(BaseModel):
    id: Optional[PyObjectId] = Field(alias="_id", default=None)
    processId: str = Field(..., description="Unique identifier for the execution process (UUID)")
    workflowId: str = Field(
        ...,
        description="Identifier of the workflow definition that was run",
        serialization_alias="workflowId",
    )
    userId: str = Field(..., description="Identifier of the user who initiated the run")
    startTime: datetime = Field(..., description="Timestamp when the execution started")
    endTime: Optional[datetime] = Field(None,
                                        description="Timestamp when the execution ended (completed, failed, or stopped)")
    status: Literal["running", "completed", "failed", "stopped"] = Field(...,
                                                                         description="Current status of the execution")
    finalOutput: Optional[str] = Field(None, description="The final raw output string from the workflow execution")
    logFileS3Path: Optional[str] = Field(None, description="Full S3 path to the persisted JSON log file")
    artifactDirectoryS3Path: Optional[str] = Field(None, description="S3 prefix for artifacts generated during the run")

    model_config = {
        "populate_by_name": True,
        "arbitrary_types_allowed": True,
        "json_encoders": {datetime: lambda dt: dt.isoformat()},
    }

    def to_canonical_execution(self) -> CanonicalExecution:
        status_map = {
            "running": ExecutionStatus.RUNNING,
            "completed": ExecutionStatus.COMPLETED,
            "failed": ExecutionStatus.FAILED,
            "stopped": ExecutionStatus.CANCELLED,
        }
        output_payload = None if self.finalOutput is None else {"content": self.finalOutput}
        return CanonicalExecution(
            id=self.processId,
            workflow_id=self.workflowId,
            runtime_adapter_id="crewai",
            status=status_map[self.status],
            input_payload={},
            output_payload=output_payload,
            error=None if self.status != "failed" else self.finalOutput,
            created_at=self.startTime,
            started_at=self.startTime,
            completed_at=self.endTime,
            created_by=self.userId,
            metadata={
                "log_file_s3_path": self.logFileS3Path,
                "artifact_directory_s3_path": self.artifactDirectoryS3Path,
            },
        )

    @classmethod
    def from_canonical_execution(cls, execution: CanonicalExecution) -> "ExecutionRecord":
        status_map = {
            ExecutionStatus.CREATED: "running",
            ExecutionStatus.QUEUED: "running",
            ExecutionStatus.RUNNING: "running",
            ExecutionStatus.WAITING_FOR_APPROVAL: "running",
            ExecutionStatus.PAUSED: "running",
            ExecutionStatus.CANCELLING: "running",
            ExecutionStatus.COMPLETED: "completed",
            ExecutionStatus.FAILED: "failed",
            ExecutionStatus.CANCELLED: "stopped",
        }
        final_output = None
        if execution.output_payload is not None:
            final_output = execution.output_payload.get("content") or str(execution.output_payload)
        return cls(
            processId=execution.id,
            workflowId=execution.workflow_id,
            userId=execution.created_by or "system",
            startTime=execution.started_at or execution.created_at,
            endTime=execution.completed_at,
            status=status_map[execution.status],
            finalOutput=final_output,
            logFileS3Path=execution.metadata.get("log_file_s3_path"),
            artifactDirectoryS3Path=execution.metadata.get("artifact_directory_s3_path"),
        )
