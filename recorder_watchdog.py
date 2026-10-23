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
from urllib.request import urlopen


def selfcheck():
    from unittest.mock import patch

    failures = []
    def case(label, test):
        try:
            test()
        except Exception as error:
            failures.append(label)
            print(f'FAIL {label}: {type(error).__name__}: {error}')

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = [10000.0]
        restarts, probes, events = [], [], []
        args = argparse.Namespace(root=root, site='site', stale=180, grace=300,
                                  min_interval=600, state=root / 'watchdog_state.json',
                                  unit='fieldkit', dry_run=False, record_dir=Path('recordings'),
                                  port=8080, cam=['cam=<camera-address>', 'stopped=<other-address>'])
        desired = root / 'record_state.json'
        folder = root / 'recordings/site/cam'
        folder.mkdir(parents=True)
        segment = folder / 'segment.mkv'
        status = {'cameras': {'cam': {'state': 'RECORDING', 'restarts': 0, 'bytes': 7, 'log': []},
                              'stopped': {'state': 'RECORDING', 'restarts': 0, 'bytes': 7, 'log': []}}}
        def stamp(age):
            segment.write_bytes(b'fixture')
            os.utime(segment, (now[0] - age, now[0] - age))
        def reset():
            now[0] = 10000.0
            restarts.clear(); probes.clear(); events.clear()
            args.state.unlink(missing_ok=True)
            args.dry_run = False
            desired.write_text(json.dumps({'cam': {'until': None}}))
            folder.mkdir(exist_ok=True)
            stamp(1000)
            status['cameras']['cam'] = {'state': 'RECORDING', 'restarts': 0, 'bytes': 7, 'log': []}
        def check(reachable=True, uptime=1000, probe_action=None, timeout=False):
            with patch(__name__ + '.read_status',
                       side_effect=TimeoutError if timeout else None, return_value=status):
                result = run(args, clock=lambda: now[0],
                           app_info=lambda unit: (42, uptime, 'identity'),
                           probe=lambda address: probes.append(address) or (
                               probe_action() if probe_action else reachable),
                           restart=lambda unit, pid, identity: restarts.append(pid), emit=events.append)
                assert result == 0, 'watchdog run must succeed'
        def baseline():
            reset(); check(); events.clear(); probes.clear()
            assert not restarts, 'first observation must establish a fingerprint'
        def healthy():
            reset(); stamp(1); check()
            assert not events and not probes and not restarts
            args.dry_run = True; check()
            assert not events and not restarts, 'healthy dry-run must be silent'
        def active():
            for field, value in [('state', 'RECONNECTING'), ('restarts', 1), ('bytes', 14)]:
                baseline()
                status['cameras']['cam'][field] = value
                check()
                assert not restarts, field
                saved = json.loads(args.state.read_text())
                expected = ('configuration error: path mismatch: check --record-dir/--site'
                            if field == 'bytes' else 'supervisor active, not restarting')
                assert saved['events']['cam'] == expected, field
                if field != 'bytes':
                    assert not events, 'unchanged supervisor-active decision must be silent'
        def dead():
            baseline()
            status['cameras']['cam']['log'] = ['ffmpeg progress']
            check()
            assert restarts == [42], 'unchanged RECORDING with stale video must restart'
        def path_mismatch():
            baseline()
            for size in (14, 21):
                status['cameras']['cam']['bytes'] = size
                check()
            assert not restarts, 'growing bytes outside watched path must not restart'
            assert len(events) == 1 and 'path mismatch: check --record-dir/--site' in events[0]
            stamp(1); check()
            assert any('recovered' in e for e in events)
        def stopped_unit():
            reset()
            with patch(__name__ + '.subprocess.run') as systemctl, \
                 patch(__name__ + '.process_identity') as identity:
                systemctl.return_value.stdout = '0\n'
                for _ in range(2):
                    assert run(args, clock=lambda: now[0], emit=events.append) == 1
                identity.assert_not_called()
            assert len(events) == 1 and 'no app main process' in events[0], events
            assert not restarts
            check(); events.clear()
            with patch(__name__ + '.subprocess.run') as systemctl:
                systemctl.return_value.stdout = '0\n'
                assert run(args, clock=lambda: now[0], emit=events.append) == 1
            assert len(events) == 1, 'a later stop must log again'
        def api_timeout():
            reset(); check(timeout=True)
            assert restarts == [42]
            with patch(__name__ + '.urlopen', side_effect=TimeoutError) as request:
                try:
                    read_status(8123)
                except TimeoutError:
                    pass
                else:
                    raise AssertionError('API timeout was swallowed')
                request.assert_called_once_with(
                    'http://127.0.0.1:8123/api/record/status', timeout=5)
        def backoff():
            baseline(); check()
            assert restarts == [42]
            for delay in (600, 1200, 2400, 4800, 9600, 14400, 14400):
                now[0] += delay - 1; events.clear(); check()
                assert any('backing off until ' in e for e in events)
                count = len(restarts); events.clear(); check()
                assert not events, 'identical backoff must be silent'
                now[0] += 1; check()
                assert len(restarts) == count + 1, delay
            stamp(1); events.clear(); check()
            assert any('recovered' in e for e in events)
            stamp(1000); now[0] += 601; check()
            count = len(restarts); now[0] += 600; check()
            assert len(restarts) == count + 1, 'recovery must reset cooldown to 600'
        def shared_backoff():
            baseline()
            (root / 'recordings/site/stopped').mkdir(exist_ok=True)
            desired.write_text(json.dumps({'cam': {'until': None}, 'stopped': {'until': None}}))
            times = []
            for elapsed in range(0, 4201, 60):
                now[0] = 10000.0 + elapsed
                count = len(restarts)
                check()
                if len(restarts) > count:
                    times.append(elapsed)
            assert times == [0, 600, 1800, 4200], times
            stamp(1); check()
            saved = json.loads(args.state.read_text())
            assert saved['restart_delay'] == 4800, 'one recovered camera must not reset app cooldown'
            now[0] += 600; stamp(1); check()
            assert len(restarts) == 4, 'remaining stale camera must retain app cooldown'
            other = root / 'recordings/site/stopped/live.mkv'
            try:
                other.write_bytes(b'fixture')
                os.utime(other, (now[0], now[0]))
                check()
                saved = json.loads(args.state.read_text())
                assert saved['restart_delay'] == 0 and saved['last_restart'] is None
                assert 'backoff' not in saved, 'per-camera backoff must not persist'
            finally:
                other.unlink()
        def advancing_probe_clock():
            baseline()
            (root / 'recordings/site/stopped').mkdir(exist_ok=True)
            desired.write_text(json.dumps({'cam': {'until': None}, 'stopped': {'until': None}}))
            check(reachable=False)
            def advance():
                now[0] += 1
                return True
            for _ in range(2):
                count = len(restarts)
                check(probe_action=advance)
                assert len(restarts) == count + 1, 'two stale cameras must signal once per run'
                now[0] += 600
        def repeated_restart_logging():
            baseline(); check()
            events.clear(); now[0] += 600; check()
            assert restarts == [42, 42]
            assert len(events) == 1 and 'restart app (SIGINT)' in events[0], events
        def mixed_timestamps():
            baseline()
            future = folder / 'future.mkv'
            try:
                future.write_bytes(b'old footage')
                os.utime(future, (now[0] + 10000, now[0] + 10000))
                for elapsed in (0, 60):
                    now[0] += elapsed
                    stamp(1); check()
                    assert not restarts, 'future old segment must not hide live footage'
                assert any('recovered' in e for e in events)
            finally:
                future.unlink()
        def missing_dir():
            reset(); segment.unlink(); folder.rmdir()
            check(); check()
            assert not restarts and len(events) == 1 and 'configuration error' in events[0]
            folder.mkdir(); stamp(1000); check()
            segment.unlink(); folder.rmdir(); events.clear(); check()
            assert sum('configuration error' in e for e in events) == 1
        def future_times():
            baseline(); stamp(-181); check()
            assert restarts == [42], 'far future mtime is stale-unknown'
            baseline(); stamp(-180); check()
            assert not restarts and any('recovered' in e for e in events)
            baseline(); check(); count = len(restarts)
            now[0] -= 100; check()
            assert len(restarts) == count + 1, 'future restart time must expire'
        def corrupt_state():
            for content in ('{bad', '[]', '{}', '{"last_restart": "bad", "stale": []}',
                            '{"last_restart": null, "stale": [], "events": []}',
                            '{"last_restart": null, "stale": [], "restart_delay": {}}'):
                reset(); stamp(1); args.state.write_text(content)
                check(); check()
                assert len(events) == 1 and 'corrupt watchdog state; reset' in events[0], events
        def dedup():
            baseline(); events.clear(); check(reachable=False); check(reachable=False)
            assert len(events) == 1 and 'camera unreachable' in events[0]
            (root / 'recordings/site/stopped').mkdir(exist_ok=True)
            desired.write_text(json.dumps({'cam': {'until': None}, 'stopped': {'until': None}}))
            check(reachable=False)
            check(); events.clear(); check(); check()
            assert restarts == [42], 'two stale cameras must cause only one restart'
            assert len(events) == 2 and all('backing off until ' in e for e in events), events
            events.clear(); check()
            assert not events, 'identical shared cooldown decisions must be silent'
        def stop_during_probe():
            baseline()
            def stop():
                desired.write_text('{}'); return True
            check(probe_action=stop)
            assert not restarts, 'operator Stop during probe must win'
            baseline()
            desired.write_text(json.dumps({'cam': {'until': now[0] + 1}}))
            def expire():
                now[0] += 2; return True
            check(probe_action=expire)
            assert not restarts, 'timer expiry during probe must win'
        def pid_identity():
            for current_pid, identity in ((43, 'identity'), (42, 'changed')):
                with patch(__name__ + '.main_pid', return_value=current_pid), \
                     patch(__name__ + '.process_identity', return_value=identity), \
                     patch(__name__ + '.os.kill') as kill:
                    try:
                        interrupt('fieldkit', 42, 'identity')
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('changed process accepted')
                    kill.assert_not_called()
            with patch(__name__ + '.main_pid', return_value=42), \
                 patch(__name__ + '.process_identity', return_value='identity'), \
                 patch(__name__ + '.os.kill') as kill:
                interrupt('fieldkit', 42, 'identity')
                kill.assert_called_once_with(42, signal.SIGINT)
        def existing_guards():
            baseline(); saved = args.state.read_bytes()
            args.dry_run = True; check()
            assert args.state.read_bytes() == saved, 'dry-run must not change state'
            assert not restarts and any('dry-run would restart' in e for e in events)
            args.dry_run = False; check()
            assert restarts == [42], 'dry-run must not consume cooldown'
            reset(); check(uptime=10)
            assert not events and not probes and not restarts
            for entry in ({}, {'cam': {'until': now[0] - 1}}):
                desired.write_text(json.dumps(entry)); check()
                assert not probes and not restarts
            reset(); check()
            with args.state.with_suffix('.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                events.clear(); check()
                assert not events and not restarts
            desired.write_text(json.dumps({'unconfigured': {'until': None}}))
            events.clear(); probes.clear()
            for _ in range(2):
                assert run(args, clock=lambda: now[0],
                           app_info=lambda unit: (_ for _ in ()).throw(AssertionError('app lookup')),
                           probe=lambda address: probes.append(address),
                           restart=lambda *a: restarts.append(42), emit=events.append) == 0
            assert not restarts and not probes
            assert len(events) == 2 and sum('no address configured' in e for e in events) == 1
            assert any('no longer desired' in e for e in events)
        def segments():
            baseline()
            fresh = folder / 'new.mkv'; fresh.write_bytes(b'fixture')
            os.utime(fresh, (now[0], now[0]))
            directory = folder / 'directory.mkv'; directory.mkdir()
            marker = folder / 'old.mkv.uploaded'; marker.touch()
            os.utime(marker, (now[0], now[0]))
            check()
            assert any('recovered' in e for e in events) and not restarts
            fresh.unlink(); check()
            assert restarts == [42], 'uploaded marker/directory must not count as segment'
            directory.rmdir(); marker.unlink()
        for label, test in [('healthy and dry-run', healthy), ('supervisor activity', active),
                            ('dead supervisor', dead), ('path mismatch', path_mismatch),
                            ('stopped unit', stopped_unit), ('API timeout', api_timeout),
                            ('exponential backoff and recovery', backoff),
                            ('shared app backoff', shared_backoff),
                            ('advancing probe clock', advancing_probe_clock),
                            ('repeated restart logging', repeated_restart_logging),
                            ('mixed future and live segments', mixed_timestamps),
                            ('missing directory', missing_dir), ('future timestamps', future_times),
                            ('corrupt state', corrupt_state), ('event deduplication', dedup),
                            ('desired-state recheck', stop_during_probe), ('PID identity recheck', pid_identity),
                            ('existing guards', existing_guards), ('segment selection', segments)]:
            case(label, test)
        for bad in ('../escape', '/absolute', 'line\nbreak'):
            try:
                component(bad)
            except ValueError:
                pass
            else:
                raise AssertionError('unsafe path accepted')
    assert not failures, f'{len(failures)} selfcheck cases failed: {failures}'
    print('recorder_watchdog selfcheck: OK (19 cases)')


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
    if pid < 0 or pid == 1:
        raise ValueError('no app main process')
    return pid


def app_process(unit):
    pid = main_pid(unit)
    if pid == 0:
        return 0, 0, None
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


def read_status(port):
    with urlopen(f'http://127.0.0.1:{port}/api/record/status', timeout=5) as response:
        return json.load(response)


def watchdog_state(path, event):
    defaults = {'last_restart': None, 'stale': [], 'missing': [],
                'fingerprints': {}, 'restart_delay': 0, 'events': {}}
    def number(value):
        return type(value) in (int, float) and math.isfinite(value)
    try:
        saved = read_json(path, defaults)
        if not isinstance(saved, dict) or not {'last_restart', 'stale'} <= saved.keys():
            raise ValueError('invalid watchdog state')
        state = {**defaults, **{key: saved[key] for key in defaults if key in saved}}
        if state['last_restart'] is not None and not number(state['last_restart']):
            raise ValueError('invalid watchdog restart time')
        if not number(state['restart_delay']) or state['restart_delay'] < 0:
            raise ValueError('invalid watchdog restart delay')
        for key in ('stale', 'missing'):
            if not isinstance(state[key], list):
                raise ValueError('invalid watchdog camera list')
            for name in state[key]:
                component(name)
        for key in ('fingerprints', 'events'):
            if not isinstance(state[key], dict):
                raise ValueError('invalid watchdog camera map')
            for name, value in state[key].items():
                component(name)
                if not isinstance(value, str):
                    raise ValueError('invalid watchdog fingerprint/event')
        return state
    except (ValueError, TypeError):
        event('corrupt watchdog state; reset to defaults')
        return defaults


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
        out = args.root / args.record_dir / component(args.site)
        now = clock()
        desired = desired_sessions(args.root, now)
        state = watchdog_state(args.state, event)
        stale = set(state['stale'])
        def transition(name, message):
            if state['events'].get(name) != message:
                event(f'{name}: {message}')
                state['events'][name] = message
        missing = {name for name in desired if name not in cameras}
        for name in sorted(missing - set(state['missing'])):
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
        for key in ('events', 'fingerprints'):
            state[key] = {name: value for name, value in state[key].items() if name in desired}
        process = app_info(args.unit) if desired else None
        if process and process[0] == 0:
            for name in desired:
                transition(name, 'no app main process (unit stopped)')
            if not args.dry_run:
                save()
            return 1
        statuses = None
        if process and process[1] > args.grace:
            try:
                statuses = read_status(args.port)['cameras']
                if not isinstance(statuses, dict):
                    raise ValueError('invalid status')
            except (OSError, ValueError, KeyError, TypeError):
                statuses = None  # An unresponsive app cannot supervise its recorders.
        for name in desired:
            camera = statuses.get(name, {}) if statuses is not None else {}
            fingerprint = None
            if (isinstance(camera, dict) and type(camera.get('restarts')) is int
                    and type(camera.get('bytes')) is int and camera['bytes'] >= 0):
                fingerprint = json.dumps([camera['restarts'], camera['bytes']])
            previous = state['fingerprints'].get(name)
            try:
                previous_values = json.loads(previous) if previous else None
            except ValueError:
                previous_values = None  # Older state stored a hash instead of JSON.
            if fingerprint is not None:
                state['fingerprints'][name] = fingerprint
            fresh = False
            try:
                segments = list((out / name).iterdir())
            except FileNotFoundError:
                transition(name, 'configuration error: camera directory missing')
                continue
            for segment in segments:
                if not segment.name.endswith('.mkv'):
                    continue
                try:
                    info = segment.stat()
                except FileNotFoundError:
                    continue  # Offload can remove a finished segment during this scan.
                if stat.S_ISREG(info.st_mode) and abs(now - info.st_mtime) <= args.stale:
                    fresh = True
                    break
            if fresh:
                if name in stale:
                    event(f'{name}: recovered')
                    stale.remove(name)
                state['events'].pop(name, None)
                continue
            pid, uptime, identity = process
            if uptime <= args.grace:
                continue
            if name not in stale:
                event(f'{name}: stale')
                stale.add(name)
            if (fingerprint is not None and isinstance(previous_values, list)
                    and len(previous_values) == 2 and type(previous_values[1]) is int
                    and camera['bytes'] > previous_values[1]):
                transition(name, 'configuration error: path mismatch: check --record-dir/--site')
                continue
            if not probe(cameras[name]):
                transition(name, 'camera unreachable')
                continue
            if statuses is not None:
                if (isinstance(camera, dict) and camera.get('state') == 'RECONNECTING'
                        or fingerprint is not None and fingerprint != previous):
                    transition(name, 'supervisor active, not restarting')
                    continue
                if (not isinstance(camera, dict) or camera.get('state') != 'RECORDING'
                        or fingerprint is None):
                    transition(name, 'supervisor status unknown, not restarting')
                    continue
            last = state['last_restart']
            delay = state['restart_delay'] or args.min_interval
            if last is not None and 0 <= now - last < delay:
                until = datetime.fromtimestamp(last + delay, timezone.utc).isoformat(timespec='seconds')
                transition(name, f'backing off until {until}')
                continue
            # Honour Stop or timer expiry that happened during a slow probe/API call.
            if name not in desired_sessions(args.root, clock()):
                continue
            if args.dry_run:
                transition(name, 'dry-run would restart app (SIGINT)')
                continue
            # Reserve cooldown before signalling, including if cron is interrupted.
            state['last_restart'] = clock()
            state['restart_delay'] = min(
                14400, state['restart_delay'] * 2 if state['restart_delay'] else args.min_interval)
            save()
            restart(args.unit, pid, identity)
            event(f'{name}: restart app (SIGINT)')
            state['events'][name] = 'restart app (SIGINT)'
            break
        if not stale:
            state['restart_delay'] = 0
            state['last_restart'] = None
        if not args.dry_run:
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
    parser.add_argument('--port', type=int, default=8080, help='local FieldKit HTTP port')
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
