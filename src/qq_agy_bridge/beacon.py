"""Zero-trust encrypted tunnel beacon & static iOS Standalone PWA publisher for 反重力助手.

Architecture:
- Hosts the permanent HTTPS iOS Standalone Web App shell (`index.html`, `manifest.json`, `sw.js`,
  `apple-touch-icon.png`) on the repository's `gh-pages` branch (`https://<owner>.github.io/<repo>/`).
- Encrypts the PC's live `{tunnel_url, lan_url, updated_ms}` using PBKDF2-HMAC-SHA256 (100k iterations)
  + HMAC-SHA256 Counter Mode + Encrypt-then-MAC (`AGY-BEACON-V1`), derived from `WEB_ACCESS_TOKEN`.
- Publishes `beacon.json` as a single orphan commit on `gh-pages` from an isolated `.gh_pages_stage/`
  directory so `main` working tree is never touched and commit history never bloats.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
PBKDF2_ITERATIONS = 100_000
BEACON_DOMAIN_TAG = b"AGY-BEACON-V1"
_BEACON_PUBLISH_LOCK = threading.Lock()

# Protect upstream project repositories against accidental force-push from cloned forks without explicit configuration
PROTECTED_UPSTREAM_REPOS = frozenset(
    {"wwd177/antigravity-remote-assistant", "wwd177/stayy"}
)

GITHUB_REMOTE_RE = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("ascii"))


def _derive_keys(access_token: str, salt: bytes) -> tuple[bytes, bytes]:
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        access_token.strip().encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
        dklen=64,
    )
    return dk[:32], dk[32:]


def _xor_keystream(enc_key: bytes, nonce: bytes, data: bytes) -> bytes:
    out = bytearray(len(data))
    blocks = (len(data) + 31) // 32
    for idx in range(blocks):
        ks = hmac.new(enc_key, nonce + idx.to_bytes(4, "big"), hashlib.sha256).digest()
        start = idx * 32
        end = min(start + 32, len(data))
        for j in range(start, end):
            out[j] = data[j] ^ ks[j - start]
    return bytes(out)


def encrypt_beacon_payload(payload: dict[str, Any], access_token: str) -> dict[str, Any]:
    """Encrypt connection endpoints so public `beacon.json` leaks zero IP or URL metadata."""
    if not access_token or not access_token.strip():
        raise ValueError("access_token must not be empty")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    enc_key, mac_key = _derive_keys(access_token, salt)

    pt = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = _xor_keystream(enc_key, nonce, pt)
    mac = hmac.new(mac_key, BEACON_DOMAIN_TAG + salt + nonce + ct, hashlib.sha256).digest()

    return {
        "v": 1,
        "updated_ms": int(time.time() * 1000),
        "iter": PBKDF2_ITERATIONS,
        "salt": _b64u_encode(salt),
        "nonce": _b64u_encode(nonce),
        "ct": _b64u_encode(ct),
        "mac": _b64u_encode(mac),
    }


def decrypt_beacon_payload(
    envelope: dict[str, Any],
    access_token: str,
    *,
    max_age_sec: float | None = None,
) -> dict[str, Any]:
    """Verify MAC, decrypt `beacon.json` envelope, and optionally enforce inner timestamp freshness."""
    if int(envelope.get("v") or 0) != 1:
        raise ValueError("Unsupported beacon version")
    salt = _b64u_decode(str(envelope["salt"]))
    nonce = _b64u_decode(str(envelope["nonce"]))
    ct = _b64u_decode(str(envelope["ct"]))
    expected_mac = _b64u_decode(str(envelope["mac"]))

    enc_key, mac_key = _derive_keys(access_token, salt)
    actual_mac = hmac.new(mac_key, BEACON_DOMAIN_TAG + salt + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(actual_mac, expected_mac):
        raise ValueError("Beacon MAC verification failed (invalid token)")

    pt = _xor_keystream(enc_key, nonce, ct)
    parsed = json.loads(pt.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Decrypted beacon payload is not an object")
    if max_age_sec is not None:
        inner_ts = int(parsed.get("ts") or 0)
        now_ms = int(time.time() * 1000)
        if inner_ts <= 0 or (now_ms - inner_ts) > int(max_age_sec * 1000) or (inner_ts - now_ms) > 300_000:
            raise ValueError("Beacon payload timestamp expired or invalid")
    return parsed


_FORBIDDEN_BEACON_BRANCHES = frozenset(
    {"main", "master", "trunk", "develop", "dev", "head", "prod", "production", "release"}
)


def _normalize_safe_beacon_branch(branch: str) -> str | None:
    cand = (branch or "").strip()
    if not cand or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in cand):
        return None
    if cand.lower().startswith("refs/heads/"):
        cand = cand[len("refs/heads/"):]
    elif cand.lower().startswith("refs/"):
        return None
    if not cand or cand.startswith(("-", "/")) or cand.endswith(("/", ".", ".lock")):
        return None
    if ".." in cand or "@{" in cand or "\\" in cand or "//" in cand:
        return None
    for bad in ("~", "^", ":", "?", "*", "["):
        if bad in cand:
            return None
    if cand.lower() in _FORBIDDEN_BEACON_BRANCHES:
        return None
    return cand


def _apply_git_push_rewrites(project_dir: Path, raw_url: str) -> str:
    extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    cwd_target = str(project_dir) if project_dir.is_dir() else None
    try:
        res = subprocess.run(
            ["git", "config", "--get-regexp", r"^url\..*\.(pushinsteadof|insteadof)$"],
            cwd=cwd_target,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=4,
            **extra,
        )
        if res.returncode != 0 or not res.stdout:
            return raw_url
    except Exception:
        return raw_url

    best_push: tuple[int, str, str] | None = None
    best_any: tuple[int, str, str] | None = None
    for raw_ln in res.stdout.splitlines():
        ln = raw_ln.strip()
        if not ln or " " not in ln:
            continue
        key, _, prefix = ln.partition(" ")
        prefix = prefix.strip()
        if not prefix or not raw_url.startswith(prefix):
            continue
        k_low = key.lower()
        if k_low.startswith("url.") and k_low.endswith(".pushinsteadof"):
            repl = key[4 : -len(".pushinsteadof")]
            if repl and (best_push is None or len(prefix) > best_push[0]):
                best_push = (len(prefix), prefix, repl)
        elif k_low.startswith("url.") and k_low.endswith(".insteadof"):
            repl = key[4 : -len(".insteadof")]
            if repl and (best_any is None or len(prefix) > best_any[0]):
                best_any = (len(prefix), prefix, repl)
    chosen = best_push or best_any
    if chosen is not None:
        _, pref, repl = chosen
        return repl + raw_url[len(pref):]
    return raw_url


def resolve_explicit_beacon_target(
    project_dir: Path,
    *,
    beacon_enabled: bool,
    explicit_remote: str,
    allow_upstream_protected: bool = False,
) -> tuple[str, str, str, str] | None:
    """Resolve explicit user-owned GitHub Pages beacon target.

    NEVER infers `origin` implicitly when `beacon_enabled` is False or `explicit_remote` is blank.
    Always inspects git push URL (`--push`), rejects inline credentials and protected upstream repos.
    Returns `(remote_url, owner, repo, pages_url)` or None.
    """
    if not beacon_enabled:
        return None
    clean_remote = (explicit_remote or "").strip()
    if not clean_remote:
        return None

    remote_url = ""
    is_literal_url = False
    if clean_remote.startswith(("https://", "http://", "git@", "ssh://", "file://")) or Path(clean_remote).exists():
        remote_url = clean_remote
        is_literal_url = True
    else:
        extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}
        try:
            res = subprocess.run(
                ["git", "remote", "get-url", "--push", "--all", clean_remote],
                cwd=str(project_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
                **extra,
            )
            if res.returncode != 0 or not (res.stdout or "").strip():
                logger.warning("Refusing beacon remote without verifiable push URL: %s", clean_remote)
                return None
            lines_out = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip()]
            if len(lines_out) == 1:
                remote_url = lines_out[0]
            elif len(lines_out) > 1:
                logger.warning("Refusing ambiguous multi-pushurl git remote %s", clean_remote)
                return None
        except Exception:
            return None

    if not remote_url:
        return None
    if is_literal_url:
        remote_url = _apply_git_push_rewrites(project_dir, remote_url)

    try:
        if remote_url.startswith(("http://", "https://")):
            parsed = urlsplit(remote_url)
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                logger.warning("Refusing beacon URL containing inline credentials or query/fragment")
                return None
        elif remote_url.startswith("ssh://"):
            parsed = urlsplit(remote_url)
            if parsed.password or (parsed.username and parsed.username != "git"):
                logger.warning("Refusing ssh beacon URL with non-standard credentials")
                return None
    except ValueError:
        logger.warning("Refusing malformed beacon URL")
        return None

    m = GITHUB_REMOTE_RE.match(remote_url)
    if not m:
        return None
    owner = m.group("owner")
    repo = m.group("repo")
    slug = f"{owner}/{repo}".lower()
    if slug in PROTECTED_UPSTREAM_REPOS and not allow_upstream_protected:
        logger.warning("Refusing to publish beacon onto protected upstream repo %s", slug)
        return None
    pages_url = f"https://{owner.lower()}.github.io/{repo}/"
    return (remote_url, owner, repo, pages_url)


def detect_candidate_beacon_remote(project_dir: Path) -> tuple[str, str, str, str] | None:
    """Inspect git `origin` push URL as a candidate target while blocking protected upstream repos."""
    return resolve_explicit_beacon_target(
        project_dir,
        beacon_enabled=True,
        explicit_remote="origin",
        allow_upstream_protected=False,
    )


def detect_github_pages_info(project_dir: Path) -> tuple[str, str, str, str] | None:
    """Return `(remote_url, owner, repo, pages_url)` for safe user-owned remote, blocking upstream repos."""
    return detect_candidate_beacon_remote(project_dir)


STATIC_PWA_SW_JS = """\
const SCOPE_SLUG = new URL(self.registration.scope).pathname.replace(/[^a-zA-Z0-9_-]/g, '_');
const CACHE_PREFIX = 'antigravity-ios-shell-' + SCOPE_SLUG + '-';
const CACHE_NAME = CACHE_PREFIX + 'v19';
const LEGACY_EXACT = new Set(['antigravity-ios-shell-v16', 'antigravity-ios-shell-v17', 'antigravity-ios-shell-v18']);
const REQUIRED_SHELL = ['./', './index.html'];
const OPTIONAL_ASSETS = ['./apple-touch-icon.png', './icon-192.png', './icon-512.png', './manifest.json'];

async function cacheUrlStrict(cache, assetUrl) {
  const res = await fetch(assetUrl, { cache: 'no-cache' });
  if (!res || !res.ok) throw new Error('SHELL_PRECACHE_FAILED:' + assetUrl);
  await cache.put(assetUrl, res);
}

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      // Required HTML shell MUST succeed before this SW is allowed to install & replace the previous version!
      await Promise.all(REQUIRED_SHELL.map((u) => cacheUrlStrict(cache, u)));
      await Promise.allSettled(OPTIONAL_ASSETS.map((u) => cacheUrlStrict(cache, u)));
      await self.skipWaiting();
    })
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => (k.startsWith(CACHE_PREFIX) || LEGACY_EXACT.has(k)) && k !== CACHE_NAME)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

function fetchWithTimeout(req, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  return fetch(req, { cache: 'no-store', signal: ctrl.signal }).finally(() => clearTimeout(timer));
}

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin || url.pathname.endsWith('/beacon.json')) {
    return;
  }
  const isNavShell = e.request.mode === 'navigate'
    || url.pathname.endsWith('/')
    || url.pathname.endsWith('/index.html');

  if (isNavShell) {
    e.respondWith(
      fetchWithTimeout(e.request, 1800)
        .then((res) => {
          if (!res || !res.ok) throw new Error('HTTP_' + (res ? res.status : 0));
          const clone = res.clone();
          e.waitUntil(caches.open(CACHE_NAME).then((c) => c.put(e.request, clone)).catch(() => {}));
          return res;
        })
        .catch(() =>
          caches.open(CACHE_NAME)
            .then((c) => c.match(e.request).then((m) => m || c.match('./index.html')))
            .then((m) => m || caches.match('./index.html'))
        )
    );
    return;
  }

  // Subresources (icons/manifest): fast cache-first with background refresh, NEVER falling back to HTML!
  e.respondWith(
    caches.open(CACHE_NAME).then((cache) =>
      cache.match(e.request).then((cached) => {
        const netUpdate = fetchWithTimeout(e.request, 3500)
          .then((res) => {
            if (res && res.ok) {
              return cache.put(e.request, res.clone()).catch(() => {}).then(() => res);
            }
            return cached || res;
          })
          .catch(() => cached);
        if (cached) {
          e.waitUntil(netUpdate);
          return cached;
        }
        return netUpdate;
      })
    )
  );
});
"""


def build_static_pwa_bundle(
    project_dir: Path,
    stage_dir: Path,
    *,
    owner: str,
    repo: str,
    access_token: str,
    tunnel_url: str,
    lan_url: str,
) -> dict[str, Any]:
    """Assemble the complete static iOS Standalone PWA shell + encrypted `beacon.json` into `stage_dir`."""
    stage_dir.mkdir(parents=True, exist_ok=True)
    src_html = project_dir / "src" / "qq_agy_bridge" / "static" / "index.html"
    assets_dir = project_dir / "assets"

    html_text = src_html.read_text(encoding="utf-8")
    # Inject repository beacon metadata attribute onto <html> so standalone shell knows its API beacon source
    repo_slug = f"{owner}/{repo}"
    html_text = html_text.replace(
        '<html lang="zh-CN">',
        f'<html lang="zh-CN" data-beacon-repo="{repo_slug}">',
        1,
    )
    (stage_dir / "index.html").write_text(html_text, encoding="utf-8")
    (stage_dir / ".nojekyll").write_text("", encoding="utf-8")
    (stage_dir / "sw.js").write_text(STATIC_PWA_SW_JS, encoding="utf-8")

    # Omit `start_url` so iOS/W3C Home Screen bookmark preserves `#key=...` during initial pairing!
    manifest = {
        "name": "反重力助手",
        "short_name": "反重力助手",
        "description": "专属 iPhone 远程电脑操控与全会话 AI 编程中枢",
        "display": "standalone",
        "background_color": "#000000",
        "theme_color": "#000000",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "./apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
            {"src": "./icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "./icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }
    (stage_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for icon_name in ("apple-touch-icon.png", "icon-192.png", "icon-512.png"):
        src_icon = assets_dir / icon_name
        if not src_icon.is_file() and icon_name == "apple-touch-icon.png":
            src_icon = assets_dir / "icon-192.png"
        if src_icon.is_file():
            shutil.copy2(src_icon, stage_dir / icon_name)

    beacon_env = encrypt_beacon_payload(
        {
            "tunnel_url": tunnel_url.rstrip("/"),
            "lan_url": lan_url.rstrip("/"),
            "ts": int(time.time() * 1000),
        },
        access_token,
    )
    (stage_dir / "beacon.json").write_text(
        json.dumps(beacon_env, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return beacon_env


def publish_ios_pwa_and_beacon(
    project_dir: Path,
    *,
    access_token: str,
    tunnel_url: str,
    lan_url: str,
    beacon_enabled: bool = False,
    explicit_remote: str = "",
    allow_upstream_protected: bool = False,
    beacon_branch: str = "gh-pages",
) -> tuple[bool, str]:
    """Build and force-push single-commit `gh-pages` branch with encrypted `beacon.json`.

    Never touches the `main` working directory or git index, and refuses protected upstream repos.
    Returns `(ok, pages_url_or_error)`.
    """
    target_branch = _normalize_safe_beacon_branch(beacon_branch)
    if not target_branch:
        return (False, "拒绝将信标强推至受保护源码分支、标签或非法分支名")
    gh_info = resolve_explicit_beacon_target(
        project_dir,
        beacon_enabled=beacon_enabled,
        explicit_remote=explicit_remote,
        allow_upstream_protected=allow_upstream_protected,
    )
    if gh_info is None:
        return (False, "未启用自有仓库加密信标或目标仓库不受支持（已拦截受保护上游仓库）")
    remote_url, owner, repo, pages_url = gh_info

    stage_dir = project_dir / ".gh_pages_stage"
    extra = {"creationflags": CREATE_NO_WINDOW} if sys.platform == "win32" else {}

    with _BEACON_PUBLISH_LOCK:
        try:
            # Wipe the entire staging folder so stray residual files can never leak onto public gh-pages
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)
            stage_dir.mkdir(parents=True, exist_ok=True)

            build_static_pwa_bundle(
                project_dir,
                stage_dir,
                owner=owner,
                repo=repo,
                access_token=access_token,
                tunnel_url=tunnel_url,
                lan_url=lan_url,
            )

            def _git(*args: str, timeout: int = 25) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", *args],
                    cwd=str(stage_dir),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                    **extra,
                )

            init_res = _git("init", "-b", target_branch)
            if init_res.returncode != 0:
                return (False, f"初始化信标仓库失败：{(init_res.stderr or init_res.stdout).strip()[:120]}")
            _git("config", "user.name", "Antigravity Beacon")
            _git("config", "user.email", "beacon@antigravity.local")
            _git("remote", "add", "_beacon_target", remote_url)
            re_checked = resolve_explicit_beacon_target(
                stage_dir,
                beacon_enabled=True,
                explicit_remote="_beacon_target",
                allow_upstream_protected=allow_upstream_protected,
            )
            if (
                re_checked is None
                or (re_checked[1].lower(), re_checked[2].lower()) != (owner.lower(), repo.lower())
            ):
                return (False, "最终 Git push 目标校验未通过（已拦截受保护上游或 rewrite 规则）")
            add_res = _git(
                "add",
                "--",
                "index.html",
                ".nojekyll",
                "sw.js",
                "manifest.json",
                "beacon.json",
                "apple-touch-icon.png",
                "icon-192.png",
                "icon-512.png",
            )
            if add_res.returncode != 0:
                return (False, f"暂存信标文件失败：{(add_res.stderr or add_res.stdout).strip()[:120]}")

            c_res = _git("commit", "-m", "chore(pwa): sync encrypted tunnel beacon & iOS shell")
            if c_res.returncode != 0:
                return (False, f"生成信标提交失败：{(c_res.stderr or c_res.stdout).strip()[:120]}")

            p_res = _git("push", "-f", "_beacon_target", f"HEAD:refs/heads/{target_branch}", timeout=30)
            if p_res.returncode != 0 and sys.platform == "win32":
                # Transient Windows schannel TLS retry using OpenSSL backend
                p_res = _git(
                    "-c",
                    "http.sslBackend=openssl",
                    "push",
                    "-f",
                    "_beacon_target",
                    f"HEAD:refs/heads/{target_branch}",
                    timeout=30,
                )
            if p_res.returncode != 0:
                return (False, f"推送 {target_branch} 失败：{(p_res.stderr or p_res.stdout).strip()[:140]}")

            return (True, pages_url)
        except Exception as exc:
            return (False, f"同步永久入口异常：{exc}")
