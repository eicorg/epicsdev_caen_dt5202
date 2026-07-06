#!/usr/bin/env python3
"""PVAccess server for CAEN DT5202 run-list files.

This server:
- watches an input directory,
- parses new files with CAEN list format (example: data/RunXXX_list.txt),
- calls a user hook for custom processing,
- updates PVs,
- moves processed files to an output directory.
"""
# pylint: disable=invalid-name
from __future__ import annotations
__version__ = 'v0.0.3 2026-07-06'# Parsing and posting of channels is working.

import argparse
import importlib
import logging
import queue
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Optional
from datetime import datetime

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

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except Exception as exc:  # pragma: no cover - startup dependency check
    raise RuntimeError(
        "watchdog is required to detect new files. Install watchdog in this environment."
    ) from exc


LOG = logging.getLogger("dt5202-pva")
EPICSDEV_VERSION = getattr(epicsdev, "__version__", "unknown")

#````````````````````````````Necessary explicit globals```````````````````````
pargs = None# program arguments

def _printTime():
    return datetime.now().strftime("%m%d:%H%M%S,%f")#[:-3]
def verb_is(level:int) -> bool:
    return level <= pargs.verbose
def printv(level:int, msg: str):
    print(f'DBG{level}@{_printTime()}: {msg}')

DEFAULT_NUMBER_OF_BOARDS = 1
MAX_NUMBER_OF_BOARDS = 16
DEFAULT_MAX_CHANNELS_PER_BOARD = 12
MAX_MAX_CHANNELS_PER_BOARD = 64


@dataclass(slots=True)
class TriggedChannels:
    board: int
    channels: list[int]
    lg: list[float]
    hg: list[float]
    timestamp_us: Optional[float] = None
    trigger_id: Optional[int] = None

@dataclass(slots=True)
class ParsedRunFile:
    path: Path
    header: dict[str, str]
    triggedChannels: list[TriggedChannels]

    @property
    def run_start_time(self) -> str:
        return self.header.get("Run start time", "")


class Dt5202PVServer:
    def __init__(self, max_channels_per_board: int):
        self.provider = StaticProvider("dt5202")

        self.run_start_time_pv = SharedPV(
            nt=NTScalar("d"),
            initial={"value": 0.0},
        )
        self.b0_channels_pv = SharedPV(
            nt=NTScalar("ad"),
            initial={"value": [0.0] * max_channels_per_board},
        )

        self.provider.add("runStartTime", self.run_start_time_pv)
        self.provider.add("b0Channels", self.b0_channels_pv)

        self._server = Server(providers=[self.provider])

    def update_run_start_time(self, value: str) -> None:
        # Parse string to datetime object
        dt_obj = datetime.strptime(value, '%a %b %d %H:%M:%S %Y %Z')
        # Convert to timestamp
        self.runTimestamp = dt_obj.timestamp()
        #print(f"Updating runStartTime PV with value: {value, self.runTimestamp}")
        self.run_start_time_pv.post(value=self.runTimestamp, timestamp=self.runTimestamp)

    def update_b0_channels(self, parsed: ParsedRunFile) -> None:
        if verb_is(2): printv(2, f"Updating b0Channels PV with values")
        for record in parsed.triggedChannels:
            if record.board == 0:
                #print(f"Updating b0Channels PV with record: {record}")
                self.b0_channels_pv.post({"value": record.lg}, timestamp=self.runTimestamp + record.timestamp_us / 1e6)
                time.sleep(0.01)  # Small delay to ensure PV update is processed

    def close(self) -> None:
        self._server.stop()


def parse_run_file(path: Path) -> ParsedRunFile:
    """Parse a CAEN DT5202 run-list file.
    Returns a ParsedRunFile object containing the header and TriggedChannels records.
    """
    if verb_is(2): printv(2, f"Parsing run file: {path}")
    header: dict[str, str] = {}
    records: list[TriggedChannels] = []

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
            if verb_is(3): printv(3, f"Parsed row: board={board}, channel={channel}, lg={lg}, hg={hg}, timestamp_us={timestamp_us}, trigger_id={trigger_id}, nchs={nchs}") 

            # If timestamp_us is present, start a new TriggedChannels entry; otherwise, append to the last one.
            if timestamp_us is not None:
                records.append(TriggedChannels(
                    board=board,
                    channels=[],
                    lg=[],
                    hg=[],
                    timestamp_us=float(timestamp_us),
                    trigger_id=int(trigger_id)
                ))

            # Append the channel data to the last TriggedChannels entry.
            records[-1].channels.append(channel)
            records[-1].lg.append(lg)
            records[-1].hg.append(hg)

    #print(f"Parsed run file: {records}")
    return ParsedRunFile(path=path, header=header, triggedChannels=records)


def default_user_processor(parsed: ParsedRunFile) -> None:
    """Default hook for user-defined processing."""
    #LOG.info("Processed %s rows from %s", len(parsed.triggedChannels), parsed.path.name)
    if verb_is(1): printv(1, f"Processed {len(parsed.triggedChannels)} rows from {parsed.path.name}")


def load_user_processor(spec: str) -> Callable[[ParsedRunFile], None]:
    """Load callback from MODULE:FUNCTION spec."""
    if verb_is(2): printv(2, f"Loading user processor: {spec}")
    if ":" not in spec:
        raise ValueError("processor must be in MODULE:FUNCTION format")

    mod_name, fn_name = spec.split(":", 1)
    module = importlib.import_module(mod_name)
    fn = getattr(module, fn_name)
    if not callable(fn):
        raise TypeError(f"{spec} is not callable")
    return fn

def process_one_file(
    file_path: Path,
    out_dir: Path,
    pva: Dt5202PVServer,
    max_channels_per_board: int,
    user_processor: Callable[[ParsedRunFile], None],
) -> None:
    if verb_is(2): printv(2, f"Processing file: {file_path}")
    parsed = parse_run_file(file_path)

    user_processor(parsed)

    pva.update_run_start_time(parsed.run_start_time)
    pva.update_b0_channels(parsed)

    destination = out_dir / file_path.name
    if destination.exists():
        timestamp = int(time.time())
        destination = out_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"

    shutil.move(str(file_path), str(destination))
    #LOG.info("Moved processed file to %s", destination)

def wait_until_file_is_stable(file_path: Path, checks: int = 10, delay_seconds: float = 0.2) -> bool:
    if verb_is(2): printv(2, f"Waiting for file to stabilize: {file_path}")
    last_size: Optional[int] = None
    for _ in range(checks):
        try:
            size = file_path.stat().st_size
        except FileNotFoundError:
            return False

        if last_size is not None and size == last_size:
            return True

        last_size = size
        time.sleep(delay_seconds)

    return False


class IncomingFileHandler(FileSystemEventHandler):
    def __init__(self, in_dir: Path, file_queue: "queue.Queue[Path]"):
        super().__init__()
        self.in_dir = in_dir
        self.file_queue = file_queue

    def _queue_if_file(self, candidate: Path) -> None:
        if candidate.is_file() and candidate.parent == self.in_dir:
            self.file_queue.put(candidate)

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        self._queue_if_file(Path(event.src_path).resolve())

    def on_moved(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        dest_path = getattr(event, "dest_path", "")
        if not dest_path:
            return
        self._queue_if_file(Path(dest_path).resolve())


def watch_loop(
    in_dir: Path,
    out_dir: Path,
    pva: Dt5202PVServer,
    max_channels_per_board: int,
    user_processor: Callable[[ParsedRunFile], None],
    should_stop: Callable[[], bool],
) -> None:
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    LOG.info("Watching input directory: %s", in_dir)
    LOG.info("Output directory: %s", out_dir)

    file_queue: "queue.Queue[Path]" = queue.Queue()
    stop_event = Event()

    for existing_file in sorted(p for p in in_dir.iterdir() if p.is_file()):
        file_queue.put(existing_file.resolve())

    def worker() -> None:
        while not stop_event.is_set() or not file_queue.empty():
            try:
                file_path = file_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                if not wait_until_file_is_stable(file_path):
                    LOG.warning("Skipping unstable file %s", file_path)
                    continue

                process_one_file(
                    file_path=file_path,
                    out_dir=out_dir,
                    pva=pva,
                    max_channels_per_board=max_channels_per_board,
                    user_processor=user_processor,
                )
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.exception("Failed to process %s: %s", file_path, exc)
            finally:
                file_queue.task_done()

    worker_thread = Thread(target=worker, name="dt5202-file-worker", daemon=True)
    worker_thread.start()

    observer = Observer()
    observer.schedule(IncomingFileHandler(in_dir.resolve(), file_queue), str(in_dir), recursive=False)
    observer.start()

    try:
        while not should_stop():
            time.sleep(0.2)
    finally:
        stop_event.set()
        observer.stop()
        observer.join(timeout=5.0)
        worker_thread.join(timeout=5.0)


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    raw_argv = argv if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(description="CAEN DT5202 PVAccess server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=__version__)
    parser.add_argument("--inDir", default='dataIn', help="Directory to watch for new files")
    parser.add_argument("--outDir", default='dataOut', help="Directory to move processed files")
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
    # parser.add_argument(
    #     '--verbose',
    #     choices=['v', 'vv'],
    #     default=None,
    #     metavar="LEVEL",
    #     help="Verbosity level: 'v' for verbose, 'vv' for very verbose.",
    # )
    parser.add_argument('-v', '--verbose', action='count', default=0, help=
        'Show more log messages (-vv: show even more)')

    args = parser.parse_args(raw_argv)

    if not 1 <= args.numberOfBoards <= MAX_NUMBER_OF_BOARDS:
        parser.error(f"--numberOfBoards must be in [1, {MAX_NUMBER_OF_BOARDS}]")

    # Request has contradictory constraints: default=112 and max=64.
    # Keep default as requested; enforce max=64 for explicit user values.
    if "--maxChannelsPerBoard" in raw_argv and args.maxChannelsPerBoard > MAX_MAX_CHANNELS_PER_BOARD:
        parser.error(f"--maxChannelsPerBoard must be <= {MAX_MAX_CHANNELS_PER_BOARD}")

    if args.maxChannelsPerBoard < 1:
        parser.error("--maxChannelsPerBoard must be >= 1")

    return args


def main(argv: Optional[list[str]] = None) -> int:
    global pargs
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOG.info("Using epicsdev version: %s", EPICSDEV_VERSION)

    pargs = parse_args(argv)

    if verb_is(2): printv(2,f"Verbose level: {pargs.verbose}")

    in_dir = Path(pargs.inDir).resolve()
    out_dir = Path(pargs.outDir).resolve()

    user_processor = default_user_processor
    if pargs.processor:
        user_processor = load_user_processor(pargs.processor)
    pva = Dt5202PVServer(max_channels_per_board=pargs.maxChannelsPerBoard)

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
            max_channels_per_board=pargs.maxChannelsPerBoard,
            user_processor=user_processor,
            should_stop=lambda: stop,
        )
    finally:
        pva.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
