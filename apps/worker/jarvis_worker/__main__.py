"""Worker entry point: python -m jarvis_worker"""

import asyncio

from jarvis_worker.loop import main

if __name__ == "__main__":
    asyncio.run(main())
