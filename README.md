# Google Drive 归档桥接（MoviePilot V2）

这是 `gdrive-archiver` 的 MoviePilot V2 事件桥接插件。它不运行 rclone，不访问 Google Drive，也不删除媒体文件。

插件监听 `EventType.TransferComplete`，从 `transferinfo.target_diritem.path` 取得 MoviePilot 的整理目标目录；以 `library_root`（默认 `/media`）计算相对路径，然后原子写入 `bridge_dir/inbox`（默认 `/bridge/inbox`）。同一目录的重复事件会覆盖为同一 job，因此可安全地由逐文件事件聚合为一个目录任务。

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
  "category": "日韩电影",
  "created_at": "2026-08-01T00:00:00+00:00"
}
```

`category` 只用于宿主上传器选择对应的 Team Drive；`relative_path` 的其余层级必须原样保留。上传器必须等待目录稳定后再递归扫描，这样 MoviePilot 随后写入的 NFO、字幕和图片也会一起上传。
