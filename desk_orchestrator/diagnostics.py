"""Bounded, process-shared diagnostics. Never store credentials or API bodies."""
import contextvars
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager, closing
from pathlib import Path

_trace = contextvars.ContextVar("desk_trace", default=None)
MAX_EVENTS = 2000


@contextmanager
def trace(identifier=None):
    token = _trace.set(identifier or _trace.get() or uuid.uuid4().hex)
    try:
        yield _trace.get()
    finally:
        _trace.reset(token)


def error_text(exc):
    # Known adapter errors contain actionable messages, never HTTP response bodies.
    from .core import DeskError
    return str(exc) if isinstance(exc, (DeskError, OSError)) else type(exc).__name__


def target(config, step):
    """Explicit allowlist: do not copy connection configuration into the log."""
    kind, name = step["kind"], step.get("device")
    result = {"transport": kind}
    if name:
        result.update(device=name, label=config.get("inventory", {}).get(name, {}).get("name", name))
    if kind in ("ir", "volume"):
        device = config.get("ir", {}).get("devices", {}).get(name, {})
        result.update(transport="lirc", socket=config.get("ir", {}).get("socket", "/run/lirc/lircd"),
                      remote=device.get("remote"))
        macro = (device.get("commands", {}).get(step.get("command"), {}) if kind == "ir" else
                 next((p for p in device.get("volume_presets", []) if p.get("db") == step.get("db")), {}))
        destinations = [{"remote": pulse.get("remote", device.get("remote")), "key": pulse.get("key")}
                        for pulse in macro.get("sequence", [])]
        if destinations:
            result["pulses"] = destinations
            remotes = {pulse["remote"] for pulse in destinations}
            result["remote"] = next(iter(remotes)) if len(remotes) == 1 else None
    elif kind in ("kvm", "keyboard"):
        keyboard = config.get("kvm", {})
        result.update(transport="ch9328" if keyboard.get("transport") == "ch9328" else "usb_hid",
                      path=keyboard.get("device", "/dev/hidg0"))
        if kind == "kvm":
            result["port"] = step["port"]
    elif kind == "monitor":
        monitor = config.get("monitors", {}).get(name, {})
        result.update(transport="smartthings", **{key: monitor.get(key) for key in
                      ("device_id", "component", "capability", "command", "attribute")},
                      value=monitor.get("inputs", {}).get(step["input"]))
    return result


class EventLog:
    def __init__(self, directory):
        self.path = Path(directory) / "events.sqlite3"
        self.warned = False

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=.1)) as db:
            db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS listeners (path TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            with db:
                yield db

    def _write(self, operation):
        try:
            with self.connect() as db:
                operation(db)
            self.warned = False
        except (OSError, sqlite3.Error):
            # A failed diagnostic disk write must never interrupt hardware execution.
            if not self.warned:
                logging.getLogger(__name__).warning("Developer console log unavailable; check runtime directory permissions and free space.")
                self.warned = True

    def emit(self, category, message, *, level="info", **details):
        payload = json.dumps(dict(time=time.time(), category=category, level=level,
                                  message=message, trace=_trace.get(), details=details))
        def write(db):
            row = db.execute("INSERT INTO events(payload) VALUES (?)", (payload,))
            db.execute("DELETE FROM events WHERE id <= ?", (row.lastrowid - MAX_EVENTS,))
        self._write(write)

    def listener(self, path, state, *, live, **details):
        payload = json.dumps(dict(path=path, state=state, live=live, updated=time.time(), **details))
        def write(db):
            db.execute("INSERT OR REPLACE INTO listeners VALUES (?, ?)", (path, payload))
            db.execute("DELETE FROM listeners WHERE path NOT IN (SELECT path FROM listeners ORDER BY rowid DESC LIMIT 32)")
        self._write(write)

    def read(self, after=0):
        if not self.path.exists():
            return dict(events=[], cursor=0, gap=False, listeners=[])
        with self.connect() as db:
            db.execute("BEGIN")
            oldest, latest = db.execute("SELECT MIN(id), MAX(id) FROM events").fetchone()
            latest = latest or 0
            reset = after > latest
            rows = db.execute("SELECT id, payload FROM events WHERE id > ? ORDER BY id", (0 if reset else after,)).fetchall()
            listeners = [json.loads(row[0]) for row in db.execute("SELECT payload FROM listeners")]
        return dict(events=[dict(json.loads(payload), id=identifier) for identifier, payload in rows],
                    cursor=latest, gap=bool(after and (reset or (oldest and after < oldest - 1))), listeners=listeners)
