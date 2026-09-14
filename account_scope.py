"""Thread-local credential selection. Business data always stays in the shared DB."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

CURRENT=ContextVar('clubops_task_account',default=None)

def current():return CURRENT.get()

def directory():
    import clubops as app
    import collection_accounts
    row=current()
    return collection_accounts.directory(row) if row else Path(app.DATA_DIR).resolve()

@contextmanager
def use(row):
    if row:
        import collection_accounts
        collection_accounts.directory(row)
    token=CURRENT.set(dict(row) if row else None)
    try:yield row
    finally:CURRENT.reset(token)
