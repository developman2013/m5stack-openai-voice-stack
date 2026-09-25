from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from app.ha_mcp import HomeAssistantMcpClient


@pytest.mark.asyncio
async def test_paginated_tools_keep_distinct_names_and_original_mapping():
    client = HomeAssistantMcpClient('http://ha/api/mcp', 'token')
    tool = lambda name: NS(name=name, description='Control light', inputSchema={'type':'object'})
    session = NS(list_tools=AsyncMock(side_effect=[
        NS(tools=[tool('light.on')], nextCursor='next'),
        NS(tools=[tool('light/on')], nextCursor=None),
    ]), call_tool=AsyncMock(return_value=NS(content=[NS(type='text', text='{"done":true}')], isError=False)))
    client._session = session
    tools = await client.get_openai_tools()
    assert len({t['name'] for t in tools}) == 2
    await client.call_openai_tool(tools[1]['name'], {'area':'bedroom'})
    session.call_tool.assert_awaited_once_with('light/on', arguments={'area':'bedroom'})


@pytest.mark.asyncio
async def test_mcp_error_is_not_reported_as_successful_json():
    client = HomeAssistantMcpClient('http://ha/api/mcp', 'token')
    client._tool_names = {'ha_light':'light'}
    client._session = NS(call_tool=AsyncMock(return_value=NS(
        isError=True, content=[NS(type='text', text='{"message":"offline"}')], structuredContent=None)))
    result = await client.call_openai_tool('ha_light', {})
    assert result['ok'] is False
    result = await client.call_openai_tool('unknown', {})
    assert result['ok'] is False
    assert client._session.call_tool.await_count == 1
