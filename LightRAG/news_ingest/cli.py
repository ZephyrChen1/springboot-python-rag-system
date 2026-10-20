"""命令行入口：python -m news_ingest ..."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from typing import Sequence

from .config import NewsConfig, load_config
from .fetch import Fetcher
from .kb_client import KBClient
from .lock import LockBusy, RunLock
from .pipeline import NewsPipeline, RunOptions, check_sources
from .sources import SourceConfigError
from .state import StateStore

logger = logging.getLogger("news_ingest")


def _split_sources(values: Sequence[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or ():
        for part in value.split(","):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return tuple(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m news_ingest",
        description="从官方 RSS/sitemap 抓取 AI 前沿发布，筛选后通过 FastAPI 入库。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="运行一轮后退出（默认）")
    mode.add_argument("--loop", action="store_true", help="常驻运行，按 --interval-minutes 循环")
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=None,
        help="loop 模式间隔分钟数（默认读取 NEWS_INTERVAL_MINUTES=360）",
    )
    parser.add_argument("--dry-run", action="store_true", help="预览，不写 DB、不上传")
    parser.add_argument(
        "--dry-run-no-llm",
        action="store_true",
        help="只抓取/清洗/规则预筛，不调用 LLM（隐含 --dry-run）",
    )
    parser.add_argument(
        "--source",
        action="append",
        metavar="ID",
        help="只处理指定源；可重复或逗号分隔（可用 disabled 源做测试）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本轮最多处理多少篇候选；0=不限（默认取 NEWS_MAX_ARTICLES_PER_RUN，0=不限）",
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=None,
        help="日期窗口（抓取/预筛/过期清理共用）；0 表示关闭日期过滤和过期清理",
    )
    parser.add_argument("--force", action="store_true", help="重置已终态文章，重新判断/入库")
    parser.add_argument(
        "--check-sources",
        action="store_true",
        help="只检查源可达性和最新条目，不写 DB/不调 LLM/不入库",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING/ERROR")
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def _print_json(payload: dict | list) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    print(text)


def _run_once(cfg: NewsConfig, opts: RunOptions, *, store: StateStore, fetcher: Fetcher, kb: KBClient) -> int:
    pipeline = NewsPipeline(cfg, store, fetcher, kb)
    summary = pipeline.run(opts)
    _print_json(summary.to_dict())
    return summary.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run_no_llm:
        args.dry_run = True
    if args.interval_minutes is not None and args.interval_minutes <= 0:
        parser.error("--interval-minutes must be positive")
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0 (0 = unlimited)")
    if args.since_days is not None and args.since_days < 0:
        parser.error("--since-days must be >= 0 (0 disables date filtering and expiry)")

    try:
        cfg = load_config()
    except Exception as exc:
        print(f"加载配置失败: {exc}", file=sys.stderr)
        return 1
    _configure_logging(args.log_level or cfg.log_level)
    source_ids = _split_sources(args.source)

    if args.check_sources:
        try:
            results = check_sources(cfg, fetcher=Fetcher(cfg))
        except SourceConfigError as exc:
            print(f"源配置错误: {exc}", file=sys.stderr)
            return 1
        _print_json(results)
        return 0 if all(item.get("ok") for item in results) else 2

    opts = RunOptions(
        dry_run=bool(args.dry_run),
        dry_run_no_llm=bool(args.dry_run_no_llm),
        force=bool(args.force),
        source_ids=source_ids,
        limit=args.limit,
        since_days=args.since_days,
    )
    interval = args.interval_minutes or cfg.interval_minutes

    lock: RunLock | None = None
    store: StateStore | None = None
    try:
        if not args.dry_run:
            lock = RunLock(cfg.lock_file)
            try:
                lock.acquire()
            except LockBusy as exc:
                print(f"已有 news_ingest 实例在运行: {exc}", file=sys.stderr)
                return 1
        store = StateStore(cfg.state_db, dry_run=bool(args.dry_run))
        fetcher = Fetcher(cfg)
        kb = KBClient(cfg)

        if not args.loop:
            return _run_once(cfg, opts, store=store, fetcher=fetcher, kb=kb)

        while True:
            try:
                code = _run_once(cfg, opts, store=store, fetcher=fetcher, kb=kb)
            except KeyboardInterrupt:
                logger.info("收到中断，退出 loop")
                return 0
            if code == 1:
                logger.error("本轮致命错误，停止 loop")
                return 1
            logger.info("下一轮将在 %s 分钟后开始", interval)
            time.sleep(max(1, interval * 60))
    except KeyboardInterrupt:
        return 0
    except SourceConfigError as exc:
        print(f"源配置错误: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        logger.exception("news_ingest 异常退出")
        print(f"异常退出: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()
        if lock is not None:
            lock.release()
