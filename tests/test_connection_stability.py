from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import websockets

from qq_agy_bridge.agy_session import SessionManager
from qq_agy_bridge.config import BridgeConfig
from qq_agy_bridge.launcher import probe_cloudflared_ready, probe_tunnel_e2e_ping
from qq_agy_bridge.web_pwa import WebPwaServer

MOCK_AGY = Path(__file__).parent / "mock_agy.py"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_cfg(tmp_path: Path, **overrides) -> BridgeConfig:
    base = dict(
        agy_executable=sys.executable,
        agy_workspace=tmp_path,
        agy_model="balanced-test",
        agy_effort="",
        agy_dangerously_skip_permissions=True,
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        log_level="DEBUG",
        web_pwa_enabled=True,
        web_pwa_host="127.0.0.1",
        web_pwa_port=_free_port(),
        web_access_token="bridge-conn-stability-token-2026",
    )
    base.update(overrides)
    return BridgeConfig(**base)


@pytest.mark.asyncio
async def test_ws_ping_never_blocked_by_slow_handler_and_tunnel_zombie_probe(tmp_path: Path) -> None:
    """Verify slow WebSocket handlers do not block ping/pong, tunnel zombie probes work, and client JS is hardened."""

    def _slow_ide_actions(_cid: str = "") -> list[dict[str, object]]:
        time.sleep(1.2)
        return [{"token": "btn-1", "label": "Allow", "kind": "action"}]

    def _slow_pc_system(_ws: Path | None = None) -> dict[str, object]:
        time.sleep(1.2)
        return {"ok": True, "hostname": "bridge-test-host"}

    port = _free_port()
    tok = "bridge-stability-token-sec"
    cfg = _make_cfg(tmp_path, web_pwa_port=port, web_access_token=tok)
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=tok,
        ide_store_dir=tmp_path / "ide_stab",
        cli_store_dir=tmp_path / "cli_stab",
        codex_store_dir=tmp_path / "cdx_stab",
        ide_actions_inspector=_slow_ide_actions,
        pc_clipboard_getter=lambda: {"ok": True, "text": "", "truncated": False, "length": 0},
        pc_system_inspector=_slow_pc_system,
    )
    stop_event = asyncio.Event()
    pwa_task = asyncio.create_task(pwa.run(stop_event=stop_event))
    try:
        await asyncio.sleep(0.2)
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token={tok}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
            assert hello["type"] == "hello"

            await ws.send(json.dumps({"type": "ide_actions_inspect", "conversation_id": "conv-b1"}))
            await ws.send(json.dumps({"type": "pc_system_inspect"}))
            await asyncio.sleep(0.05)

            t0 = time.monotonic()
            await ws.send(json.dumps({"type": "ping", "ts": 987654321}))

            saw_pong = False
            pong_elapsed = 999.0
            saw_actions = False
            saw_sys = False
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (saw_pong and saw_actions and saw_sys):
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - time.monotonic()))
                ev = json.loads(raw)
                etype = ev.get("type")
                if etype == "pong":
                    pong_elapsed = time.monotonic() - t0
                    saw_pong = True
                elif etype == "ide_actions":
                    saw_actions = bool(ev.get("actions"))
                elif etype == "pc_system_state":
                    saw_sys = (ev.get("system") or {}).get("hostname") == "bridge-test-host"

            assert saw_pong is True
            assert pong_elapsed < 0.35, f"ping/pong blocked by slow handlers: {pong_elapsed:.3f}s"
            assert saw_actions is True
            assert saw_sys is True
    finally:
        stop_event.set()
        await asyncio.wait_for(pwa_task, timeout=5.0)
        await sessions.shutdown_all()

    state = {"ready": False, "service": "antigravity-assistant", "ping_code": 200}

    class _MockProbeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/ready":
                code = 200 if state["ready"] else 503
                body = json.dumps({"status": code, "readyConnections": 2 if state["ready"] else 0}).encode("utf-8")
            elif path == "/ping":
                code = int(state["ping_code"])
                if code == 200:
                    body = json.dumps({"ok": True, "service": state["service"]}).encode("utf-8")
                else:
                    body = b"bad gateway"
            else:
                code = 404
                body = b"{}"
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockProbeHandler)
    probe_port = srv.server_address[1]
    srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
    srv_thread.start()
    try:
        ready_false, conns_false = probe_cloudflared_ready(probe_port, timeout=1.0)
        assert ready_false is False
        assert conns_false == 0

        state["ready"] = True
        ready_true, conns_true = probe_cloudflared_ready(probe_port, timeout=1.0)
        assert ready_true is True
        assert conns_true == 2

        dead_ok, dead_conns = probe_cloudflared_ready(_free_port(), timeout=0.5)
        assert dead_ok is False
        assert dead_conns == -1

        assert (
            probe_tunnel_e2e_ping(
                f"http://127.0.0.1:{probe_port}",
                expected_service="antigravity-assistant",
                timeout=1.5,
            )
            is True
        )

        state["service"] = "stayy"
        assert (
            probe_tunnel_e2e_ping(
                f"http://127.0.0.1:{probe_port}",
                expected_service="antigravity-assistant",
                timeout=1.5,
            )
            is False
        )

        state["service"] = "antigravity-assistant"
        state["ping_code"] = 502
        assert (
            probe_tunnel_e2e_ping(
                f"http://127.0.0.1:{probe_port}",
                expected_service="antigravity-assistant",
                timeout=1.5,
            )
            is False
        )
    finally:
        srv.shutdown()
        srv.server_close()

    index_html = (Path(__file__).resolve().parent.parent / "src" / "qq_agy_bridge" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    for marker in ("missedPongCount", "15000", "65536", "Promise.any(sourcePromises)", "wasHidden"):
        assert marker in index_html, f"missing client stability marker: {marker}"
