from unittest import TestCase

from vora.browser.events import EventBus, LifecycleEvent, RuntimeEvent


class EventBusTests(TestCase):
    def test_event_subscription_and_unsubscription(self) -> None:
        bus = EventBus()
        received = []
        unsubscribe = bus.subscribe(LifecycleEvent.EXECUTION_START, received.append)
        event = RuntimeEvent(LifecycleEvent.EXECUTION_START, "run-1")
        bus.emit(event)
        unsubscribe()
        bus.emit(event)
        self.assertEqual(received, [event])

    def test_listener_failure_does_not_block_other_listeners(self) -> None:
        bus = EventBus()
        received = []

        def fail(_: RuntimeEvent) -> None:
            raise RuntimeError("listener failed")

        bus.subscribe(LifecycleEvent.NETWORK_IDLE, fail)
        bus.subscribe(LifecycleEvent.NETWORK_IDLE, received.append)
        event = RuntimeEvent(LifecycleEvent.NETWORK_IDLE, "run-1")
        with self.assertLogs("vora.core.events", level="ERROR"):
            bus.emit(event)
        self.assertEqual(received, [event])
