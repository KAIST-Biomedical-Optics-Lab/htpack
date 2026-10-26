"""Tests for the numcodecs-compatible HTPackCodec adapter."""
import numpy as np
import numcodecs
import pytest
from htpack import HTPackCodec, codec


@pytest.mark.parametrize("mode", ["fourier", "fast"])
@pytest.mark.parametrize("leading", [(), (1, 1)])
def test_numcodecs_float32_roundtrip(mode, leading):
    rng = np.random.default_rng(42)
    u16 = rng.integers(13300, 13800, size=(8, 32, 32), dtype="<u2")
    # Simulate dithering noise within (-5e-5, +5e-5) as in OME-Zarr conversion
    noise = rng.uniform(-4.9e-5, 4.9e-5, size=u16.shape).astype("<f4")
    f32 = (u16.astype("<f4") / np.float32(10000.0)) + noise
    if leading:
        f32 = f32.reshape(*leading, *u16.shape)

    c = HTPackCodec(mode=mode, epsilon=1e-4, return_float32=True)
    encoded = c.encode(f32)
    assert isinstance(encoded, bytes) and len(encoded) > 0

    out = np.empty_like(f32)
    decoded = c.decode(encoded, out=out)
    assert decoded is out
    assert out.shape == f32.shape and out.dtype == np.float32

    true_ri = codec.reader_values(u16)
    max_err_vs_true = float(np.max(np.abs(out.reshape(u16.shape) - true_ri)))
    assert max_err_vs_true <= 1e-4


@pytest.mark.parametrize("mode", ["fourier", "fast"])
def test_numcodecs_uint16_roundtrip_and_registry(mode):
    u16 = np.resize(np.arange(13200, 13600, dtype="<u2"), 4 * 16 * 16).reshape(4, 16, 16)
    cfg = {"id": "htpack", "mode": mode, "epsilon": 2e-4, "return_float32": False}
    c = numcodecs.get_codec(cfg)
    assert isinstance(c, HTPackCodec)
    assert c.get_config() == cfg

    encoded = c.encode(u16)
    decoded = c.decode(encoded)
    assert decoded.dtype == np.dtype("<u2") and decoded.shape == u16.shape
    q = codec.quality(u16, decoded, 2e-4)
    assert q["violations"] == 0
