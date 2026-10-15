import asyncio
import json
import os
import uuid
from pathlib import Path

from jsonschema import Draft7Validator

from . import __version__
from .memory import MEMORY_GUIDANCE, config_lines, sanitize_memory_config


class RpcRejected(RuntimeError):
    pass


class ConnectionLost(RuntimeError):
    pass


class TurnUncertain(RuntimeError):
    pass


class CodexAdapter:
    PERMISSION_PROFILE = "heimdall_readonly"
    SCHEMAS = {"initialize": "v1/InitializeParams.json",
        "config/read": "v2/ConfigReadParams.json",
        "thread/list": "v2/ThreadListParams.json", "thread/read": "v2/ThreadReadParams.json",
        "thread/start": "v2/ThreadStartParams.json", "thread/resume": "v2/ThreadResumeParams.json",
        "thread/fork": "v2/ThreadForkParams.json", "thread/name/set": "v2/ThreadSetNameParams.json",
        "thread/unsubscribe": "v2/ThreadUnsubscribeParams.json",
        "turn/start": "v2/TurnStartParams.json", "turn/interrupt": "v2/TurnInterruptParams.json",
        "thread/turns/list": "v2/ThreadTurnsListParams.json", "thread/items/list": "v2/ThreadItemsListParams.json"}

    def __init__(self, config):
        self.config = config
        self.process = None
        self.reader = None
        self.pending = {}
        self.sequence = 0
        self.subscribers = {}
        self.write_lock = asyncio.Lock()
        self.ready = False
        self.version = "unknown"
        self.validators = {}
        self.rejection_diagnostics = []
        self.new_threads = set()
        self.loaded_threads = set()
        self.thread_modes = {}
        self.connection_id = uuid.uuid4().hex
        self.approval_handler = None
        self.approval_tasks = set()
        self.live_items = {}
        for method, filename in self.SCHEMAS.items():
            if method in {"thread/start", "thread/resume", "thread/fork", "turn/start"}:
                filename = "experimental/" + filename
            schema = json.loads((config.schema_dir / filename).read_text())
            # Server schemas allow unknown keys on some objects; never send invented fields.
            schema["additionalProperties"] = False
            self.validators[method] = Draft7Validator(schema)

    def policy_args(self, mode="readonly"):
        name = self.profile_name(mode)
        rules = {":minimal": "read", str(self.config.cwd): "read"}
        # Host package inventory is a read-only diagnostic, not a sandbox escape.
        # :minimal does not expose /var. Never open all system logs/state.
        package_paths = [Path('/var/log/apt'), Path('/var/lib/dpkg/status'),
                         Path('/var/lib/dpkg/arch')]
        package_paths.extend(Path('/var/log').glob('dpkg.log*'))
        for path in package_paths:
            if path.exists() and not path.is_symlink():
                rules[str(path)] = 'read'
        denied = [self.config.data_dir, self.config.codex_home / "auth.json", self.config.codex_home / "sessions",
                  self.config.schema_dir.parent / "api_bot_key",
                  self.config.codex_home / "config.toml", Path.home() / ".ssh",
                  Path.home() / ".config", Path.home() / ".aws", Path.home() / ".gnupg",
                  Path.home() / "personal-agent", Path("/etc/kakao-radar"),
                  Path("/var/lib/kakao-radar"), Path("/proc")]
        denied.extend(p for p in self.config.codex_home.iterdir() if p.name != "packages")
        for path in denied:
            rules[str(path)] = "deny"
        # This installation stores the Codex executable/runtime under CODEX_HOME/packages.
        # Reopen only the immutable runtime subtree, keeping auth/config/history denied to tools.
        rules[str(self.config.codex_home / "packages")] = "read"
        temporary = mode == 'network' or self.config.temporary_active()
        if temporary:
            rules[str(self.config.cwd)] = 'write'
            rules['/tmp'] = 'write'
            # Protect bridge/runtime code from being rewritten and later run
            # outside the model sandbox. Existing secret denies stay stronger.
            for protected in self.config.temporary_protected():
                if str(protected) not in rules:
                    rules[str(protected)] = 'read'
            for secret_name in ('.env', '.env.local', '.netrc', '.npmrc', '.pypirc', '.git-credentials'):
                rules[str(Path.home() / secret_name)] = 'deny'
                rules[str(self.config.schema_dir.parent / secret_name)] = 'deny'
            for startup_name in ('.bashrc', '.profile', '.bash_profile', '.zshrc', '.zprofile'):
                rules[str(Path.home() / startup_name)] = 'read'
        if mode == "workspace" or temporary:
            for root in self.config.write_roots:
                rules[str(root)] = "write"
        if temporary:
            # 0.162.1 bubblewrap fails to mount non-existent deny/read targets
            # below a writable home (e.g. ~/.aws). Keep all existing protected
            # paths; do not create empty credential files to satisfy mounts.
            rules = {key: value for key, value in rules.items() if key.startswith(':') or Path(key).exists()}
        # Parse quoted filesystem keys inside a TOML inline table, not the -c dotted-key parser.
        filesystem = ", ".join(json.dumps(k) + "=" + json.dumps(v) for k, v in rules.items())
        profile = "{filesystem={" + filesystem + "},network={enabled=" + ('true' if temporary else 'false') + "}}"
        args = ["-c", f"permissions.{name}={profile}",
                "-c", 'apps._default.enabled=false', "-c", 'shell_environment_policy.inherit="core"']
        if temporary and self.config.write_roots:
            root = self.config.write_roots[0]
            settings = {'TMPDIR': str(root), 'XDG_CONFIG_HOME': str(root / 'browser-config'),
                        'XDG_CACHE_HOME': str(root / 'browser-cache')}
            inline = ','.join(key + '=' + json.dumps(value) for key, value in settings.items())
            args += ['-c', 'shell_environment_policy.set={' + inline + '}']
        return args

    def child_env(self):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in
            ("TOKEN", "SECRET", "API_KEY", "PASSWORD", "CREDENTIAL")) and k not in {"SSH_AUTH_SOCK", "DOCKER_HOST"}}
        env["CODEX_HOME"] = str(self.config.codex_home)
        if self.config.temporary_active() and self.config.write_roots:
            # Do not use ~/.config (protected). A short TMPDIR avoids Chromium
            # SingletonSocket exceeding the Unix-domain socket path length.
            root = self.config.write_roots[0]
            env['TMPDIR'] = str(root)
            env['XDG_CONFIG_HOME'] = str(root / 'browser-config')
            env['XDG_CACHE_HOME'] = str(root / 'browser-cache')
        return env

    def approval_policy(self, mode):
        return 'untrusted' if mode in {'workspace', 'network'} or self.config.temporary_active() else 'on-request'

    def profile_name(self, mode):
        return {'readonly':self.PERMISSION_PROFILE, 'workspace':'heimdall_workspace', 'network':'heimdall_network'}[mode]

    def check_temporary_expiry(self):
        if self.config.temporary_access_until and not self.config.temporary_active():
            raise RpcRejected('temporary_access_expired')

    async def open(self):
        if self.ready:
            return
        await self.close()
        proc = await asyncio.create_subprocess_exec(self.config.codex_bin, "--version",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await proc.communicate()
        self.version = output.decode().strip()
        if self.version != (self.config.schema_dir / "VERSION").read_text().strip():
            raise RpcRejected("installed_codex_schema_version_mismatch")
        self.process = await asyncio.create_subprocess_exec(self.config.codex_bin, "app-server",
            "--strict-config", "--listen", "stdio://", "-c", 'sandbox_mode="read-only"',
            "-c", 'approval_policy="on-request"', "-c", 'approvals_reviewer="user"', *self.policy_args(), *self.policy_args("workspace"),
            *(self.policy_args('network') if self.config.enable_network_profile else []),
            stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=8 * 1024 * 1024, cwd=self.config.cwd, env=self.child_env())
        self.new_threads.clear()
        self.loaded_threads.clear()
        self.thread_modes.clear()
        self.connection_id = uuid.uuid4().hex
        self.reader = asyncio.create_task(self.read_loop())
        await self.request("initialize", {"clientInfo": {"name": "heimdall",
            "title": "Heimdall", "version": __version__}, "capabilities": {"experimentalApi": True}})
        await self.send({"method": "initialized", "params": {}})
        self.ready = True

    async def send(self, data):
        async with self.write_lock:
            if not self.process or self.process.returncode is not None:
                raise ConnectionLost("app_server_disconnected")
            try:
                self.process.stdin.write((json.dumps(data) + "\n").encode())
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise ConnectionLost("app_server_disconnected") from None

    async def request(self, method, params):
        self.validators[method].validate(params)
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        future.rpc_method = method
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, self.config.rpc_timeout)
        finally:
            self.pending.pop(request_id, None)

    async def read_loop(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" in message:
                    if "id" in message:
                        if self.approval_handler and message["method"] in {
                            "item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                            task = asyncio.create_task(self.handle_approval(message, self.connection_id))
                            self.approval_tasks.add(task)
                            task.add_done_callback(self.approval_tasks.discard)
                        else:
                            await self.deny_request(message)
                    else:
                        params = message.get("params", {})
                        if message["method"] in {"item/started", "item/completed"}:
                            item = params.get("item", {})
                            key = (params.get("threadId"), params.get("turnId"), item.get("id"))
                            if key[0] in self.subscribers:
                                self.live_items[key] = item
                        queue = self.subscribers.get(params.get("threadId"))
                        if queue:
                            queue.put_nowait(message)
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            code = message["error"].get("code", "unknown")
                            detail = message["error"].get("message", "").lower()
                            self.rejection_diagnostics.append({"method": future.rpc_method, "code": code,
                                "reason_flags": [s for s in ("rollout", "not found", "no messages", "sandbox", "permission",
                                    "model", "unsupported", "invalid", "thread", "resume", "not loaded",
                                    "history", "paginated", "environment", "code-mode", "already", "active",
                                    "in-progress", "truncated", "no rollout", "session", "path", "store") if s in detail]})
                            if future.rpc_method == "thread/resume" and all(s in detail for s in ("thread", "already", "active")):
                                future.set_exception(RpcRejected("thread_already_active"))
                            else:
                                future.set_exception(RpcRejected(f"rpc_rejected:{future.rpc_method}:{code}"))
                        else:
                            future.set_result(message["result"])
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            self.ready = False
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionLost("app_server_disconnected"))
            for queue in self.subscribers.values():
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait({"method": "bridge/disconnected", "params": {}})

    async def handle_approval(self, message, connection):
        try:
            result = await self.approval_handler(message, connection, self.live_items)
            if connection == self.connection_id and self.process and self.process.returncode is None:
                await self.send({"id": message["id"], "result": result})
                if result.get("decision") != "accept":
                    queue = self.subscribers.get(message.get("params", {}).get("threadId"))
                    if queue:
                        readonly = (self.thread_modes.get(message.get('params', {}).get('threadId')) == 'readonly'
                                    and not self.config.temporary_active())
                        queue.put_nowait({"method": "bridge/requestDenied", "params": {
                            "reason": "readonly_policy" if readonly else "approval_not_granted"}})
        except asyncio.CancelledError:
            raise
        except Exception:
            if connection == self.connection_id:
                try:
                    await self.send({"id": message["id"], "result": {"decision": "decline"}})
                except Exception:
                    pass

    @staticmethod
    def denial_notice(reason):
        if reason == 'readonly_policy':
            return ('읽기 모드 정책으로 격리 밖 실행·추가 권한 요청을 자동 차단했습니다. '
                    '사용자가 거부한 것이 아닙니다. 설치 기록 조회는 격리 안에서 가능합니다.')
        if reason == 'unsupported_request':
            return ('브리지가 지원하지 않는 권한·입력 요청을 자동 차단했습니다. '
                    '사용자가 거부한 것이 아닙니다.')
        return ('요청에 필요한 승인이 확보되지 않았습니다. 정책 차단·거부·만료 등이 '
                '가능하며, 이 안내만으로 사용자 거부라고 단정할 수 없습니다.')

    async def deny_request(self, message):
        method = message["method"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            reply = {"result": {"decision": "decline"}}
        elif method == "item/permissions/requestApproval":
            reply = {"result": {"permissions": {}, "scope": "turn"}}
        elif method == "item/tool/requestUserInput":
            reply = {"result": {"answers": {}}}
        else:
            reply = {"error": {"code": -32601, "message": "Unsupported in read-only P0 bridge"}}
        await self.send({"id": message["id"], **reply})
        queue = self.subscribers.get(message.get("params", {}).get("threadId"))
        if queue:
            queue.put_nowait({"method": "bridge/requestDenied", "params": {"reason": "unsupported_request"}})

    async def list_threads(self, cursor=None, search=None):
        params = {"limit": 20, "sortKey": "updated_at", "sortDirection": "desc",
                  "sourceKinds": ["cli", "vscode", "exec", "appServer"],
                  "archived": False, "useStateDbOnly": True, "cwd": str(self.config.cwd)}
        if cursor:
            params["cursor"] = cursor
        if search:
            params["searchTerm"] = search
        result = await self.request("thread/list", params)
        for thread in result["data"]:
            self.config.check_thread(thread)
        return result

    async def read_thread(self, thread_id):
        result = await self.request("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = result["thread"]
        if thread["id"] != thread_id:
            raise RpcRejected("thread_id_mismatch")
        self.config.check_thread(thread)
        return thread

    async def memory_config(self):
        # Config is read by the host RPC, never by a model shell command. Only
        # allowlisted booleans leave this method; do not log/persist the response.
        try:
            result = await self.request("config/read", {"includeLayers": False, "cwd": str(self.config.cwd)})
        except (RpcRejected, ConnectionLost, asyncio.TimeoutError):
            result = None
        return sanitize_memory_config(result)

    async def new_thread(self):
        self.check_temporary_expiry()
        result = await self.request("thread/start", {"cwd": str(self.config.cwd), "permissions": self.PERMISSION_PROFILE,
            "approvalPolicy": self.approval_policy('readonly'), "approvalsReviewer": "user"})
        self.config.check_thread(result["thread"])
        if result["sandbox"].get("type") != ('workspaceWrite' if self.config.temporary_active() else 'readOnly'):
            raise RpcRejected("readonly_not_applied")
        if result.get("activePermissionProfile", {}).get("id") != self.PERMISSION_PROFILE:
            raise RpcRejected("restricted_profile_not_applied")
        self.new_threads.add(result["thread"]["id"])
        self.loaded_threads.add(result["thread"]["id"])
        self.thread_modes[result["thread"]["id"]] = "readonly"
        return result["thread"]

    async def rename_thread(self, thread_id, name):
        await self.read_thread(thread_id)
        await self.request("thread/name/set", {"threadId": thread_id, "name": name})

    async def history(self, thread_id, limit=10):
        await self.read_thread(thread_id)
        result = await self.request("thread/turns/list", {"threadId": thread_id,
            "limit": limit, "sortDirection": "desc", "itemsView": "full"})
        return result["data"]

    async def fork_thread(self, thread_id):
        self.check_temporary_expiry()
        await self.read_thread(thread_id)
        result = await self.request("thread/fork", {"threadId": thread_id,
            "cwd": str(self.config.cwd), "permissions": self.PERMISSION_PROFILE,
            "approvalPolicy": self.approval_policy('readonly'), "approvalsReviewer": "user", "excludeTurns": True,
            "deferGoalContinuation": True})
        self.validate_policy(result, "readonly")
        self.config.check_thread(result["thread"])
        self.loaded_threads.add(result["thread"]["id"])
        self.new_threads.add(result["thread"]["id"])
        self.thread_modes[result["thread"]["id"]] = "readonly"
        return result["thread"]

    async def release_thread(self, thread_id):
        if thread_id not in self.loaded_threads:
            return False
        if thread_id in self.subscribers or await self.thread_is_active(thread_id):
            raise RpcRejected("cannot_release_active_turn")
        await self.request("thread/unsubscribe", {"threadId": thread_id})
        self.loaded_threads.discard(thread_id)
        self.new_threads.discard(thread_id)
        self.thread_modes.pop(thread_id, None)
        return True

    async def resume_thread(self, thread_id, mode="readonly"):
        self.check_temporary_expiry()
        profile = self.profile_name(mode)
        if thread_id in self.loaded_threads and self.thread_modes.get(thread_id) != mode:
            # 0.162.1 returns cached settings when resuming an already loaded
            # actor. Unload our idle actor before changing its permission mode.
            await self.release_thread(thread_id)
        result = await self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True,
            "permissions": profile, "approvalPolicy": self.approval_policy(mode),
            "approvalsReviewer": "user"})
        self.config.check_thread(result["thread"])
        self.validate_policy(result, mode)
        self.loaded_threads.add(thread_id)
        self.thread_modes[thread_id] = mode
        return result["thread"]

    def validate_policy(self, response, mode):
        expected = self.profile_name(mode)
        kind = "readOnly" if mode == "readonly" and not self.config.temporary_active() else "workspaceWrite"
        if response.get("sandbox", {}).get("type") != kind or response.get("activePermissionProfile", {}).get("id") != expected:
            raise RpcRejected("restricted_profile_not_applied")

    async def interrupt(self, thread_id, turn_id):
        await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def thread_is_active(self, thread_id):
        if thread_id in self.new_threads:
            # This server created it; no first turn or persisted rollout exists yet.
            return False
        result = await self.request("thread/turns/list", {"threadId": thread_id,
            "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded"})
        turns = result.get("data", [])
        if not turns:
            return False
        status = turns[0].get("status")
        if status in {"completed", "failed", "interrupted"}:
            return False
        # In-progress or an unknown status is not safe to resume concurrently.
        return True

    async def run_turn(self, thread_id, prompt, on_started, progress, permission_mode="readonly", image_paths=()):
        self.check_temporary_expiry()
        if permission_mode not in {"readonly", "workspace", "network"} or (permission_mode == "workspace" and not self.config.write_roots):
            raise RpcRejected("write_workspace_not_configured")
        if permission_mode == 'network' and (not self.config.enable_network_profile or not self.config.temporary_active()):
            raise RpcRejected('network_window_expired')
        profile = self.profile_name(permission_mode)
        policy = self.approval_policy(permission_mode)
        images = []
        if len(image_paths) > 10:
            raise RpcRejected("too_many_images")
        for value in image_paths:
            path = Path(value)
            assets = self.config.data_dir / "assets"
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.parent != assets or path.resolve().parent != assets.resolve()
                or path.suffix not in {".jpg", ".png"}):
                raise RpcRejected("unsafe_image_path")
            images.append({"type": "localImage", "path": str(path)})
        # Release must happen before installing the current turn's subscription.
        if thread_id not in self.new_threads:
            await self.resume_thread(thread_id, permission_mode)
        context = MEMORY_GUIDANCE + "\nReported configuration (not per-turn memory status):\n" + config_lines(await self.memory_config())
        if self.config.temporary_active():
            context += ('\nTemporary operator-approved access: ordinary files under the tracked home and /tmp are writable, '
                'and command network access is enabled until the fixed deadline. Secrets, bridge code and runtime code '
                'remain protected. Command approvals still apply. Do not claim the normal mode is currently read-only '
                'or network-blocked. Do not change router settings unless explicitly requested by the user. '
                'Browser temporary/cache/config directories are provided through TMPDIR and XDG_CONFIG_HOME/XDG_CACHE_HOME.')
        self.check_temporary_expiry()
        queue = asyncio.Queue(maxsize=2000)
        self.subscribers[thread_id] = queue
        turn_id = None
        try:
            try:
                result = await self.request("turn/start", {"threadId": thread_id,
                    "input": [{"type": "text", "text": prompt},
                              {"type": "text", "text": context}, *images],
                    "permissions": profile,
                    "approvalPolicy": policy, "approvalsReviewer": "user"})
            except RpcRejected:
                raise
            except (ConnectionLost, asyncio.TimeoutError):
                # A missing response is not evidence that a new thread is idle.
                self.new_threads.discard(thread_id)
                raise TurnUncertain("turn_start_acceptance_unknown") from None
            turn_id = result["turn"]["id"]
            self.thread_modes[thread_id] = permission_mode
            self.new_threads.discard(thread_id)
            on_started(turn_id)  # Must persist before the next await.
            items = {}
            async def collect():
                while True:
                    event = await queue.get()
                    method, params = event["method"], event.get("params", {})
                    if method == "bridge/disconnected":
                        raise TurnUncertain("connection_lost_during_turn")
                    if method == "bridge/requestDenied":
                        await progress(self.denial_notice(params.get('reason')))
                        continue
                    event_turn = params.get("turnId") or params.get("turn", {}).get("id")
                    if event_turn != turn_id:
                        continue
                    if method == "item/completed":
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage":
                            items[item["id"]] = item
                    elif method == "turn/completed":
                        turn = params["turn"]
                        for item in turn.get("items", []):
                            if item.get("type") == "agentMessage":
                                items[item["id"]] = item
                        return {"status": turn["status"], "text": final_text(list(items.values())),
                                "turn_id": turn_id, "error_code": turn_error_code(turn)}
            try:
                return await asyncio.wait_for(collect(), self.config.runtime)
            except asyncio.TimeoutError:
                await self.interrupt(thread_id, turn_id)
                try:
                    return await asyncio.wait_for(collect(), 15)
                except asyncio.TimeoutError:
                    raise TurnUncertain("timeout_without_stop_confirmation") from None
        finally:
            self.subscribers.pop(thread_id, None)
            self.live_items = {key: item for key, item in self.live_items.items() if key[0] != thread_id}

    async def reconcile(self, thread_id, turn_id):
        result = await self.request("thread/turns/list", {"threadId": thread_id, "limit": 20, "itemsView": "full"})
        turns = result.get("data", [])
        turn = next((t for t in turns if t["id"] == turn_id), None)
        if turn and turn["status"] in {"completed", "failed", "interrupted"}:
            return {"status": turn["status"], "text": final_text(turn.get("items", [])),
                    "turn_id": turn_id, "error_code": turn_error_code(turn)}
        return None

    async def close(self):
        self.ready = False
        for task in list(self.approval_tasks):
            task.cancel()
        await asyncio.gather(*list(self.approval_tasks), return_exceptions=True)
        self.approval_tasks.clear()
        if self.process and self.process.returncode is None:
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)
        self.process = None


def final_text(items):
    messages = [i for i in items if i.get("type") == "agentMessage"]
    finals = [i for i in messages if i.get("phase") == "final_answer"]
    # Legacy phase=None is compatible only from completed authoritative items.
    selected = finals or [i for i in messages if not i.get("phase")]
    return "\n\n".join(i.get("text", "") for i in selected)


def turn_error_code(turn):
    """Only stable protocol labels, never raw model/backend error messages."""
    error = turn.get("error") or {}
    value = error.get("codexErrorInfo")
    labels = {"contextWindowExceeded", "sessionBudgetExceeded", "usageLimitExceeded", "rateLimitExceeded",
              "flexUnavailable", "serverOverloaded", "cyberPolicy", "misalignmentPolicyViolation",
              "tooManyDenials", "internalServerError", "unauthorized", "badRequest",
              "threadRollbackFailed", "sandboxError", "other"}
    if isinstance(value, str) and value in labels:
        return value
    if isinstance(value, dict):
        for label in ("httpConnectionFailed", "responseStreamConnectionFailed", "responseStreamDisconnected",
                      "responseTooManyFailedAttempts"):
            if label in value:
                return label
    return None
