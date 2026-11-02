"""Standalone v17 RI codec: float32 output, native-reference certification.

No uint16 grid conversion, scalar rounding guards, or research-script imports.
An encoder returns a packet only after checking its serialized reconstruction.
"""
import json
import math
import struct

import numpy as np
from numcodecs import BitRound, PCodec
from scipy import fft

from .codec import MAX_VOXELS, bands, compact, digest

MAGIC = b'HTFCF017'
MAX_HEADER = 1024 * 1024
CODER = PCodec(level=6)


class ErrorBudgetExceeded(ValueError):
    """The requested bound was not achieved; no scalar guard is inserted."""


def quality(reference, restored, epsilon):
    reference = np.asarray(reference)
    restored = np.asarray(restored)
    if reference.shape != restored.shape or not np.isfinite(reference).all() or not np.isfinite(restored).all():
        raise ValueError('Reference/output shape mismatch or non-finite values')
    error = restored.astype('f8') - reference.astype('f8')
    return dict(max_error=float(np.abs(error).max()), rmse=float(np.sqrt(np.mean(error**2))),
                bias=float(error.mean()), violations=int(np.count_nonzero(np.abs(error) > epsilon)))


def _predict(header, streams):
    shape = tuple(header['shape'])
    spectrum = np.empty((*shape[:-1], shape[-1]//2+1), dtype='c16')
    indices = bands(shape)
    for component in ('real', 'imag'):
        values = np.empty(spectrum.size, dtype='f8')
        for band, idx in enumerate(indices):
            values[idx] = streams[f'{component}{band}'].astype('f8') * header['coefficient_step']
        getattr(spectrum, component)[:] = values.reshape(spectrum.shape)
    return (fft.irfftn(spectrum, s=shape, norm='ortho', workers=1) + header['mean']).astype('<f4')


def _restore(header, streams):
    if header['mode'] == 'fast':
        return np.array(streams['base'], dtype='<f4', copy=True).reshape(header['shape'])
    prediction = _predict(header, streams)
    return (prediction.ravel().astype('f8') + header['residual_step'] *
            streams['residual'].astype('f8')).astype('<f4').reshape(header['shape'])


def encode(original, epsilon=1e-4, mode='fourier', *, reference=None, coefficient_step=None):
    original = np.asarray(original)
    if original.dtype.kind != 'f' or original.ndim != 3 or not 0 < original.size <= MAX_VOXELS:
        raise ValueError('Expected a nonempty floating RI block, at most 32x256x256 voxels')
    if not np.isfinite(original).all() or not math.isfinite(epsilon) or not 0 < epsilon <= .1:
        raise ValueError('Finite RI input and epsilon in (0, 0.1] are required')
    native = original if reference is None else np.asarray(reference)
    if native.dtype.kind != 'f' or native.shape != original.shape or not np.isfinite(native).all():
        raise ValueError('Native reference must be a finite floating array of the same shape')
    x = np.ascontiguousarray(original, dtype='<f4')
    if not np.isfinite(x).all():
        raise ValueError('Input is outside the float32 range')
    header = dict(format='htpack-ri-f32-v17', version=1, mode=mode, shape=list(x.shape),
                  output_dtype='<f4', input_dtype='<f4', epsilon=float(epsilon),
                  reference_dtype=native.dtype.str, reference_sha256=digest(native),
                  input_sha256=digest(x), pcodec_level=6, guard='none',
                  error_reference='native_before_float32_cast')
    if mode == 'fourier':
        step = 5*epsilon if coefficient_step is None else float(coefficient_step)
        if not math.isfinite(step) or step <= 0:
            raise ValueError('Fourier coefficient step must be finite and positive')
        mean = float(x.mean(dtype='f8'))
        spectrum = fft.rfftn(x.astype('f8') - mean, norm='ortho', workers=1)
        streams = {}
        for component in ('real', 'imag'):
            integers = np.rint(getattr(spectrum, component).ravel()/step)
            if not np.isfinite(integers).all():
                raise ValueError('Coefficient quantization overflow')
            integers = compact(integers)
            for band, idx in enumerate(bands(x.shape)):
                streams[f'{component}{band}'] = np.ascontiguousarray(integers[idx])
        header.update(mean=mean, coefficient_step=step, residual_step=2*.99*epsilon,
                      fft_norm='ortho', fft_workers=1, band_profile='radial4', prediction_dtype='<f4')
        prediction = _predict(header, streams)
        residual = np.rint((x.ravel().astype('f8') - prediction.ravel().astype('f8')) / header['residual_step'])
        if not np.isfinite(residual).all():
            raise ValueError('Residual quantization overflow')
        streams['residual'] = compact(residual)
    elif mode == 'fast':
        if coefficient_step is not None:
            raise ValueError('coefficient_step is only valid for Fourier mode')
        maximum = float(np.abs(x.astype('f8')).max())
        exponent = math.frexp(maximum)[1]-1 if maximum else 0
        bits = min(23, max(0, math.ceil(exponent-math.log2(2*epsilon)))) if maximum else 0
        streams = dict(base=np.frombuffer(BitRound(keepbits=bits).encode(x.ravel().copy()), dtype='<f4').copy())
        header.update(keepbits=bits)
    else:
        raise ValueError('Compression mode must be fourier or fast')
    restored = _restore(header, streams)
    audit = quality(native, restored, epsilon)
    if audit['violations']:
        raise ErrorBudgetExceeded(f"v17 native-reference bound failed: max_error={audit['max_error']:.17g}, "
                                  f"epsilon={epsilon:.17g}, violations={audit['violations']}")
    entries, payloads = [], []
    for name, values in streams.items():
        payload = bytes(CODER.encode(values)) if values.size else b''
        entries.append(dict(name=name, dtype=values.dtype.str, count=values.size, length=len(payload)))
        payloads.append(payload)
    header.update(streams=entries, decoded_sha256=digest(restored), quality=audit)
    raw = json.dumps(header, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    packet = MAGIC + struct.pack('<I', len(raw)) + raw + b''.join(payloads)
    decoded = decode(packet)
    if not np.array_equal(decoded.view('u4'), restored.view('u4')):
        raise ValueError('Serialized v17 reconstruction differs from certified output')
    return packet, header


def unpack(packet):
    if len(packet) < 12 or packet[:8] != MAGIC:
        raise ValueError('Unrecognized v17 RI packet')
    length = struct.unpack_from('<I', packet, 8)[0]
    if length > MAX_HEADER or length > len(packet)-12:
        raise ValueError('Invalid v17 header length')
    header = json.loads(packet[12:12+length])
    shape = header.get('shape', [])
    if len(shape) != 3 or any(type(n) is not int or n <= 0 for n in shape) or math.prod(shape) > MAX_VOXELS:
        raise ValueError('Invalid v17 block shape')
    if header.get('format') != 'htpack-ri-f32-v17' or header.get('version') != 1 or header.get('output_dtype') != '<f4':
        raise ValueError('Unsupported v17 packet format')
    epsilon = header.get('epsilon')
    if not isinstance(epsilon, (int, float)) or not math.isfinite(epsilon) or not 0 < epsilon <= .1:
        raise ValueError('Invalid v17 RI budget')
    if header.get('guard') != 'none' or header.get('pcodec_level') != 6:
        raise ValueError('Invalid v17 guard/coder contract')
    mode = header.get('mode')
    if mode == 'fourier':
        if (header.get('residual_step') != 2*.99*epsilon or header.get('fft_norm') != 'ortho' or
                header.get('fft_workers') != 1 or header.get('band_profile') != 'radial4' or
                header.get('prediction_dtype') != '<f4' or not math.isfinite(header.get('mean', float('nan'))) or
                not math.isfinite(header.get('coefficient_step', float('nan'))) or header['coefficient_step'] <= 0):
            raise ValueError('Invalid v17 Fourier parameters')
        expected = {f'{component}{k}': len(idx) for component in ('real', 'imag')
                    for k, idx in enumerate(bands(tuple(shape)))}
        expected['residual'] = math.prod(shape)
    elif mode == 'fast':
        if type(header.get('keepbits')) is not int or not 0 <= header['keepbits'] <= 23:
            raise ValueError('Invalid v17 BitRound parameters')
        expected = dict(base=math.prod(shape))
    else:
        raise ValueError('Unsupported v17 compression mode')
    streams, offset = {}, 12+length
    for entry in header['streams']:
        name, count, size, dtype = (entry[k] for k in ('name', 'count', 'length', 'dtype'))
        allowed = ('<f4',) if name == 'base' else ('<i2', '<i4', '<i8')
        if (name in streams or name not in expected or type(count) is not int or count != expected[name] or
                type(size) is not int or size < 0 or size > len(packet)-offset or dtype not in allowed):
            raise ValueError('Invalid v17 stream descriptor')
        raw = CODER.decode(packet[offset:offset+size]) if count else b''
        if memoryview(raw).nbytes != count*np.dtype(dtype).itemsize or bool(size) != bool(count):
            raise ValueError('Invalid v17 stream size')
        streams[name] = np.frombuffer(raw, dtype=dtype)
        offset += size
    if set(streams) != set(expected) or offset != len(packet):
        raise ValueError('Missing stream or trailing v17 bytes')
    return header, streams


def decode(packet):
    header, streams = unpack(packet)
    restored = _restore(header, streams)
    if digest(restored) != header['decoded_sha256']:
        raise ValueError('v17 decoded checksum mismatch or numerical backend difference')
    return restored


def verify(packet, reference=None):
    header, _ = unpack(packet)
    restored = decode(packet)
    result = dict(shape=header['shape'], dtype=restored.dtype.str, epsilon=header['epsilon'],
                  decoded_checksum_verified=True, reference_supplied=reference is not None)
    if reference is not None:
        reference = np.asarray(reference)
        if digest(reference) != header['reference_sha256'] or reference.dtype.str != header['reference_dtype']:
            raise ValueError('Native reference differs from encoding reference')
        result.update(quality(reference, restored, header['epsilon']))
        if result['violations']:
            raise ErrorBudgetExceeded('Decoded v17 packet fails native-reference bound')
    return result
