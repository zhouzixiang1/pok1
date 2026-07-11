"""pok-arena CLI(typer)。

入口:``python -m arena.backend.cli`` 或 ``pok-arena``(pyproject scripts 注册)。

子命令:
  serve    启动 TCP 平台 + Web(同进程),长驻接 bot 打 70 局
  connect  无策略协议自测客户端(protocol exerciser),内置最小 call/check 跟随器
  thp      THP 棋谱导出/列表(thp show <id> / thp list)
  status   查询运行中平台状态(HTTP GET /api/state)

不做 bot 池 run/list 命令(arena 不碰进化系统)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import time
import urllib.request
from pathlib import Path

import typer

from .main import WEB_DEFAULT_PORT, run_server
from .server.match_manager import MatchManager
from .server.tcp_server import DEFAULT_HOST, DEFAULT_PORT

app = typer.Typer(
    help="pok-arena: 国赛德州扑克对弈平台 web 复刻",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
thp_app = typer.Typer(help="THP 棋谱导出/列表", no_args_is_help=True)
app.add_typer(thp_app, name="thp")

DEFAULT_RECORDS_DIR = Path("~/.local/share/pok-arena/records").expanduser()


def _setup_logging(log_file: str | None, log_level: str) -> None:
    level = getattr(logging, log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level, handlers=handlers,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


# ── serve ────────────────────────────────────────────────────

@app.command()
def serve(
    host: str = typer.Option(DEFAULT_HOST, "--host", help="绑定地址(默认 127.0.0.1;0.0.0.0 会告警)"),
    tcp_port: int = typer.Option(DEFAULT_PORT, "--tcp-port", help="TCP 平台端口(默认 50101)"),
    web_port: int = typer.Option(WEB_DEFAULT_PORT, "--web-port", help="Web 端口(默认 50180)"),
    max_matches: int | None = typer.Option(None, "--max-matches", help="最多跑几场后退出"),
    once: bool = typer.Option(False, "--once", help="只跑一场(等价 --max-matches 1)"),
    records_dir: Path = typer.Option(DEFAULT_RECORDS_DIR, "--records-dir", help="THP/索引目录"),
    event_name: str = typer.Option("CCGC", "--event-name", help="赛事名(写入 THP footer)"),
    hands_per_match: int = typer.Option(70, "--hands-per-match", help="每场手数(默认 70;测试可调小)"),
    log_file: str | None = typer.Option(None, "--log-file", help="日志文件(默认仅 stderr)"),
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG/INFO/WARNING/ERROR"),
) -> None:
    """启动 TCP 平台 + Web,长驻接 bot 打 70 局。"""
    if host == "0.0.0.0":
        typer.echo("WARNING: binding 0.0.0.0 (all interfaces); arena 无鉴权, 仅限可信网络。", err=True)
    _setup_logging(log_file, log_level)
    n = 1 if once else max_matches
    manager = MatchManager(records_dir=str(records_dir), event_name=event_name,
                           hands_per_match=hands_per_match)
    static_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    try:
        asyncio.run(run_server(
            host=host, tcp_port=tcp_port, web_port=web_port,
            manager=manager, static_dir=static_dir, max_matches=n,
        ))
    except KeyboardInterrupt:
        typer.echo("interrupted", err=True)


# ── connect(protocol exerciser)──────────────────────────────

@app.command()
def connect(
    host: str = typer.Argument(..., help="平台地址"),
    port: int = typer.Argument(..., help="平台 TCP 端口"),
    name: str = typer.Argument(..., help="队名(ASCII)"),
    bare: bool = typer.Option(False, "--bare", help="发裸字节无 \\n(模拟 native bot send);默认发 \\n"),
    policy: str = typer.Option(
        "follow", "--policy",
        help="follow(最小 call/check 跟随) | fold(永远 fold) | allin(响应即 allin)"),
    recv_timeout: float = typer.Option(65.0, "--recv-timeout", help="recv 超时秒"),
    verbose: bool = typer.Option(True, "--verbose/--quiet", help="打印收发字节"),
) -> None:
    """无策略协议自测客户端。

    内置最小 call/check 跟随器(参考 pok1 sever/test_client.py)。recv 按服务端
    ``\\n`` 分帧(server send_line 加 \\n);``--bare`` 仅让本端 send 发裸字节,
    用于压测服务端 token 解析的 no-\\n 路径(no-\\n 的完整端到端验证须用真
    native bot)。只验证协议连通,不下注策略。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        asyncio.run(_connect_client(host, port, name, bare=bare, policy=policy,
                                    recv_timeout=recv_timeout, verbose=verbose))
    except KeyboardInterrupt:
        typer.echo("interrupted", err=True)


async def _connect_client(
    host: str, port: int, name: str, *,
    bare: bool, policy: str, recv_timeout: float, verbose: bool,
) -> None:
    log = logging.getLogger("connect")
    reader, writer = await asyncio.open_connection(host, port)
    suffix = "" if bare else "\n"

    # 本街是否已行动:防止轮次结束后对对手 call/check 再发多余动作 -> 粘包
    # (transport 会把连续动作解析为 protocol_multiple_actions -> 非法 -> fold)。
    state = {"acted": False}
    is_small_blind = False
    in_allin_runout = False
    buf = ""

    def send(msg: str) -> None:
        writer.write((msg + suffix).encode("utf-8"))
        state["acted"] = True
        if verbose:
            log.info("> %r", msg)

    def do_policy(default_follow: str) -> None:
        if policy == "fold":
            send("fold")
        elif policy == "allin":
            send("allin")
        else:
            send(default_follow)

    async def recv_line() -> str | None:
        nonlocal buf
        while "\n" not in buf:
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=recv_timeout)
            except asyncio.TimeoutError:
                return None
            if not data:
                return None
            buf += data.decode("utf-8", "replace")
        line, buf = buf.split("\n", 1)
        return line.rstrip("\r")

    try:
        while True:
            line = await recv_line()
            if line is None:
                log.info("server closed / recv timeout")
                break
            line = line.strip()
            if not line:
                continue
            if verbose:
                log.info("< %r", line)

            if line == "name":
                writer.write((name + suffix).encode("utf-8"))  # name 不算行动
                if verbose:
                    log.info("> name=%r", name)
                continue
            if line.startswith("preflop|"):
                parts = line.split("|")
                is_small_blind = (len(parts) > 1 and parts[1] == "SMALLBLIND")
                in_allin_runout = False
                state["acted"] = False
                if is_small_blind:            # SB 翻前先动 -> limp(call)
                    do_policy("call")
                continue
            if line.startswith(("flop|", "turn|", "river|")):
                state["acted"] = False
                # postflop BB 先手 -> check;SB 在对手动作后再动
                if not in_allin_runout and not is_small_blind:
                    do_policy("check")
                continue
            if line.startswith("earnChips"):
                in_allin_runout = False
                continue
            if line.startswith("oppo_hands|"):
                continue
            # 对手动作转发:仅本街未行动时响应(已行动说明轮次将/已结束,避免粘包)
            if line == "allin":
                in_allin_runout = True
                if not state["acted"]:
                    do_policy("call")
                continue
            if line.startswith("raise"):
                if not state["acted"]:
                    do_policy("call")
                continue
            if line == "check":
                if not state["acted"]:        # 对手 check -> 本方 call(过牌)
                    do_policy("call")
                continue
            if line == "call":
                if not state["acted"]:        # 对手 call(SB limp) -> BB check
                    do_policy("check")
                continue
            if line == "fold":
                continue
            log.warning("unhandled message: %r", line)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# ── thp ──────────────────────────────────────────────────────

@thp_app.command("list")
def thp_list(
    records_dir: Path = typer.Option(DEFAULT_RECORDS_DIR, "--records-dir"),
    as_json: bool = typer.Option(False, "--json", help="原样 JSON"),
) -> None:
    """列出已记录的比赛。"""
    manager = MatchManager(records_dir=str(records_dir))
    matches = manager.list_matches()
    if as_json:
        typer.echo(json.dumps(matches, ensure_ascii=False, indent=2))
        return
    if not matches:
        typer.echo("(无比赛记录)")
        return
    for m in matches:
        typer.echo(f"{m.get('match_id')}  {m.get('names')}  "
                   f"earnings={m.get('total_earnings')}  reason={m.get('reason')}")


@thp_app.command("show")
def thp_show(
    match_id: str = typer.Argument(..., help="match-id(thp list 查看)"),
    records_dir: Path = typer.Option(DEFAULT_RECORDS_DIR, "--records-dir"),
    out: Path | None = typer.Option(None, "--out", help="写到文件(默认 stdout,gb2312)"),
) -> None:
    """导出某场比赛 THP 棋谱(gb2312 文本)。"""
    manager = MatchManager(records_dir=str(records_dir))
    text = manager.read_thp(match_id)
    if text is None:
        typer.echo(f"match not found: {match_id}", err=True)
        raise typer.Exit(code=1)
    if out:
        out.write_text(text, encoding="gb2312", errors="replace")
        typer.echo(f"wrote {out} ({len(text)} chars)", err=True)
    else:
        sys.stdout.buffer.write(text.encode("gb2312", errors="replace"))
        sys.stdout.buffer.write(b"\n")


# ── status ───────────────────────────────────────────────────

@app.command()
def status(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(WEB_DEFAULT_PORT, "--port", help="Web 端口(默认 50180)"),
    json_out: bool = typer.Option(False, "--json", help="原样输出 JSON"),
    timeout: float = typer.Option(5.0, "--timeout"),
) -> None:
    """查询运行中的平台状态(HTTP GET /api/state)。"""
    url = f"http://{host}:{port}/api/state"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"查询失败: {exc}", err=True)
        raise typer.Exit(code=1)
    if json_out:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2))
        return
    typer.echo(f"status:         {data.get('status')}")
    typer.echo(f"match_id:       {data.get('match_id')}")
    typer.echo(f"names:          {data.get('names')}")
    typer.echo(f"hand:           {data.get('hand_num')}/{data.get('hands_per_match')}")
    typer.echo(f"total_earnings: {data.get('total_earnings')}")
    typer.echo(f"matches_played: {data.get('matches_played')}")
    if data.get("server_addr"):
        typer.echo(f"server_addr:    {data.get('server_addr')}")


if __name__ == "__main__":
    app()
