"""Real native sandbox checks; reads metadata only and never installs packages."""
import asyncio
import json
from pathlib import Path

from heimdall_bridge.codex import CodexAdapter
from heimdall_bridge.config import Config

ROOT = Path(__file__).resolve().parents[1]
PROBE = r'''
import errno,json,os,socket,subprocess,sys
paths=['/var/log/apt/history.log','/var/log/dpkg.log','/var/lib/dpkg/status']
result={p:bool(open(p,'rb').read(64)) for p in paths}
p=subprocess.run(['/usr/bin/dpkg-query','-W','-f=${Package}\n'],capture_output=True,text=True)
result['dpkg_query']=p.returncode==0 and len(p.stdout.splitlines())>0
for path in paths:
 try:
  fd=os.open(path,os.O_WRONLY);os.close(fd);result['no_write:'+path]=False
 except OSError as e:result['no_write:'+path]=e.errno in (errno.EPERM,errno.EACCES,errno.EROFS)
try:
 socket.socket(socket.AF_INET,socket.SOCK_STREAM);result['network_blocked']=False
except OSError:result['network_blocked']=True
try:
 fd=os.open(sys.argv[1],os.O_RDONLY);os.close(fd);result['auth_still_blocked']=False
except OSError:result['auth_still_blocked']=True
print(json.dumps(result))
'''


async def smoke():
    config = Config.load(ROOT / 'config.toml', live=False)
    adapter = CodexAdapter(config)
    results = {}
    for mode in ('readonly', 'workspace'):
        proc = await asyncio.create_subprocess_exec(config.codex_bin, 'sandbox', '--permission-profile',
            adapter.profile_name(mode), *adapter.policy_args(mode), '--', '/usr/bin/python3', '-c', PROBE,
            str(config.codex_home / 'auth.json'),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=config.cwd, env=adapter.child_env())
        out, err = await asyncio.wait_for(proc.communicate(), 25)
        if proc.returncode:
            raise RuntimeError('package_probe_failed:' + err.decode()[-600:])
        result = json.loads(out)
        assert all(result.values()), result
        results[mode] = result
    return results


if __name__ == '__main__':
    print(json.dumps(asyncio.run(smoke())))
