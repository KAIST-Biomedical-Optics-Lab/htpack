"""Numcodecs-compatible adapter for HTPack 3D RI block compression."""
import numpy as np
from numcodecs.abc import Codec
from numcodecs.compat import ensure_bytes, ndarray_copy
from numcodecs.registry import register_codec
from . import codec


class HTPackCodec(Codec):
    """Numcodecs adapter wrapping HTPack Fourier and fast block codecs."""

    codec_id = "htpack"

    def __init__(self, mode: str = "fourier", epsilon: float = 1e-4, return_float32: bool = True):
        if mode not in codec.CODECS:
            raise ValueError(f"Compression mode must be fourier or fast, got {mode!r}")
        if not np.isfinite(epsilon) or not 0 < epsilon <= 0.1:
            raise ValueError("RI error budget must be finite and in (0, 0.1]")
        self.mode = str(mode)
        self.epsilon = float(epsilon)
        self.return_float32 = bool(return_float32)

    def encode(self, buf):
        arr = np.asarray(buf)
        while arr.ndim > 3 and arr.shape[0] == 1:
            arr = arr[0]
        if arr.ndim != 3:
            raise ValueError(f"Expected 3D block (or leading singleton axes), got shape {arr.shape}")
        if arr.dtype == np.dtype("<u2") or arr.dtype == np.uint16:
            u16 = np.ascontiguousarray(arr, dtype="<u2")
        elif np.issubdtype(arr.dtype, np.floating):
            scaled = np.rint(arr.astype("f8") * codec.SCALE_DENOMINATOR)
            if scaled.min(initial=0) < 0 or scaled.max(initial=0) > 65535:
                raise ValueError("Scaled float RI values exceed uint16 range [0, 65535]")
            u16 = np.ascontiguousarray(scaled, dtype="<u2")
        else:
            raise ValueError(f"Unsupported input dtype for HTPackCodec: {arr.dtype}")
        packet, _ = codec.encode_block(u16, epsilon=self.epsilon, mode=self.mode)
        return packet

    def decode(self, buf, out=None):
        raw = ensure_bytes(buf)
        restored_u16 = codec.decode_block(raw)
        result = codec.reader_values(restored_u16) if self.return_float32 else restored_u16
        return ndarray_copy(result, out)


def register_htpack_codecs():
    """Register HTPackCodec in the numcodecs codec registry."""
    register_codec(HTPackCodec)


register_htpack_codecs()
