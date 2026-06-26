import asyncio
import functools
import json
import logging
import os
import socket
import threading
import time
import secrets
import uuid
from urllib.parse import urlparse, parse_qs
from flask import Flask, request, jsonify, session
from websockets import serve
from websockets.exceptions import ConnectionClosed
from winpty import PtyProcess

# --- 配置 ---
USERNAME = os.environ.get("RS_USER", "admin")
PASSWORD = os.environ.get("RS_PASS", "admin123456")
HTTP_PORT = int(os.environ.get("RS_HTTP_PORT", "5010"))
WS_PORT = int(os.environ.get("RS_WS_PORT", "5011"))
ACCESS_TOKEN = os.environ.get("RS_ACCESS_TOKEN", "remote_shell_2026")
SECRET_KEY = os.environ.get("RS_SECRET_KEY", secrets.token_hex(32))
TOKEN_EXPIRE = 86400  # token 有效期 24 小时
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW = 600  # 10 分钟内
HISTORY_MAX = 256 * 1024  # 每个 PTY 的历史缓冲 256KB

sessions = {}         # token -> {'ts': float}
pty_sessions = {}     # pty_id -> PTY dict (全局, 单用户场景)
pty_lock = threading.Lock()
login_attempts = {}   # ip -> (count, first_attempt_time)

HTML = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>Remote Shell v4</title>
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

        #main-area { display: none; flex: 1; flex-direction: column; overflow: hidden; }

        /* Tab 栏 */
        #tab-bar {
            display: flex;
            align-items: stretch;
            background: #141414;
            border-bottom: 1px solid #333;
            overflow-x: auto;
            overflow-y: hidden;
            flex-shrink: 0;
            scrollbar-width: none;
            -ms-overflow-style: none;
            min-height: 34px;
        }
        #tab-bar::-webkit-scrollbar { display: none; }

        .tab {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            font-size: 13px;
            color: #888;
            background: #1a1a1a;
            border-right: 1px solid #333;
            cursor: pointer;
            white-space: nowrap;
            user-select: none;
            -webkit-user-select: none;
            flex-shrink: 0;
            transition: background 0.15s;
        }
        .tab:hover { background: #252525; color: #ccc; }
        .tab.active { background: #000; color: #00ff00; border-bottom: 2px solid #00ff00; }

        .tab-title { pointer-events: none; }
        .tab-close {
            font-size: 15px;
            line-height: 1;
            padding: 2px 4px;
            border-radius: 3px;
            color: #666;
            pointer-events: auto;
        }
        .tab-close:hover { background: #444; color: #ff4444; }

        .tab-add {
            padding: 6px 16px;
            font-size: 18px;
            font-weight: 700;
            color: #00ff00;
            cursor: pointer;
            border-right: none;
            background: transparent;
        }
        .tab-add:hover { background: #1a3a1a; }

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
            flex-shrink: 0;
        }
        .status-bar .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
        .status-bar .status-dot.connected { background: #00ff00; }
        .status-bar .status-dot.disconnected { background: #ff4444; }
        .status-bar .logout-btn { background: transparent; color: #888; border: 1px solid #555; padding: 2px 8px; border-radius: 3px; cursor: pointer; font-size: 11px; width: auto; margin: 0; }

        .mobile-toolbar {
            position: fixed;
            left: 0;
            right: 0;
            bottom: 0;
            z-index: 60;
            background: #1a1a1a;
            border-top: 1px solid #333;
            padding: 10px 8px;
            padding-bottom: calc(10px + env(safe-area-inset-bottom, 0px));
            gap: 8px;
            flex-wrap: wrap;
            justify-content: center;
            display: none;
            transform: translateY(100%);
            transition: transform 0.2s ease;
        }
        .mobile-toolbar.open {
            display: flex;
            transform: translateY(0);
        }

        .toolbar-toggle {
            display: none;
            position: fixed;
            bottom: calc(40px + env(safe-area-inset-bottom, 0px));
            right: 12px;
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: rgba(0, 123, 255, 0.85);
            color: #fff;
            border: none;
            font-size: 18px;
            cursor: pointer;
            z-index: 55;
            align-items: center;
            justify-content: center;
            backdrop-filter: blur(8px);
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
            touch-action: manipulation;
            -webkit-user-select: none;
            user-select: none;
        }
        .toolbar-toggle:active { background: rgba(0, 86, 179, 0.9); }
        .toolbar-toggle.shifted {
            bottom: calc(90px + env(safe-area-inset-bottom, 0px));
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

            .toolbar-toggle { display: flex; }
            .status-bar { display: flex; }

            #tab-bar { min-height: 38px; }
            .tab { padding: 8px 12px; font-size: 13px; }
            .tab-close { font-size: 16px; padding: 3px 5px; }
            .tab-add { padding: 8px 14px; font-size: 20px; }

            #terminal-container { padding: 4px; }
        }

        @media screen and (min-width: 769px) {
            .status-bar { display: flex; }
        }

        @media screen and (max-width: 768px) and (orientation: landscape) {
            .login-screen { flex-direction: row; }
            .login-box { max-width: 280px; padding: 20px; }
            #tab-bar { min-height: 30px; }
            .tab { padding: 4px 10px; font-size: 12px; }
        }

        * { -webkit-tap-highlight-color: transparent; }
        input:focus, button:focus { outline: none; }
    </style>
</head>
<body>
    <div id="cdn-error" class="cdn-error">
        <div><p>Failed to load xterm.js from CDN. Please check your network connection.</p></div>
    </div>
    
    <div class="status-bar" id="status-bar">
        <span id="status-indicator"><span class="status-dot disconnected"></span>Disconnected</span>
        <button class="logout-btn" onclick="logout()">Logout</button>
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

    <div id="main-area">
        <div id="tab-bar"></div>
        <div id="terminal-container"></div>
    </div>

    <button class="toolbar-toggle" id="toolbar-toggle"
        ontouchstart="event.stopPropagation()"
        onclick="toggleToolbar(event)" title="快捷键">⌨</button>

    <div class="mobile-toolbar" id="toolbar"
        ontouchstart="event.stopPropagation()"
        onclick="event.stopPropagation()">
        <button class="toolbar-btn" onclick="sendKey('Ctrl+C')">Ctrl+C</button>
        <button class="toolbar-btn" onclick="sendKey('Ctrl+Z')">Ctrl+Z</button>
        <button class="toolbar-btn" onclick="sendKey('Tab')">Tab</button>
        <button class="toolbar-btn" onclick="sendKey('Esc')">Esc</button>
        <button class="toolbar-btn" onclick="sendKey('Enter')">Enter</button>
        <button class="toolbar-btn" onclick="clearTerminal()">Clear</button>
    </div>


    <script>
        const ACCESS_TOKEN = "ACCESS_TOKEN_PLACEHOLDER";
        const WS_PORT = WS_PORT_PLACEHOLDER;
        let term, socket, token;
        let currentPtyId = null;
        let ptyList = [];
        const fitAddon = new FitAddon.FitAddon();
        const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

        function getCookie(name) {
            const match = document.cookie.match(new RegExp('(^| )' + name + '=([^;]+)'));
            return match ? match[2] : null;
        }

        function setCookie(name, value, days) {
            const d = new Date();
            d.setTime(d.getTime() + days * 86400000);
            document.cookie = name + '=' + value + ';expires=' + d.toUTCString() + ';path=/;SameSite=Strict';
        }

        function deleteCookie(name) {
            document.cookie = name + '=;expires=Thu, 01 Jan 1970 00:00:00 UTC;path=/';
        }

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

        function showLogin() {
            deleteCookie('rs_token');
            token = null;
            document.getElementById('main-area').style.display = 'none';
            document.getElementById('login-interface').style.display = 'flex';
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
                    setCookie('rs_token', token, 1);
                    document.getElementById('login-interface').style.display = 'none';
                    document.getElementById('main-area').style.display = 'flex';
                    enterWorkspace();
                } else {
                    showError(data.error || 'Login failed');
                }
            }).catch(() => showError('Network error'));
        }

        function logout() {
            if (socket) {
                try { socket.close(); } catch(e) {}
                socket = null;
            }
            if (term) { term.dispose(); term = null; }
            deleteCookie('rs_token');
            token = null;
            currentPtyId = null;
            ptyList = [];
            document.getElementById('tab-bar').innerHTML = '';
            setStatus(false);
            document.getElementById('main-area').style.display = 'none';
            document.getElementById('login-interface').style.display = 'flex';
            document.getElementById('terminal-container').innerHTML = '';
        }

        // --- Tab 管理 ---

        function renderTabs() {
            const tabBar = document.getElementById('tab-bar');
            tabBar.innerHTML = '';
            ptyList.forEach(pty => {
                const tab = document.createElement('div');
                tab.className = 'tab' + (pty.id === currentPtyId ? ' active' : '');
                tab.innerHTML = '<span class="tab-title">' + escHtml(pty.title) + '</span>' +
                    '<span class="tab-close" data-id="' + pty.id + '">&times;</span>';
                tab.addEventListener('click', () => switchPty(pty.id));
                tab.querySelector('.tab-close').addEventListener('click', (e) => {
                    e.stopPropagation();
                    closePty(pty.id);
                });
                tabBar.appendChild(tab);
            });
            const addBtn = document.createElement('div');
            addBtn.className = 'tab tab-add';
            addBtn.textContent = '+';
            addBtn.title = 'New Terminal';
            addBtn.addEventListener('click', createPty);
            tabBar.appendChild(addBtn);
        }

        function escHtml(s) {
            return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        }

        async function createPty() {
            const resp = await fetch('/pty/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({token: token})
            });
            const data = await resp.json();
            if (data.pty_id) {
                ptyList.push({id: data.pty_id, title: data.title});
                switchPty(data.pty_id);
            }
        }

        async function closePty(ptyId) {
            if (ptyList.length <= 1) {
                if (!confirm('Close the last terminal? You can create a new one.')) return;
            }
            await fetch('/pty/' + ptyId + '?token=' + encodeURIComponent(token), { method: 'DELETE' });
            ptyList = ptyList.filter(p => p.id !== ptyId);
            if (currentPtyId === ptyId) {
                const next = ptyList[0];
                if (next) {
                    switchPty(next.id);
                } else {
                    // 所有 PTY 已关闭, 自动创建新的
                    await createPty();
                }
            } else {
                renderTabs();
            }
        }

        function switchPty(ptyId) {
            if (ptyId === currentPtyId) return;
            // 断开当前 WebSocket (静默, 不触发 onclose 提示)
            if (socket) {
                socket.onclose = null;
                socket.onerror = null;
                try { socket.close(); } catch(e) {}
                socket = null;
            }
            currentPtyId = ptyId;
            renderTabs();
            if (term) term.reset();
            connectPty(ptyId);
        }

        function connectPty(ptyId) {
            const dims = getTerminalDimensions();
            const wsUrl = 'ws://' + window.location.hostname + ':' + WS_PORT +
                '?key=' + encodeURIComponent(ACCESS_TOKEN) +
                '&token=' + encodeURIComponent(token) +
                '&pty_id=' + encodeURIComponent(ptyId) +
                '&cols=' + dims.cols + '&rows=' + dims.rows;
            socket = new WebSocket(wsUrl);

            socket.onmessage = (e) => {
                if (typeof e.data === 'string' && e.data.startsWith('{')) {
                    try {
                        const msg = JSON.parse(e.data);
                        if (msg.type === 'pty_exited') {
                            term.write('\x1b[1;31m[Process exited]\x1b[0m\r\n');
                            return;
                        }
                    } catch (_) {}
                }
                term.write(e.data);
            };

            let wsOpened = false;

            socket.onopen = () => {
                wsOpened = true;
                setStatus(true);
            };

            socket.onclose = () => {
                setStatus(false);
                if (!wsOpened) {
                    showLogin();
                } else {
                    term.write('\x1b[1;31m[DISCONNECTED]\x1b[0m\r\n');
                }
            };

            socket.onerror = () => setStatus(false);
        }

        // --- 终端操作 ---

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

        function toggleToolbar(event) {
            event.preventDefault();
            event.stopPropagation();
            const toolbar = document.getElementById('toolbar');
            const toggle = document.getElementById('toolbar-toggle');
            const isOpen = toolbar.classList.toggle('open');
            toggle.textContent = isOpen ? '✕' : '⌨';
            toggle.classList.toggle('shifted', isOpen);
            if (isOpen && term) term.blur();
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

        // --- 初始化 ---

        function initTerminal() {
            const fontSize = isMobile ? 13 : 15;
            term = new Terminal({
                cursorBlink: true,
                theme: { background: '#000000', foreground: '#ffffff', cursor: '#00ff00' },
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
            setTimeout(() => fitAddon.fit(), 50);

            term.onData(data => {
                if (socket && socket.readyState === WebSocket.OPEN) {
                    socket.send(data);
                }
            });

            // resize 事件
            let resizeTimer;
            window.addEventListener('resize', () => {
                fitAddon.fit();
                clearTimeout(resizeTimer);
                resizeTimer = setTimeout(() => { fitAddon.fit(); notifyResize(); }, isMobile ? 300 : 150);
            });
            if (isMobile) {
                window.visualViewport?.addEventListener('resize', () => {
                    clearTimeout(resizeTimer);
                    resizeTimer = setTimeout(() => { fitAddon.fit(); notifyResize(); }, 300);
                });
            }

            // 点击终端区域关闭工具栏
            document.getElementById('terminal-container').addEventListener('click', () => {
                const toolbar = document.getElementById('toolbar');
                if (toolbar.classList.contains('open')) {
                    toolbar.classList.remove('open');
                    document.getElementById('toolbar-toggle').textContent = '⌨';
                    document.getElementById('toolbar-toggle').classList.remove('shifted');
                }
            });

            term.focus();
        }

        async function enterWorkspace() {
            initTerminal();
            // 获取已有 PTY 列表
            try {
                const resp = await fetch('/pty/list?token=' + encodeURIComponent(token));
                const data = await resp.json();
                ptyList = data.ptys || [];
            } catch(e) {
                ptyList = [];
            }

            if (ptyList.length > 0) {
                // 有已有 PTY, 渲染 tab 栏, 连第一个
                currentPtyId = ptyList[0].id;
                renderTabs();
                connectPty(currentPtyId);
            } else {
                // 无 PTY, 自动创建
                await createPty();
            }
        }

        // 页面加载: Cookie 有 token → 验证 → 直接进 workspace
        window.addEventListener('DOMContentLoaded', () => {
            const savedToken = getCookie('rs_token');
            if (!savedToken) return;

            fetch('/verify', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({token: savedToken})
            }).then(r => r.json()).then(data => {
                if (data.valid) {
                    token = savedToken;
                    document.getElementById('login-interface').style.display = 'none';
                    document.getElementById('main-area').style.display = 'flex';
                    enterWorkspace();
                } else {
                    deleteCookie('rs_token');
                }
            }).catch(() => {});
        });
    </script>
</body>
</html>"""

# 注入实际 WS 端口和 Access Token
HTML = HTML.replace("WS_PORT_PLACEHOLDER", str(WS_PORT))
HTML = HTML.replace("ACCESS_TOKEN_PLACEHOLDER", ACCESS_TOKEN)

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = SECRET_KEY


@app.before_request
def check_gate():
    token = request.args.get('token')
    if token == ACCESS_TOKEN:
        session['gate'] = True
    if not session.get('gate'):
        return 'Unauthorized', 401


@app.route('/')
def index():
    return HTML


@app.route('/login', methods=['POST'])
def login():
    ip = request.remote_addr
    now = time.time()

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
        sessions[token] = {'ts': now}
        return jsonify({'success': True, 'token': token})
    else:
        count, first = login_attempts[ip]
        login_attempts[ip] = (count + 1, first)
        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401


@app.route('/verify', methods=['POST'])
def verify():
    data = request.get_json(silent=True) or {}
    token = data.get('token')
    if token and token in sessions:
        if time.time() - sessions[token]['ts'] < TOKEN_EXPIRE:
            return jsonify({'valid': True})
    return jsonify({'valid': False}), 401


@app.route('/pty/list')
def pty_list():
    token = request.args.get('token')
    if not token or token not in sessions:
        return jsonify({'ptys': []}), 401
    now = time.time()
    sessions[token]['ts'] = now
    ptys = []
    for pid, pty in pty_sessions.items():
        ptys.append({
            'id': pid,
            'title': pty['title'],
            'created_at': pty['created_at'],
            'subscriber_count': len(pty['subscribers']),
        })
    ptys.sort(key=lambda p: p['created_at'])
    return jsonify({'ptys': ptys})


@app.route('/pty/create', methods=['POST'])
def pty_create():
    data = request.get_json(silent=True) or {}
    token = data.get('token') or request.args.get('token')
    if not token or token not in sessions:
        return jsonify({'error': 'Unauthorized'}), 401
    pty_id = uuid.uuid4().hex[:8]
    count = len([p for p in pty_sessions.values() if p['token'] == token]) + 1
    pty_sessions[pty_id] = {
        'id': pty_id,
        'token': token,
        'title': 'Shell ' + str(count),
        'created_at': time.time(),
        'proc': None,
        'cols': 80,
        'rows': 24,
        'subscribers': set(),
        'history': '',
        'loop': None,
    }
    sessions[token]['ts'] = time.time()
    return jsonify({'pty_id': pty_id, 'title': pty_sessions[pty_id]['title']})


@app.route('/pty/<pty_id>', methods=['DELETE'])
def pty_delete(pty_id):
    token = request.args.get('token')
    if not token or token not in sessions:
        return jsonify({'error': 'Unauthorized'}), 401
    pty = pty_sessions.get(pty_id)
    if not pty:
        return jsonify({'error': 'Not found'}), 404
    # 通知所有 subscribers 断开
    for ws in list(pty['subscribers']):
        try:
            pty['loop'].call_soon_threadsafe(
                lambda w=ws: asyncio.ensure_future(_safe_send(w, '{"type":"pty_closed"}'))
            )
        except Exception:
            pass
    if pty['proc']:
        try:
            pty['proc'].terminate()
        except Exception:
            pass
    pty_sessions.pop(pty_id, None)
    return jsonify({'success': True})


async def _safe_send(ws, data):
    try:
        await ws.send(data)
    except Exception:
        pass


def broadcast_to_subscribers(pty_id, data):
    """从 reader 线程调用, 向 PTY 的所有 subscribers 广播输出"""
    pty = pty_sessions.get(pty_id)
    if not pty or not pty['subscribers']:
        return
    loop = pty.get('loop')
    if not loop:
        return
    for ws in list(pty['subscribers']):
        try:
            loop.call_soon_threadsafe(
                functools.partial(_schedule_send, ws, data)
            )
        except Exception:
            pty['subscribers'].discard(ws)


def _schedule_send(ws, data):
    asyncio.ensure_future(_safe_send(ws, data))


def start_pty_reader(pty_id):
    """为 PTY 启动独立的 reader 线程"""
    pty = pty_sessions[pty_id]

    def read_pty():
        while True:
            try:
                buf = pty['proc'].read(4096)
                if not buf:
                    break
                # 追加历史
                pty['history'] = (pty['history'] + buf)[-HISTORY_MAX:]
                # 广播给所有 subscribers
                broadcast_to_subscribers(pty_id, buf)
            except Exception:
                break
        # 进程退出
        pty['proc'] = None
        loop = pty.get('loop')
        if loop:
            for ws in list(pty['subscribers']):
                try:
                    loop.call_soon_threadsafe(
                        lambda w=ws: asyncio.ensure_future(
                            _safe_send(w, '{"type":"pty_exited"}')
                        )
                    )
                except Exception:
                    pass

    threading.Thread(target=read_pty, daemon=True).start()


async def ws_handler(ws):
    try:
        query = parse_qs(urlparse(ws.request.path).query)
    except Exception as e:
        logging.error(f"WS parse query failed: {e}")
        await ws.close()
        return
    key = query.get('key', [None])[0]
    token = query.get('token', [None])[0]
    pty_id = query.get('pty_id', [None])[0]
    cols = int(query.get('cols', [80])[0])
    rows = int(query.get('rows', [24])[0])

    if key != ACCESS_TOKEN:
        logging.warning(f"WS bad key: {key}")
        await ws.close()
        return
    if not token or token not in sessions:
        logging.warning(f"WS bad token: {token}")
        await ws.close()
        return
    if time.time() - sessions[token]['ts'] > TOKEN_EXPIRE:
        del sessions[token]
        await ws.close()
        return

    # 续期
    sessions[token]['ts'] = time.time()

    # 处理 pty_id
    if not pty_id or pty_id == 'new':
        logging.warning(f"WS invalid pty_id: {pty_id!r}")
        await ws.close()
        return

    if pty_id not in pty_sessions:
        logging.warning(f"WS pty_id not found: {pty_id!r}")
        await ws.close()
        return

    pty = pty_sessions[pty_id]
    logging.info(f"WS connect: pty_id={pty_id!r} cols={cols} rows={rows} subs={len(pty['subscribers'])}")

    try:
        # 首次连接: spawn PTY + 启动 reader
        with pty_lock:
            if pty['proc'] is None:
                env = os.environ.copy()
                env["TERM"] = "xterm-256color"
                logging.info(f"Spawning cmd.exe for pty {pty_id}")
                proc = PtyProcess.spawn('cmd.exe', env=env, dimensions=(rows, cols))
                pty['proc'] = proc
                pty['cols'] = cols
                pty['rows'] = rows
                pty['loop'] = asyncio.get_running_loop()
                start_pty_reader(pty_id)

        # 发送历史回放 (仅此新 subscriber), 过滤 DA 查询避免终端响应循环
        if pty['history']:
            clean = pty['history'].replace('\x1b[c', '')
            await _safe_send(ws, clean)

        # 注册 subscriber
        pty['subscribers'].add(ws)

        # 对齐终端尺寸
        if cols != pty['cols'] or rows != pty['rows']:
            try:
                pty['proc'].setwinsize(rows, cols)
                pty['cols'] = cols
                pty['rows'] = rows
            except Exception:
                pass

        try:
            async for msg in ws:
                sessions[token]['ts'] = time.time()

                if isinstance(msg, str) and msg.startswith('{'):
                    try:
                        ctrl = json.loads(msg)
                        if ctrl.get('type') == 'resize':
                            nc = ctrl.get('cols', cols)
                            nr = ctrl.get('rows', rows)
                            try:
                                pty['proc'].setwinsize(nr, nc)
                            except Exception:
                                pass
                            pty['cols'] = nc
                            pty['rows'] = nr
                            continue
                        elif ctrl.get('type') == 'logout':
                            break
                    except (json.JSONDecodeError, ValueError):
                        pass

                try:
                    pty['proc'].write(msg)
                except Exception:
                    break
        except ConnectionClosed:
            pass
    except Exception as e:
        logging.error(f"WS handler error for pty {pty_id}: {e}", exc_info=True)
    finally:
        pty['subscribers'].discard(ws)
        logging.info(f"WS disconnect: pty_id={pty_id!r} subs={len(pty['subscribers'])}")


def cleanup_sessions():
    while True:
        time.sleep(3600)
        now = time.time()
        expired_tokens = [t for t, s in sessions.items() if now - s['ts'] > TOKEN_EXPIRE]
        for t in expired_tokens:
            # 清理该 token 的所有 PTY
            for pid in list(pty_sessions.keys()):
                pty = pty_sessions[pid]
                if pty['token'] == t:
                    if pty['proc']:
                        try:
                            pty['proc'].terminate()
                        except Exception:
                            pass
                    pty_sessions.pop(pid, None)
            sessions.pop(t, None)


def start_ws_server():
    threading.Thread(target=cleanup_sessions, daemon=True).start()
    async def _run():
        async with serve(ws_handler, "0.0.0.0", WS_PORT):
            await asyncio.Event().wait()
    asyncio.run(_run())


if __name__ == '__main__':
    if os.environ.get('WERKZEUG_RUN_MAIN') == 'true':
        print(f"Remote Shell Ready: http://{socket.gethostname()}:{HTTP_PORT}/?token={ACCESS_TOKEN}")
        threading.Thread(target=start_ws_server, daemon=True).start()

    app.run(host='0.0.0.0', port=HTTP_PORT, debug=True)
