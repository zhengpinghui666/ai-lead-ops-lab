"""Repeated group events retain cursor bounds but never duplicate text ingestion."""
import json,unittest
from test_group_monitor import GROUP
from test_uid_inbox import InboxTests,reply
from test_uid_session import SENDER,RECEIVER
import group_inbox,uid_protocol as wire

class GroupDuplicates(InboxTests):
    def fields(self,**changes):
        result={1:GROUP['conversation_id'],2:2,3:987654321,4:100,
            5:int(GROUP['conversation_short_id']),6:7,7:int(RECEIVER),
            8:json.dumps({'text':'合成测试：找个瓦搭子'}),10:1789059717000,12:0}
        result.update({int(k):v for k,v in changes.items()})
        return result

    def read_rows(self,rows,*,cursor=0,more=False,next_cursor=0):
        def exchange(operation,prepared,body):
            encoded=b''.join(wire.field(1,b''.join(wire.field(k,v) for k,v in row.items())) for row in rows)
            return reply(operation,prepared,encoded+wire.field(2,next_cursor)+wire.field(3,int(more)))
        return group_inbox.messages(SENDER,GROUP,cursor=cursor,provider=self.provider,exchange=self.exchange(exchange))

    def test_same_event_new_index_keeps_all_bounds_and_one_text(self):
        first=self.fields();second=self.fields(**{'4':90,'9':'opaque-ext','13':2,'17':91})
        for rows in ([first,second],[second,first]):
            with self.subTest(reversed=rows[0] is second):
                result=self.read_rows(rows,more=True,next_cursor=89)
                self.assertEqual(len(result['messages']),1)
                self.assertEqual(result['messages'][0]['index'],'100')
                self.assertEqual((result['minimum_index'],result['maximum_index']),('90','100'))
                self.assertEqual((result['duplicates'],result['next_cursor']),(1,'89'))
                self.assertTrue(result['has_more'])
                self.assertFalse(result['read_marker_requested'])

    def test_exact_duplicates_and_distinct_ids(self):
        row=self.fields();result=self.read_rows([row,row,self.fields(**{'3':987654322,'4':99})])
        self.assertEqual(len(result['messages']),2)
        self.assertEqual(result['duplicates'],1)

    def test_conflicting_business_fields_reject_entire_page(self):
        for field,value in [(1,'wrong-group'),(2,1),(5,55555),(6,8),(7,int(RECEIVER)+1),
                (8,json.dumps({'text':'other text'})),(10,1789059718000),(12,1),(14,'different-secuid'),(18,1)]:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.read_rows([self.fields(),self.fields(**{str(field):value})])

    def test_duplicate_does_not_relax_cursor_or_index_validation(self):
        row=self.fields()
        with self.assertRaisesRegex(ValueError,'nonadvancing_cursor'):
            self.read_rows([row,row],cursor=100,more=True,next_cursor=100)
        invalid=self.fields();invalid[4]='wrong-type'
        with self.assertRaises(ValueError):self.read_rows([row,invalid])

    def test_duplicate_nontext_is_skipped_once(self):
        row=self.fields(**{'6':8});result=self.read_rows([row,row])
        self.assertEqual(result['messages'],[])
        self.assertEqual((result['skipped'],result['duplicates']),(1,1))

if __name__=='__main__':unittest.main()
