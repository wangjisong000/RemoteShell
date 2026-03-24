import asyncio
import json
import os
import threading
import secrets
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from websockets.server import serve
from winpty import PtyProcess

# --- 配置 ---
USERNAME = "admin"
PASSWORD = "123456"
terminals = {}

HTML = r"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <title>Remote Shell</title>
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
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

        #terminal-container { flex: 1; padding: 8px; }
        .xterm { padding: 4px; }

        @supports (padding-bottom: env(safe-area-inset-bottom)) {
            #terminal-container { padding-bottom: calc(8px + env(safe-area-inset-bottom)); }
        }

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

        @media screen and (max-width: 768px) {
            .login-box { padding: 24px 20px; }
            .login-box h2 { font-size: 1.3rem; }
            input { font-size: 16px; padding: 12px 10px; }
            button { font-size: 16px; padding: 12px 16px; }

            .mobile-toolbar { display: flex; }

            #terminal-container { padding: 4px; }
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
    <div id="login-interface" class="login-screen">
        <div class="login-box">
            <h2>Remote Shell</h2>
            <input type="text" id="user" placeholder="Username" autocomplete="off" autocapitalize="off"><br>
            <input type="password" id="pass" placeholder="Password"><br>
            <button onclick="login()">Connect</button>
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

    <script>
        let term, socket, token;
        const fitAddon = new FitAddon.FitAddon();
        const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);

        function login() {
            const user = document.getElementById('user').value;
            const pass = document.getElementById('pass').value;
            fetch('/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({username: user, password: pass})
            }).then(r => r.json()).then(data => {
                if(data.success) {
                    token = data.token;
                    document.getElementById('login-interface').style.display = 'none';
                    initShell();
                } else alert('Unauthorized');
            });
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
            fitAddon.fit();

            const dims = getTerminalDimensions();
            const wsUrl = `ws://${window.location.hostname}:5001?token=${token}&cols=${dims.cols}&rows=${dims.rows}`;
            socket = new WebSocket(wsUrl);

            socket.onmessage = (e) => term.write(e.data);

            term.onData(data => {
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(data);
                }
            });

            socket.onopen = () => {
                term.write('\x1b[1;32m[CONNECTED]\x1b[0m\r\n');
                if (isMobile) {
                    term.write('\x1b[33m[Mobile Mode] Use toolbar for shortcuts\x1b[0m\r\n');
                }
            };

            window.addEventListener('resize', () => {
                fitAddon.fit();
                if (isMobile) {
                    setTimeout(() => fitAddon.fit(), 100);
                }
            });

            if (isMobile) {
                window.visualViewport?.addEventListener('resize', () => {
                    setTimeout(() => fitAddon.fit(), 200);
                });
            }

            term.focus();
        }
    </script>
</body>
</html>"""

class SimpleHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/login':
            content_length = int(self.headers.get('Content-Length', 0))
            data = json.loads(self.rfile.read(content_length).decode())
            if data.get('username') == USERNAME and data.get('password') == PASSWORD:
                token = secrets.token_hex(16)
                terminals[token] = True
                self.send_response(200)
                self.send_header('Content-type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({'success': True, 'token': token}).encode())
            else:
                self.send_response(401)
                self.end_headers()

async def ws_handler(ws):
    query = parse_qs(urlparse(ws.path).query)
    token = query.get('token', [None])[0]
    cols = int(query.get('cols', [80])[0])
    rows = int(query.get('rows', [24])[0])

    if not token or token not in terminals:
        await ws.close()
        return

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    proc = PtyProcess.spawn('cmd.exe', env=env, dimensions=(rows, cols))
    loop = asyncio.get_running_loop()

    def read_pty():
        while True:
            try:
                data = proc.read(4096)
                if not data:
                    break
                loop.call_soon_threadsafe(lambda d=data: asyncio.create_task(ws.send(d)))
            except:
                break

    threading.Thread(target=read_pty, daemon=True).start()

    try:
        async for msg in ws:
            proc.write(msg)
    finally:
        proc.terminate()

async def main():
    async with serve(ws_handler, "0.0.0.0", 5001):
        server = HTTPServer(("0.0.0.0", 5000), SimpleHandler)
        print("Remote Shell Ready: http://home.coopez.cn:5000")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())