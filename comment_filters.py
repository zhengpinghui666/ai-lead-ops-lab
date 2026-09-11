"""Literal comment filters, independent of discovery search and intent scoring."""
import re
import unicodedata


def normalize(value, label='关键词'):
    if not isinstance(value, str) or len(value) > 4000:
        raise ValueError(f'{label}须为最多 4000 字的文本')
    terms, seen = [], set()
    for term in re.split(r'[,，;；\r\n]+', unicodedata.normalize('NFKC', value)):
        term = term.strip()
        if not term:
            continue
        if len(term) > 40 or any(unicodedata.category(c).startswith('C') for c in term):
            raise ValueError(f'{label}每个词最多 40 字，不能包含控制字符')
        if term.casefold() not in seen:
            seen.add(term.casefold())
            terms.append(term)
    if len(terms) > 50:
        raise ValueError(f'{label}最多 50 个词，请用逗号或换行分隔')
    return '\n'.join(terms)


def rejection(text, include_keywords='', exclude_keywords=''):
    # Inputs are validated and snapshotted when a task is created. Terms are
    # literal substrings, never regular expressions or executable expressions.
    folded = unicodedata.normalize('NFKC', str(text or '')).casefold()
    if any(term.casefold() in folded for term in exclude_keywords.splitlines() if term):
        return 'filtered_blocked'
    if include_keywords and not any(term.casefold() in folded for term in include_keywords.splitlines() if term):
        return 'filtered_keyword'
    return None


def matches(text, keywords):
    folded = unicodedata.normalize('NFKC', str(text or '')).casefold()
    return [term for term in keywords.splitlines() if term and term.casefold() in folded]
