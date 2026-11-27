"""Automated cross-machine installer, configurator, and health doctor for 反重力助手 (Antigravity Assistant).

Designed for BOTH human users and autonomous AI coding agents (Antigravity, Claude Code,
Cursor, Codex, Windsurf, Cline) deploying this repository on a fresh computer.

Examples:
  python -m qq_agy_bridge.setup_wizard --non-interactive --with-tunnel --create-shortcut --doctor
  python -m qq_agy_bridge.setup_wizard --doctor --json
"""

from __future__ import annotations

import argparse
import json
import platform
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from .config import BridgeConfig, resolve_agy_executable
from .conversations import list_local_conversations
from .web_pwa import get_lan_ipv4

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
LEGACY_SHORTCUTS = ("QQ Antigravity 机器人.lnk", "Antigravity 远程助理.lnk")


def _update_env_key(lines: list[str], key: str, value: str, *, only_if_empty: bool = False) -> list[str]:
    prefix = f"{key}="
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(prefix):
            cur_val = stripped[len(prefix) :].strip()
            if only_if_empty and cur_val and cur_val != "change-me":
                return lines
            lines[idx] = f"{key}={value}"
            return lines
    lines.append(f"{key}={value}")
    return lines


def ensure_env_configured(
    project_dir: Path = PROJECT_ROOT,
    *,
    force_overwrite: bool = False,
    workspace: str = "",
    pwa_port: int = 0,
) -> Path:
    """Create or update .env safely with strong cryptographic tokens."""
    env_path = project_dir / ".env"
    example_path = project_dir / ".env.example"

    if not env_path.is_file() or force_overwrite:
        if example_path.is_file():
            base_text = example_path.read_text(encoding="utf-8")
        else:
            base_text = "WEB_PWA_ENABLED=true\nWEB_PWA_PORT=8765\nCLOUDFLARE_TUNNEL_ENABLED=true\n"
        env_path.write_text(base_text, encoding="utf-8")

    # Strip deprecated QQ / OneBot keys cleanly while preserving user's WEB_ACCESS_TOKEN
    deprecated_prefixes = (
        "ONEBOT_",
        "ALLOWED_QQ_USERS=",
        "BOT_QQ=",
        "NAPCAT_DIR=",
        "CLEAN_QQ_FORMATTING=",
        "MAX_MESSAGE_LENGTH=",
        "MESSAGE_DEBOUNCE_SECONDS=",
        "AGY_PREWARM_USERS=",
    )
    lines = [
        ln
        for ln in env_path.read_text(encoding="utf-8").splitlines()
        if not any(ln.strip().startswith(p) for p in deprecated_prefixes)
    ]
    lines = _update_env_key(lines, "WEB_ACCESS_TOKEN", secrets.token_urlsafe(18), only_if_empty=True)
    lines = _update_env_key(lines, "AGY_SYSTEM_PROMPT_FILE", "prompts/persona.md")

    if workspace.strip():
        lines = _update_env_key(lines, "AGY_WORKSPACE", str(Path(workspace.strip()).expanduser().resolve()))
    if pwa_port > 0:
        lines = _update_env_key(lines, "WEB_PWA_PORT", str(pwa_port))

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    mem_target = project_dir / "memory" / "user_profile.md"
    mem_example = project_dir / "memory" / "user_profile.example.md"
    if not mem_target.is_file() and mem_example.is_file():
        mem_target.parent.mkdir(parents=True, exist_ok=True)
        mem_target.write_text(mem_example.read_text(encoding="utf-8"), encoding="utf-8")

    return env_path


def ensure_cloudflared(project_dir: Path = PROJECT_ROOT) -> tuple[bool, str]:
    """Locate or download official cloudflared binary for encrypted 4G/5G tunnel access."""
    bin_dir = project_dir / "bin"
    exe_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    local_bin = bin_dir / exe_name
    if local_bin.is_file():
        return True, str(local_bin)

    sys_found = shutil.which("cloudflared")
    if sys_found:
        return True, sys_found

    machine = platform.machine().lower()
    is_arm = "arm" in machine or "aarch64" in machine
    if sys.platform == "win32":
        asset = "cloudflared-windows-arm64.exe" if is_arm else "cloudflared-windows-amd64.exe"
    elif sys.platform == "linux":
        asset = "cloudflared-linux-arm64" if is_arm else "cloudflared-linux-amd64"
    else:
        return False, "macOS 请运行 `brew install cloudflared` 安装外网隧道组件（局域网扫码无需此组件）"

    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
        tmp_dest = bin_dir / f"{exe_name}.part"
        req = urllib.request.Request(url, headers={"User-Agent": "Antigravity-Assistant-Setup/2.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, tmp_dest.open("wb") as out:
            shutil.copyfileobj(resp, out)
        tmp_dest.replace(local_bin)
        if sys.platform != "win32":
            local_bin.chmod(0o755)
        return True, str(local_bin)
    except Exception as exc:
        return False, f"自动下载 cloudflared 失败（不影响同 Wi-Fi 局域网使用）：{exc}"


def create_windows_desktop_shortcut(
    project_dir: Path = PROJECT_ROOT,
    shortcut_name: str = "反重力助手.lnk",
) -> tuple[bool, str]:
    """Create a Windows Desktop .lnk pointing to 反重力助手 and clean up legacy shortcuts."""
    if sys.platform != "win32":
        return False, "桌面 .lnk 快捷方式仅适用于 Windows"

    venv_pyw = project_dir / ".venv" / "Scripts" / "pythonw.exe"
    target_exe = venv_pyw if venv_pyw.is_file() else Path(sys.executable)
    icon_path = project_dir / "assets" / "app.ico"

    cleanup_cmds = "; ".join(
        f"$old = Join-Path $desk '{old}'; if (Test-Path $old) {{ Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue }}"
        for old in LEGACY_SHORTCUTS
        if old != shortcut_name
    )
    ps_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$desk = [Environment]::GetFolderPath('Desktop'); "
        f"{cleanup_cmds}; "
        "Get-ChildItem -LiteralPath $desk -Filter '*.lnk' -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'Antigravity|\\ufffd' } | Remove-Item -Force -ErrorAction SilentlyContinue; "
        f"$lnkPath = Join-Path $desk '{shortcut_name}'; "
        "$ws = New-Object -ComObject WScript.Shell; "
        "$s = $ws.CreateShortcut($lnkPath); "
        f"$s.TargetPath = '{target_exe}'; "
        "$s.Arguments = '-X utf8 -m qq_agy_bridge.launcher'; "
        f"$s.WorkingDirectory = '{project_dir}'; "
        f"if (Test-Path '{icon_path}') {{ $s.IconLocation = '{icon_path},0' }}; "
        "$s.Description = '反重力助手 — 电脑全会话同步与移动端远程控制中枢'; "
        "$s.Save(); "
        "Write-Output $lnkPath"
    )
    import base64 as _b64
    encoded_cmd = _b64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded_cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout.strip():
            return True, res.stdout.strip().splitlines()[-1]
        return False, (res.stderr or "创建快捷方式失败").strip()
    except Exception as exc:
        return False, str(exc)


def run_doctor(project_dir: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Run comprehensive environment diagnostics for human or AI agent verification."""
    env_path = project_dir / ".env"
    checks: list[dict[str, Any]] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        {
            "name": "python_version",
            "ok": py_ok,
            "detail": f"Python {platform.python_version()} ({sys.executable})",
        }
    )

    env_vals = dotenv_values(env_path) if env_path.is_file() else {}
    agy_cfg = str(env_vals.get("AGY_EXECUTABLE") or "agy")
    agy_resolved = resolve_agy_executable(agy_cfg)
    agy_exists = bool(shutil.which(agy_resolved) or Path(agy_resolved).is_file())
    agy_ver = ""
    if agy_exists:
        try:
            extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            r = subprocess.run(
                [agy_resolved, "--version"],
                capture_output=True,
                text=True,
                timeout=8,
                **extra,
            )
            agy_ver = (r.stdout or r.stderr or "").strip().splitlines()[0] if (r.stdout or r.stderr) else "ok"
        except Exception as exc:
            agy_ver = f"warning: {exc}"
    checks.append(
        {
            "name": "antigravity_cli",
            "ok": agy_exists,
            "detail": f"{agy_resolved} ({agy_ver})" if agy_exists else "未找到 agy，请先安装并登录 Antigravity IDE 或 CLI",
        }
    )

    cfg_ok = False
    pwa_lan_url = ""
    if env_path.is_file():
        try:
            cfg = BridgeConfig.from_env(env_path)
            cfg_ok = cfg.web_pwa_enabled and bool(cfg.web_access_token)
            lan_ip = get_lan_ipv4()
            if cfg_ok:
                pwa_lan_url = f"http://{lan_ip}:{cfg.web_pwa_port}/"
        except Exception:
            cfg_ok = False

    checks.append(
        {
            "name": "env_config",
            "ok": cfg_ok,
            "detail": ".env 配置已就绪（强随机访问密钥已启用）" if cfg_ok else ".env 未初始化",
        }
    )

    cf_bin = project_dir / "bin" / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")
    cf_path = str(cf_bin) if cf_bin.is_file() else (shutil.which("cloudflared") or "")
    checks.append(
        {
            "name": "cloudflare_tunnel_optional",
            "ok": True,
            "installed": bool(cf_path),
            "detail": cf_path if cf_path else "未安装（同 Wi-Fi 局域网低延迟扫码不受影响）",
        }
    )

    convs = list_local_conversations(limit=500)
    checks.append(
        {
            "name": "local_conversations",
            "ok": True,
            "detail": f"已发现本机对话共 {len(convs)} 个（Antigravity {sum(1 for x in convs if x.store=='ide')} · Codex {sum(1 for x in convs if x.store=='codex')} · 远程 {sum(1 for x in convs if x.store=='cli')}）",
        }
    )

    overall = all(bool(c["ok"]) for c in checks)
    return {
        "ok": overall,
        "app_name": "反重力助手",
        "project_dir": str(project_dir),
        "pwa_lan_url": pwa_lan_url,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="反重力助手 (Antigravity Assistant) — 自动初始化与自检工具")
    parser.add_argument("--non-interactive", action="store_true", help="无人值守一键配置（适合 AI 智能体或脚本）")
    parser.add_argument("--workspace", default="", help="默认工作区目录路径")
    parser.add_argument("--port", type=int, default=0, help="移动端服务监听端口（默认 8765）")
    parser.add_argument("--with-tunnel", action="store_true", help="自动下载 cloudflared 以支持 4G/5G 外网加密连接")
    parser.add_argument("--create-shortcut", action="store_true", help="在 Windows 桌面创建『反重力助手』快捷方式")
    parser.add_argument("--doctor", action="store_true", help="运行完整环境自检报告")
    parser.add_argument("--json", action="store_true", help="以机器可读 JSON 输出结果")
    args = parser.parse_args(argv)

    actions_taken: list[str] = []

    if not args.doctor or args.non_interactive or not (PROJECT_ROOT / ".env").is_file():
        env_file = ensure_env_configured(
            PROJECT_ROOT,
            workspace=args.workspace,
            pwa_port=args.port,
        )
        actions_taken.append(f"已配置环境变量文件：{env_file}")

    if args.with_tunnel:
        ok_cf, msg_cf = ensure_cloudflared(PROJECT_ROOT)
        actions_taken.append(f"外网隧道组件：{'已就绪 ' + msg_cf if ok_cf else msg_cf}")

    if args.create_shortcut and sys.platform == "win32":
        ok_sc, msg_sc = create_windows_desktop_shortcut(PROJECT_ROOT)
        actions_taken.append(f"桌面快捷方式：{'已创建 ' + msg_sc if ok_sc else msg_sc}")

    report = run_doctor(PROJECT_ROOT)
    report["actions"] = actions_taken

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("\n=== 反重力助手 (Antigravity Assistant) 部署自检报告 ===")
        for act in actions_taken:
            print(f"  [完成] {act}")
        for c in report["checks"]:
            mark = "OK" if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['name']}: {c['detail']}")
        if report["pwa_lan_url"]:
            masked = report["pwa_lan_url"].split("?token=")[0] + "?token=<已写入.env>"
            print(f"\n  手机端局域网入口: {masked}")
            print("  启动桌面控制台  : .\\.venv\\Scripts\\pythonw.exe -X utf8 -m qq_agy_bridge.launcher")
            print("  命令行无界面启动: .\\start_all.ps1")
        print()

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
