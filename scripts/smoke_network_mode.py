"""Real Codex /mode network with a localhost fixture and scripted owner approval."""
import asyncio
from dataclasses import replace
import http.server
import json
from pathlib import Path
import shlex
import tempfile
import threading

from heimdall_bridge.__main__ import update
from heimdall_bridge.bridge import Bridge
from heimdall_bridge.config import Config
from heimdall_bridge.db import Store
from heimdall_bridge.handoff import HandoffCodexAdapter
from heimdall_bridge.transport import FakeTelegramTransport

ROOT = Path(__file__).resolve().parents[1]


class Page(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'NETWORK_MODE_OK')
    def log_message(self, *args): pass


async def smoke():
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with tempfile.TemporaryDirectory(prefix='network-mode-smoke-', dir='/home/haaland') as directory:
            config = replace(Config.load(ROOT / 'config.toml', live=False),
                data_dir=Path(directory) / 'private', user_id=7, chat_id=7, runtime=120, approval_timeout=15)
            adapter = HandoffCodexAdapter(config)
            store = Store(config.data_dir / 'bridge.sqlite3')
            transport = FakeTelegramTransport()
            bridge = Bridge(config, store, adapter, transport, conflict_check=lambda *_:False)
            try:
                await adapter.open()
                await bridge.handle(update(1, '/new Network mode verification'))
                thread_id = store.binding(7)
                await bridge.handle(update(2, '/mode network'))
                target = Path(directory) / 'ordinary-result.txt'
                code = ("import urllib.request,pathlib; result=urllib.request.urlopen("
                    + repr(f'http://127.0.0.1:{server.server_port}') + ",timeout=5).read().decode(); "
                    + f"pathlib.Path({str(target)!r}).write_text(result); print(result)")
                command = '/usr/bin/python3 -c ' + shlex.quote(code)
                await bridge.handle(update(3, 'Execute exactly this harmless fixture command once. '
                    'Do not request extra permissions; do not access other files or URLs. Then reply NETWORK_MODE_VERIFIED.\n' + command))
                task = asyncio.create_task(bridge.work_once())
                clicked = 0
                while not task.done():
                    while clicked < len(transport.buttons):
                        chat, mid, markup = transport.buttons[clicked]
                        clicked += 1
                        await bridge.approvals.callback({'id':f'fixture-{clicked}', 'from':{'id':7},
                            'message':{'message_id':mid,'chat':{'id':chat,'type':'private'}},
                            'data':markup['inline_keyboard'][0][0]['callback_data']})
                    await asyncio.sleep(.05)
                await task
                job = store.job(1)
                if job['state'] != 'completed':
                    raise RuntimeError('network_mode_fixture_failed:' + str(job['error_code']))
                assert target.read_text() == 'NETWORK_MODE_OK'
                assert clicked > 0
                assert thread_id not in adapter.owners
                assert job['permission_mode'] == 'network'
                await bridge.handle(update(4, '/mode read'))
                assert store.permission_mode(7) == 'readonly' and store.network_deadline(7) == 0
                await bridge.handle(update(5, 'Reply exactly NETWORK_READ_RESET_OK. Do not use tools.'))
                await bridge.work_once()
                job = store.job(2)
                assert job['state'] == 'completed', job['error_code']
                assert job['permission_mode'] == 'readonly'
                assert 'NETWORK_READ_RESET_OK' in job['response_text']
                return {'real_network_and_home_write':True, 'real_owner_approval_rpc':True,
                    'terminal_owner_released':True, 'read_mode_restored_and_resumed':True, 'status':'passed'}
            finally:
                await adapter.close()
                store.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


if __name__ == '__main__':
    print(json.dumps(asyncio.run(smoke())))
