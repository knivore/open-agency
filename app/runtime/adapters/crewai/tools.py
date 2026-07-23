"""CrewAI tool wrappers for app-owned tool implementations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from typing import Any, Type

from app.tools.implementations.browser import (
    BrowserAnalyseInput,
    BrowserClickInput,
    BrowserExtractInput,
    BrowserInputText,
    BrowserOpenInput,
    BrowserScrollInput,
    BrowserSelectInput,
    BrowserVerifyInput,
    click_element,
    open_browser,
    screenshot,
    screenshot_and_analyse,
    screenshot_and_extract,
    scroll_page,
    select_dropdown,
    send_keys,
    terminate_browser,
    verify_content,
)
from app.tools.implementations.custom.files import FileWriteInput, write_text_file
from app.tools.implementations.documents import SaveMarkdownToWordToolSchema, save_markdown_to_word
from app.tools.implementations.http_integrations import CustomAPIInput, execute_custom_api
from app.tools.implementations.human_input import HumanInputRequest, request_human_input
from app.tools.implementations.spreadsheets import ExcelImageInput, ExcelJSONInput, ExcelTextInput, write_excel_image, \
    write_excel_json, write_excel_text

try:
    from crewai.tools import BaseTool as CrewAIBaseTool
except Exception:  # pragma: no cover
    class CrewAIBaseTool(BaseModel):
        model_config = ConfigDict(extra="allow")


class _BaseCompatTool(CrewAIBaseTool):
    model_config = ConfigDict(extra="allow")

    def _browser_owner(self) -> dict[str, str | None]:
        """Bind compatibility browser sessions to the canonical Crew execution."""
        process_id = getattr(self, "process_id", None)
        return {
            "execution_id": str(process_id) if process_id is not None else None,
            "actor": f"crewai:{process_id}" if process_id is not None else "crewai:local",
        }


class CustomAPICrewAITool(_BaseCompatTool):
    name: str = "HTTP: Send Request"
    description: str = "Send one HTTP request to an explicit API endpoint and return the status code plus parsed body."
    args_schema: Type[BaseModel] = CustomAPIInput
    url: str | None = None
    method: str | None = None
    headers: dict[str, str] | None = None
    auth: Any | None = None
    query_params: dict[str, Any] | None = None
    body: Any | None = None
    verify_ssl: bool = True

    def _run(self, **kwargs: Any) -> dict[str, Any]:
        return execute_custom_api(
            url=kwargs.get("url") or self.url,
            method=kwargs.get("method") or self.method or "GET",
            headers=kwargs.get("headers") or self.headers,
            query_params=kwargs.get("query_params") or self.query_params,
            body=kwargs.get("body", self.body),
            verify_ssl=kwargs.get("verify_ssl", self.verify_ssl),
            auth=kwargs.get("auth", self.auth),
            **kwargs,
        )


class FileWriteCrewAITool(_BaseCompatTool):
    name: str = "File: Write Text"
    description: str = "Write or append text content to a file under an allowed base folder."
    args_schema: Type[BaseModel] = FileWriteInput
    base_folder: str
    filename: str | None = None

    def _run(self, content: str, mode: str, filename: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return write_text_file(content=content, mode=mode, base_folder=self.base_folder, filename=filename,
                               default_filename=self.filename)


class HumanInputCrewAITool(_BaseCompatTool):
    name: str = "Human: Ask Operator"
    description: str = "Ask the human operator one focused question when execution is blocked or unsafe to infer."
    args_schema: Type[BaseModel] = HumanInputRequest
    timeout: int = 60

    def _run(self, query: str, **kwargs: Any) -> str:
        result = request_human_input(query=query, process_id=getattr(self, "process_id", 0), timeout=self.timeout)
        if result["status"] == "received":
            return f"Response received from human: '{result['response']}'"
        return "No response received from human within the timeout period."


class SaveMarkdownToWordCrewAITool(_BaseCompatTool):
    name: str = "Document: Markdown to Word"
    description: str = "Convert markdown text into a Word document and upload the generated .docx artifact."
    args_schema: Type[BaseModel] = SaveMarkdownToWordToolSchema
    filename: str | None = None
    img_directory: str | None = None

    def _run(self, **kwargs: Any) -> str:
        params = dict(kwargs)
        return save_markdown_to_word(
            markdown_text=params.pop("markdown_text", None),
            filename=params.pop("filename", None) or self.filename or "",
            img_directory=params.pop("img_directory", None) or self.img_directory or "",
            process_id=getattr(self, "process_id", None),
            run_by=getattr(self, "run_by", None),
            **params,
        )


class ExcelImageCrewAITool(_BaseCompatTool):
    name: str = "Excel: Write Image"
    description: str = "Embed one screenshot or image into an Excel worksheet row."
    args_schema: Type[BaseModel] = ExcelImageInput
    save_all_ss_ind: bool = False
    row_offset: dict[str, Any] | None = None
    header_title: str | None = None

    def _run(self, **kwargs: Any) -> Any:
        return write_excel_image(
            sheet_name=kwargs.get("sheet_name", ""),
            excel_file_path=kwargs.get("excel_file_path", ""),
            image_path=kwargs.get("image_path", ""),
            serial_number=kwargs.get("serial_number", 1),
            header_title=kwargs.get("header_title") or self.header_title,
            save_all_ss_ind=kwargs.get("save_all_ss_ind", self.save_all_ss_ind),
            row_offset=kwargs.get("row_offset") or self.row_offset,
        )


class ExcelJSONCrewAITool(_BaseCompatTool):
    name: str = "Excel: Write JSON"
    description: str = "Read a JSON file and write its keys and values into an Excel worksheet row."
    args_schema: Type[BaseModel] = ExcelJSONInput
    row_offset: dict[str, Any] | None = None

    def _run(self, **kwargs: Any) -> Any:
        return write_excel_json(
            json_file_path=kwargs.get("json_file_path", ""),
            sheet_name=kwargs.get("sheet_name", ""),
            excel_file_path=kwargs.get("excel_file_path", ""),
            serial_number=kwargs.get("serial_number", 1),
            row_offset=kwargs.get("row_offset") or self.row_offset,
        )


class ExcelTextCrewAITool(_BaseCompatTool):
    name: str = "Excel: Write Text"
    description: str = "Read a text file and insert its content into an Excel worksheet row."
    args_schema: Type[BaseModel] = ExcelTextInput
    row_offset: dict[str, Any] | None = None
    header_title: str | None = None

    def _run(self, **kwargs: Any) -> Any:
        return write_excel_text(
            text_file_path=kwargs.get("text_file_path", ""),
            sheet_name=kwargs.get("sheet_name", ""),
            excel_file_path=kwargs.get("excel_file_path", ""),
            serial_number=kwargs.get("serial_number", 1),
            header_title=kwargs.get("header_title") or self.header_title,
            row_offset=kwargs.get("row_offset") or self.row_offset,
        )


class BrowserOpenCrewAITool(_BaseCompatTool):
    name: str = "Browser: Open Page"
    description: str = "Start or reuse a browser session and navigate to a URL."
    args_schema: Type[BaseModel] = BrowserOpenInput
    url: str | None = None
    img_directory: str | None = None
    headless_mode: bool | None = None

    def _run(self, **kwargs: Any) -> Any:
        params = dict(kwargs)
        return open_browser(
            url=params.pop("url", None) or self.url,
            img_directory=params.pop("img_directory", None) or self.img_directory,
            headless_mode=params.pop("headless_mode", None) if "headless_mode" in params else self.headless_mode,
            _browser_owner=self._browser_owner(),
            **params,
        )


class BrowserScreenshotCrewAITool(_BaseCompatTool):
    name: str = "Browser: Capture Screenshot"
    description: str = "Capture a screenshot of the current browser page."
    img_directory: str | None = None
    full_page_screenshot: bool = True

    def _run(self, **kwargs: Any) -> Any:
        return screenshot(
            img_directory=kwargs.get("img_directory") or self.img_directory,
            full_page_screenshot=kwargs.get("full_page_screenshot", self.full_page_screenshot),
            session_id=kwargs.get("session_id"),
            process_id=getattr(self, "process_id", None),
            run_by=getattr(self, "run_by", None),
            _browser_owner=self._browser_owner(),
        )


class BrowserAnalyseCrewAITool(_BaseCompatTool):
    name: str = "Browser: Analyze Screenshot"
    description: str = "Capture and analyze the current page screenshot with the configured vision model."
    args_schema: Type[BaseModel] = BrowserAnalyseInput
    img_directory: str | None = None
    full_page_screenshot: bool = True

    def _run(self, **kwargs: Any) -> Any:
        return screenshot_and_analyse(
            text=kwargs.get("text", ""),
            img_directory=kwargs.get("img_directory") or self.img_directory,
            full_page_screenshot=kwargs.get("full_page_screenshot", self.full_page_screenshot),
            session_id=kwargs.get("session_id"),
            process_id=getattr(self, "process_id", None),
            run_by=getattr(self, "run_by", None),
            _browser_owner=self._browser_owner(),
        )


class BrowserExtractCrewAITool(_BaseCompatTool):
    name: str = "Browser: Extract From Screenshot"
    description: str = "Capture a screenshot and extract requested content from the visual page state."
    args_schema: Type[BaseModel] = BrowserExtractInput
    img_directory: str | None = None
    full_page_screenshot: bool = True

    def _run(self, **kwargs: Any) -> Any:
        return screenshot_and_extract(
            text=kwargs.get("text", ""),
            img_directory=kwargs.get("img_directory") or self.img_directory,
            full_page_screenshot=kwargs.get("full_page_screenshot", self.full_page_screenshot),
            session_id=kwargs.get("session_id"),
            process_id=getattr(self, "process_id", None),
            run_by=getattr(self, "run_by", None),
            _browser_owner=self._browser_owner(),
        )


class BrowserScrollCrewAITool(_BaseCompatTool):
    name: str = "Browser: Scroll Page"
    description: str = "Scroll the current browser page up or down."
    args_schema: Type[BaseModel] = BrowserScrollInput

    def _run(self, **kwargs: Any) -> Any:
        return scroll_page(
            scroll_direction=kwargs.get("scroll_direction", ""),
            instruction=kwargs.get("instruction"),
            session_id=kwargs.get("session_id"),
            _browser_owner=self._browser_owner(),
        )


class BrowserClickCrewAITool(_BaseCompatTool):
    name: str = "Browser: Click Element"
    description: str = "Click an element on the current page using a description or sequence number."
    args_schema: Type[BaseModel] = BrowserClickInput

    def _run(self, **kwargs: Any) -> Any:
        return click_element(
            instruction=kwargs.get("instruction", ""),
            sequence_number=kwargs.get("sequence_number"),
            x=kwargs.get("x"),
            y=kwargs.get("y"),
            session_id=kwargs.get("session_id"),
            _browser_owner=self._browser_owner(),
        )


class BrowserSelectCrewAITool(_BaseCompatTool):
    name: str = "Browser: Select Option"
    description: str = "Select an option from a dropdown on the current page."
    args_schema: Type[BaseModel] = BrowserSelectInput

    def _run(self, **kwargs: Any) -> Any:
        return select_dropdown(
            instruction=kwargs.get("instruction", ""),
            sequence_number=kwargs.get("sequence_number"),
            session_id=kwargs.get("session_id"),
            _browser_owner=self._browser_owner(),
        )


class BrowserInputCrewAITool(_BaseCompatTool):
    name: str = "Browser: Type Text"
    description: str = "Type text into an input field or textarea on the current page."
    args_schema: Type[BaseModel] = BrowserInputText

    def _run(self, **kwargs: Any) -> Any:
        return send_keys(
            instruction=kwargs.get("instruction", ""),
            sequence_number=kwargs.get("sequence_number"),
            session_id=kwargs.get("session_id"),
            _browser_owner=self._browser_owner(),
        )


class BrowserVerifyCrewAITool(_BaseCompatTool):
    name: str = "Browser: Verify Content"
    description: str = "Compare expected text or requirements against the current page content."
    args_schema: Type[BaseModel] = BrowserVerifyInput

    def _run(self, **kwargs: Any) -> Any:
        return verify_content(
            text=kwargs.get("text", ""),
            session_id=kwargs.get("session_id"),
            _browser_owner=self._browser_owner(),
        )


class BrowserTerminateCrewAITool(_BaseCompatTool):
    name: str = "Browser: Close Session"
    description: str = "Terminate the current browser session and release browser resources."

    def _run(self, **kwargs: Any) -> Any:
        return terminate_browser(**kwargs, _browser_owner=self._browser_owner())


def create_crewai_tool(tool_id: str, **params: Any):
    if tool_id == "agency.human.ask":
        return HumanInputCrewAITool(**params)
    if tool_id == "agency.http.request":
        return CustomAPICrewAITool(**params)
    if tool_id == "agency.file.write-text":
        return FileWriteCrewAITool(**params)
    if tool_id == "agency.document.markdown-to-word":
        return SaveMarkdownToWordCrewAITool(**params)
    if tool_id == "agency.excel.write-image":
        return ExcelImageCrewAITool(**params)
    if tool_id == "agency.excel.write-json":
        return ExcelJSONCrewAITool(**params)
    if tool_id == "agency.excel.write-text":
        return ExcelTextCrewAITool(**params)
    if tool_id == "agency.browser.open":
        return BrowserOpenCrewAITool(**params)
    if tool_id == "agency.browser.screenshot":
        return BrowserScreenshotCrewAITool(**params)
    if tool_id == "agency.browser.analyze-screenshot":
        return BrowserAnalyseCrewAITool(**params)
    if tool_id == "agency.browser.extract-screenshot":
        return BrowserExtractCrewAITool(**params)
    if tool_id == "agency.browser.scroll":
        return BrowserScrollCrewAITool(**params)
    if tool_id == "agency.browser.click":
        return BrowserClickCrewAITool(**params)
    if tool_id == "agency.browser.select-option":
        return BrowserSelectCrewAITool(**params)
    if tool_id == "agency.browser.type-text":
        return BrowserInputCrewAITool(**params)
    if tool_id == "agency.browser.verify-content":
        return BrowserVerifyCrewAITool(**params)
    if tool_id == "agency.browser.close":
        return BrowserTerminateCrewAITool(**params)
    raise KeyError(f"Unknown CrewAI tool '{tool_id}'")
