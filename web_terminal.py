import asyncio
import json
import logging
import os
import socket
import threading
import time
import secrets
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify
from websockets import serve
from winpty import PtyProcess

# --- 配置 ---
USERNAME = os.environ.get("RS_USER", "admin")
PASSWORD = os.environ.get("RS_PASS", "123456")
HTTP_PORT = int(os.environ.get("RS_HTTP_PORT", "5010"))
WS_PORT = int(os.environ.get("RS_WS_PORT", "5011"))
TOKEN_EXPIRE = 86400  # token 有效期 24 小时，每次交互自动续期
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = 600  # 10 分钟内

terminals = {}       # token -> timestamp
login_attempts = {}  # ip -> (count, first_attempt_time)

HTML = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>Remote Shell</title>
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"
        onerror="document.getElementById('cdn-error').style.display='block'"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-web-links@0.9.0/lib/xterm-addon-web-links.min.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"/>
    <style>
        * { box-sizing: border-box; }
        html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; }
        body { background: #000; color: #fff; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; flex-direction: column; }

        .login-screen { position: fixed; inset: 0; background: #1e1e1e; display: flex; align-items: center; justify-content: center; z-index: 100; padding: 20px; }
        .login-box { background: #2d2d2d; padding: 30px; border-radius: 12px; text-align: center; max-width: 320px; width: 100%; }
        .login-box h2 { margin: 0 0 20px; font-size: 1.5rem; color: #00ff00; }
        input { width: 100%; padding: 14px 12px; margin: 10px 0; border: 1px solid #444; background: #1a1a1a; color: #00ff00; border-radius: 8px; font-size: 16px; }
        button { width: 100%; padding: 14px 20px; background: #007bff; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 16px; font-weight: 600; margin-top: 10px; }
        button:active { background: #0056b3; }
        .login-error { color: #ff4444; font-size: 14px; margin-top: 10px; display: none; }

        #terminal-container { flex: 1; padding: 8px; }
        .xterm { padding: 4px; }

        @supports (padding-bottom: env(safe-area-inset-bottom)) {
            #terminal-container { padding-bottom: calc(8px + env(safe-area-inset-bottom)); }
        }

        .status-bar {
            display: none;
            background: #1a1a1a;
            border-top: 1px solid #333;
            padding: 4px 10px;
            font-size: 11px;
            color: #888;
            justify-content: space-between;
            align-items: center;
        }
        .status-bar .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
        .status-bar .status-dot.connected { background: #00ff00; }
        .status-bar .status-dot.disconnected { background: #ff4444; }
        .status-bar .logout-btn { background: transparent; color: #888; border: 1px solid #555; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 11px; width: auto; margin: 0; }

        .mobile-toolbar {
            display: none;
            background: #1a1a1a;
            border-top: 1px solid #333;
            padding: 8px;
            gap: 8px;
            flex-wrap: wrap;
            justify-content: center;
        }

        .toolbar-btn {
            padding: 6px 10px;
            background: #333;
            color: #fff;
            border: none;
            border-radius: 4px;
            font-size: 12px;
            cursor: pointer;
            touch-action: manipulation;
        }
        .toolbar-btn:active { background: #555; }

        .cdn-error {
            display: none;
            position: fixed;
            inset: 0;
            background: #1e1e1e;
            z-index: 200;
            align-items: center;
            justify-content: center;
            text-align: center;
            padding: 20px;
        }
        .cdn-error p { color: #ff4444; font-size: 16px; }

        @media screen and (max-width: 768px) {
            .login-box { padding: 24px 20px; }
            .login-box h2 { font-size: 1.3rem; }
            input { font-size: 16px; padding: 12px 10px; }
            button { font-size: 16px; padding: 12px 16px; }

            .mobile-toolbar { display: flex; }
            .status-bar { display: flex; }

            #terminal-container { padding: 4px; }
        }

        @media screen and (min-width: 769px) {
            .status-bar { display: flex; }
        }

        @media screen and (max-width: 768px) and (orientation: landscape) {
            .login-screen { flex-direction: row; }
            .login-box { max-width: 280px; padding: 20px; }
        }

        * { -webkit-tap-highlight-color: transparent; }
        input:focus, button:focus { outline: none; }
    </style>
</head>
<body>
    <div id="cdn-error" class="cdn-error">
        <div><p>Failed to load xterm.js from CDN. Please check your network connection.</p></div>
    </div>

    <div id="login-interface" class="login-screen">
        <div class="login-box">
            <h2>Remote Shell</h2>
            <input type="text" id="user" placeholder="Username" autocomplete="off" autocapitalize="off"><br>
            <input type="password" id="pass" placeholder="Password"><br>
            <button onclick="login()">Connect</button>
            <div id="login-error" class="login-error"></div>
        </div>
    </div>

    <div id="terminal-container"></div>

    <div class="mobile-toolbar" id="toolbar">
        <button class="toolbar-btn" onclick="sendKey('Ctrl+C')">Ctrl+C</button>
        <button class="toolbar-btn" onclick="sendKey('Ctrl+Z')">Ctrl+Z</button>
        <button class="toolbar-btn" onclick="sendKey('Tab')">Tab</button>
        <button class="toolbar-btn" onclick="sendKey('Esc')">Esc</button>
        <button class="toolbar-btn" onclick="sendKey('Enter')">Enter</button>
        <button class="toolbar-btn" onclick="clearTerminal()">Clear</button>
    </div>

    <div class="status-bar" id="status-bar">
        <span id="status-indicator"><span class="status-dot disconnected"></span>Disconnected</span>
        <button class="logout-btn" onclick="logout()">Logout</button>
    </div>

    <script>
        let term, socket, token;
        const fitAddon = new FitAddon.FitAddon();
        const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

        function setStatus(connected) {
            const indicator = document.getElementById('status-indicator');
            if (connected) {
                indicator.innerHTML = '<span class="status-dot connected"></span>Connected';
            } else {
                indicator.innerHTML = '<span class="status-dot disconnected"></span>Disconnected';
            }
        }

        function showError(msg) {
            const el = document.getElementById('login-error');
            el.textContent = msg;
            el.style.display = msg ? 'block' : 'none';
        }

        function login() {
            const user = document.getElementById('user').value;
            const pass = document.getElementById('pass').value;
            showError('');
            fetch('/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({username: user, password: pass})
            }).then(r => r.json()).then(data => {
                if(data.success) {
                    token = data.token;
                    document.getElementById('login-interface').style.display = 'none';
                    initShell();
                } else {
                    showError(data.error || 'Login failed');
                }
            }).catch(() => showError('Network error'));
        }

        function logout() {
            if (socket) {
                socket.send(JSON.stringify({type: 'logout'}));
                socket.close();
            }
            if (term) term.dispose();
            token = null;
            setStatus(false);
            document.getElementById('login-interface').style.display = 'flex';
            document.getElementById('terminal-container').innerHTML = '';
        }

        function sendKey(key) {
            if (socket && socket.readyState === WebSocket.OPEN) {
                if (key === 'Tab') socket.send('\t');
                else if (key === 'Ctrl+C') socket.send('\x03');
                else if (key === 'Ctrl+Z') socket.send('\x1a');
                else if (key === 'Esc') socket.send('\x1b');
                else if (key === 'Enter') socket.send('\r');
                else socket.send(key);
            }
        }

        function clearTerminal() {
            if (term) term.clear();
        }

        function getTerminalDimensions() {
            const container = document.getElementById('terminal-container');
            const cols = Math.max(20, Math.floor(container.clientWidth / 9.5));
            const rows = Math.max(10, Math.floor(container.clientHeight / 18));
            return { cols, rows };
        }

        function notifyResize() {
            if (socket && socket.readyState === WebSocket.OPEN) {
                const dims = getTerminalDimensions();
                socket.send(JSON.stringify({type: 'resize', cols: dims.cols, rows: dims.rows}));
            }
        }

        function initShell() {
            const fontSize = isMobile ? 13 : 15;

            term = new Terminal({
                cursorBlink: true,
                theme: {
                    background: '#000000',
                    foreground: '#ffffff',
                    cursor: '#00ff00'
                },
                fontSize: fontSize,
                fontFamily: '"Cascadia Code", "Consolas", "Monaco", monospace',
                letterSpacing: 0,
                lineHeight: 1.2,
                allowProposedApi: true,
                scrollback: 1000
            });

            term.loadAddon(new WebLinksAddon.WebLinksAddon());
            term.loadAddon(fitAddon);
            term.open(document.getElementById('terminal-container'));

            // 延迟 fit 等 DOM 布局稳定
            setTimeout(() => fitAddon.fit(), 50);

            const dims = getTerminalDimensions();
            const wsUrl = `ws://${window.location.hostname}:WS_PORT_PLACEHOLDER?token=${token}&cols=${dims.cols}&rows=${dims.rows}`;
            socket = new WebSocket(wsUrl);

            socket.onmessage = (e) => term.write(e.data);

            term.onData(data => {
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(data);
                }
            });

            socket.onopen = () => {
                setStatus(true);
                term.write('\x1b[1;32m[CONNECTED]\x1b[0m\r\n');
                if (isMobile) {
                    term.write('\x1b[33m[Mobile Mode] Use toolbar for shortcuts\x1b[0m\r\n');
                }
            };

            socket.onclose = () => {
                setStatus(false);
                term.write('\x1b[1;31m[DISCONNECTED]\x1b[0m\r\n');
            };

            socket.onerror = () => {
                setStatus(false);
            };

            // 窗口大小变化时同步 PTY 尺寸
            let resizeTimer;
            window.addEventListener('resize', () => {
                fitAddon.fit();
                clearTimeout(resizeTimer);
                resizeTimer = setTimeout(() => {
                    fitAddon.fit();
                    notifyResize();
                }, isMobile ? 300 : 150);
            });

            if (isMobile) {
                window.visualViewport?.addEventListener('resize', () => {
                    clearTimeout(resizeTimer);
                    resizeTimer = setTimeout(() => {
                        fitAddon.fit();
                        notifyResize();
                    }, 300);
                });
            }

            term.focus();
        }
    </script>
</body>
</html>"""

# 在 HTML 中注入实际 WS 端口
HTML = HTML.replace("WS_PORT_PLACEHOLDER", str(WS_PORT))

# 关闭 Flask/Werkzeug access log
log = logging.getLogger('werkzeug')
log.setLevel(logging.WARNING)

app = Flask(__name__)


@app.route('/')
def index():
    return HTML


@app.route('/login', methods=['POST'])
def login():
    ip = request.remote_addr
    now = time.time()

    # 登录频率限制
    if ip in login_attempts:
        count, first = login_attempts[ip]
        if now - first > LOGIN_WINDOW:
            count = 0
            login_attempts[ip] = (0, now)
        elif count >= LOGIN_MAX_ATTEMPTS:
            return jsonify({'success': False, 'error': 'Too many attempts, try later'}), 429
    else:
        login_attempts[ip] = (0, now)

    data = request.get_json(silent=True) or {}
    if data.get('username') == USERNAME and data.get('password') == PASSWORD:
        token = secrets.token_hex(16)
        terminals[token] = now

        # 清理过期 token
        expired = [t for t, ts in terminals.items() if now - ts > TOKEN_EXPIRE]
        for t in expired:
            del terminals[t]

        return jsonify({'success': True, 'token': token})
    else:
        count, first = login_attempts[ip]
        login_attempts[ip] = (count + 1, first)
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401


async def ws_handler(ws):
    query = parse_qs(urlparse(ws.path).query)
    token = query.get('token', [None])[0]
    cols = int(query.get('cols', [80])[0])
    rows = int(query.get('rows', [24])[0])

    if not token or token not in terminals:
        await ws.close()
        return

    # 检查 token 过期
    if time.time() - terminals.get(token, 0) > TOKEN_EXPIRE:
        terminals.pop(token, None)
        await ws.close()
        return

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    proc = PtyProcess.spawn('cmd.exe', env=env, dimensions=(rows, cols))
    loop = asyncio.get_running_loop()
    running = True

    def read_pty():
        while running:
            try:
                data = proc.read(4096)
                if not data:
                    break
                loop.call_soon_threadsafe(
                    lambda d=data: asyncio.ensure_future(_safe_send(ws, d))
                )
            except Exception:
                break

    async def _safe_send(ws, data):
        try:
            await ws.send(data)
        except Exception:
            pass

    thread = threading.Thread(target=read_pty, daemon=True)
    thread.start()

    try:
        async for msg in ws:
            # 每次交互续期 token
            if token in terminals:
                terminals[token] = time.time()

            # 检测 JSON 控制消息 (resize / logout)
            if isinstance(msg, str) and msg.startswith('{'):
                try:
                    ctrl = json.loads(msg)
                    if ctrl.get('type') == 'resize':
                        new_cols = ctrl.get('cols', cols)
                        new_rows = ctrl.get('rows', rows)
                        proc.setwinsize(new_rows, new_cols)
                        cols, rows = new_cols, new_rows
                        continue
                    elif ctrl.get('type') == 'logout':
                        break
                except (json.JSONDecodeError, ValueError):
                    pass
            proc.write(msg)
    finally:
        running = False
        proc.terminate()
        terminals.pop(token, None)


async def main():
    async with serve(ws_handler, "0.0.0.0", WS_PORT):
        print(f"Remote Shell Ready: http://{socket.gethostname()}:{HTTP_PORT}")
        threading.Thread(
            target=app.run,
            kwargs={'host': '0.0.0.0', 'port': HTTP_PORT, 'debug': False, 'use_reloader': False},
            daemon=True
        ).start()
        await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
