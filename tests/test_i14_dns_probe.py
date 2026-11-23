"""DNS wire fixtures: invalid replies must never become successful soak samples."""
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch


def load_probe():
    path = Path(__file__).resolve().parents[1] / 'scripts/i14-dns-soak.py'
    spec = importlib.util.spec_from_file_location('probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SERVER = '192.0.2.10'
    return module


probe = load_probe()
QUESTION = b'\x0ai14-health\x07invalid\0\0\x01\0\x01'
ANSWER = b'\xc0\x0c\0\x01\0\x01\0\0\0\x3c\0\x04\xc0\0\x02\x01'


def reply(*, flags=0x8180, ident=0x1234, answers=1, answer=ANSWER, question=QUESTION):
    return struct.pack('>HHHHHH', ident, flags, 1, answers, 0, 0) + question + answer


class Socket:
    def __init__(self, packets):
        self.packets = iter(packets)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def settimeout(self, value):
        pass

    def connect(self, address):
        pass

    def send(self, data):
        return len(data)

    def sendto(self, data, address):
        return len(data)

    def sendall(self, data):
        pass

    def recv(self, count):
        # Bound an old EOF loop in the RED test without leaving a hanging test.
        try:
            return next(self.packets)
        except StopIteration:
            raise TimeoutError('fixture exhausted after EOF')


class ProbeTests(unittest.TestCase):
    def query(self, packets, tcp=False, name='i14-health.invalid'):
        sock = Socket(packets)
        with patch.object(probe.socket, 'socket', return_value=sock), \
             patch.object(probe.socket, 'create_connection', return_value=sock), \
             patch.object(probe.random, 'randint', return_value=0x1234):
            return probe.query(name, tcp=tcp).split(',')[0]

    def test_health_answer(self):
        self.assertEqual(self.query([reply()]), 'NOERROR')

    def test_empty_noerror_is_not_health(self):
        self.assertEqual(self.query([reply(answers=0, answer=b'')]), 'ERROR:ValueError')

    def test_wrong_health_address(self):
        self.assertEqual(self.query([reply(answer=ANSWER[:-1] + b'\x02')]), 'ERROR:ValueError')

    def test_unrelated_owner_with_expected_health_address_is_rejected(self):
        answer = b'\x05other\x07invalid\0' + ANSWER[2:]
        self.assertEqual(self.query([reply(answer=answer)]), 'ERROR:ValueError')

    def test_self_referencing_owner_pointer_is_rejected(self):
        self.assertEqual(self.query([reply(answer=b'\xc0\x24' + ANSWER[2:])]), 'ERROR:ValueError')

    def test_out_of_bounds_owner_pointer_is_rejected(self):
        self.assertEqual(self.query([reply(answer=b'\xff\xff' + ANSWER[2:])]), 'ERROR:ValueError')

    def test_nested_owner_compression_is_accepted(self):
        answers = ANSWER + b'\xc0\x24' + ANSWER[2:]
        self.assertEqual(self.query([reply(answers=2, answer=answers)]), 'NOERROR')

    def test_owner_names_are_case_insensitive(self):
        answer = b'\x0aI14-HEALTH\x07INVALID\0' + ANSWER[2:]
        self.assertEqual(self.query([reply(answer=answer)]), 'NOERROR')

    def test_real_name_probe_rejects_an_unrelated_a_record(self):
        answer = b'\x05other\x07invalid\0' + ANSWER[2:]
        question = b'\x05alias\x07invalid\0\0\x01\0\x01'
        self.assertEqual(self.query([reply(question=question, answer=answer)], name='alias.invalid'), 'ERROR:ValueError')

    def test_real_name_probe_accepts_a_cname_chain(self):
        question = b'\x05alias\x07invalid\0\0\x01\0\x01'
        target = b'\x06target\x07invalid\0'
        cname = b'\xc0\x0c\0\x05\0\x01\0\0\0\x3c\0\x10' + target
        answer = target + ANSWER[2:]
        self.assertEqual(self.query([reply(question=question, answers=2, answer=cname + answer)], name='alias.invalid'), 'NOERROR')

    def test_wrong_transaction(self):
        self.assertEqual(self.query([reply(ident=0x5678)]), 'ERROR:ValueError')

    def test_query_packet_is_not_a_response(self):
        self.assertEqual(self.query([reply(flags=0x0100)]), 'ERROR:ValueError')

    def test_truncated_udp_reply(self):
        self.assertEqual(self.query([reply(flags=0x8380)]), 'ERROR:ValueError')

    def test_incomplete_answer(self):
        self.assertEqual(self.query([reply(answer=ANSWER[:-1])]), 'ERROR:ValueError')

    def test_tcp_fragmented_length_and_body(self):
        data = reply()
        size = struct.pack('>H', len(data))
        self.assertEqual(self.query([size[:1], size[1:], data[:7], data[7:]], tcp=True), 'NOERROR')

    def test_tcp_eof_during_body_is_terminal(self):
        self.assertEqual(self.query([b'\0\x3c', b'\x12\x34', b'', b''], tcp=True), 'ERROR:EOFError')

    def test_tcp_eof_during_length_is_terminal(self):
        self.assertEqual(self.query([b''], tcp=True), 'ERROR:EOFError')

    def test_servfail_without_answer_is_preserved(self):
        self.assertEqual(self.query([reply(flags=0x8182, answers=0, answer=b'')]), 'SERVFAIL')

    def test_ad_flag_is_preserved(self):
        self.assertEqual(self.query([reply(flags=0x81A0)]), 'NOERROR+AD')

    def test_tcp_deadline_is_shared_across_fragments(self):
        with patch.object(probe.time, 'monotonic', side_effect=[0, 0, 4, 6, 6]):
            self.assertEqual(self.query([b'\0', b'\x3c', b'\x12'], tcp=True), 'ERROR:TimeoutError')


if __name__ == '__main__':
    unittest.main()
