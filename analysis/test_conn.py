import asyncio
import logging

from ib_async import IB

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s %(message)s")


async def main():
    ib = IB()
    await ib.connectAsync("127.0.0.1", 4001, clientId=23, timeout=40)
    print("CONNECTED accounts:", ib.managedAccounts())
    ib.disconnect()


asyncio.run(main())
