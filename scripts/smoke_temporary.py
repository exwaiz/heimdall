"""Actual temporary sandbox/browser and expired-policy checks; no router access."""
import asyncio
from dataclasses import replace
import http.server
import json
from pathlib import Path
import tempfile
import threading
import time

from heimdall_bridge.config import Config
from heimdall_bridge.codex import CodexAdapter

ROOT = Path(__file__).resolve().parents[1]

PROBE = r'''
import errno,json,os,sys
from pathlib import Path
from playwright.sync_api import sync_playwright
result={}
ordinary=Path(sys.argv[1]);ordinary.write_text('OK');result['general_home_write']=ordinary.read_text()=='OK'
for key,path in zip(('private_read_blocked','code_write_blocked'),sys.argv[2:4]):
 try:
  fd=os.open(path,os.O_RDONLY if key=='private_read_blocked' else os.O_WRONLY);os.close(fd);result[key]=False
 except OSError as error:result[key]=error.errno in (errno.EACCES,errno.EPERM,errno.EROFS)
result['browser_environment_set']='browser-config' in os.environ.get('XDG_CONFIG_HOME','')
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/home/haaland/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome',headless=True,timeout=15000,args=['--no-sandbox','--disable-dev-shm-usage','--disable-background-networking'])
 page=browser.new_page();page.goto(sys.argv[4],timeout=10000);page.locator('button').click()
 result['network_page_and_click']=page.locator('button').inner_text()=='PASS'
 page.screenshot(path=str(ordinary.with_suffix('.png')));result['screenshot_saved']=ordinary.with_suffix('.png').is_file()
 browser.close()
print(json.dumps(result))
'''

EXPIRED = r'''
import errno,json,os,socket,sys
result={}
try:
 fd=os.open(sys.argv[1],os.O_WRONLY);os.close(fd);result['expired_write_blocked']=False
except OSError as error:result['expired_write_blocked']=error.errno in (errno.EACCES,errno.EPERM,errno.EROFS)
try:
 connection=socket.create_connection(('127.0.0.1',int(sys.argv[2])),timeout=2);connection.close();result['expired_network_blocked']=False
except OSError:result['expired_network_blocked']=True
print(json.dumps(result))
'''


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'<button onclick="this.textContent=\'PASS\'">Click</button>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def smoke():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='temporary-access-smoke-', dir='/home/haaland') as directory:
            root = Path(directory)
            config = replace(Config.load(ROOT / 'config.toml', live=False), data_dir=root / 'private',
                temporary_access_until=time.time() + 180)
            config.data_dir.mkdir(mode=0o700)
            secret = config.data_dir / 'sentinel'
            secret.write_text('test fixture')
            adapter = CodexAdapter(config)
            async def run(code, python, *args, config=config):
                client = CodexAdapter(config)
                process = await asyncio.create_subprocess_exec(config.codex_bin, 'sandbox', '--permission-profile',
                    'heimdall_readonly', *client.policy_args(), '--', python, '-c', code, *args,
                    cwd=config.cwd, env=client.child_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                out, err = await asyncio.wait_for(process.communicate(), 40)
                if process.returncode:
                    raise RuntimeError('temporary_sandbox_probe_failed:' + err.decode()[-1000:])
                result = json.loads(out)
                assert all(result.values()), result
                return result
            results = await run(PROBE, '/home/haaland/.local/share/router-browser-venv/bin/python',
                str(root / 'ordinary.txt'), str(secret), str(ROOT / 'src/heimdall_bridge/__init__.py'),
                f'http://127.0.0.1:{server.server_port}')
            try:
                await adapter.open()
                await adapter.new_thread()
                results['appserver_temporary_profile_validated'] = True
            finally:
                await adapter.close()
            results.update(await run(EXPIRED, '/usr/bin/python3', str(root / 'ordinary.txt'),
                str(server.server_port), config=replace(config, temporary_access_until=time.time() - 1)))
            return results
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    print(json.dumps(asyncio.run(smoke())))
