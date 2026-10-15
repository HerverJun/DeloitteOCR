"""One daily backup at 02:00 Asia/Hong_Kong while the portal is running."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from zoneinfo import ZoneInfo
from .maintenance import backup,retention
from .store import wire

def next_backup(current):
    local=current.astimezone(ZoneInfo('Asia/Hong_Kong'))
    target=local.replace(hour=2,minute=0,second=0,microsecond=0)
    if target<=local:target+=timedelta(days=1)
    return target.astimezone(timezone.utc)

async def daily(engine,root):
    due=next_backup(datetime.now(timezone.utc))
    while True:
        await asyncio.sleep(min(60,max(1,(due-datetime.now(timezone.utc)).total_seconds())))
        if datetime.now(timezone.utc)<due:continue
        try:
            await asyncio.to_thread(backup,engine,root)
            await asyncio.to_thread(retention,engine,root,True)
        except Exception as error:
            (root/'central-backup-error.json').write_text(json.dumps({'failed_at':wire(datetime.now(timezone.utc)),'error_type':type(error).__name__}),'utf-8')
        due=next_backup(datetime.now(timezone.utc))
