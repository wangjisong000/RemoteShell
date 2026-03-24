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

HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Claude Web Console</title>
    <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css"/>
    <style>
        body { margin: 0; background: #000; color: #fff; font-family: sans-serif; height: 100vh; display: flex; flex-direction: column; }
        #terminal-container { flex: 1; padding: 10px; }
        .login-screen { position: fixed; inset: 0; background: #1e1e1e; display: flex; align-items: center; justify-content: center; z-index: 100; }
        .login-box { background: #2d2d2d; padding: 30px; border-radius: 8px; text-align: center; }
        input { padding: 10px; margin: 10px; border: 1px solid #444; background: #1a1a1a; color: #00ff00; border-radius: 4px; }
        button { padding: 10px 20px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
    </style>
</head>
<body>
    <div id="login-interface" class="login-screen">
        <div class="login-box">
            <h2>Claude Remote Shell</h2>
            <input type="text" id="user" placeholder="Username"><br>
            <input type="password" id="pass" placeholder="Password"><br>
            <button onclick="login()">Connect Session</button>
        </div>
    </div>

    <div id="terminal-container"></div>

    <script>
        let term, socket, token;
        const fitAddon = new FitAddon.FitAddon();

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

        function initShell() {
            // 1. 初始化终端，设置合适的字体大小
            term = new Terminal({
                cursorBlink: true,
                theme: { background: '#000000' },
                fontSize: 15,
                fontFamily: '"Cascadia Code", "Consolas", monospace',
                letterSpacing: 0,
                lineHeight: 1.1
            });
            
            term.loadAddon(fitAddon);
            term.open(document.getElementById('terminal-container'));
            fitAddon.fit();

            // 2. 建立 WebSocket
            const wsUrl = `ws://${window.location.hostname}:5001?token=${token}`;
            socket = new WebSocket(wsUrl);

            // 3. 核心：双向绑定
            // 后端 -> 前端 (显示)
            socket.onmessage = (e) => term.write(e.data);

            // 前端 -> 后端 (捕捉所有按键：Tab, Ctrl+C, 箭头)
            term.onData(data => {
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(data);
                }
            });

            socket.onopen = () => {
                term.write('\\x1b[1;32m[CONNECTED] You can now type directly in this window.\\x1b[0m\\r\\n');
                // 自动运行一次 claude 命令 (可选)
                // socket.send('claude\\r\\n'); 
            };

            // 窗口大小改变时自动适应
            window.onresize = () => fitAddon.fit();
            
            // 聚焦终端，确保一进来就能打字
            term.focus();
        }
    </script>
</body>
</html>
"""

class SimpleHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode('utf-8'))
        else: super().do_GET()

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
            else: self.send_response(401); self.end_headers()

async def ws_handler(ws):
    query = parse_qs(urlparse(ws.path).query)
    token = query.get('token', [None])[0]
    if not token or token not in terminals:
        await ws.close(); return

    # 重点：设置启动大小，增加环境变量以获得更好的颜色支持
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    
    # cols 和 rows 要和前端 fit 后的效果接近，否则 UI 会错位
    proc = PtyProcess.spawn('cmd.exe', env=env, dimensions=(40, 120))
    loop = asyncio.get_running_loop()

    def read_pty():
        while True:
            try:
                data = proc.read(4096)
                if not data: break
                loop.call_soon_threadsafe(lambda d=data: asyncio.create_task(ws.send(d)))
            except: break

    threading.Thread(target=read_pty, daemon=True).start()

    try:
        async for msg in ws:
            proc.write(msg)
    finally:
        proc.terminate()

async def main():
    async with serve(ws_handler, "0.0.0.0", 5001):
        server = HTTPServer(("0.0.0.0", 5000), SimpleHandler)
        print("🚀 Claude Console Ready: http://localhost:5000")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())