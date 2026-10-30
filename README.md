# Pavo 官网 MCP

独立操作 [Pavo 官网](https://app.pavo-ai.cn/)账号的本地 stdio MCP 服务。当前版本 **0.3.1**，共 **43 个工具**，涵盖画布、参考素材、生成任务、结果下载、官网资产，以及独立 AI 剧本和角色/场景/道具资料管理。

客户端直接实现官网 API，不依赖 PAVO CLI，也不使用 Agnes API Key。本项目是独立客户端，不由 Pavo 官方运营；官网接口可能变化，不能把本地测试通过当作所有生产业务已验证。

## 功能与边界

| 范围 | 已实现 |
| --- | --- |
| 画布 | 项目、节点、中文提示词、模型配置、参考连接、首尾帧和多图模板、批量预览及应用 |
| 媒体与生成 | 上传图片/视频/音频、提交任务、查询进度、结果回写、下载及 SHA 校验 |
| 官网资产 | 将已有节点资源保存到“我的资产”，查询图片/视频资产和历史产物 |
| AI 剧本 | 剧情文本/文件交由 Pavo 生成分集、大纲/人物小传/分集编辑、角色场景道具提取、分镜及导出 |
| 手动剧本资产 | 新增、改名、增加或编辑状态描述，设置参考图与可编辑的分集绑定 |
| 恢复保护 | 本地操作台账、同号复用、UNKNOWN 防重投、账号归属校验、原子下载 |

剧本内角色图、三视图、音色、场景图和道具图生成，以及删除、订阅购买等功能没有接入。画布生成工具不等于这些剧本内媒体接口。

## 环境与安装

支持 **macOS、Linux，Python 3.12 及以上**。跨进程锁使用 POSIX `fcntl`，暂不支持原生 Windows。先安装 Git 和 [uv](https://docs.astral.sh/uv/)，然后执行：

```sh
git clone https://github.com/congliu1851-ui/pavo-website-mcp.git
cd pavo-website-mcp
uv sync --frozen --all-groups
```

项目自带依赖锁，不需要修改其他 Python 项目的环境。没有安装 uv 时，也可自行创建虚拟环境后执行 `python -m pip install .`；开发和持续集成使用锁文件安装。

## 接入 Codex

在克隆目录执行：

```sh
codex mcp add pavo-website -- "$(pwd)/.venv/bin/python" -m pavo_website_mcp.server
```

其他 MCP 客户端使用以下配置，替换成自己机器的绝对路径：

```toml
[mcp_servers.pavo-website]
command = "/ABSOLUTE/PATH/pavo-website-mcp/.venv/bin/python"
args = ["-m", "pavo_website_mcp.server"]
```

配置示例另见 [codex-mcp.example.toml](codex-mcp.example.toml)。本服务不监听 HTTP 端口，stdout 只承载 MCP 协议。更新源码后，需重新连接 MCP，调用 `pavo_server_status` 核对运行版本；配置启用不等于当前会话已加载最新版。

## 登录认证

需要用户有权使用的官网 `Authorization: Bearer …`。推荐在本机终端输入，输入不会回显：

```sh
.venv/bin/python -m pavo_website_mcp.login
```

macOS 也可以运行 [登录Pavo.command](登录Pavo.command)。工具 `pavo_auth_import_authorization` 同样可导入用户明确提供的凭证，但工具输入可能留在客户端会话记录中，因此优先使用本机终端。

导入会先通过官网只读接口验证，失败不替换原会话。凭证和台账默认存于 `~/.local/share/pavo-website-mcp`，目录权限0700、会话与数据库0600；也可通过 `PAVO_STATE_DIR` 指定自己的私有目录。不要将该目录、Token、Cookie 或真实回执提交到仓库。

导入新账号影响后续新操作；已经开始的写操作固定使用开始时的账号认证，防止执行途中换号导致请求和台账归属不一致。JWT 到期时间只是未验证的本地提示，官网响应才是认证依据。

## 快速使用

### 画布生成

1. 读取 `pavo_server_status`、`pavo_list_projects` 和实时 `pavo_list_models`。
2. 创建项目，上传素材，保存真实项目/画布/节点 ID 和稳定素材地址。
3. 创建生成节点并填写模型、提示词和参数。首尾帧使用实时官网 guide 模板，不猜输入键名。
4. 预览批量修改，再执行 `pavo_validate_canvas`。预览只构造变更计划，不代替最终提交前的模型及参考输入校验。
5. 用户确定本次模型、参数和费用范围后，调用 `pavo_submit_task`，保存原生 task_id。
6. 查询任务，成功后分别回写、下载或保存官网资产。回写失败只重试回写，下载失败只恢复下载。

实时校验包括模型可用性、画幅、分辨率、时长，以及官网明确提供的参考类型、每类参考节点数和总数上限。文件本身的时长、分辨率、格式和人物对白质量仍需对应检查，不能凭“支持音频”推断支持准确台词或口型。

### 剧情交给 Pavo 分集

文本直接调用 `pavo_script_import_text`；文件先调用 `pavo_script_upload_file`，再将上传操作号交给 `pavo_script_import_file`。两个提交入口都使用官网 `projects/ai-generate`，将原剧情作为 `requirements`，由 Pavo 分配每集情节。

必须提供 `planned_episode_count`，范围1至100；官网当前接口不支持省略后自动决定总集数。AI 可能改写、扩写剧情，不是逐字导入。生成大纲后先读取真实进度，待确认时单独调用 `confirm_outline`，不会自动连续触发后续任务。

TXT 要求 UTF-8，支持 BOM；DOCX 必须为真实文档。文件32MiB、解压64MiB上限，原文连同标题与生成说明最多100000个UTF-16单位，超限拒绝且不截断。文件提交会核上传回执对应的本地原文件 SHA，因此原文件须仍存在且未修改。

## 操作回执与费用

所有写操作使用稳定的 `operation_id`。同号相同输入复用回执，同号不同输入或账号会拒绝。

| 状态 | 含义 |
| --- | --- |
| SUCCESS | 请求回执已保存；生成提交成功不等于生成完成 |
| FAILED | 明确的输入错误或官网拒绝；修正后再判断是否可用新操作号 |
| RUNNING / UNKNOWN | 执行中或结果未知；禁止换号重新提交，先查本地回执和官网真实状态 |

服务不会自动重投未知的收费请求，也不承诺官网没有提供的全局“只执行一次”。UNKNOWN 没有强行清空或跳过保护的工具，需要核对真实回执。模型价格、免费状态与账号权限均以调用时官网数据为准，剧本生成和解析不能默认免费。

## 文档

- [43个工具及参数](docs/工具参考.md)
- [画布、参考图与剧本调用示例](docs/使用示例.md)
- [认证、安全和故障恢复](docs/恢复与安全.md)
- [开发、测试及验证边界](docs/开发说明.md)
- [变更记录](CHANGELOG.md)、[贡献说明](CONTRIBUTING.md)、[安全报告](SECURITY.md)

## 验证与许可

```sh
uv run --frozen pytest -q
uv build
```

测试使用模拟官网响应、临时状态目录和真实 stdio 进程，不需要登录或消耗账号额度。发布前做过官网只读联调及专用测试画布的非生成联调；收费生成和所有剧本写入没有完整生产验收，具体边界见开发说明。

提供[macOS/Linux GitHub Actions模板](ci/github-actions.yml)。本次推送凭证不含workflow权限，因此模板未放入`.github/workflows/`，自动CI尚未启用，也没有GitHub Actions通过记录。仓库维护者取得相应权限后，可将模板复制到`.github/workflows/test.yml`再提交启用。

公开仓库不包含个人会话、生产回执、官方演示正文、下载的第三方官网脚本或本机路径。项目代码采用 [MIT 许可证](LICENSE)，不对外部平台、模型或用户素材授予额外权利。
