# ✦ 反重力助手 (Antigravity Assistant)

> **让手机化身你的本地 AI 编程与远程电脑操控中枢** —— 零第三方聊天软件依赖、免 API Key，完整同步并直驱电脑端 **Google Antigravity IDE** 与 **OpenAI Codex Desktop** 全部会话，支持 **低延迟局域网直连 + 全球 4G/5G 加密隧道**、**权限门控的远程触控桌面**、**电脑端 IDE 确认按钮一键放行**、**双向剪贴板与开发端口雷达**以及 **Hunk 级 Git Diff 精准引用追问**。

---

## 核心亮点

- **💻 电脑真实聊天窗口直连驱动 + 一次性令牌安全审批放行**
  - 手机端选中任意 **Antigravity 电脑端** 对话发送消息时，通过 Windows UI Automation **直接驱动电脑上打开的 Antigravity 窗口现场执行**（自动切换对应会话、规避中文输入法干扰、焦点与剪贴板序列号校验、实时回传工具状态、流式回复与 AI 截图），并支持在手机上一键**新建 Antigravity / Codex 电脑端对话**。
  - 投递回执严格区分为 **「已投递至电脑窗口」**（仅在桌面 UIA 成功接收后触发）、**「已启动电脑 CLI 进程」**（Codex/独立 CLI）与 **「⚠ 桌面拒收/投递失败」**，杜绝误报成功。
  - **电脑端确认弹窗一键放行**：当电脑端停在 `Proceed` / `Accept` / `Allow Once` / `Run Command` 等单次确认按钮时，手机端自动浮现绿色放行胶囊；采用绑定窗口句柄、会话 ID 与 UIA `RuntimeId` 的**一次性短效令牌**及无焦点 `InvokePattern` 安全触发，请求变更立即自动失效。
  - 自动发现并合并本机 **Antigravity IDE**（SQLite 索引 + 未入库 `brain/` 会话）与 **OpenAI Codex Desktop**（`state_*.sqlite` + `session_index.jsonl` + `codex exec` 原生续写）的全部对话。
- **📲 一次性配对码扫码 + 可选独立全屏 iOS App（需自行 Fork 开启）**
  - 默认采用 **高熵短效一次性配对码（`/#pair=...`） + 独立确认授权弹窗**，配对后下发 `HttpOnly; SameSite=Strict` 会话 Cookie，长期主密钥不再进入局域网链接或地址栏历史，电脑端点击「🔒 撤销」即可瞬间轮换密钥并断开全部设备。
  - 若需「iPhone 电脑重启免重扫」，需先 **Fork 本仓库到你自己的 GitHub** 并在 `.env` 中显式开启 `BEACON_ENABLED=true` 与 `BEACON_GIT_REMOTE=<你的仓库>`；默认 `BEACON_ENABLED=false` 不会尝试向上游仓库推送。
- **👆 默认收紧、按需放行的远程触控与双向剪贴板**
  - `REMOTE_DESKTOP_ENABLED`、`REMOTE_CLIPBOARD_ENABLED` 与 `AGY_DANGEROUSLY_SKIP_PERMISSIONS` 默认均为 **`false`**，可在桌面端「反重力助手」窗口随时实时切换授权并持久化到 `.env`。
  - **触控点按模式**：授权后实时远程桌面支持在「缩放查看」与「触控点按」间一键切换（自动扣除黑边进行 DPI 级精确坐标映射 + 脉冲点击动效 + 自动回传新帧），内置 `Enter` / `Esc` / `Space` / 上下滚轮快捷按键栏。
  - **电脑状态与双向剪贴板**：内置「电脑与剪贴板」中枢面板，实时查看 CPU / 内存 / 电量或磁盘空间、本机活跃开发监听端口（如 `:3000` / `:5173` / `:8765` 及所属进程），授权后支持读取/写入电脑剪贴板。
- **📂 工作区源码浏览与 Hunk 级 Git Diff 精准引用**
  - 手机端内置工作区抽屉：一键查看当前 Git 分支、未提交文件状态（`M` / `A` / `D`）与按 `@@ ... @@` 自动切分的彩色高亮 Diff 卡片，支持**按单个代码块（Hunk）一键引用到当前对话追问**或下载项目文件到手机。
- **🎨 最新版 ChatGPT iOS 悬浮圆钮、三段式导航与 Bento 空间界面**
  - **AMOLED 纯黑与高光折射悬浮圆钮**：采用 `#000000` 纯黑沉浸画布，顶部去除横栏分割线，配备 `42×42` 高光折射双悬浮圆钮；首页居中呈现双行问候语「很高兴见到您 / 我们从哪里继续呢？」与**叠层续接卡片堆（Stacked Deck）**，支持左右滑动切换并一键接力最近电脑会话或待办确认。
  - **单输入框三态自适应胶囊 + 底部三段式导航栏**：空首页呈现极简居中全圆角输入胶囊（`有问题，随便问`），聚焦或进入会话时无缝展开双层工具舱（保持同一 `<textarea>` 节点不断开中文输入法组合）；底部配备「🏠 首页 · 动态置顶位 · ⊞ 更多」三段式标签栏。
  - **2×4 Bento 空间网格与真·长按置顶**：「更多」页提供资料空间、项目、图像与桌面、Git 变更、Codex、待办审批、自定义、电脑与端口 8 大圆角卡片及实时搜索胶囊，**长按任意卡片即可将其置顶到底部导航栏与侧边栏快捷位**。
  - **纯黑悬浮双胶囊侧边抽屉**：侧栏采用极简无框单行会话流与底缘防遮挡渐隐遮罩，左下角悬浮亮天蓝「📝 新建」胶囊，右下角悬浮磨砂「🔍 搜索 / ⚙️ 设置」双图标胶囊。
  - **可折叠思考时间轴 & Canvas 隔离沙盒预览**：实时流式与电脑端历史会话均支持 `已完成 N 步操作 · X 秒 ›` 步骤时间轴；`HTML` / `SVG` 支持透明 `null` 源隔离沙盒与离线 `DOMParser` 前置严格 `CSP` 实时预览。
  - **严防分叉双写与断线自动重绑**：电脑端 IDE 会话严格绑定桌面窗口执行（绝不静默切换后台其他模型分叉双写），网关重启或锁屏唤醒重连后自动恢复原会话订阅与实时增量同步。
- **🪟 Windows 11 凝霜毛玻璃桌面中枢**
  - 打开电脑端「反重力助手」即在主面板内嵌展示双通道免密二维码，具备网关专属身份探针（防止端口冲突误映射其他本地程序到公网）、隧道断线自动自愈重启、窗口通透度调节与开机静默自启。

---

## 一键自动部署（支持 AI 智能体全自动安装）

你可以把下面这段指令直接发给任何电脑上的 **Antigravity / Claude Code / Codex / Cursor**，智能体会自动完成拉取、虚拟环境安装、外网隧道组件下载、桌面快捷方式创建与健康自检：

```text
请帮我部署「反重力助手」：克隆 https://github.com/wwd177/antigravity-remote-assistant ，阅读仓库根目录的 INSTALL_FOR_AI.md 并按步骤完成依赖安装、生成随机安全密钥、配置外网加密隧道、创建桌面快捷方式并运行 --doctor 自检。
```

### 手动快速启动（Windows PowerShell）

```powershell
git clone https://github.com/wwd177/antigravity-remote-assistant.git
cd antigravity-remote-assistant
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

启动桌面毛玻璃控制台：
```powershell
.\.venv\Scripts\pythonw.exe -X utf8 -m qq_agy_bridge.launcher
```

---

## 安全架构说明

- **一次性配对码 + `HttpOnly` 会话 Cookie + CSWSH 防护**：首次运行自动生成高熵 `WEB_ACCESS_TOKEN` 写入 `.env`；二维码采用短效单次 `/#pair=` 配对码并经确认后 `POST /api/pair` 换取 `SameSite=Strict; HttpOnly`（HTTPS 自动启用 `__Host-` + `Secure`）会话 Cookie；WebSocket 强制校验来源 `Origin`，页面启用 `no-referrer` 策略防止凭据泄漏。
- **默认最小权限门控**：`REMOTE_DESKTOP_ENABLED`、`REMOTE_CLIPBOARD_ENABLED`、`AGY_DANGEROUSLY_SKIP_PERMISSIONS` 与 `BEACON_ENABLED` 默认全部为 `false`，支持在电脑端「反重力助手」右上角按需实时开启并持久化到 `.env`，亦可一键「🔒 撤销」强制轮换密钥并断开全部设备。
- **防伪造速率限制与端口归属校验**：仅在配置或受信任环境下处理代理头，杜绝局域网伪造 IP 绕过鉴权锁定；启动外网隧道前强制校验 `/ping` 服务身份标识。
- **敏感文件沙箱与一次性动作绑定**：工作区文件浏览与下载严格限制在工作目录内，自动拦截 `.env`、SSH 私钥与凭据文件；桌面确认按钮采用绑定 `RuntimeId` 的一次性令牌，远程按键严格执行白名单校验。

---

## 开源协议

[MIT License](LICENSE)
