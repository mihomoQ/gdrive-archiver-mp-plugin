# Google Drive 归档桥接（MoviePilot V2）

这是 `gdrive-archiver` 的 MoviePilot V2 事件桥接插件。它不运行 rclone，不访问 Google Drive，也不删除媒体文件。

插件用 `TransferComplete` 发现媒体根目录，并用 `MetadataScrape`（若 MP 启动刮削）在全部整理任务汇总、刮削已调度后重新开始等待。前者按主媒体文件触发，后者不是刮削完成事件；两者都以 `target_diritem.path`（MP 定义的媒体根目录）计算相对路径，写入同一个原子 job。宿主机仅在目录持续静默后递归上传，因此多集剧集、字幕、NFO 和图片会被合并为一次归档。

## Docker 挂载

MoviePilot 内的媒体路径是 `/media`，宿主机对应 `/data/media`。另建一个仅用于任务交换的目录，例如：

```yaml
volumes:
  - /data/media:/media
  - /data/gdrive-archiver/bridge:/bridge
```

宿主机的 `gdrive-archiver --config ...` 使用同一个 `/data/gdrive-archiver/bridge`，并将 job 的 `relative_path` 拼接到自己的媒体根目录 `/data/media`。不要把 rclone 配置或 Google OAuth token 挂进 MoviePilot 容器。

## 安装与配置

将本目录作为一个 MoviePilot 第三方插件仓库：

```text
mp-plugin/
├── package.v2.json
└── plugins.v2/gdrivearchiverbridge/__init__.py
```

插件市场安装后，填写：

| 配置 | 值 |
| --- | --- |
| `library_root` | `/media` |
| `bridge_dir` | `/bridge` |
| `enabled` | 开启 |

可选开启“读取宿主机结果通知”。此时插件每隔 `outbox_poll_seconds`（至少 10 秒）读取 `/bridge/outbox/*.json`，调用 MoviePilot 原有通知渠道后删除该结果文件。

宿主机写入 outbox 的结果文件使用原子重命名。实际协议为：

```json
{
  "version": 1,
  "job_id": "<inbox job id>",
  "status": "complete",
  "title": "Google Drive 上传完成",
  "relative_path": "日韩电影/血战冲绳岛 (1971) {tmdbid=130853}",
  "message": "已校验并移动到共享云端硬盘",
  "created_at": "2026-08-01T00:00:00Z"
}
```

## Inbox job 格式

```json
{
  "version": 1,
  "id": "sha256(relative_path)",
  "source_path": "/media/日韩电影/血战冲绳岛 (1971) {tmdbid=130853}",
  "relative_path": "日韩电影/血战冲绳岛 (1971) {tmdbid=130853}",
  "trigger": "transfer_complete",
  "completed_item": "血战冲绳岛 (1971) - 1080p - BluRay REMUX.mkv",
  "created_at": "2026-08-01T00:00:00+00:00"
}
```

宿主上传器始终以 `relative_path` 路由 Team Drive：它优先识别第一层类别；若第一层是旧整理规则留下的外层分组，则识别第二层类别。例如 `电影/日韩电影/片名` 与 `日韩电影/片名` 都会上传到“日韩电影”盘的 `片名` 路径。上传器必须等待目录稳定后再递归扫描，这样 MoviePilot 随后写入的 NFO、字幕和图片也会一起上传。
