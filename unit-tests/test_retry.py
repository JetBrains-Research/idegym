from idegym.client.operations.utils import retry_with_backoff


async def test_walk_with_flat_dictionary():
    x = {"attempt": 0}

    @retry_with_backoff(attempts=3, base_delay=0.1)
    async def f():
        x["attempt"] += 1
        if x["attempt"] <= 2:
            raise Exception("test")
        else:
            print("Good")

    await f()
