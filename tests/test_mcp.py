import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import Client, StdioServerParameters


def test_real_stdio_handshake_list_and_call(tmp_path):
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pavo_website_mcp.server"],
                                      env={**os.environ, "PAVO_STATE_DIR": str(tmp_path / "state")})
        async with Client(params) as client:
            listed = await client.list_tools()
            tools = {t.name: t for t in listed.tools}
            expected = {"pavo_server_status", "pavo_auth_import_authorization", "pavo_list_projects", "pavo_create_project", "pavo_get_project",
                        "pavo_upload_media", "pavo_list_nodes", "pavo_get_node", "pavo_create_node", "pavo_update_node", "pavo_list_models",
                        "pavo_get_model", "pavo_get_tool_specs", "pavo_connect_references", "pavo_list_connections", "pavo_validate_canvas",
                        "pavo_apply_canvas", "pavo_apply_template", "pavo_submit_task", "pavo_get_task", "pavo_sync_task_result",
                        "pavo_download_task_results", "pavo_save_node_asset", "pavo_list_assets", "pavo_list_artifacts", "pavo_get_operation"}
            assert expected <= tools.keys()
            script_tools = {"pavo_script_list_styles", "pavo_script_list_projects", "pavo_script_get_project", "pavo_script_get_episode",
                "pavo_script_create_ai", "pavo_script_import_text", "pavo_script_upload_file", "pavo_script_import_file",
                "pavo_script_update_outline", "pavo_script_update_episodes", "pavo_script_get_assets", "pavo_script_get_storyboard",
                "pavo_script_run_stage", "pavo_script_export"}
            script_tools.update({"pavo_script_create_asset", "pavo_script_rename_asset", "pavo_script_save_asset_state"})
            assert script_tools <= tools.keys()
            for name in ('pavo_script_import_text','pavo_script_import_file'):
                assert 'planned_episode_count' in tools[name].input_schema['required']
            assert tools["pavo_server_status"].annotations.read_only_hint
            assert not tools["pavo_submit_task"].annotations.read_only_hint
            status = await client.call_tool("pavo_server_status", {})
            assert not status.is_error
            assert status.structured_content["logged_in"] is False
            assert status.structured_content["api_base_url"] == "https://api.pavo-ai.cn"
            result = await client.call_tool("pavo_list_projects", {})
            assert result.is_error
            assert "未登录" in json.dumps(result.model_dump(), ensure_ascii=False)
            invalid = await client.call_tool("pavo_get_project", {"project_uuid": "../bad", "canvas_uuid": "c"})
            assert invalid.is_error
            assert "Traceback" not in json.dumps(invalid.model_dump())
            script_error = await client.call_tool("pavo_script_run_stage", {"project_id": "100", "stage": "generate_video", "operation_id": "bad-media"})
            assert script_error.is_error
            assert "阶段" in json.dumps(script_error.model_dump(), ensure_ascii=False)
            missing_count=await client.call_tool('pavo_script_import_text',{'raw_text':'剧情','title':'测试','ratio':'9:16','style':{'style_id':'unknown'},'operation_id':'missing-count'})
            assert missing_count.is_error
            assert 'planned_episode_count' in json.dumps(missing_count.model_dump())
    asyncio.run(exercise())
