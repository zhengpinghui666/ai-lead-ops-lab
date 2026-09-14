"""Auditable local rules, not a semantic model or a calibrated probability model."""
import re
from game_scope import GAME_PATTERN, exclusion_reason

RULESET_VERSION = 'rules-v6-cn-pc'
SEPARATOR = re.compile(r'[，,。.!?！？；;\n]')
NEGATIVE_BEFORE = re.compile(r'(?:不(?:是|再|要|想|用|需要|打算|考虑)?|没(?:有|想|打算)?|并非|无需|拒绝|谢绝)(?:再|去|找)?\s*$')
NEGATIVE_AFTER = re.compile(r'^\s*(?:不要|不行|没空|不方便|不考虑|不用|不需要|不合适|取消|太贵)')
SERVICE = r'陪玩|陪练|带练|教练|复盘|教学|练枪'
CLAUSE = r'[^，,。.!?！？；;\n]'
REQUEST = (rf'(?:预约|需要|想要|想买|想购买|购买|找|求|(?<![原本后未将回出进])来|(?<!契)约){CLAUSE}{{0,14}}?(?:{SERVICE})'
           rf'|(?:付钱|付费|花\s*\d+\s*(?:元|块)?){CLAUSE}{{0,12}}?(?:请|找|购买|帮我){CLAUSE}{{0,10}}?(?:{SERVICE})'
           rf'|(?:{SERVICE}){CLAUSE}{{0,6}}?(?:还能|能否|是否|可以)?预约[吗么?？]'
           rf'|(?:有没有|谁|哪[里位]){CLAUSE}{{0,10}}?(?:收费|有偿|提供){CLAUSE}{{0,10}}?(?:{SERVICE})')
GROUP = r'(?:找|来|求|缺|差)[^，,。.!?！？；;\n]{0,8}?(?:搭子|队友|个人|人|一位|两位|组队)|带我|带一下|一起(?:玩|开黑|匹配)|组队|互带互学|互学互带|[双三五]排'
PRODUCT = r'皮肤|枪皮|外设|键盘|鼠标|显卡|显示器|电脑|账号|通行证'
AMOUNT = r'\d+(?:\.\d+)?(?:\s*[-–到至]\s*\d+(?:\.\d+)?)?'
RELEVANCE_VERSION = 'comment-relevance-v3'
COMPANION = r'陪玩|陪练|陪打|陪排|带练|代练|代打|男陪|女陪|技术陪|娱乐陪|点陪|陪\s*[wW]'
HELP = (r'求带|带带我|带我(?:打|玩|上分|排位|开黑)|带我[啊呀吧呗吗么?？!！\s]*$'
        r'|(?:找|求|来|缺|有没有|一起)[^，,。.!?！？；;\n]{0,8}(?:搭子|队友|组队|开黑|[双三五]排)'
        r'|(?:免费|付费|有偿|花钱)\s*(?:组队|开黑|上分)|一起玩|互带互学|互学互带')
SERVICE_ACTION = (rf'(?:找|求|请|约|预约|购买|付费|有偿|招聘|招募|提供|接单){CLAUSE}{{0,12}}(?:教练|老师|教学|复盘|练枪)'
                  rf'|(?:教练|老师|教学|复盘|练枪){CLAUSE}{{0,8}}(?:收费|接单|预约|多少钱|怎么约|求职|应聘)'
                  r'|教教我|教我(?:打|玩|练枪|上分)|接单|找(?:个|位)?老板|有老板吗'
                  r'|多少(?:钱|米).{0,4}(?:一小时|每小时|一局)|(?:一小时|每小时|一局).{0,4}多少(?:钱|米)')
SERVICE_QUESTION = r'多少(?:钱|米)|怎么收费|如何收费|什么价格|价格多少|怎么下单|如何下单|在哪下单|怎么买|怎么约|能约|预约|怎么联系|怎么找你|在哪找你|还接吗|接吗|来一个'
PLAY_INVITATION = (r'(?:有人|有没有人|谁|有无)(?:要|想|能|来|一起)?(?:打|玩)(?:瓦|排位|匹配)?(?:不|吗|么|嘛|啊|呀|没|$)'
                   r'|(?:排位|匹配|开黑|打瓦|玩瓦)[^，,。.!?！？；;\n]{0,6}(?:来不|玩不|打不|缺人|缺[一二两三四五1-5])'
                   r'|(?:找|缺|来|求)(?:个|一[个位]|两[个位])?人(?:一起)?(?:打|玩)(?:瓦|排位|匹配|游戏|不|吗|$)'
                   r'|(?:有|有没有)(?:人)?(?:一起)?(?:打|玩)(?:瓦|排位|匹配)?的(?:吗|么|嘛|不|没|$)'
                   r'|(?:来|找|求|缺|差)(?:个|一[个位]|两[个位])?(?:[qQcC]男|男生|女生|妹子|小姐姐|小哥哥|猛男|大佬|高手)')


def companion_relevance(raw, video_context='', parent_context=''):
    """A recall-oriented model gate, separate from buying intent and collection acceptance."""
    def decision(passed, reason, evidence=()):
        return dict(version=RELEVANCE_VERSION, passed=passed, reason=reason, evidence=list(evidence)[:4])
    if not raw.strip():
        return decision(False, '没有可供判断的文字。')
    for pattern, reason in ((COMPANION, '原文提到陪玩、陪练或相关服务。'),
                            (HELP, '原文有求带、陪同游戏或组队表达，交由模型区分付费与免费意图。'),
                            (SERVICE_ACTION, '原文有游戏服务咨询、接单或招募线索。')):
        hits=matches(raw,pattern,'companion_relevance')
        if hits:
            return decision(True,reason,hits)
    play=matches(raw,r'组队|[双三五]排|开黑|上分|练枪','companion_play')
    payment=matches(raw,r'付费|有偿|预算\s*\d|花钱|付钱','companion_payment')
    if play and payment:
        return decision(True,'原文同时有游戏协作和费用线索，交由模型确认服务对象与意图。',play[:2]+payment[:2])
    invitations = matches(raw, PLAY_INVITATION, 'play_invitation')
    if invitations:
        # A literal invitation plus game context only opens model analysis. It
        # never claims payment intent, nor allows the title alone to pass.
        for source, text in (('comment', raw), ('parent', parent_context), ('video', video_context)):
            context = matches(text, GAME_PATTERN.pattern, 'game_context', source=source)
            if context:
                return decision(True, '原文有游戏邀约，且已保存上下文指向瓦；交由模型判断是否存在可争取的陪玩需求。', invitations[:2]+context[:2])
    questions=matches(raw,SERVICE_QUESTION,'companion_question')
    if questions:
        # Resolve the nearest named object; a keyboard question must not borrow
        # a more distant companion-service title. Context alone never passes.
        for source,text in (('comment',raw),('parent',parent_context),('video',video_context)):
            services=matches(text,COMPANION+'|教练|教学|复盘老师','companion_context',source=source)
            products=matches(text,PRODUCT,'product_context',source=source)
            if services:
                return decision(True,'原文询价或咨询，最近的明确话题包含陪玩服务；具体意图仍需模型判断。',questions[:2]+services[:2])
            if products:
                return decision(False,'最近的询问对象是账号、皮肤或设备，没有陪玩服务线索。',questions[:2]+products[:2])
        return decision(False,'只有泛化询价或咨询，原文及已保存上下文未明确指向陪玩服务。',questions[:2])
    return decision(False,'原文未见陪玩、陪练、求带、服务咨询或接单招募线索；仅有相关视频标题不足以通过。')


def matches(text, pattern, kind, group=0, source='comment'):
    result = []
    for m in re.finditer(pattern, text, re.IGNORECASE):
        prefix = SEPARATOR.split(text[max(0, m.start()-14):m.start()])[-1]
        suffix = text[m.end():m.end()+10]
        negative = bool(NEGATIVE_BEFORE.search(prefix) or NEGATIVE_AFTER.search(suffix))
        if kind == 'request' and re.search(r'不是|不要|不用|无需|不想|不需要|不找', m.group()):
            negative = True
        result.append(dict(kind=kind, text=m.group(), value=m.group(group), start=m.start(), end=m.end(), negated=negative, source=source))
        if len(result) >= 25:
            break
    return result


def positive(items):
    return [item for item in items if not item['negated']]


def classify_comment(raw, video_context, parent_context, games, target_game):
    evidence, warnings = [], []

    def read(pattern, kind, group=0):
        items = matches(raw, pattern, kind, group)
        evidence.extend(items)
        return positive(items)

    # Explicit original-comment game wins; context never supplies buying intent.
    game, game_source, game_conflict = '', '', False
    for source, text in [('comment', raw), ('parent', parent_context), ('video', video_context)]:
        found = []
        for name, terms in games.items():
            pattern = GAME_PATTERN.pattern if name == '无畏契约' else '|'.join(re.escape(t) for t in terms)
            hits = positive(matches(text, pattern, 'game', source=source))
            if hits:
                found.append((name, hits[0]))
        if found:
            evidence.extend(item for _, item in found)
            game_source = source
            if len(found) == 1:
                game = found[0][0]
            else:
                game_conflict = True
                warnings.append('同一层原文出现多个游戏，未选择其中一个。')
            break

    requested = read(REQUEST, 'request')
    pricing = read(r'怎么收费|如何收费|多少钱|什么价格|价格多少|怎么下单|如何下单|在哪下单|可以预约|想预约|接单[吗么]', 'pricing')
    supplied = read(rf'接单|可接(?:单|订单|陪玩|陪练|复盘)|接(?:陪玩|陪练|复盘)订单|找(?:个|位)?老板|有老板吗|来(?:个|位)?老板|老板来|应聘|求职'
                    rf'|(?:提供|承接){CLAUSE}{{0,12}}?(?:{SERVICE})|(?:本人|我|我们)(?:做|教){CLAUSE}{{0,12}}?(?:{SERVICE})', 'supply')
    # Asking whether the other person takes orders is not advertising one's service.
    for item in supplied:
        is_question = bool(re.match(r'^[吗么]', raw[item['end']:]) or
                           re.search(r'谁|哪[里位]|有没有|能否|是否', raw[max(0,item['start']-6):item['start']]) or
                           re.search(r'什么|哪些', item['text']))
        if is_question:
            item['kind'] = 'availability_question'
    supplied = [x for x in supplied if x['kind'] == 'supply']
    recruited = read(rf'招聘|招募|招(?:[一二两三四五六七八九十\d]+[名位个])?{CLAUSE}{{0,10}}?(?:陪玩|陪练|教练|复盘老师|队员|打手|人)', 'recruit')
    grouped = read(GROUP, 'group')
    free = read(r'免费|只找队友|只找搭子|不找收费|不花钱|不收费|不付费|不付钱|不要收费|谢绝收费|白嫖', 'free')
    paid = read(r'付费|有偿|付钱|收费|花\s*\d+\s*[元块]', 'payment')
    # Bare limits such as "最多 3 人" are not spending budgets.
    budgets = read(rf'(?:预算\s*(?:不超过|最多|最高)?\s*|(?:最高|最多|上限)\s*(?={AMOUNT}\s*[元块]))({AMOUNT})\s*(?:元|块)?', 'budget', 1)
    service_context, product_context = [], []
    for source, text in [('comment', raw), ('parent', parent_context), ('video', video_context)]:
        hits = positive(matches(text, SERVICE, 'service_context', source=source))
        products = positive(matches(text, PRODUCT, 'product_context', source=source))
        if hits or products:
            service_context = hits[:2]
            product_context = products[:2]
            break
    service_scope = bool(service_context)
    if pricing:
        evidence.extend(service_context + product_context)
    off_topic_price = bool(pricing and product_context and not requested and not service_scope)
    ambiguous_price = bool(pricing and product_context and service_scope and not requested)
    # A provider's "可以预约" is an offer, not the provider buying their own service.
    pricing_request = bool(pricing and not (supplied and not re.search(r'[吗么?？]|怎么|如何|多少|什么价格|价格多少|在哪下单', raw)))
    buyer_signal = bool(requested or (pricing_request and service_scope and not off_topic_price and not ambiguous_price) or ((paid or budgets) and grouped))
    reported = read(r'听说|据说|听[^，,。!?！？；;\n]{0,6}说|[他她]说|[他她]想|别人说|有人说|举例|例如|比如|原话|引用|不是我', 'reported')
    hypothetical = read(r'如果|假如|要是|假设|以后[^，,。.!?！？；;\n]{0,16}(?:再说|再考虑|再预约|再找)|以后再|等[^，,。.!?！？；;\n]{0,8}再(?:找|约|预约)', 'hypothetical')

    scope_exclusion=exclusion_reason(raw,video_context,parent_context)
    if scope_exclusion:
        category, explanation = 'uncertain', scope_exclusion
    elif reported and (buyer_signal or supplied or recruited):
        category, explanation = 'uncertain', '包含转述或举例，不能把他人的表达当作此用户需求。'
    elif hypothetical and (buyer_signal or supplied or recruited):
        category, explanation = 'uncertain', '包含假设或将来再考虑的表达，尚不能确认当前需求。'
    elif game_conflict:
        category, explanation = 'uncertain', '游戏上下文存在冲突，需要人工判断。'
    elif recruited:
        category, explanation = 'recruit', '原文有招募表达，不归入购买陪玩服务的客户。'
    elif supplied and buyer_signal:
        category, explanation = 'uncertain', '同时存在接单与购买表达，需要人工区分角色。'
    elif supplied:
        category, explanation = 'seller', '原文有提供服务、接单、找老板或求职表达。'
    elif free and (paid or budgets):
        category, explanation = 'uncertain', '免费与付费/预算表达同时出现，需要人工核对。'
    elif free and (grouped or requested or service_scope):
        category, explanation = 'social', '原文明示不付费或只找队友，不视为付费客户。'
    elif buyer_signal:
        category, explanation = 'buyer', '原文表达服务需求或询价；仅为潜在需求，不等于已付费或同意联系。'
    elif off_topic_price:
        category, explanation = 'noise', '询价对象是游戏物品或设备，不是陪玩服务。'
    elif grouped:
        category, explanation = 'uncertain', '只有找搭子、组队或求带表达，未明确购买服务。'
    elif any(x['negated'] for x in evidence if x['kind'] in ('request', 'pricing', 'supply')):
        category, explanation = 'uncertain', '相关表达含否定，不按正向购买或接单需求处理。'
    elif re.search(r'哈哈|路过|好看|围观|厉害', raw):
        category, explanation = 'noise', '未发现服务需求，属于一般讨论。'
    else:
        category, explanation = 'uncertain', '缺少明确的购买、接单或招募表达，需要人工判断。'

    def single(items, label):
        values = list(dict.fromkeys(x['value'].strip() for x in items))
        if len(values) > 1:
            warnings.append(f'{label}有多处不同表达，未自动选定。')
            return ''
        return values[0] if values else ''

    counts = read(r'(?:最多|至少|缺)?[一二两三四五六七八九十\d]+\s*(?:个|位)?(?:陪玩|陪练|队友|人)', 'party_size')
    regions = read(r'国服|亚服|国际服|港服|台服|电信|网通|微信区|QQ区', 'region')
    times = read(r'明天晚上|今天晚上|今晚|明晚|明天|后天|今天|现在|周末|周[一二三四五六日天]|上午|中午|下午|晚上|(?:[01]?\d|2[0-3]):[0-5]\d(?:\s*[-–到至]\s*(?:[01]?\d|2[0-3]):[0-5]\d)?', 'time')
    ranks = read(r'段位\s*[:：]?\s*([^\s，。！？,;；]{1,12})', 'rank_label', 1)
    service = ''
    if game == target_game:
        # Keep the existing single-primary-service field; do not infer it from a nickname or parent.
        for name, pattern in [('对局复盘', '复盘'), ('新手陪练', '新手|陪练|教学|练枪|入门'), ('排位组队', '排位|双排|三排|五排'), ('娱乐开黑', '娱乐|开黑|匹配|陪玩')]:
            hits = read(pattern, 'service_type')
            if hits:
                service = name
                break
    facts = dict(time=' / '.join(dict.fromkeys(x['value'] for x in times)), party_size=single(counts, '人数'),
        budget=single(budgets, '预算'), region=single(regions, '区服'), service_type=service,
        rank_label=single(ranks, '段位'), rules_version=RULESET_VERSION, warnings=warnings)
    if reported or hypothetical:
        warnings.append('原文含转述或假设，预算、区服、人数、时间、段位和服务方向暂不归属此用户；请人工核对。')
        for key in ('time', 'party_size', 'budget', 'region', 'service_type', 'rank_label'):
            facts[key] = ''
        for item in evidence:
            if item['kind'] in ('time', 'party_size', 'budget', 'region', 'service_type', 'rank_label'):
                item['attribution'] = 'unconfirmed'
    # Evidence is bounded and consists of literal spans from the named input only.
    # Repeated request/price hits must not push all field or negation evidence out.
    anchors, seen = [], set()
    for index, item in enumerate(evidence):
        key = (item['kind'], item['source'], item['negated'])
        if key not in seen:
            seen.add(key)
            anchors.append(index)
    keep = set(anchors[:40])
    for index in range(len(evidence)):
        if len(keep) >= 40:
            break
        keep.add(index)
    facts['evidence'] = [item for index, item in enumerate(evidence) if index in keep]
    facts['game_source'] = game_source
    facts['companion_relevance'] = companion_relevance(raw,video_context,parent_context)
    source_label = {'comment': '评论原文', 'parent': '已保存的上级原文', 'video': '视频标题'}.get(game_source, '未明确')
    reason = explanation + f' 游戏依据：{source_label}。规则初筛，需人工核对。'
    return dict(category=category, confidence=None, game=game, reason=reason, facts=facts, analysis_method='rules')
