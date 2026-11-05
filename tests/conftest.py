import asyncio

import pytest


@pytest.fixture(autouse=True)
def real_asyncio_sleep():
    """The vendor codrone_edu module replaces asyncio.sleep when imported (see backend.edu.load_edu_lib);
    make sure no test leaks that into the next one."""
    yield
    asyncio.sleep = asyncio.tasks.sleep
