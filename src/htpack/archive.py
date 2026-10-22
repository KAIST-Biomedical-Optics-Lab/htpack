"""Self-contained HDF5 archive with a preserved TCF template and checked packets."""
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import h5py
import numpy as np
from . import __version__
from . import codec,tcf,parallel
from .platform_support import peak_rss,publish_no_overwrite

FORMAT="htfc-mvp-v1"


def _is_series(path):
    with h5py.File(path,"r") as f:return f.attrs.get("htfc_format")=="htfc-timeseries-v2"


def error_budget(epsilon):
    return dict(epsilon=epsilon,ri_error_scale=float(Decimal(str(epsilon))/Decimal("0.0001")))


def progress(message):
    print(message,file=sys.stderr,flush=True)


def file_stat(path):
    st=Path(path).stat()
    return dict(bytes=st.st_size,mtime_ns=st.st_mtime_ns)


@contextmanager
def atomic_output(path):
    path=Path(path).expanduser().resolve()
    if path.exists():raise ValueError(f"Refusing to overwrite existing path: {path}")
    if not path.parent.is_dir():raise ValueError(f"Output parent does not exist: {path.parent}")
    fd,name=tempfile.mkstemp(prefix="."+path.name+".",suffix=".partial",dir=path.parent)
    os.close(fd)
    temporary=Path(name)
    try:
        yield temporary
        # Windows FlushFileBuffers/_commit requires a writable handle.
        with temporary.open("r+b") as f:os.fsync(f.fileno())
        publish_no_overwrite(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def _header(f):
    if f.attrs.get("htfc_format")!=FORMAT or "manifest" not in f or "tcf" not in f:
        raise ValueError("Unrecognized or incomplete HTFC archive")
    if f["manifest"].size>32*2**20:raise ValueError("Manifest exceeds the MVP limit")
    m=json.loads(f["manifest"][()].tobytes())
    if m.get("format")!=FORMAT or m.get("codec") not in codec.CODECS.values():raise ValueError("Unsupported archive version/codec")
    expected_mode=next(k for k,v in codec.CODECS.items() if v==m["codec"])
    if m.get("mode",expected_mode)!=expected_mode:raise ValueError("Archive mode/codec mismatch")
    shape=m["source_schema"]["shape"]
    if len(shape)!=3 or any(type(n) is not int or not 0<n<=65536 for n in shape):
        raise ValueError("Invalid source shape")
    if m["block_shape"]!=list(tcf.BLOCK_SHAPE) or m["source_schema"]["ri_path"]!=tcf.RI_PATH:
        raise ValueError("Unsupported block layout")
    if list(f["tcf"+tcf.RI_PATH].shape)!=shape or f["tcf"+tcf.RI_PATH].id.get_storage_size()!=0:
        raise ValueError("TCF template RI placeholder is invalid")
    expected=list(tcf.block_layout(shape))
    if len(expected)!=len(m["blocks"]) or len(f["blocks"])!=len(expected):raise ValueError("Incomplete block coverage")
    for i,(row,(begin,size)) in enumerate(zip(m["blocks"],expected)):
        if row["index"]!=i or row["origin"]!=list(begin) or row["shape"]!=list(size):
            raise ValueError("Overlapping, missing or out-of-order block coverage")
    if tcf.metadata_digest(tcf.metadata(f["tcf"]))!=m["metadata_sha256"]:
        raise ValueError("TCF metadata checksum mismatch")
    return m


def packet(f,row):
    ds=f[f"blocks/{row['index']:06d}"]
    if ds.dtype!=np.dtype("u1") or ds.ndim!=1 or ds.size>128*2**20:
        raise ValueError("Invalid block payload descriptor")
    value=ds[()].tobytes()
    if hashlib.sha256(value).hexdigest()!=row["packet_sha256"] or len(value)!=row["packet_bytes"]:
        raise ValueError("Packet checksum mismatch")
    h,_=codec.unpack(value)
    if h["shape"]!=row["shape"] or h["decoded_sha256"]!=row["decoded_sha256"]:
        raise ValueError("Packet/manifest mismatch")
    return value,h


def encode(source,output,epsilon=1e-4,mode="fourier",concurrent_volumes="auto",memory_gib=None):
    start=time.perf_counter();source=Path(source).resolve();before=file_stat(source)
    if not np.isfinite(epsilon) or not 0<epsilon<=.1:raise ValueError("Invalid maximum RI error")
    if mode not in codec.CODECS:raise ValueError("Compression mode must be fourier or fast")
    with h5py.File(source,"r") as f:schema=tcf.validate_source(f)
    scheduling=parallel.plan(schema,mode,"encode",concurrent_volumes,memory_gib)
    if schema["volume_count"]>1 or scheduling["input_buffer"]=="tile":
        from . import series
        return series.encode(source,output,epsilon,mode,concurrent_volumes,memory_gib)
    with atomic_output(output) as temporary, h5py.File(source,"r") as src,h5py.File(temporary,"w") as dst:
        schema=tcf.validate_source(src);meta=tcf.metadata(src["/"])
        dst.attrs["htfc_format"]=FORMAT
        root=dst.create_group("tcf")
        progress("Preserving non-RI data and exact TCF attributes...")
        tcf.copy_tree(src["/"],root)
        if tcf.metadata(root)!=meta:raise ValueError("TCF metadata changed during template copy")
        keep=tcf.other_hashes(root)
        ds=src[tcf.RI_PATH];shape=ds.shape;blocks=[];native_hash=hashlib.sha256()
        dst.create_group("blocks")
        total=sum(1 for _ in tcf.block_layout(shape));native_bytes=ds.size*ds.dtype.itemsize
        guards=0;maximum=reader_maximum=0.;payload_bytes=0
        for z in range(0,shape[0],tcf.BLOCK_SHAPE[0]):
            slab=np.ascontiguousarray(ds[z:z+tcf.BLOCK_SHAPE[0]])
            native_hash.update(memoryview(slab).cast("B"))
            for begin,size in tcf.block_layout(slab.shape):
                actual=(z+begin[0],begin[1],begin[2])
                block=np.ascontiguousarray(slab[tcf.slices(begin,size)])
                blob,h=codec.encode_block(block,epsilon,mode)
                index=len(blocks);dst.create_dataset(f"blocks/{index:06d}",data=np.frombuffer(blob,dtype="u1"))
                row=dict(index=index,origin=list(actual),shape=list(size),packet_bytes=len(blob),
                    packet_sha256=hashlib.sha256(blob).hexdigest(),decoded_sha256=h["decoded_sha256"],
                    native_sha256=h["native_sha256"],guard_count=h["guard_count"],quality=h["quality"])
                blocks.append(row);payload_bytes+=len(blob);guards+=h["guard_count"]
                maximum=max(maximum,h["quality"]["max_RI_error"])
                reader_maximum=max(reader_maximum,h["quality"]["max_reader_float32_RI_error"])
                if len(blocks)%24==0 or len(blocks)==total:progress(f"Encoded {len(blocks)}/{total} blocks")
            del slab,block
        if file_stat(source)!=before:raise ValueError("Source changed during compression")
        m=dict(format=FORMAT,codec=codec.CODECS[mode],mode=mode,tool_version=__version__,
            profile="tcf-u16-mvp-v1" if mode=="fourier" else "tcf-u16-bitround-fast-v1",
            created_utc=datetime.now(timezone.utc).isoformat(),source_name=source.name,source_file=before,
            source_full_file_hashed=False,source_schema=schema,source_native_RI_sha256=native_hash.hexdigest(),
            metadata_sha256=tcf.metadata_digest(meta),metadata=meta,non_RI_dataset_sha256=keep,
            epsilon=epsilon,error_reference="native uint16/10000 AND public-reader float32(uint16)/float32(10000)",
            block_shape=list(tcf.BLOCK_SHAPE),blocks=blocks,native_RI_bytes=native_bytes,
            RI_packet_bytes=payload_bytes,guard_voxels=guards,max_RI_error=maximum,max_reader_float32_RI_error=reader_maximum,
            derived_cache_policy="Original MIP/thumbnails/annotations preserved without recomputation; they remain original-derived caches.",
            dependency_versions={n:importlib.metadata.version(n) for n in ("numpy","scipy","numcodecs","pcodec","h5py")},
            jobs=1,fft_workers=1,concurrency_plan=scheduling,encode_numerical_check="Every packet separately parsed and decoded before writing; final stored/reader RI guard checked.")
        raw=json.dumps(m,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
        dst.create_dataset("manifest",data=np.frombuffer(raw,dtype="u1"));dst.flush()
        _header(dst)
    output_bytes=Path(output).stat().st_size
    return dict(operation="encode",mode=mode,codec=codec.CODECS[mode],output=str(Path(output).resolve()),status="passed",source_bytes=before["bytes"],
        archive_bytes=output_bytes,whole_file_ratio=before["bytes"]/output_bytes,native_RI_bytes=native_bytes,
        RI_packet_bytes=payload_bytes,native_uint16_RI_ratio=native_bytes/payload_bytes,
        float32_equivalent_RI_ratio=2*native_bytes/payload_bytes,block_count=len(blocks),guard_voxels=guards,
        max_RI_error=maximum,max_reader_float32_RI_error=reader_maximum,violations=0,**error_budget(epsilon),
        volume_count=1,time_lapse=False,concurrency_plan=scheduling,
        total_wall_s=time.perf_counter()-start,peak_process_RSS_bytes=peak_rss(),
        timing_scope="Complete command: file I/O, metadata/non-RI copy, native guards, packet redecoding and checksums included; process startup excluded.")


def decode(source,output,concurrent_volumes="auto",memory_gib=None):
    if _is_series(source):
        from . import series
        return series.decode(source,output,concurrent_volumes,memory_gib)
    start=time.perf_counter()
    with h5py.File(source,"r") as src:
        m=_header(src)
        scheduling=parallel.plan(m["source_schema"],m.get("mode","fourier"),"decode",concurrent_volumes,memory_gib)
        if tcf.other_hashes(src["tcf"])!=m["non_RI_dataset_sha256"]:raise ValueError("Non-RI preservation checksum mismatch")
        with atomic_output(output) as temporary,h5py.File(temporary,"w") as dst:
            tcf.copy_tree(src["tcf"],dst["/"])
            ds=dst[tcf.RI_PATH];index=0
            for row in m["blocks"]:
                blob,h=packet(src,row)
                if h["epsilon"]!=m["epsilon"] or h["codec"]!=m["codec"]:raise ValueError("Inconsistent RI error budget/codec")
                restored=codec.decode_block(blob)
                ds[tcf.slices(row["origin"],row["shape"])]=restored
                index+=1
                if index%24==0 or index==len(m["blocks"]):progress(f"Decoded {index}/{len(m['blocks'])} blocks")
            dst.flush()
            if tcf.metadata(dst["/"])!=m["metadata"]:raise ValueError("Restored TCF metadata mismatch")
            if tcf.other_hashes(dst["/"])!=m["non_RI_dataset_sha256"]:raise ValueError("Restored non-RI data mismatch")
            tcf.validate_source(dst)
    return dict(operation="decode",mode=m.get("mode","fourier"),codec=m["codec"],status="passed",output=str(Path(output).resolve()),restored_bytes=Path(output).stat().st_size,
        block_count=len(m["blocks"]),decoded_hashes_checked=True,metadata_preserved=True,non_RI_data_preserved=True,
        original_required=False,lossy_RI=True,derived_cache_policy=m["derived_cache_policy"],**error_budget(m["epsilon"]),
        volume_count=1,time_lapse=False,concurrency_plan=scheduling,
        total_wall_s=time.perf_counter()-start,peak_process_RSS_bytes=peak_rss(),
        timing_scope="Complete command including file I/O, metadata/non-RI copy/checksums and decoded hash checks; startup excluded.")


def inspect(path):
    if _is_series(path):
        from . import series
        return series.inspect(path)
    with h5py.File(path,"r") as f:
        if f.attrs.get("htfc_format")==FORMAT:
            m=_header(f)
            return dict(format="HTFC",format_version=FORMAT,file_bytes=Path(path).stat().st_size,
                source_name=m["source_name"],source_schema=m["source_schema"],profile=m["profile"],mode=m.get("mode","fourier"),codec=m["codec"],
                block_count=len(m["blocks"]),**error_budget(m["epsilon"]),recorded_max_RI_error=m["max_RI_error"],
                recorded_max_reader_float32_RI_error=m["max_reader_float32_RI_error"],guard_voxels=m["guard_voxels"],
                metadata_checked=True,payload_checksums_checked=False,
                derived_cache_policy=m["derived_cache_policy"],reference_required_for_new_error_measurement=True)
        return dict(file_bytes=Path(path).stat().st_size,**tcf.inspect_tcf(f))


def verify(path,reference=None,restored=None):
    if _is_series(path):
        from . import series
        return series.verify(path,reference,restored)
    start=time.perf_counter()
    with ExitStack() as stack:
        f=stack.enter_context(h5py.File(path,"r"));m=_header(f)
        if tcf.other_hashes(f["tcf"])!=m["non_RI_dataset_sha256"]:raise ValueError("Non-RI archive checksum mismatch")
        ref=stack.enter_context(h5py.File(reference,"r")) if reference else None
        out=stack.enter_context(h5py.File(restored,"r")) if restored else None
        for label,obj in (("reference",ref),("restored",out)):
            if obj is not None:
                tcf.validate_source(obj)
                if tcf.metadata(obj["/"])!=m["metadata"]:raise ValueError(f"{label} TCF metadata mismatch")
                if tcf.other_hashes(obj["/"])!=m["non_RI_dataset_sha256"]:raise ValueError(f"{label} non-RI data mismatch")
        maximum=reader_maximum=0.;violations=0;index=0;native_hash=hashlib.sha256();changed=0
        shape=m["source_schema"]["shape"]
        for z in range(0,shape[0],tcf.BLOCK_SHAPE[0]):
            slabshape=(min(tcf.BLOCK_SHAPE[0],shape[0]-z),*shape[1:])
            original=ref[tcf.RI_PATH][z:z+slabshape[0]] if ref is not None else None
            written=out[tcf.RI_PATH][z:z+slabshape[0]] if out is not None else None
            if original is not None:native_hash.update(memoryview(np.ascontiguousarray(original)).cast("B"))
            for begin,size in tcf.block_layout(slabshape):
                row=m["blocks"][index];blob,h=packet(f,row);y=codec.decode_block(blob)
                if h["epsilon"]!=m["epsilon"] or h["codec"]!=m["codec"]:raise ValueError("Inconsistent packet epsilon/codec")
                key=tcf.slices(begin,size)
                if written is not None and not np.array_equal(written[key],y):raise ValueError("Written TCF differs from decoded packet")
                if original is not None:
                    x=np.ascontiguousarray(original[key])
                    if codec.digest(x)!=row["native_sha256"]:raise ValueError("Reference is not the encoded native source")
                    q=codec.quality(x,y,m["epsilon"])
                    maximum=max(maximum,q["max_RI_error"]);reader_maximum=max(reader_maximum,q["max_reader_float32_RI_error"])
                    violations+=q["violations"];changed+=int(np.count_nonzero(x!=y))
                index+=1
                if index%48==0 or index==len(m["blocks"]):progress(f"Verified {index}/{len(m['blocks'])} blocks")
        if ref is not None and native_hash.hexdigest()!=m["source_native_RI_sha256"]:raise ValueError("Native RI dataset hash mismatch")
        if violations:raise ValueError(f"Final RI error budget violated at {violations} voxels")
    return dict(operation="verify",mode=m.get("mode","fourier"),codec=m["codec"],status="passed",block_count=index,payload_and_decoded_hashes_checked=True,
        metadata_preserved=True,non_RI_data_preserved=True,reference_supplied=ref is not None,restored_file_checked=out is not None,**error_budget(m["epsilon"]),
        max_RI_error=maximum if ref is not None else None,max_reader_float32_RI_error=reader_maximum if ref is not None else None,
        violations=violations if ref is not None else None,changed_RI_voxels=changed if ref is not None else None,
        error_check="All voxels against the supplied native source" if ref is not None else "Checksums/hash reproduction only; RI error not remeasured without native source",
        total_wall_s=time.perf_counter()-start,peak_process_RSS_bytes=peak_rss())
