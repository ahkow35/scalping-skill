import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def no_live_recorder_tape(monkeypatch, request):
    """assemble() asks the Railway watcher for the recorder tape. No test may
    reach the network, the Keychain or a saved watcher config: default to
    "not configured" (REST buckets, as before). Tests of the tape path
    override this with their own monkeypatch."""
    if request.module.__name__ == "test_account_monitor_remote_check":
        return  # these test remote_flow itself, with injected fakes
    import account_monitor
    monkeypatch.setattr(account_monitor, "remote_flow",
                        lambda coin, *a, **kw: (None, "not configured"))
