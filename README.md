# AI Camera · 本地 AI 监控摄像头

把一台普通电脑 + 一个普通摄像头，变成一个**完全离线**的智能监控摄像头：7×24 小时连续录像、
每天一个视频文件、自动保留 7 天、检测到人像自动截图与录制事件短片，全部在本机 CPU 上完成，
不依赖任何云服务。

```
    ┌────────────┐    ┌──────────────┐    ┌──────────────────────────────┐
    │  USB 摄像头 │──▶│  采集线程     │──┬─▶│ 每日录像  recordings/YYYY-MM-DD.mp4 │
    │ /dev/video0│    │  1280×720    │  │  └──────────────────────────────┘
    └────────────┘    └──────┬───────┘  │  ┌──────────────────────────────┐
                             │          ├─▶│ 预览 MJPEG  /stream.mjpg       │
                             ▼          │  └──────────────────────────────┘
                    ┌────────────────┐  │  ┌──────────────────────────────┐
                    │ 离线检测 4 fps  │◀─┘  │ 事件（截图 + 10s 短片）       │
                    │ SSD 人体 + YuNet│────▶│ SQLite + events/YYYY-MM-DD/  │
                    └────────────────┘     └──────────────────────────────┘
```

## 功能

- **一键开关**：Web 界面上的开关控制整个管线；关闭后摄像头停止采集（指示灯熄灭），不做任何录制。
- **持续录像**：固定帧率时钟写入，画面时间轴与真实时间严格一致；每天 00:00 自动换新文件。
- **7 天循环**：定时清理超过保留期的录像与事件；磁盘空间不足时优先删除最旧的数据。
- **离线人像检测**：MobileNet-SSD 检测人体 + YuNet 检测人脸，纯 CPU、约 4 次/秒、无网络请求。
- **事件留证**：检测到人像立刻保存带检测框的全分辨率截图，并录制一段 10 秒 MP4。
- **实时预览**：浏览器内 MJPEG 直播，叠加检测框、时间戳、当前人数。
- **事件与录像回放**：按日期筛选事件、弹窗播放事件短片与整日录像、直接下载。
- **断电/崩溃安全**：录像使用 fragmented MP4，进程被 kill 后文件依然可播放；写入卡死有看门狗自动重启编码器。
- **零前端依赖**：单页 UI 用原生 JS/CSS，局域网内手机也能直接看。

## 快速开始

```bash
# 1. 依赖：Python 3.10+、ffmpeg、可用的 /dev/videoX
sudo apt install ffmpeg python3-venv

# 2. 安装
git clone https://github.com/david4034647/ai-camera.git
cd ai-camera
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. 运行（首次启动会自动下载 23MB 模型）
.venv/bin/python -m app.main
```

打开 <http://localhost:8080> 即可。默认 `autostart: true`，启动服务就开始监控。
同一局域网内的手机访问 `http://<本机IP>:8080`。

### 安装为开机自启服务

```bash
sudo ./scripts/install.sh /opt/ai-camera      # 复制到 /opt 并注册 systemd
sudo systemctl status aicam
```

## 目录结构

```
app/
  main.py          FastAPI 路由（状态、开关、直播、事件、录像）
  camera.py        V4L2 采集线程 + 订阅分发
  recorder.py      ffmpeg 编码器、每日分段、事件短片、分段合并
  detector.py      MobileNet-SSD / YOLO ONNX 人体检测，YuNet 人脸检测
  monitor.py       检测循环、事件状态机、预览叠加、保留策略
  storage.py       SQLite 事件表
  model_manager.py 模型自动下载
  static/          单页 Web UI
tests/             无需摄像头的单元测试
data/              录像、截图、事件库（不进入 git）
models/            检测模型（不进入 git，自动下载）
```

## 配置

首次运行会生成 `config.json`（示例见 `config.example.json`）。修改后重启服务生效。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `host` / `port` | `0.0.0.0` / `8080` | 监听地址与端口 |
| `auth_token` | `""` | 非空时 API/直播需要 `?token=xxx` 或 `Authorization: Bearer xxx` |
| `camera_index` | `0` | `/dev/video0` 对应 0 |
| `capture_width` / `capture_height` | `1280` / `720` | 采集分辨率 |
| `capture_fps` | `30` | 请求的摄像头帧率（见下方 FAQ） |
| `record_width` / `record_height` | `960` / `540` | 录像分辨率（0 表示不缩放） |
| `record_fps` | `12` | 录像帧率 |
| `record_crf` / `record_maxrate` | `30` / `600k` | H.264 质量与码率上限 |
| `retention_days` | `7` | 录像与事件保留天数 |
| `min_free_gb` | `3` | 磁盘剩余低于该值时提前删除最旧数据 |
| `detect_fps` | `4` | 检测频率（次/秒） |
| `person_conf` | `0.45` | 人体检测置信度阈值 |
| `face_enabled` / `face_conf` | `true` / `0.6` | 是否检测人脸 |
| `event_clip_seconds` | `10` | 事件短片长度，0 表示只截图 |
| `event_gap_seconds` | `2.5` | 画面中无人多久后结束事件 |
| `autostart` | `true` | 服务启动时自动开始监控 |

### 存储估算

960×540 / 12 fps / CRF 30 在安静室内约 **150–300 kbps**，即每天 1.6–3 GB，7 天约 12–22 GB；
画面剧烈运动时最高 600 kbps，7 天上限约 45 GB。用 `df -h` 关注剩余空间，或调低
`record_maxrate` / `record_fps` 进一步压缩。

## 检测模型

| 模型 | 用途 | 体积 | 速度（4 核 CPU） | 许可 |
| --- | --- | --- | --- | --- |
| MobileNet-SSD (VOC) | 人体检测 | 23 MB | ~55 ms | MIT |
| YuNet 2023mar | 人脸检测 | 232 KB | ~25 ms | MIT |
| YOLOv8n / YOLO11n（可选） | 人体检测（更准） | ~12 MB | ~80–150 ms | AGPL-3.0（自行放置） |

把任意 `yolov8n.onnx`、`yolo11n.onnx` 或 `yolov5n.onnx` 放进 `models/`，程序会自动优先使用它，
无需改代码。

**隐私**：所有推理都在本机进程内完成，没有云端调用，也不做身份识别（只判断"有人/有人脸"）。
录像、截图、事件库全部写入本地 `data/`，不会上传。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/status` | 运行状态、帧率、检测延迟、存储占用 |
| POST | `/api/monitor/start` \| `/stop` \| `/toggle` | 开关监控 |
| GET | `/stream.mjpg` | MJPEG 实时画面（带检测框） |
| GET | `/api/snapshot.jpg` | 当前画面单帧截图 |
| GET | `/api/events?day=&limit=` | 事件列表 |
| GET | `/api/events/{id}` | 单条事件详情 |
| GET | `/api/events/{id}/snapshot` \| `/clip` | 事件截图 / 短片 |
| DELETE | `/api/events/{id}` | 删除事件及其文件 |
| GET | `/api/recordings` | 录像文件列表 + 存储统计 |
| GET | `/api/recordings/{name}` | 播放/下载整日录像（支持 Range） |
| GET | `/api/config` | 生效中的关键配置 |
| GET | `/api/health` | 健康检查 |

## 测试

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
```

## 常见问题

**打不开摄像头**：确认 `/dev/video0` 存在，且当前用户在 `video` 组：
`sudo usermod -aG video $USER` 后重新登录。

**帧率只有 8 fps**：部分 UVC 摄像头在请求 15 fps 时只协商到低帧率；本项目默认请求 30 fps，
实际得到的通常是 15 fps。可用 `ffmpeg -f v4l2 -list_formats all -i /dev/video0` 查看设备能力。

**录制文件能播一半吗**：可以。使用 fragmented MP4，异常退出后已写入的分片仍可播放；
下次启动会把同一天的多个分段自动合并成一个文件。

**能收到声音吗**：不能。本项目只处理视频轨道，麦克风不会被占用。

**OpenCV 5 报错 `readNetFromCaffe` 不存在**：Caffe 导入器在 OpenCV 5 中被移除，
`requirements.txt` 已固定 `opencv-python-headless<5`。

## 合法使用

请仅在你拥有或已获授权的场所使用本软件，并遵守当地关于隐私与监控的法律法规
（例如在办公场所明确告知员工、不在浴室/更衣室等私密区域安装）。作者不对滥用行为负责。

## License

MIT © david4034647
