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
import os
import socket
import sys
import time
import urllib.request
from pathlib import Path

import typer

from .auth import AuthManager
from .main import WEB_DEFAULT_PORT, run_server
from .server.match_manager import MatchManager
from .server.tcp_server import DEFAULT_HOST, DEFAULT_PORT
from .store import DEFAULT_DB_PATH, Store

app = typer.Typer(
    help="pok-arena: 国赛德州扑克对弈平台 web 复刻",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
thp_app = typer.Typer(help="THP 棋谱导出/列表", no_args_is_help=True)
app.add_typer(thp_app, name="thp")

DEFAULT_RECORDS_DIR = Path("records")  # 默认当前目录 ./records


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
    db_path: str = typer.Option("arena.db", "--db-path", help="SQLite 库路径(默认当前目录 arena.db;空串禁用 DB)"),
    no_logs: bool = typer.Option(False, "--no-logs", help="禁用每场日志(默认写 logs/<match_id>/)"),
    log_file: str | None = typer.Option(None, "--log-file", help="日志文件(默认仅 stderr)"),
    log_level: str = typer.Option("INFO", "--log-level", help="DEBUG/INFO/WARNING/ERROR"),
) -> None:
    """启动 TCP 平台 + Web,长驻接 bot 打 70 局。"""
    if host == "0.0.0.0":
        typer.echo("WARNING: binding 0.0.0.0 (all interfaces); arena 无鉴权, 仅限可信网络。", err=True)
    _setup_logging(log_file, log_level)
    n = 1 if once else max_matches
    manager = MatchManager(records_dir=str(records_dir), event_name=event_name,
                           hands_per_match=hands_per_match,
                           db_path=db_path or None, write_logs=not no_logs)
    static_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    try:
        asyncio.run(run_server(
            host=host, tcp_port=tcp_port, web_port=web_port,
            manager=manager, static_dir=static_dir, max_matches=n,
        ))
    except KeyboardInterrupt:
        typer.echo("interrupted", err=True)


# ── serve-web(新平台,里程碑 8a)─────────────────────────────

@app.command(name="serve-web")
def serve_web(
    host: str = typer.Option("127.0.0.1", "--host", help="绑定地址(默认 127.0.0.1)"),
    web_port: int = typer.Option(50280, "--web-port", help="新平台 Web 端口(默认 50280,与旧 50180 区分)"),
    db_path: str = typer.Option("arena_platform.db", "--db-path", help="新平台 SQLite 库"),
    upload_root: Path = typer.Option(Path("bot_uploads"), "--upload-root", help="bot 上传根目录"),
    no_builtin: bool = typer.Option(False, "--no-builtin", help="不自动注册内置 bot 库"),
    log_file: str | None = typer.Option(None, "--log-file"),
    log_level: str = typer.Option("INFO", "--log-level"),
) -> None:
    """启动新平台 web(botzone 风格:账号/bot上传/对战/排行榜/回放)。

    与 serve(TCP 通道)隔离:独立端口 50280、独立 db arena_platform.db、
    用户上传 bot 在 Docker 沙箱跑。
    """
    from .platform.main import WEB_DEFAULT_PORT, run_platform_server
    if host == "0.0.0.0":
        typer.echo("WARNING: binding 0.0.0.0 (新平台);务必配置好认证与防火墙。", err=True)
    _setup_logging(log_file, log_level)
    static_dir = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    try:
        asyncio.run(run_platform_server(
            host=host, web_port=web_port, db_path=db_path,
            static_dir=static_dir, upload_root=str(upload_root),
            register_builtin=not no_builtin,
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


# ── 扩展命令:天梯/用户/历史/对局/注册/管理员/清理 ──────────

@app.command()
def leaderboard(
    top: int = typer.Option(20, "--top", help="前 N 名"),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), "--db-path"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """天梯榜(Glicko-2 rating 排序 + 战绩 + 净筹码)。"""
    rows = Store(db_path).leaderboard(top)
    if json_out:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2)); return
    if not rows:
        typer.echo("(无评分记录,先 serve 跑几场)"); return
    typer.echo(f"{'排名':<4} {'bot':<18} {'rating':>8} {'RD':>6} {'W-L-D':>8} {'净筹码':>10}")
    for i, r in enumerate(rows, 1):
        typer.echo(f"{i:<4} {r['name']:<18} {r['rating']:>8.1f} {r['rd']:>6.1f} "
                   f"{r['wins']}-{r['losses']}-{r['draws']:>3} {r['net_chips']:>10}")


@app.command()
def user(
    name: str = typer.Argument(..., help="bot 名"),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), "--db-path"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """用户战绩(rating + W/L/D + 对各对手 bb/100 + 最近对局)。"""
    s = Store(db_path)
    u = s.get_user(name)
    if u is None:
        typer.echo(f"用户不存在: {name}", err=True); raise typer.Exit(code=1)
    r = s.get_rating(name)
    ps = s.pair_stats_for(name)
    recent = s.list_matches(user=name, limit=10)
    if json_out:
        typer.echo(json.dumps({"user": u, "rating": r, "pair_stats": ps, "recent": recent},
                              ensure_ascii=False, indent=2)); return
    typer.echo(f"=== {u['display_name']} ({name}) ===")
    if u.get("team"):
        typer.echo(f"队伍: {u['team']}")
    if r:
        typer.echo(f"rating: {r['rating']:.1f} (RD {r['rd']:.1f})  战绩 {r['wins']}-{r['losses']}-{r['draws']}  "
                   f"净筹码 {r['net_chips']}  对局 {r['matches_played']}")
    for p in ps:
        opp = p['name_b'] if p['name_a'] == name else p['name_a']
        typer.echo(f"  vs {opp}: bb/100 {p['bb_per_100_mean']:.2f} "
                   f"CI=[{p['ci_low']:.2f},{p['ci_high']:.2f}] n={p['samples']}")
    typer.echo(f"最近 {len(recent)} 场:")
    for m in recent:
        opp = m['name_b'] if m['name_a'] == name else m['name_a']
        me = m['earnings_a'] if m['name_a'] == name else m['earnings_b']
        typer.echo(f"  {m['match_id']} vs {opp} earnings={me} {m['reason']}")


@app.command(name="history")
def history_cmd(
    user_name: str = typer.Option(None, "--user", help="按 bot 筛选"),
    limit: int = typer.Option(20, "--limit"),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), "--db-path"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """历史对局(可按 bot 筛选)。"""
    rows = Store(db_path).list_matches(user=user_name, limit=limit)
    if json_out:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2)); return
    if not rows:
        typer.echo("(无对局记录)"); return
    for m in rows:
        typer.echo(f"{m['match_id'][:32]}  {m['name_a']} vs {m['name_b']}  "
                   f"{m['earnings_a']}/{m['earnings_b']}  {m['hands_played']}手  {m['reason']}")


@app.command(name="match")
def match_cmd(
    match_id: str = typer.Argument(...),
    records_dir: Path = typer.Option(DEFAULT_RECORDS_DIR, "--records-dir"),
) -> None:
    """查看一场对局的 THP 棋谱(等同 thp show)。"""
    text = MatchManager(records_dir=str(records_dir)).read_thp(match_id)
    if text is None:
        typer.echo(f"对局不存在: {match_id}", err=True); raise typer.Exit(code=1)
    sys.stdout.buffer.write(text.encode("gb2312", errors="replace"))


@app.command()
def register(
    name: str = typer.Argument(..., help="bot 名"),
    display: str = typer.Option(None, "--display", help="显示名(默认=name)"),
    team: str = typer.Option("", "--team"),
    note: str = typer.Option("", "--note"),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), "--db-path"),
) -> None:
    """预注册 bot 用户(管理员手动注册,含元数据)。"""
    s = Store(db_path)
    created = s.ensure_user(name, display_name=display, team=team, note=note)
    if display or team or note:
        s.update_user(name, display_name=display or name, team=team, note=note)
    typer.echo(f"{'新建' if created else '已存在'} bot 用户: {name}")
    typer.echo(json.dumps(s.get_user(name), ensure_ascii=False, indent=2))


admin_app = typer.Typer(help="管理员", no_args_is_help=True)
app.add_typer(admin_app, name="admin")


@admin_app.command("set-password")
def admin_set_password(
    username: str = typer.Option("admin", "--username"),
    password: str = typer.Option(os.environ.get("POK_ARENA_ADMIN_PASSWORD", ""),
                                 "--password", help="管理员密码(或 env POK_ARENA_ADMIN_PASSWORD)"),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), "--db-path"),
) -> None:
    """设置管理员密码(用于 web /admin 登录)。"""
    if not password:
        typer.echo("密码必填(--password 或 env POK_ARENA_ADMIN_PASSWORD)", err=True)
        raise typer.Exit(code=1)
    AuthManager(Store(db_path)).set_password(username, password)
    typer.echo(f"管理员 {username} 密码已设置")


@app.command()
def clean(
    keep: int = typer.Option(1000, "--keep", help="保留最近 N 场 logs/records"),
    records_dir: Path = typer.Option(DEFAULT_RECORDS_DIR, "--records-dir"),
) -> None:
    """清理旧 logs/ 与 records/(保留最近 N 场,按 mtime)。"""
    import shutil
    cleaned = 0
    for d in [Path("logs"), Path(records_dir)]:
        if not d.exists():
            continue
        items = sorted(
            [p for p in d.iterdir() if p.is_dir() or p.suffix == ".thp"],
            key=lambda p: p.stat().st_mtime, reverse=True)
        for old in items[keep:]:
            try:
                if old.is_dir():
                    shutil.rmtree(old)
                else:
                    old.unlink()
                cleaned += 1
            except OSError:
                pass
    typer.echo(f"清理 {cleaned} 个旧项(保留最近 {keep})")


if __name__ == "__main__":
    app()
