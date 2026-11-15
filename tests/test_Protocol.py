#===========================================================================
#
# Tests for: insteont_mqtt/handler/Protocol.py
#
# pylint: disable=protected-access
#===========================================================================
import time
import pytest
import insteon_mqtt as IM
import insteon_mqtt.message as Msg

# 0x50 frame: from 0a.12.33 to 2b.93.07, direct, 3 hops left of 3, cmd 11 ff.
STD_FRAME = bytes([0x02, 0x50, 0x0a, 0x12, 0x33, 0x2b, 0x93, 0x07, 0x2f,
                   0x11, 0xff])


@pytest.fixture
def test_proto():
    link = MockSerial()
    return IM.Protocol(link)

class Test_Protocol:
    def test_reads(self):
        link = MockSerial()
        IM.Protocol(link)

        link.signal_read.emit(link, bytes([0x01, 0x03, 0x04]))
        link.signal_read.emit(link, bytes([0x02, 0x03, 0x04]))

    #-----------------------------------------------------------------------

    def test_duplicate(self):
        link = MockSerial()
        proto = IM.Protocol(link)

        # test standard input
        flags = Msg.Flags(Msg.Flags.Type.DIRECT_ACK, False)
        addr = IM.Address('0a.12.33')
        msg_keep = Msg.InpStandard(addr, addr, flags, 0x11, 0x01)
        dupe = proto._is_duplicate(msg_keep)
        assert dupe is False

        # test dupe with different hops
        flags = Msg.Flags(Msg.Flags.Type.DIRECT_ACK, False, hops_left=2,
                          max_hops=2)
        addr = IM.Address('0a.12.33')
        msg = Msg.InpStandard(addr, addr, flags, 0x11, 0x01)
        dupe = proto._is_duplicate(msg)
        assert dupe is True
        assert len(proto._read_history) == 1

        # not correct message type
        msg = Msg.InpUserReset()
        dupe = proto._is_duplicate(msg)
        assert dupe is False

        # test deleting an expired message
        flags = Msg.Flags(Msg.Flags.Type.DIRECT_ACK, False)
        addr = IM.Address('0a.12.44')
        msg = Msg.InpStandard(addr, addr, flags, 0x11, 0x01)
        proto._read_history.append(msg)
        msg.expire_time = 1
        assert len(proto._read_history) == 2
        proto._remove_expired_read(time.time())
        assert len(proto._read_history) == 1
        assert proto._read_history[0] == msg_keep

    #-----------------------------------------------------------------------
    def test_set_wait_time(self, test_proto):
        assert test_proto._next_write_time == 0
        test_proto.set_wait_time(5)
        assert test_proto._next_write_time == 5
        test_proto.set_wait_time(2)
        assert test_proto._next_write_time == 5
        test_proto.set_wait_time(0)
        assert test_proto._next_write_time > 5

    #-----------------------------------------------------------------------
    def test_signal_raw_read(self):
        link = MockSerial()
        proto = IM.Protocol(link)
        seen = SlotRecorder()
        proto.signal_raw_read.connect(seen.slot)

        # A modem reply is reported too, never as a duplicate.
        link.signal_read.emit(link, bytes([0x02, 0x55]))
        assert seen.calls == [(bytes([0x02, 0x55]), False)]

        # An inbound message arriving in two reads, then a hop repeat of it.
        seen.calls.clear()
        link.signal_read.emit(link, STD_FRAME[:5])
        assert seen.calls == []
        link.signal_read.emit(link, STD_FRAME[5:])
        repeat = STD_FRAME[:8] + bytes([0x2a]) + STD_FRAME[9:]
        link.signal_read.emit(link, repeat)
        assert seen.calls == [(STD_FRAME, False), (repeat, True)]

    #-----------------------------------------------------------------------
    def test_inject(self, test_proto):
        seen = SlotRecorder()
        test_proto.signal_received.connect(seen.slot)

        assert test_proto.inject(STD_FRAME) is True
        assert len(seen.calls) == 1
        msg = seen.calls[0][0]
        assert isinstance(msg, Msg.InpStandard)
        assert msg.from_addr == IM.Address('0a.12.33')
        assert msg.cmd1 == 0x11

        # The same message again, as the modem or another receiver would
        # report it, is dropped by the normal duplicate check.
        assert test_proto.inject(STD_FRAME) is False
        assert len(seen.calls) == 1

        ext = (bytes([0x02, 0x51, 0x0a, 0x12, 0x44, 0x2b, 0x93, 0x07, 0x1a,
                      0x2f, 0x00]) + bytes(14))
        assert test_proto.inject(ext) is True
        assert isinstance(seen.calls[1][0], Msg.InpExtended)

    #-----------------------------------------------------------------------
    def test_inject_modem_copy_is_duplicate(self):
        link = MockSerial()
        proto = IM.Protocol(link)
        seen = SlotRecorder()
        proto.signal_received.connect(seen.slot)

        assert proto.inject(STD_FRAME) is True
        link.signal_read.emit(link, STD_FRAME)
        assert len(seen.calls) == 1

    #-----------------------------------------------------------------------
    def test_next_identical_message_after_injection(self, monkeypatch):
        # Injection must not swallow a later, real, identical message (a
        # second press) that the modem does hear.
        link = MockSerial()
        proto = IM.Protocol(link)
        seen = SlotRecorder()
        proto.signal_received.connect(seen.slot)
        now = [1000.0]
        monkeypatch.setattr(time, "time", lambda: now[0])

        assert proto.inject(STD_FRAME) is True
        now[0] += 1.5
        link.signal_read.emit(link, STD_FRAME)
        assert len(seen.calls) == 2

    #-----------------------------------------------------------------------
    @pytest.mark.parametrize("frame", [
        b"",
        bytes([0x50]) + STD_FRAME[1:],     # no start byte
        bytes([0x02, 0x62]) + STD_FRAME[2:],  # not an inbound message
        bytes([0x02, 0x6b, 0x00]),         # modem config command
        STD_FRAME[:-1],                    # short
        STD_FRAME + bytes([0x00]),         # trailing bytes
        bytes([0x02, 0x51]) + STD_FRAME[2:],  # extended flag, standard size
        ])
    def test_inject_rejects(self, test_proto, frame):
        seen = SlotRecorder()
        test_proto.signal_received.connect(seen.slot)
        assert test_proto.inject(frame) is False
        assert seen.calls == []

#===========================================================================


class MockSerial:
    def __init__(self):
        self.signal_read = IM.Signal()
        self.signal_wrote = IM.Signal()
        self.config = None

    def poll(self):
        pass

    def load_config(self, config):
        self.config = config


class SlotRecorder:
    # Signal slots are weak references, so the slot has to be a method of
    # an object the test holds on to.
    def __init__(self):
        self.calls = []

    def slot(self, *args):
        self.calls.append(args)
