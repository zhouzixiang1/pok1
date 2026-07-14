"""PokerSkill Agent CLI: play against GTO Wizard."""

import argparse
import asyncio
import logging
import os
import signal
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="pokerskill-agent",
        description="PokerSkill Agent: play against GTO Wizard Researcher API.",
    )

    parser.add_argument("--version", "-v", action="store_true", help="Show version and exit")

    # --- play subcommand ---
    subparsers = parser.add_subparsers(dest="command")
    play_parser = subparsers.add_parser(
        "play",
        help="Play against GTO Wizard Researcher API.",
    )
    play_parser.add_argument("--num-hands", "-n", type=int, default=100, help="Number of hands to play (default: 100)")
    play_parser.add_argument("--model", "-m", type=str, default="claude-opus-4-6", help="LLM model name")
    play_parser.add_argument("--backend", "-b", type=str, default="", help="LLM backend: claude/openai (auto-detected from model)")
    play_parser.add_argument("--concurrent", type=int, default=3, help="Concurrent hands (default: 3)")
    play_parser.add_argument("--thinking-budget", type=int, default=0, help="Extended thinking budget (Claude/OpenAI reasoning)")
    play_parser.add_argument("--no-skills", action="store_true", help="Disable PokerSkill strategy layers (baseline mode)")
    play_parser.add_argument("--output", "-o", type=str, default="", help="CSV output path (default: results_<model>.csv)")
    play_parser.add_argument("--base-url", type=str, default="", help="LLM API base URL override")
    play_parser.add_argument("--temperature", type=float, default=0.3, help="LLM temperature (default: 0.3)")
    play_parser.add_argument("--max-tokens", type=int, default=1024, help="Max tokens (default: 1024)")
    play_parser.add_argument("--max-retries", type=int, default=20, help="Max LLM retries on connection failure (default: 20)")

    guosai_parser = subparsers.add_parser(
        "play-guosai",
        help="Play on guosai socket platform.",
    )
    guosai_parser.add_argument("--host", type=str, default="127.0.0.1", help="Platform host (default: 127.0.0.1)")
    guosai_parser.add_argument("--port", type=int, default=10001, help="Platform port (default: 10001)")
    guosai_parser.add_argument("--name", type=str, default="PokerSkillAgent", help="Team english name for `name` handshake")
    guosai_parser.add_argument("--num-hands", "-n", type=int, default=70, help="Hands to play (default: 70)")
    guosai_parser.add_argument("--model", "-m", type=str, default="claude-opus-4-6", help="LLM model name")
    guosai_parser.add_argument("--backend", "-b", type=str, default="", help="LLM backend: claude/openai (auto-detected from model)")
    guosai_parser.add_argument("--thinking-budget", type=int, default=0, help="Extended thinking budget")
    guosai_parser.add_argument("--no-skills", action="store_true", help="Disable PokerSkill strategy layers")
    guosai_parser.add_argument("--base-url", type=str, default="", help="LLM API base URL override")
    guosai_parser.add_argument("--temperature", type=float, default=0.3, help="LLM temperature (default: 0.3)")
    guosai_parser.add_argument("--max-tokens", type=int, default=1024, help="Max tokens (default: 1024)")
    guosai_parser.add_argument("--step-timeout", type=float, default=50.0, help="Decision timeout seconds (default: 50)")
    guosai_parser.add_argument("--socket-timeout", type=float, default=120.0, help="Socket read timeout seconds (default: 120)")
    guosai_parser.add_argument("--max-retries", type=int, default=20, help="Max LLM retries on connection failure (default: 20)")

    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"pokerskill-agent {__version__}")
        sys.exit(0)

    if args.command == "play":
        _run_play(args)
    elif args.command == "play-guosai":
        _run_play_guosai(args)
    else:
        parser.print_help()
        sys.exit(1)


def _run_play(args):
    """Handle the play subcommand."""
    from ._battle import BattleRunner, BattleConfig

    gto_api_key = os.environ.get("GTO_WIZARD_API_KEY", "")
    if not gto_api_key:
        print("Error: GTO_WIZARD_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)

    backend = args.backend or _infer_backend(args.model)
    output = args.output or f"results_{args.model.replace('/', '_')}.csv"

    os.environ["POKERSKILL_MAX_RETRIES"] = str(args.max_retries)

    config = BattleConfig(
        gto_api_key=gto_api_key,
        game_name="HUNL 200BB",
        num_hands=args.num_hands,
        num_concurrent=args.concurrent,
        model=args.model,
        backend=backend,
        llm_base_url=args.base_url,
        llm_api_key="",
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        thinking_budget=args.thinking_budget,
        use_skills=not args.no_skills,
        output_csv=output,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(_play_async(config))


async def _play_async(config):
    """Async entry point for battle."""
    from ._battle import BattleRunner

    async with BattleRunner(config) as runner:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, runner.request_stop)

        result = await runner.run()

    print(f"\n{'='*50}")
    print(result.summary())
    print(f"{'='*50}")


def _infer_backend(model: str) -> str:
    """Infer LLM backend from model name."""
    model_lower = model.lower()
    if any(p in model_lower for p in ("claude", "opus", "sonnet", "haiku")):
        return "claude"
    return "openai"


def _run_play_guosai(args):
    """Handle the play-guosai subcommand."""
    from .guosai import GuosaiConfig, GuosaiRunner

    backend = args.backend or _infer_backend(args.model)
    os.environ["POKERSKILL_MAX_RETRIES"] = str(args.max_retries)

    config = GuosaiConfig(
        host=args.host,
        port=args.port,
        team_name=args.name,
        num_hands=args.num_hands,
        model=args.model,
        backend=backend,
        llm_base_url=args.base_url,
        llm_api_key="",
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        thinking_budget=args.thinking_budget,
        use_skills=not args.no_skills,
        step_timeout_s=args.step_timeout,
        socket_timeout_s=args.socket_timeout,
        max_retries=args.max_retries,
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    result = asyncio.run(GuosaiRunner(config).run())
    print(f"\n{'='*50}")
    print(result.summary())
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
