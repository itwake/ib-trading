import asyncio
import logging

from ib_async import IB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


async def try_connect(host, cid, timeout=12):
    ib = IB()
    try:
        await ib.connectAsync(host, 4001, clientId=cid, timeout=timeout)
        print(f"SUCCESS host={host} clientId={cid} accounts={ib.managedAccounts()}")
        ib.disconnect()
        return True
    except Exception as e:
        print(f"FAIL host={host} clientId={cid}: {type(e).__name__} {e}")
        return False


async def main():
    for host, cid in [("127.0.0.1", 0), ("::1", 5), ("127.0.0.1", 2)]:
        ok = await try_connect(host, cid)
        if ok:
            break


asyncio.run(main())
