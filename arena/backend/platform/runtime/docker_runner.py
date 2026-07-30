"""Docker Runner(里程碑 4 第二组件)。

用 asyncio + subprocess 管理 docker 容器进程,负责:

- ``start_session``: ``docker run -i --rm --network=none --memory=512m --cpus=0.5``
  起一个容器,保留 stdin/stdout pipe,返回 session_id。
- ``send``: 写 stdin(request + "\\n"),读 stdout 一行 response,超时判异常。
- ``stop_session`` / ``cleanup_all``: 停止容器。

对调用方(MatchRunner)透明的是:容器内的 entrypoint 可能是 JSON bot
(``python main.py``,stdin/stdout 直连),也可能是 TCP 桥
(``python tcp_bridge.py --bot-entry ...``,桥在容器内自起 socket server)。
平台始终用 stdin/stdout 与容器通信。

资源限制严格遵循 CONTRACT.md 第二节:
- ``--network=none``(隔离网络)
- ``--memory=512m``
- ``--cpus=0.5``
- 决策超时默认 60s(超时由调用方处理为 fold)
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Docker 资源限制(与 CONTRACT.md 对齐)
NETWORK_MODE = "none"
MEMORY_LIMIT = "512m"
CPU_LIMIT = "0.5"
DEFAULT_ACTION_TIMEOUT = 60.0


@dataclass
class _Session:
    """单个容器会话的运行时句柄。"""

    session_id: str
    image: str
    proc: asyncio.subprocess.Process
    # stdout 读缓冲(按行切分),便于一次读完整一行
    _stdout_buf: str = field(default="", init=False)
    closed: bool = False

    @property
    def stdin(self):
        return self.proc.stdin

    @property
    def stdout(self):
        return self.proc.stdout


class DockerRunner:
    """管理一组 docker 容器会话,每会话对应一个 bot 实例。

    用法::

        runner = DockerRunner()
        sid_a = await runner.start_session("arena-bot-1:v1", "botA")
        sid_b = await runner.start_session("arena-bot-2:v1", "botB")
        resp = await runner.send(sid_a, '{"requests":[...]}', timeout=60)
        await runner.cleanup_all()
    """

    def __init__(self, *, docker_bin: str = "docker") -> None:
        self._docker_bin = docker_bin
        self._sessions: dict[str, _Session] = {}
        self._id_counter = itertools.count(1)

    # ── 容器生命周期 ────────────────────────────────────────

    async def start_session(self, image: str, name_hint: str = "") -> str:
        """启动一个容器会话,返回 session_id。

        执行 ``docker run -i --rm --network=none --memory=512m --cpus=0.5 <image>``。
        保留 stdin/stdout pipe 供后续 send 读写。
        """
        session_id = f"sess-{next(self._id_counter)}-{uuid.uuid4().hex[:8]}"
        # 容器名(便于 docker ps 识别);docker 要求唯一,加 uuid 后缀
        container_name = f"arena-{name_hint or 'bot'}-{session_id}"[:63].rstrip("-")
        cmd = [
            self._docker_bin, "run",
            "-i",                       # 交互(stdin 连通)
            "--rm",                     # 退出即删,不留容器
            "--network", NETWORK_MODE,  # 隔离网络
            "--memory", MEMORY_LIMIT,
            "--cpus", CPU_LIMIT,
            "--pids-limit", "128",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,size=64m,mode=1777",
            "--name", container_name,
            image,
        ]
        logger.info("start_session %s image=%s name=%s",
                    session_id, image, container_name)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._sessions[session_id] = _Session(
            session_id=session_id, image=image, proc=proc,
        )
        return session_id

    async def send(self, session_id: str, request: str,
                   timeout: float = DEFAULT_ACTION_TIMEOUT) -> str:
        """向容器发一行 request,读回一行 response。

        - request 末尾自动加 ``\\n``;encode utf-8。
        - 读 stdout 直到遇到 ``\\n``(一行);decode utf-8。
        - ``timeout`` 秒内未读完 → 抛 ``asyncio.TimeoutError``(调用方通常判 fold)。
        - 容器已关闭 → 抛 ``RuntimeError``。
        """
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"未知 session_id: {session_id}")
        if session.closed or session.proc.returncode is not None:
            session.closed = True
            raise RuntimeError(f"session {session_id} 已退出(rc="
                               f"{session.proc.returncode})")

        payload = (request + "\n").encode("utf-8")
        assert session.stdin is not None
        session.stdin.write(payload)
        try:
            await asyncio.wait_for(session.stdin.drain(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise asyncio.TimeoutError(
                f"session {session_id} stdin drain 超时({timeout}s)") from exc

        response = await asyncio.wait_for(
            session.stdout.readline(), timeout=timeout)
        if not response:
            # 容器关闭了 stdout → 进程退出或崩溃
            session.closed = True
            stderr_tail = ""
            if session.proc.stderr is not None:
                try:
                    err = await asyncio.wait_for(
                        session.proc.stderr.read(), timeout=1.0)
                    stderr_tail = err.decode("utf-8", "replace")[-500:]
                except (asyncio.TimeoutError, Exception):
                    stderr_tail = ""
            raise RuntimeError(
                f"session {session_id} stdout 已关闭(rc="
                f"{session.proc.returncode}); stderr: {stderr_tail!r}")
        return response.decode("utf-8", "replace").rstrip("\r\n")

    async def stop_session(self, session_id: str) -> None:
        """停止单个 session:terminate 容器进程。

        容器以 ``--rm`` 起的,terminate stdin/docker run 进程后 docker 会回收容器。
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        await _terminate_process(session.proc)
        session.closed = True

    async def cleanup_all(self) -> None:
        """停止所有活跃 session。"""
        ids = list(self._sessions.keys())
        for sid in ids:
            await self.stop_session(sid)

    # ── 辅助(测试/调试用)────────────────────────────────────

    def has_session(self, session_id: str) -> bool:
        return session_id in self._sessions

    @property
    def active_count(self) -> int:
        return len(self._sessions)


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """优雅终止子进程:先 terminate,3s 后 kill。"""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
