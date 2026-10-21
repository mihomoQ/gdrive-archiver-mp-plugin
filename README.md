# Google Drive 归档桥接（MoviePilot V2 / V3）

插件负责媒体整理事件、归档仪表盘、控制请求和结果通知。上传器与 rclone 在宿主机运行；插件不访问 Google Drive、账号 `.conf` 或上传器私有 `state.json`，也不删除媒体文件。

## 安装

在 MoviePilot 插件市场添加仓库根地址 `https://github.com/mihomoQ/gdrive-archiver-mp-plugin`，刷新插件市场缓存后安装或更新。

| MoviePilot | 索引 | 自包含源码 | 插件版本 |
| --- | --- | --- | --- |
| V2，`>=2.0.0,<3.0.0` | `package.v2.json` | `plugins.v2/gdrivearchiverbridge/__init__.py` | 2.0.0 |
| V3，`>=3.0.0` | `package.v3.json` | `plugins.v3/gdrivearchiverbridge/__init__.py` | 3.0.0 |

V2 使用对应的历史宿主导入；V3 使用 `app.sdk.plugin`、`app.sdk.events` 和 `app.sdk.logging`。两个目录都能单独被 MP 安装，不需要安装另一版本的源码，无额外 Python 依赖。

Docker 挂载示例：

```yaml
volumes:
  - /data/media:/media
  - /var/lib/gdrive-archiver/bridge:/bridge
```

插件配置 `library_root=/media`、`bridge_dir=/bridge` 并启用。宿主机配置使用对应的实际路径。原生 Windows MP 则填写 Windows 的媒体根目录和 bridge 目录。容器与宿主机均需对 bridge 有读写权限；不要把账号目录挂进 MP。

## 仪表盘与控制

在 MP 首页的仪表盘设置中加入“Google Drive 归档”；插件详情页也可查看状态。仪表盘每 3 秒读取宿主机的 `bridge/status.json`，显示：

- 在线/离线、暂停状态、上传总速度与当前文件的阶段、进度、速度、ETA。
- 各状态任务总数、完成文件数、已处理字节和错误。宿主机公开快照最多列出 500 个任务，优先未完成任务；页面最多展示前 50 项，并提示截断。总数来自 `job_counts`，私有状态始终保留全部待处理任务；旧版状态缺少该字段时按可见任务汇总。
- 账号容量、OAuth client 来源、活动上传数、冷却倒计时及异常。
- 本机滚动 24 小时上传预算、已预留量和剩余估算；**这不是 Google 精确剩余额度**。
- 最近事件、提示及最近 20 个操作回执。

状态超过 15 秒或归档器停止时，界面标记离线并把速度清零。页面刷新不调用 Google API。原生详情页由 MP 控制刷新，持续查看请使用首页仪表盘。

控制入口在**插件设置**：选择操作、填写可选参数、勾选“执行一次”然后保存。

| 操作 | 参数/行为 |
| --- | --- |
| 暂停 | 停止派发新文件，当前文件完成后暂停 |
| 继续 | 恢复上传派发 |
| 重试 | 填任务 ID 重试该任务；留空重试所有异常/等待账号任务 |
| 检查账号 | 请求宿主机重新检查账号 |
| 手动入队 | 填媒体根目录下的目录，如 `欧美剧集/剧名`，不填绝对路径 |

操作写入后显示 `pending`。只有宿主机写回 `done` 才算已确认执行；`error` 显示失败原因。保存开关前会先清除“执行一次”，避免插件重载重复提交。旧版归档器没有状态协议 2 时，事件队列与通知仍可工作，但控制不可用。

## 媒体事件

`TransferComplete` 按主视频文件触发。插件取 `transferinfo.target_diritem.path` 作为媒体根目录，记录 `target_item.path` 对应的具体集/文件；`MetadataScrape` 的目录信号用于重新开始稳定等待，它不表示刮削已经完成。兼容对象与字典形态的事件参数，只处理配置媒体根目录下的本地目录。

同一个媒体目录合并为同一个任务，宿主机等待目录稳定后递归处理全部普通文件，包括 NFO、字幕和海报。`类别/片名` 与 `电影或剧集/类别/片名` 均保留相对路径，由宿主机按类别选择团队盘。安装前的旧媒体不会自动全盘入队，可用手动入队补充。

## 文件协议

任务与通知保留 `version: 1`；公开状态与控制使用 `version: 2`。

| 路径 | 写入方 | 用途 |
| --- | --- | --- |
| `bridge/inbox/*.json` | 插件 | 整理事件任务 |
| `bridge/status.json` | 宿主机 | 原子替换的公开状态快照 |
| `bridge/commands/<32位ID>.json` | 插件 | 带唯一 ID 的控制请求 |
| `bridge/results/<32位ID>.json` | 宿主机 | `done/error` 回执 |
| `bridge/outbox/*.json` | 宿主机 | 归档结果通知 |

任务格式示例：

```json
{
  "version": 1,
  "id": "<相对目录文本的 SHA256，不读取视频内容>",
  "source_path": "/media/欧美剧集/剧名",
  "relative_path": "欧美剧集/剧名",
  "trigger": "transfer_complete",
  "completed_item": "Season 1/S01E01.mkv",
  "created_at": "2026-10-21T00:00:00Z"
}
```

开启通知后，MP 公共服务每 2–5 秒读取 outbox。通知先认领，失败保留供重试；重启后会恢复遗留的 `.processing` 文件。插件保留最近 256 个已通知标记，避免正常重载与重复投递引起重复通知。发送成功但持久化标记前进程崩溃的极短窗口仍可能重复通知；不承诺跨服务恰好一次送达。无效 JSON 会隔离成 `.invalid` 文件，不会不断重试。

## 鉴权 API

三个端点均声明 `auth: bear`，由 MoviePilot 验证登录身份。请求/响应使用显式标准库 dataclass 模型，由宿主 FastAPI 生成 OpenAPI；不新增插件 HTTP 服务。

```text
GET  /api/v1/plugin/GDriveArchiverBridge/status
POST /api/v1/plugin/GDriveArchiverBridge/command
GET  /api/v1/plugin/GDriveArchiverBridge/result/{command_id}
```

POST JSON 示例：`{"action":"retry","job_id":"<64位任务ID>"}` 或 `{"action":"enqueue","relative_path":"欧美剧集/剧名"}`。

控制响应示例：

```json
{
  "id": "<32位命令ID>",
  "action": "pause",
  "status": "pending",
  "message": "已提交，等待宿主机确认",
  "created_at": "2026-10-21T00:00:00Z"
}
```

返回的是插件自有模型，不额外套宿主响应 envelope。分身实例的 URL 使用实际插件 ID。客户端应轮询对应回执，不能把 HTTP 成功理解为上传器已经执行。

## 验证与维护

在插件仓库根目录运行（归档器源码中先进入 `mp-plugin`）：

```text
python -m unittest discover -s tests -v
python -m compileall -q plugins.v2 plugins.v3
```

测试用宿主桩分别按 `app.plugins.gdrivearchiverbridge` 导入 V2/V3，覆盖事件路径、目录聚合、离线状态、控制回执、恢复通知、停用及版本一致性。修改逻辑时需同步两个自包含文件；测试会检查它们除宿主导入与版本号之外保持一致。

这些测试不等于真实 MP 进程联调。发布部署验收还需在 V2/V3 宿主确认市场安装、API 鉴权、服务重载及原生仪表盘渲染，并完成一次事件→归档→回执链路。

宿主接口依据：[V3 开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)、[V2 历史指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md)、[仪表盘约定](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/faq/08-render-dashboard.md)、[V3 API 响应约定](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V3_API_Response_Adaptation.md)。
