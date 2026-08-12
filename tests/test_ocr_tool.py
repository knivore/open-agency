from __future__ import annotations

import base64
import os
import unittest

import httpx

from app.tools.definitions import get_tool_catalog_specs
from app.tools.implementations.ocr import BaiduUnlimitedOCRProvider, OCRDocumentInput, recognize_document


class _FakeOCRProvider:
    def __init__(self) -> None:
        self.request: OCRDocumentInput | None = None

    async def parse(self, request: OCRDocumentInput) -> dict:
        self.request = request
        return {
            "status": "success",
            "provider": request.provider,
            "model": request.model,
            "task_id": "task_test",
            "markdown_url": None,
            "parse_result_url": None,
            "task_error": None,
        }


class OCRToolCatalogTests(unittest.TestCase):
    def test_ocr_tool_is_in_the_builtin_catalog_with_approval(self) -> None:
        tool = get_tool_catalog_specs()["agency.document.ocr"].tool_definition

        self.assertEqual(tool.name, "recognize_document")
        self.assertTrue(tool.security.allow_network)
        self.assertTrue(tool.security.read_only)
        self.assertTrue(tool.security.requires_approval)
        self.assertEqual(tool.implementation.module, "app.tools.implementations.ocr")
        self.assertEqual(tool.implementation.function, "recognize_document")

    def test_ocr_input_rejects_multiple_sources_and_unsupported_models(self) -> None:
        with self.assertRaises(ValueError):
            OCRDocumentInput(file_path="document.pdf", file_url="https://files.example/document.pdf")
        with self.assertRaises(ValueError):
            OCRDocumentInput(file_url="https://files.example/document.pdf", filename="document.pdf", model="other/model")


class OCRToolBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def test_recognize_document_uses_the_selected_adapter_contract(self) -> None:
        provider = _FakeOCRProvider()

        result = await recognize_document(
            file_base64=base64.b64encode(b"document").decode(),
            filename="document.pdf",
            _provider=provider,
        )

        self.assertEqual(result["task_id"], "task_test")
        self.assertIsNotNone(provider.request)
        self.assertEqual(provider.request.model, "baidu/Unlimited-OCR")

    async def test_baidu_adapter_polls_then_downloads_markdown(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            if request.url.path == "/oauth/2.0/token":
                return httpx.Response(200, json={"access_token": "token"})
            if request.url.path.endswith("/unlimited-ocr-parser/task"):
                return httpx.Response(200, json={"error_code": 0, "result": {"task_id": "task_123"}})
            if request.url.path.endswith("/unlimited-ocr-parser/task/query"):
                return httpx.Response(
                    200,
                    json={
                        "error_code": 0,
                        "result": {
                            "task_id": "task_123",
                            "status": "success",
                            "markdown_url": "https://bucket.bcebos.com/ocr/task_123.md",
                            "parse_result_url": "https://bucket.bcebos.com/ocr/task_123.json",
                        },
                    },
                )
            if request.url.host == "bucket.bcebos.com":
                return httpx.Response(200, text="# Parsed\n\nHello")
            return httpx.Response(404)

        previous_app_env = os.environ.get("APP_ENV")
        os.environ["APP_ENV"] = "test"
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                provider = BaiduUnlimitedOCRProvider(
                    api_key="key",
                    secret_key="secret",
                    http_client=client,
                )
                result = await provider.parse(
                    OCRDocumentInput(
                        file_base64=base64.b64encode(b"document").decode(),
                        filename="document.pdf",
                        poll_interval_seconds=1,
                    )
                )
        finally:
            if previous_app_env is None:
                os.environ.pop("APP_ENV", None)
            else:
                os.environ["APP_ENV"] = previous_app_env

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["markdown"], "# Parsed\n\nHello")
        self.assertFalse(result["markdown_truncated"])
        self.assertEqual(len(calls), 4)


if __name__ == "__main__":
    unittest.main()
