#!/usr/bin/env python3
"""Loopback-only dashboard for the typed events emitted by the C collector."""

import argparse
from collections import deque
import copy
import csv
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import webbrowser


COMMON = {
    "presence", "motion", "amplitude", "distance-cm", "resp-status",
    "resp-per-minute", "heart-per-minute", "range",
}
SLEEP = {
    "bed", "stage", "awake-minutes", "light-minutes", "deep-minutes",
    "score", "summary", "analysis", "sleep-error", "quality", "struggle", "absence",
}
WAVES = {"resp-wave", "heart-wave"}
VITAL_KEYS = {"motion", "resp-status", "heart-per-minute"}
HISTORY_FIELDS = {"resp-per-minute": "resp_rate", "heart-per-minute": "heart_rate",
                  "amplitude": "amplitude", "presence": "presence", "motion": "motion",
                  "resp-status": "resp_status"}
SERIES_FIELDS = ("resp_rate", "heart_rate", "amplitude")
RATE_FIELDS = {"resp_rate", "heart_rate"}  # 0 means the module could not measure
STATE_LOG_NAMES = {"presence", "motion", "resp-status"}
STATE_LOG_INTERVAL_NS = 30_000_000_000
SNAPSHOT_EVERY = 30  # record ticks (2 s each) between full snapshots
FRESH_NS = 10_000_000_000
ALARM_HOLD_SECONDS = 30
RECORD_VOLUME_SECONDS = 12 * 60 * 60


def fresh_value(entry, now_ns):
    if not entry:
        return None
    values = entry.get("values", [])
    age = now_ns - int(entry["received_ns"])
    if len(values) != 1 or type(values[0]) is not int or not 0 <= age <= FRESH_NS:
        return None
    return values[0]


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.mode = None
        self.after_ns = 0
        self.state_after_ns = 0
        self.sequence = 0
        self.common = {}
        self.sleep = {}
        self.waves = deque(maxlen=256)
        self.wave_lost_through = 0
        self.diagnostics = {}
        self.error = None
        self.active = True
        self.started_ns = time.time_ns()
        self.generation = 0
        self.switching = False
        self.target_mode = None
        self.received_events = {}
        self.accepted_events = {}
        self.discarded_events = {}
        self.resp_query = None
        self.presence_query = None
        self.monitor_config = {}
        self.mild_since = None
        self.severe_since = None
        self.alarm = {"level": 0, "state": "unknown"}
        self.alarm_events = []
        self.history_events = []
        self.logged = {}

    def _should_log(self, name, entry):
        # State reports are polled every few seconds; keep only changes and a slow heartbeat.
        if name not in STATE_LOG_NAMES:
            return True
        value, stamp = entry["values"][0], int(entry["received_ns"])
        last = self.logged.get(name)
        if last is not None and last[0] == value and stamp - last[1] < STATE_LOG_INTERVAL_NS:
            return False
        self.logged[name] = (value, stamp)
        return True

    def _evaluate_alarm(self):
        now_ns, now = time.time_ns(), time.monotonic()
        values = {key: fresh_value(self.common.get(key), now_ns) for key in VITAL_KEYS}
        motion, resp, heart = values["motion"], values["resp-status"], values["heart-per-minute"]
        available = self.active and not self.switching and self.mode in (0, 1)
        still = available and motion == 1
        resp_low = resp == 3
        heart_low = heart is not None and 0 <= heart < 50
        mild = still and (resp_low or heart_low)
        severe = still and resp_low and heart_low
        self.mild_since = (self.mild_since if self.mild_since is not None else now) if mild else None
        self.severe_since = (self.severe_since if self.severe_since is not None else now) if severe else None
        mild_seconds = now - self.mild_since if self.mild_since is not None else 0
        severe_seconds = now - self.severe_since if self.severe_since is not None else 0
        level = 2 if severe_seconds >= ALARM_HOLD_SECONDS else (
            1 if mild_seconds >= ALARM_HOLD_SECONDS else 0)
        complete = available and motion in (0, 1, 2) and resp in (1, 2, 3, 4) and (
            heart is not None and 0 <= heart <= 255)
        status = "severe" if level == 2 else "mild" if level == 1 else (
            "pending" if mild else "normal" if complete else "unknown")
        previous_level = self.alarm["level"]
        self.alarm = {
            "level": level, "state": status, "candidate": 2 if severe else 1 if mild else 0,
            "pending_seconds": int(severe_seconds if severe else mild_seconds),
            "hold_seconds": ALARM_HOLD_SECONDS, "complete": complete,
            "inputs": values, "recorded_ns": str(now_ns),
        }
        if level != previous_level:
            self.alarm_events.append(dict(self.alarm, kind="alarm", mode=self.mode,
                                          session=str(self.started_ns)))

    def get_alarm(self):
        with self.lock:
            self._evaluate_alarm()
            return copy.deepcopy(self.alarm)

    def take_alarm_events(self):
        with self.lock:
            events, self.alarm_events = self.alarm_events, []
            return events

    def take_history_events(self):
        with self.lock:
            events, self.history_events = self.history_events, []
            return events

    def begin(self, mode):
        with self.lock:
            self.generation += 1
            self.started_ns = max(time.time_ns(), self.started_ns + 1)
            self.target_mode = mode
            self.mode = None
            self.switching = True
            self.active = True
            self.error = None
            self.common.clear()
            self.sleep.clear()
            self.waves.clear()
            self.wave_lost_through = 0
            self.diagnostics.clear()
            self.received_events.clear()
            self.accepted_events.clear()
            self.discarded_events.clear()
            self.resp_query = None
            self.presence_query = None
            self.monitor_config.clear()
            self.logged.clear()
            self._evaluate_alarm()
            return self.generation

    def apply(self, message, generation=None):
        with self.lock:
            if generation is not None and generation != self.generation:
                return False
            # Check the old samples first so a reporting gap cannot bridge the hold timer.
            self._evaluate_alarm()
            kind = message["kind"]
            if kind == "session":
                if self.target_mode is not None and message["mode"] != self.target_mode:
                    raise ValueError("collector confirmed the wrong mode")
                self.mode = message["mode"]
                self.after_ns = int(message["after_ns"])
                self.state_after_ns = self.after_ns
                self.switching = False
                self.common.clear()
                self.sleep.clear()
                self.waves.clear()
                self.wave_lost_through = 0
                return True
            if kind == "status":
                self.diagnostics = {key: value for key, value in message.items() if key != "kind"}
                return
            if kind == "resp-query":
                self.resp_query = {"ok": message["ok"], "error": message["error"]}
                return
            if kind == "presence-query":
                self.presence_query = {"ok": message["ok"], "error": message["error"]}
                return
            if kind == "data-loss":
                for key in VITAL_KEYS | {"presence"}:
                    self.common.pop(key, None)
                self.presence_query = self.resp_query = None
                self.state_after_ns = max(self.after_ns, time.time_ns())
                self._evaluate_alarm()
                return
            if kind == "monitor-config":
                control = message["control"]
                if control not in (0x80, 0x81, 0x84, 0x85):
                    raise ValueError("unexpected monitor control")
                self.monitor_config[str(control)] = {"ok": message["ok"], "error": message["error"]}
                return
            if kind != "event":
                raise ValueError("unknown collector message kind")
            name = message["name"]
            self.received_events[name] = self.received_events.get(name, 0) + 1
            if name in {"reset", "protocol-error"}:
                self.error = "{}: {}".format(name, message["error"])
            if message["mode"] != self.mode or int(message["received_ns"]) < self.after_ns:
                self.discarded_events[name] = self.discarded_events.get(name, 0) + 1
                return
            if name in VITAL_KEYS | {"presence"} and int(message["received_ns"]) < self.state_after_ns:
                self.discarded_events[name] = self.discarded_events.get(name, 0) + 1
                return
            group = self.common if name in COMMON else self.sleep
            existing = group.get(name)
            if existing and int(message["received_ns"]) < int(existing["received_ns"]):
                self.discarded_events[name] = self.discarded_events.get(name, 0) + 1
                return
            self.sequence += 1
            entry = {
                "values": message["values"], "received_ns": message["received_ns"],
                "sequence": self.sequence,
                "source": "query" if message.get("command", 0) & 0x80 else "report",
            }
            if name in COMMON:
                self.common[name] = entry
            elif name in SLEEP and self.mode == 1:
                self.sleep[name] = entry
            elif name in WAVES and self.mode == 0:
                if len(self.waves) == self.waves.maxlen:
                    self.wave_lost_through = self.waves[0]["sequence"]
                self.waves.append(dict(entry, name=name))
            else:
                return
            self.accepted_events[name] = self.accepted_events.get(name, 0) + 1
            self._evaluate_alarm()
            if name in HISTORY_FIELDS and entry["values"] and self._should_log(name, entry):
                self.history_events.append(dict(
                    entry, kind="event", name=name, mode=self.mode,
                    session=str(self.started_ns), recorded_ns=entry["received_ns"],
                    alarm={"level": self.alarm["level"], "state": self.alarm["state"]}))

    def snapshot(self, after=0, session=None):
        with self.lock:
            self._evaluate_alarm()
            if session is not None and session != str(self.started_ns):
                after = 0
            return {
                "schema": 1, "session": str(self.started_ns), "sequence": self.sequence,
                "recorded_ns": str(time.time_ns()), "mode": self.mode,
                "active": self.active, "error": self.error,
                "switching": self.switching, "target_mode": self.target_mode,
                "common": copy.deepcopy(self.common), "sleep": copy.deepcopy(self.sleep),
                "diagnostics": dict(
                    self.diagnostics, received_events=dict(self.received_events),
                    accepted_events=dict(self.accepted_events),
                    discarded_events=dict(self.discarded_events),
                    resp_query=copy.deepcopy(self.resp_query),
                    presence_query=copy.deepcopy(self.presence_query),
                    monitor_config=copy.deepcopy(self.monitor_config)),
                "waves": [dict(batch) for batch in self.waves if batch["sequence"] > after],
                "wave_gap": after < self.wave_lost_through,
                "alarm": copy.deepcopy(self.alarm),
            }

    def finish(self, error, generation=None):
        with self.lock:
            if generation is not None and generation != self.generation:
                return
            if not self.active:
                return
            self.active = False
            self.switching = False
            if error:
                self.error = error
            self._evaluate_alarm()


class HistoryStore:
    def __init__(self, directory, prefix="r60abd1"):
        if not prefix or any(not (c.isalnum() or c in "-_") for c in prefix):
            raise ValueError("record prefix must contain only letters, digits, '-' or '_'")
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.started_ns = time.time_ns()
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.volume = None
        self.record = None

    def write(self, snapshot, alarms, events=()):
        lines = [json.dumps(event, ensure_ascii=True) for event in events]
        if snapshot is not None:
            lines.append(json.dumps(dict(snapshot, kind="snapshot"), ensure_ascii=True))
        lines.extend(json.dumps(alarm, ensure_ascii=True) for alarm in alarms)
        if not lines:
            return
        with self.lock:
            volume = int((time.monotonic() - self.started) // RECORD_VOLUME_SECONDS)
            if self.volume != volume or self.record is None:
                if self.record is not None:
                    self.record.close()
                path = self.directory / "{}-{}-{:04d}.jsonl".format(
                    self.prefix, self.started_ns, volume)
                self.record = path.open("x", encoding="utf-8")
                self.volume = volume
            self.record.write("\n".join(lines) + "\n")
            self.record.flush()

    def close(self):
        with self.lock:
            if self.record is not None:
                self.record.close()
                self.record = None

    def files(self, start_ns=None):
        result = []
        for path in sorted(self.directory.glob("{}-*.jsonl".format(self.prefix))):
            try:
                # Skip files last written long before the range; one day of slack covers clock steps.
                if start_ns is not None and path.stat().st_mtime_ns < start_ns - 86_400_000_000_000:
                    continue
            except OSError:
                continue
            result.append(path)
        return result

    @staticmethod
    def records(files, start_ns=None, end_ns=None, stats=None):
        """Yield valid records; a torn last line (killed writer) or a corrupt line is skipped."""
        stats = stats if stats is not None else {}
        for path in files:
            try:
                handle = path.open("rb")
            except OSError:
                stats["unreadable"] = stats.get("unreadable", 0) + 1
                continue
            with handle:
                for line in handle:
                    if not line.endswith(b"\n"):
                        stats["partial"] = stats.get("partial", 0) + 1
                        break
                    try:
                        data = json.loads(line)
                        stamp = int(data["recorded_ns"])
                        if data.get("kind", "snapshot") not in ("snapshot", "alarm", "event"):
                            raise ValueError("invalid record kind")
                    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
                        stats["skipped"] = stats.get("skipped", 0) + 1
                        continue
                    if (start_ns is None or stamp >= start_ns) and (
                            end_ns is None or stamp <= end_ns):
                        yield data

    @staticmethod
    def point(record):
        stamp = int(record["recorded_ns"])
        common = record.get("common", {})
        result = {"recorded_ns": str(stamp), "mode": record.get("mode"),
                  "session": record.get("session")}
        for field, key in (("presence", "presence"), ("resp_rate", "resp-per-minute"),
                           ("heart_rate", "heart-per-minute"), ("motion", "motion"),
                           ("amplitude", "amplitude"), ("resp_status", "resp-status")):
            result[field] = fresh_value(common.get(key), stamp)
            if record.get("kind") == "alarm" and key in VITAL_KEYS:
                result[field] = record.get("inputs", {}).get(key)
            if record.get("kind") == "event" and key == record.get("name"):
                result[field] = fresh_value(record, int(record["received_ns"]))
        alarm = record if record.get("kind") == "alarm" else record.get("alarm", {})
        result["alarm_level"] = alarm.get("level")
        result["alarm_state"] = alarm.get("state")
        return result

    @staticmethod
    def entries(record):
        kind = record.get("kind", "snapshot")
        if kind == "event":
            yield record.get("name"), record
        elif kind == "snapshot" and isinstance(record.get("common"), dict):
            yield from record["common"].items()

    def summary(self, start_ns, end_ns, buckets=240):
        """Aggregate stored reports into a bounded number of time buckets per series."""
        if not 0 <= start_ns <= end_ns or not 10 <= buckets <= 1000:
            raise ValueError("invalid history range or bucket count")
        width = max(end_ns - start_ns, 1) / buckets
        bins = {field: {} for field in SERIES_FIELDS}
        counts = dict.fromkeys(SERIES_FIELDS, 0)
        stats = {}
        last_sequence, session_end, presence = {}, {}, []
        alarms, alarm_count = deque(maxlen=20), 0
        files = self.files(start_ns)
        for record in self.records(files, stats=stats):
            recorded = int(record["recorded_ns"])
            if record.get("kind") == "alarm":
                if start_ns <= recorded <= end_ns:
                    alarm_count += 1
                    alarms.append({"recorded_ms": recorded // 1_000_000, "level": record.get("level"),
                                   "state": record.get("state")})
                continue
            session = record.get("session")
            session_end[session] = max(session_end.get(session, 0), recorded)
            for name, entry in self.entries(record):
                field = HISTORY_FIELDS.get(name)
                if field is None or not isinstance(entry, dict):
                    continue
                try:
                    stamp = int(entry["received_ns"])
                    value = entry["values"][0]
                    sequence = entry.get("sequence")
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                if type(value) is not int or not start_ns <= stamp <= end_ns:
                    continue
                if sequence is not None:
                    key = (session, name)
                    if last_sequence.get(key, -1) >= sequence:
                        continue
                    last_sequence[key] = sequence
                if field in SERIES_FIELDS:
                    if field in RATE_FIELDS and value <= 0:
                        continue
                    index = min(buckets - 1, int((stamp - start_ns) / width))
                    cell = bins[field].get(index)
                    if cell is None:
                        bins[field][index] = [1, value, value, value]
                    else:
                        cell[0] += 1
                        cell[1] += value
                        cell[2] = min(cell[2], value)
                        cell[3] = max(cell[3], value)
                    counts[field] += 1
                elif field == "presence" and value in (0, 1):
                    presence.append((stamp, session, value))
        series = {}
        for field in SERIES_FIELDS:
            rows = []
            for index in sorted(bins[field]):
                count, total, low, high = bins[field][index]
                middle = start_ns + (index + 0.5) * width
                rows.append([int(middle // 1_000_000), round(total / count, 1), low, high, count])
            series[field] = rows
        presence.sort(key=lambda item: item[0])
        segments = []
        for stamp, session, value in presence:
            last = segments[-1] if segments else None
            if last is not None and last["session"] == session:
                if last["value"] == value:
                    continue
                last["end"] = stamp
            elif last is not None:
                last["end"] = max(last["start"], session_end.get(last["session"], 0))
            segments.append({"session": session, "value": value, "start": stamp, "end": stamp})
        if segments:
            tail = segments[-1]
            tail["end"] = max(tail["start"], session_end.get(tail["session"], 0))
        return {
            "schema": 3, "start_ms": start_ns // 1_000_000, "end_ms": end_ns // 1_000_000,
            "series": series, "counts": counts,
            "presence": [{"start": s["start"] // 1_000_000, "end": min(s["end"], end_ns) // 1_000_000,
                          "value": s["value"]} for s in segments],
            "alarms": list(alarms), "alarm_count": alarm_count,
            "files": len(files), "skipped": stats.get("skipped", 0) + stats.get("partial", 0),
        }

    def export(self, files, start_ns, end_ns, format_name):
        if format_name not in ("csv", "jsonl") or not 0 <= start_ns <= end_ns:
            raise ValueError("invalid export range or format")
        fields = ["recorded_ns", "time_utc", "kind", "name", "received_ns", "mode", "session", "presence", "resp_rate",
                  "heart_rate", "motion", "amplitude", "resp_status", "alarm_level", "alarm_state"]
        if format_name == "csv":
            yield (",".join(fields) + "\r\n").encode("utf-8")
        for record in self.records(files, start_ns, end_ns):
            if format_name == "jsonl":
                yield (json.dumps(record, ensure_ascii=True) + "\n").encode("utf-8")
            else:
                point = self.point(record)
                point["kind"] = record.get("kind", "snapshot")
                point["name"] = record.get("name")
                point["received_ns"] = record.get("received_ns")
                point["time_utc"] = datetime.fromtimestamp(
                    int(record["recorded_ns"]) / 1e9, timezone.utc).isoformat()
                buffer = io.StringIO(newline="")
                csv.DictWriter(buffer, fieldnames=fields).writerow(point)
                yield buffer.getvalue().encode("utf-8")


class Collector:
    def __init__(self, executable, device, state, record):
        self.executable = executable
        self.device = device
        self.state = state
        self.record = record
        self.operation = threading.Lock()
        self.record_lock = threading.Lock()
        self.process = None
        self.reader = None
        self.closing = False

    def record_snapshot(self, final=False, full=True):
        with self.record_lock:
            snapshot = None
            if full or final:
                # A cursor past every wave batch keeps raw waveforms out of the record file.
                snapshot = self.state.snapshot(after=sys.maxsize)
                snapshot["final"] = final
            self.record.write(snapshot, self.state.take_alarm_events(), self.state.take_history_events())

    def _stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.reader is not None:
            self.reader.join(timeout=3)
            if self.reader.is_alive():
                raise RuntimeError("old collector reader did not stop")
        self.process = None
        self.reader = None

    def switch(self, mode):
        if mode not in ("realtime", "sleep"):
            raise ValueError("invalid mode")
        if not self.operation.acquire(blocking=False):
            raise RuntimeError("another mode operation is running")
        generation = None
        try:
            if self.closing:
                raise RuntimeError("service is stopping")
            target = 0 if mode == "realtime" else 1
            current = self.state.snapshot()
            if current["mode"] == target and current["active"] and not current["switching"]:
                return current
            if self.process is not None:
                self.record_snapshot()
            generation = self.state.begin(target)
            self._stop()
            ready = threading.Event()
            self.process = subprocess.Popen(
                [self.executable, "--json", self.device, mode],
                stdout=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
            self.reader = threading.Thread(
                target=collect, args=(self.process, self.state, generation, ready), daemon=True)
            self.reader.start()
            if not ready.wait(20):
                raise RuntimeError("collector mode confirmation timed out")
            snapshot = self.state.snapshot()
            if not snapshot["active"] or snapshot["mode"] != target:
                raise RuntimeError(snapshot["error"] or "mode was not confirmed")
            return snapshot
        except (OSError, RuntimeError, ValueError) as error:
            if generation is not None:
                self.state.finish("Mode change failed: {}".format(error), generation)
                self._stop()
            raise
        finally:
            self.operation.release()

    def close(self):
        with self.operation:
            self.closing = True
            self.state.finish(None)
            self._stop()


def handler_for(state, html, collector=None, history=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def do_GET(self):
            route = urlsplit(self.path)
            if route.path == "/":
                body = html
                content_type = "text/html; charset=utf-8"
            elif route.path == "/api/snapshot":
                try:
                    after = int(parse_qs(route.query).get("after", ["0"])[0])
                    if after < 0:
                        raise ValueError("negative cursor")
                except ValueError:
                    self.send_error(400, "Invalid cursor")
                    return
                session = parse_qs(route.query).get("session", [None])[0]
                body = json.dumps(state.snapshot(after, session), ensure_ascii=True).encode("utf-8")
                content_type = "application/json"
            elif route.path == "/api/alarm":
                body = json.dumps(state.get_alarm(), ensure_ascii=True).encode("utf-8")
                content_type = "application/json"
            elif route.path in ("/api/history", "/api/export") and history is not None:
                query = parse_qs(route.query)
                try:
                    start_ns = int(query.get("start_ns", [""])[0])
                    end_ns = int(query.get("end_ns", [""])[0])
                    buckets = int(query.get("buckets", ["240"])[0])
                    format_name = query.get("format", ["jsonl"])[0]
                    if not 0 <= start_ns <= end_ns or not 10 <= buckets <= 1000:
                        raise ValueError("invalid time range or bucket count")
                    if format_name not in ("csv", "jsonl"):
                        raise ValueError("format must be csv or jsonl")
                except ValueError as error:
                    self.send_error(400, str(error))
                    return
                try:
                    if route.path == "/api/export":
                        self.export_history(history.files(start_ns), start_ns, end_ns, format_name)
                        return
                    body = json.dumps(history.summary(start_ns, end_ns, buckets),
                                      ensure_ascii=True).encode("utf-8")
                    content_type = "application/json"
                except (OSError, ValueError, TypeError, KeyError) as error:
                    print("History query failed: {}".format(error), file=sys.stderr)
                    self.send_error(500, "History could not be read")
                    return
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError) as error:
                print("Browser disconnected: {}".format(error), file=sys.stderr)

        def export_history(self, files, start_ns, end_ns, format_name):
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8" if format_name == "csv"
                             else "application/x-ndjson; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="r60abd1-history.{}"'.format(format_name))
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                for chunk in history.export(files, start_ns, end_ns, format_name):
                    self.wfile.write("{:x}\r\n".format(len(chunk)).encode("ascii"))
                    self.wfile.write(chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
                # Do not terminate the chunk stream normally after a failed export.
                self.close_connection = True
                print("History export interrupted: {}".format(error), file=sys.stderr)

        def do_POST(self):
            if self.path != "/api/mode" or collector is None:
                self.send_error(404)
                return
            origin = self.headers.get("Origin")
            if origin and origin != "http://127.0.0.1:{}".format(self.server.server_port):
                self.send_error(403, "Cross-origin mode requests are not allowed")
                return
            if self.headers.get_content_type() != "application/json":
                self.send_error(415, "Expected application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 128:
                    raise ValueError("invalid request size")
                message = json.loads(self.rfile.read(length))
                if not isinstance(message, dict) or message.get("mode") not in ("realtime", "sleep"):
                    raise ValueError("invalid mode")
            except (ValueError, UnicodeError) as error:
                self.send_error(400, str(error))
                return
            try:
                snapshot = collector.switch(message["mode"])
                code, result = 200, snapshot
            except (OSError, RuntimeError, ValueError) as error:
                print("Mode request failed: {}".format(error), file=sys.stderr)
                code, result = 409, {"error": str(error), "snapshot": state.snapshot()}
            body = json.dumps(result, ensure_ascii=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError) as error:
                print("Browser disconnected: {}".format(error), file=sys.stderr)

        def log_message(self, fmt, *args):
            pass

    return Handler


def collect(process, state, generation=None, ready=None):
    try:
        for line in process.stdout:
            confirmed = state.apply(json.loads(line), generation)
            if confirmed and ready is not None:
                ready.set()
        code = process.wait()
        snapshot = state.snapshot()
        report_failure = code and snapshot["active"] and (
            generation is None or generation == state.generation)
        state.finish("Collector exited with code {}".format(code), generation)
        if report_failure:
            print("Collector failed with code {}".format(code), file=sys.stderr)
    except (ValueError, KeyError, TypeError, OSError) as error:
        state.finish("Collector stream error: {}".format(error), generation)
        print(state.error, file=sys.stderr)
        if process.poll() is None:
            process.terminate()
    finally:
        process.stdout.close()
        if ready is not None:
            ready.set()


def open_browser(url):
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("Open {} in the board's browser.".format(url), file=sys.stderr)
        return
    # A root shell without the desktop's X authority cannot start a browser; skip the noisy failure.
    if hasattr(os, "geteuid") and os.geteuid() == 0 and not os.environ.get("XAUTHORITY"):
        print("Open {} in the board's browser (root shell, no X authority).".format(url), file=sys.stderr)
        return
    try:
        if not webbrowser.open(url, new=1):
            print("Open {} in the board's browser.".format(url), file=sys.stderr)
    except (webbrowser.Error, OSError) as error:
        print("Browser launch failed: {}; open {} manually.".format(error, url), file=sys.stderr)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector", required=True)
    parser.add_argument("device", nargs="?", default="/dev/r60abd1")
    parser.add_argument("mode", nargs="?", choices=("realtime", "sleep"), default="realtime")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--record-dir", default="~/r60abd1-data")
    parser.add_argument("--record", help="legacy filename: use its directory/stem for rotating volumes")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    html = Path(__file__).with_name("r60abd1-ui.html").read_bytes()
    state = State()
    # Bind before opening the device: port conflicts must not start a hidden collector.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(state, html))
    server.daemon_threads = True
    collector = None
    history = None
    http_thread = None
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    try:
        record_path = Path(args.record).expanduser() if args.record else None
        history = HistoryStore(record_path.parent if record_path else args.record_dir,
                               record_path.stem if record_path else "r60abd1")
        collector = Collector(args.collector, args.device, state, history)
        server.RequestHandlerClass = handler_for(state, html, collector, history)
        collector.switch(args.mode)
        collector.record_snapshot()
        http_thread = threading.Thread(target=server.serve_forever, daemon=True)
        http_thread.start()
        url = "http://127.0.0.1:{}/".format(server.server_port)
        print("Dashboard: {}  (records: {})".format(url, history.directory), flush=True)
        if not args.no_browser:
            threading.Thread(target=open_browser, args=(url,), daemon=True).start()
        try:
            ticks = 0
            while not stopping.wait(2):
                ticks += 1
                collector.record_snapshot(full=ticks % SNAPSHOT_EVERY == 0)
        except KeyboardInterrupt:
            pass
        finally:
            collector.close()
            collector.record_snapshot(final=True)
        return 0
    finally:
        if collector is not None:
            collector.close()
        if http_thread is not None:
            server.shutdown()
        server.server_close()
        if history is not None:
            history.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print("UI startup/recording failed: {}".format(error), file=sys.stderr)
        sys.exit(1)
