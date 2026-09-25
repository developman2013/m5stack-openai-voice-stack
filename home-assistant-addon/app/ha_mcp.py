from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class HomeAssistantMcpClient:
    """Bridge Home Assistant's local MCP server to OpenAI function tools."""

    def __init__(self, url: str, token: str, timeout: float = 30.0) -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tool_names: dict[str, str] = {}

    async def connect(self) -> None:
        if not self.token:
            raise ValueError("Home Assistant token is empty")

        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=self.timeout,
                )
            )
            read_stream, write_stream, _ = await stack.enter_async_context(
                streamable_http_client(self.url, http_client=http_client)
            )
            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream, read_timeout_seconds=timedelta(seconds=self.timeout))
            )
            await session.initialize()
        except BaseException:
            # Transport failures cancel the owner task through AnyIO. Unwind
            # in this task so the HTTP task group does not leak its cancel scope.
            await stack.aclose()
            raise

        self._stack = stack
        self._session = session

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._session = None
        self._tool_names.clear()

    async def get_openai_tools(self) -> list[dict[str, Any]]:
        session = self._require_session()
        tools: list[dict[str, Any]] = []
        self._tool_names.clear()

        discovered = []
        cursor = None
        while True:
            result = await session.list_tools(cursor=cursor)
            discovered.extend(result.tools)
            cursor = result.nextCursor
            if not cursor:
                break
        for tool in discovered:
            openai_name = self._openai_tool_name(tool.name)
            self._tool_names[openai_name] = tool.name
            tools.append(
                {
                    "type": "function",
                    "name": openai_name,
                    "description": tool.description or "",
                    "parameters": tool.inputSchema,
                }
            )
        return tools

    async def get_prompt(self) -> str:
        session = self._require_session()
        prompts = await session.list_prompts()
        if not prompts.prompts:
            return ""

        result = await session.get_prompt(prompts.prompts[0].name)
        parts: list[str] = []
        for message in result.messages:
            text = getattr(message.content, "text", None)
            if text:
                parts.append(str(text))
        return "\n\n".join(parts)

    async def call_openai_tool(
        self, openai_name: str, arguments: dict[str, Any]
    ) -> Any:
        session = self._require_session()
        mcp_name = self._tool_names.get(openai_name)
        if mcp_name is None:
            return {"ok": False, "error": f"Unknown Home Assistant tool: {openai_name}"}

        result = await session.call_tool(mcp_name, arguments=arguments)
        text_parts = [
            str(content.text)
            for content in result.content
            if getattr(content, "type", None) == "text"
            and getattr(content, "text", None) is not None
        ]
        if not result.isError and len(text_parts) == 1:
            try:
                return json.loads(text_parts[0])
            except json.JSONDecodeError:
                pass

        payload: dict[str, Any] = {
            "ok": not bool(result.isError),
            "content": text_parts,
        }
        structured_content = getattr(result, "structuredContent", None)
        if structured_content is not None:
            payload["structured_content"] = structured_content
        return payload

    def _openai_tool_name(self, mcp_name: str) -> str:
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", mcp_name)
        base_name = f"ha_{safe_name}"[:64]
        candidate = base_name
        suffix = 2
        while candidate in self._tool_names:
            suffix_text = f"_{suffix}"
            candidate = f"{base_name[:64 - len(suffix_text)]}{suffix_text}"
            suffix += 1
        return candidate

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("Home Assistant MCP client is not connected")
        return self._session
