import asyncio

from .main import main

if __name__ == "__main__":
    print("PDF MERGER: Starting Actor", flush=True)
    asyncio.run(main())