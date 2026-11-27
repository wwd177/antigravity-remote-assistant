from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import websockets

from qq_agy_bridge.agy_session import AgySession, SessionManager
from qq_agy_bridge.beacon import (
    publish_ios_pwa_and_beacon,
    resolve_explicit_beacon_target,
)
from qq_agy_bridge.config import BridgeConfig
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
        agy_timeout_seconds=30,
        progress_notice_delay_seconds=0,
        log_level="DEBUG",
        web_pwa_enabled=True,
        web_pwa_host="127.0.0.1",
        web_pwa_port=_free_port(),
        web_access_token="sec-delivery-master-tok-2026",
    )
    base.update(overrides)
    return BridgeConfig(**base)


@pytest.mark.asyncio
async def test_delivery_ack_states_pc_reject_accept_then_error_and_codex_started_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify honest chat_ack receipts across:
    1. PC IDE reject (False, reason) -> delivery_failed, zero delivered_pc.
    2. Astra counterexample: PC IDE accept (True, ok) followed by stream exception -> single delivered_pc, zero delivery_failed.
    3. Codex CLI turn -> started_cli, zero delivered_pc.
    """
    ide_dir = tmp_path / "ide_ack"
    cli_dir = tmp_path / "cli_ack"
    codex_dir = tmp_path / "cdx_ack"
    ide_cid = "ack-ide-conv-1111"
    tf = ide_dir / "brain" / ide_cid / ".system_generated" / "logs" / "transcript.jsonl"
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(
        json.dumps({"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-27T10:00:00Z", "content": "hi"}, ensure_ascii=False)
        + "\n"
        + json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-27T10:00:02Z", "content": "ready"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    cdx_id = "ack-codex-thread-2222"
    rollout = codex_dir / "sessions" / "2026" / "11" / "27" / f"rollout-2026-11-27T10-00-00-{cdx_id}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "init"}]}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    con = sqlite3.connect(codex_dir / "state_5.sqlite")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INT, source TEXT, cwd TEXT, title TEXT, archived INT)")
    con.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (cdx_id, str(rollout), 1795600000, "exec", str(tmp_path), "Codex Ack Thread", 0))
    con.commit()
    con.close()

    mock_codex_py = tmp_path / "mock_codex_ack.py"
    mock_codex_py.write_text(
        "import json, sys\n"
        "sys.stdin.reconfigure(encoding='utf-8', errors='replace')\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "_ = sys.stdin.read()\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Codex 进程已真实启动并完成'}}, ensure_ascii=False), flush=True)\n",
        encoding="utf-8",
    )
    mock_codex_cmd = tmp_path / ("mock_codex_ack.cmd" if sys.platform == "win32" else "mock_codex_ack.sh")
    if sys.platform == "win32":
        mock_codex_cmd.write_text(f'@chcp 65001 >nul\r\n@"{sys.executable}" -X utf8 "{mock_codex_py}" %*\r\n', encoding="utf-8")
    else:
        mock_codex_cmd.write_text(f'#!/bin/sh\nexec "{sys.executable}" -X utf8 "{mock_codex_py}" "$@"\n', encoding="utf-8")
        mock_codex_cmd.chmod(0o755)

    sender_mode = {"accept": False}

    def _pc_sender(_cid: str, _title: str, _prompt: str) -> tuple[bool, str]:
        if sender_mode["accept"]:
            return (True, "OK")
        return (False, "模拟桌面锁屏拒收")

    cfg = _make_cfg(tmp_path)
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=cfg.web_access_token,
        ide_store_dir=ide_dir,
        cli_store_dir=cli_dir,
        codex_store_dir=codex_dir,
        codex_cli_path=mock_codex_cmd,
        pc_window_sender=_pc_sender,
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(pwa.run(stop_event=stop_event))
    try:
        for _ in range(40):
            if pwa.admin_port > 0:
                break
            await asyncio.sleep(0.05)

        async with websockets.connect(f"ws://127.0.0.1:{cfg.web_pwa_port}/ws?token={cfg.web_access_token}") as ws:
            await asyncio.wait_for(ws.recv(), timeout=4.0)  # hello

            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": ide_cid, "store": "ide"}))
            assert json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))["type"] == "conversation_loaded"

            # 1. PC rejects -> delivery_failed, NEVER delivered_pc!
            await ws.send(json.dumps({"type": "chat", "client_id": "cid-reject-1", "text": "拒绝测试"}))
            reject_acks: list[str] = []
            reject_end = None
            for _ in range(15):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
                if ev.get("type") == "chat_ack" and ev.get("client_id") == "cid-reject-1":
                    reject_acks.append(str(ev.get("stage")))
                elif ev.get("type") == "stream_end":
                    reject_end = ev["item"]
                    break
            assert "delivery_failed" in reject_acks
            assert "delivered_pc" not in reject_acks
            assert reject_end is not None and "未能投递" in reject_end["text"]

            # 2. Astra counterexample: PC accepts (True), then transcript watcher throws ->
            # single delivered_pc, ZERO delivery_failed!
            sender_mode["accept"] = True

            async def _boom_stream(**_kw):
                raise RuntimeError("simulated_stream_timeout")

            monkeypatch.setattr(pwa, "_stream_pc_ide_transcript_until_idle", _boom_stream)
            await ws.send(json.dumps({"type": "chat", "client_id": "cid-accept-err-1", "text": "接收后报错测试"}))
            accept_acks: list[str] = []
            accept_end = None
            for _ in range(15):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
                if ev.get("type") == "chat_ack" and ev.get("client_id") == "cid-accept-err-1":
                    accept_acks.append(str(ev.get("stage")))
                elif ev.get("type") == "stream_end":
                    accept_end = ev["item"]
                    break
            assert accept_acks.count("delivered_pc") == 1
            assert "delivery_failed" not in accept_acks
            assert accept_end is not None and accept_end["text"].strip()

            # 3. Codex CLI -> started_cli, NEVER delivered_pc!
            await ws.send(json.dumps({"type": "select_conversation", "conversation_id": cdx_id, "store": "codex"}))
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
                if ev.get("type") == "conversation_loaded":
                    break
            await ws.send(json.dumps({"type": "chat", "client_id": "cid-codex-1", "text": "codex ack test"}))
            cdx_acks: list[str] = []
            cdx_end = None
            for _ in range(20):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if ev.get("type") == "chat_ack" and ev.get("client_id") == "cid-codex-1":
                    cdx_acks.append(str(ev.get("stage")))
                elif ev.get("type") == "stream_end":
                    cdx_end = ev["item"]
                    break
            assert "started_cli" in cdx_acks
            assert "delivered_pc" not in cdx_acks
            assert "delivery_failed" not in cdx_acks
            assert cdx_end is not None and "Codex 进程已真实启动并完成" in cdx_end["text"]
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_safe_defaults_permissions_and_capability_gates_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify safe defaults (permissions/desktop/clipboard/beacon False) and live capability gating."""
    default_cfg = BridgeConfig(agy_executable=sys.executable, agy_workspace=tmp_path)
    assert default_cfg.agy_dangerously_skip_permissions is False
    assert default_cfg.remote_desktop_enabled is False
    assert default_cfg.remote_clipboard_enabled is False
    assert default_cfg.beacon_enabled is False

    sess_def = AgySession("test-user", default_cfg)
    assert "--dangerously-skip-permissions" not in sess_def._build_command()
    sess_opt = AgySession("test-user", _make_cfg(tmp_path, agy_dangerously_skip_permissions=True))
    assert "--dangerously-skip-permissions" in sess_opt._build_command()

    import qq_agy_bridge.web_pwa as _wp

    fake_frame = tmp_path / "fake_frame.jpg"
    fake_frame.write_bytes(b"\xff\xd8\xff\xe0" + (b"\x00" * 64) + b"\xff\xd9")
    monkeypatch.setattr(_wp, "capture_desktop_fast_jpeg_sync", lambda *_a, **_kw: fake_frame)
    monkeypatch.setattr(_wp, "get_desktop_cursor_ratio", lambda: {"x_ratio": 0.5, "y_ratio": 0.5})

    desk_calls: list[tuple] = []
    clip_get_calls: list[int] = []
    clip_set_calls: list[str] = []

    def _clip_get() -> dict:
        clip_get_calls.append(1)
        return {"ok": True, "text": "secret-clip", "truncated": False, "length": 11}

    cfg = _make_cfg(tmp_path)
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=cfg.web_access_token,
        ide_store_dir=tmp_path / "ide_cap",
        cli_store_dir=tmp_path / "cli_cap",
        codex_store_dir=tmp_path / "cdx_cap",
        desktop_input_handler=lambda *a: (desk_calls.append(a) or True, "ok"),
        pc_clipboard_getter=_clip_get,
        pc_clipboard_setter=lambda t: (clip_set_calls.append(t) or True, "ok"),
        pc_system_inspector=lambda _ws: {"cpu_pct": 10, "ram_pct": 40, "ports": []},
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(pwa.run(stop_event=stop_event))
    try:
        for _ in range(40):
            if pwa.admin_port > 0:
                break
            await asyncio.sleep(0.05)

        async with websockets.connect(f"ws://127.0.0.1:{cfg.web_pwa_port}/ws?token={cfg.web_access_token}") as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
            assert hello["capabilities"]["remote_desktop"]["enabled"] is False
            assert hello["capabilities"]["remote_clipboard"]["enabled"] is False

            # All disabled operations must fail closed with capability_disabled + zero handler calls
            for req in (
                {"type": "desktop_frame"},
                {"type": "live_screen", "enable": True, "interval_ms": 600},
                {"type": "desktop_input", "action": "click", "x_ratio": 0.5, "y_ratio": 0.5},
                {"type": "pc_clipboard_get"},
                {"type": "pc_clipboard_set", "text": "overwrite"},
            ):
                await ws.send(json.dumps(req))
                saw_dis = False
                saw_denied = False
                for _ in range(4):
                    ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
                    if ev.get("type") == "capability_disabled":
                        saw_dis = True
                    elif ev.get("type") == "toast" and ev.get("code") == "capabilities_denied":
                        saw_denied = True
                    if saw_dis and saw_denied:
                        break
                assert saw_dis and saw_denied

            await ws.send(json.dumps({"type": "pc_system_inspect"}))
            sys_ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
            assert sys_ev["type"] == "pc_system_state"
            assert sys_ev["clipboard"]["disabled"] is True and sys_ev["clipboard"]["text"] == ""
            assert not desk_calls and not clip_get_calls and not clip_set_calls

            # Enable remote_desktop, start live_screen, then disable -> active stream stops
            await pwa.set_remote_capabilities(remote_desktop=True)
            cap_up = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
            assert cap_up["type"] == "capabilities_update" and cap_up["capabilities"]["remote_desktop"]["enabled"] is True

            await ws.send(json.dumps({"type": "live_screen", "enable": True, "interval_ms": 600}))
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
                if ev.get("type") == "desktop_frame":
                    break
            assert len(pwa._live_screen_stoppers) == 1

            await pwa.set_remote_capabilities(remote_desktop=False)
            await asyncio.sleep(0.05)
            cap_down = None
            for _ in range(6):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
                if ev.get("type") == "capabilities_update":
                    cap_down = ev
                    break
            assert cap_down is not None and cap_down["capabilities"]["remote_desktop"]["enabled"] is False
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_pairing_race_http_only_cookies_and_admin_revoke_disconnect(tmp_path: Path) -> None:
    """Verify GET / never sets cookies, concurrent POST /api/pair mints <=1 session, HTTPS login cookies, and /admin/revoke."""
    env_file = tmp_path / ".env"
    tok = "race-master-token-987654321"
    env_file.write_text(f"WEB_ACCESS_TOKEN={tok}\n", encoding="utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # 1. Pairing GET inertness + 6-thread concurrent POST /api/pair race + revoke disconnect
    cfg1 = _make_cfg(tmp_path, web_access_token=tok, trust_cf_proxy_headers=True)
    sessions1 = SessionManager(cfg1, command_override=[sys.executable, str(MOCK_AGY)])
    pwa1 = WebPwaServer(
        cfg1,
        session_manager=sessions1,
        access_token=tok,
        env_path=env_file,
        runtime_dir=tmp_path / "rt1",
        ide_store_dir=tmp_path / "ide_r1",
        cli_store_dir=tmp_path / "cli_r1",
        codex_store_dir=tmp_path / "cdx_r1",
    )
    stop1 = asyncio.Event()
    t1 = asyncio.create_task(pwa1.run(stop_event=stop1))
    try:
        for _ in range(40):
            if pwa1.admin_port > 0:
                break
            await asyncio.sleep(0.05)

        code = pwa1.auth.issue_pairing_code(ttl_sec=60.0)
        for q_path in (f"/?pair={code}", f"/?token={tok}"):
            def _get_noop(path_s: str = q_path) -> tuple[int, str]:
                with opener.open(f"http://127.0.0.1:{cfg1.web_pwa_port}{path_s}", timeout=4) as resp:
                    return resp.status, (resp.headers.get("Set-Cookie") or "")

            st_code, ck_get = await asyncio.to_thread(_get_noop)
            assert st_code == 200 and ck_get == ""

        barrier = threading.Barrier(6)
        results: list[tuple[int, str]] = []
        res_lock = threading.Lock()

        def _try_pair() -> None:
            barrier.wait(timeout=3.0)
            req = urllib.request.Request(
                f"http://127.0.0.1:{cfg1.web_pwa_port}/api/pair",
                method="POST",
                data=b"",
                headers={
                    "X-Pair-Code": code,
                    "Origin": f"http://127.0.0.1:{cfg1.web_pwa_port}",
                    "Sec-Fetch-Site": "same-origin",
                },
            )
            try:
                with opener.open(req, timeout=4) as r:
                    ck = (r.headers.get("Set-Cookie") or "").split(";", 1)[0].strip()
                    with res_lock:
                        results.append((r.status, ck))
            except urllib.error.HTTPError as he:
                with res_lock:
                    results.append((he.code, ""))

        await asyncio.gather(*(asyncio.to_thread(_try_pair) for _ in range(6)))

        ok_pairs = [r for r in results if r[0] == 200]
        fail_pairs = [r for r in results if r[0] == 401]
        assert len(ok_pairs) == 1 and len(fail_pairs) == 5
        paired_cookie = ok_pairs[0][1]

        async with websockets.connect(
            f"ws://127.0.0.1:{cfg1.web_pwa_port}/ws",
            additional_headers={"Cookie": paired_cookie},
        ) as ws_paired:
            hello = json.loads(await asyncio.wait_for(ws_paired.recv(), timeout=4.0))
            assert hello["type"] == "hello"

            req_rev = urllib.request.Request(
                f"http://127.0.0.1:{pwa1.admin_port}/admin/revoke",
                method="POST",
                data=b"{}",
                headers={"X-Admin-Token": pwa1.admin_secret},
            )
            def _do_revoke() -> dict:
                with opener.open(req_rev, timeout=4) as rev_resp:
                    return json.loads(rev_resp.read().decode("utf-8"))

            rev_json = await asyncio.to_thread(_do_revoke)
            assert rev_json["ok"] is True and rev_json["closed_clients"] >= 1

            with pytest.raises(websockets.exceptions.ConnectionClosed) as close_exc:
                await asyncio.wait_for(ws_paired.recv(), timeout=1.0)
            assert close_exc.value.rcvd is not None and close_exc.value.rcvd.code == 4001

            new_env_txt = env_file.read_text(encoding="utf-8")
            assert tok not in new_env_txt
    finally:
        stop1.set()
        await asyncio.wait_for(t1, timeout=5.0)
        await sessions1.shutdown_all()

    # 2. Clean instance: POST /api/auth/login with X-Forwarded-Proto: https sets __Host-agy_session + Secure
    cfg2 = _make_cfg(tmp_path, web_access_token=tok, trust_cf_proxy_headers=True)
    sessions2 = SessionManager(cfg2, command_override=[sys.executable, str(MOCK_AGY)])
    pwa2 = WebPwaServer(
        cfg2,
        session_manager=sessions2,
        access_token=tok,
        runtime_dir=tmp_path / "rt2",
        ide_store_dir=tmp_path / "ide_r2",
        cli_store_dir=tmp_path / "cli_r2",
        codex_store_dir=tmp_path / "cdx_r2",
    )
    stop2 = asyncio.Event()
    t2 = asyncio.create_task(pwa2.run(stop_event=stop2))
    try:
        for _ in range(40):
            if pwa2.admin_port > 0:
                break
            await asyncio.sleep(0.05)

        req_https = urllib.request.Request(
            f"http://127.0.0.1:{cfg2.web_pwa_port}/api/auth/login",
            method="POST",
            data=b"",
            headers={
                "Authorization": f"Bearer {tok}",
                "Host": "demo.trycloudflare.com",
                "Origin": "https://demo.trycloudflare.com",
                "Sec-Fetch-Site": "same-origin",
                "X-Forwarded-Proto": "https",
            },
        )
        def _login_https() -> str:
            with opener.open(req_https, timeout=4) as r_https:
                return r_https.headers.get("Set-Cookie") or ""

        ck_hdr = await asyncio.to_thread(_login_https)
        assert ck_hdr.startswith("__Host-agy_session=")
        assert "HttpOnly" in ck_hdr and "Secure" in ck_hdr and "SameSite=Strict" in ck_hdr
        assert tok not in ck_hdr

        # Non-ASCII pairing codes / credentials MUST fail cleanly and increment rate limiter without TypeError!
        for bad_ip in ("10.0.0.77",):
            ok_pair_bad, _, _ = pwa2.auth.exchange_pairing_code("非ASCII配对码-测试", bad_ip)
            ok_tok_bad, _, _ = pwa2.auth.verify_credential_with_expiry("中文密钥-无效", bad_ip, record_failure=True)
            assert ok_pair_bad is False and ok_tok_bad is False
            assert len(pwa2.auth._failed_auth.get(bad_ip, [])) == 2
    finally:
        stop2.set()
        await asyncio.wait_for(t2, timeout=5.0)
        await sessions2.shutdown_all()


def test_beacon_upstream_protection_and_url_hardening(tmp_path: Path) -> None:
    """Verify beacon requires opt-in and rejects protected upstreams, pushurl overrides, multi-pushurl, and creds."""
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(proj), check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/wwd177/antigravity-remote-assistant.git"],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )

    ok_dis, _ = publish_ios_pwa_and_beacon(
        proj,
        access_token="tok-123456",
        tunnel_url="https://demo.trycloudflare.com",
        lan_url="http://192.168.1.10:8765",
        beacon_enabled=False,
        explicit_remote="origin",
    )
    assert ok_dis is False

    # Upstream repo must be rejected even when beacon_enabled=True
    assert resolve_explicit_beacon_target(proj, beacon_enabled=True, explicit_remote="origin") is None

    # Fetch URL is benign fork, but pushurl points to protected upstream -> MUST be rejected!
    subprocess.run(
        ["git", "remote", "add", "sneaky", "https://github.com/alice/my-pages.git"],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "--push", "sneaky", "https://github.com/wwd177/stayy.git"],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )
    assert resolve_explicit_beacon_target(proj, beacon_enabled=True, explicit_remote="sneaky") is None

    # Multiple push URLs -> rejected
    subprocess.run(
        ["git", "remote", "add", "multi", "https://github.com/alice/pages-1.git"],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "--push", "--add", "multi", "https://github.com/alice/pages-2.git"],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "set-url", "--push", "--add", "multi", "https://github.com/alice/pages-3.git"],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )
    assert resolve_explicit_beacon_target(proj, beacon_enabled=True, explicit_remote="multi") is None

    # Inline creds / query / fragment / malformed bracket URL rejected; valid user fork accepted
    for bad_url in (
        "https://user:pass@github.com/alice/pages.git",
        "https://github.com/alice/pages.git?foo=1",
        "https://github.com/alice/pages.git#frag",
        "https://github.com/alice/[bad.git",
    ):
        assert resolve_explicit_beacon_target(proj, beacon_enabled=True, explicit_remote=bad_url) is None

    # Refusing source branches (main/master) and tags (refs/tags/v1) for beacon push
    for src_br in ("main", "master", "refs/heads/main", "refs/tags/v1"):
        ok_br, msg_br = publish_ios_pwa_and_beacon(
            proj,
            access_token="tok-123456",
            tunnel_url="https://demo.trycloudflare.com",
            lan_url="http://192.168.1.10:8765",
            beacon_enabled=True,
            explicit_remote="https://github.com/alice/my-agy-pages.git",
            beacon_branch=src_br,
        )
        assert ok_br is False and ("源码分支" in msg_br or "标签" in msg_br)

    # Global/local git pushInsteadOf rewrite pointing literal URL to protected upstream MUST be blocked!
    subprocess.run(
        [
            "git",
            "config",
            "url.https://github.com/wwd177/stayy.git.pushInsteadOf",
            "https://github.com/alice/my-agy-pages.git",
        ],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )
    assert (
        resolve_explicit_beacon_target(
            proj,
            beacon_enabled=True,
            explicit_remote="https://github.com/alice/my-agy-pages.git",
        )
        is None
    )
    subprocess.run(
        ["git", "config", "--unset", "url.https://github.com/wwd177/stayy.git.pushInsteadOf"],
        cwd=str(proj),
        check=True,
        capture_output=True,
    )

    ok_target = resolve_explicit_beacon_target(
        proj,
        beacon_enabled=True,
        explicit_remote="https://github.com/alice/my-agy-pages.git",
    )
    assert ok_target is not None and ok_target[3] == "https://alice.github.io/my-agy-pages/"
