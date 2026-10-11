"""Exercise public FastMCP protocol behavior, not server-private registries."""
import asyncio
import base64
from pathlib import Path
import sys

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import freecad_bridge as bridge


def run(coroutine):
    return asyncio.run(coroutine)


def test_combined_discovery_and_metadata():
    async def check():
        async with Client(bridge.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert {'execute', 'get_screenshot', 'compare_print_candidates', 'bambu_optimize',
                    'bambu_compare_project', 'bambu_project_validate'} <= tools.keys()
            for name, tool in tools.items():
                assert tool.title or tool.annotations.title, name
                for field in ('read_only_hint', 'destructive_hint', 'idempotent_hint', 'open_world_hint'):
                    if field != 'destructive_hint' or not tool.annotations.read_only_hint:
                        assert getattr(tool.annotations, field) is not None, (name, field)
            assert tools['list_objects'].annotations.read_only_hint
            assert tools['execute'].annotations.open_world_hint
            assert tools['export_component'].annotations.destructive_hint
            assert not tools['get_screenshot'].annotations.read_only_hint  # temporary=False can affect view
            assert 'print' in tools['compare_print_candidates'].meta['fastmcp']['tags']
            resources = await client.list_resources()
            assert 'freecad://guide/cookbook' in {str(r.uri) for r in resources}
            cookbook = await client.read_resource('freecad://guide/cookbook')
            assert 'FreeCAD' in cookbook[0].text
    run(check())


def test_structured_results_and_errors(monkeypatch):
    async def fake(command):
        if command['type'] == 'get_object':
            return {'status': 'error', 'error': {'code': 'OBJECT_NOT_FOUND',
                    'message': 'No object named Missing', 'recoverable': True}}
        return {'status': 'success', 'result': {'objects': [{'name': 'Box'}]}}
    monkeypatch.setattr(bridge, 'send_to_freecad', fake)
    async def check():
        async with Client(bridge.mcp) as client:
            result = await client.call_tool('list_objects')
            assert result.structured_content == {'objects': [{'name': 'Box'}]}
            error = await client.call_tool('get_object', {'name': 'Missing'}, raise_on_error=False)
            assert error.is_error
            assert error.structured_content == {'error': {'code': 'OBJECT_NOT_FOUND',
                    'message': 'No object named Missing', 'recoverable': True}}
    run(check())


def test_images_remain_native_mcp_content(monkeypatch):
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6nJkAAAAASUVORK5CYII=')
    async def fake(command):
        return {'status': 'success', 'result': {'image_base64': base64.b64encode(png).decode()}}
    monkeypatch.setattr(bridge, 'send_to_freecad', fake)
    async def check():
        async with Client(bridge.mcp) as client:
            for tool in ('get_screenshot', 'render_component'):
                result = await client.call_tool(tool, {'component_id': 'component://D/C'} if tool == 'render_component' else {})
                assert result.content[0].type == 'image'
                assert result.content[0].mime_type == 'image/png'
                assert base64.b64decode(result.content[0].data) == png
    run(check())


def test_real_progress_events_without_exposing_context_in_schema(monkeypatch):
    async def fake(command):
        return {'status': 'success', 'result': {'built': True}}
    monkeypatch.setattr(bridge, 'send_to_freecad', fake)
    events = []
    async def progress(value, total, message):
        events.append((value, total, message))
    async def check():
        async with Client(bridge.mcp, progress_handler=progress) as client:
            result = await client.call_tool('execute', {'code': 'result = 1'})
            assert result.structured_content == {'built': True}
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert 'ctx' not in tools['execute'].input_schema['properties']
    run(check())
    assert events == [(0, 1, 'execute started'), (1, 1, 'execute completed')]


def test_stdio_entrypoint_uses_current_interpreter_and_combined_server():
    transport = PythonStdioTransport(Path(bridge.__file__), python_cmd=sys.executable,
                                    cwd=str(Path(__file__).resolve().parents[1]))
    async def check():
        async with Client(transport, timeout=15) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert {'list_objects', 'bambu_project_inspect', 'bambu_compare_project'} <= tools
    run(check())


def test_optional_nulls_match_the_advertised_schema(monkeypatch):
    async def fake(command):
        assert command['params']['about'] is None
        return {'status': 'success', 'result': {'volume': 10}}
    monkeypatch.setattr(bridge, 'send_to_freecad', fake)
    async def check():
        async with Client(bridge.mcp) as client:
            result = await client.call_tool('measure', {'a': 'Box', 'about': None})
            assert result.structured_content == {'volume': 10}
    run(check())


def test_failed_operation_does_not_report_completed(monkeypatch):
    async def fake(command):
        return {'status': 'error', 'error': {'code': 'BUILD_FAILED', 'message': 'invalid shape'}}
    monkeypatch.setattr(bridge, 'send_to_freecad', fake)
    events = []
    async def progress(value, total, message):
        events.append(message)
    async def check():
        async with Client(bridge.mcp, progress_handler=progress) as client:
            result = await client.call_tool('execute', {'code': 'invalid'}, raise_on_error=False)
            assert result.is_error and result.structured_content['error']['code'] == 'BUILD_FAILED'
    run(check())
    assert events == ['execute started']
