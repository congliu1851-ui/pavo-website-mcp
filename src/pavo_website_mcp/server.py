"""本地stdio MCP入口。stdout仅输出协议消息。"""

from functools import wraps
from typing import Any
import logging

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .canvas import Pavo
from .client import APIError, Client
from .store import OperationUnknown, Store
from .workflow import Workflow
from .script import Script
from .script_assets import ScriptAssets

store = Store()
client = Client(store)
pavo = Pavo(client, store)
workflow = Workflow(pavo)
script = Script(client, store)
script_assets = ScriptAssets(script)
server = MCPServer("pavo-website", title="Pavo官网", version=__version__, log_level="WARNING", instructions="操作Pavo官网账号。先读取实时项目、模型和模板。所有写操作使用稳定operation_id，UNKNOWN禁止换号重投。首尾帧使用官网实时guide模板。生成会消耗账号额度，须有用户的模型参数及费用授权。不得把模拟测试或工具启动当成生产业务验证。")


def tool(title, read_only=False, idempotent=False):
    def register(fn):
        @wraps(fn)
        def guarded(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, APIError, OperationUnknown) as e:
                raise ToolError(str(e)) from None
            except Exception as e:
                logging.getLogger("pavo").warning("工具内部错误类型：%s", type(e).__name__)
                raise ToolError("本地处理失败；写操作先查操作回执，不能自动重投。诊断日志仅保留错误类型。") from None
        return server.tool(title=title, annotations=ToolAnnotations(read_only_hint=read_only, destructive_hint=False, idempotent_hint=read_only or idempotent, open_world_hint=not read_only))(guarded)
    return register


@tool("连接和登录状态", read_only=True)
def pavo_server_status() -> dict[str, Any]:
    """读取脱敏登录状态，不访问官网、不返回凭证。JWT到期值仅用于本地提示，未做签名认证。"""
    return {"version": __version__, "api_base_url": client.base_url, "transport": "stdio", "state_dir": str(store.root), **store.status()}


@tool("导入官网Bearer认证")
def pavo_auth_import_authorization(authorization: str) -> dict[str, Any]:
    """仅接收用户明确提供的Authorization: Bearer值，通过官网只读项目接口核验后保存在本机0600会话文件；不回显凭证。禁止把凭证写到源码、配置示例或日志。"""
    return client.import_authorization(authorization)


@tool("读取项目列表", read_only=True)
def pavo_list_projects() -> dict[str, Any]:
    """读取当前官网登录账号的项目列表。"""
    return pavo.list_projects()


@tool("创建官网项目", idempotent=True)
def pavo_create_project(title: str, operation_id: str, cover_url: str = "") -> dict[str, Any]:
    """创建新项目；operation_id必须稳定，相同操作号相同输入返回已有真实回执。"""
    return pavo.create_project(title, operation_id, cover_url)


@tool("读取项目画布", read_only=True)
def pavo_get_project(project_uuid: str, canvas_uuid: str) -> dict[str, Any]:
    """显式读取项目和画布，返回原生节点、连接、最新version和官网链接。"""
    return pavo.get_project(project_uuid, canvas_uuid)


@tool("上传本地媒体", idempotent=True)
def pavo_upload_media(path: str, operation_id: str) -> dict[str, Any]:
    """上传明确指定的本地绝对路径图片、视频或音频，返回稳定public_url和SHA；不会自动创建节点或保存资产。"""
    return workflow.upload_media(path, operation_id)


@tool("读取画布节点", read_only=True)
def pavo_list_nodes(project_uuid: str, canvas_uuid: str) -> dict[str, Any]:
    """读取画布全部节点，节点data保留官网原始结构。"""
    return {"nodes": pavo.list_nodes(project_uuid, canvas_uuid)}


@tool("读取指定节点", read_only=True)
def pavo_get_node(project_uuid: str, canvas_uuid: str, node_key: str) -> dict[str, Any]:
    """按真实node_key读取节点，并解析字符串或对象形式的data。"""
    return pavo.get_node(project_uuid, canvas_uuid, node_key)


@tool("创建画布节点", idempotent=True)
def pavo_create_node(project_uuid: str, canvas_uuid: str, node_type: str, operation_id: str, name: str = "", prompt: str | None = None, model: str | None = None, params: dict | None = None, media_type: str = "image", url: str | None = None, x: float | None = None, y: float | None = None, dry_run: bool = False) -> dict[str, Any]:
    """创建text/image/video/audio/upload节点；upload使用上传返回的url和media_type。模型code从实时列表选择，params含duration/count/settings。dry_run仅预览，不写官网。"""
    return pavo.create_node(project_uuid, canvas_uuid, node_type, operation_id, name, prompt, model, params, media_type, url, x, y, dry_run)


@tool("更新节点和提示词", idempotent=True)
def pavo_update_node(project_uuid: str, canvas_uuid: str, node_key: str, operation_id: str, name: str | None = None, prompt: str | None = None, model: str | None = None, params: dict | None = None, data_patch: dict | None = None, dry_run: bool = False) -> dict[str, Any]:
    """保留未知字段并深合并指定参数。prompt只替换文字片段，保留skill及媒体。版本冲突若同字段已改则拒绝覆盖。"""
    return pavo.update_node(project_uuid, canvas_uuid, node_key, operation_id, name, prompt, model, params, data_patch, dry_run)


@tool("查询实时模型", read_only=True)
def pavo_list_models(scene_code: str) -> dict[str, Any]:
    """读取账号实时canvas_image、canvas_video或canvas_audio模型列表与约束；文字模型由tool-specs返回。"""
    return pavo.list_models(scene_code)


@tool("读取模型约束", read_only=True)
def pavo_get_model(scene_code: str, model_code: str) -> dict[str, Any]:
    """读取指定实时model_code的账号可用性、分辨率、画幅、时长、输入模式等约束。"""
    return pavo.get_model(scene_code, model_code)


@tool("读取官网实时模板", read_only=True)
def pavo_get_tool_specs(code: str | None = None) -> dict[str, Any]:
    """不传code返回完整官网tool-specs；传code返回指定模板。guide.extra.node_list中action_type=input的node_key就是真实输入键，不能猜first/last。"""
    return pavo.get_tool_specs(code)


@tool("连接参考素材", idempotent=True)
def pavo_connect_references(project_uuid: str, canvas_uuid: str, target_node_key: str, references: list[dict], operation_id: str, dry_run: bool = False) -> dict[str, Any]:
    """为节点添加有序图片/视频/音频参考。references每项含source=node_key、可选role=reference或prompt、media_order及原生handle/port。首尾帧必须用apply_template。不会替换已有连接。"""
    return pavo.connect_references(project_uuid, canvas_uuid, target_node_key, references, operation_id, dry_run)


@tool("读取参考连接", read_only=True)
def pavo_list_connections(project_uuid: str, canvas_uuid: str) -> dict[str, Any]:
    """读取权威connection_list，保留role、端口、media_order；不以节点data.source猜依赖。"""
    return {"connections": pavo.list_connections(project_uuid, canvas_uuid)}


@tool("预检画布", read_only=True)
def pavo_validate_canvas(project_uuid: str, canvas_uuid: str) -> dict[str, Any]:
    """核节点、依赖循环和已选择模型的实时约束；占位节点未选模型列警告。valid只表示已知协议预检，不等于生成或成片成功。"""
    return pavo.validate_canvas(project_uuid, canvas_uuid)


@tool("批量搭建画布", idempotent=True)
def pavo_apply_canvas(project_uuid: str, canvas_uuid: str, operations: list[dict], operation_id: str, dry_run: bool = True) -> dict[str, Any]:
    """批量create/update/connect，默认只预览。create含type、可选as别名/name/prompt/model/params/media_type/url；update含node_key和待改字段；connect含source/target/role/media_order，使用$name引用此前创建的别名。dry_run=false才写入。"""
    return pavo.apply_canvas(project_uuid, canvas_uuid, operations, operation_id, dry_run)


@tool("按实时模板绑定首尾帧", idempotent=True)
def pavo_apply_template(project_uuid: str, canvas_uuid: str, code: str, inputs: dict[str, str], operation_id: str, model: str | None = None, prompt: str | None = None, dry_run: bool = True) -> dict[str, Any]:
    """按官网guide构建首尾帧等多输入模板。inputs={官网实际输入node_key:已有素材node_key}，所有输入必须明确绑定，不使用模板示例素材。保留技能、原生连接和顺序，返回run_node_key；仅搭图，不生成。"""
    return pavo.apply_template(project_uuid, canvas_uuid, code, inputs, operation_id, model, prompt, dry_run)


@tool("提交生成任务", idempotent=True)
def pavo_submit_task(project_uuid: str, canvas_uuid: str, node_key: str, operation_id: str, regenerate: bool = False) -> dict[str, Any]:
    """会消耗官网账号额度，仅在用户明确授权模型、参数和费用范围后调用。只提交一次，立即保存真实task_id，不等待生成。UNKNOWN禁止重投。regenerate仅用于用户明确要求再次生成且原任务已终止的情况。"""
    return workflow.submit_task(project_uuid, canvas_uuid, node_key, operation_id, regenerate)


@tool("查询生成进度", read_only=True)
def pavo_get_task(task_id: str) -> dict[str, Any]:
    """按真实任务ID查询原生进度；found=false表示历史暂缺，不能据此重新生成。succeeded依据原生状态，不根据URL猜成功。"""
    return workflow.get_task(task_id)


@tool("回写任务状态和结果", idempotent=True)
def pavo_sync_task_result(submission_operation_id: str, operation_id: str) -> dict[str, Any]:
    """从本地已保存的生成提交回执定位准确项目和节点，查询并回写状态或成功产物；禁止覆盖后来修改的参数或其他任务。轮询状态变化后需要新的回写operation_id。"""
    return workflow.sync_task_result(submission_operation_id, operation_id)


@tool("下载真实生成结果", idempotent=True)
def pavo_download_task_results(task_id: str, output_dir: str) -> dict[str, Any]:
    """从原生成功任务的真实结果下载至绝对目录，逐项返回路径、字节数和SHA。复用经回执核SHA的已有文件，不覆盖其他文件；失败只重试下载，不重新生成。"""
    return workflow.download_task_results(task_id, output_dir)


@tool("保存节点资源到官网资产", idempotent=True)
def pavo_save_node_asset(project_uuid: str, canvas_uuid: str, node_key: str, operation_id: str, resource_index: int = 0, name: str = "") -> dict[str, Any]:
    """把节点指定的真实媒体资源保存到官网我的资产。上传资产链路为上传媒体、创建upload节点、保存节点资产。"""
    return workflow.save_node_asset(project_uuid, canvas_uuid, node_key, operation_id, resource_index, name)


@tool("读取账号资产", read_only=True)
def pavo_list_assets(category: str = "images", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """分页读取账号images或videos资产，保留服务端分页结构。"""
    return workflow.list_assets(category, page, page_size)


@tool("读取项目历史产物", read_only=True)
def pavo_list_artifacts(project_uuid: str, canvas_uuid: str, category: str = "all", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    """读取项目历史真实产物。category=all/images/videos；该接口按日期分组分页，total不是文件总数。"""
    return workflow.list_artifacts(project_uuid, canvas_uuid, category, page, page_size)


@tool("读取本地操作回执", read_only=True)
def pavo_get_operation(operation_id: str) -> dict[str, Any]:
    """查询本地持久化回执：SUCCESS为写请求回执成功，FAILED为明确拒绝，RUNNING/UNKNOWN不得重投。生成提交SUCCESS不等于任务已生成成功。"""
    return store.get_operation(operation_id)


@tool("读取剧本风格库", read_only=True)
def pavo_script_list_styles() -> dict[str, Any]:
    """读取独立AI剧本创作入口的实时风格库；使用真实style_id，不把画布模板或模型code当风格ID。"""
    return script.list_styles()


@tool("读取AI剧本项目", read_only=True)
def pavo_script_list_projects(page: int = 1, page_size: int = 12, is_demo: bool = False) -> dict[str, Any]:
    """分页查询官网/playlet/list剧本项目。is_demo=true读取官方只读演示，不可修改。"""
    return script.list_projects(page, page_size, is_demo)


@tool("读取剧本内容和进度", read_only=True)
def pavo_script_get_project(project_id: str, include_content: bool = True) -> dict[str, Any]:
    """读取剧本原生状态、active_tasks、任务ID、大纲和分集。返回outline及episodes指纹，编辑前必须核对；无任务不代表未知提交未发生。include_content=false的结果不能用来覆盖完整正文。"""
    return script.get_project(project_id, include_content)


@tool("读取剧本指定分集", read_only=True)
def pavo_script_get_episode(project_id: str, episode_id: str) -> dict[str, Any]:
    """核对项目归属后读取指定集的原生正文和状态，不能跨项目误用分集ID。"""
    return script.get_episode(project_id, episode_id)


@tool("AI创作剧本", idempotent=True)
def pavo_script_create_ai(requirements: str, planned_episode_count: int, ratio: str, style: dict, operation_id: str) -> dict[str, Any]:
    """调用官网AI剧本生成，可能消耗额度，需用户授权本次创作与费用。计划1至100集、画幅9:16或16:9，style使用实时style_id或custom_style_prompt。只提交一次并返回真实project_id/task_ids，不自动确认大纲或启动后续媒体。"""
    return script.create_ai(requirements, planned_episode_count, ratio, style, operation_id)


@tool("提交剧情由Pavo生成分集", idempotent=True)
def pavo_script_import_text(raw_text: str, title: str, ratio: str, style: dict, operation_id: str, planned_episode_count: int) -> dict[str, Any]:
    """把未分集的剧情作为requirements交给官网AI生成剧本，由Pavo安排每集内容，不再走完整剧本paste解析。planned_episode_count为用户指定的1至100集，官网接口不支持省略；连同标题及分集说明最多100000个UTF-16单位，不截断原文。可能消耗额度，需本次生成授权；只返回真实创建回执，不自动确认大纲或启动媒体。"""
    return script.import_text(raw_text, title, ratio, style, operation_id, planned_episode_count)


@tool("上传TXT或DOCX剧本", idempotent=True)
def pavo_script_upload_file(path: str, operation_id: str) -> dict[str, Any]:
    """准备步骤：上传本地非空UTF-8 TXT或真实DOCX并检查，返回file_url和SHA；文件32MiB、解压64MiB上限，不自动创建剧本。后续用import_file及计划集数交给Pavo AI生成分集；保留本地源文件用于核SHA及读取剧情。不发送Bearer到存储，inspect按用户文件上传授权调用。"""
    return script.upload_file(path, operation_id)


@tool("从上传剧情由Pavo生成分集", idempotent=True)
def pavo_script_import_file(upload_operation_id: str, ratio: str, style: dict, operation_id: str, planned_episode_count: int) -> dict[str, Any]:
    """使用本账号成功上传回执，核本地源文件SHA并提取剧情，以requirements调用官网AI生成分集，不再调用projects/upload完整剧本解析。兼容原上传回执，但源文件须仍存在且未改；不下载任意URL或重新上传。计划集数1至100；可能消耗额度，需本次生成授权，不自动确认大纲。"""
    return script.import_file(upload_operation_id, ratio, style, operation_id, planned_episode_count)


@tool("编辑剧本大纲", idempotent=True)
def pavo_script_update_outline(project_id: str, patch: dict, expected_sha256: str, operation_id: str) -> dict[str, Any]:
    """根据get_project返回的outline指纹修改指定字段。支持story_type/target_audience/one_line_story/story_summary/world_setting/character_biographies(name,bio)，保留其余内容，不自动确认大纲。官网无version CAS，避免同时在网页编辑。"""
    return script.update_outline(project_id, patch, expected_sha256, operation_id)


@tool("编辑和增加剧本分集", idempotent=True)
def pavo_script_update_episodes(project_id: str, episodes: list[dict], new_episodes: list[dict], expected_sha256: str, operation_id: str) -> dict[str, Any]:
    """使用get_project完整内容返回的episodes指纹。episodes每项含episode_id及title/content修改；未指定的已有分集保留。new_episodes含episode_no/title/content。只保存，不自动触发资产提取或后续生成。"""
    return script.update_episodes(project_id, episodes, new_episodes, expected_sha256, operation_id)


@tool("读取剧本角色场景道具", read_only=True)
def pavo_script_get_assets(project_id: str) -> dict[str, Any]:
    """读取characters/scenes/props与各状态，额外返回_mcp_fingerprints用于手动编辑前核对；不重新提取或生成图片。"""
    return script_assets.get(project_id)


@tool("手动新增角色场景道具", idempotent=True)
def pavo_script_create_asset(project_id: str, asset_type: str, name: str, operation_id: str, description: str = "", state_name: str = "默认") -> dict[str, Any]:
    """手动新增character角色、scene场景或prop道具及初始状态描述。按原生name/title字段保存，返回真实asset_id/state_id，不生成图片、不自动同步到后续制作。官网只读演示拒绝写入。"""
    return script_assets.create(project_id, asset_type, name, operation_id, description, state_name)


@tool("重命名角色场景道具", idempotent=True)
def pavo_script_rename_asset(project_id: str, asset_type: str, asset_id: str, name: str, expected_sha256: str, operation_id: str) -> dict[str, Any]:
    """核本项目、本类型资产及get_assets返回的asset指纹后，只修改角色name或场景/道具title，不改状态描述、图片或绑定。"""
    return script_assets.rename(project_id, asset_type, asset_id, name, expected_sha256, operation_id)


@tool("新增或编辑角色场景道具状态", idempotent=True)
def pavo_script_save_asset_state(project_id: str, asset_type: str, asset_id: str, patch: dict, operation_id: str, state_id: str | None = None, expected_sha256: str | None = None) -> dict[str, Any]:
    """不传state_id时新增状态，须省略expected_sha256，patch须含state_name和description；编辑已有状态须传真实state_id及对应states指纹。可改state_name/description/episode_nos/reference_images，参考图使用已上传的HTTPS公网地址。完整保留未改的图片、模型、样式与可编辑绑定，按原生POST states upsert保存；不会发起图片生成。"""
    return script_assets.save_state(project_id, asset_type, asset_id, patch, operation_id, state_id, expected_sha256)


@tool("读取剧本分集分镜", read_only=True)
def pavo_script_get_storyboard(project_id: str, episode_id: str) -> dict[str, Any]:
    """核项目归属后读取原生段落、场景、镜头及active_tasks。生成分镜需单独调用run_stage，不会自动制作图片或视频。"""
    return script.get_storyboard(project_id, episode_id)


@tool("执行指定剧本阶段", idempotent=True)
def pavo_script_run_stage(project_id: str, stage: str, operation_id: str, episode_id: str | None = None, regenerate: bool = False) -> dict[str, Any]:
    """可能消耗额度，需用户明确授权本阶段。stage支持confirm_outline/extract_assets/finalize_assets/generate_episode/generate_storyboard/resume_episodes/retry_outline/retry_episodes。finalize_assets显式同步资产(is_manual=true)，不与手动保存捆绑；可能推进后续阶段。generate_episode和generate_storyboard必须提供归属正确的episode_id。复核只读、原生状态及运行任务；UNKNOWN不得换号重投。不是视频、关键帧或合成接口。"""
    return script.run_stage(project_id, stage, operation_id, episode_id, regenerate)


@tool("导出剧本和分镜到本地", idempotent=True)
def pavo_script_export(project_id: str, output_dir: str, format: str = "markdown", include_storyboards: bool = False) -> dict[str, Any]:
    """读取原生剧本内容后导出到绝对目录，支持json/markdown/doc。doc为与官网同类的Word兼容HTML，不冒称DOCX。可附原生分镜数据，返回SHA和状态；不会触发生成、视频导出或覆盖不同内容的文件。"""
    return script.export(project_id, output_dir, format, include_storyboards)


def main():
    try:
        server.run(transport="stdio")
    finally:
        client.close()


if __name__ == "__main__":
    main()
