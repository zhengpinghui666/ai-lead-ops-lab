"""Authored development cases, not an independent production accuracy estimate."""
import unittest
import live_rules

CASES = {
    'buyer': ['找个陪玩', '无畏契约找陪练，今晚，预算100元', '陪玩多少钱一小时', '陪练还接单吗',
        '复盘怎么收费', '付费带我上分', '出50元带我打排位', '带我上分，预算100元，找陪练',
        '给你50块陪我玩', '有没有收费陪练', '不接单，找个陪玩', '付费找个搭子',
        '加油！找个陪玩', '亚服找陪练，段位白银', '想找陪练，我电脑修好了', '找陪玩[比心]', '带我上分怎么收费', '教我练枪多少钱'],
    'seller': ['陪练接单，有老板吗', '我带人上分', '无畏契约陪玩30一小时私我', '陪练滴滴',
        '接单找老板', '本人带你上分', '我提供陪练，可以预约', '教练求职', '有偿带人'],
    'recruit': ['俱乐部招陪练', '招募无畏契约队员', '招两名陪玩'],
    'social': ['免费带我', '不花钱，找个搭子', '无畏契约不付费，找陪练', '只找队友，不找收费',
        '免费组队', '白嫖陪练带我'],
    'noise': ['aq加油', '666666', '[鼓掌][比心]', '哈哈哈哈', '？', '打完了吗', '让一追二了吗',
        '这个皮肤多少钱', '鼠标多少钱', 'fpx是一队吗', '赛点了', '第3局打得不错',
        '@找陪玩 加油', '@陪玩接单 666', 'VCTCN不能没有你', '前排', '来了'],
    'uncertain': ['多少钱', '带我', '求带', '求个大佬', '有没有人一起玩', '五缺一', '国服白银今晚有空',
        '主播找教练', '他说找陪玩，不是我', '如果有钱就找陪玩', '别找陪玩',
        '找陪玩是什么意思', '找陪玩，开玩笑的', '我接单，也想找个陪玩', '免费带我，预算100元',
        '鼠标预算200，找个人带我', '找三角洲和无畏契约陪练', '什么段位', '@找陪玩 在吗',
        '想找陪练，以后再说', '队友需要陪练', '皮肤和陪练多少钱', '不用带我'],
}


class LiveRuleTests(unittest.TestCase):
    def test_authored_cases(self):
        for expected, cases in CASES.items():
            for text in cases:
                with self.subTest(text=text):
                    self.assertEqual(live_rules.classify(text)['category'], expected)

    def test_evidence_is_literal_and_bounded(self):
        for text in [s for rows in CASES.values() for s in rows] + ['找陪练多少钱，' * 200]:
            result = live_rules.classify(text)
            self.assertEqual(result['facts']['rules_version'], 'live-rules-v1')
            self.assertIsNone(result['confidence'])
            self.assertLessEqual(len(result['facts']['evidence']), 40)
            for item in result['facts']['evidence']:
                self.assertEqual(text[item['start']:item['end']], item['text'])

    def test_mentions_and_room_inference_do_not_supply_user_fields(self):
        result = live_rules.classify('@无畏契约陪练 带我')
        self.assertEqual(result['game'], '')
        self.assertEqual(result['facts']['budget'], '')
        self.assertEqual(result['category'], 'uncertain')
        self.assertEqual(live_rules.classify('三角洲找陪练')['game'], '三角洲行动')
        self.assertEqual(live_rules.classify('带我上分')['facts']['live_signal'], 'group_only')


if __name__ == '__main__':
    unittest.main()
