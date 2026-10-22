"""Time-series HTFC v2: independent guarded packets, bounded volume jobs and exact TCF template."""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
from threading import RLock
import time
import h5py
import numpy as np
from . import __version__, codec, parallel, tcf
from .archive import atomic_output, error_budget, file_stat, peak_rss, progress

FORMAT="htfc-timeseries-v2"
MAX_JSON=32*2**20
CACHE_POLICY="Original MIP/thumbnails/annotations preserved without recomputation; they remain original-derived caches."


def raw_json(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()


def write_json(root,name,value):
    raw=raw_json(value)
    if len(raw)>MAX_JSON:raise ValueError("Manifest exceeds the per-dataset size limit")
    root.create_dataset(name,data=np.frombuffer(raw,dtype="u1"))
    return hashlib.sha256(raw).hexdigest()


def read_json(ds):
    if ds.dtype!=np.dtype("u1") or ds.ndim!=1 or ds.size>MAX_JSON:
        raise ValueError("Invalid time-series manifest dataset")
    raw=ds[()].tobytes()
    return json.loads(raw),hashlib.sha256(raw).hexdigest()


def header(f):
    if f.attrs.get("htfc_format")!=FORMAT or "tcf" not in f or "volumes" not in f:
        raise ValueError("Invalid time-series archive")
    m,_=read_json(f["manifest"])
    if m["format"]!=FORMAT or m["codec"]!=codec.CODECS.get(m["mode"]):
        raise ValueError("Unsupported time-series format/codec")
    if not np.isfinite(m["epsilon"]) or not 0<m["epsilon"]<=.1:raise ValueError("Invalid RI budget")
    schema=m["source_schema"]
    actual=tcf.validate_source(f["tcf"])
    if actual!=schema:raise ValueError("Time-series TCF schema mismatch")
    if tcf.metadata_digest(tcf.metadata(f["tcf"]))!=m["metadata_sha256"]:
        raise ValueError("TCF metadata checksum mismatch")
    count=schema["volume_count"]
    if len(m["volumes"])!=count or sorted(f["volumes"])!=[f"{i:06d}" for i in range(count)]:
        raise ValueError("Missing time-series volumes")
    for i,row in enumerate(m["volumes"]):
        if row["index"]!=i or row["ri_path"]!=schema["ri_paths"][i]:
            raise ValueError("Invalid time order or volume path")
        if f["tcf"+row["ri_path"]].id.get_storage_size()!=0:
            raise ValueError("Allocated RI template data")
    return m


def volume_manifest(f,m,index):
    row=m["volumes"][index]
    group=f[f"volumes/{index:06d}"]
    v,digest=read_json(group["manifest"])
    if digest!=row["manifest_sha256"]:raise ValueError("Volume manifest checksum mismatch")
    if v["index"]!=index or v["ri_path"]!=row["ri_path"] or v["shape"]!=m["source_schema"]["shape"]:
        raise ValueError("Volume manifest identity mismatch")
    expected=list(tcf.block_layout(v["shape"]))
    if v["block_shape"]!=list(tcf.BLOCK_SHAPE) or len(v["blocks"])!=len(expected):
        raise ValueError("Incomplete time-series block coverage")
    if sorted(group["blocks"])!=[f"{i:06d}" for i in range(len(expected))]:
        raise ValueError("Missing time-series block payload")
    for i,(block,(begin,size)) in enumerate(zip(v["blocks"],expected)):
        if block["index"]!=i or block["origin"]!=list(begin) or block["shape"]!=list(size):
            raise ValueError("Overlapping or out-of-order time-series block")
    return v


def read_packet(group,row,epsilon,mode):
    ds=group[f"blocks/{row['index']:06d}"]
    if ds.ndim!=1 or ds.dtype!=np.dtype("u1") or ds.size>128*2**20:
        raise ValueError("Invalid block payload descriptor")
    blob=ds[()].tobytes()
    if len(blob)!=row["packet_bytes"] or hashlib.sha256(blob).hexdigest()!=row["packet_sha256"]:
        raise ValueError("Packet checksum mismatch")
    h,_=codec.unpack(blob)
    if h["shape"]!=row["shape"] or h["decoded_sha256"]!=row["decoded_sha256"] or h["epsilon"]!=epsilon or h["codec"]!=codec.CODECS[mode]:
        raise ValueError("Packet/volume budget or codec mismatch")
    return blob


def source_blocks(ds,plan,lock):
    shape=ds.shape
    if plan["input_buffer"]=="tile":
        for begin,size in tcf.block_layout(shape):
            with lock:block=np.ascontiguousarray(ds[tcf.slices(begin,size)])
            yield begin,size,block
        return
    for z in range(0,shape[0],tcf.BLOCK_SHAPE[0]):
        with lock:slab=np.ascontiguousarray(ds[z:z+tcf.BLOCK_SHAPE[0]])
        for begin,size in tcf.block_layout(slab.shape):
            actual=(z+begin[0],begin[1],begin[2])
            block=np.ascontiguousarray(slab[tcf.slices(begin,size)])
            yield actual,size,block
        del slab


def encode(source,output,epsilon,mode,concurrent_volumes="auto",memory_gib=None):
    start=time.perf_counter();source=Path(source).resolve();before=file_stat(source)
    if not np.isfinite(epsilon) or not 0<epsilon<=.1 or mode not in codec.CODECS:
        raise ValueError("Invalid RI budget or compression mode")
    with atomic_output(output) as temporary,h5py.File(source,"r") as src,h5py.File(temporary,"w") as dst:
        schema=tcf.validate_source(src);meta=tcf.metadata(src["/"])
        scheduling=parallel.plan(schema,mode,"encode",concurrent_volumes,memory_gib)
        limit=scheduling["concurrent_volume_limit_W"];count=schema["volume_count"]
        progress(f"Time-lapse TCF: {count} volumes; automatic/capped W={limit}; buffer={scheduling['input_buffer']}")
        dst.attrs["htfc_format"]=FORMAT
        tcf.copy_tree(src["/"],dst.create_group("tcf"))
        if tcf.metadata(dst["tcf"])!=meta:raise ValueError("TCF template metadata changed")
        keep=tcf.other_hashes(dst["tcf"]);dst.create_group("volumes")
        lock=RLock()
        def one(index):
            path=schema["ri_paths"][index];rows=[];native=hashlib.sha256()
            guards=packet_bytes=0;maximum=reader_maximum=0.
            with lock:group=dst["volumes"].create_group(f"{index:06d}");group.create_group("blocks");ds=src[path]
            for begin,size,x in source_blocks(ds,scheduling,lock):
                blob,h=codec.encode_block(x,epsilon,mode)
                block_index=len(rows)
                native.update(bytes.fromhex(h["native_sha256"]))
                row=dict(index=block_index,origin=list(begin),shape=list(size),packet_bytes=len(blob),
                    packet_sha256=hashlib.sha256(blob).hexdigest(),decoded_sha256=h["decoded_sha256"],
                    native_sha256=h["native_sha256"],guard_count=h["guard_count"],quality=h["quality"])
                with lock:group["blocks"].create_dataset(f"{block_index:06d}",data=np.frombuffer(blob,dtype="u1"))
                rows.append(row);packet_bytes+=len(blob);guards+=h["guard_count"]
                maximum=max(maximum,h["quality"]["max_RI_error"])
                reader_maximum=max(reader_maximum,h["quality"]["max_reader_float32_RI_error"])
                del x,blob,h
                if len(rows)%128==0:progress(f"Encoded volume {index+1}/{count}: {len(rows)} blocks")
            v=dict(index=index,ri_path=path,shape=schema["shape"],block_shape=list(tcf.BLOCK_SHAPE),blocks=rows,
                native_block_hash_chain=native.hexdigest())
            with lock:digest=write_json(group,"manifest",v)
            return dict(index=index,ri_path=path,manifest_sha256=digest,block_count=len(rows),
                native_block_hash_chain=native.hexdigest(),RI_packet_bytes=packet_bytes,guard_voxels=guards,
                max_RI_error=maximum,max_reader_float32_RI_error=reader_maximum)
        volumes=[None]*count
        for done,row in enumerate(parallel.bounded_map(one,range(count),limit),1):
            volumes[row["index"]]=row
            progress(f"Encoded {done}/{count} volumes (W={limit})")
        if file_stat(source)!=before:raise ValueError("Source changed during compression")
        native_bytes=count*int(np.prod(schema["shape"]))*2
        payload_bytes=sum(v["RI_packet_bytes"] for v in volumes)
        maximum=max(v["max_RI_error"] for v in volumes)
        reader_maximum=max(v["max_reader_float32_RI_error"] for v in volumes)
        m=dict(format=FORMAT,codec=codec.CODECS[mode],mode=mode,tool_version=__version__,
            profile="tcf-u16-independent-series-v2",created_utc=datetime.now(timezone.utc).isoformat(),
            source_name=source.name,source_file=before,source_full_file_hashed=False,source_schema=schema,
            metadata=meta,metadata_sha256=tcf.metadata_digest(meta),non_RI_dataset_sha256=keep,
            epsilon=epsilon,volumes=volumes,native_RI_bytes=native_bytes,RI_packet_bytes=payload_bytes,
            block_count=sum(v["block_count"] for v in volumes),guard_voxels=sum(v["guard_voxels"] for v in volumes),
            max_RI_error=maximum,max_reader_float32_RI_error=reader_maximum,concurrency_plan=scheduling,
            native_RI_hash_scheme="Per-volume SHA256 of ordered per-block raw uint16 SHA256 digests",
            error_reference="native uint16/10000 AND public-reader float32(uint16)/float32(10000)",
            temporal_prediction=False,fft_workers_per_volume=1,derived_cache_policy=CACHE_POLICY,
            dependency_versions={n:importlib.metadata.version(n) for n in ("numpy","scipy","numcodecs","pcodec","h5py","psutil")})
        write_json(dst,"manifest",m);dst.flush();header(dst)
    archive_bytes=Path(output).stat().st_size
    return dict(operation="encode",status="passed",mode=mode,codec=codec.CODECS[mode],output=str(Path(output).resolve()),
        source_bytes=before["bytes"],archive_bytes=archive_bytes,whole_file_ratio=before["bytes"]/archive_bytes,
        native_RI_bytes=native_bytes,RI_packet_bytes=payload_bytes,native_uint16_RI_ratio=native_bytes/payload_bytes,
        float32_equivalent_RI_ratio=2*native_bytes/payload_bytes,block_count=m["block_count"],volume_count=count,
        time_lapse=count>1,guard_voxels=m["guard_voxels"],max_RI_error=maximum,max_reader_float32_RI_error=reader_maximum,
        violations=0,concurrency_plan=scheduling,temporal_prediction=False,**error_budget(epsilon),
        total_wall_s=time.perf_counter()-start,mean_wall_s_per_volume=(time.perf_counter()-start)/count,
        peak_process_RSS_bytes=peak_rss(),timing_scope="Complete command including I/O, guards and packet redecoding; startup excluded")


def decode(source,output,concurrent_volumes="auto",memory_gib=None):
    start=time.perf_counter()
    with h5py.File(source,"r") as src:
        m=header(src);schema=m["source_schema"];count=schema["volume_count"]
        scheduling=parallel.plan(schema,m["mode"],"decode",concurrent_volumes,memory_gib)
        limit=scheduling["concurrent_volume_limit_W"]
        if tcf.other_hashes(src["tcf"])!=m["non_RI_dataset_sha256"]:raise ValueError("Non-RI checksum mismatch")
        with atomic_output(output) as temporary,h5py.File(temporary,"w") as dst:
            tcf.copy_tree(src["tcf"],dst["/"]);lock=RLock()
            def one(index):
                with lock:
                    v=volume_manifest(src,m,index)
                    group=src[f"volumes/{index:06d}"];ds=dst[v["ri_path"]]
                # Write tiles immediately. No completed volume or T-sized array
                # is queued; distinct timepoint datasets retain native metadata.
                for row in v["blocks"]:
                    with lock:blob=read_packet(group,row,m["epsilon"],m["mode"])
                    y=codec.decode_block(blob)
                    with lock:ds[tcf.slices(row["origin"],row["shape"])]=y
                    del blob,y
                return index
            for done,_ in enumerate(parallel.bounded_map(one,range(count),limit),1):
                progress(f"Decoded {done}/{count} volumes (W={limit})")
            dst.flush()
            if tcf.metadata(dst["/"])!=m["metadata"] or tcf.other_hashes(dst["/"])!=m["non_RI_dataset_sha256"]:
                raise ValueError("Restored TCF metadata/non-RI mismatch")
            tcf.validate_source(dst)
    elapsed=time.perf_counter()-start
    return dict(operation="decode",status="passed",mode=m["mode"],codec=m["codec"],
        output=str(Path(output).resolve()),restored_bytes=Path(output).stat().st_size,
        volume_count=count,time_lapse=count>1,block_count=m["block_count"],decoded_hashes_checked=True,
        metadata_preserved=True,non_RI_data_preserved=True,original_required=False,lossy_RI=True,
        derived_cache_policy=CACHE_POLICY,concurrency_plan=scheduling,**error_budget(m["epsilon"]),
        total_wall_s=elapsed,mean_wall_s_per_volume=elapsed/count,peak_process_RSS_bytes=peak_rss())


def inspect(path):
    with h5py.File(path,"r") as f:
        m=header(f)
        return dict(format="HTFC",format_version=FORMAT,file_bytes=Path(path).stat().st_size,
            mode=m["mode"],codec=m["codec"],source_name=m["source_name"],source_schema=m["source_schema"],
            profile=m["profile"],volume_count=m["source_schema"]["volume_count"],time_lapse=m["source_schema"]["volume_count"]>1,
            block_count=m["block_count"],recorded_max_RI_error=m["max_RI_error"],
            recorded_max_reader_float32_RI_error=m["max_reader_float32_RI_error"],
            encode_concurrency_plan=m["concurrency_plan"],**error_budget(m["epsilon"]),
            metadata_checked=True,payload_checksums_checked=False,derived_cache_policy=CACHE_POLICY,
            reference_required_for_new_error_measurement=True)


def verify(path,reference=None,restored=None):
    start=time.perf_counter();maximum=reader_maximum=0.;violations=changed=blocks=0
    with ExitStack() as stack:
        f=stack.enter_context(h5py.File(path,"r"));m=header(f);schema=m["source_schema"]
        if tcf.other_hashes(f["tcf"])!=m["non_RI_dataset_sha256"]:raise ValueError("Non-RI checksum mismatch")
        ref=stack.enter_context(h5py.File(reference,"r")) if reference else None
        out=stack.enter_context(h5py.File(restored,"r")) if restored else None
        for label,obj in (("reference",ref),("restored",out)):
            if obj is not None:
                if tcf.validate_source(obj)!=schema or tcf.metadata(obj["/"])!=m["metadata"]:
                    raise ValueError(f"{label} metadata/schema mismatch")
                if tcf.other_hashes(obj["/"])!=m["non_RI_dataset_sha256"]:
                    raise ValueError(f"{label} non-RI checksum mismatch")
        volume_errors=[]
        for index,path in enumerate(schema["ri_paths"]):
            v=volume_manifest(f,m,index);group=f[f"volumes/{index:06d}"];native=hashlib.sha256()
            vmax=rmax=0.;count=0
            for row in v["blocks"]:
                blob=read_packet(group,row,m["epsilon"],m["mode"]);y=codec.decode_block(blob)
                key=tcf.slices(row["origin"],row["shape"])
                if out is not None and not np.array_equal(out[path][key],y):raise ValueError("Written TCF differs from decoded packet")
                if ref is not None:
                    x=np.ascontiguousarray(ref[path][key]);digest=codec.digest(x)
                    if digest!=row["native_sha256"]:raise ValueError("Reference is not encoded native source")
                    native.update(bytes.fromhex(digest))
                    q=codec.quality(x,y,m["epsilon"]);count+=q["violations"]
                    vmax=max(vmax,q["max_RI_error"]);rmax=max(rmax,q["max_reader_float32_RI_error"])
                    changed+=int(np.count_nonzero(x!=y))
                blocks+=1
                del blob,y
            if ref is not None and (native.hexdigest()!=v["native_block_hash_chain"] or native.hexdigest()!=m["volumes"][index]["native_block_hash_chain"]):
                raise ValueError("Native RI block-hash chain mismatch")
            maximum=max(maximum,vmax);reader_maximum=max(reader_maximum,rmax);violations+=count
            volume_errors.append(dict(index=index,ri_path=path,max_RI_error=vmax if ref is not None else None,
                max_reader_float32_RI_error=rmax if ref is not None else None,violations=count if ref is not None else None))
            progress(f"Verified {index+1}/{schema['volume_count']} volumes")
        if violations:raise ValueError("Final RI error budget violated")
    return dict(operation="verify",status="passed",mode=m["mode"],codec=m["codec"],volume_count=schema["volume_count"],
        time_lapse=schema["volume_count"]>1,block_count=blocks,metadata_preserved=True,non_RI_data_preserved=True,
        payload_and_decoded_hashes_checked=True,reference_supplied=ref is not None,restored_file_checked=out is not None,
        max_RI_error=maximum if ref is not None else None,max_reader_float32_RI_error=reader_maximum if ref is not None else None,
        violations=violations if ref is not None else None,changed_RI_voxels=changed if ref is not None else None,
        volume_errors=volume_errors,**error_budget(m["epsilon"]),total_wall_s=time.perf_counter()-start,
        peak_process_RSS_bytes=peak_rss())

