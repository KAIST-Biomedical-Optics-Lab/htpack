"""Fourier and fast BitRound/Pcodec packets with identical final-output guards."""
from functools import lru_cache
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import math
import struct
import numpy as np
from numcodecs import BitRound,PCodec
from scipy import fft

MAGIC=b"HTFCB001"
SCALE_DENOMINATOR=10000
MAX_VOXELS=32*256*256
CODER=PCodec(level=6)
CODECS={"fourier":"fourier-ri-u16-v1","fast":"bitround-pcodec-ri-u16-v1"}


def digest(x):
    return hashlib.sha256(memoryview(np.ascontiguousarray(x)).cast("B")).hexdigest()


def allowed_codes(epsilon):
    value=Decimal(str(epsilon))*SCALE_DENOMINATOR
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


@lru_cache(maxsize=4)
def bands(shape):
    axes=[2*np.abs(np.fft.fftfreq(n)) for n in shape[:-1]]+[2*np.fft.rfftfreq(shape[-1])]
    radius=axes[0][:,None,None]**2+axes[1][None,:,None]**2+axes[2][None,None,:]**2
    labels=np.digitize(radius.ravel(),[.25**2,.5**2,1.])
    return tuple(np.flatnonzero(labels==k).astype("u4") for k in range(4))


def compact(values):
    lo=int(values.min(initial=0)); hi=int(values.max(initial=0))
    for dtype in ("<i2","<i4","<i8"):
        bounds=np.iinfo(dtype)
        if bounds.min<=lo and hi<=bounds.max:
            return np.ascontiguousarray(values,dtype=dtype)
    raise ValueError("Integer stream exceeds int64")


def reader_values(codes):
    # Exact public TCFile conversion: uint16 -> float32 -> divide by 1e4.
    return codes.astype("f4")/np.float32(SCALE_DENOMINATOR)


def quality(original,restored,epsilon):
    delta=restored.astype("i8")-original.astype("i8")
    maximum=int(np.abs(delta).max(initial=0))
    reader_error=np.abs(reader_values(restored).astype("f8")-reader_values(original).astype("f8"))
    return dict(max_code_error=maximum,max_RI_error=maximum/SCALE_DENOMINATOR,
        max_reader_float32_RI_error=float(reader_error.max(initial=0)),
        violations=int(np.count_nonzero((np.abs(delta)>allowed_codes(epsilon))|(reader_error>epsilon))))


def _predict(header,streams):
    shape=tuple(header["shape"])
    specshape=(*shape[:-1],shape[-1]//2+1)
    spectrum=np.empty(specshape,dtype="c16")
    idx=bands(shape)
    for component in ("real","imag"):
        flat=getattr(spectrum,component).reshape(-1)
        for k,positions in enumerate(idx):
            flat[positions]=streams[f"{component}{k}"].astype("f8")*header["coefficient_step"]
    return (fft.irfftn(spectrum,s=shape,norm="ortho",workers=1)+header["mean"]).astype("f4")


def _restore(header,streams):
    if header["codec"]==CODECS["fast"]:
        value=streams["base"].astype("f8")
    else:
        prediction=_predict(header,streams)
        value=prediction.ravel().astype("f8")+header["residual_step"]*streams["residual"].astype("f8")
    rounded=np.rint(value*SCALE_DENOMINATOR)
    if not np.isfinite(rounded).all():
        raise ValueError("Non-finite reconstructed stored code")
    codes=rounded.astype("i8")
    index=streams["guard_index"]
    if index.size and (int(index.max())>=codes.size or np.any(index[1:]<=index[:-1])):
        raise ValueError("Invalid or unsorted guard index")
    if streams["guard_values"].size!=index.size:
        raise ValueError("Guard stream length mismatch")
    codes[index]=streams["guard_values"]
    if codes.min(initial=0)<0 or codes.max(initial=0)>65535:
        raise ValueError("Stored code exceeds uint16; no clipping is permitted")
    return codes.astype("<u2").reshape(header["shape"])


def _encode_fourier(original,epsilon=1e-4):
    original=np.ascontiguousarray(original,dtype="<u2")
    if original.ndim!=3 or not 0<original.size<=MAX_VOXELS:
        raise ValueError("Expected a nonempty 3D uint16 block of at most 32x256x256 voxels")
    if not np.isfinite(epsilon) or epsilon<=0 or epsilon>.1:
        raise ValueError("RI error budget must be finite and in (0,0.1]")
    x=original.astype("f8")/SCALE_DENOMINATOR
    mean=float(x.mean(dtype="f8")); step=5*epsilon
    spec=fft.rfftn(x-mean,norm="ortho",workers=1)
    streams={}
    for component in ("real","imag"):
        values=compact(np.rint(getattr(spec,component).ravel()/step))
        for k,idx in enumerate(bands(original.shape)):
            streams[f"{component}{k}"]=np.ascontiguousarray(values[idx])
    h=dict(codec="fourier-ri-u16-v1",shape=list(original.shape),stored_dtype="<u2",
        ri_scale_denominator=SCALE_DENOMINATOR,epsilon=epsilon,mean=mean,coefficient_step=step,
        residual_step=2*epsilon,fft_norm="ortho",fft_dtype="float64/complex128",prediction_dtype="float32",
        band_profile="radial4-nonphysical",band_edges=[.25,.5,1.],fft_workers=1,
        guard="exact_native_uint16_after_storage_and_reader_conversion")
    p=_predict(h,streams)
    r=compact(np.rint((x.ravel()-p.ravel().astype("f8"))/h["residual_step"]))
    candidate=np.rint((p.ravel().astype("f8")+h["residual_step"]*r.astype("f8"))*SCALE_DENOMINATOR).astype("i8")
    safe=(candidate>=0)&(candidate<=65535)
    # No wrap or clipping: invalid candidates are always exact-native exceptions.
    candidate_reader=(candidate.astype("f4")/np.float32(SCALE_DENOMINATOR)).astype("f8")
    native_reader=reader_values(original).ravel().astype("f8")
    bad=(~safe)|(np.abs(candidate-original.ravel().astype("i8"))>allowed_codes(epsilon))
    bad|=np.abs(candidate_reader-native_reader)>epsilon
    streams.update(residual=r,guard_index=np.flatnonzero(bad).astype("<u4"),
                   guard_values=np.ascontiguousarray(original.ravel()[bad],dtype="<u2"))
    restored=_restore(h,streams)
    q=quality(original,restored,epsilon)
    if q["violations"]:
        raise ValueError("Final stored/reader RI guard verification failed")
    entries=[]; payloads=[]
    for name,values in streams.items():
        payload=bytes(CODER.encode(values)) if values.size else b""
        entries.append(dict(name=name,dtype=values.dtype.str,count=values.size,length=len(payload)))
        payloads.append(payload)
    h.update(streams=entries,decoded_sha256=digest(restored),native_sha256=digest(original),
             guard_count=int(np.count_nonzero(bad)),quality=q)
    raw=json.dumps(h,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    packet=MAGIC+struct.pack("<I",len(raw))+raw+b"".join(payloads)
    if digest(decode_block(packet))!=h["decoded_sha256"]:
        raise ValueError("Serialized packet differs from the checked reconstruction")
    return packet,h


def encode_block(original,epsilon=1e-4,mode="fourier"):
    if mode=="fourier":return _encode_fourier(original,epsilon)
    if mode!="fast":raise ValueError("Compression mode must be fourier or fast")
    original=np.ascontiguousarray(original,dtype="<u2")
    if original.ndim!=3 or not 0<original.size<=MAX_VOXELS:
        raise ValueError("Expected a nonempty 3D uint16 block of at most 32x256x256 voxels")
    if not np.isfinite(epsilon) or not 0<epsilon<=.1:
        raise ValueError("RI error budget must be finite and in (0,0.1]")
    x=reader_values(original)
    maximum=float(x.max(initial=0))
    exponent=math.frexp(maximum)[1]-1 if maximum else 0
    keepbits=min(23,max(0,math.ceil(exponent-math.log2(2*epsilon)))) if maximum else 0
    # One deterministic analytic setting, no candidate search or FFT.
    rounded=BitRound(keepbits=keepbits).encode(x.copy())
    base=np.frombuffer(rounded,dtype="<f4").copy()
    candidate=np.rint(base.astype("f8")*SCALE_DENOMINATOR).astype("i8")
    bad=(candidate<0)|(candidate>65535)
    bad|=np.abs(candidate-original.ravel().astype("i8"))>allowed_codes(epsilon)
    candidate_reader=(candidate.astype("f4")/np.float32(SCALE_DENOMINATOR)).astype("f8")
    bad|=np.abs(candidate_reader-x.ravel().astype("f8"))>epsilon
    streams=dict(base=base,guard_index=np.flatnonzero(bad).astype("<u4"),
        guard_values=np.ascontiguousarray(original.ravel()[bad],dtype="<u2"))
    h=dict(codec=CODECS["fast"],shape=list(original.shape),stored_dtype="<u2",
        ri_scale_denominator=SCALE_DENOMINATOR,epsilon=epsilon,keepbits=keepbits,
        bitround_input_dtype="<f4",pcodec_level=6,
        keepbits_rule="ceil(floor(log2(max_RI))-log2(2*epsilon)), clamped to [0,23]; final-output guard",
        guard="exact_native_uint16_after_storage_and_reader_conversion")
    restored=_restore(h,streams);q=quality(original,restored,epsilon)
    if q["violations"]:raise ValueError("Fast final stored/reader RI guard verification failed")
    entries=[];payloads=[]
    for name,values in streams.items():
        payload=bytes(CODER.encode(values)) if values.size else b""
        entries.append(dict(name=name,dtype=values.dtype.str,count=values.size,length=len(payload)))
        payloads.append(payload)
    h.update(streams=entries,decoded_sha256=digest(restored),native_sha256=digest(original),
        guard_count=int(np.count_nonzero(bad)),quality=q)
    raw=json.dumps(h,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
    packet=MAGIC+struct.pack("<I",len(raw))+raw+b"".join(payloads)
    if digest(decode_block(packet))!=h["decoded_sha256"]:
        raise ValueError("Serialized fast packet differs from checked reconstruction")
    return packet,h


def unpack(packet):
    if len(packet)<12 or packet[:8]!=MAGIC:
        raise ValueError("Unrecognized block format")
    length=struct.unpack_from("<I",packet,8)[0]
    if length>1024*1024 or length>len(packet)-12:
        raise ValueError("Invalid header size")
    h=json.loads(packet[12:12+length]); shape=h.get("shape",[])
    if len(shape)!=3 or any(type(n) is not int or n<=0 for n in shape) or np.prod(shape,dtype="i8")>MAX_VOXELS:
        raise ValueError("Invalid block shape")
    if h.get("codec") not in CODECS.values() or h.get("stored_dtype")!="<u2" or h.get("ri_scale_denominator")!=SCALE_DENOMINATOR:
        raise ValueError("Unsupported scalar codec")
    if not np.isfinite(h["epsilon"]) or not 0<h["epsilon"]<=.1:
        raise ValueError("Invalid RI error budget")
    if h["codec"]==CODECS["fourier"]:
        if h.get("band_profile")!="radial4-nonphysical" or h.get("fft_norm")!="ortho" or h.get("prediction_dtype")!="float32":
            raise ValueError("Unsupported reconstruction convention")
        if h["residual_step"]!=2*h["epsilon"] or h["coefficient_step"]!=5*h["epsilon"] or not np.isfinite(h["mean"]):
            raise ValueError("Invalid Fourier quantization parameters")
        positions=bands(tuple(shape));counts={f"{c}{k}":len(idx) for c in ("real","imag") for k,idx in enumerate(positions)}
        counts.update(residual=int(np.prod(shape)))
    else:
        if type(h.get("keepbits")) is not int or not 0<=h["keepbits"]<=23 or h.get("bitround_input_dtype")!="<f4" or h.get("pcodec_level")!=6:
            raise ValueError("Invalid BitRound/Pcodec parameters")
        counts=dict(base=int(np.prod(shape)))
    counts.update(guard_index=h["guard_count"],guard_values=h["guard_count"])
    if not isinstance(h["guard_count"],int) or not 0<=h["guard_count"]<=np.prod(shape):
        raise ValueError("Invalid guard count")
    streams={};offset=12+length
    for entry in h["streams"]:
        name=entry["name"];count=entry["count"];size=entry["length"];dtype=entry["dtype"]
        if name in streams or name not in counts or count!=counts[name] or not isinstance(size,int) or size<0 or size>len(packet)-offset:
            raise ValueError("Invalid or duplicate stream descriptor")
        if dtype not in ("<i2","<i4","<i8","<u2","<u4","<f4"):
            raise ValueError("Unsupported integer stream dtype")
        if name=="base" and dtype!="<f4" or name not in ("base","guard_index","guard_values") and dtype not in ("<i2","<i4","<i8"):
            raise ValueError("Unexpected numeric stream dtype")
        if name=="guard_index" and dtype!="<u4" or name=="guard_values" and dtype!="<u2":
            raise ValueError("Invalid guard dtype")
        raw=CODER.decode(packet[offset:offset+size]) if count else b""
        if memoryview(raw).nbytes!=count*np.dtype(dtype).itemsize or bool(size)!=bool(count):
            raise ValueError("Decoded stream length mismatch")
        streams[name]=np.frombuffer(raw,dtype=dtype)
        offset+=size
    if set(streams)!=set(counts) or offset!=len(packet):
        raise ValueError("Missing stream or trailing data")
    return h,streams


def decode_block(packet):
    h,streams=unpack(packet)
    restored=_restore(h,streams)
    if digest(restored)!=h["decoded_sha256"]:
        raise ValueError("Decoded array hash mismatch; unsupported numerical backend or damaged packet")
    return restored
