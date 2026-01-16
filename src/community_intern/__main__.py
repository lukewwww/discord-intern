from __future__ import annotations

import argparse
import asyncio
import logging

from community_intern.adapters.discord import DiscordBotAdapter
from community_intern.ai import AIClientImpl
from community_intern.config import YamlConfigLoader
from community_intern.config.models import ConfigLoadRequest
from community_intern.kb.impl import FileSystemKnowledgeBase
from community_intern.logging import init_logging

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="community-intern", description="Community Intern bot runner")
    parser.add_argument(
        "--config",
        default="data/config/config.yaml",
        help="Path to config.yaml (default: data/config/config.yaml)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True, help="Command to run")

    # Command: run
    run_parser = subparsers.add_parser("run", help="Start the Discord bot")
    run_parser.add_argument(
        "--run-seconds",
        type=float,
        default=None,
        help="Run the bot for N seconds then exit (useful for smoke testing).",
    )

    # Command: init_kb
    subparsers.add_parser("init_kb", help="Initialize Knowledge Base index")

    return parser


async def _stop_adapter_gracefully(adapter: DiscordBotAdapter, *, timeout_seconds: float = 15.0) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(adapter.stop()), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning("Shutdown timed out while stopping the Discord adapter. timeout_seconds=%s", timeout_seconds)
    except Exception:
        logger.exception("Unexpected error during shutdown.")


def _log_index_task_result(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Knowledge base indexing failed.")


async def _load_config(args: argparse.Namespace):
    loader = YamlConfigLoader()
    request = ConfigLoadRequest(
        yaml_path=args.config,
    )
    return await loader.load(request)


async def _run_bot(args: argparse.Namespace) -> None:
    config = await _load_config(args)
    init_logging(config.logging)
    logger.info("Starting application in bot mode. dry_run=%s", config.app.dry_run)

    # Initialize AI and KnowledgeBase with circular dependency injection
    ai_client = AIClientImpl(config=config.ai)
    kb = FileSystemKnowledgeBase(config=config.kb, ai_client=ai_client)
    ai_client.set_kb(kb)

    index_task = asyncio.create_task(kb.build_index())
    index_task.add_done_callback(_log_index_task_result)
    kb.start_runtime_refresh()

    adapter = DiscordBotAdapter(config=config, ai_client=ai_client)
    try:
        if args.run_seconds is not None:
            await adapter.run_for(seconds=args.run_seconds)
        else:
            await adapter.start()
    finally:
        await _stop_adapter_gracefully(adapter)
        await kb.stop_runtime_refresh()
        if index_task and not index_task.done():
            index_task.cancel()
            try:
                await index_task
            except asyncio.CancelledError:
                logger.info("Knowledge base indexing task cancelled during shutdown.")


async def _init_kb(args: argparse.Namespace) -> None:
    config = await _load_config(args)
    init_logging(config.logging)
    logger.info("Starting knowledge base indexing.")

    ai_client = AIClientImpl(config=config.ai)
    kb = FileSystemKnowledgeBase(config=config.kb, ai_client=ai_client)
    # Note: ai_client.set_kb(kb) is not strictly needed for indexing,
    # but good for consistency if AIClient needs to read KB during init in future.
    ai_client.set_kb(kb)

    await kb.build_index()
    logger.info("Knowledge base indexing completed.")


async def _main_async() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "run":
        await _run_bot(args)
    elif args.command == "init_kb":
        await _init_kb(args)


def main() -> None:
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")


if __name__ == "__main__":
    main()
