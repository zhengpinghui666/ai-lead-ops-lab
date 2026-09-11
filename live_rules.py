"""Conservative, literal live-chat screening; no model or cross-user context."""
import re

import intent_rules as rules

RULESET_VERSION = 'live-rules-v1'
CLAUSE = rules.CLAUSE
LIVE_SERVICE = r'陪玩|陪练|带练|复盘|教练|教学|练枪'
HELP = r'带我(?:上分|打排位|打|玩)?|陪我(?:打|玩|上分)|教我(?:玩|打|练枪)|帮我复盘'
MONEY = rf'(?:预算\s*{rules.AMOUNT}(?:\s*[元块])?|(?:给你|出|花)\s*{rules.AMOUNT}\s*[元块]|付费|付钱|有偿)'
PAID_HELP = rf'(?:{MONEY}){CLAUSE}{{0,12}}?(?:{HELP})|(?:{HELP}){CLAUSE}{{0,12}}?(?:{MONEY})'
HELP_PRICE = rf'(?:{HELP}){CLAUSE}{{0,8}}?(?:怎么收费|多少钱|什么价格)|(?:怎么收费|多少钱){CLAUSE}{{0,8}}?(?:{HELP})'
WEAK_HELP = rf'{HELP}|求带(?:一下)?|求个大佬|有人一起(?:打|玩)|[双三五]缺[一二两三四1234]|[23]等[123]'
OFFER = rf'(?:{LIVE_SERVICE}){CLAUSE}{{0,18}}?(?:私我|找我|滴滴|私聊我|联系我)|(?:我|本人){CLAUSE}{{0,5}}?(?:带人|带老板|带你上分)|(?:有偿|收费)带人'
THIRD_PARTY = rf'(?:他|她|他们|她们|主播|队友|战队|俱乐部|这队|那队){CLAUSE}{{0,6}}?(?:想|需要|找|请|约){CLAUSE}{{0,12}}?(?:{LIVE_SERVICE}|人带)'
DENIED = rf'(?:别|别再|不要|不必|不需要|不想|不用){CLAUSE}{{0,5}}?(?:找|请|约|要)?{CLAUSE}{{0,5}}?(?:{LIVE_SERVICE}|{HELP})'
CHAT = r'加油|让一追二|让二追三|手枪局|赛点|比分|战队|一队|二队|指挥|决斗位|C位|这场比赛|看比赛|看得懂比赛|打完了吗|什么时候打|几比几|第[一二三四五六七\d]+[局把]|vct(?:cn)?'
SHORT_CHAT = r'(?:6{2,}|哈{2,}|好{1,3}|是|来了|笑死|牛[啊呀逼]?|路过|前排|打卡|签到|收到|确实|绷不住|[?？!！。…]+|[\W_]+)'


def classify(raw):
    import clubops as app
    # Keep character offsets intact. Nicknames in mentions and platform emote
    # labels are not statements made by this speaker.
    text = re.sub(r'@[^\s，,。!?！？;；]{1,80}|\[[^\]\n]{1,20}\]', lambda m: ' ' * len(m.group()), raw)
    result = rules.classify_comment(text, '', '', app.GAMES, app.TARGET_GAME)
    facts = result['facts']
    evidence = facts['evidence']

    def hits(pattern, kind):
        items = rules.matches(text, pattern, kind)
        evidence.extend(items[:4])
        return rules.positive(items)

    paid_help = hits(PAID_HELP, 'live_paid_help') + hits(HELP_PRICE, 'live_help_price')
    weak = hits(WEAK_HELP, 'live_group')
    offers = hits(OFFER, 'live_offer')
    third = hits(THIRD_PARTY, 'live_third_party')
    denied = hits(DENIED, 'live_denial')
    meta = hits(r'是什么意思|啥意思|这个词|怎么理解|原话|都在刷|开玩笑|逗你玩|整活', 'live_ambiguity')
    reported = any(e['kind'] in ('reported', 'hypothetical') and not e['negated'] for e in evidence)
    free = any(e['kind'] == 'free' and not e['negated'] for e in evidence)
    free_conflict = free and any(e['kind'] in ('payment', 'budget') and not e['negated'] for e in evidence)
    sensitive = any(e['kind'] in ('request', 'pricing', 'supply', 'recruit', 'group', 'live_paid_help', 'live_help_price', 'live_group', 'live_offer') for e in evidence)
    product_budget = any(re.search(rules.PRODUCT, clause) and re.search(MONEY, clause)
                         for clause in rules.SEPARATOR.split(text))
    category, reason = result['category'], ''
    signal = 'none'
    if (reported or third or meta) and sensitive:
        category, reason = 'uncertain', '含转述、他人需求或玩笑／词义讨论，不能确认此用户当前需求。'
    elif denied and category != 'social':
        category, reason = 'uncertain', '含明确否定或劝阻，不按正向需求处理。'
    elif free_conflict:
        category, reason = 'uncertain', '免费与付费／预算表达冲突，需人工核对，暂不归为服务客户。'
    elif offers and (category == 'buyer' or paid_help):
        category, reason = 'uncertain', '同时有提供服务和购买表达，需要人工核对角色。'
    elif offers and category != 'recruit':
        category, reason = 'seller', '原文提供陪玩／带人服务并邀请联系，属于接单方。'
    elif paid_help and category not in ('seller', 'recruit'):
        if free or product_budget:
            category, reason = 'uncertain', '费用表达与免费条件或物品预算混杂，暂不归为服务客户。'
        else:
            category, reason = 'buyer', '同一句内有付费意愿与请人带／陪／教自己的表达，仅为潜在服务需求。'
            signal = 'paid_help'
    elif category == 'buyer' and product_budget and not any(e['kind'] == 'request' and not e['negated'] for e in evidence):
        category, reason = 'uncertain', '物品预算不能作为组队付费意愿，需核对询价对象。'
    elif weak and category not in ('buyer', 'seller', 'recruit', 'social'):
        category, reason = ('social', '原文明示免费组队，不视为付费客户。') if free else ('uncertain', '只有求带、缺人或组队表达，尚未明确购买服务。')
        signal = 'group_only'
    elif not sensitive and (not text.strip() or re.fullmatch(SHORT_CHAT + r'[。.!！?？\s]*', text.strip(), re.IGNORECASE) or re.search(CHAT, text, re.IGNORECASE)):
        category, reason = 'noise', '只有比赛讨论、助威或简短互动，未发现服务需求。'
        signal = 'chat'

    if category == 'buyer' and signal == 'none':
        signal = 'service_request' if any(e['kind'] == 'request' and not e['negated'] for e in evidence) else 'service_price'
    if category in ('seller', 'recruit', 'social'):
        signal = category
    if third or meta or denied:
        facts['warnings'].append('角色或表达归属未确认，不将预算、区服、段位等字段自动归属此用户。')
        for field in ('time', 'party_size', 'budget', 'region', 'service_type', 'rank_label'):
            facts[field] = ''
    # Rank/region or room category alone never supply buying intent. A room
    # context is intentionally absent, so it cannot overwrite an explicit game.
    facts.update(rules_version=RULESET_VERSION, base_rules_version=rules.RULESET_VERSION, live_signal=signal)
    for item in evidence:
        item['text'] = raw[item['start']:item['end']]
        if (third or meta or denied) and item['kind'] in ('time', 'party_size', 'budget', 'region', 'service_type', 'rank_label'):
            item['attribution'] = 'unconfirmed'
    anchors, seen = [], set()
    for i, item in enumerate(evidence):
        key = (item['kind'], item['source'], item['negated'])
        if key not in seen:
            seen.add(key)
            anchors.append(i)
    keep = set(anchors[:40])
    for i in range(len(evidence)):
        if len(keep) >= 40:
            break
        keep.add(i)
    facts['evidence'] = [item for i, item in enumerate(evidence) if i in keep]
    if reason:
        result['reason'] = reason + ' 直播规则初筛，需人工核对。'
    else:
        result['reason'] = result['reason'].replace('评论原文', '弹幕原文')
    result['category'] = category
    return result
