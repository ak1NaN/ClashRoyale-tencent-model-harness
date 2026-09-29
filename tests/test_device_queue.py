from types import SimpleNamespace

import pytest

from ai_client.bridge import NativeQueueStateChanged
from ai_client.device import AndroidDevice


@pytest.mark.parametrize('message,transient', [
    ('Error: Ability state changed before queue: source missing at tick 2001', True),
    ('Error: Ability already queued: source 5000086 at tick 2001', True),
    ('Error: Ability is unavailable or already queued', True),
    ('Error: Native command rejected', False),
])
def test_only_prequeue_ability_state_rejections_are_skipped(monkeypatch,message,transient):
    device=AndroidDevice({})
    device.connected=True
    device.stream=SimpleNamespace(latest=lambda: {'tick':2000})
    def reject(_request):raise RuntimeError(message)
    device.script=SimpleNamespace(exports_sync=SimpleNamespace(nativequeue=reject))
    monkeypatch.setattr('ai_client.device.deployment_request',
                        lambda _action,_snapshot:{'kind':'activate_ability'})
    with pytest.raises(NativeQueueStateChanged if transient else RuntimeError):
        device.queue_action({'kind':'activate_ability'})
