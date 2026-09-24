"""Bounded SSE projection; reconnect uses the same persisted event sequence."""
import asyncio
import json
import time


async def stream_events(request, agent, project_id, session_id, after_seq, *, heartbeat=15):
    cursor = after_seq
    last_heartbeat = time.monotonic()
    while not await request.is_disconnected():
        rows = await asyncio.to_thread(agent.events, project_id, session_id, cursor, 100)
        if rows:
            for row in rows:
                yield f"id: {row['seq']}\nevent: agent\ndata: {json.dumps(row, ensure_ascii=False)}\n\n"
                cursor = row["seq"]
            last_heartbeat = time.monotonic()
        else:
            if time.monotonic() - last_heartbeat >= heartbeat:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(.25)
