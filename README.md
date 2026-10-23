# Google Drive 归档桥接（MoviePilot V2 / V3）

插件负责媒体整理事件、归档仪表盘、控制请求和结果通知。上传器与 rclone 在宿主机运行；插件不访问 Google Drive、账号 `.conf` 或上传器私有 `state.json`，也不删除媒体文件。

## 安装

在 MoviePilot 插件市场添加仓库根地址 `https://github.com/mihomoQ/gdrive-archiver-mp-plugin`，刷新插件市场缓存后安装或更新。

| MoviePilot | 索引 | 自包含源码 | 插件版本 |
| --- | --- | --- | --- |
| V2，`>=2.0.0,<3.0.0` | `package.v2.json` | `plugins.v2/gdrivearchiverbridge/__init__.py` | 2.1.1 |
| V3，`>=3.0.0` | `package.v3.json` | `plugins.v3/gdrivearchiverbridge/__init__.py` | 3.1.1 |

V2 使用对应的历史宿主导入；V3 使用 `app.sdk.plugin`、`app.sdk.events` 和 `app.sdk.logging`。两个目录都能单独被 MP 安装，不需要安装另一版本的源码，无额外 Python 依赖。

Docker 挂载示例：

```yaml
volumes:
  - /data/media:/media
  - /var/lib/gdrive-archiver/bridge:/bridge
```

插件配置 `library_root=/media`、`bridge_dir=/bridge` 并启用。宿主机配置使用对应的实际路径。原生 Windows MP 则填写 Windows 的媒体根目录和 bridge 目录。容器与宿主机均需对 bridge 有读写权限；不要把账号目录挂进 MP。

## 仪表盘与控制

安装后先填写媒体根目录与 bridge，启用并保存插件，再打开插件设置点击 **“显示到首页”**。看到成功提示后刷新 MP 页面，即可在首页看到归档卡片。此按钮只为当前登录用户添加归档卡片，保留桌面、平板、手机布局中的其他组件、位置和显示选择。

MP 的首页布局按用户保存，插件没有“安装即强制插入首页”的公开钩子，因此首次需要点击这一次。若当前前端不支持该按钮，可在 MP 首页的仪表盘设置中手动勾选“Google Drive 归档”。按钮不会提交暂停、继续或上传命令。

首页每 3 秒读取 `bridge/status.json`，使用原生主题组件适配深浅色和手机屏幕：

- 四个关键指标：实际活动文件、当前速度、待处理任务、健康账号。暂停为橙色、离线为灰色，活动传输为绿色。
- 首页最多显示 3 条传输和 3 个账号，展示进度条、中文阶段、速度、分钟级 ETA 和预算；完整队列、账号、回执和事件放在详情页的折叠区域。
- 各状态任务总数、完成文件数、已处理字节和错误。宿主机公开快照最多列出 500 个任务，优先未完成任务；页面最多展示前 50 项，并提示截断。总数来自 `job_counts`，私有状态始终保留全部待处理任务；旧版状态缺少该字段时按可见任务汇总。
- 账号容量、OAuth client 来源、活动上传数、冷却倒计时及异常。
- 本机滚动 24 小时上传预算、已预留量和剩余估算；**这不是 Google 精确剩余额度**。
- 最近事件、提示及最近 20 个操作回执。

状态超过 15 秒或归档器停止时，界面标记离线并把速度清零。页面刷新不调用 Google API。原生详情页由 MP 控制刷新，持续查看请使用首页仪表盘。

原生 Go 归档器支持手动暂停和继续，通过共享 bridge 接收控制。MP 在 Docker 中或原生运行都可以使用，只需双方访问同一个 bridge；不需要额外的控制容器或 relay 服务。

### 手动暂停与继续

1. 在 MoviePilot 打开“Google Drive 归档桥接”的插件设置，保持“启用插件”开启。
2. 在“操作”中选择“暂停新上传”或“继续上传”。暂停/继续时，将“重试任务 ID（留空重试全部异常任务）”和“手动入队相对目录，例如 欧美剧集/剧名”留空。
3. 勾选“执行一次所选操作（保存后自动关闭）”，然后保存。
4. 回到首页“Google Drive 归档”仪表盘或插件详情页，查看“操作回执”。对应的 `pause` 或 `resume` 显示 `done`，才表示归档器已执行；`pending` 仍在等待，`error` 需按提示处理。

新安装默认不选择操作，避免误暂停；旧配置保留已选操作。每次执行前先确认选择，想继续时选择“继续上传”再勾选执行一次。普通保存且未勾选执行一次，不会提交控制。

暂停会停止新文件以及等待账号的文件开始上传；已经开始的上传会继续完成移动和本地清理。因此 `pause / done` 后可能仍看到当前文件传输，待它完成后活动传输数归零。

暂停状态保存在 Go 归档器中。重启服务、重启 MP、更新插件，或“执行一次”开关自动关闭，都不会恢复上传；需要明确执行“继续上传”。归档器暂停期间仍接收任务并更新状态。直接停止服务会退出进程，可能中断当前传输，适合维护或升级。

| 操作 | 参数/行为 |
| --- | --- |
| 暂停新上传 | 停止新文件开始上传，已开始的文件继续完成 |
| 继续上传 | 解除暂停，恢复上传派发 |
| 重试任务 | 填任务 ID 重试该任务；留空重试所有异常/等待账号任务，不解除全局暂停 |
| 重新检查账号 | 请求宿主机重新检查账号 |
| 手动添加媒体目录 | 填媒体根目录下的目录，如 `欧美剧集/剧名`，不填绝对路径，不解除全局暂停 |

插件在提交操作前清除“执行一次”开关，避免插件重载重复提交。旧版归档器没有状态协议 2 时，事件队列与通知仍可工作，但控制不可用。

### 判断是否正在上传

服务显示 `active` 只说明进程运行。面板按实际状态显示：活动为绿色，暂停为橙色，离线为灰色。过期记录不计入活动文件数或速度，未知 ETA 显示 `—`。

归档器原始任务状态 `uploading` 包含已准备、等待 worker 的批次。新版面板会结合实际活动文件区分“等待调度”“已暂停”和“上传中”，任务数量不等于并发数。`paused=true` 且活动文件数为 0 时，任务是在等待继续。

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

POST JSON 示例：暂停 `{"action":"pause"}`，继续 `{"action":"resume"}`，重试 `{"action":"retry","job_id":"<64位任务ID>"}`，手动入队 `{"action":"enqueue","relative_path":"欧美剧集/剧名"}`。这些请求也需要读取对应回执确认执行。

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
node tests/test_homepage_action.cjs
```

Node 测试仅用于开发验证首页布局合并，不是插件运行依赖。

测试用宿主桩分别按 `app.plugins.gdrivearchiverbridge` 导入 V2/V3，覆盖事件路径、目录聚合、离线状态、控制回执、恢复通知、停用及版本一致性。修改逻辑时需同步两个自包含文件；测试会检查它们除宿主导入与版本号之外保持一致。

这些测试不等于真实 MP 进程联调。发布部署验收还需在 V2/V3 宿主确认市场安装、API 鉴权、服务重载及原生仪表盘渲染，并完成一次事件→归档→回执链路。

宿主接口依据：[V3 开发指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/Plugin_Development.md)、[V2 历史指南](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V2_Plugin_Development.md)、[仪表盘约定](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/faq/08-render-dashboard.md)、[V3 API 响应约定](https://github.com/jxxghp/MoviePilot-Plugins/blob/main/docs/V3_API_Response_Adaptation.md)。
