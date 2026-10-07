# nest-capture

Google Nest 摄像头 → 事件触发 → 自动抓片 → AI 审片 → 推送 Telegram 的完整管道。

## 为什么绕这么大圈

Google 的 SDM (Device Access) API **不给视频**（WebRTC 视频轨收不到帧、事件静态图被拒），
页面内的数据又出不来（CSP 拦 fetch/XHR、混合内容拦表单、Chrome 拦下载存盘）。
本项目用 **页面内 `MediaRecorder` 录原生流 + WebRTC 数据通道回传** —— 唯一能拿到
**连续画面**的通道，然后自动剪辑、审片、推送。

## 架构

```
sdm_events.py            # 常驻：守 Nest Pub/Sub 事件
   ├─ Person/门铃事件 → 写 .trigger，唤醒预录守护
   └─ 预录不可用 → 回退：开页面录制

nest_warm.py <camera>    # 常驻：每个相机一个
   ├─ 在【已打开的】相机页里连续录 10s × 6 段 ≈ 60–70s 环形缓冲
   ├─ 收到 .trigger → 取回缓冲 → 拼成 mp4
   └─ 交给 smooth_capture.py --from-clip

smooth_capture.py        # 一次性：规范化 + 审片 + 推送
   ├─ normalize_for_send()  # 恒定帧率 + faststart → Telegram 可拖进度条
   ├─ sample_frames()       # 抽 4 帧
   ├─ judge()               # 视觉模型 → NOTIFY / NOISE / UNCLEAR + SUMMARY 标题
   └─ judge_and_send()      # NOTIFY/UNCLEAR → 推视频；NOISE → 静默

nest_watchdog.py         # 定时：体检 + 保活
webdc_receiver.py        # WebRTC 数据通道收件（页面 → 本机）
make_clip.py             # 截图序列 → mp4（PyAV，无需系统 ffmpeg）
clip.py                  # 旧路径：开页面→截图→拼片（现为回退方案）
```

## 审片三态（投递规则）

| 判定 | 行为 |
| --- | --- |
| NOTIFY | 推视频 + 一句话标题 + 人影出现秒数 |
| UNCLEAR | **也推视频**（标 ⚠️），交给屋主自己看 |
| NOISE | **静默**，不打扰 |

推送正文格式：

```
🕒 2026-10-06 22:19:11 PDT
🏷️ 1名快递员走向门口
🎥 Garage camera · 20s（前 70s 预录）
👤 画面里有人出现在：第 3 秒、第 7 秒
```

## 部署

### 1. 依赖
```bash
python3 -m venv ~/.venvs/nest
~/.venvs/nest/bin/pip install av requests aiortc
```
（`av` 自带 libx264，**不需要系统 ffmpeg**）

### 2. 配置（本仓库不含真值，需自己建）
- `device-names.json` —— SDM 设备 id → 可读名字；格式见 `examples/device-names.example.json`
- `gh-cameras.json` —— 设备名 → Google Home 网页相机 id；见 `examples/gh-cameras.example.json`
- OAuth 凭据（client_secret / refresh_token）—— 由 `sdm_oauth.py` 首次授权生成，**切勿提交**
- `smooth_capture.py` 顶部常量需按自己环境改：
  - `TARGET` / `THREAD_ID` —— Telegram 群 / 论坛话题 id
  - `GH_BASE` —— Google Home 网页地址（含个人 id）

### 3. systemd（用户级）
```bash
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nest-events.service \
  nest-warm-garage.service nest-warm-doorbell.service nest-watchdog.timer
```

### 4. 前置条件
- **图形桌面 + Chrome 在运行**（抓流依赖相机页开着）
- OpenClaw Chrome 扩展已连接（`openclaw browser extension status`）
- OpenClaw CLI 可用于推送（`openclaw message send`）

## 发布前检查（若要转公开）
- [ ] `smooth_capture.py` 的 `TARGET` / `THREAD_ID` / `GH_BASE` 改成占位符
- [ ] 确认 `device-names.json` / `gh-cameras.json` / 任何 token 文件**未被提交**
- [ ] 凭据路径不含个人信息

## 环境变量（源码已脱敏，按需设置）

| 变量 | 用途 | 默认值 |
| --- | --- | --- |
| OPENCLAW_BIN | openclaw 可执行文件路径 | openclaw |
| NEST_TG_TARGET | Telegram 论坛群 id（抓拍事件话题） | 占位符 |
| NEST_TG_THREAD | 论坛话题 id | 占位符 |
| NEST_GH_BASE | Google Home 网页地址前缀（含个人 id） | 占位符 |
| NEST_TG_DM | 旧路径（clip.py）的 Telegram 私聊目标 | 占位符 |

systemd 单元已改用 %h（用户 home 占位符），不再含绝对路径。
