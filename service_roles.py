"""Shared supply-side family for comments, live chat and group messages.

Keep seller/recruit as subtypes. A source being about services is not evidence
that its author wants to buy them. Original model results remain in the ledger.
"""
import re

FAMILY = 'supply'
LABEL = '陪玩打手招募／接单'
CATEGORIES = ('seller', 'recruit')
SUPPLY = (r'求(?:个|位)?老板|蹲(?:个|位)?老板|等(?:个|位)?老板'
          r'|(?:有无|有没有)(?:个|位)?老板(?:来|要)?点(?:我|俺|咱)'
          r'|(?:谁|老板)(?:来|能|可以|要)?点(?:我|俺|咱)'
          r'|(?:求|等|蹲)(?:单|订单)|(?:老板|老\s*板)(?:滴滴|dd|DD|看我|看看我)'
          r'|(?:本人|我是|我做|我当|自荐).{0,8}(?:男陪|女陪|技术陪|娱乐陪|打手)'
          r'|(?:男陪|女陪|技术陪|娱乐陪|打手).{0,8}(?:求职|应聘|接单|找单|求单|找老板|求老板)')
# No compulsory qualifier: "招打手" and "收女陪" are common compact forms.
RECRUIT = r'(?:招|招聘|招募|收)(?:长期|大量|全职|兼职|国服|瓦|端游|高段位|[0-9一二两三四五]+[个名位]?)?(?:陪玩|陪练|打手|男陪|女陪|技术陪|娱乐陪)'
QUICK = re.compile(SUPPLY + '|' + RECRUIT + r'|接单|找.{0,2}老板|有老板吗|招聘|招募|求职|应聘', re.I)
TEAMUP_ONLY = re.compile(r'(?:(?:现在|今晚|今天|一会儿|一会|这会儿|这会|此时)\s*)?(?:有(?:人|没有人|没有)?(?:要)?(?:一起)?(?:打|玩)(?:瓦|游戏)?(?:的)?(?:吗|么|嘛|不)?|(?:有没有|有无)(?:人)?(?:要)?(?:一起)?(?:打|玩)(?:瓦|游戏)?(?:的)?|(?:来|找)(?:个|几个|点)?(?:搭子|队友)(?:一起)?(?:打瓦|开黑)?)[？?！!。,.，\s]*')


def ordinary_teamup(text):
    return bool(TEAMUP_ONLY.fullmatch((text or '').strip()))


def classify(text, title='', parent=''):
    """Return only an unambiguous provider/recruiter role, never a buyer."""
    if not QUICK.search(text or ''):
        return None
    import clubops as app
    from intent_rules import classify_comment
    result = classify_comment(text, title, parent, app.GAMES, app.TARGET_GAME)
    if result['category'] not in CATEGORIES or result['game'] != app.TARGET_GAME:
        return None
    return result


def project(row, source):
    """Make legacy false buyers visible as supply; preserve human reviews."""
    if row.get('analysis_method') not in ('human', 'pending') and row.get('category') == 'buyer' and ordinary_teamup(source['text']):
        row['role_guard'] = dict(version='ordinary-teamup-v1', previous_category='buyer')
        row.update(category='social', reason='普通找搭子、组队邀约，未表达陪玩服务需求。', analysis_method='rules')
    if row.get('analysis_method') not in ('human', 'pending'):
        role = classify(source['text'], source.get('title', ''), source.get('parent', ''))
        if role and row.get('category') != role['category']:
            row['role_guard'] = dict(version='service-role-v1', previous_category=row.get('category'),
                                     reason=role['reason'])
            row.update(category=role['category'], reason=role['reason'], facts=role['facts'], analysis_method='rules')
    row['category_group'] = FAMILY if row.get('category') in CATEGORIES else row.get('category')
    return row
