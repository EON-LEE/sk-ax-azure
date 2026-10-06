"""Demo state kept under AXK2_DATA; App Service keeps /home across restarts and deployments.

state.json holds:
- the wanted power state and the submitted jobs, keyed by the sha256 of their link token (the token itself
  is never stored);
- the active job, and the retry/race and cooldown bookkeeping;
- password overrides and cookie generations;
- recent events and GPU node-seconds per region;
- the eval wish and the known eval runs.
evals/<run>/ holds the records the eval runner uploads (records.jsonl) and its latest summary (summary.json).
Chat prompts and replies are never written anywhere.
"""
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

ROUNDS = 120_000
RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
EVENTS = 100
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
DEFAULT = {"desired": "off", "jobs": {}, "active": None, "active_since": None, "last_region": None,
           "mode": "race", "mode_since": None, "cooldown": {}, "restart": False, "passwords": {},
           "gen": {"demo": 0, "admin": 0}, "events": [], "cost": {"node_seconds": {}}, "nodes": {},
           "eval_wish": None, "eval_runs": {}, "eval_current": None}


def hash_password(password, rounds=ROUNDS, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), rounds).hex()
    return f"pbkdf2-sha256:{rounds}:{salt}:{digest}"


def check_password(password, stored):
    try:
        scheme, rounds, salt, digest = (stored or "").split(":")
        rounds = int(rounds)
    except ValueError:
        return False
    if scheme != "pbkdf2-sha256" or not 1_000 <= rounds <= 10_000_000 or not isinstance(password, str):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode(), rounds).hex()
    return hmac.compare_digest(candidate, digest)


def new_password():
    """Three groups of four lowercase letters and digits without look-alikes (about 59 bits)."""
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(4)) for _ in range(3))


def token_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "evals").mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.lock = threading.RLock()
        self.data = self.load()

    def load(self):
        data = copy.deepcopy(DEFAULT)
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return data
        except (OSError, ValueError):
            broken = self.path.with_name(f"state.broken-{int(time.time())}.json")
            try:
                os.replace(self.path, broken)
            except OSError:
                pass
            data["events"].append({"t": round(time.time(), 1), "kind": "store",
                                   "message": f"state.json was unreadable and was moved to {broken.name}"})
            return data
        if isinstance(saved, dict):
            data.update({key: value for key, value in saved.items() if key in DEFAULT})
        return data

    def save(self):
        with self.lock:
            text = json.dumps(self.data, ensure_ascii=False, indent=1)
            tmp = self.path.with_name(f".state.{os.getpid()}.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self.path)

    def event(self, kind, message, save=True):
        with self.lock:
            self.data["events"].append({"t": round(time.time(), 1), "kind": kind, "message": str(message)[:500]})
            del self.data["events"][:-EVENTS]
            if save:
                self.save()

    def secret(self):
        """A random signing key kept beside the state, for when AXK2_SESSION_SECRET is not set."""
        path = self.root / "session-secret"
        with self.lock:
            try:
                value = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                value = ""
            if len(value) < 32:
                value = secrets.token_urlsafe(48)
                path.write_text(value, encoding="utf-8")
            return value

    # -------------------------------------------------------------------------------------------- evals
    def run_dir(self, run):
        if not isinstance(run, str) or not RUN.fullmatch(run):
            raise ValueError("invalid run name")
        return self.root / "evals" / run

    def add_records(self, run, records):
        folder = self.run_dir(run)
        lines = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        with self.lock:
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / "records.jsonl", "a", encoding="utf-8") as handle:
                handle.write(lines)

    def records(self, run):
        path = self.run_dir(run) / "records.jsonl"
        records = []
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return records
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a torn last line after a crash
            if isinstance(record, dict):
                records.append(record)
        return records

    def put_summary(self, run, summary):
        folder = self.run_dir(run)
        with self.lock:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = folder / f".summary.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, folder / "summary.json")

    def summary(self, run):
        try:
            return json.loads((self.run_dir(run) / "summary.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None

    def stamp(self, run):
        """Changes whenever the run's records or summary change; keys the computed-summary cache."""
        folder = self.run_dir(run)
        out = []
        for name in ("records.jsonl", "summary.json"):
            try:
                info = (folder / name).stat()
                out += [info.st_size, info.st_mtime_ns]
            except FileNotFoundError:
                out += [0, 0]
        return tuple(out)

    def runs(self):
        folders = [p for p in (self.root / "evals").iterdir() if p.is_dir() and RUN.fullmatch(p.name)]
        return [p.name for p in sorted(folders, key=lambda p: p.stat().st_mtime)]
