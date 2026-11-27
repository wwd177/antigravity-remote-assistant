from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path
from types import SimpleNamespace

import pytest
import websockets

import qq_agy_bridge.agy_session as agy_session_mod
import qq_agy_bridge.auth as auth_mod
import qq_agy_bridge.launcher as launcher_mod
import qq_agy_bridge.web_pwa as web_pwa_mod
from qq_agy_bridge.agy_session import AgySession, SessionManager, TurnResult
from qq_agy_bridge.beacon import build_static_pwa_bundle
from qq_agy_bridge.config import BridgeConfig
from qq_agy_bridge.web_pwa import WebPwaServer

MOCK_AGY = Path(__file__).parent / "mock_agy.py"
GITHUB_PAGES_ORIGIN = "https://wwd177.github.io"


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
        web_access_token="agy-reg-suite-master-token-2026",
    )
    base.update(overrides)
    return BridgeConfig(**base)


def _http_call(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with opener.open(req, timeout=5.0) as resp:
            return int(resp.status), resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        body = exc.read()
        hdrs = dict(exc.headers.items()) if exc.headers else {}
        return int(exc.code), body, hdrs


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x100000 | 0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return int(code.value) == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.mark.asyncio
async def test_cross_origin_dl_revoke_anti_lockout_live_frames_and_ws_params(
    tmp_path: Path,
) -> None:
    """Verify /dl/ cross-origin ticket auth, anti-IP-self-lock on revoke, isolated 429 cleared on pair,
    history ticket refresh immutability, live_frame LRU isolation, and malformed WS param tolerance."""
    env_file = tmp_path / ".env"
    init_tok = "agy-anti-lockout-init-tok-2026"
    env_file.write_text(f"WEB_ACCESS_TOKEN={init_tok}\n", encoding="utf-8")

    img_file = tmp_path / "screen_a.png"
    img_bytes = b"\x89PNG\r\n\x1a\n" + (b"Y" * 32)
    img_file.write_bytes(img_bytes)

    img2_file = tmp_path / "persist.png"
    img2_bytes = b"\x89PNG\r\n\x1a\n" + (b"P" * 24)
    img2_file.write_bytes(img2_bytes)

    frame_file = tmp_path / "live_frame.jpg"
    frame_bytes = b"\xff\xd8\xff\xe0" + (b"\x11" * 24) + b"\xff\xd9"
    frame_file.write_bytes(frame_bytes)

    cfg = _make_cfg(tmp_path, web_access_token=init_tok, allowed_origins=(GITHUB_PAGES_ORIGIN,))
    sessions = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions,
        access_token=init_tok,
        env_path=env_file,
        runtime_dir=tmp_path / "rt",
        ide_store_dir=tmp_path / "ide_dl",
        cli_store_dir=tmp_path / "cli_dl",
        codex_store_dir=tmp_path / "cdx_dl",
    )
    stop_event = asyncio.Event()
    task = asyncio.create_task(pwa.run(stop_event=stop_event))
    try:
        for _ in range(50):
            if pwa.admin_port > 0:
                break
            await asyncio.sleep(0.05)
        assert pwa.admin_port > 0

        mid = pwa.register_media(img_file)
        persist_mid = pwa.register_media(img2_file)
        old_ticket = pwa.auth.issue_media_ticket(mid)

        # 0. Verify _refresh_history_media_tickets refreshes valid tickets and marks evicted items without mutating inputs
        orig_history = [
            {
                "role": "assistant",
                "text": "see ![shot](/dl/" + mid + ")",
                "images": [{"media_id": mid, "media_ticket": "stale-old-ticket", "name": "screen_a.png"}],
                "files": [{"media_id": "missing-id-999", "media_ticket": "gone-t", "name": "gone.bin"}],
                "attachments": [{"media_id": mid, "name": "screen_a.png"}],
            }
        ]
        refreshed = pwa._refresh_history_media_tickets(orig_history)
        assert orig_history[0]["images"][0]["media_ticket"] == "stale-old-ticket"
        assert "media_ticket" not in orig_history[0]["attachments"][0]
        assert "media_unavailable" not in orig_history[0]["files"][0]

        assert pwa.auth.verify_media_ticket_status(refreshed[0]["images"][0]["media_ticket"], mid) == "valid"
        assert pwa.auth.verify_media_ticket_status(refreshed[0]["attachments"][0]["media_ticket"], mid) == "valid"
        assert refreshed[0]["files"][0].get("media_unavailable") is True

        pwa._slot_history["ide:conv-hist"] = orig_history
        pwa._active_slot = "ide:conv-hist"

        # 1. Valid cross-origin /dl/ succeeds with ACAO header
        st_ok, body_ok, hdr_ok = await asyncio.to_thread(
            _http_call,
            f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{mid}?ticket={old_ticket}",
            headers={"Origin": GITHUB_PAGES_ORIGIN},
        )
        assert st_ok == 200 and body_ok == img_bytes
        assert hdr_ok.get("Access-Control-Allow-Origin") == GITHUB_PAGES_ORIGIN

        # 2. Revoke tokens -> 12 stale /dl/ fetches return 401 b"" WITHOUT locking login IP
        ok_rev, _, new_ep = await pwa.revoke_all_tokens()
        assert ok_rev is True and new_ep >= 2

        for _ in range(12):
            st_401, b_401, _ = await asyncio.to_thread(
                _http_call,
                f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{mid}?ticket={old_ticket}",
                headers={"Origin": GITHUB_PAGES_ORIGIN},
            )
            assert st_401 == 401 and b_401 == b""
        assert pwa.auth.is_ip_locked("127.0.0.1") is False

        # Register post-revoke media entries (previous registry was cleared by revoke) and verify hello refreshes tickets safely
        mid_post = pwa.register_media(img_file)
        persist_mid = pwa.register_media(img2_file)
        post_history = [
            {
                "role": "assistant",
                "text": "post ![shot](/dl/" + mid_post + ")",
                "images": [{"media_id": mid_post, "media_ticket": "stale-post-ticket", "name": "screen_a.png"}],
                "files": [{"media_id": mid, "media_ticket": old_ticket, "name": "revoked.png"}],
            }
        ]
        pwa._slot_history["ide:conv-hist-post"] = post_history
        pwa._active_slot = "ide:conv-hist-post"

        # 3. Same IP can immediately POST /api/pair, connect WS (receiving nav_seq + refreshed hello tickets),
        #    download fresh /dl/, survive malformed WS params, and respond to ping -> pong
        pair_code = pwa.auth.issue_pairing_code(ttl_sec=60.0)
        st_pair, _, hdr_pair = await asyncio.to_thread(
            _http_call,
            f"http://127.0.0.1:{cfg.web_pwa_port}/api/pair",
            method="POST",
            data=b"",
            headers={
                "X-Pair-Code": pair_code,
                "Origin": f"http://127.0.0.1:{cfg.web_pwa_port}",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert st_pair == 200
        cookie_kv = (hdr_pair.get("Set-Cookie") or "").split(";", 1)[0].strip()
        assert cookie_kv

        async with websockets.connect(
            f"ws://127.0.0.1:{cfg.web_pwa_port}/ws",
            origin=GITHUB_PAGES_ORIGIN,
            additional_headers={"Cookie": cookie_kv},
        ) as ws:
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
            assert hello["type"] == "hello" and "nav_seq" in hello
            fresh_t = hello["history"][0]["images"][0]["media_ticket"]
            assert fresh_t != "stale-post-ticket"
            assert post_history[0]["images"][0]["media_ticket"] == "stale-post-ticket"
            assert hello["history"][0]["files"][0].get("media_unavailable") is True

            st_fresh, b_fresh, _ = await asyncio.to_thread(
                _http_call,
                f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{mid_post}?ticket={fresh_t}",
                headers={"Origin": GITHUB_PAGES_ORIGIN},
            )
            assert st_fresh == 200 and b_fresh == img_bytes

            # Send malformed numeric params across WS handlers -> connection must remain healthy
            await ws.send(json.dumps({"type": "live_screen", "enable": True, "interval_ms": "NaN"}))
            await ws.send(
                json.dumps(
                    {
                        "type": "select_conversation",
                        "conversation_id": "nonexistent-conv",
                        "store": "ide",
                        "max_messages": "bad",
                    }
                )
            )
            await ws.send(json.dumps({"type": "upload_init", "upload_id": "u-nan", "name": "a.txt", "size": "NaN"}))
            await ws.send(json.dumps({"type": "ping", "ts": 54321}))

            saw_pong = False
            for _ in range(10):
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=4.0))
                if ev.get("type") == "pong" and ev.get("ts") == 54321:
                    saw_pong = True
                    break
            assert saw_pong is True

        # 4. Expired current-epoch ticket returns 401 with 0 failure penalty
        pwa.auth.clear_media_failures("127.0.0.1")
        orig_auth_time = auth_mod.time.time
        base_now = orig_auth_time()
        try:
            auth_mod.time.time = lambda: base_now - 3600.0
            exp_t = pwa.auth.issue_media_ticket(mid_post, ttl_sec=60.0)
        finally:
            auth_mod.time.time = orig_auth_time
        assert pwa.auth.verify_media_ticket_status(exp_t, mid_post) == "expired"
        st_exp, b_exp, _ = await asyncio.to_thread(
            _http_call,
            f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{mid_post}?ticket={exp_t}",
            headers={"Origin": GITHUB_PAGES_ORIGIN},
        )
        assert st_exp == 401 and b_exp == b""
        assert len(pwa.auth._failed_media_auth.get("127.0.0.1", ())) == 0

        # 5. 24 invalid current-epoch media tries -> 429 on /dl/ only, cleared on POST /api/pair
        for i in range(24):
            st_bad, b_bad, _ = await asyncio.to_thread(
                _http_call,
                f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{mid_post}?ticket=tampered-{i}",
                headers={"Origin": GITHUB_PAGES_ORIGIN},
            )
            assert st_bad == 401 and b_bad == b""
        assert pwa.auth.is_media_ip_locked("127.0.0.1") is True
        assert pwa.auth.is_ip_locked("127.0.0.1") is False

        st_429, b_429, _ = await asyncio.to_thread(
            _http_call,
            f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{mid_post}?ticket=tampered-over",
            headers={"Origin": GITHUB_PAGES_ORIGIN},
        )
        assert st_429 == 429 and b_429 == b""

        pair2 = pwa.auth.issue_pairing_code(ttl_sec=60.0)
        st_pair2, _, _ = await asyncio.to_thread(
            _http_call,
            f"http://127.0.0.1:{cfg.web_pwa_port}/api/pair",
            method="POST",
            data=b"",
            headers={
                "X-Pair-Code": pair2,
                "Origin": f"http://127.0.0.1:{cfg.web_pwa_port}",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        assert st_pair2 == 200
        assert pwa.auth.is_media_ip_locked("127.0.0.1") is False

        # 6. _live_frame_registry 24-frame LRU eviction never evicts persistent _media_registry
        frame_paths = []
        for idx_f in range(30):
            fp = tmp_path / f"live_frame_{idx_f}.jpg"
            fp.write_bytes(frame_bytes + bytes([idx_f]))
            frame_paths.append(fp)
        frame_ids = [pwa.register_live_frame(fp) for fp in frame_paths]
        assert len(set(frame_ids)) == 30
        assert len(pwa._live_frame_registry) == 24
        t_first_f = pwa.auth.issue_media_ticket(frame_ids[0])
        t_last_f = pwa.auth.issue_media_ticket(frame_ids[-1])
        t_persist = pwa.auth.issue_media_ticket(persist_mid)
        st_f0, _, _ = await asyncio.to_thread(
            _http_call, f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{frame_ids[0]}?ticket={t_first_f}"
        )
        st_f_last, b_f_last, _ = await asyncio.to_thread(
            _http_call, f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{frame_ids[-1]}?ticket={t_last_f}"
        )
        st_persist, b_persist, _ = await asyncio.to_thread(
            _http_call, f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{persist_mid}?ticket={t_persist}"
        )
        assert st_f0 == 404
        assert st_f_last == 200 and b_f_last == (frame_bytes + bytes([29]))
        assert st_persist == 200 and b_persist == img2_bytes

        # 7. Deleted underlying file returns 404 cleanly even with valid ticket
        fresh_t2 = pwa.auth.issue_media_ticket(mid_post)
        img_file.unlink()
        st_404, _, _ = await asyncio.to_thread(
            _http_call,
            f"http://127.0.0.1:{cfg.web_pwa_port}/dl/{mid_post}?ticket={fresh_t2}",
            headers={"Origin": GITHUB_PAGES_ORIGIN},
        )
        assert st_404 == 404
    finally:
        stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)
        await sessions.shutdown_all()


@pytest.mark.asyncio
async def test_oversized_stdout_kills_child_and_recovers_in_agy_and_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify >STREAM_BUFFER_LIMIT stdout line terminates child process without hanging in AgySession and Codex,
    nonexistent Agy executable returns ERROR cleanly, and subsequent normal turns succeed."""
    monkeypatch.setattr(web_pwa_mod, "STREAM_BUFFER_LIMIT", 256)
    monkeypatch.setattr(agy_session_mod, "STREAM_BUFFER_LIMIT", 256)

    # 1. AgySession missing executable returns clean TurnResult(status="ERROR")
    bad_cfg = _make_cfg(tmp_path)
    missing_sess = AgySession("missing-exe", bad_cfg, command_override=[str(tmp_path / "does_not_exist_agy.exe")])
    res_missing = await missing_sess.ask("hello")
    assert res_missing.status == "ERROR" and res_missing.error
    await missing_sess.stop(force_tree=True)

    # 2. AgySession oversized line terminates child without claiming fake restart, then next turn works
    agy_pid_file = tmp_path / "agy_oversize.pid"
    mock_agy_oversize = tmp_path / "mock_agy_oversize.py"
    mock_agy_oversize.write_text(
        "import json, os, pathlib, sys, time\n"
        "sys.stdin.reconfigure(encoding='utf-8', errors='replace')\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "cid = 'mock-oversize-conv'\n"
        "print(json.dumps({'event':'init','conversation_id':cid,'init':{'cwd':os.getcwd(),'tools':[]}}), flush=True)\n"
        "for raw in sys.stdin:\n"
        "    raw = raw.strip()\n"
        "    if not raw:\n"
        "        continue\n"
        "    msg = json.loads(raw)\n"
        "    content = msg.get('message', {}).get('content', '')\n"
        "    if 'OVERSIZE' in content:\n"
        f"        pathlib.Path({str(agy_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "        sys.stdout.buffer.write((b'B' * 600) + b'\\n')\n"
        "        sys.stdout.buffer.flush()\n"
        "        time.sleep(30)\n"
        "    else:\n"
        "        print(json.dumps({'event':'result','result':{'conversation_id':cid,'status':'SUCCESS','response':'AGY_RECOVERED_OK','duration_seconds':0.01,'num_turns':1}}), flush=True)\n",
        encoding="utf-8",
    )
    sessions_agy = SessionManager(
        _make_cfg(tmp_path), command_override=[sys.executable, "-X", "utf8", str(mock_agy_oversize)]
    )
    try:
        sess = sessions_agy.get("user-oversize")
        t0 = time.monotonic()
        res_agy_bad = await sess.ask("TRIGGER OVERSIZE")
        elapsed_agy = time.monotonic() - t0
        assert res_agy_bad.status == "ERROR" and elapsed_agy < 4.0
        assert "已安全重启会话" not in (res_agy_bad.error or "")

        agy_child_pid = int(agy_pid_file.read_text(encoding="utf-8").strip())
        for _ in range(20):
            if not _is_pid_alive(agy_child_pid):
                break
            await asyncio.sleep(0.05)
        assert not _is_pid_alive(agy_child_pid)

        res_agy_ok = await sess.ask("normal followup")
        assert res_agy_ok.status == "SUCCESS" and "AGY_RECOVERED_OK" in res_agy_ok.response
    finally:
        await sessions_agy.shutdown_all()

    # 3. Codex CLI oversized line kills child promptly and recovers on next turn
    cdx_pid_file = tmp_path / "codex_oversize.pid"
    mock_cdx_py = tmp_path / "mock_codex_oversize.py"
    mock_cdx_py.write_text(
        "import json, os, pathlib, sys, time\n"
        "sys.stdin.reconfigure(encoding='utf-8', errors='replace')\n"
        "sys.stdout.reconfigure(encoding='utf-8', errors='replace')\n"
        "payload = sys.stdin.read()\n"
        f"pathlib.Path({str(cdx_pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "if 'OVERSIZE' in payload:\n"
        "    sys.stdout.buffer.write((b'C' * 600) + b'\\n')\n"
        "    sys.stdout.buffer.flush()\n"
        "    time.sleep(30)\n"
        "else:\n"
        "    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'CODEX_RECOVERED_OK'}}), flush=True)\n",
        encoding="utf-8",
    )
    mock_cdx_cmd = tmp_path / ("mock_codex_oversize.cmd" if sys.platform == "win32" else "mock_codex_oversize.sh")
    if sys.platform == "win32":
        mock_cdx_cmd.write_text(
            f'@chcp 65001 >nul\r\n@"{sys.executable}" -X utf8 "{mock_cdx_py}" %*\r\n',
            encoding="utf-8",
        )
    else:
        mock_cdx_cmd.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -X utf8 "{mock_cdx_py}" "$@"\n',
            encoding="utf-8",
        )
        mock_cdx_cmd.chmod(0o755)

    cfg = _make_cfg(tmp_path)
    sessions_cdx = SessionManager(cfg, command_override=[sys.executable, str(MOCK_AGY)])
    pwa = WebPwaServer(
        cfg,
        session_manager=sessions_cdx,
        access_token=cfg.web_access_token,
        codex_cli_path=mock_cdx_cmd,
    )

    async def _noop(*_a: object) -> None:
        return None

    t1 = time.monotonic()
    res_cdx_bad = await pwa._try_run_codex_cli_turn(
        chat_slot="codex:oversize",
        codex_cid="",
        workspace=tmp_path,
        prompt_text="TRIGGER OVERSIZE LINE",
        reply_id="r-1",
        on_delta=_noop,
        on_tool=_noop,
        on_status=_noop,
    )
    elapsed_cdx = time.monotonic() - t1
    assert isinstance(res_cdx_bad, TurnResult) and res_cdx_bad.status == "ERROR"
    assert elapsed_cdx < 4.0

    cdx_child_pid = int(cdx_pid_file.read_text(encoding="utf-8").strip())
    for _ in range(20):
        if not _is_pid_alive(cdx_child_pid):
            break
        await asyncio.sleep(0.05)
    assert not _is_pid_alive(cdx_child_pid)

    res_cdx_ok = await pwa._try_run_codex_cli_turn(
        chat_slot="codex:oversize",
        codex_cid="",
        workspace=tmp_path,
        prompt_text="normal turn",
        reply_id="r-2",
        on_delta=_noop,
        on_tool=_noop,
        on_status=_noop,
    )
    assert isinstance(res_cdx_ok, TurnResult) and res_cdx_ok.status == "SUCCESS"
    assert "CODEX_RECOVERED_OK" in res_cdx_ok.response


def test_launcher_health_poll_race_and_beacon_revoke_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify LauncherApp poll state machine (no re-entrancy + exception-safe force rerun),
    _has_running_managed_services tracking _tunnel_proc, and revoke-vs-publish race resync."""
    app = object.__new__(launcher_mod.LauncherApp)
    app._poll_state_lock = threading.Lock()
    app._poll_running = False
    app._poll_rerun_pending = False
    app._closing = False
    app._enqueue_log = lambda *_a, **_kw: None
    app._post_ui = lambda *_a, **_kw: None

    entered_first = threading.Event()
    release_first = threading.Event()
    poll_calls: list[bool] = []
    active = 0
    max_active = 0
    poll_lock = threading.Lock()

    def _fake_poll_once(force: bool = False) -> None:
        nonlocal active, max_active
        with poll_lock:
            active += 1
            max_active = max(max_active, active)
            idx = len(poll_calls) + 1
            poll_calls.append(force)
        try:
            if idx == 1:
                entered_first.set()
                release_first.wait(timeout=3.0)
                raise RuntimeError("transient_poll_error")
        finally:
            with poll_lock:
                active -= 1

    app._health_poll_once = _fake_poll_once  # type: ignore[method-assign]
    t1 = threading.Thread(target=app._health_poll_worker, kwargs={"force": False}, daemon=True)
    t1.start()
    assert entered_first.wait(timeout=2.0)

    app._health_poll_worker(force=True)
    release_first.set()
    t1.join(timeout=3.0)

    assert max_active == 1
    assert poll_calls == [False, True]
    assert app._poll_running is False
    assert app._poll_rerun_pending is False

    # _has_running_managed_services tracks _tunnel_proc when _server_proc is None
    app._server_proc = None
    app._tunnel_proc = SimpleNamespace(poll=lambda: None)
    assert app._has_running_managed_services() is True
    app._tunnel_proc = SimpleNamespace(poll=lambda: 0)
    assert app._has_running_managed_services() is False

    # Revoke vs. in-flight beacon publish race: old publish result does not set _beacon_synced, triggers resync with NEW_TOK
    app._beacon_sync_lock = threading.Lock()
    app._beacon_syncing = False
    app._beacon_publish_gen = 0
    app._beacon_last_started_gen = -1
    app._beacon_pending_resync = None
    app._beacon_synced = False
    app._beacon_retry_count = 0
    app._beacon_last_error = ""
    app._beacon_last_attempt_mono = 0.0
    app._tunnel_gen = 1
    app._tunnel_url = "https://t1.trycloudflare.com"
    app._ios_pages_url = "https://u.github.io/r/"
    app.settings = SimpleNamespace(
        beacon_enabled=True,
        web_access_token="OLD_TOK",
        web_pwa_port=8765,
        project_dir=tmp_path,
        public_base_url="",
        beacon_git_remote="",
        beacon_allow_upstream=False,
        beacon_branch="gh-pages",
    )

    entered_old_pub = threading.Event()
    release_old_pub = threading.Event()
    second_pub_done = threading.Event()
    seen_tokens: list[str] = []

    def _mock_publish(*_a, **kwargs):
        tok = str(kwargs["access_token"])
        seen_tokens.append(tok)
        if len(seen_tokens) == 1:
            entered_old_pub.set()
            release_old_pub.wait(timeout=3.0)
            # While still running before return, assert stale token hasn't marked synced
            return True, "https://u.github.io/r/"
        second_pub_done.set()
        return True, "https://u.github.io/r/"

    monkeypatch.setattr(launcher_mod, "publish_ios_pwa_and_beacon", _mock_publish)
    pub_thread = threading.Thread(
        target=app._sync_ios_beacon_worker,
        args=("https://t1.trycloudflare.com", 1),
        daemon=True,
    )
    pub_thread.start()
    assert entered_old_pub.wait(timeout=2.0)

    # Simulate concurrent revoke updating settings + publish generation
    app.settings.web_access_token = "NEW_TOK"
    with app._beacon_sync_lock:
        app._beacon_publish_gen += 1
        app._beacon_synced = False

    release_old_pub.set()
    pub_thread.join(timeout=3.0)
    assert second_pub_done.wait(timeout=3.0)
    assert seen_tokens == ["OLD_TOK", "NEW_TOK"]

    # Give follow-up thread a moment to finish updating _beacon_synced
    for _ in range(40):
        if app._beacon_synced and not app._beacon_syncing:
            break
        time.sleep(0.02)
    assert app._beacon_synced is True


def test_beacon_dynamic_shell_hash_and_frontend_ticket_precedence(tmp_path: Path) -> None:
    """Verify `build_static_pwa_bundle` injects `data-beacon-branch` and HTML SHA-256 cache suffix while preserving v19,
    and frontend `formatInlineMarkdown` prefers `ticketMap` over stale URL `?ticket=`."""
    proj_root = Path(__file__).resolve().parents[1]
    stage = tmp_path / "stage_pages"
    build_static_pwa_bundle(
        proj_root,
        stage,
        owner="wwd177",
        repo="antigravity-remote-assistant",
        access_token="beacon-secret-2026",
        tunnel_url="https://agy.trycloudflare.com",
        lan_url="",
        beacon_branch="custom-pages",
    )
    out_html = (stage / "index.html").read_text(encoding="utf-8")
    out_sw = (stage / "sw.js").read_text(encoding="utf-8")
    expected_hash = hashlib.sha256(out_html.encode("utf-8")).hexdigest()[:10]
    assert 'data-beacon-repo="wwd177/antigravity-remote-assistant"' in out_html
    assert 'data-beacon-branch="custom-pages"' in out_html
    assert "CACHE_PREFIX + 'v19'" in out_sw
    assert expected_hash in out_sw and "__SHELL_HASH__" not in out_sw

    src_html = (proj_root / "src" / "qq_agy_bridge" / "static" / "index.html").read_text(encoding="utf-8")
    assert "const ticket = (ticketMap && ticketMap.get(decId)) || mediaTicketCache.get(decId) || qTicket || '';" in src_html

    node_bin = shutil.which("node")
    if node_bin:
        m = re.search(
            r"(function escapeHtml\(s\)\s*\{.*?function formatInlineMarkdown\(escapedLine, ticketMap = null\)\s*\{.*?\n  \})",
            src_html,
            re.S,
        )
        assert m is not None
        js_probe = tmp_path / "agy_fmt_probe.js"
        js_probe.write_text(
            "const apiBase = 'https://tun.trycloudflare.com';\n"
            "const mediaTicketCache = new Map();\n"
            "function mediaUrl(mid, ticket) { return `${apiBase}/dl/${encodeURIComponent(mid)}${ticket ? '?ticket=' + encodeURIComponent(ticket) : ''}`; }\n"
            + m.group(1)
            + "\n"
            "const rendered = formatInlineMarkdown('![pic](/dl/m1?ticket=OLD_Q)', new Map([['m1', 'FRESH_T']]));\n"
            "if (!rendered.includes('https://tun.trycloudflare.com/dl/m1?ticket=FRESH_T')) { throw new Error('Wrong ticket precedence: ' + rendered); }\n",
            encoding="utf-8",
        )
        proc = subprocess.run([node_bin, str(js_probe)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stderr or proc.stdout
