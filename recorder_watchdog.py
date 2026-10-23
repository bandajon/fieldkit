#!/usr/bin/env python3
"""Independent Linux recording watchdog; run once per minute as the app user."""
import argparse
import fcntl
import math
import stat
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


def selfcheck():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = [10000.0]
        restarts, probes, events = [], [], []
        args = argparse.Namespace(root=root, site='site', stale=180, grace=300,
                                  min_interval=600, state=root / 'watchdog_state.json',
                                  unit='fieldkit', dry_run=False, record_dir=Path('recordings'),
                                  cam=['cam=<camera-address>', 'stopped=<other-address>'])
        desired = root / 'record_state.json'
        desired.write_text(json.dumps({'cam': {'until': None}}))
        folder = root / 'recordings/site/cam'
        folder.mkdir(parents=True)
        segment = folder / 'segment.mkv'
        segment.write_bytes(b'fixture')
        def stamp(age):
            os.utime(segment, (now[0] - age, now[0] - age))
        def check(reachable=True, uptime=1000):
            return run(args, clock=lambda: now[0], app_info=lambda unit: (42, uptime, 'identity'),
                       probe=lambda address: probes.append(address) or reachable,
                       restart=lambda unit, pid, identity: restarts.append(pid),
                       emit=events.append)
        stamp(1)
        assert check() == 0 and not events and not probes and not restarts, 'healthy'
        stamp(181)
        assert check() == 0 and restarts == [42], 'stale reachable restart'
        assert any('stale' in e for e in events)
        events.clear()
        now[0] += 60
        assert check() == 0 and restarts == [42], 'cooldown'
        assert any('suppressed' in e for e in events)
        events.clear()
        now[0] += 601
        assert check(reachable=False) == 0 and restarts == [42], 'unreachable'
        assert any('camera unreachable' in e for e in events)
        stamp(1)
        events.clear()
        check()
        assert any('recovered' in e for e in events), 'stale to fresh'
        desired.write_text('{}')
        stamp(1000)
        probes.clear()
        check()
        assert not probes and restarts == [42], 'operator stopped'
        desired.write_text(json.dumps({'cam': {'until': now[0] - 1}}))
        check()
        assert not probes and restarts == [42], 'expired'
        desired.write_text(json.dumps({'cam': {'until': now[0] + 5000}}))
        events.clear()
        check(uptime=10)
        assert not probes and restarts == [42] and not events, 'startup grace'
        segment.unlink()
        folder.rmdir()
        check()
        assert restarts == [42, 42], 'missing directory'
        now[0] += 601
        args.dry_run = True
        check()
        assert restarts == [42, 42], 'dry run'
        args.dry_run = False
        check()
        assert restarts == [42, 42, 42], 'dry run must not consume cooldown'
        args.state.unlink()
        restarts.clear()
        (root / 'recordings/site/stopped').mkdir(parents=True)
        desired.write_text(json.dumps({'cam': {'until': None}, 'stopped': {'until': None}}))
        events.clear()
        check()
        assert restarts == [42], 'two stale cameras must cause only one restart'
        assert any('suppressed' in e for e in events)
        with args.state.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            events.clear()
            check()
            assert not events and restarts == [42], 'overlapping cron skipped'
        folder.mkdir()
        stamp_path = folder / 'new.mkv'
        stamp_path.write_bytes(b'fixture')
        os.utime(stamp_path, (now[0], now[0]))
        segment.write_bytes(b'fixture')
        stamp(1000)
        (folder / 'directory.mkv').mkdir()
        marker = folder / 'old.mkv.uploaded'
        marker.touch()
        desired.write_text(json.dumps({'cam': {'until': None}}))
        events.clear()
        check()
        assert any('recovered' in e for e in events), 'newest segment wins'
        stamp_path.unlink()
        now[0] += 601
        events.clear()
        check()
        assert restarts == [42, 42], 'uploaded marker is not a segment'
        desired.write_text(json.dumps({'unconfigured': {'until': None}}))
        probes.clear()
        events.clear()
        now[0] += 601
        for _ in range(2):
            assert run(args, clock=lambda: now[0],
                       app_info=lambda unit: (_ for _ in ()).throw(AssertionError('app lookup')),
                       probe=lambda address: probes.append(address),
                       restart=lambda unit, pid, identity: restarts.append(pid),
                       emit=events.append) == 0
        assert not probes and restarts == [42, 42], 'missing address never restarts'
        assert len(events) == 2 and sum('no address configured' in e for e in events) == 1, events
        assert any('no longer desired' in e for e in events), 'previous stale session cleared'
        for bad in ('../escape', '/absolute', 'line\nbreak'):
            try:
                component(bad)
            except ValueError:
                pass
            else:
                raise AssertionError('unsafe path accepted')
    print('recorder_watchdog selfcheck: OK')


def component(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('invalid directory component')
    return value


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def desired_sessions(root, now):
    saved = read_json(root / 'record_state.json', {})
    if not isinstance(saved, dict):
        raise ValueError('invalid recording state')
    desired = []
    for name, entry in saved.items():
        component(name)
        if not isinstance(entry, dict) or 'until' not in entry:
            raise ValueError('invalid session')
        until = entry['until']
        if until is not None and (type(until) not in (int, float) or not math.isfinite(until)):
            raise ValueError('invalid deadline')
        if until is None or until > now:
            desired.append(name)
    return desired


def process_identity(pid):
    # comm may contain spaces and parentheses: fields begin after its last ')'.
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return fields[19]


def main_pid(unit):
    result = subprocess.run(['systemctl', 'show', unit, '-p', 'MainPID', '--value'],
                            capture_output=True, text=True, check=True, timeout=5)
    pid = int(result.stdout.strip())
    if pid <= 1:
        raise ValueError('no app main process')
    return pid


def app_process(unit):
    pid = main_pid(unit)
    identity = process_identity(pid)
    uptime = float(Path('/proc/uptime').read_text().split()[0])
    return pid, uptime - int(identity) / os.sysconf('SC_CLK_TCK'), identity


def reachable(address):
    try:
        with socket.create_connection((address, 554), timeout=3):
            return True
    except OSError:
        return False


def interrupt(unit, pid, identity):
    # Recheck both unit membership and start time to avoid signalling a reused PID.
    if main_pid(unit) != pid or process_identity(pid) != identity:
        raise RuntimeError('app main process changed')
    os.kill(pid, signal.SIGINT)


def run(args, *, clock=time.time, app_info=app_process, probe=reachable,
        restart=interrupt, emit=None):
    emit = emit or (lambda message: print(message, flush=True))
    def event(message):
        stamp = datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec='seconds')
        emit(f'{stamp} {message}')
    with args.state.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        cameras = {}
        for override in args.cam:
            name, separator, address = override.partition('=')
            if not separator or not address:
                raise ValueError('invalid camera address')
            cameras[component(name)] = address
        site = component(args.site)
        out = args.root / args.record_dir / site
        now = clock()
        desired = desired_sessions(args.root, now)
        state = read_json(args.state, {'last_restart': None, 'stale': []})
        last = state['last_restart']
        if last is not None and (type(last) not in (int, float) or not math.isfinite(last)):
            raise ValueError('invalid watchdog restart time')
        previous = state['stale']
        if not isinstance(previous, list) or any(not isinstance(n, str) for n in previous):
            raise ValueError('invalid watchdog camera state')
        stale = {component(name) for name in previous}
        missing = {name for name in desired if name not in cameras}
        previous_missing = state.get('missing', [])
        if not isinstance(previous_missing, list) or any(not isinstance(n, str) for n in previous_missing):
            raise ValueError('invalid watchdog missing camera state')
        for name in sorted(missing - set(previous_missing)):
            event(f'{name}: no address configured')
        desired = [name for name in desired if name not in missing]
        def save():
            state['missing'] = sorted(missing)
            state['stale'] = sorted(stale)
            temporary = args.state.with_suffix('.tmp')
            temporary.write_text(json.dumps(state) + '\n')
            os.replace(temporary, args.state)
        for name in stale - set(desired):
            event(f'{name}: no longer desired')
        stale.intersection_update(desired)
        process = app_info(args.unit) if desired else None
        attempted = False
        for name in desired:
            newest = None
            try:
                segments = list((out / name).iterdir())
            except FileNotFoundError:
                segments = []
            for segment in segments:
                if not segment.name.endswith('.mkv'):
                    continue
                try:
                    info = segment.stat()
                except FileNotFoundError:
                    continue  # Offload can remove a finished segment during this scan.
                if stat.S_ISREG(info.st_mode):
                    newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
            if newest is not None and now - newest <= args.stale:
                if name in stale:
                    event(f'{name}: recovered')
                    stale.remove(name)
                continue
            pid, uptime, identity = process
            if uptime <= args.grace:
                continue
            if name not in stale:
                event(f'{name}: stale')
                stale.add(name)
            if not probe(cameras[name]):
                event(f'{name}: camera unreachable')
                continue
            if attempted or (last is not None and now - last < args.min_interval):
                event(f'{name}: suppressed (restart cooldown)')
                continue
            # Honour Stop or timer expiry that happened during a slow probe.
            if name not in desired_sessions(args.root, clock()):
                continue
            if args.dry_run:
                event(f'{name}: dry-run would restart app (SIGINT)')
                continue
            # Reserve cooldown before signalling, including if cron is interrupted.
            state['last_restart'] = last = clock()
            save()
            restart(args.unit, pid, identity)
            attempted = True
            event(f'{name}: restart app (SIGINT)')
        if not args.dry_run and (not args.state.exists() or state['stale'] != sorted(stale)
                                 or previous_missing != sorted(missing)):
            save()
    return 0


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be a positive finite number')
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--site', default='site1')
    parser.add_argument('--record-dir', type=Path, default=Path('recordings'),
                        help='recording root (default: <root>/recordings)')
    parser.add_argument('--stale', type=positive, default=180)
    parser.add_argument('--grace', type=positive, default=300)
    parser.add_argument('--min-interval', type=positive, default=600)
    parser.add_argument('--state', type=Path, help='JSON state; sibling .lock file')
    parser.add_argument('--unit', default='fieldkit')
    parser.add_argument('--cam', action='append', default=[], metavar='NAME=IP',
                        help='required address for each monitored camera (repeatable)')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--selfcheck', action='store_true')
    args = parser.parse_args()
    if args.selfcheck:
        selfcheck()
        return 0
    args.root = args.root.resolve()
    args.state = args.root / (args.state or 'watchdog_state.json')
    try:
        return run(args)
    except Exception as error:
        # Exception text can contain addresses; log only the type.
        message = (datetime.now(timezone.utc).isoformat(timespec='seconds')
                   + f' watchdog error ({type(error).__name__}); check arguments/state/service access')
        print(message, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
