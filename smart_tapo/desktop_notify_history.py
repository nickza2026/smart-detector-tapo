"""Read-only, bounded Notify attempt history viewer (no sending capability)."""
from datetime import datetime
import json
import tkinter as tk
from tkinter import ttk


_ERROR = 'ไม่สามารถอ่านประวัติได้ กรุณาลองใหม่อีกครั้ง'
_WARNING = 'คำเตือน: บันทึกประวัติการส่งบางรายการไม่สำเร็จ'
_PAGE_SIZE = 100


class NotifyHistoryButton(ttk.Button):
    def __init__(self, parent, history_getter):
        super().__init__(parent, text='ประวัติการส่ง Notify', command=self._open)
        self._history_getter = history_getter
        self.window = None

    def _open(self):
        if self.window is None or not self.window.winfo_exists():
            self.window = NotifyHistoryWindow(self.winfo_toplevel(), self._history_getter)
        else:
            self.window.refresh()
        self.window.deiconify()

    def close(self):
        if self.window is not None:
            if self.window.winfo_exists():
                self.window.destroy()
            self.window = None

    def destroy(self):
        self.close()
        super().destroy()


class NotifyHistoryWindow(tk.Toplevel):
    def __init__(self, parent, history_getter):
        super().__init__(parent)
        self.withdraw()
        self.title('ประวัติการส่ง Notify — 30 วัน')
        self.geometry('1000x650')
        self.minsize(700, 400)
        self._history_getter = history_getter
        self._history = None
        self._cursors = [None]
        self._rows = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(2, weight=2)
        bar = ttk.Frame(self, padding=8)
        bar.grid(row=0, column=0, sticky='ew')
        self.refresh_button = ttk.Button(bar, text='รีเฟรช', command=self.refresh)
        self.refresh_button.pack(side='left')
        self.newer_button = ttk.Button(bar, text='ใหม่กว่า', command=self.newer)
        self.newer_button.pack(side='left', padx=6)
        self.older_button = ttk.Button(bar, text='เก่ากว่า', command=self.older)
        self.older_button.pack(side='left')
        self.status = ttk.Label(bar, text='')
        self.status.pack(side='left', padx=8)
        listing = ttk.Frame(self, padding=(8, 0))
        listing.grid(row=1, column=0, sticky='nsew')
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        columns = ('id', 'created', 'channel', 'alert_id', 'importance', 'http_status', 'state', 'code', 'elapsed_ms')
        self.tree = ttk.Treeview(listing, columns=columns, show='headings', selectmode='browse')
        for name in columns:
            self.tree.heading(name, text=name)
            self.tree.column(name, width=90 if name != 'created' else 155, minwidth=40)
        self.tree.grid(row=0, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind('<<TreeviewSelect>>', self._select)
        detail = ttk.Frame(self, padding=8)
        detail.grid(row=2, column=0, sticky='nsew')
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(0, weight=1)
        self.details = tk.Text(detail, wrap='none', state='disabled')
        self.details.grid(row=0, column=0, sticky='nsew')
        vertical = ttk.Scrollbar(detail, orient='vertical', command=self.details.yview)
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal = ttk.Scrollbar(detail, orient='horizontal', command=self.details.xview)
        horizontal.grid(row=1, column=0, sticky='ew')
        self.details.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.refresh()

    def _set_detail(self, value=''):
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', value)
        self.details.configure(state='disabled')

    def refresh(self):
        self._cursors = [None]
        self._load()

    def _load(self):
        self._set_detail()
        self.tree.delete(*self.tree.get_children())
        self._rows = []
        self._history = None
        try:
            history = self._history_getter()
            rows = history.list_page(before_id=self._cursors[-1], limit=_PAGE_SIZE)
            self._history = history
            self._rows = rows[:_PAGE_SIZE]
            for row in self._rows:
                values = []
                for name in self.tree['columns']:
                    value = row.get(name)
                    if name == 'created' and value is not None:
                        value = datetime.fromtimestamp(value).astimezone().strftime('%Y-%m-%d %H:%M:%S')
                    values.append('.' if value is None else value)
                self.tree.insert('', 'end', iid=str(row['id']), values=values)
            self.status.configure(text=_WARNING if getattr(history, 'last_error', False) else '')
        except Exception:
            self.tree.delete(*self.tree.get_children())
            self._rows = []
            self._history = None
            self.status.configure(text=_ERROR)
        self.newer_button.configure(state='normal' if len(self._cursors) > 1 else 'disabled')
        self.older_button.configure(state='normal' if len(self._rows) == _PAGE_SIZE else 'disabled')

    def older(self):
        if len(self._rows) == _PAGE_SIZE:
            self._cursors.append(self._rows[-1]['id'])
            self._load()

    def newer(self):
        if len(self._cursors) > 1:
            self._cursors.pop()
            self._load()

    def _select(self, _event=None):
        self._set_detail()
        selected = self.tree.selection()
        if not selected or self._history is None:
            return
        try:
            record = self._history.detail(int(selected[0]))
            if record is not None:
                meta = {key:value for key,value in record.items()
                        if key not in ('request_body','response_body')}
                self._set_detail(json.dumps(meta, ensure_ascii=False, indent=2)
                    +'\n\nREQUEST BODY\n'+(record.get('request_body') or '')
                    +'\n\nRESPONSE BODY\n'+(record.get('response_body') or '(ไม่มี response body)'))
            else:
                self.status.configure(text=_ERROR)
        except Exception:
            self.status.configure(text=_ERROR)


def smoke_notify_history(root, app):
    """Actual SDK/SQLite/viewer flow with an in-memory HTTP opener only."""
    import io
    from uuid import uuid4
    import threading
    from .notify_api import NotifyTarget, AlertMessage, send_alert
    key = 'synthetic-history-key-never-store'
    alert = AlertMessage('history-'+uuid4().hex,uuid4().hex,'TEST','synthetic history body','critical')
    target = NotifyTarget('https://notify.invalid',key,history=app._notify_history,channel='manual_test')
    class Response:
        def __init__(self,code,body):
            self.code,self.headers = code,{'Content-Type':'application/json'}
            self.stream = io.BytesIO(json.dumps(body).encode())
        def read(self,size):return self.stream.read(size)
        def close(self):self.stream.close()
    class Opener:
        calls = 0
        def open(self,request,timeout):
            self.calls += 1
            if self.calls==1:
                return Response(202,dict(alert_id=alert.alert_id,importance='critical',status='provider_accepted',
                    acknowledged=False,delivery_verified=False,expires_at='2099-01-01T00:00:00Z'))
            return Response(401,dict(error=dict(code='unauthorized',message='synthetic upstream detail'),
                                     api_key=key,debug='visible diagnostic field'))
    opener = Opener()
    results = [send_alert(target,alert,threading.Event(),opener=opener) for _ in range(2)]
    rows = [row for row in app._notify_history.list_page() if row['alert_id']==alert.alert_id]
    if len(rows)!=2 or [r['state'] for r in rows]!=['failed','success']:
        raise RuntimeError('Notify history did not persist both actual fake-HTTP attempts')
    root.deiconify()
    root.update()
    button=app.notify_history_button
    left,top=button.winfo_rootx()-root.winfo_rootx(),button.winfo_rooty()-root.winfo_rooty()
    if (not button.winfo_ismapped() or left<0 or top<0
            or left+button.winfo_width()>root.winfo_width()
            or top+button.winfo_height()>root.winfo_height()):
        raise RuntimeError('Notify history button is outside the actual viewport')
    button.invoke()
    window=app.notify_history_button.window
    if window is None:
        raise RuntimeError('Notify history button was not available')
    root.update()
    window.tree.selection_set(str(rows[0]['id']))
    window._select()
    text=window.details.get('1.0','end-1c')
    good = (window.winfo_ismapped() and 'https://notify.invalid/v1/alerts' in text
            and 'REQUEST BODY' in text and 'RESPONSE BODY' in text
            and 'visible diagnostic field' in text and '"http_status": 401' in text
            and 'critical' in text and str(window.details['state'])=='disabled')
    redacted = key not in text and '[REDACTED]' in text
    app.notify_history_button.close()
    return dict(notify_history_gui=bool(good),notify_history_both_outcomes=True,
                notify_history_redacted=redacted,
                notify_history_fake_only=opener.calls==2 and all(r.history_saved for r in results))
