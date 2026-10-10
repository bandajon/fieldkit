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
from unittest.mock import patch

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
        with patch.object(ta, 'is_val', side_effect=lambda sid: sid == 'road-val'):
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
            report = ta.report_dict(run.name, {'make': .75, 'type': None}, 4,
                                    {'make': 4}, {})
            assert report['sources'] == sources, 'report preserves source counts'
            assert report['mean_acc'] == .75 and report['val_crops'] == 4
            assert report['baseline'] == ta.BASELINE
            write = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                         and isinstance(node.func, ast.Attribute) and node.func.attr == 'write_text')
            namespace = {**vars(ta), 'run': run, 'accs': {'make': .75, 'type': None},
                         'ok': [.75], 'xv': [0] * 4, 'labelled': {'make': 4}, 'confusion': {}}
            eval(compile(ast.Expression(write), '<train report>', 'eval'), namespace)
            assert json.loads((run / 'report.json').read_text()) == report, \
                'actual report.json must carry sources and all report fields'

def review_validation():
    with dataset() as (root, heads):
        for sid in ('road-val', 'road-train'):
            frame(root, sid, {'0': {'colour': 'white'}}, rows=2)
            ai(root, sid, {'make': 'Ford'})
            ai(root, sid, {'make': 'Toyota'}, box=1)
        with patch.object(ta, 'is_val', side_effect=lambda sid: sid == 'road-val'):
            _, targets, stems, _ = ta.build(heads)
        assert [t for t, sid in zip(targets, stems) if sid == 'road-val'] == [[-1, 0, -1]], 'validation is human-only, AI-only box excluded'
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
        assert [t for t, sid in zip(targets, stems) if sid == 'archive-val'] == [[-1, 0, -1]], 'archive validation uses only human labels'
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


def main():
    failures = []
    for test in [review_archive_validation, review_cli_gate, review_validation, review_external_default, human_wins, stale_bbox, ai_only_and_last_line, archive_and_reference, external, counts_and_sync, run_file_order, training_mask_and_report]:
        try:
            test()
            print(f'PASS {test.__name__}')
        except (AssertionError, AttributeError, ValueError) as exc:
            failures.append(test.__name__)
            print(f'FAIL {test.__name__}: {type(exc).__name__}: {exc}')
    assert not failures, f'{len(failures)} source checks failed: {", ".join(failures)}'
    print('attribute sources self-check ok')


if __name__ == '__main__':
    main()
