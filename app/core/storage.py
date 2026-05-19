from __future__ import annotations

import boto3
import json
import os
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import HTTPException
from io import BytesIO
from typing import BinaryIO, Dict, List

from app.core.logging import get_logger

logger = get_logger(__name__)

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")
LOCAL_STORAGE_PATH = os.getenv("LOCAL_STORAGE_PATH", "local_storage")


def is_local_environment() -> bool:
    return os.getenv("ENVIRONMENT", "dev").lower() == "local"


def ensure_local_storage() -> None:
    os.makedirs(LOCAL_STORAGE_PATH, exist_ok=True)


def get_local_file_path(key: str) -> str:
    return os.path.join(LOCAL_STORAGE_PATH, key)


def _is_file_like(value: object) -> bool:
    return hasattr(value, "read")


def workflow_run_directory(user_id: str, workflow_id: str, process_id: str) -> str:
    return f"user_{user_id}/workflow_{workflow_id}/run_{process_id}"


def get_s3_client():
    if is_local_environment():
        logger.info("Running in local environment - using mock S3 client")
        return None

    missing_env_vars = [
        var
        for var, value in {
            "AWS_REGION": AWS_REGION,
            "S3_BUCKET_NAME": S3_BUCKET_NAME,
        }.items()
        if not value
    ]
    if missing_env_vars:
        missing = ", ".join(missing_env_vars)
        logger.error("Missing environment variables: %s", missing)
        raise RuntimeError(f"Missing environment variables: {missing}")

    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        return boto3.client(
            "s3",
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
            config=Config(signature_version="s3v4"),
        )
    return boto3.client("s3", region_name=AWS_REGION, config=Config(signature_version="s3v4"))


def mock_upload_to_local(file_data: str | BinaryIO | bytes, target_path: str) -> None:
    ensure_local_storage()
    target_dir = os.path.dirname(target_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    if isinstance(file_data, str):
        import shutil

        shutil.copy2(file_data, target_path)
        return
    if _is_file_like(file_data):
        file_data.seek(0)
        with open(target_path, "wb") as handle:
            handle.write(file_data.read())
        return
    if isinstance(file_data, bytes):
        with open(target_path, "wb") as handle:
            handle.write(file_data)
        return
    raise ValueError("Unsupported file type")


def upload_to_s3(
        workflow_id: str,
        process_id: str,
        user_id: str,
        files: List[str | BinaryIO | bytes],
        file_names: List[str] | None = None,
) -> dict[str, object]:
    uploaded_files: list[str] = []
    failed_files: list[dict[str, object]] = []
    directory = workflow_run_directory(user_id, workflow_id, process_id)

    if is_local_environment():
        logger.info("Running in local environment - storing files locally")
        for idx, file in enumerate(files):
            try:
                if isinstance(file, str):
                    if not os.path.isfile(file):
                        failed_files.append({"file": file, "error": "Invalid file path"})
                        continue
                    file_name = os.path.basename(file)
                    target_path = get_local_file_path(os.path.join(directory, file_name))
                elif _is_file_like(file):
                    file_name = getattr(file, "name", "uploaded_file")
                    if not os.path.splitext(file_name)[1]:
                        file_name += ".bin"
                    target_path = get_local_file_path(os.path.join(directory, file_name))
                elif isinstance(file, bytes):
                    if not file_names or len(file_names) <= idx:
                        raise ValueError("File name must be provided for raw bytes uploads.")
                    file_name = file_names[idx]
                    target_path = get_local_file_path(os.path.join(directory, file_name))
                else:
                    raise ValueError("Unsupported file type")

                mock_upload_to_local(file, target_path)
                uploaded_files.append(os.path.join(directory, file_name))
                logger.info("Mock upload successful: %s", target_path)
            except Exception as exc:
                logger.error("Failed to upload %s: %s", file, exc)
                failed_files.append({"file": file, "error": str(exc)})
    else:
        s3_client = get_s3_client()
        for idx, file in enumerate(files):
            try:
                if isinstance(file, str):
                    if not os.path.isfile(file):
                        failed_files.append({"file": file, "error": "Invalid file path"})
                        continue
                    file_name = os.path.basename(file)
                    with open(file, "rb") as handle:
                        s3_key = os.path.join(directory, file_name)
                        s3_client.upload_fileobj(handle, S3_BUCKET_NAME, s3_key)
                elif _is_file_like(file):
                    file_name = getattr(file, "name", "uploaded_file")
                    if not os.path.splitext(file_name)[1]:
                        file_name += ".bin"
                    s3_key = os.path.join(directory, file_name)
                    file.seek(0)
                    s3_client.upload_fileobj(file, S3_BUCKET_NAME, s3_key)
                elif isinstance(file, bytes):
                    if not file_names or len(file_names) <= idx:
                        raise ValueError("File name must be provided for raw bytes uploads.")
                    file_name = file_names[idx]
                    s3_key = os.path.join(directory, file_name)
                    s3_client.upload_fileobj(BytesIO(file), S3_BUCKET_NAME, s3_key)
                else:
                    raise ValueError("Unsupported file type")

                uploaded_files.append(s3_key)
                logger.info("Uploaded successfully: %s", s3_key)
            except Exception as exc:
                logger.error("Failed to upload %s: %s", file, exc)
                failed_files.append({"file": file, "error": str(exc)})

    message = "Some files failed to upload." if failed_files else "Upload process completed."
    return {
        "message": message,
        "uploaded_files": uploaded_files,
        "failed_files": failed_files,
    }


def return_file_from_s3(s3_key: str):
    if is_local_environment():
        logger.info("Running in local environment - retrieving file locally: %s", s3_key)
        local_path = get_local_file_path(s3_key)
        try:
            return open(local_path, "rb")
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"File not found: {s3_key}") from exc

    s3_client = boto3.client("s3")
    try:
        response = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
        return response["Body"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchKey":
            raise HTTPException(status_code=404, detail=f"File not found: {s3_key}") from exc
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def generate_presigned_url(operation: str, filename: str, content_type: str | None = None) -> str:
    if is_local_environment():
        logger.info("Running in local environment - generating mock presigned URL")
        operation_path = "upload" if operation == "upload" else "download"
        return f"http://localhost:8000/api/local-storage/{operation_path}?file={filename}"

    if not S3_BUCKET_NAME:
        raise ValueError("S3_BUCKET_NAME environment variable is not set.")

    params = {"Bucket": S3_BUCKET_NAME, "Key": filename}
    if operation == "upload" and content_type:
        params["ContentType"] = content_type

    try:
        return get_s3_client().generate_presigned_url(
            ClientMethod="put_object" if operation == "upload" else "get_object",
            Params=params,
            ExpiresIn=3600,
            HttpMethod="PUT" if operation == "upload" else "GET",
        )
    except ClientError as exc:
        raise HTTPException(status_code=500, detail=f"AWS ClientError: {str(exc)}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Error generating presigned URL: {str(exc)}") from exc


def get_log_file_content(user_id: str, workflow_id: str, process_id: str) -> List[Dict]:
    log_filename = f"{process_id}.json"
    s3_key = os.path.join(workflow_run_directory(user_id, workflow_id, process_id), log_filename)
    logger.info("Attempting to read log file from: %s", s3_key)
    try:
        file_data_stream = return_file_from_s3(s3_key)
        if hasattr(file_data_stream, "read"):
            log_content_bytes = file_data_stream.read()
            if hasattr(file_data_stream, "close"):
                file_data_stream.close()
            log_content_str = log_content_bytes.decode("utf-8")
        else:
            log_content_str = file_data_stream.decode("utf-8") if isinstance(file_data_stream, bytes) else str(
                file_data_stream)

        logs = json.loads(log_content_str)
        if not isinstance(logs, list):
            logger.warning("Log file %s does not contain a valid JSON list.", s3_key)
            return []
        logger.info("Successfully read and parsed log file: %s", s3_key)
        return logs
    except HTTPException as exc:
        if exc.status_code == 404:
            logger.warning("Log file not found: %s", s3_key)
            return []
        logger.error("HTTP error fetching log file %s: %s", s3_key, exc.detail)
        raise
    except json.JSONDecodeError as exc:
        logger.error("Error decoding JSON from log file %s: %s", s3_key, exc)
        return []
    except Exception as exc:
        logger.error("Unexpected error reading log file %s: %s", s3_key, exc)
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {str(exc)}") from exc


def list_artifacts(user_id: str, workflow_id: str, process_id: str, artifact_type: str = "images") -> List[str]:
    if artifact_type not in {"images", "files", "all"}:
        raise ValueError("artifact_type must be 'images', 'files', or 'all'")

    artifact_directory = f"{workflow_run_directory(user_id, workflow_id, process_id)}/"
    image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

    if is_local_environment():
        logger.info("Listing artifacts in directory: %s", artifact_directory)
        local_dir_path = get_local_file_path(artifact_directory.rstrip("/"))
        if not os.path.exists(local_dir_path) or not os.path.isdir(local_dir_path):
            logger.warning("Local artifact directory not found: %s", local_dir_path)
            return []

        try:
            files = [
                os.path.join(artifact_directory, file_name)
                for file_name in os.listdir(local_dir_path)
                if os.path.isfile(os.path.join(local_dir_path, file_name))
            ]
            if artifact_type == "images":
                files = [file_path for file_path in files if os.path.splitext(file_path)[1].lower() in image_extensions]
            elif artifact_type == "files":
                files = [file_path for file_path in files if
                         os.path.splitext(file_path)[1].lower() not in image_extensions]
            logger.info("Found %s artifacts locally in %s", len(files), artifact_directory)
            return files
        except Exception as exc:
            logger.error("Error listing local artifacts in %s: %s", local_dir_path, exc)
            return []

    s3_client = get_s3_client()
    if not s3_client:
        logger.error("S3 client is not available.")
        return []
    logger.info("Listing artifacts in directory: %s", artifact_directory)
    try:
        paginator = s3_client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=S3_BUCKET_NAME, Prefix=artifact_directory)
        all_keys: list[str] = []
        for page in pages:
            if "Contents" in page:
                all_keys.extend(obj["Key"] for obj in page["Contents"])
        files = [key for key in all_keys if not key.endswith("/")]
        if artifact_type == "images":
            files = [file_path for file_path in files if os.path.splitext(file_path)[1].lower() in image_extensions]
        elif artifact_type == "files":
            files = [file_path for file_path in files if os.path.splitext(file_path)[1].lower() not in image_extensions]
        logger.info("Found %s artifacts in S3 prefix: %s", len(files), artifact_directory)
        return files
    except ClientError as exc:
        logger.error("S3 ClientError listing artifacts in %s: %s", artifact_directory, exc)
        return []
    except Exception as exc:
        logger.error("Unexpected error listing S3 artifacts in %s: %s", artifact_directory, exc)
        return []


__all__ = [
    "LOCAL_STORAGE_PATH",
    "ensure_local_storage",
    "generate_presigned_url",
    "get_local_file_path",
    "get_log_file_content",
    "get_s3_client",
    "is_local_environment",
    "list_artifacts",
    "mock_upload_to_local",
    "return_file_from_s3",
    "upload_to_s3",
]
