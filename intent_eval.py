"""Reproducible labeled-set evaluation; no network and no production-accuracy claim."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import unicodedata

import clubops as app
import analysis_store as store
import live_rules
from intent_rules import RULESET_VERSION


def source(row):
    return {key: row.get(key, 'comment' if key == 'kind' else '') for key in ('kind', 'text', 'parent', 'title')}


def duplicate_key(row):
    # Context remains part of the key: "多少钱" under two different parents is
    # not interchangeable. Whitespace/case-only duplicates aren't extra votes.
    return store.digest({k: re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', v)).strip().casefold() for k, v in source(row).items()})


def evaluate(dataset, predictions=None):
    if not isinstance(dataset, dict) or not isinstance(dataset.get('rows'), list) or not 1 <= len(dataset['rows']) <= 10000:
        raise ValueError('标注集需要 1–10000 条 rows')
    groups, ids, kinds, skipped = {}, set(), set(), Counter()
    for row in dataset['rows']:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'] or row['id'] in ids:
            raise ValueError('样本 ID 缺失或重复')
        ids.add(row['id'])
        if source(row)['kind'] not in ('comment', 'live') or any(not isinstance(v, str) or len(v) > 10000 for v in source(row).values()):
            raise ValueError('样本原文或上下文格式无效')
        kinds.add(source(row)['kind'])
        if source(row)['kind'] == 'live' and (source(row)['parent'] or source(row)['title']):
            raise ValueError('直播评测只使用当前弹幕原文，parent 和 title 须留空')
        if not row.get('text', '').strip() or row.get('origin') not in ('synthetic', 'public_comment', 'public_live') or row.get('labeler') not in ('assistant', 'human', 'unassigned'):
            raise ValueError('需保留原文、来源类型和标注者类型')
        if not isinstance(row.get('source_ref'), str) or not row['source_ref']:
            raise ValueError('样本缺少可追溯来源')
        if row.get('review_status') != 'confirmed':
            skipped['unconfirmed'] += 1
            continue
        if row['labeler'] == 'unassigned':
            raise ValueError('已确认样本须注明实际标注者类型')
        if row.get('label') not in app.LABELS or not isinstance(row.get('rationale'), str) or not row['rationale'].strip():
            raise ValueError('已确认样本须填写分类及原文依据')
        groups.setdefault(duplicate_key(row), []).append(row)
    selected, conflicts = [], []
    for items in groups.values():
        if len({x['label'] for x in items}) > 1:
            skipped['conflicting_labels'] += len(items)
            conflicts.append([x['id'] for x in items])
        else:
            selected.append(items[0])
            skipped['duplicates'] += len(items)-1
    by_id, engines = None, set()
    if predictions is not None:
        if not isinstance(predictions, list):
            raise ValueError('预测文件须为 JSON 数组')
        by_id = {}
        for p in predictions:
            if not isinstance(p, dict) or p.get('id') not in ids or p['id'] in by_id or p.get('category') not in app.LABELS:
                raise ValueError('预测样本 ID 或分类无效')
            if p.get('method') not in ('rules', 'model') or not isinstance(p.get('engine'), str) or not p['engine']:
                raise ValueError('预测必须标明实际方法和引擎；规则回退不能冒充模型预测')
            by_id[p['id']] = p
            engines.add((p['method'], p['engine']))
        if len(engines) != 1:
            raise ValueError('每次评测只能包含一个实际方法与引擎')
    else:
        engines.update(('rules', live_rules.RULESET_VERSION if kind == 'live' else RULESET_VERSION) for kind in kinds)
    labels = list(app.LABELS)
    matrix = {truth: {pred: 0 for pred in labels} for truth in labels}
    errors, evaluated, origins, labelers = [], [], Counter(), Counter()
    for row in selected:
        data = source(row)
        if by_id is None:
            result = (live_rules.classify(data['text']) if data['kind'] == 'live' else
                      app.classify(data['text'], data['title'], parent_context=data['parent']))
            predicted = result['category']
            actual_method, actual_engine = 'rules', live_rules.RULESET_VERSION if data['kind'] == 'live' else RULESET_VERSION
        else:
            p = by_id.get(row['id'])
            if p is None:
                skipped['missing_predictions'] += 1
                continue
            if p.get('input_hash') != store.digest(data):
                raise ValueError('预测与标注原文版本不一致：' + row['id'])
            predicted = p['category']
            actual_method, actual_engine = p['method'], p['engine']
        truth = row['label']
        matrix[truth][predicted] += 1
        item = dict(id=row['id'], input_hash=store.digest(data), expected=truth, predicted=predicted,
                    source_ref=row['source_ref'], text=row['text'], rationale=row['rationale'],
                    kind=data['kind'], method=actual_method, engine=actual_engine)
        evaluated.append(item)
        origins[row['origin']] += 1
        labelers[row['labeler']] += 1
        if truth != predicted:
            errors.append(item)
    metrics = {}
    for label in labels:
        tp = matrix[label][label]
        fp = sum(matrix[x][label] for x in labels if x != label)
        fn = sum(matrix[label][x] for x in labels if x != label)
        metrics[label] = dict(tp=tp, fp=fp, fn=fn, support=sum(matrix[label].values()),
            precision=tp/(tp+fp) if tp+fp else None, recall=tp/(tp+fn) if tp+fn else None,
            false_positives=[e['id'] for e in errors if e['predicted'] == label],
            false_negatives=[e['id'] for e in errors if e['expected'] == label])
    method, engine = next(iter(engines)) if len(engines) == 1 else ('rules', 'mixed-rules')
    per_kind = {}
    for kind in sorted(kinds):
        items = [r for r in evaluated if r['kind'] == kind]
        correct = sum(r['predicted'] == r['expected'] for r in items)
        kind_matrix = {truth: {pred: sum(r['expected'] == truth and r['predicted'] == pred for r in items)
                              for pred in labels} for truth in labels}
        per_kind[kind] = dict(engine=(live_rules.RULESET_VERSION if kind == 'live' else RULESET_VERSION) if predictions is None else engine,
                             evaluated=len(items), sample_accuracy=correct/len(items) if items else None, confusion_matrix=kind_matrix)
    classifier_sources = {}
    primary_source = 'live_rules.py' if 'live' in kinds else 'intent_rules.py'
    if predictions is None:
        for name in {primary_source, 'intent_rules.py', 'clubops.py'}:
            classifier_sources[name] = hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
    return dict(dataset=dataset.get('name', 'unnamed'), dataset_sha256=store.digest(dataset), method=method, engine=engine,
        input_kinds=sorted(kinds), per_kind=per_kind,
        classifier_source_sha256=classifier_sources.get(primary_source) if len(kinds) == 1 else None,
        classifier_sources_sha256=dict(sorted(classifier_sources.items())),
        input_rows=len(dataset['rows']), eligible_unique=len(selected), evaluated=len(evaluated),
        skipped=dict(skipped), conflicting_groups=conflicts, origins=dict(origins), labelers=dict(labelers),
        sample_accuracy=(len(evaluated)-len(errors))/len(evaluated) if evaluated else None,
        production_accuracy_verified=False, confusion_matrix=matrix, per_class=metrics, errors=errors,
        evaluated_inputs=[dict(id=r['id'], input_hash=r['input_hash']) for r in evaluated],
        predictions=[dict(id=r['id'],input_hash=r['input_hash'],category=r['predicted'],method=r['method'],engine=r['engine']) for r in evaluated],
        limitations=['样本内指标不代表线上准确率；合成或助手标注并非独立人工验收',
                    '冲突标注不投票裁决；去重、未确认标签与缺失预测均另行计数',
                    '规则回退与真实模型预测须分开评测；本工具不调用外部模型'])


def markdown(report):
    lines = [f"# 分类开发评测：{report['dataset']}", '',
             '**开发样本结果，不是线上准确率验收。**', '',
             f"方法：{report['method']} / {report['engine']}。原始 {report['input_rows']} 条，去重且无标注冲突 {report['eligible_unique']} 条，实际评测 {report['evaluated']} 条。",
             f"来源：{json.dumps(report['origins'], ensure_ascii=False)}；标注者：{json.dumps(report['labelers'], ensure_ascii=False)}。", '',
             '输入分组：' + '；'.join(f"{kind} / {item['engine']}：{item['evaluated']} 条" for kind, item in report['per_kind'].items()) + '。', '',
             '| 分类 | 样本数 | 判对 | 误判 FP | 漏判 FN | 精确率 | 召回率 |', '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    percent = lambda x: '无分母' if x is None else f'{x:.1%}'
    for label, m in report['per_class'].items():
        lines.append(f"| {app.LABELS[label]} | {m['support']} | {m['tp']} | {m['fp']} | {m['fn']} | {percent(m['precision'])} | {percent(m['recall'])} |")
    lines.extend(['', '## 逐条分歧', ''])
    for e in report['errors']:
        # Quotes are data; escape markdown control characters and raw HTML.
        text = e['text'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('`', '\\`').replace('\n', ' ')
        lines.append(f"- {e['id']}：标注 {app.LABELS[e['expected']]}，预测 {app.LABELS[e['predicted']]}。原文：{text}")
    if not report['evaluated']:
        lines.append('没有可评测的已确认样本及对应预测，不能计算准确率或判断分类分歧。')
    elif not report['errors']:
        lines.append('此样本内未发现分类分歧，不能外推到未见数据。')
    lines.extend(['', *report['limitations'], '', f"数据集摘要：{report['dataset_sha256']}", ''])
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=Path(__file__).parent/'evaluation'/'intent-development-v1.json')
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dataset = json.loads(args.dataset.read_text(encoding='utf-8'))
    predictions = json.loads(args.predictions.read_text(encoding='utf-8')) if args.predictions else None
    report = evaluate(dataset, predictions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    args.output.with_suffix('.md').write_text(markdown(report), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('method', 'engine', 'input_rows', 'eligible_unique', 'evaluated', 'skipped')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
