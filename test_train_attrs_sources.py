#!/usr/bin/env python3
"""Attribute source regression checks; temporary datasets, no model training."""
import ast
import contextlib
import inspect
import io
import json
import runpy
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image
import yaml

import classifier_crops
import dataset_sync
import train_attrs as ta

BOX = [.5, .5, .4, .4]


@contextlib.contextmanager
def dataset():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for kind in ('images', 'labels', 'attrs'):
            (root / 'approved' / kind).mkdir(parents=True)
        (root / 'classes.txt').write_text('truck\na-small\n')
        (root / 'attributes.yaml').write_text(yaml.safe_dump(
            {'type': ['van'], 'colour': ['white', 'blue'], 'make': ['Toyota', 'Ford']}, sort_keys=False))
        with patch.object(ta, 'DATASET', root), patch.object(ta, 'APPROVED', root / 'approved'), \
             patch.object(ta, 'BASELINE', False), patch.object(ta, 'EXTERNAL', False):
            yield root, ta.vocab()


def frame(root, sid, attrs=None, rows=1):
    Image.new('RGB', (40, 30), (20, 40, 60)).save(root / 'approved/images' / f'{sid}.jpg')
    (root / 'approved/labels' / f'{sid}.txt').write_text('0 .5 .5 .4 .4\n' * rows)
    if attrs is not None:
        (root / 'approved/attrs' / f'{sid}.json').write_text(json.dumps(attrs))


def ai(root, sid, attrs, box=0, bbox=BOX):
    directory = root / 'ai-attrs'
    directory.mkdir(exist_ok=True)
    run = directory / f'20261010T{len(list(directory.glob("*.jsonl"))):06d}Z.jsonl'
    tmp = run.with_suffix('.tmp')
    tmp.write_text(json.dumps({'sid': sid, 'box': box, 'bbox': bbox, 'attrs': attrs,
                               'models': ['haiku', 'sonnet'], 'at': '2026-10-10T00:00:00Z'}) + '\n')
    tmp.rename(run)


def human_wins():
    with dataset() as (root, heads):
        frame(root, 'human', {'0': {'colour': 'white'}})
        ai(root, 'human', {'colour': 'blue', 'make': 'Ford'})
        assert ta.build(heads)[1] == [[-1, 0, 1]], 'human wins; AI fills missing make'


def stale_bbox():
    with dataset() as (root, heads):
        for i in range(4):
            sid = f'stale-{i}'
            frame(root, sid, {'0': {'type': 'van'}})
            bbox = BOX.copy(); bbox[i] += .0002
            ai(root, sid, {'make': 'Ford'}, bbox=bbox)
        frame(root, 'tolerated')
        ai(root, 'tolerated', {'make': 'Toyota'}, bbox=[.50005, .5, .4, .4])
        _, targets, stems, _ = ta.build(heads)
        assert targets[stems.index('tolerated')] == [-1, -1, 0], 'within tolerance accepted'
        assert all(targets[stems.index(f'stale-{i}')] == [0, -1, -1] for i in range(4)), 'every bbox field checked'


def ai_only_and_last_line():
    with dataset() as (root, heads):
        frame(root, 'only', rows=2)
        ai(root, 'only', {'make': 'Toyota'})
        ai(root, 'only', {'make': 'Ford'})
        ai(root, 'only', {'colour': 'blue'}, box=1)
        assert ta.build(heads)[1] == [[-1, -1, 1], [-1, 1, -1]], 'AI-only boxes; last line wins'
        ai(root, 'only', {'make': 'Toyota'}, bbox=[.6, .5, .4, .4])
        assert ta.build(heads)[1] == [[-1, 1, -1]], 'stale last line must not revive an older entry'


def archive_and_reference():
    with dataset() as (root, heads):
        frame(root, 'archived', {'0': {'colour': 'white'}}, rows=2)
        classifier_crops.export_sample(root, 'archived')
        for kind, ext in [('images', '.jpg'), ('labels', '.txt'), ('attrs', '.json')]:
            (root / 'approved' / kind / f'archived{ext}').unlink()
        ai(root, 'archived', {'make': 'Ford', 'colour': 'blue'})
        ai(root, 'archived', {'make': 'Toyota'}, box=1)
        frame(root, 'reference')
        ai(root, 'reference', {'make': 'Ford'})
        (root / 'reference.txt').write_text('reference\n')
        assert ta.build(heads)[1:3] == ([[-1, 0, 1], [-1, -1, 0]], ['archived', 'archived']), 'archive AI fill and AI-only; exclude reference'
        ai(root, 'archived', {'make': 'Ford'}, bbox=[.6, .5, .4, .4])
        assert ta.build(heads)[1] == [[-1, 0, -1], [-1, -1, 0]], 'archive also rejects stale bbox'
        (root / 'reference.txt').write_text('reference\narchived\n')
        assert not ta.build(heads)[0], 'reference archive excluded'


def external():
    with dataset() as (root, heads), patch.object(ta, "EXTERNAL", True):
        src = root / 'external/make/cars'; src.mkdir(parents=True)
        image = Image.new('RGB', (9, 5)); image.putdata([(x * 20, y * 40, 30) for y in range(5) for x in range(9)])
        image.save(src / 'crop.png')
        (src / 'manifest.jsonl').write_text('\n'.join(json.dumps({'image': 'crop.png', 'make': make}) for make in ['Toyota', 'unknown']) + '\n')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            crops, targets, stems, classes = ta.build(heads)
        assert targets == [[-1, -1, 0]] and classes == [1], 'make-only and a-small conditioning; skip unknown make'
        assert stems == ['external-make-cars-0']
        assert ta.SOURCES['external'] == {'cars': 1}
        assert crops[0].tobytes() == image.resize((ta.INPUT, ta.INPUT)).tobytes(), 'resize whole external crop without padding'
        assert '1' in out.getvalue() and 'skip' in out.getvalue(), 'unknown make skip counted'
        with patch.object(ta, 'is_val', return_value=True):
            assert ta.report_counts(heads, targets, stems) == [], 'external always train even if hash says val'
            assert ta.is_validation(stems[0]) is False and ta.is_validation('road') is True
        with patch.object(ta, 'is_val', side_effect=lambda sid: sid == 'road-val'), \
             patch.object(ta, 'AI_VAL_TARGETS', [[-1] * len(heads)] * 110):
            ta.check(heads, crops * 110, targets * 110,
                     ['road-val'] + ['road-train'] * 9 + stems * 100)
        for sid in ['road', 'katuba-20261010-120000', 'retained']:
            assert ta.is_validation(sid) == ta.is_val(sid), 'non-external split unchanged'
        with patch.object(ta, 'EXTERNAL', False):
            assert not ta.build(heads)[0], 'default drops external sources'
        with patch.object(sys, 'argv', ['train_attrs.py', 'external']):
            assert runpy.run_path(str(Path(ta.__file__)))['EXTERNAL'], 'CLI opts in to external'


def counts_and_sync():
    with dataset() as (root, heads):
        frame(root, 'mixed', {'0': {'type': 'van'}})
        ai(root, 'mixed', {'make': 'Ford'})
        ta.build(heads)
        assert ta.SOURCES == {'human': 1, 'ai': 1, 'archive': 0, 'external': {}}, 'contributing sample counts'
        ta.build({'type': ['van']})
        assert ta.SOURCES['ai'] == 0, 'count only AI heads that fed training'
    assert 'ai-attrs' in dataset_sync.PUSH and 'ai-attrs' in dataset_sync.LEDGERS
    assert not dataset_sync.always('ai-attrs/20261010T000000Z.jsonl')



def run_file_order():
    with dataset() as (root, heads), patch.object(ta, 'is_val', return_value=False):
        frame(root, 'ordered')
        directory = root / 'ai-attrs'; directory.mkdir()
        def row(make, at):
            return json.dumps({'sid': 'ordered', 'box': 0, 'bbox': BOX,
                               'attrs': {'make': make}, 'models': ['haiku', 'sonnet'], 'at': at}) + '\n'
        # Create newest first; timestamps deliberately disagree with filename order.
        (directory / '20261010T020000Z.jsonl').write_text(
            row('Toyota', 'z') + row('Ford', 'a'))
        (directory / '20261010T010000Z.jsonl').write_text(row('Toyota', 'z'))
        (directory / '20261010T030000Z.tmp').write_text(row('Toyota', 'z'))
        assert ta.build(heads)[1] == [[-1, -1, 1]], 'filename order then line order; ignore temp files'


def training_mask_and_report():
    external_stem = next(f'external-make-cars-{i}' for i in range(1000)
                         if ta.is_val(f'external-make-cars-{i}'))
    stems = [external_stem, 'road']
    expected = [False, ta.is_val('road')]
    assert ta.validation_mask(stems) == expected, 'external hash-val crop belongs to train'
    # Evaluate the actual train() expressions with a tensor stand-in: no torch required.
    tree = ast.parse(inspect.getsource(ta.train))
    mask = next(node.value for node in ast.walk(tree) if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'val' for t in node.targets))
    class Tensor:
        tensor = staticmethod(lambda values: values)
    namespace = {**vars(ta), 'torch': Tensor, 'stems': stems}
    assert eval(compile(ast.Expression(mask), '<train mask>', 'eval'), namespace) == expected, \
        'actual train mask must exclude external validation crops'
    with tempfile.TemporaryDirectory() as td:
        run = Path(td)
        sources = {'human': 2, 'ai': 1, 'archive': 1, 'external': {'cars': 3}}
        with patch.object(ta, 'SOURCES', sources):
            floors, withheld = {'colour': .8}, ['make']
            calibration = {'colour': {'coverage': .8, 'precision': 1.}}
            values = {'colour': ['white'], 'make': []}
            report = ta.report_dict(run.name, {'make': .75, 'type': None}, 4,
                                    {'make': 4}, {}, floors, withheld, calibration, values)
            assert report['floors'] == floors and report['withheld'] == withheld
            assert report['calibration'] == calibration and report['values'] == values
            assert report['sources'] == sources, 'report preserves source counts'
            assert report['mean_acc'] == .75 and report['val_crops'] == 4
            assert report['baseline'] == ta.BASELINE
            write = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute) and node.func.attr == 'write_text')
            namespace = {**vars(ta), 'run': run, 'accs': {'make': .75, 'type': None},
                         'ok': [.75], 'xv': [0] * 4, 'targets': [[0]] * 4,
                         'val': Mock(tolist=lambda: [True] * 4), 'labelled': {'make': 4}, 'confusion': {},
                         'floors': floors, 'withheld': withheld, 'calibration': calibration, 'values': values}
            eval(compile(ast.Expression(write), '<train report>', 'eval'), namespace)
            assert json.loads((run / 'report.json').read_text()) == report, \
                'actual report.json must carry sources and all report fields'
            save = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute) and node.func.attr == 'save')
            class EmptyState:
                def state_dict(self):
                    return {}
            namespace.update(backbone=EmptyState(), heads_fc=EmptyState(),
                             heads={'colour': ['white'], 'make': ['unknown']},
                             names=['colour', 'make'], cnames=[])
            checkpoint = eval(compile(ast.Expression(save.args[0]), '<train checkpoint>', 'eval'), namespace)
            assert checkpoint['floors'] == floors and checkpoint['withheld'] == withheld and checkpoint['values'] == values, \
                'actual checkpoint must carry calibration metadata'


def review_validation():
    with dataset() as (root, heads):
        for sid in ('road-val', 'road-train'):
            frame(root, sid, {'0': {'colour': 'white'}}, rows=2)
            ai(root, sid, {'make': 'Ford'})
            ai(root, sid, {'make': 'Toyota'}, box=1)
        with patch.object(ta, 'is_val', side_effect=lambda sid: sid == 'road-val'):
            _, targets, stems, _ = ta.build(heads)
        assert [t for t, sid in zip(targets, stems) if sid == 'road-val'] == [[-1, 0, -1], [-1, -1, -1]], 'AI validation crops keep human targets unchanged'
        assert [t for t, sid in zip(targets, stems) if sid == 'road-train'] == [[-1, 0, 1], [-1, -1, 0]]


def review_external_default():
    with dataset() as (root, heads):
        src = root / 'external/make/cars'; src.mkdir(parents=True)
        Image.new('RGB', (10, 10)).save(src / 'crop.png')
        (src / 'manifest.jsonl').write_text('{"image":"crop.png","make":"Toyota"}\n')
        assert not ta.build(heads)[0], 'external data must require opt-in'


def review_archive_validation():
    with dataset() as (root, heads):
        for sid in ('archive-val', 'archive-train'):
            frame(root, sid, {'0': {'colour': 'white'}}, rows=2)
            classifier_crops.export_sample(root, sid)
            for kind, suffix in (('images', '.jpg'), ('labels', '.txt'), ('attrs', '.json')):
                (root / 'approved' / kind / (sid + suffix)).unlink()
            ai(root, sid, {'make': 'Ford'})
            ai(root, sid, {'make': 'Toyota'}, box=1)
        with patch.object(ta, 'is_val', side_effect=lambda sid: sid == 'archive-val'):
            _, targets, stems, _ = ta.build(heads)
        assert [t for t, sid in zip(targets, stems) if sid == 'archive-val'] == [[-1, 0, -1], [-1, -1, -1]], 'archive validation keeps human targets unchanged'
        assert [t for t, sid in zip(targets, stems) if sid == 'archive-train'] == [[-1, 0, 1], [-1, -1, 0]], 'archive training uses AI fill and AI-only boxes'


def review_cli_gate():
    tree = ast.parse(Path(ta.__file__).read_text())
    body = tree.body[-1].body
    heads = {'make': ['Toyota']}
    calls = []
    for args in (['external', 'check'], ['check', 'external']):
        with patch.object(sys, 'argv', ['train_attrs.py', *args]):
            parsed = runpy.run_path(str(Path(ta.__file__)))
        assert parsed['ARGS'] == set(args) and parsed['EXTERNAL']
        namespace = {**vars(ta), 'ARGS': parsed['ARGS'], 'vocab': lambda: heads,
                     'build': lambda h: ([None] * 300, [[0]] * 300, ['road'] * 200 + ['external-cars'] * 100, [0] * 300),
                     'check': lambda *a: calls.append('check'), 'train': lambda *a: calls.append('train')}
        exec(compile(ast.Module(body=body, type_ignores=[]), '<main>', 'exec'), namespace)
    assert calls == ['check', 'check'], 'check mode accepts any argument order'
    namespace['build'] = lambda h: ([None] * 300, [[0]] * 300, ['road'] * 199 + ['external-cars'] * 101, [0] * 300)
    namespace['report_counts'] = lambda *a: None
    try:
        exec(compile(ast.Module(body=body, type_ignores=[]), '<main>', 'exec'), namespace)
    except SystemExit as exc:
        assert '199 local enriched boxes' in str(exc), 'external crops cannot meet MIN_BOXES'
    else:
        assert False, '199 road crops must reject training/check'


def promotion_floors():
    import selfloop as sl
    def rep(**accs):
        measured = [v for v in accs.values() if v is not None]
        return {'per_head_acc': accs, 'mean_acc': sum(measured) / len(measured) if measured else None}
    champ = rep(type=.89, axles=.91, colour=None)
    assert sl.promote_attrs(rep(type=.90, axles=.92, colour=.50), champ), 'compare shared heads'
    assert not sl.promote_attrs(rep(type=.89, axles=.91, colour=None), champ), 'shared tie without new measurement'
    assert not sl.promote_attrs(rep(type=.885, axles=.905, colour=.50), champ), 'added colour cannot excuse regression'
    assert not sl.promote_attrs(rep(type=.884, axles=.904, colour=.99), champ), 'colour gains cannot excuse type and axles regression'
    assert not sl.promote_attrs(rep(type=.89, axles=.91, colour=.50), champ), 'added colour cannot break a tie'
    assert not sl.promote_attrs(rep(type=.86, axles=.88, make=.80), rep(type=.89, axles=.91, make=.40)), 'make gains cannot offset established-head regression'
    assert not sl.promote_attrs(rep(type=.90, axles=.92, **{'axle-config': None}), rep(type=.89, axles=.91, **{'axle-config': .8})), 'candidate cannot drop a measured non-floored head'
    assert not sl.promote_attrs(rep(make=.99), champ), 'no shared measurements'
    assert not sl.promote_attrs(None, champ)
    assert sl.promote_attrs(None, None), 'no champion behaves as before'
    assert sl.promote_attrs(rep(type=.1), {'mean_acc': None})
    assert sl.promote_attrs(rep(type=.90, axles=.92, colour=None), rep(type=.89, axles=.91, colour=.99)), 'missing floored colour does not enter promotion'
    assert not sl.promote_attrs(rep(type=.8, make=1.), {'mean_acc': .85}), 'legacy comparison excludes floored heads'
    assert sl.promote_attrs(rep(type=.9, colour=.1), {'mean_acc': .85}), 'legacy comparison uses established-head mean'



def promotion_floor_gain():
    import selfloop as sl
    champ = {'mean_acc': .90, 'per_head_acc': {'type': .89, 'axles': .91},
             'floors': {}, 'withheld': []}
    def candidate(delta=0., head='colour', withheld=()):
        return {'mean_acc': .99, 'per_head_acc': {'type': .89 + delta, 'axles': .91 + delta,
                                                 head: .01},
                'floors': {head: .8}, 'withheld': list(withheld)}
    cases = [
        ('equal old heads, new emitted make', candidate(head='make'), champ, True),
        ('old heads -0.004, new emitted colour', candidate(-.004), champ, True),
        ('old heads -0.005 at tolerance', candidate(-.005), champ, True),
        ('old heads -0.006, new emitted colour', candidate(-.006), champ, False),
        ('make floor also withheld', candidate(head='make', withheld=['make']), champ, False),
        ('champion already emits make', candidate(head='make'),
         {**champ, 'floors': {'make': .7}}, False),
        ('legacy champion without floors', candidate(),
         {'mean_acc': .90, 'per_head_acc': champ['per_head_acc']}, True),
        ('legacy mean-only champion', candidate(), {'mean_acc': .90}, True),
        ('legacy mean-only excessive regression', candidate(-.006), {'mean_acc': .90}, False),
        ('champion make floor withheld', candidate(head='make'),
         {**champ, 'floors': {'make': .7}, 'withheld': ['make']}, True),
        ('lost champion measurement despite new emission',
         {**candidate(), 'per_head_acc': {'type': .99, 'colour': .99}}, champ, False),
        ('non-floored head does not earn gain', candidate(head='cargo'), champ, False),
    ]
    failures = [(name, expected, sl.promote_attrs(rep, champion))
                for name, rep, champion, expected in cases
                if sl.promote_attrs(rep, champion) != expected]
    assert not failures, failures


def promotion_floor_gain_history():
    import selfloop as sl
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        run = root / 'alternating'; run.mkdir()
        (run / 'attrs.pt').write_bytes(b'checkpoint')
        for toggle_withheld in (False, True):
            state = {'attrs_champion': {'mean_acc': .9, 'per_head_acc': {'type': .9}}}
            client, crowns = Mock(), 0
            with patch.object(sl, 'ATTRS_CHAMPION', root / 'attrs-champion.pt'), \
                 patch.object(sl, 'DATASET', root), contextlib.redirect_stdout(io.StringIO()):
                for i in range(180):
                    head = ('colour', 'make')[i % 2]
                    score = state['attrs_champion']['per_head_acc']['type'] - .005
                    report = {'mean_acc': score, 'per_head_acc': {'type': score},
                              'floors': {'colour': .8, 'make': .8} if toggle_withheld else {head: .8},
                              'withheld': [('make', 'colour')[i % 2]] if toggle_withheld else []}
                    if sl.promote_attrs(report, state['attrs_champion']):
                        sl.crown_attrs(state, run, report, client, 'test-bucket')
                        crowns += 1
            loss = .9 - state['attrs_champion']['per_head_acc']['type']
            assert loss <= .010 + 1e-12, f'180 alternating attempts lost {loss:.3f} over {crowns} crowns'
            assert crowns == 2, 'each floored head earns its allowance once'
            assert state['attrs_champion']['earned'] == ['colour', 'make']
            assert json.loads(client.put_object.call_args.kwargs['Body']) == state['attrs_champion']
            # A strict improvement dropping both emissions must retain their earned history.
            report = {'mean_acc': .91, 'per_head_acc': {'type': .91}, 'floors': {}, 'withheld': []}
            assert sl.promote_attrs(report, state['attrs_champion'])
            with patch.object(sl, 'ATTRS_CHAMPION', root / 'attrs-champion.pt'), \
                 patch.object(sl, 'DATASET', root), contextlib.redirect_stdout(io.StringIO()):
                sl.crown_attrs(state, run, report, client, 'test-bucket')
            assert state['attrs_champion']['earned'] == ['colour', 'make']
            for head in ('colour', 'make'):
                assert not sl.promote_attrs({**report, 'mean_acc': .906,
                                            'per_head_acc': {'type': .906}, 'floors': {head: .8}},
                                           state['attrs_champion']), 'strict crowns cannot renew allowances'


def promotion_floor_gain_null_metadata():
    import selfloop as sl
    champ = {'mean_acc': .9, 'per_head_acc': {'type': .9}}
    candidate = {'mean_acc': .896, 'per_head_acc': {'type': .896}, 'floors': {'make': .8}}
    for key in ('floors', 'withheld'):
        assert sl.promote_attrs(candidate, {**champ, key: None}), f'null champion {key}'
    assert sl.promote_attrs({**candidate, 'withheld': None}, champ), 'null candidate withheld'
    assert not sl.promote_attrs({**candidate, 'floors': None}, champ), 'null candidate floors earns nothing'
    assert not sl.promote_attrs(candidate, {**champ, 'earned': ['make']}), 'earned gain cannot repeat'
    assert sl.promote_attrs(candidate, {**champ, 'earned': None}), 'null earned is legacy history'


def calibration_floors():
    heads = {'colour': ['white', 'blue'], 'make': ['Toyota', 'unknown'], 'type': ['van']}
    # Low-confidence errors are withheld; tied confidence crops enter together.
    probs = {'colour': [[.6, .4]] * 20 + [[.8, .2]] * 80,
             'make': [[.95, .05]] * 99}
    labels = {'colour': [1] * 20 + [0] * 80, 'make': [0] * 99}
    floors, withheld, stats, values = ta.calibrate_floors(probs, labels, heads)
    assert floors == {'colour': .8} and withheld == ['make'], (floors, withheld)
    assert stats['colour']['coverage'] == .8 and stats['colour']['precision'] == 1.0
    assert stats['colour']['emitted'] == 80 and .97 < stats['colour']['lower_bound'] < 1.
    floors, withheld, _, values = ta.calibrate_floors({'colour': [[.8, .2]] * 100},
                                            {'colour': [0] * 89 + [1] * 11}, {'colour': heads['colour']})
    assert not floors and withheld == ['colour'], 'ties cannot be split to fake precision'
    floors, withheld, stats, values = ta.calibrate_floors({'colour': [[.6, .4]] * 100},
                                                {'colour': [0] * 90 + [1] * 10}, {'colour': heads['colour']})
    assert not floors and withheld == ['colour'], 'point precision .90 has a Wilson lower bound below .90'
    floors, withheld, stats, values = ta.calibrate_floors({'colour': [[.6, .4]] * 100},
                                                {'colour': [0] * 92 + [1] * 8}, {'colour': heads['colour']})
    assert not floors and withheld == ['colour'], '92/100 must be withheld by the 90% one-sided Wilson bound'
    floors, withheld, _, values = ta.calibrate_floors({}, {}, heads)
    assert not floors and withheld == ['colour', 'make'], 'missing validation is withheld'
    floors, withheld, stats, values = ta.calibrate_floors({'make': [[.1, .9]] * 100},
                                                {'make': [1] * 100}, {'make': heads['make']})
    assert not floors and withheld == ['make'], 'unknown is not covered'
    floors, withheld, stats, values = ta.calibrate_floors({'colour': [[.8, .2]] * 100},
        {'colour': [1] * 100}, {'colour': heads['colour']}, {'colour': [['blue']] * 100})
    assert floors == {'colour': .2} and stats['colour']['precision'] == 1., 'same constrained choice, full-head probability'
    floors, withheld, _, values = ta.calibrate_floors({'colour': [[.8, .2]] * 100},
        {'colour': [0] * 99 + [-1]}, {'colour': heads['colour']})
    assert withheld == ['colour'], 'unlabelled crops do not count toward MIN_VAL'

    # Precision need not rise monotonically with threshold; choose the lowest success.
    floors, withheld, stats, values = ta.calibrate_floors({'colour': [[.9, .1]] * 10 + [[.8, .2]] * 10 + [[.7, .3]] * 180},
        {'colour': [0] * 10 + [1] * 10 + [0] * 180}, {'colour': heads['colour']})
    assert floors == {'colour': .7} and stats['colour']['coverage'] == 1. and stats['colour']['precision'] == .95
    assert stats['colour']['emitted'] == 200 and stats['colour']['lower_bound'] >= .9


def calibration_min_emit():
    for emitted in (1, 49, 50):
        floors, withheld, stats, values = ta.calibrate_floors(
            {'make': [[.99, .01]] * emitted + [[.01, .99]] * (100 - emitted)},
            {'make': [0] * emitted + [1] * (100 - emitted)}, {'make': ['Toyota', 'other']})
        if emitted < 50:
            assert not floors and withheld == ['make'], f'{emitted} emissions cannot qualify'
        else:
            assert floors == {'make': .99} and not withheld, '50 perfect emissions qualify'
            assert stats['make']['emitted'] == 50 and stats['make']['lower_bound'] >= .9


def calibration_unknown_truth():
    for head in ('colour', 'make'):
        heads = {head: ['valid', 'unknown', 'other']}
        floors, withheld, stats, values = ta.calibrate_floors(
            {head: [[.9, .05, .05]] * 70 + [[.05, .9, .05]] * 30},
            {head: [0] * 70 + [1] * 30}, heads)
        assert not floors and withheld == [head], '70 known truths cannot meet MIN_VAL'
        assert stats[head]['human_known'] == stats[head]['labelled_known'] == 70 and stats[head]['source'] == 'ai', 'AI fallback retains the insufficient human truths'

        floors, withheld, stats, values = ta.calibrate_floors(
            {head: [[.9, .05, .05]] * 100 + [[.05, .9, .05]] * 30},
            {head: [0] * 100 + [1] * 30}, heads)
        assert floors == {head: .9} and not withheld
        assert stats[head]['labelled_known'] == 100 and stats[head]['coverage'] == 1.

        floors, withheld, stats, values = ta.calibrate_floors(
            {head: [[.9, .05, .05]] * 100 + [[.05, .9, .05]]},
            {head: [0] * 99 + [1, 0]}, heads)
        assert floors == {head: .9} and not withheld
        assert stats[head]['emitted'] == 100 and stats[head]['precision'] == .99, \
            'emission on an unknown truth must count as wrong'
        assert stats[head]['labelled_known'] == 100 and stats[head]['coverage'] == 1.

        floors, withheld, stats, values = ta.calibrate_floors(
            {head: [[.9, .05, .05]] * 70 + [[.05, .05, .9]] * 30},
            {head: [0] * 70 + [2] * 30}, heads)
        assert floors == {head: .9} and not withheld, 'other truths count toward MIN_VAL'
        assert stats[head]['labelled_known'] == 100 and stats[head]['coverage'] == .7


def suppressed_calibration():
    for head in ('colour', 'make'):
        for value in ('unknown', 'other'):
            floors, withheld, stats, values = ta.calibrate_floors(
                {head: [[.99, .01]] * 100}, {head: [0] * 100}, {head: [value, 'valid']})
            assert not floors and withheld == [head], f'{head}/{value} must not count as emissions'
            assert stats[head]['coverage'] == 0.


def suppressed_selection():
    import detect
    for head in ('colour', 'make'):
        for value in ('unknown', 'other'):
            assert detect.select_attr(head, [4., 0.], [value, 'valid'], None, {}, []) is None, f'{head}/{value} must stay silent'
    assert detect.select_attr('type', [4., 0.], ['other', 'van'], None, {}, []) == 'other', 'only floored heads suppress'


def training_constraint_path():
    import detect
    import torch
    # Execute train's actual calibration wiring, including the real YAML path lookup.
    body = ast.parse(inspect.getsource(ta.train)).body[0].body
    start = next(i for i, node in enumerate(body) if isinstance(node, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'cons' for t in node.targets))
    end = next(i for i, node in enumerate(body) if isinstance(node, ast.Assign)
               and isinstance(node.targets[0], ast.Tuple)
               and [t.id for t in node.targets[0].elts] == ['floors', 'withheld', 'calibration', 'values'])
    wiring = compile(ast.Module(body=body[start:end + 1], type_ignores=[]), '<train calibration>', 'exec')
    with dataset() as (root, _):
        heads = {'colour': ['white', 'blue']}
        (root / 'attributes.yaml').write_text(yaml.safe_dump(
            {**heads, 'constraints': {'truck': {'colour': ['blue']}, 'car': {'colour': ['white']}}}))
        out = root / 'attr_runs/round2/attrs.pt'
        out.parent.mkdir(parents=True)
        assert not (out.parent / 'attributes.yaml').exists()
        namespace = {**vars(ta), 'attr_constraints': detect.attr_constraints, 'out': out,
                     'heads': heads, 'names': ['colour'], 'cnames': ['car', 'truck'],
                     'classes': [0] + [1] * 100, 'val': torch.tensor([False] + [True] * 100),
                     'yv': torch.tensor([[1]] * 100), 'AI_VAL_TARGETS': [[-1]] * 101, 'probabilities': {'colour': [[.8, .2]] * 100}}
        exec(wiring, namespace)
        assert namespace['floors'] == {'colour': .2}, 'train must load dataset constraints, not run-local YAML'
        assert namespace['withheld'] == []
        assert namespace['val_classes'] == ['truck'] * 100, 'validation classes exclude the training car'
        assert namespace['calibration']['colour']['coverage'] == 1. and namespace['calibration']['colour']['precision'] == 1.
        assert detect.select_attr('colour', [0., -1.3862943611198906], heads['colour'], ['blue'],
                                  namespace['floors'], []) == 'blue'

        # Execute the actual run-save path, then load both deployment locations.
        from torchvision.models import mobilenet_v3_small
        model = mobilenet_v3_small(weights=None)
        fc = torch.nn.ModuleList([torch.nn.Linear(578, 2)])
        with torch.no_grad():
            fc[0].weight.zero_()
            fc[0].bias.copy_(torch.tensor([0., -1.3862943]))
        save_start = next(i for i, node in enumerate(body) if isinstance(node, ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == 'run' for t in node.targets))
        save_end = next(i for i, node in enumerate(body) if isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == 'save')
        saving = compile(ast.Module(body=body[save_start:save_end + 1], type_ignores=[]),
                         '<train save>', 'exec')
        namespace.update(torch=torch, RUNS=root / 'attr_runs', backbone=model.features, heads_fc=fc)
        exec(saving, namespace)
        deployed = root / 'attrs-champion.pt'
        deployed.write_bytes(namespace['out'].read_bytes())
        image = Image.new('RGB', (32, 32))
        for path in (namespace['out'], deployed):
            checkpoint = torch.load(path, weights_only=True)
            assert checkpoint['floors'] == namespace['floors'] == {'colour': .2}
            assert detect.attr_classifier(path)(image, 'truck') == {'colour': 'blue'}, \
                f'{path.parent.name} deployment must use calibrated constraints and floor'
            checkpoint['floors'] = {'colour': .21}
            torch.save(checkpoint, path)
            assert detect.attr_classifier(path)(image, 'truck') == {}, 'loader enforces the same floor'
        assert (namespace['run'] / 'attributes.yaml').read_bytes() == (root / 'attributes.yaml').read_bytes()
        (root / 'attributes.yaml').unlink()
        namespace['RUNS'] = root / 'without-yaml'
        exec(saving, namespace)
        assert namespace['out'].is_file() and not (namespace['run'] / 'attributes.yaml').exists(), \
            'missing dataset YAML remains a silent skip'


def champion_calibration_metadata():
    import selfloop as sl
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        run = root / 'round2'; run.mkdir()
        (run / 'attrs.pt').write_bytes(b'checkpoint')
        rep = {'mean_acc': .9, 'per_head_acc': {'colour': .9}, 'floors': {'colour': .2},
               'withheld': ['make'], 'values': {'colour': ['blue'], 'make': []}, 'calibration': {'colour': {'coverage': 1., 'precision': 1.}}}
        for report in (rep, {'mean_acc': .9, 'per_head_acc': {'colour': .9}},
                       {**rep, 'floors': None, 'withheld': None}):
            state, client = {}, Mock()
            with patch.object(sl, 'ATTRS_CHAMPION', root / 'attrs-champion.pt'), \
                 contextlib.redirect_stdout(io.StringIO()):
                sl.crown_attrs(state, run, report, client, 'test-bucket')
            published = client.put_object.call_args.kwargs
            assert published['Bucket'] == 'test-bucket'
            assert published['Key'] == f'{sl.MODELS}attrs-champion.json'
            assert json.loads(published['Body']) == state['attrs_champion']
            for key, default in (('floors', {}), ('withheld', []), ('values', {}), ('calibration', {})):
                assert state['attrs_champion'].get(key) == report.get(key, default), \
                    f'champion must preserve {key}, including legacy defaults'
            assert (root / 'attrs-champion.pt').read_bytes() == b'checkpoint'


def champion_constraints_publication():
    import selfloop as sl
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        run = root / 'round'; run.mkdir()
        (run / 'attrs.pt').write_bytes(b'checkpoint')
        rules = root / 'attributes.yaml'
        rules.write_text('constraints: {truck: {colour: [blue]}}\n')
        for exists in (True, False):
            if not exists:
                rules.unlink()
            client = Mock()
            with patch.object(sl, 'DATASET', root), patch.object(sl, 'ATTRS_CHAMPION', root / 'attrs-champion.pt'), contextlib.redirect_stdout(io.StringIO()):
                sl.crown_attrs({}, run, {'mean_acc': .9, 'per_head_acc': {'type': .9}}, client, 'test-bucket')
            yaml_calls = [c.args for c in client.upload_file.call_args_list if c.args[2] == f'{sl.MODELS}attributes.yaml']
            assert yaml_calls == ([(str(rules), 'test-bucket', f'{sl.MODELS}attributes.yaml')] if exists else []), 'publish dataset constraints alongside deployed champion when present'


def champion_constraints_download():
    import detect
    import selfloop as sl
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        run = root / 'round'; run.mkdir()
        (run / 'attrs.pt').write_bytes(b'checkpoint')
        rules = root / 'attributes.yaml'
        deployed = root / 'deployed'; deployed.mkdir()
        objects = {}
        client = Mock()
        client.upload_file.side_effect = lambda path, bucket, key: objects.update({key: Path(path).read_bytes()})
        client.put_object.side_effect = lambda **kw: objects.update({kw['Key']: kw['Body']})
        client.head_object.side_effect = lambda **kw: {'ContentLength': len(objects[kw['Key']])}
        client.download_file.side_effect = lambda bucket, key, path: Path(path).write_bytes(objects[key])
        for colour in ('blue', 'grey', 'orange'):
            rules.write_text(f'constraints: {{truck: {{colour: [{colour}]}}}}\n')
            with patch.object(sl, 'DATASET', root), patch.object(sl, 'ATTRS_CHAMPION', root / 'attrs-champion.pt'), \
                 contextlib.redirect_stdout(io.StringIO()):
                sl.crown_attrs({}, run, {'mean_acc': .9, 'per_head_acc': {'type': .9}}, client, 'test-bucket')
                got = dataset_sync.models(client, 'test-bucket', deployed)
            assert (deployed / 'attrs-champion.pt').read_bytes() == b'checkpoint'
            if colour == 'blue':
                assert 'attributes.yaml' in got, 'fresh node downloads published constraints'
                assert (deployed / 'attributes.yaml').read_bytes() == rules.read_bytes()
            else:
                assert 'attributes.yaml' not in got, 'existing live vocabulary must never be overwritten, regardless of size'
                assert (deployed / 'attributes.yaml').read_bytes() == local_rules
            assert detect.attr_constraints(deployed / 'attrs-champion.pt') == {'truck': {'colour': ['blue']}}, \
                'deployed champion must read the preserved live constraints beside its weights'
            local_rules = (deployed / 'attributes.yaml').read_bytes()
        del objects[f'{sl.MODELS}attributes.yaml']
        (deployed / 'attributes.yaml').unlink()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            got = dataset_sync.models(client, 'test-bucket', deployed)
        assert 'attributes.yaml' not in got and 'attributes.yaml' not in output.getvalue(), 'missing remote constraints skipped silently'
        assert not (deployed / 'attributes.yaml').exists()


def selection_floors():
    import detect
    select = detect.select_attr
    assert select('colour', [4., 0.], ['white', 'blue'], None, {'colour': .9}, ['colour']) is None
    assert select('colour', [0., 0.], ['white', 'blue'], None, {'colour': .9}, []) is None
    assert select('colour', [4., 0.], ['white', 'blue'], None, {'colour': .9}, []) == 'white'
    assert select('colour', [4., 0.], ['white', 'blue'], ['blue'], {'colour': .9}, []) is None
    assert select('make', [0., 4.], ['Toyota', 'unknown'], None, {}, []) is None
    assert select('colour', [0., 0.], ['white', 'blue'], None, None, None) == 'white'
    assert select('make', [0., 4.], ['Toyota', 'unknown'], None, None, None) == 'unknown', 'legacy unchanged'
    assert select('type', [0., 1.], ['van', 'bus'], None, {}, []) == 'bus'
    assert select('colour', [0., 0.], ['white', 'blue'], None, {'colour': .5}, []) == 'white', 'floor inclusive'
    assert select('colour', [1., 0.], ['white', 'blue'], [], {}, []) is None

    # Exercise the real weights_only loader and its selection wiring, without downloads.
    import torch
    from torchvision.models import mobilenet_v3_small
    with tempfile.TemporaryDirectory() as td:
        heads = {'colour': ['white', 'blue'], 'make': ['Toyota', 'unknown'], 'type': ['van', 'bus']}
        model = mobilenet_v3_small(weights=None)
        fc = torch.nn.ModuleList([torch.nn.Linear(576, 2) for _ in heads])
        with torch.no_grad():
            for layer, bias in zip(fc, ([0., 0.], [0., 4.], [0., 1.])):
                layer.weight.zero_()
                layer.bias.copy_(torch.tensor(bias))
        ck = {'state_dict': {**{f'features.{k}': v for k, v in model.features.state_dict().items()},
                             **{f'fc.{k}': v for k, v in fc.state_dict().items()}},
              'heads': heads, 'input': 32}
        path = Path(td) / 'attrs.pt'
        image = Image.new('RGB', (32, 32))
        torch.save(ck, path)
        assert detect.attr_classifier(path)(image) == {'colour': 'white', 'make': 'unknown', 'type': 'bus'}
        ck.update(floors={'colour': .9}, withheld=[])
        torch.save(ck, path)
        assert detect.attr_classifier(path)(image) == {'type': 'bus'}, 'loader uses floors and suppresses unknown'
        ck.update(floors={'colour': .5}, withheld=[], values={'colour': ['blue']})
        torch.save(ck, path)
        assert detect.attr_classifier(path)(image) == {'type': 'bus'}, 'loader must pass proven values to selection'
        ck['values'] = {'colour': ['white']}
        torch.save(ck, path)
        assert detect.attr_classifier(path)(image) == {'colour': 'white', 'type': 'bus'}
        ck.update(floors={'colour': .5}, withheld=['colour'])
        torch.save(ck, path)
        assert detect.attr_classifier(path)(image) == {'type': 'bus'}, 'withheld overrides passing floor'



def ai_validation_and_value_proof():
    import detect
    heads = {'colour': ['white', 'blue']}
    probs = {'colour': [[.9, .1]] * 200}
    human = {'colour': [-1] * 200}
    floors, withheld, stats, values = ta.calibrate_floors(probs, human, heads, ai_labels={'colour': [0] * 200})
    assert floors == {'colour': .9} and not withheld and values == {'colour': ['white']}
    assert stats['colour']['source'] == 'ai'
    floors, withheld, stats, values = ta.calibrate_floors(probs, human, heads, ai_labels={'colour': [0] * 176 + [1] * 24})
    assert not floors and withheld == ['colour'], 'AI .88 precision cannot meet .90 target'
    floors, withheld, stats, values = ta.calibrate_floors(probs, {'colour': [0] * 192 + [1] * 8}, heads,
                                                       ai_labels={'colour': [1] * 200})
    assert floors == {'colour': .9} and not withheld and stats['colour']['source'] == 'human'
    heads = {'make': ['toyota', 'nissan']}
    floors, withheld, stats, values = ta.calibrate_floors(
        {'make': [[.9, .1]] * 197 + [[.1, .9]] * 3}, {'make': [-1] * 200}, heads,
        ai_labels={'make': [0] * 197 + [1] * 3})
    assert floors == {'make': .9} and not withheld and values == {'make': ['toyota']}
    assert detect.select_attr('make', [0., 4.], heads['make'], None, floors, [], values) is None
    assert detect.select_attr('make', [0., 4.], heads['make'], None, floors, []) == 'nissan'
    many = {'make': [str(i) for i in range(20)]}
    rows = [[.9 if j == i else .1 / 19 for j in range(20)] for i in range(20) for _ in range(5)]
    floors, withheld, stats, values = ta.calibrate_floors(
        {'make': rows}, {'make': [i for i in range(20) for _ in range(5)]}, many)
    assert floors == {'make': .9} and withheld == ['make'] and values == {'make': []}, 'empty value proof withholds head'


def ai_validation_build():
    with dataset() as (root, heads), patch.object(ta, 'is_val', side_effect=lambda sid: sid.endswith('val')):
        frame(root, 'ai-val', rows=3)
        ai(root, 'ai-val', {'colour': 'white', 'make': 'Toyota', 'type': 'van'})
        ai(root, 'ai-val', {'type': 'van'}, box=1)
        ai(root, 'ai-val', {'colour': 'blue'}, box=2, bbox=[.6, .5, .4, .4])
        frame(root, 'ai-train'); ai(root, 'ai-train', {'make': 'Ford'})
        crops, targets, stems, classes = ta.build(heads)
        assert stems == ['ai-train', 'ai-val'] and targets == [[-1, -1, 1], [-1, -1, -1]]
        assert ta.validation_mask(stems) == [False, True]
        assert ta.AI_VAL_TARGETS == [[-1, -1, -1], [-1, 0, 0]] and len(crops) == len(classes) == 2
        ta.build({'type': ['van']})
        assert ta.AI_VAL_TARGETS == [], 'parallel targets reset on every build'


def calibration_proven_population():
    heads = {'make': ['toyota'] + [f'make{i}' for i in range(6)] + ['other']}
    ids = [0] * 12 + [i for i in range(1, 7) for _ in range(9)] + [7] * 34
    rows = [[.9 if j == i else .1 / 7 for j in range(8)] for i in ids]
    floors, withheld, stats, values = ta.calibrate_floors(
        {'make': rows}, {'make': [-1] * 100}, heads, ai_labels={'make': ids})
    assert withheld == ['make'], '66 pooled emissions leave too few proven emissions'
    assert values == {'make': []} and stats['make']['emitted'] == 0
    assert stats['make']['precision'] is None and stats['make']['lower_bound'] == 0.
    # A value can prove .90 individually yet still fall short of the head's 50 crops.
    ids = [0] * 20 + [i for i in range(1, 7) for _ in range(9)] + [7] * 26
    rows = [[.9 if j == i else .1 / 7 for j in range(8)] for i in ids]
    floors, withheld, stats, values = ta.calibrate_floors({'make': rows}, {'make': ids}, heads)
    assert withheld == ['make'] and values == {'make': ['toyota']}
    assert stats['make']['emitted'] == 20 and stats['make']['precision'] == 1.
    assert stats['make']['coverage'] == .2 and .90 < stats['make']['lower_bound'] < .95
    floors, withheld, stats, values = ta.calibrate_floors(
        {'make': rows}, {'make': [-1] * 100}, heads, ai_labels={'make': ids})
    assert withheld == ['make'] and values == {'make': ['toyota']}, 'AI 20/20 proves .90 but needs 50 head emissions'
    assert stats['make']['source'] == 'ai' and stats['make']['emitted'] == 20


def calibration_value_wilson():
    heads = {'make': ['toyota', 'nissan']}
    rows = {'make': [[.9, .1]] * 180 + [[.1, .9]] * 20}
    floors, withheld, stats, values = ta.calibrate_floors(
        rows, {'make': [0] * 180 + [1] * 18 + [0] * 2}, heads)
    assert floors == {'make': .9} and not withheld and values == {'make': ['toyota']}
    assert stats['make']['emitted'] == 180 and stats['make']['precision'] == 1.
    floors, withheld, stats, values = ta.calibrate_floors(
        {'make': [[.9, .1]] * 190 + [[.1, .9]] * 10}, {'make': [-1] * 200}, heads,
        ai_labels={'make': [0] * 190 + [1] * 10})
    assert values == {'make': ['toyota']}, '10/10 cannot prove a value at .90; at least 15 clean emissions are needed'
    assert stats['make']['emitted'] == 190
    floors, withheld, stats, values = ta.calibrate_floors(
        {'make': [[.9, .1]] * 180 + [[.1, .9]] * 20}, {'make': [-1] * 200}, heads,
        ai_labels={'make': [0] * 180 + [1] * 20})
    assert floors == {'make': .9} and not withheld and values == {'make': ['toyota', 'nissan']}, 'AI 20/20 proves a value at .90'
    assert stats['make']['emitted'] == 200 and stats['make']['source'] == 'ai'


def calibration_human_override():
    floors, withheld, stats, values = ta.calibrate_floors(
        {'make': [[.9, .1]] * 200}, {'make': [1] * 99 + [-1] * 101},
        {'make': ['toyota', 'nissan']}, ai_labels={'make': [0] * 200})
    assert not floors and withheld == ['make'], '99 human nissan truths override AI toyota'
    assert stats['make']['human_known'] == 99 and stats['make']['labelled_known'] == 200


def calibration_ai_target():
    args = ({'make': [[.9, .1]] * 200}, {'make': [-1] * 200}, {'make': ['toyota', 'nissan']})
    ai = {'make': [0] * 188 + [1] * 12}
    floors, withheld, stats, values = ta.calibrate_floors(*args, ai_labels=ai)
    assert floors == {'make': .9} and not withheld, '.94 AI precision at n=200 proves .90'
    assert values == {'make': ['toyota']} and stats['make']['source'] == 'ai'
    assert stats['make']['precision'] == .94 and stats['make']['lower_bound'] >= ta.FLOORED['make']
    with patch.object(ta, 'FLOORED', {'make': .95}):
        assert ta.calibrate_floors(*args, ai_labels=ai)[1] == ['make'], 'AI uses the head target unchanged'
    floors, withheld, _, _ = ta.calibrate_floors(*args, ai_labels={'make': [0] * 176 + [1] * 24})
    assert not floors and withheld == ['make'], '.88 AI precision still fails .90'


def training_ai_wiring():
    tree = ast.parse(inspect.getsource(ta.train))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == 'calibrate_floors')
    class Mask:
        def tolist(self):
            return [False, True, True]
    namespace = {**vars(ta), 'probabilities': {'make': [[.9, .1]] * 2}, 'names': ['make'],
                 'val': Mask(), 'AI_VAL_TARGETS': [[1], [0], [1]]}
    assert eval(compile(ast.Expression(call.args[4]), '<train AI argument>', 'eval'), namespace) == {'make': [0, 1]}, \
        'actual train calibration argument must deliver validation AI truths'


def training_ai_alignment():
    body = ast.parse(inspect.getsource(ta.train)).body[0].body
    checks = [n for n in body if isinstance(n, ast.Assert) and 'AI_VAL_TARGETS' in ast.unparse(n.test)]
    assert checks, 'train must assert AI target alignment before tensor/model work'
    for count, should_fail in ((2, False), (1, True)):
        try:
            exec(compile(ast.Module(body=checks, type_ignores=[]), '<train alignment>', 'exec'),
                 {'AI_VAL_TARGETS': [[0]] * count, 'targets': [[0], [0]]})
        except AssertionError:
            assert should_fail
        else:
            assert not should_fail, 'misaligned AI validation targets must fail'


def human_training_crop_counts():
    heads = {'make': ['toyota', 'nissan']}
    targets = [[0]] * 10 + [[-1]] * 100
    stems = ['train'] * 9 + ['val'] * 101
    crop = Image.new('RGB', (ta.INPUT, ta.INPUT))
    with patch.object(ta, 'AI_VAL_TARGETS', [[-1]] * 10 + [[0]] * 100), \
         patch.object(ta, 'is_val', side_effect=lambda s: s == 'val'):
        ta.check(heads, [crop] * 110, targets, stems)
    tree = ast.parse(Path(ta.__file__).read_text())
    count = next(n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == 'local_boxes' for t in n.targets))
    assert eval(compile(ast.Expression(count), '<local boxes>', 'eval'), {**vars(ta), 'targets': targets, 'stems': stems}) == 10
    train = ast.parse(inspect.getsource(ta.train))
    report = next(n for n in ast.walk(train) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == 'report_dict')
    class Mask:
        def tolist(self):
            return [False] * 9 + [True] * 101
    assert eval(compile(ast.Expression(report.args[2]), '<val crops>', 'eval'),
                {**vars(ta), 'targets': targets, 'val': Mask(), 'xv': [crop] * 101}) == 1


def main():
    failures = []
    for test in [calibration_proven_population, calibration_value_wilson, calibration_human_override, calibration_ai_target, training_ai_wiring, training_ai_alignment, human_training_crop_counts, ai_validation_and_value_proof, ai_validation_build, calibration_unknown_truth, calibration_min_emit, suppressed_calibration, suppressed_selection, training_constraint_path, champion_constraints_publication, champion_constraints_download, champion_calibration_metadata, promotion_floors, promotion_floor_gain, promotion_floor_gain_history, promotion_floor_gain_null_metadata, calibration_floors, selection_floors, review_archive_validation, review_cli_gate, review_validation, review_external_default, human_wins, stale_bbox, ai_only_and_last_line, archive_and_reference, external, counts_and_sync, run_file_order, training_mask_and_report]:
        try:
            test()
            print(f'PASS {test.__name__}')
        except (AssertionError, AttributeError, ValueError, TypeError) as exc:
            failures.append(test.__name__)
            print(f'FAIL {test.__name__}: {type(exc).__name__}: {exc}')
    assert not failures, f'{len(failures)} source checks failed: {", ".join(failures)}'
    print('attribute sources self-check ok')


if __name__ == '__main__':
    main()
