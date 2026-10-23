#!/usr/bin/env python3

import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time
import traceback
import uuid


def command(*args):
    return subprocess.check_output(args, text=True,
                                   env={**os.environ, "LC_ALL": "C"})


def build_id(path):
    ids = re.findall(r"Build ID: ([0-9a-f]+)", command("readelf", "-n", path))
    if len(set(ids)) != 1:
        raise ValueError(f"No unique build ID: {path}")
    return ids[0]


def function_values(path, name, dynamic=False):
    values = set()
    option = "--dyn-syms" if dynamic else "--syms"
    for line in command("readelf", "-W", option, path).splitlines():
        fields = line.split()
        if (len(fields) >= 8 and fields[3] == "FUNC" and
                fields[6] != "UND" and
                fields[7].split("@")[0] == name and
                ("@" not in fields[7] or "@@" in fields[7])):
            values.add(int(fields[1], 16))
    return values


def symbol_offset(path, name):
    identity = build_id(path)
    debug = Path("/usr/lib/debug/.build-id") / identity[:2]
    debug = debug / (identity[2:] + ".debug")
    symbols = path
    values = set()
    built = sorted(Path("/home/zfs/zfs/lib").glob("*/.libs/" + path.name))
    for candidate in [path, debug, *built]:
        if not candidate.exists():
            continue
        if build_id(candidate) != identity:
            raise ValueError(f"Debug build ID mismatch: {candidate}")
        values = function_values(candidate, name)
        if values:
            symbols = candidate
            break
    if len(values) != 1:
        raise ValueError(f"No unique function {name}: {path}")
    address = values.pop()
    offsets = []
    for line in command("readelf", "-Wl", path).splitlines():
        fields = line.split()
        if fields and fields[0] == "LOAD" and "E" in fields[6:-1]:
            offset, vaddr, _, size = [int(x, 16) for x in fields[1:5]]
            if vaddr <= address < vaddr + size:
                offsets.append(address - vaddr + offset)
    if len(offsets) != 1 or offsets[0] >= path.stat().st_size:
        raise ValueError(f"Function outside executable file segment: {name}")
    return {"path": str(path), "symbols": str(symbols), "build_id": identity,
            "symbol": name, "value": address, "offset": offsets[0]}


def control(path, value):
    # tracefs command files reject the seek performed by append-mode streams.
    fd = os.open(path, os.O_WRONLY)
    try:
        data = (value + "\n").encode()
        if os.write(fd, data) != len(data):
            raise OSError(f"Short tracefs write: {path}")
    finally:
        os.close(fd)


def read(path):
    try:
        return path.read_text()
    except OSError as error:
        return f"ERROR errno={error.errno}\n"


def snapshot(destination, tick, extra_pid=None):
    stamp = {"monotonic_ns": time.monotonic_ns(),
             "realtime_ns": time.time_ns(), "tick": tick}
    with (destination / "processes.jsonl").open("a") as output:
        for process in Path("/proc").glob("[0-9]*"):
            comm = read(process / "comm").strip()
            if (comm not in {"zhack", "zpool", "mmp_concurrent_"} and
                    process.name != str(extra_pid)):
                continue
            data = {**stamp, "pid": int(process.name), "comm": comm,
                    "children": read(process / "task" / process.name /
                                     "children"), "tasks": {}}
            data["child_processes"] = {
                pid: {field: read(Path("/proc") / pid / field)
                      for field in ["comm", "stat", "wchan", "syscall"]}
                for pid in data["children"].split() if pid.isdecimal()}
            for task in (process / "task").glob("[0-9]*"):
                fields = ["comm", "stat", "wchan", "syscall"]
                if tick % 6 == 0:
                    fields.append("stack")
                data["tasks"][task.name] = {
                    field: read(task / field) for field in fields}
            output.write(json.dumps(data) + "\n")
        output.write(json.dumps({**stamp, "snapshot_complete": True}) + "\n")
    for logfile in destination.glob("iteration-*/work/mmp/zhack.log.*"):
        saved = logfile.parent.parent.parent / "zhack-snapshots"
        saved.mkdir(exist_ok=True)
        try:
            shutil.copyfile(logfile, saved / f"{tick:04d}-{logfile.name}")
        except FileNotFoundError:
            pass


def trace_stats(destination, instance, tracefs, group):
    stats = {str(path.relative_to(instance)): read(path)
             for path in instance.glob("per_cpu/cpu*/stats")}
    incomplete = not stats or any(value.startswith("ERROR")
                                  for value in stats.values())
    profile = read(tracefs / "uprobe_profile")
    stats["uprobe_profile"] = "\n".join(
        line for line in profile.splitlines() if group in line)
    stats["unreadable"] = incomplete or profile.startswith("ERROR")
    (destination / "trace-losses.json").write_text(json.dumps(stats, indent=2))
    return stats["unreadable"] or any(
        int(value) for contents in stats.values() if isinstance(contents, str)
        for value in re.findall(r"(?:overrun|dropped events):\s+(\d+)",
                                contents))


def main():
    destination = Path(sys.argv[1])
    fixture_pid = int(sys.argv[2])
    if os.geteuid() != 0 or os.uname().machine != "x86_64":
        raise RuntimeError("Requires the isolated x86_64 CI guest as root")
    tracefs = Path("/sys/kernel/tracing")
    if not (tracefs / "uprobe_events").exists():
        tracefs = Path("/sys/kernel/debug/tracing")
    if not (tracefs / "uprobe_events").exists():
        raise RuntimeError("tracefs uprobes unavailable")

    zhack = Path(shutil.which("zhack")).resolve()
    dependencies = command("ldd", zhack)
    (destination / "zhack-ldd.txt").write_text(dependencies)
    libraries = {}
    for line in dependencies.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[1] == "=>":
            libraries[fields[0].split(".so")[0]] = Path(fields[2]).resolve()
    definitions = {}
    for symbol in ["zio_suspend", "taskq_destroy", "cv_broadcast",
                   "pthread_exit"]:
        candidates = [path for path in [zhack, *libraries.values()]
                      if function_values(path, symbol, dynamic=True)]
        if len(candidates) != 1:
            raise ValueError(f"No unique runtime definition: {symbol}")
        definitions[symbol] = candidates[0]
    definitions["taskq_thread"] = definitions["taskq_destroy"]
    probes = []
    for symbol, event, kind, args in [
        ("zio_suspend", "suspend", "p",
         "spa=%di:x64 reason=%dx:s32"),
        ("taskq_destroy", "destroy_enter", "p", "tq=%di:x64"),
        ("taskq_destroy", "destroy_return", "r", ""),
        ("taskq_thread", "worker_enter", "p", "tq=%di:x64"),
        ("cv_broadcast", "broadcast_enter", "p", "cv=%di:x64"),
        ("cv_broadcast", "broadcast_return", "r", ""),
        ("pthread_exit", "thread_exit", "p", ""),
    ]:
        probes.append({**symbol_offset(definitions[symbol], symbol),
                       "event": event, "kind": kind, "args": args})
    group = "mmp_" + uuid.uuid4().hex
    instance = tracefs / "instances" / group
    metadata = {"group": group, "fixture_pid": fixture_pid,
                "monitor_pid": os.getpid(), "probes": probes,
                "uname": list(os.uname()),
                "clock_ticks": os.sysconf("SC_CLK_TCK"),
                "packages": command("dpkg-query", "-W", "libc6", "gcc", "ksh"),
                "gcc_version": command("gcc", "-dumpfullversion"),
                "ksh_build_id": build_id(Path(shutil.which("ksh")).resolve())}
    (destination / "provenance.json").write_text(
        json.dumps(metadata, indent=2))
    registered = []
    pipe = None
    instance.mkdir()
    try:
        control(instance / "tracing_on", "0")
        control(instance / "buffer_size_kb", "4096")
        control(instance / "trace_clock", "mono")
        control(instance / "options/event-fork", "1")
        control(instance / "set_event_pid", f"{fixture_pid} {os.getpid()}")
        for probe in probes:
            event = f"{group}/{group}_{probe['event']}"
            definition = (f"{probe['kind']}:{event} {probe['path']}:"
                          f"0x{probe['offset']:x} {probe['args']}")
            control(tracefs / "uprobe_events", definition)
            registered.append(event)
            control(instance / "events" / event / "enable", "1")
            (destination / (probe["event"] + "-format.txt")).write_text(
                read(instance / "events" / event / "format"))
        for name in ["fork", "exec", "exit"]:
            control(instance / "events/sched" /
                    ("sched_process_" + name) / "enable", "1")
        pipe = os.open(instance / "trace_pipe", os.O_RDONLY | os.O_NONBLOCK)
        control(instance / "tracing_on", "1")
        preflight_code = """
import ctypes
import sys
library = ctypes.CDLL(sys.argv[1])
library.kernel_init.argtypes = [ctypes.c_int]
library.kernel_init.restype = None
library.kernel_fini.argtypes = []
library.kernel_fini.restype = None
library.kernel_init(1)
library.kernel_fini()
"""
        seen = set()
        pending = b""
        with (destination / "events.txt").open("wb", buffering=0) as events:
            def drain():
                nonlocal pending
                for _ in range(32):
                    if not select.select([pipe], [], [], 0)[0]:
                        break
                    try:
                        chunk = os.read(pipe, 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    events.write(chunk)
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        for probe in probes:
                            if (probe["event"] + ":").encode() in line:
                                seen.add(probe["event"])

            with (destination / "preflight.txt").open("w") as output:
                child = subprocess.Popen([sys.executable, "-c", preflight_code,
                                          str(libraries["libzpool"])],
                                         stdout=output, stderr=output)
                deadline = time.monotonic() + 120
                while child.poll() is None and time.monotonic() < deadline:
                    drain()
                    select.select([pipe], [], [], 0.1)
                drain()
                required = {p["event"] for p in probes} - {"suspend"}
                preflight = {"pid": child.pid, "returncode": child.poll(),
                             "observed": sorted(seen),
                             "missing": sorted(required - seen)}
                (destination / "preflight.json").write_text(
                    json.dumps(preflight, indent=2))
                if child.poll() != 0 or not required <= seen:
                    snapshot(destination, 0, child.pid)
                    raise RuntimeError("Lifecycle preflight did not qualify")
                if trace_stats(destination, instance, tracefs, group):
                    raise RuntimeError("Lost events during preflight")

            (destination / "ready").touch()
            tick = 0
            next_snapshot = 0
            while not (destination / "done").exists():
                drain()
                now = time.monotonic()
                if now >= next_snapshot:
                    snapshot(destination, tick)
                    if tick % 6 == 0 and trace_stats(
                            destination, instance, tracefs, group):
                        raise RuntimeError("Trace ring buffer lost events")
                    if events.tell() > 512 * 1024 * 1024:
                        raise RuntimeError("Trace exceeded 512 MiB budget")
                    tick += 1
                    next_snapshot = now + 10
                    (destination / "heartbeat").write_text(str(now))
                select.select([pipe], [], [], 0.1)
            snapshot(destination, tick)
            control(instance / "tracing_on", "0")
            while select.select([pipe], [], [], 0)[0]:
                drain()
    finally:
        cleanup_errors = []
        try:
            control(instance / "tracing_on", "0")
        except OSError as error:
            cleanup_errors.append(str(error))
        lost_events = False
        try:
            lost_events = trace_stats(destination, instance, tracefs, group)
        except OSError as error:
            cleanup_errors.append(str(error))
        if pipe is not None:
            os.close(pipe)
        try:
            control(instance / "events/enable", "0")
            instance.rmdir()
        except OSError as error:
            cleanup_errors.append(str(error))
        for event in reversed(registered):
            try:
                control(tracefs / "uprobe_events", "-:" + event)
            except OSError as error:
                cleanup_errors.append(str(error))
        (destination / "cleanup.json").write_text(
            json.dumps({"errors": cleanup_errors}, indent=2))
        if cleanup_errors:
            raise RuntimeError("Private trace cleanup failed")
        if lost_events:
            raise RuntimeError("Trace ring buffer lost events")


if __name__ == "__main__":
    os.umask(0o022)
    sys.stdout = sys.stderr = open(Path(sys.argv[1], "monitor.txt"), "w")
    try:
        main()
    except Exception:
        Path(sys.argv[1], "failed").write_text(traceback.format_exc())
        raise
