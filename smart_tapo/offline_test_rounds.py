"""Bounded TEST-only phrase intervals. Never a customer/payment/session sink."""
import math


class TestRoundTracker:
    def __init__(self, limit=200):
        if type(limit) is not int or not 1<=limit<=200:raise ValueError('invalid_test_result_limit')
        self.limit=limit
        self.records=[]
        self.pending=None
        self.last=-1.
        self.counts=dict(ready=0,end=0,complete=0,end_without_start=0,ambiguous=0)
        self.omitted=0

    def _append(self,record):
        if len(self.records)<self.limit:self.records.append(record)
        else:self.omitted+=1

    def consume(self,row):
        if row.reason!='accepted_test_candidate':return
        onset=row.onset
        if type(onset) not in (int,float) or not math.isfinite(onset) or onset<self.last or onset<0:
            raise ValueError('invalid_test_timeline')
        if row.label not in ('paid_ready','end'):raise ValueError('invalid_test_phrase')
        self.last=onset
        if row.label=='paid_ready':
            self.counts['ready']+=1
            if self.pending is None:self.pending=dict(start=onset,last_start=onset,start_count=1)
            else:
                self.pending['last_start']=onset
                self.pending['start_count']+=1
            return
        self.counts['end']+=1
        if self.pending is None:
            self.counts['end_without_start']+=1
            self._append(dict(status='end_without_start',start=None,end=onset,duration=None))
            return
        pending,self.pending=self.pending,None
        if pending['start_count']>1 or onset<=pending['start']:
            self.counts['ambiguous']+=1
            self._append(dict(status='ambiguous',**pending,end=onset,duration=None))
        else:
            self.counts['complete']+=1
            self._append(dict(status='complete',**pending,end=onset,duration=onset-pending['start']))

    def snapshot(self):
        pending=None if self.pending is None else dict(status='start_without_end',**self.pending,end=None,duration=None)
        return dict(mode='TEST_PHRASE_INTERVALS_NOT_TRANSACTIONS',counts=dict(self.counts),
                    records=[dict(record) for record in self.records],pending=pending,
                    omitted_records=self.omitted,records_truncated=self.omitted>0)
