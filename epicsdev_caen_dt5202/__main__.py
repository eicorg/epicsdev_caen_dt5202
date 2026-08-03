#!/usr/bin/env python3
"""PVAccess server for CAEN DT5202 run-list files.

This server:
- watches an input directory,
- parses new files with CAEN list format (example: data/RunXXX_list.txt),
- calls a user hook for custom processing,
- updates PVs,
- moves processed files to an output directory.

The user hook is implemented as a plugin module, which can be specified with the --plugin argument.
The plugin module must implement the following functions:
- init(parent): Initialize the plugin with a reference to the main data class.
- get_pvdefs(): Return PV definitions for the plugin.
- publish(): Publish PVs based on the current data.
"""
# pylint: disable=invalid-name
from __future__ import annotations
__version__ = 'v1.0.3 2026-08-03'# beamloss: Reference rings published for J3 and J4

import argparse
import logging
import queue
import shutil
import sys
import time
from time import perf_counter as timer
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from datetime import datetime
import numpy as np
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from epicsdev import epicsdev

def my_pv_defs():
    """Define PVs for the CAEN DT5202 server."""
    F = "features"
    T = "type"
    U = "units"
    LL = "limitLow"
    LH = "limitHigh"
    SET = "setter"
    pvdefs = [
['b0LGMean','Low Gain Mean values of board0 chanels, accumulated during run', [0.]],
['b0LGRMS','Low Gain RMS of board0 chanels, accumulated during run', [0.]],
['b0LGP2P','Low Gain Peak-to-peak of board0 chanels, accumulated during run', [0.]],
['b0HGMean','High Gain Mean values of board0 chanels, accumulated during run', [0.]],
['b0HGRMS','High Gain RMS of board0 chanels, accumulated during run', [0.]],
['b0HGP2P','High Gain Peak-to-peak of board0 chanels, accumulated during run', [0.]],
    ]

    # Add PVs from the plugin.
    if C_.plugin:
        pvdefs = pvdefs + C_.plugin.get_pvdefs()

    return pvdefs

LOG = logging.getLogger("dt5202")

#````````````````````````````Necessary explicit globals```````````````````````
def _printTime():
    return datetime.now().strftime("%m%d:%H%M%S,%f")#[:-3]
def verb_is(level:int) -> bool:
    return level <= C_.pargs.verbose
def printv(level:int, msg: str):
    print(f'DBG{level}@{_printTime()}: {msg}')

MAX_NUMBER_OF_BOARDS = 16
MAX_CHANNELS_PER_BOARD = 64

@dataclass(slots=True)
class C_:
    pargs = None# program arguments
    prefix = "dt5202"
    plugin = None# plugin module for custom processing
    cyclesSinceUpdate = 0

@dataclass(slots=True)
class TriggedChannels:
    board: int
    channels: list[int]
    lg: list[float]
    hg: list[float]

class Dt5202PVServer:
    def update_b0channels(self, parsed: dict) -> None:
        for key in parsed:
            if key.startswith('b0'):
                channels = [0.] * MAX_CHANNELS_PER_BOARD
                for ch, value in parsed[key].items():
                    channels[ch] = value
                #print(f"Updating PV {key} with value: {channels}")
                epicsdev.publish(key, channels, t=parsed['run_start_time'])
        if C_.plugin:
            C_.plugin.publish()

    def close(self) -> None:
        print("Closing PV server...")

def parse_run_file(path: Path):
    """Parse a CAEN DT5202 run-list file.
    """
    if verb_is(1): printv(1, f"Parsing run file: {path}")
    header: dict[str, str] = {}
    lgCannels = {}
    hgCannels = {}
    trigCount = 0
    timestamps = []

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            if trigCount >= C_.pargs.accumulate:
                #printv(1, f"Reached accumulation limit of {C_.pargs.accumulate} events. Stopping parsing.")
                break
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
            if channel not in lgCannels:
                lgCannels[channel] = []
                hgCannels[channel] = []
            lg = float(cols[2])
            hg = float(cols[3])
            #print(f'channel: {channel}, {lg2arr}')

            timestamp_us = float(cols[4]) if len(cols) > 4 else None
            # # If timestamp_us is present, start a new TriggedChannels entry; otherwise, append to the last one.
            if timestamp_us is not None:
                trigCount += 1
                timestamps.append(timestamp_us/1e6)
                if trigCount >= C_.pargs.accumulate:
                    #printv(1, f"Reached accumulation limit of {pargs.accumulate} events. Stopping parsing.")
                    break

            lgCannels[channel].append(lg)
            hgCannels[channel].append(hg)
            trigger_id = int(cols[5]) if len(cols) > 5 else None
            nchs = int(cols[6]) if len(cols) > 6 else None
            if verb_is(2): printv(2, f"Parsed row: board={board}, channel={channel}, lg={lg}, hg={hg}, trigCount:{trigCount}")#, timestamp_us={timestamp_us}, trigger_id={trigger_id}, nchs={nchs}") 

    #print(f"Parsed run file: {trigCount} events, header: {header}")#lg shape: {lg.shape}, hg shape: {hg.shape}")
    # extract run start time from header and convert to timestamp
    timetxt = header.get("Run start time", "")
    dt_obj = datetime.strptime(timetxt, '%a %b %d %H:%M:%S %Y %Z')
    runStartTime = dt_obj.timestamp() + timestamps[0]

    #print(f"lgChannels: {lgCannels}, ")
    #print(f"Parsed run file: {trigCount} events, header: {header}, runStartTime: {runStartTime}, timestamps: {timestamps}")
    r = {
    'run_start_time': runStartTime,
    'timestamps': timestamps,
    'b0LGMean': {ch: np.mean(np.array(lgCannels[ch])) for ch in lgCannels},
    'b0LGRMS': {ch: np.std(np.array(lgCannels[ch])) for ch in lgCannels},
    'b0LGP2P': {ch: np.ptp(np.array(lgCannels[ch])) for ch in lgCannels},
    'b0HGMean': {ch: np.mean(np.array(hgCannels[ch])) for ch in hgCannels},
    'b0HGRMS': {ch: np.std(np.array(hgCannels[ch])) for ch in hgCannels},
    'b0HGP2P': {ch: np.ptp(np.array(hgCannels[ch])) for ch in hgCannels},
    }
    #print(f"Computed mean values: lg mean: {r['b0LGMean']}, hg mean: {r['b0HGMean']} RMS: {r['b0LGRMS']}, P2P: {r['b0LGP2P']}")
    return r

def process_one_file(file_path: Path, out_dir: Path, pva: Dt5202PVServer):
    if verb_is(2): printv(2, f"Processing file: {file_path}")
    parsed = parse_run_file(file_path)

    pva.update_b0channels(parsed)

    destination = out_dir / file_path.name
    if destination.exists():
        timestamp = int(time.time())
        destination = out_dir / f"{file_path.stem}_{timestamp}{file_path.suffix}"

    shutil.move(str(file_path), str(destination))
    #LOG.info("Moved processed file to %s", destination)

def wait_until_file_is_stable(file_path: Path, timeout_seconds: float = 4, delay_seconds: float = 0.5) -> bool:
    if verb_is(2): printv(2, f"Waiting for file to stabilize: {file_path}")
    last_size = None
    ts = time.time()
    while time.time() - ts < timeout_seconds:
        try:
            size = file_path.stat().st_size
        except FileNotFoundError:
            return False
        if verb_is(1): printv(1, f"Checking file size {size} for {last_size} at {time.time() - ts:.2f}s")
        if last_size is not None and size == last_size:
            return True

        last_size = size
        time.sleep(delay_seconds)
    return False

class IncomingFileHandler(FileSystemEventHandler):
    def __init__(self, in_dir: Path, fileQueue: "queue.Queue[Path]"):
        super().__init__()
        self.in_dir = in_dir
        self.file_queue = fileQueue

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

file_queue: "queue.Queue[Path]" = queue.Queue()

def start_watch( in_dir: Path, out_dir: Path, pva: Dt5202PVServer, ) -> None:
    """ Start watching the input directory for new files and process them.
    """
    LOG.info("Watching input directory: %s", in_dir)
    LOG.info("Output directory: %s", out_dir)

    stop_event = Event()

    for existing_file in sorted(p for p in in_dir.iterdir() if p.is_file()):
        file_queue.put(existing_file.resolve())

    def worker() -> None:
        print("Worker thread started.")
        fileCount = 0
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
                )
                fileCount += 1
                LOG.info('%s',f'file#{fileCount} processed {file_path.name}')
            except (OSError, ValueError, RuntimeError) as exc:
                LOG.exception("Failed to process %s: %s", file_path, exc)
            finally:
                file_queue.task_done()
        print("Worker thread exiting.")

    worker_thread = Thread(target=worker, name="dt5202-file-worker", daemon=True)
    worker_thread.start()

    observer = Observer()
    observer.schedule(IncomingFileHandler(in_dir.resolve(), file_queue), str(in_dir), recursive=False)
    observer.start()

def parse_args(argv: list[str]) -> argparse.Namespace:
    raw_argv = argv if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(description="CAEN DT5202 PVAccess server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=__version__)
    parser.add_argument("-a","--accumulate", type=float, default=1e6, help=
        "How many events to accumulate in each run")
    parser.add_argument("-i","--inDir", default='dataIn', help=
        "Directory to watch for new files, directory name will be suffixed with _[instance]")
    parser.add_argument("-n","--numberOfBoards", type=int, default=1, help=
        "Number of boards max 1)",)
    parser.add_argument("-o","--outDir", default='dataOut', help=
        "Directory to move processed files to, directory name will be suffixed with _[instance]")
    parser.add_argument("-p", "--plugin", default='beamloss', help=
        "Plugin name for custom processing, e.g. 'beamloss' will use epicsdev_caen_dt5202.beamloss")
    parser.add_argument("-v", "--verbose", action="count", default=0, help=
        "Show more log messages (-vv: show even more)")
    parser.add_argument("instance", default='01', help=
        "Instance name for PVs, e.g. '01' will create PVs like dt5202_01:b0LGMean")  
    args = parser.parse_args(raw_argv)

    args.inDir = f"{args.inDir}_{args.instance}"
    args.outDir = f"{args.outDir}_{args.instance}"
    args.prefix = f"dt5202_{args.instance}:"
    return args

ElapsedTime = {'process': 0., 'publish': 0., 'poll': 0.}
def periodic_update():
    """Perform periodic update"""
    #LOG.info('%s',f'Elapsed times during last {C_.cyclesSinceUpdate} cycles: {[(name, round(v,4)) for name, v in ElapsedTime.items()]}')
    if verb_is(1): printv(1, f'Elapsed times during last {C_.cyclesSinceUpdate} cycles: {[(name, round(v,4)) for name, v in ElapsedTime.items()]}')
    C_.cyclesSinceUpdate = 0
    for key in ElapsedTime:
        ElapsedTime[key] = 0.

def poll():
    """Device polling function, called every cycle when server is running.
    Recompute image and publish row PVs and statistics periodically
    """
    C_.cyclesSinceUpdate += 1
    ts0 = timer()
    ElapsedTime['poll'] += timer() - ts0

#def main(argv=None):
def main(argv=None):
    """Main function to start the PVAccess server for CAEN DT5202 run-list files
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    C_.pargs = parse_args(argv)

    if C_.pargs.plugin:
        try:
            C_.plugin = __import__(f"epicsdev_caen_dt5202.{C_.pargs.plugin}", fromlist=[''])
            C_.plugin.init(C_)
            LOG.info("Loaded plugin: %s", C_.plugin.__name__)
        except ImportError as e:
            LOG.error("Failed to load plugin '%s': %s", C_.pargs.plugin, e)
            sys.exit(1)

    in_dir = Path(C_.pargs.inDir).resolve()
    out_dir = Path(C_.pargs.outDir).resolve()
    for dir_path in [in_dir, out_dir]:
        if not dir_path.exists():
            LOG.error("Directory %s does not exist", dir_path)
            #raise FileNotFoundError(f"{dir_path} does not exist")
            sys.exit(1)

    C_.pargs.accumulate = int(C_.pargs.accumulate)
    #print(f"Parsed arguments: {C_.pargs}")

    if verb_is(2): printv(2,f"Verbose level: {C_.pargs.verbose}")

    pva = Dt5202PVServer()
    pvs = epicsdev.init_epicsdev(C_.pargs.prefix, my_pv_defs(), verbose=0)#
    epicsdev.set_server('Start')
    _ = epicsdev.Server(providers=[pvs])# Should assign the server to a variable to prevent it from being garbage collected

    start_watch(in_dir=in_dir, out_dir=out_dir, pva=pva,)
    try:
        while True:
            state = epicsdev.serverState()
            if state.startswith("Exit"):
                break
            if not state.startswith('Stop'):
                poll()
            if not epicsdev.sleep():# Sleep and update performance metrics periodically
                periodic_update()
    except KeyboardInterrupt:
        print("KeyboardInterrupt received. Exiting...")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
