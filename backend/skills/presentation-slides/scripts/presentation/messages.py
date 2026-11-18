"""按调用收集日志，避免原生PPT函数改写进程stdout。"""
from contextlib import contextmanager
from contextvars import ContextVar

_messages=ContextVar('presentation_messages',default=None)


def record_message(*values,sep=' ',end='\n',file=None,flush=False):
    messages=_messages.get()
    if messages is not None:
        messages.append(sep.join(str(value) for value in values)+end)


@contextmanager
def collect_messages():
    messages=[]
    token=_messages.set(messages)
    try:
        yield messages
    finally:
        _messages.reset(token)
