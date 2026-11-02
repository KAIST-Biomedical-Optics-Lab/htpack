"""RI contract regression: no integer-grid conversion or hidden error fallback."""
import struct

import numpy as np
import pytest

from htpack import codec, ri
from htpack.cli import main


@pytest.mark.parametrize('shape', [(1, 1, 1), (5, 7, 9), (6, 8, 10)])
@pytest.mark.parametrize('mode', ['fourier', 'fast'])
def test_continuous_native_contract(shape, mode):
    native = np.random.default_rng(42).uniform(1.33, 1.37, shape)
    packet, header = codec.encode_block(native, mode=mode)
    restored = codec.decode_block(packet)
    assert restored.dtype == np.dtype('<f4')
    assert np.max(np.abs(restored.astype('f8')-native)) <= 1e-4
    assert header['guard'] == 'none'
    assert ri.verify(packet, native)['violations'] == 0
    assert codec.quality(native,restored,1e-4)['violations'] == 0
    assert codec.unpack(packet)[0]['output_dtype'] == '<f4'
    # Continuous outputs are not forced onto the 1e-4 storage grid.
    if restored.size > 1:
        assert np.any(np.abs(restored.astype('f8')*10000-np.rint(restored.astype('f8')*10000)) > .01)


def test_explicit_native_reference_and_float32_working_input():
    native = np.random.default_rng(11).uniform(1.33, 1.37, (3, 5, 7))
    x = native.astype('f4')
    a, _ = codec.encode_block(native)
    b, _ = codec.encode_block(x, reference=native)
    assert a == b
    with pytest.raises(ValueError, match='reference differs'):
        ri.verify(a, x)


def test_precision_floor_fails_without_guard_or_packet(tmp_path, capsys):
    native = np.full((3, 5, 7), 1.33000004, dtype='f8')
    with pytest.raises(ri.ErrorBudgetExceeded):
        codec.encode_block(native, epsilon=1e-10)
    source, dest = tmp_path/'native.npy', tmp_path/'failed.htf'
    np.save(source, native)
    assert main(['ri-encode', str(source), '-o', str(dest), '--max-ri-error', '1e-10']) == 1
    assert not dest.exists()


def test_cli_roundtrip_and_no_overwrite(tmp_path, capsys):
    source, packet, dest = tmp_path/'native.npy', tmp_path/'block.htf', tmp_path/'output.npy'
    native = np.random.default_rng(22).normal(1.34, .003, (3, 5, 7))
    np.save(source, native)
    assert main(['ri-encode', str(source), '-o', str(packet)]) == 0
    assert main(['ri-decode', str(packet), '-o', str(dest)]) == 0
    assert main(['ri-verify', str(packet), '--reference', str(source)]) == 0
    assert np.max(np.abs(np.load(dest).astype('f8')-native)) <= 1e-4
    original = dest.read_bytes()
    assert main(['ri-decode', str(packet), '-o', str(dest)]) == 1
    assert dest.read_bytes() == original
    damaged = bytearray(packet.read_bytes())
    damaged[12+struct.unpack_from('<I', damaged, 8)[0]+10] ^= 1
    with pytest.raises((ValueError, RuntimeError)):
        ri.decode(damaged)


@pytest.mark.parametrize('epsilon', [0, -1, float('nan'), float('inf')])
def test_invalid_budget(epsilon):
    with pytest.raises(ValueError):
        ri.encode(np.ones((2, 3, 4), dtype='f4'), epsilon)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        ri.encode(np.ones((2, 3, 4), dtype='u2'))
    with pytest.raises(ValueError):
        ri.encode(np.full((2, 3, 4), float('nan')))
    with pytest.raises(ValueError):
        ri.encode(np.ones((2, 3, 4)), reference=np.ones((2, 3, 5)))
