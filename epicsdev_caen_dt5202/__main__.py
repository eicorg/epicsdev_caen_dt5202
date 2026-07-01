#!/usr/bin/env python3
"""PVAccess server for CAEN DT5202 run-list files.

This server:
- watches an input directory,
- parses new files with CAEN list format (example: data/RunXXX_list.txt),
- calls a user hook for custom processing,
- updates PVs,
- moves processed files to an output directory.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Required by request: server is based on epicsdev module.
# epicsdev may provide environment/bootstrap pieces in deployments.
import epicsdev

try:
    # Practical PVAccess backend.
    from p4p.nt import NTScalar
    from p4p.server import Server, StaticProvider
    from p4p.server.thread import SharedPV
except Exception as exc:  # pragma: no cover - startup dependency check
    raise RuntimeError(
        "p4p is required to run the PVAccess server. Install p4p in this environment."
    ) from exc


LOG = logging.getLogger("dt5202-pva")
EPICSDEV_VERSION = getattr(epicsdev, "__version__", "unknown")

DEFAULT_NUMBER_OF_BOARDS = 1
MAX_NUMBER_OF_BOARDS = 16
DEFAULT_MAX_CHANNELS_PER_BOARD = 112
MAX_MAX_CHANNELS_PER_BOARD = 64


@dataclass(slots=True)
class RunRow:
    board: int
    channel: int
    lg: float
    hg: float
    timestamp_us: Optional[float] = None
    trigger_id: Optional[int] = None
    nchs: Optional[int] = None


@dataclass(slots=True)
class ParsedRunFile:
    path: Path
    header: dict[str, str]
    rows: list[RunRow]

    @property
    def run_start_time(self) -> str:
        return self.header.get("Run start time", "")


class Dt5202PVServer:
    def __init__(self, max_channels_per_board: int):
        self.provider = StaticProvider("dt5202")

        self.run_start_time_pv = SharedPV(
            nt=NTScalar("s"),
            initial={"value": ""},
        )
        self.b0_channels_pv = SharedPV(
            nt=NTScalar("ad"),
            initial={"value": [0.0] * max_channels_per_board},
        )

        self.provider.add("runStartTime", self.run_start_time_pv)
        self.provider.add("b0Channels", self.b0_channels_pv)

        self._server = Server(providers=[self.provider])

    def update_run_start_time(self, value: str) -> None:
        self.run_start_time_pv.post({"value": value})

    def update_b0_channels(self, values: list[float]) -> None:
        self.b0_channels_pv.post({"value": values})

    def close(self) -> None:
        self._server.stop()


def parse_run_file(path: Path) -> ParsedRunFile:
    header: dict[str, str] = {}
    rows: list[RunRow] = []

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith("//"):
                payload = line[2:].strip()
                if ":" in payload:
                    key, value = payload.split(":", 1)
                    header[key.strip()] = value.strip()
                continue

            if line.startswith("Brd"):
                continue

            cols = line.split()
            if len(cols) < 4:
                continue

            board = int(cols[0])
            channel = int(cols[1])
            lg = float(cols[2])
            hg = float(cols[3])

            timestamp_us = float(cols[4]) if len(cols) > 4 else None
            trigger_id = int(cols[5]) if len(cols) > 5 else None
            nchs = int(cols[6]) if len(cols) > 6 else None

            rows.append(
                RunRow(
                    board=board,
                    channel=channel,
                    lg=lg,
                    hg=hg,
                    timestamp_us=timestamp_us,
                    trigger_id=trigger_id,
                    nchs=nchs,
                )
            )

    return ParsedRunFile(path=path, header=header, rows=rows)


def default_user_processor(parsed: ParsedRunFile) -> None:
    """Default hook for user-defined processing."""
    LOG.info("Processed %s rows from %s", len(parsed.rows), parsed.path.name)


def load_user_processor(spec: str) -> Callable[[ParsedRunFile], None]:
    """Load callback from MODULE:FUNCTION spec."""
    if ":" not in spec:
        raise ValueError("processor must be in MODULE:FUNCTION format")

    mod_name, fn_name = spec.split(":", 1)
    module = importlib.import_module(mod_name)
    fn = getattr(module, fn_name)
    if not callable(fn):
        raise TypeError(f"{spec} is not callable")
    return fn


def compute_board0_channels(parsed: ParsedRunFile, max_channels_per_board: int) -> list[float]:
    values = [0.0] * max_channels_per_board
    for row in parsed.rows:
        if row.board != 0:
            continue
        if 0 <= row.channel < max_channels_per_board:
            values[row.channel] = float(row.lg)
    return values


def process_one_file(
    file_path: Path,
    out_dir: Path,
    pva: Dt5202PVServer,
    max_channels_per_board: int,
    user_processor: Callable[[ParsedRunFile], None],
) -> None:
    parsed = parse_run_file(file_path)

    user_processor(parsed)

    pva.update_run_start_time(parsed.run_start_time)
    pva.update_b0_channels(compute_board0_channels(parsed, max_channels_per_board))

    destination = out_dir / file_path.name
    if destination.exists():
        timestamp = int(time.time())
        destination = out_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"

    shutil.move(str(file_path), str(destination))
    LOG.info("Moved processed file to %s", destination)


def watch_loop(
    in_dir: Path,
    out_dir: Path,
    pva: Dt5202PVServer,
    max_channels_per_board: int,
    user_processor: Callable[[ParsedRunFile], None],
    should_stop: Callable[[], bool],
    poll_seconds: float = 1.0,
) -> None:
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    LOG.info("Watching input directory: %s", in_dir)
    LOG.info("Output directory: %s", out_dir)

    # size cache to avoid processing files that are still being written
    last_seen_size: dict[Path, int] = {}

    while not should_stop():
        for file_path in sorted(p for p in in_dir.iterdir() if p.is_file()):
            try:
                current_size = file_path.stat().st_size
            except FileNotFoundError:
                continue

            previous_size = last_seen_size.get(file_path)
            if previous_size is None or previous_size != current_size:
                last_seen_size[file_path] = current_size
                continue

            # stable size across 2 scans => process
            last_seen_size.pop(file_path, None)

            try:
                process_one_file(
                    file_path=file_path,
                    out_dir=out_dir,
                    pva=pva,
                    max_channels_per_board=max_channels_per_board,
                    user_processor=user_processor,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.exception("Failed to process %s: %s", file_path, exc)

        time.sleep(poll_seconds)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    raw_argv = argv if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(description="CAEN DT5202 PVAccess server")
    parser.add_argument("--inDir", required=True, help="Directory to watch for new files")
    parser.add_argument("--outDir", required=True, help="Directory to move processed files")
    parser.add_argument(
        "--numberOfBoards",
        type=int,
        default=DEFAULT_NUMBER_OF_BOARDS,
        help=f"Number of boards (default {DEFAULT_NUMBER_OF_BOARDS}, max {MAX_NUMBER_OF_BOARDS})",
    )
    parser.add_argument(
        "--maxChannelsPerBoard",
        type=int,
        default=DEFAULT_MAX_CHANNELS_PER_BOARD,
        help=(
            "Max channels per board "
            f"(default {DEFAULT_MAX_CHANNELS_PER_BOARD}, requested max {MAX_MAX_CHANNELS_PER_BOARD})"
        ),
    )
    parser.add_argument(
        "--processor",
        default="",
        help="Optional callback in MODULE:FUNCTION format",
    )
    parser.add_argument(
        "--pollSeconds",
        type=float,
        default=1.0,
        help="Polling interval for input directory",
    )

    args = parser.parse_args(raw_argv)

    if not 1 <= args.numberOfBoards <= MAX_NUMBER_OF_BOARDS:
        parser.error(f"--numberOfBoards must be in [1, {MAX_NUMBER_OF_BOARDS}]")

    # Request has contradictory constraints: default=112 and max=64.
    # Keep default as requested; enforce max=64 for explicit user values.
    if "--maxChannelsPerBoard" in raw_argv and args.maxChannelsPerBoard > MAX_MAX_CHANNELS_PER_BOARD:
        parser.error(f"--maxChannelsPerBoard must be <= {MAX_MAX_CHANNELS_PER_BOARD}")

    if args.maxChannelsPerBoard < 1:
        parser.error("--maxChannelsPerBoard must be >= 1")

    if args.pollSeconds <= 0:
        parser.error("--pollSeconds must be > 0")

    return args


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOG.info("Using epicsdev version: %s", EPICSDEV_VERSION)

    args = parse_args(argv)

    in_dir = Path(args.inDir).resolve()
    out_dir = Path(args.outDir).resolve()

    user_processor = default_user_processor
    if args.processor:
        user_processor = load_user_processor(args.processor)

    pva = Dt5202PVServer(max_channels_per_board=args.maxChannelsPerBoard)

    stop = False

    def _handle_stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    try:
        watch_loop(
            in_dir=in_dir,
            out_dir=out_dir,
            pva=pva,
            max_channels_per_board=args.maxChannelsPerBoard,
            user_processor=user_processor,
            should_stop=lambda: stop,
            poll_seconds=args.pollSeconds,
        )
    finally:
        pva.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
