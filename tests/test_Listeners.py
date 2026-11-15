#===========================================================================
#
# Tests for: insteon_mqtt/Listeners.py
#
# pylint: disable=redefined-outer-name,protected-access
#===========================================================================
import json
import logging
import os
import time
import pytest
import yaml
import insteon_mqtt as IM
from insteon_mqtt.device.BatterySensor import BatterySensor
from insteon_mqtt.device.Leak import Leak
from insteon_mqtt.Listeners import Listeners, message_key

MODEM = IM.Address('2b.93.07')
LEAK = IM.Address('3f.53.9e')
SWITCH = IM.Address('29.4e.52')

# Leak sensor heartbeat: group broadcast to group 4, cmd 11 04.
LEAK_BCAST = bytes.fromhex('02503F539E000004CB1104')
# Switch reporting to the modem: direct message, cmd 11 ff.
SWITCH_DIRECT = bytes.fromhex('0250294E522B93070F11FF')
# Switch ACK to the modem.
SWITCH_ACK = bytes.fromhex('0250294E522B93072F0D02')
# Group 0 broadcast from the switch ("all on").
GROUP0 = bytes.fromhex('0250294E52000000CB1100')


ON = bytes.fromhex('02503F539E000001CB1101')
OFF = bytes.fromhex('02503F539E000001CB1301')


def capture(s):
    """Record injected frames instead of handing them to the protocol."""
    calls = []
    s.listeners.protocol = MockInject(calls)
    return calls


def plm_read(s, when, frame):
    """The modem reads a frame at insteon-mqtt time 'when'."""
    s.listeners.clock = lambda: when
    s.listeners.handle_plm_read(frame, False)


def run(s, when):
    """Run the release tick at insteon-mqtt time 'when'."""
    s.listeners.clock = lambda: when
    for _ in range(10):
        s.modem.timed_call.poll(when + 0.01)


@pytest.fixture
def setup():
    serial = MockSerial()
    protocol = IM.Protocol(serial)
    modem = MockModem()
    mqtt = MockMqtt()
    listeners = Listeners(mqtt, modem, protocol)
    listeners.load_config({'inject': ['battery'], 'shadow': ['state'],
                           'stats_interval': 0})
    received = SlotRecorder()
    protocol.signal_received.connect(received.slot)
    return Setup(listeners, protocol, modem, mqtt, serial, received)


def heard(frame, rx='t-embed', t=None, repaired=0):
    return {'v': 1, 'rx': rx, 't': time.time() if t is None else t,
            'frame': frame.hex().upper(), 'copies': 2, 'hops_left': 2,
            'rssi': -80.0, 'snr': None, 'repaired': repaired}


def settle(s):
    """Jump past the grace period and run the main loop's timed calls."""
    later = time.time() + 8
    s.listeners.clock = lambda: later
    for _ in range(10):
        s.modem.timed_call.poll(later + 1)
    s.listeners.clock = time.time


#===========================================================================
class Test_Listeners:
    #-----------------------------------------------------------------------
    def test_config(self, setup):
        assert setup.listeners.inject == {'battery'}
        assert setup.listeners.shadow == {'state'}
        assert setup.listeners.topic == 'insteon-rf/msg/+'

        # A tier in both lists is live, not shadowed.
        setup.listeners.load_config({'inject': ['group'],
                                     'shadow': ['group', 'state'],
                                     'stats_interval': 0})
        assert setup.listeners.shadow == {'state'}

    #-----------------------------------------------------------------------
    def test_missed_battery_message_is_injected(self, setup):
        setup.listeners.handle_message(heard(LEAK_BCAST))
        assert setup.received.calls == []        # held for the grace period
        settle(setup)
        assert len(setup.received.calls) == 1
        assert setup.received.calls[0][0].from_addr == LEAK
        counts = setup.listeners.counts
        assert counts['plm_missed'] == 1 and counts['injected'] == 1
        assert setup.listeners.devices[LEAK.hex]['injected'] == 1

    #-----------------------------------------------------------------------
    def test_modem_heard_it(self, setup):
        setup.listeners.handle_message(heard(LEAK_BCAST))
        # The modem's copy arrives during the grace period, with other hops.
        modem_copy = LEAK_BCAST[:8] + bytes([0xC7]) + LEAK_BCAST[9:]
        setup.serial.signal_read.emit(setup.serial, modem_copy)
        settle(setup)
        assert len(setup.received.calls) == 1    # the modem's, not ours
        assert setup.listeners.counts['plm_heard'] == 1
        assert setup.listeners.counts['injected'] == 0

    #-----------------------------------------------------------------------
    def test_modem_heard_it_first(self, setup):
        setup.serial.signal_read.emit(setup.serial, LEAK_BCAST)
        setup.listeners.handle_message(heard(LEAK_BCAST))
        settle(setup)
        assert setup.listeners.counts['plm_heard'] == 1

    #-----------------------------------------------------------------------
    def test_shadow_tier_is_only_logged(self, setup, caplog):
        with caplog.at_level(logging.INFO):
            setup.listeners.handle_message(heard(SWITCH_DIRECT))
            settle(setup)
        assert setup.received.calls == []
        assert setup.listeners.counts['shadow'] == 1
        assert "Would inject state" in caplog.text

    #-----------------------------------------------------------------------
    def test_unlisted_tier_is_ignored(self, setup):
        setup.listeners.load_config({'inject': ['battery'],
                                     'stats_interval': 0})
        setup.listeners.handle_message(heard(SWITCH_DIRECT))
        settle(setup)
        assert setup.received.calls == []
        assert setup.listeners.counts['shadow'] == 0

    #-----------------------------------------------------------------------
    @pytest.mark.parametrize("frame,repaired,why", [
        (LEAK_BCAST, 1, 'repaired'),
        (SWITCH_ACK, 0, 'ack'),
        (bytes.fromhex('02501122332B93070F11FF'), 0, 'unknown sender'),
        ])
    def test_never_injected(self, setup, frame, repaired, why):
        # pylint: disable=unused-argument
        setup.listeners.load_config({'inject': ['battery', 'group',
                                                'state'],
                                     'stats_interval': 0})
        setup.listeners.handle_message(heard(frame, repaired=repaired))
        settle(setup)
        assert setup.received.calls == []
        assert setup.listeners.counts['rejected'] == 1

    #-----------------------------------------------------------------------
    def test_message_to_another_device_is_not_a_miss(self, setup):
        setup.listeners.load_config({'inject': ['battery', 'group',
                                                'state'],
                                     'stats_interval': 0})
        # A leak sensor's cleanup to one of its other responders.
        cleanup = bytes.fromhex('02503F539E445566 4F1104'.replace(' ', ''))
        setup.listeners.handle_message(heard(cleanup))
        settle(setup)
        counts = setup.listeners.counts
        assert counts['not_for_modem'] == 1
        assert counts['plm_missed'] == 0 and counts['rejected'] == 0
        assert setup.received.calls == []

    #-----------------------------------------------------------------------
    def test_modem_own_message_is_ignored(self, setup):
        own = bytes.fromhex('02502B9307294E520F0D00')
        setup.listeners.handle_message(heard(own))
        settle(setup)
        assert setup.listeners.counts['plm_missed'] == 0
        assert MODEM.hex not in setup.listeners.devices

    #-----------------------------------------------------------------------
    def test_group_0_alert(self, setup):
        setup.listeners.handle_message(heard(GROUP0))
        settle(setup)
        assert setup.received.calls == []
        topic, payload, retain = setup.mqtt.published[-1]
        assert topic == 'insteon/listeners/alert' and retain is False
        data = json.loads(payload)
        assert data['alert'] == 'all-link-group-0'
        assert data['from'] == SWITCH.hex
        assert 't-embed' in data['heard_by']

    #-----------------------------------------------------------------------
    def test_receivers_are_merged(self, setup):
        t = time.time()
        setup.listeners.handle_message(heard(LEAK_BCAST, rx='a', t=t))
        setup.listeners.handle_message(heard(LEAK_BCAST, rx='b', t=t + .1))
        other = bytes.fromhex('02503F539E000001CB1101')
        setup.listeners.handle_message(heard(other, rx='a', t=t + .2))
        settle(setup)
        rx = setup.listeners.receivers
        assert rx['a']['heard'] == 2 and rx['b']['heard'] == 1
        assert rx['a']['sole_missed'] == 1 and rx['b']['sole_missed'] == 0
        assert setup.listeners.counts['messages'] == 2

    #-----------------------------------------------------------------------
    def test_rate_limit(self, setup):
        # A quick open/close pair both get through; a flood does not.
        limit = setup.listeners.PER_DEVICE_PER_MINUTE
        for i in range(limit + 1):
            frame = bytes.fromhex('02503F539E%06XCB11%02X' % (4, i))
            setup.listeners.handle_message(heard(frame))
            settle(setup)
        assert len(setup.received.calls) == limit
        assert setup.listeners.counts['rate_limited'] == 1

    #-----------------------------------------------------------------------
    def test_same_bytes_later_is_a_new_event(self, setup):
        now = time.time()
        setup.listeners.handle_message(heard(SWITCH_DIRECT, t=now), now=now)
        setup.listeners.handle_message(heard(SWITCH_DIRECT, t=now + 2),
                                       now=now + 2)
        settle(setup)
        assert setup.listeners.counts['messages'] == 2

    #-----------------------------------------------------------------------
    def test_superseded_by_a_later_message(self, setup):
        # Door-style, same group: the modem missed "on" (group 1) and read
        # "off" for group 1 well after it; no listener heard the "off".
        on = bytes.fromhex('02503F539E000001CB1101')
        off = bytes.fromhex('02503F539E000001CB1301')
        now = time.time()
        setup.listeners.handle_message(heard(on, t=now), now=now)
        setup.listeners.clock = lambda: now + 2
        setup.serial.signal_read.emit(setup.serial, off)
        settle(setup)
        assert setup.listeners.counts['superseded'] == 1
        assert setup.listeners.counts['injected'] == 0

    #-----------------------------------------------------------------------
    def test_late_modem_copy_of_an_older_message(self, setup):
        # "on" heard by both; "off" 0.3 s later missed by the modem. The
        # modem's copy of the older "on" lands after the listener's "off".
        on = bytes.fromhex('02503F539E000001CB1101')
        off = bytes.fromhex('02503F539E000001CB1301')
        now = time.time()
        setup.listeners.handle_message(heard(on, t=now), now=now + 0.7)
        setup.listeners.handle_message(heard(off, t=now + .3), now=now + 1.0)
        setup.listeners.clock = lambda: now + 1.2
        setup.serial.signal_read.emit(setup.serial, on)
        settle(setup)
        injected = [c[0].cmd1 for c in setup.received.calls]
        assert 0x13 in injected
        assert setup.listeners.counts['superseded'] == 0

    #-----------------------------------------------------------------------
    def test_other_group_or_report_does_not_supersede(self, setup):
        report = bytes.fromhex('02503F539E110104CB0600')   # cleanup report
        other = bytes.fromhex('02503F539E000002CB1102')    # group 2
        now = time.time()
        setup.listeners.handle_message(heard(LEAK_BCAST, t=now), now=now)
        setup.listeners.clock = lambda: now + 2
        setup.serial.signal_read.emit(setup.serial, report)
        setup.serial.signal_read.emit(setup.serial, other)
        settle(setup)
        assert setup.listeners.counts['superseded'] == 0
        assert setup.listeners.counts['injected'] == 1

    #-----------------------------------------------------------------------
    def test_broadcast_whose_cleanup_the_modem_read(self, setup):
        # The same event in its other form: the modem handled it.
        setup.listeners.handle_message(heard(LEAK_BCAST))
        cleanup = bytes.fromhex('02503F539E2B93074F1104')
        setup.serial.signal_read.emit(setup.serial, cleanup)
        settle(setup)
        assert setup.listeners.counts['paired'] == 1
        assert setup.listeners.counts['injected'] == 0

    #-----------------------------------------------------------------------
    def test_late_copy_from_another_listener(self, setup):
        now = time.time()
        setup.listeners.handle_message(heard(LEAK_BCAST, rx='a', t=now),
                                       now=now)
        settle(setup)
        assert setup.listeners.counts['injected'] == 1
        # Listener b's copy of the same transmission, 3 s late.
        setup.listeners.handle_message(heard(LEAK_BCAST, rx='b', t=now),
                                       now=now + 3)
        settle(setup)
        assert setup.listeners.counts['injected'] == 1
        assert setup.listeners.counts['late_copies'] == 1

    #-----------------------------------------------------------------------
    def test_same_listener_again_is_a_new_transmission(self, setup):
        now = time.time()
        setup.listeners.handle_message(heard(LEAK_BCAST, t=now), now=now)
        setup.listeners.handle_message(heard(LEAK_BCAST, t=now + .8),
                                       now=now + .8)
        settle(setup)
        assert setup.listeners.counts['messages'] == 2

    #-----------------------------------------------------------------------
    def test_missed_repeat_press(self, setup):
        # Press 1 heard by the modem; an identical press 3 s later missed.
        now = time.time()
        setup.listeners.clock = lambda: now + .1
        setup.serial.signal_read.emit(setup.serial, LEAK_BCAST)
        setup.listeners.handle_message(heard(LEAK_BCAST, t=now), now=now)
        setup.listeners.handle_message(heard(LEAK_BCAST, t=now + 3),
                                       now=now + 3)
        # Protocol's own duplicate check runs on the real clock; in the
        # house the two presses are 3 s apart, past its window.
        setup.protocol._read_history.clear()
        settle(setup)
        assert setup.listeners.counts['plm_heard'] == 1
        assert setup.listeners.counts['injected'] == 1

    #-----------------------------------------------------------------------
    # Review scenarios (independent review of the first versions): each of
    # these lost or replayed an event at some point.

    def test_reorder_when_listener_missed_newer(self, setup):
        # ON missed by the modem; OFF read by the modem, no listener heard it.
        calls, t0 = capture(setup), time.time()
        setup.listeners.handle_message(heard(ON, t=t0), now=t0 + .7)
        plm_read(setup, t0 + 1.5, OFF)
        run(setup, t0 + 10)
        assert calls == []
        assert setup.listeners.counts['superseded'] == 1

    def test_reorder_ignores_old_listener_evidence(self, setup):
        # An OFF the listeners heard 10 s earlier must not make a fresh OFF
        # that only the modem read look older than the missed ON.
        calls, t0 = capture(setup), time.time()
        setup.listeners.handle_message(heard(OFF, t=t0 - 10), now=t0 - 9.3)
        plm_read(setup, t0 - 9.0, OFF)
        run(setup, t0 - 5)
        setup.listeners.handle_message(heard(ON, t=t0), now=t0 + .7)
        plm_read(setup, t0 + 2.0, OFF)
        run(setup, t0 + 10)
        assert calls == []

    def test_two_listeners_two_presses(self, setup):
        # Press 1 heard by A and the modem; press 2 (3 s later) only by B.
        calls, t0 = capture(setup), time.time()
        setup.listeners.handle_message(heard(ON, rx='A', t=t0), now=t0 + .7)
        plm_read(setup, t0 + .8, ON)
        setup.listeners.handle_message(heard(ON, rx='B', t=t0 + 3),
                                       now=t0 + 3.7)
        run(setup, t0 + 10)
        assert calls == [ON]

    def test_device_retransmission(self, setup):
        # The device re-sends 0.77 s later; the modem read only the first.
        # Same event: nothing is injected.
        calls, t0 = capture(setup), time.time()
        setup.listeners.handle_message(heard(ON, rx='A', t=t0), now=t0 + .7)
        setup.listeners.handle_message(heard(ON, rx='A', t=t0 + .77),
                                       now=t0 + 1.5)
        plm_read(setup, t0 + .2, ON)
        run(setup, t0 + 10)
        assert calls == []
        assert setup.listeners.counts['plm_heard'] == 2

    def test_same_listener_repeat_press(self, setup):
        # Presses 3 s apart; the modem heard press 1 only.
        calls, t0 = capture(setup), time.time()
        setup.listeners.handle_message(heard(ON, rx='A', t=t0), now=t0 + .7)
        plm_read(setup, t0 + .9, ON)
        setup.listeners.handle_message(heard(ON, rx='A', t=t0 + 3),
                                       now=t0 + 3.7)
        run(setup, t0 + 10)
        assert calls == [ON]                       # press 2, once
        assert setup.listeners.counts['plm_heard'] == 1

    def test_cleanup_report_is_never_injected(self, setup):
        calls, t0 = capture(setup), time.time()
        report = bytes.fromhex('02503F539E110101CB0600')
        setup.listeners.handle_message(heard(report, t=t0), now=t0 + .7)
        run(setup, t0 + 10)
        assert calls == []
        assert setup.listeners.counts['reports'] == 1
        assert setup.listeners.counts['plm_missed'] == 0

    def test_cleanup_missed_broadcast_heard_earlier(self, setup):
        # Broadcast read by the modem; its cleanup 1.6 s later on air (2.3 s
        # at arrival) missed: the modem handled the event.
        calls, t0 = capture(setup), time.time()
        cleanup = bytes.fromhex('02503F539E2B93074F1101')
        plm_read(setup, t0 + .2, ON)
        setup.listeners.handle_message(heard(cleanup, t=t0 + 1.6),
                                       now=t0 + 2.4)
        run(setup, t0 + 10)
        assert calls == []
        assert setup.listeners.counts['paired'] == 1

    def test_duplicate_modem_read_is_not_credited(self, setup):
        # Press 1 read twice (hops 1 and 0); press 2, 3 s later, missed.
        calls, t0 = capture(setup), time.time()
        setup.listeners.clock = lambda: t0 + .2
        setup.listeners.handle_plm_read(ON, False)
        setup.listeners.handle_plm_read(ON, True)
        setup.listeners.handle_message(heard(ON, t=t0), now=t0 + .7)
        setup.listeners.handle_message(heard(ON, t=t0 + 3), now=t0 + 3.7)
        run(setup, t0 + 10)
        assert calls == [ON]

    def test_leak_wet_then_dry(self, setup):
        # Leak sensors: dry is group 1, wet group 2. A missed WET followed by
        # a DRY the modem read is stale.
        calls, t0 = capture(setup), time.time()
        wet = bytes.fromhex('02503F539E000002CB1102')
        dry = bytes.fromhex('02503F539E000001CB1101')
        setup.modem._devices[LEAK.id] = Leak.__new__(Leak)
        setup.listeners.handle_message(heard(wet, t=t0), now=t0 + .7)
        plm_read(setup, t0 + 1.8, dry)
        run(setup, t0 + 10)
        assert calls == []
        assert setup.listeners.counts['superseded'] == 1

    def test_error_in_one_release_does_not_strand_others(self, setup):
        t0 = time.time()
        setup.listeners.protocol = MockInject([], fail_on=LEAK_BCAST)
        dry = bytes.fromhex('02503F539E000001CB1101')
        setup.listeners.handle_message(heard(LEAK_BCAST, t=t0), now=t0 + .7)
        setup.listeners.handle_message(heard(dry, t=t0 + 5), now=t0 + 5.7)
        run(setup, t0 + 20)
        assert setup.listeners._events == []
        assert setup.listeners.protocol.calls == [dry]

    def test_broadcast_and_cleanup_both_missed(self, setup):
        calls, t0 = capture(setup), time.time()
        cleanup = bytes.fromhex('02503F539E2B93074F1101')
        setup.listeners.handle_message(heard(ON, t=t0), now=t0 + .7)
        setup.listeners.handle_message(heard(cleanup, t=t0 + .2),
                                       now=t0 + .9)
        run(setup, t0 + 10)
        assert calls == [ON]
        assert setup.listeners.counts['paired'] == 1

    def test_listener_skew_is_merged_or_counted(self, setup):
        calls, t0 = capture(setup), time.time()
        # 0.15 s apart (different earliest hop copies): one transmission.
        setup.listeners.handle_message(heard(ON, rx='A', t=t0), now=t0 + .7)
        setup.listeners.handle_message(heard(ON, rx='B', t=t0 + .15),
                                       now=t0 + .8)
        run(setup, t0 + 10)
        assert calls == [ON]
        # 0.8 s apart from another listener: counted as a likely split.
        setup.listeners.handle_message(heard(OFF, rx='A', t=t0 + 20),
                                       now=t0 + 20.7)
        setup.listeners.handle_message(heard(OFF, rx='B', t=t0 + 20.8),
                                       now=t0 + 21.4)
        assert setup.listeners.counts['split'] == 1

    def test_old_listener_copy_does_not_order_a_new_read(self, setup):
        # OFF at t0-4 heard by all; ON at t0 missed; OFF at t0+1 read only
        # by the modem. The old OFF must not make the new read look older.
        calls, t0 = capture(setup), time.time()
        setup.listeners.handle_message(heard(OFF, t=t0 - 4), now=t0 - 3.3)
        plm_read(setup, t0 - 3.2, OFF)
        run(setup, t0 - 0.5)
        setup.listeners.handle_message(heard(ON, t=t0), now=t0 + .7)
        plm_read(setup, t0 + 1.5, OFF)
        run(setup, t0 + 10)
        assert calls == []
        assert setup.listeners.counts['superseded'] == 1

    def test_shadow_counts_toward_rate_limits(self, setup):
        setup.listeners.load_config({'shadow': ['battery'],
                                     'stats_interval': 0})
        limit = setup.listeners.PER_DEVICE_PER_MINUTE
        for i in range(limit + 1):
            frame = bytes.fromhex('02503F539E%06XCB11%02X' % (4, i))
            setup.listeners.handle_message(heard(frame))
            settle(setup)
        assert setup.listeners.counts['shadow'] == limit
        assert setup.listeners.counts['rate_limited'] == 1

    def test_shadow_does_not_use_the_live_budget(self, setup):
        # inject battery, shadow group/state: a flood of shadowed state
        # messages must not rate-limit a real battery event.
        setup.listeners.load_config({'inject': ['battery'],
                                     'shadow': ['group', 'state'],
                                     'stats_interval': 0})
        calls, t0 = capture(setup), time.time()
        for i in range(setup.listeners.GLOBAL_PER_MINUTE + 2):
            frame = bytes.fromhex('0250294E522B93070F11%02X' % i)
            setup.listeners.handle_message(heard(frame, t=t0 + i * .01),
                                           now=t0 + .7 + i * .01)
        setup.listeners.handle_message(heard(ON, t=t0 + 1), now=t0 + 1.7)
        run(setup, t0 + 10)
        assert calls == [ON]
        # The shadowed flood hit its own (per-device) limit, not the live one.
        assert setup.listeners.counts['shadow'] == \
            setup.listeners.PER_DEVICE_PER_MINUTE

    def test_modem_late_copies_after_injection(self, setup):
        # A broadcast is injected; the modem then reads its cleanup (and a
        # late copy of the broadcast). Both are dropped, once each.
        t0 = time.time()
        setup.listeners.handle_message(heard(ON, t=t0), now=t0 + .7)
        run(setup, t0 + 3.3)
        assert setup.listeners.counts['injected'] == 1
        cleanup = bytes.fromhex('02503F539E2B93074F1101')
        setup.listeners.clock = lambda: t0 + 3.6
        assert setup.listeners.filter_modem_read(ON) is True
        setup.listeners.clock = lambda: t0 + 4.5
        assert setup.listeners.filter_modem_read(cleanup) is True
        assert setup.listeners.filter_modem_read(cleanup) is False
        # Past the guards, the same bytes are a real new event.
        setup.listeners.clock = lambda: t0 + 20
        assert setup.listeners.filter_modem_read(ON) is False
        assert setup.listeners.counts['late_modem_copies'] == 2

    def test_protocol_uses_the_filter(self, setup):
        # The protocol drops what the filter says, before handling it.
        t0 = time.time()
        setup.listeners.handle_message(heard(LEAK_BCAST, t=t0), now=t0 + .7)
        run(setup, t0 + 3.3)
        assert len(setup.received.calls) == 1        # the injection
        setup.protocol._read_history.clear()
        setup.listeners.clock = lambda: t0 + 3.5
        setup.serial.signal_read.emit(setup.serial, LEAK_BCAST)
        assert len(setup.received.calls) == 1        # late copy dropped

    def test_retransmission_rule_ignores_listener_clock(self, setup):
        # The listener's clock is 5 s off; the modem read the original and
        # the listener reports the retransmission: still the same event.
        calls, t0 = capture(setup), time.time()
        plm_read(setup, t0 + .2, ON)
        setup.listeners.handle_message(heard(ON, t=t0 - 4.2), now=t0 + 2.5)
        setup.listeners.handle_message(heard(ON, t=t0 - 5), now=t0 + .9)
        run(setup, t0 + 10)
        assert calls == []

    def test_cleanup_report_does_not_hide_the_event(self, setup):
        calls, t0 = capture(setup), time.time()
        report = bytes.fromhex('02503F539E110101CB0600')
        setup.listeners.handle_message(heard(ON, t=t0), now=t0 + .7)
        plm_read(setup, t0 + 1.0, report)
        run(setup, t0 + 10)
        assert calls == [ON]

    #-----------------------------------------------------------------------
    def test_listener_clock_skew_does_not_matter(self, setup):
        # The listener's clock is 5 s off; correlation uses arrival time.
        setup.listeners.handle_message(heard(LEAK_BCAST, t=time.time() - 5))
        setup.serial.signal_read.emit(setup.serial, LEAK_BCAST)
        settle(setup)
        assert setup.listeners.counts['plm_heard'] == 1

    #-----------------------------------------------------------------------
    def test_modem_copy_after_grace_but_before_release(self, setup):
        # The main loop can run the release late; a modem copy read in the
        # meantime still counts.
        now = time.time()
        setup.listeners.handle_message(heard(LEAK_BCAST, t=now), now=now)
        setup.listeners.clock = lambda: now + 4
        setup.serial.signal_read.emit(setup.serial, LEAK_BCAST)
        settle(setup)
        assert setup.listeners.counts['plm_heard'] == 1

    #-----------------------------------------------------------------------
    @pytest.mark.parametrize("t", [float('nan'), float('inf')])
    def test_non_finite_time_is_rejected(self, setup, t):
        setup.listeners.handle_message(dict(heard(LEAK_BCAST), t=t))
        assert setup.listeners.counts['malformed'] == 1
        assert setup.listeners._events == []
        # The scheduler is not poisoned: a good message still goes through.
        setup.listeners.handle_message(heard(LEAK_BCAST))
        settle(setup)
        assert setup.listeners.counts['injected'] == 1

    #-----------------------------------------------------------------------
    def test_stale_message_is_dropped(self, setup):
        setup.listeners.handle_message(heard(LEAK_BCAST, t=time.time() - 45))
        settle(setup)
        assert setup.listeners.counts['stale'] == 1
        assert setup.received.calls == []

    #-----------------------------------------------------------------------
    def test_group_without_modem_link(self, setup):
        setup.listeners.load_config({'inject': ['battery', 'group'],
                                     'stats_interval': 0})
        unlinked = bytes.fromhex('0250294E52000003CB1100')
        setup.listeners.handle_message(heard(unlinked))
        settle(setup)
        counts = setup.listeners.counts
        assert counts['no_modem_link'] == 1 and counts['plm_missed'] == 0
        assert setup.received.calls == []

    #-----------------------------------------------------------------------
    @pytest.mark.parametrize("payload", [
        b'not json', b'[1, 2]', json.dumps({'v': 2}).encode(),
        json.dumps(dict(heard(LEAK_BCAST), frame='0262AA')).encode(),
        json.dumps(dict(heard(LEAK_BCAST), frame='0250AA')).encode(),
        json.dumps(dict(heard(LEAK_BCAST), frame='zz')).encode(),
        json.dumps({k: v for k, v in heard(LEAK_BCAST).items()
                    if k != 't'}).encode(),
        ])
    def test_malformed(self, setup, payload):
        setup.listeners.handle_mqtt(None, None, MockMessage(payload))
        assert setup.listeners.counts['malformed'] == 1
        assert setup.listeners._events == []

    #-----------------------------------------------------------------------
    def test_stats(self, setup):
        setup.listeners.stats_interval = 300
        setup.listeners.handle_message(heard(LEAK_BCAST))
        settle(setup)
        setup.listeners.publish_stats()
        topic, payload, retain = setup.mqtt.published[-1]
        assert topic == 'insteon/listeners/stats' and retain is True
        data = json.loads(payload)
        assert data['counts']['injected'] == 1
        assert data['receivers']['t-embed']['heard'] == 1
        assert data['devices'][LEAK.hex]['plm_missed'] == 1

    #-----------------------------------------------------------------------
    def test_subscribe(self, setup):
        link = MockLink()
        setup.listeners.subscribe(link, 1)
        assert link.subscribed == ['insteon-rf/msg/+']
        setup.listeners.unsubscribe(link)
        assert link.subscribed == []

    #-----------------------------------------------------------------------
    def test_key_ignores_hops(self):
        a = LEAK_BCAST
        b = LEAK_BCAST[:8] + bytes([0xC3]) + LEAK_BCAST[9:]
        assert message_key(a) == message_key(b)
        assert message_key(a) != message_key(SWITCH_DIRECT)

    #-----------------------------------------------------------------------
    def test_mqtt_creates_listeners_only_when_configured(self,
                                                         mock_paho_mqtt):
        # pylint: disable=unused-argument
        link = IM.network.Mqtt()
        modem = MockModem()
        mqtt = IM.mqtt.Mqtt(link, modem)
        base = {"broker": "127.0.0.2", "port": "12345",
                "cmd_topic": "insteon/command",
                "availability_topic": "insteon/availability"}

        mqtt.load_config(dict(base))
        assert mqtt.listeners is None

        mqtt.load_config(dict(base, listeners={'inject': ['battery'],
                                               'stats_interval': 0}))
        assert isinstance(mqtt.listeners, Listeners)

    #-----------------------------------------------------------------------
    def test_schema(self):
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            '..', 'config-example.yaml')
        with open(path, "r") as f:
            document = yaml.load(f, Loader=yaml.Loader)

        document['mqtt']['listeners'] = {
            'topic': 'insteon-rf/msg/+', 'inject': ['battery'],
            'shadow': ['group', 'state'], 'stats_interval': 300}
        assert IM.config.validate_file(document, 'config-schema.yaml',
                                       'configuration') == ""

        document['mqtt']['listeners']['inject'] = ['everything']
        assert IM.config.validate_file(document, 'config-schema.yaml',
                                       'configuration') != ""


#===========================================================================
class Setup:
    def __init__(self, listeners, protocol, modem, mqtt, serial, received):
        self.listeners = listeners
        self.protocol = protocol
        self.modem = modem
        self.mqtt = mqtt
        self.serial = serial
        self.received = received


class MockSerial:
    def __init__(self):
        self.signal_read = IM.Signal()
        self.signal_wrote = IM.Signal()

    def poll(self):
        pass


class MockModem:
    signal_new_device = IM.Signal()

    def __init__(self):
        self.addr = MODEM
        self.timed_call = IM.network.TimedCall()
        self.protocol = IM.Protocol(MockSerial())
        # Device objects only need the right class here.
        self._devices = {LEAK.id: BatterySensor.__new__(BatterySensor),
                         SWITCH.id: object()}
        self.db = MockDb({(LEAK.id, 1), (LEAK.id, 2), (LEAK.id, 4),
                          (SWITCH.id, 1)})

    def find(self, addr):
        return self._devices.get(IM.Address(addr).id)


class MockInject:
    def __init__(self, calls, fail_on=None):
        self.calls = calls
        self.fail_on = fail_on

    def inject(self, frame):
        if frame == self.fail_on:
            raise RuntimeError("device handler failed")
        self.calls.append(frame)
        return True


class MockDb:
    """The modem's responder links: (address id, group)."""
    def __init__(self, links):
        self.links = links

    def find(self, addr, group, is_controller):
        assert is_controller is False
        return object() if (addr.id, group) in self.links else None


class MockMqtt:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=None):
        self.published.append((topic, payload, retain))


class MockLink:
    def __init__(self):
        self.subscribed = []

    def subscribe(self, topic, qos, callback):
        self.subscribed.append(topic)

    def unsubscribe(self, topic):
        self.subscribed.remove(topic)


class MockMessage:
    def __init__(self, payload):
        self.topic = 'insteon-rf/msg/test'
        self.payload = payload


class SlotRecorder:
    # Signal slots are weak references, so the slot has to be a method of
    # an object the test holds on to.
    def __init__(self):
        self.calls = []

    def slot(self, *args):
        self.calls.append(args)
