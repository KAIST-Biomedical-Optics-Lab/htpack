"""RAM-aware bounded volume scheduling; HDF5 access stays under a caller lock."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import math
import os
import psutil
from . import tcf

MiB=2**20
GiB=2**30


def resources():
    memory=psutil.virtual_memory()
    process=psutil.Process()
    cpus=psutil.cpu_count(logical=True) or 1
    if hasattr(os,"sched_getaffinity"):
        try:cpus=min(cpus,len(os.sched_getaffinity(0)))
        except OSError:pass
    try:
        affinity=process.cpu_affinity()
        if affinity:cpus=min(cpus,len(affinity))
    except (AttributeError,NotImplementedError,OSError,psutil.Error):pass
    return dict(total_memory_bytes=int(memory.total),available_memory_bytes=int(memory.available),
        current_process_RSS_bytes=int(process.memory_info().rss),logical_cpu_count=max(1,cpus))


def plan(schema,mode,operation="encode",concurrent_volumes="auto",memory_gib=None,snapshot=None):
    if concurrent_volumes!="auto" and (type(concurrent_volumes) is not int or concurrent_volumes<1):
        raise ValueError("concurrent_volumes must be auto or a positive integer")
    if memory_gib is not None and (not math.isfinite(memory_gib) or memory_gib<=0):
        raise ValueError("memory_gib must be finite and positive")
    info=resources() if snapshot is None else dict(snapshot)
    count=schema.get("volume_count",1)
    z,y,x=schema["shape"]
    cpu_limit=max(1,info["logical_cpu_count"]*3//4)
    budget=info["current_process_RSS_bytes"]+int(info["available_memory_bytes"]*.5)
    if memory_gib is not None:budget=min(budget,int(memory_gib*GiB))
    # Global metadata/cache headroom, beyond memory already held by this process.
    overhead=256*MiB
    usable=budget-info["current_process_RSS_bytes"]-overhead
    voxels=math.prod(min(n,b) for n,b in zip((z,y,x),tcf.BLOCK_SHAPE))
    # Conservative workspace estimate includes temporaries, guards, packet
    # redecoding, FFT geometry/cache and codec buffers. It is a planning bound.
    bytes_per_voxel=(192 if mode=="fourier" else 112) if operation=="encode" else (112 if mode=="fourier" else 64)
    working=32*MiB+voxels*bytes_per_voxel
    slab_depth=min(z,tcf.BLOCK_SHAPE[0])
    buffer="slab" if operation=="encode" else "tile"
    per_volume=working+(slab_depth*y*x*2 if buffer=="slab" else voxels*2)
    if per_volume>usable:
        buffer="tile"
        per_volume=working+voxels*2
    memory_limit=max(0,usable//per_volume)
    if memory_limit<1:
        raise ValueError("Insufficient planned RAM for one active volume; free memory or raise --memory-gib")
    requested=count if concurrent_volumes=="auto" else concurrent_volumes
    chosen=min(count,cpu_limit,memory_limit,requested)
    return dict(operation=operation,policy="half-available-RAM with 256MiB shared headroom and 75%-CPU cap",
        requested_concurrent_volumes=concurrent_volumes,concurrent_volume_limit_W=int(chosen),
        volume_count=count,cpu_limit=cpu_limit,memory_limit=int(memory_limit),memory_budget_bytes=budget,
        shared_headroom_bytes=overhead,estimated_bytes_per_active_volume=int(per_volume),
        estimated_peak_RSS_bytes=info["current_process_RSS_bytes"]+overhead+chosen*per_volume,
        input_buffer=buffer,slab_depth=slab_depth if buffer=="slab" else None,
        hard_OS_memory_limit=False,**info)


def bounded_map(function,items,limit):
    """Keep at most W submitted/not-consumed jobs, regardless of series length."""
    iterator=iter(items)
    if limit==1:
        for item in iterator:yield function(item)
        return
    with ThreadPoolExecutor(max_workers=limit,thread_name_prefix="htpack-volume") as pool:
        pending=set()
        for _ in range(limit):
            try:pending.add(pool.submit(function,next(iterator)))
            except StopIteration:break
        try:
            while pending:
                ready,_=wait(pending,return_when=FIRST_COMPLETED)
                # Consume one result before submitting its replacement. No map()
                # eager queue or completed-volume payload list can grow with T.
                for future in ready:
                    pending.remove(future)
                    yield future.result()
                    try:pending.add(pool.submit(function,next(iterator)))
                    except StopIteration:pass
        except BaseException:
            for future in pending:future.cancel()
            raise

