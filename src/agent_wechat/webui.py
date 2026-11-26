"""本地只读工作台（Phase 07）。

**默认关闭**：只有显式运行 `agent-wechat ui` 才会起服务；进程退出即关闭。
**只监听 loopback**，并把 Host/Origin 钉死在本机——防 DNS rebinding 与跨站请求。
**凭访问令牌进门**：令牌只出现在终端打印的链接里（同 Jupyter），首次访问换成 HttpOnly
cookie；没有它，页面、静态资源与接口一律 403——loopback 挡不住本机的其他账户与进程。
**不服务任何文件系统路径**：页面/脚本/样式都是本模块里的字符串，`/../index.db`
这类请求一律 404（没有路径拼接这条路）。**不写访问日志**：查询串本身就是敏感内容。

前端只拿**有界分页**（时间线/会话/检索都有上限），不整库下发；所有文本都经
`textContent` 渲染，不拼 HTML。
"""
from __future__ import annotations

import dataclasses
import hmac
import ipaddress
import json
import os
import secrets
import socket
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import account as account_mod
from . import derived, errors, paths, retrieval, store, sync, usecases

DEFAULT_PORT = 8765
MAX_LIMIT = 100
MAX_QUERY = 200

_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'")

_INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="aw-csrf" content="__CSRF__">
<title>agent-wechat 本地工作台</title>
<link rel="stylesheet" href="/style.css"></head>
<body>
<header>
  <h1>agent-wechat 本地工作台</h1>
  <p class="warn">这里会显示你的聊天内容。仅在本机使用，不要截图外传；
  页面不联网、不写浏览器缓存。MCP 的本地 stdio 也不等于内容不会进入云端模型。</p>
</header>
<main>
<section>
  <h2>账号 / 会话</h2>
  <label>账号 <select id="account"></select></label>
  <label>会话 <input id="chat" list="chats" placeholder="名称或 wxid">
    <datalist id="chats"></datalist></label>
  <button id="loadChats">刷新会话</button>
</section>
<section>
  <h2>检索</h2>
  <label>关键词 <input id="q" maxlength="200"></label>
  <label>模式 <select id="mode">
    <option>hybrid</option><option>keyword</option><option>semantic</option>
  </select></label>
  <label>起 <input id="after" type="date"></label>
  <label>止 <input id="before" type="date"></label>
  <label>条数 <input id="limit" type="number" min="1" max="100" value="20"></label>
  <button id="search">检索</button>
</section>
<section><h2>结果</h2><ol id="hits"></ol></section>
<section><h2>证据 / 上下文</h2><div id="evidence"></div></section>
<section>
  <h2>时间线</h2>
  <div id="timeline"></div><button id="older">加载更早</button>
</section>
<section><h2>状态</h2><pre id="status"></pre></section>
</main>
<script src="/app.js"></script></body></html>"""

_STYLE = """body{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:0;padding:16px;
max-width:980px}header .warn{background:#fff3cd;padding:8px;border-radius:6px}
section{border-top:1px solid #ddd;margin-top:16px;padding-top:8px}
label{display:inline-block;margin:4px 8px 4px 0}input,select{padding:4px}
#hits li,#timeline div{cursor:pointer;padding:4px;border-bottom:1px solid #eee}
#hits li:hover{background:#f4f4f4}.meta{color:#777;font-size:12px}
pre{white-space:pre-wrap}"""

_APP_JS = r"""'use strict';
const csrf = document.querySelector('meta[name=aw-csrf]').content;
const $ = (id) => document.getElementById(id);
let cursor = null;
const fmt = (ts) => new Date(ts * 1000).toLocaleString();
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined && text !== null) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
async function api(path, opts) {
  const res = await fetch(path, {...opts, headers: {...(opts && opts.headers), 'X-AW-CSRF': csrf}});
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.status);
  return body;
}
function acct() { return $('account').value; }
function params(obj) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== '' && v != null) q.set(k, v);
  return q.toString();
}
async function loadAccounts() {
  const s = await api('/api/status');
  const sel = $('account');
  sel.replaceChildren(...s.accounts.map((a) => el('option', a)));
  $('status').textContent = JSON.stringify(s, null, 2);
}
async function loadChats() {
  const res = await api('/api/chats?' + params({account: acct(), q: $('chat').value}));
  $('chats').replaceChildren(...res.chats.map((c) => {
    const o = el('option'); o.value = c.chat_id; o.label = c.display + ' (' + c.messages + ')';
    return o;
  }));
}
async function doSearch() {
  const after = $('after').value ? Date.parse($('after').value) / 1000 : null;
  const before = $('before').value ? Date.parse($('before').value) / 1000 : null;
  const res = await api('/api/search?' + params({
    account: acct(), chat: $('chat').value, q: $('q').value,
    mode: $('mode').value, limit: $('limit').value, after, before }));
  const items = res.hits.map((h) => {
    const li = el('li');
    li.append(el('div', h.snippet));
    li.append(el('div', fmt(h.ts) + ' · ' + h.sender + ' · ' + h.sources.join('+'),
                 'meta'));
    li.addEventListener('click', () => showEvidence(h.msg_key));
    return li;
  });
  $('hits').replaceChildren(...items);
  if (res.degraded.length) $('hits').append(el('li', '降级：' + JSON.stringify(res.degraded), 'meta'));
}
async function showEvidence(msgKey) {
  const res = await api('/api/evidence?' + params({account: acct(), key: msgKey}));
  const box = $('evidence');
  box.replaceChildren(...res.messages.map((m) => {
    const d = el('div');
    d.append(el('div', fmt(m.time) + ' · ' + m.sender + ' · ' + m.msg_key, 'meta'));
    d.append(el('div', m.content));
    return d;
  }));
  const media = await api('/api/media?' + params({account: acct(), key: msgKey, chat: $('chat').value}));
  box.append(el('pre', JSON.stringify(media, null, 2)));
}
async function loadTimeline(reset) {
  const q = {account: acct(), chat: $('chat').value, limit: 30};
  if (!reset && cursor) Object.assign(q, cursor);
  const res = await api('/api/timeline?' + params(q));
  const rows = res.messages.map((m) => {
    const d = el('div');
    d.append(el('div', fmt(m.ts) + ' · ' + m.sender, 'meta'));
    d.append(el('div', m.content || m.media_hint || ''));
    d.addEventListener('click', () => showEvidence(m.msg_key));
    return d;
  });
  if (reset) $('timeline').replaceChildren();
  $('timeline').append(...rows);
  cursor = res.next_cursor;
  $('older').disabled = !cursor;
}
$('account').addEventListener('change', () => {
  // 换账号 = 换一份数据：时间线与游标都属于上一个账号，留着会让「加载更早」把新账号的
  // 第一页接在旧账号的行后面。
  cursor = null;
  $('timeline').replaceChildren();
  $('older').disabled = true;
  loadChats();
});
// 换会话即从第一页重载：游标属于某一个会话，沿用它会把新会话的结果接在旧行后面。
$('chat').addEventListener('change', () => loadTimeline(true));
$('loadChats').addEventListener('click', loadChats);
$('search').addEventListener('click', doSearch);
$('older').addEventListener('click', () => loadTimeline(false));
loadAccounts().catch((e) => { $('status').textContent = String(e); });
"""


def _loopback(host_header: str | None, port: int) -> bool:
    """Host 头只允许本机（含端口）。防 DNS rebinding 的关键一步。"""
    if not host_header:
        return False
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    if host_header in allowed:
        return True
    # 默认端口时可省端口；显式拒绝其它任何主机名/IP
    if port == 80 and host_header in ("127.0.0.1", "localhost", "[::1]"):
        return True
    return False


def _origin_ok(origin: str | None, port: int) -> bool:
    if origin is None:
        return False
    return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}",
                      f"http://[::1]:{port}"}


class _State:
    def __init__(self, port: int) -> None:
        self.port = port
        self.csrf = secrets.token_hex(16)
        # 访问令牌：只印在终端的链接里。CSRF 令牌嵌在首页 HTML 里、防的是跨站；
        # 它防的是**本机**的其他账户/进程（loopback 对它们是敞开的，Host 校验也挡不住）。
        self.token = secrets.token_urlsafe(32)
        # cookie 不分端口：名字带上端口，同时开两个工作台时互不覆盖。
        self.cookie = f"aw_token_{port}"


def _same(given: str, expect: str) -> bool:
    return hmac.compare_digest(given.encode(), expect.encode())


def _json(handler: BaseHTTPRequestHandler, code: int, obj: object) -> None:
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(raw)


def _text(handler: BaseHTTPRequestHandler, code: int, text: str,
          ctype: str) -> None:
    raw = text.encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", _CSP)
    handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
    handler.end_headers()
    handler.wfile.write(raw)


class _Handler(BaseHTTPRequestHandler):
    server_version = "agent-wechat-ui"
    sys_version = ""

    def log_message(self, *args: object) -> None:            # noqa: D401 — 不写访问日志（查询串敏感）
        return

    @property
    def state(self) -> _State:
        return self.server.aw_state                      # type: ignore[attr-defined]

    def _reject_host(self) -> bool:
        if not _loopback(self.headers.get("Host"), self.state.port):
            _json(self, 421, {"error": "Host 不在本机允许列表"})
            return True
        return False

    def _authed(self) -> bool:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.state.cookie and _same(value, self.state.token):
                return True
        return False

    def _reject_unauthed(self, *, api: bool) -> bool:
        if self._authed():
            return False
        if api:
            _json(self, 403, {"error": "未授权：请用 `agent-wechat ui` 在终端打印的链接打开工作台"})
        else:
            _text(self, 403, "请用终端打印的链接打开（`agent-wechat ui` 启动时印出的带 token 的地址）。",
                  "text/plain; charset=utf-8")
        return True

    def _token_login(self, query: str) -> bool:
        """`/?token=…` 对了 → 种 cookie 并 302 到不带 token 的 `/`（令牌不留在地址栏/历史里）。"""
        token = (urllib.parse.parse_qs(query).get("token") or [""])[0]
        if not (token and _same(token, self.state.token)):
            return False
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"{self.state.cookie}={self.state.token}; "
                                       "HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        return True

    def _reject_csrf(self) -> bool:
        """每进程随机令牌（`X-AW-CSRF`）。GET `/api/*` 与 POST 共用这一份判据。"""
        if self.headers.get("X-AW-CSRF") != self.state.csrf:
            _json(self, 403, {"error": "CSRF 校验失败"})
            return True
        return False

    def do_GET(self) -> None:                # noqa: N802 — BaseHTTPRequestHandler 协议
        if self._reject_host():
            return
        path = urllib.parse.urlparse(self.path)
        if path.path == "/" and path.query and self._token_login(path.query):
            return
        if self._reject_unauthed(api=path.path.startswith("/api/")):
            return
        if path.path == "/":
            _text(self, 200, _INDEX_HTML.replace("__CSRF__", self.state.csrf),
                  "text/html; charset=utf-8")
            return
        if path.path == "/app.js":
            _text(self, 200, _APP_JS, "text/javascript; charset=utf-8")
            return
        if path.path == "/style.css":
            _text(self, 200, _STYLE, "text/css; charset=utf-8")
            return
        if path.path.startswith("/api/"):
            # GET 也要 token：Host 校验只挡 DNS rebinding，挡不住**跨站的简单 GET**——任意网页
            # 都能让浏览器带着 `Host: 127.0.0.1:<port>` 打过来，触发检索 / 语义检索（加载模型）/
            # 诊断。同源策略让它读不到响应，但计时侧信道与资源消耗都还在。自定义头会让跨站
            # fetch 先发预检，而本服务不答 CORS，于是跨站请求根本到不了这里。
            if self._reject_csrf():
                return
            qs = urllib.parse.parse_qs(path.query)
            try:
                _json(self, 200, _strip_paths(_route(path.path, qs)))
            except errors.AgentWeChatError as exc:
                # 信封形状与 MCP 侧同一份（`errors.to_payload`，含 redact）；文案里的本机
                # 路径另行遮蔽——那是本工具自己的话，不是聊天内容。
                payload = errors.to_payload(exc)
                _json(self, 400, {k: _scrub_abs_paths(v) for k, v in payload.items()})
            return
        _json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:               # noqa: N802
        if self._reject_host():
            return
        if self._reject_unauthed(api=True):
            return
        if not _origin_ok(self.headers.get("Origin"), self.state.port):
            _json(self, 403, {"error": "Origin 不在本机允许列表"})
            return
        if self._reject_csrf():
            return
        path = urllib.parse.urlparse(self.path)
        if not store.DB_PATH.exists():
            _json(self, 400, {"error": "索引库不存在"})
            return
        if path.path != "/api/derived/delete":
            _json(self, 404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 4096:
                raise errors.DerivedInputError("请求体缺失或过大")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):   # `[1]` / `1`：此前 `.get` 抛裸 AttributeError、连接断开
                raise errors.DerivedInputError("请求体必须是 JSON 对象")
            derivation_id = str(body.get("derivation_id") or "")
            _json(self, 200, derived.delete_derived(derivation_id))
        except (ValueError, errors.AgentWeChatError) as exc:
            # 与 GET 分支同一个信封：项目异常走 `to_payload`（含 redact 与 hint），
            # 裸 `ValueError`（json 解析失败）没有 hint，手工包一条。两条路各写一份
            # 形状就会漂——GET 那份曾经漏过 redact。
            payload = (errors.to_payload(exc) if isinstance(exc, errors.AgentWeChatError)
                       else {"error": errors.redact(str(exc))})
            _json(self, 400, {k: _scrub_abs_paths(v) for k, v in payload.items()})

    def do_PUT(self) -> None:                # noqa: N802
        if self._reject_host():
            return
        _json(self, 405, {"error": "method not allowed"})

    do_DELETE = do_PUT                       # type: ignore[assignment]
    do_PATCH = do_PUT                        # type: ignore[assignment]


_PATH_KEYS = ("path", "out_dir")


def _strip_paths(data: object) -> object:
    """最小披露：浏览器只看到文件名与相对路径，不拿到本机绝对路径。**API 出口递归做一次**。

    **只认路径键**（`path` / `out_dir`）：那是本工具自己产出的路径。
    · **绝对**路径 → 只剩文件名；相对的逻辑路径（媒体页里的 `../image/…`）原样保留，
      截了就是改写展示数据。

    **不对普通字符串做任何替换**。曾经把已知根（数据目录、微信容器）从所有字符串里换掉，
    结果连**消息正文与检索片段**一起改了——正文里写着数据目录的地方在页面上少了前缀。
    工作台是只读的，展示层不许改写聊天内容，哪怕那是本机路径。错误信封另走
    `_scrub_abs_paths`（那是本工具自己的文案，不是用户内容）。
    """
    if isinstance(data, dict):
        return {k: (_basename(v) if k in _PATH_KEYS
                    and isinstance(v, str) and os.path.isabs(v)
                    else _strip_paths(v))
                for k, v in data.items()}
    if isinstance(data, list):
        return [_strip_paths(v) for v in data]
    return data


def _scrub_abs_paths(text: str | None) -> str | None:
    """本工具**自己的**文案里的绝对路径 → 换成已知根的末段。

    只给错误信封用（`DataNotFound(f"{db} 不存在")` 这类），**不得**用在聊天内容上
    ——见 `_strip_paths` 的说明。
    """
    if not text:
        return text
    for root in (paths.XWECHAT_FILES, paths.HOME_DIR):
        text = text.replace(str(root), os.path.basename(str(root)))
    return text


def _basename(path: str) -> str:
    return os.path.basename(path.rstrip("/")) or path


def _account(qs: dict) -> str:
    value = (qs.get("account") or [""])[0].strip()
    if not value:
        raise errors.AccountNotFound("必须显式指定 account（不做跨账号默认）")
    if value not in {str(p) for p in paths.account_dirs()}:
        raise errors.AccountNotFound(f"账号目录不存在: {value}")
    return value


def _one(qs: dict, name: str, default: str = "") -> str:
    value = (qs.get(name) or [default])[0]
    if len(value) > MAX_QUERY:
        raise errors.SearchInputError(f"{name} 过长（>{MAX_QUERY} 字符）")
    return value


def _int(qs: dict, name: str) -> int | None:
    raw = (qs.get(name) or [""])[0]
    if not raw:
        return None
    try:
        n = int(raw)
    except ValueError as exc:
        raise errors.SearchInputError(f"{name} 必须是整数") from exc
    if not -(1 << 63) <= n < 1 << 63:     # 绑进 SQLite 时会抛裸 OverflowError，连接直接断开
        raise errors.SearchInputError(f"{name} 超出范围")
    return n


def _limit(qs: dict, default: int = 30) -> int:
    n = _int(qs, "limit")
    return default if n is None else max(1, min(n, MAX_LIMIT))


def _route(path: str, qs: dict) -> dict:
    if path == "/api/status":
        return _status()
    acct = _account(qs)
    if not store.DB_PATH.exists():
        # 只读工作台不得顺手建库：没有索引就如实说，不制造一个空库。
        raise errors.DataNotFound(
            "索引库不存在", hint="先运行一次 `agent-wechat sync` 或导出")
    if path == "/api/chats":
        return {"chats": usecases.chats(acct, query=_one(qs, "q"), limit=_limit(qs, 50))}
    if path == "/api/timeline":
        chat = _one(qs, "chat")
        if not chat:
            raise errors.SearchInputError("timeline 需要 chat")
        return usecases.timeline(chat, account=acct, before_ts=_int(qs, "ts"),
                                 before_local_id=_int(qs, "local_id"),
                                 limit=_limit(qs))
    if path == "/api/search":
        # 直接用 core retrieval + 显式 chat_id：工作台是**索引只读**的，不能因为
        # 展示名解析去要微信密钥（usecase 的 contact 解析遇到展示名要解密 contact.db）。
        return retrieval.search_messages(
            _one(qs, "q"), account=acct,
            chat_id=_one(qs, "chat") or None,
            after_ts=_int(qs, "after"), before_ts=_int(qs, "before"),
            limit=_limit(qs, 20), mode=_one(qs, "mode") or "hybrid")
    if path == "/api/evidence":
        key = _one(qs, "key")
        if not key:
            raise errors.SearchInputError("evidence 需要 key")
        res = usecases.message_context(key, account=acct, before=2, after=2)
        # 不给 contact 就不做名字解析，所以不会有 NeedConfirm（也因此不碰微信密钥）
        if isinstance(res, usecases.NeedConfirm):
            raise errors.SearchInputError("evidence 只按 msg_key 取，不做名字解析")
        return res
    if path == "/api/derived":
        return derived.list_derived(acct, chat=_one(qs, "chat") or None,
                                    limit=_limit(qs, 100))
    if path == "/api/media":
        key = _one(qs, "key")
        chat = _one(qs, "chat")
        if not (key and chat):
            raise errors.SearchInputError("media 需要 key 与 chat")
        res = usecases.message_media(chat, keys=[key], account=acct, limit=1)
        return dataclasses.asdict(res) if dataclasses.is_dataclass(res) else dict(res)
    if path == "/api/diagnostics":
        info = sync.diagnose(acct)
        return {"wechat": info["wechat"], "schema": info["schema"],
                "fts": info["fts"], "dirty": info["dirty"],
                "vector": info["vector"], "tasks": info["tasks"],
                "sync_state": info["sync_state"], "disk": info["disk"],
                "engines": derived.engine_status(),
                "problems_count": len(info["problems"])}
    raise errors.SearchInputError("未知接口")


def _status() -> dict:
    st = account_mod.status()
    return {
        "accounts": [str(p) for p in paths.account_dirs()],
        "wechat_running": st.running,
        "current": st.current,
        "index_exists": store.DB_PATH.exists(),
        "csrf_required_for_post": True,
        "notice": "本页面只读且只监听本机；检索结果会进入你的浏览器，不要截图外传。",
    }


def create_server(host: str = "127.0.0.1", port: int = DEFAULT_PORT
                  ) -> ThreadingHTTPServer:
    """建服务但不启动。**只允许 loopback**（传其它地址直接拒绝）。"""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:      # `localhost` 这类主机名：裸 ValueError 会印一整屏 traceback
        raise errors.ScopeDenied(f"不是一个 IP 地址：{host}",
                                 hint="用 127.0.0.1 或 ::1") from exc
    if not ip.is_loopback:
        raise errors.ScopeDenied("工作台只允许监听 loopback 地址",
                                 hint="用 127.0.0.1 或 ::1")
    # ThreadingHTTPServer 默认只开 IPv4 套接字：`::1` 要显式换 AF_INET6，否则 bind 抛 gaierror。
    httpd = (_V6Server if ip.version == 6 else ThreadingHTTPServer)((host, port), _Handler)
    httpd.daemon_threads = True
    httpd.aw_state = _State(httpd.server_address[1])     # type: ignore[attr-defined]
    return httpd


class _V6Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def serve(port: int = DEFAULT_PORT, *, host: str = "127.0.0.1",
          open_browser: bool = False) -> None:
    """显式启动才起服务；Ctrl-C 退出。默认**不**自动打开浏览器。

    `host` 只能是 loopback（`create_server` 校验）：IPv4 `127.0.0.1` 或 IPv6 `::1`——后者
    在只解析 IPv6 的机器上才有意义，URL 里的地址要加方括号。"""
    httpd = create_server(host, port)
    addr = f"[{host}]" if ":" in host else host
    # 带访问令牌的链接只印在这里：凭它进门，别贴给别人（同 Jupyter）。
    url = f"http://{addr}:{httpd.server_address[1]}/?token={httpd.aw_state.token}"  # type: ignore[attr-defined]
    print(f"本地工作台：{url}（仅本机；链接含访问令牌，勿外传；Ctrl-C 停止）")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
