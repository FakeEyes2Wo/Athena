# `athena.app_server` implementation test result

Tested target: `src/athena/app_server`

## Commands

```bash
.venv/Scripts/python.exe -B -m athena.app_server
.venv/Scripts/python.exe -B -m pytest -p no:cacheprovider -q -rxX test/unit/app_server
.venv/Scripts/python.exe -B -m black --check test/unit/app_server
```

## Result

- Built-in smoke: 7 passed.
- Current-interface pytest suite: 98 collected, 91 passed, 7 xfailed.
- Unittest subtests: 25 passed.
- Collection, teardown, and formatting errors: none.
- Target Python source hashes: unchanged before and after the run.

The seven strict expected failures describe current implementation issues, not
missing external interfaces:

1. `AthenaClient.shutdown()` marks itself closing before sending its request.
2. A `server/shutdown` request includes itself in the in-flight drain set.
3. The lifecycle therefore leaves that shutdown request task alive.
4. `FairMux` can starve a later ready subscription.
5. An idle `FairMux` polls with `asyncio.sleep()`.
6. `SubscriptionRegistry` has no journal-to-subscription event pump.
7. `ThreadRuntime` accepts a second turn while another turn is active.
