################################################################################
#
# Copyright (c) 2025 ByteDance Ltd. and/or its affiliates
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
################################################################################
import torch
import triton
import triton.language as tl
import triton_dist.language as dl
from triton.language.extra.cuda.language_extra import tid, st
from triton_dist import pynvshmem

from typing import Optional, List
from dataclasses import dataclass

from triton_dist.kernels.nvidia.common_ops import set_signal, barrier_all_intra_node_non_atomic
from triton_dist.kernels.nvidia.allgather import AllGatherMethod, cp_engine_producer_all_gather_intra_node, get_auto_all_gather_method, inter_node_allgather, cp_engine_producer_all_gather_full_mesh_pull


@triton.jit(do_not_specialize=["rank"])
def copy_kernel(
    rank,
    local_buf_ptr,
    global_buf_ptr,
    M_per_rank,
    N,
    stride_local_m,
    stride_local_n,
    stride_global_m,
    stride_global_n,
    BLOCK_SIZE_M: tl.constexpr,
    BLOCK_SIZE_N: tl.constexpr,
):
    sm_id = tl.program_id(axis=0)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)

    pid_m = sm_id // num_pid_n
    pid_n = sm_id % num_pid_n

    offs_m = tl.arange(0, BLOCK_SIZE_M)
    offs_n = tl.arange(0, BLOCK_SIZE_N)
    data_ptr = local_buf_ptr + (pid_m * BLOCK_SIZE_M + offs_m[:, None]) * stride_local_m + (
        pid_n * BLOCK_SIZE_N + offs_n[None, :]) * stride_local_n
    dst_ptr = global_buf_ptr + (rank * M_per_rank + pid_m * BLOCK_SIZE_M + offs_m[:, None]) * stride_global_m + (
        pid_n * BLOCK_SIZE_N + offs_n[None, :]) * stride_global_n
    mask_data = (pid_m * BLOCK_SIZE_M + offs_m[:, None] < M_per_rank) & (pid_n * BLOCK_SIZE_N + offs_n[None, :] < N)
    mask_dst = (pid_m * BLOCK_SIZE_M + offs_m[:, None] < M_per_rank) & (pid_n * BLOCK_SIZE_N + offs_n[None, :] < N)

    data = tl.load(data_ptr, mask=mask_data)
    tl.store(dst_ptr, data, mask=mask_dst)


@triton.jit(do_not_specialize=["rank", "num_ranks", "flag_value"])
def copy_and_barrier_all_inter_node_kernel(
    rank,
    num_ranks,
    local_buf_ptr,
    global_buf_ptr,
    symm_barrier_ptr,
    symm_sync_ptr,
    M_per_rank,
    N,
    stride_local_m,
    stride_local_n,
    stride_global_m,
    stride_global_n,
    flag_value,
    BLOCK_SIZE_M: tl.constexpr,
    BLOCK_SIZE_N: tl.constexpr,
):
    barrier_all_intra_node_non_atomic(rank, num_ranks, symm_sync_ptr, flag_value)
    copy_kernel(rank, local_buf_ptr, global_buf_ptr, M_per_rank, N, stride_local_m, stride_local_n, stride_global_m,
                stride_global_n, BLOCK_SIZE_M, BLOCK_SIZE_N)
    thread_idx = tid(0)
    if thread_idx < num_ranks:  # set symm barrier
        st(symm_barrier_ptr + thread_idx, 1 if thread_idx == rank else 0)
    barrier_all_intra_node_non_atomic(rank, num_ranks, symm_sync_ptr, flag_value + 1)


def local_copy_and_barrier_all(rank, num_ranks, local_data, global_data, comm_buf, barrier_ptr, M_per_rank, N, phase,
                               is_internode: bool = False):
    if not is_internode:
        grid = lambda META: (triton.cdiv(M_per_rank, META["BLOCK_SIZE_M"]) * triton.cdiv(N, META["BLOCK_SIZE_N"]), )
        copy_and_barrier_all_inter_node_kernel[grid](rank, num_ranks, local_data, global_data, barrier_ptr, comm_buf,
                                                     M_per_rank, N, local_data.stride(0), local_data.stride(1),
                                                     global_data.stride(0), global_data.stride(1), phase, 128, 256)

    else:
        pynvshmem.nvshmemx_barrier_all_on_stream(torch.cuda.current_stream().cuda_stream)
        barrier_ptr.fill_(0)
        grid = lambda META: (triton.cdiv(M_per_rank, META["BLOCK_SIZE_M"]) * triton.cdiv(N, META["BLOCK_SIZE_N"]), )
        copy_kernel[grid](rank, local_data, global_data, M_per_rank, N, local_data.stride(0), local_data.stride(1),
                          global_data.stride(0), global_data.stride(1), 128, 256)
        set_signal(barrier_ptr[rank].data_ptr(), 1, torch.cuda.current_stream(), is_internode)
        pynvshmem.nvshmemx_barrier_all_on_stream(torch.cuda.current_stream().cuda_stream)


# TMA related test
def _matmul_launch_metadata(grid, kernel, args):
    ret = {}
    M, N, K = args["M"], args["N"], args["K"]
    ret["name"] = f"{kernel.name} [M={M}, N={N}, K={K}]"
    if "c_ptr" in args:
        bytes_per_elem = args["c_ptr"].element_size()
    else:
        bytes_per_elem = 1 if args["FP8_OUTPUT"] else 2
    ret[f"flops{bytes_per_elem * 8}"] = 2.0 * M * N * K
    ret["bytes"] = bytes_per_elem * (M * K + N * K + M * N)
    return ret


@triton.jit(launch_metadata=_matmul_launch_metadata)
def kernel_consumer_gemm_persistent(a_ptr, b_ptr, c_ptr,  #
                                    M, N, K,  #
                                    rank: tl.constexpr, num_ranks: tl.constexpr, ready_ptr, comm_buf_ptr,
                                    BLOCK_SIZE_M: tl.constexpr,  #
                                    BLOCK_SIZE_N: tl.constexpr,  #
                                    BLOCK_SIZE_K: tl.constexpr,  #
                                    GROUP_SIZE_M: tl.constexpr,  #
                                    EPILOGUE_SUBTILE: tl.constexpr,  #
                                    NUM_SMS: tl.constexpr, ready_value: tl.constexpr = 1,
                                    LOCAL_WORLD_SIZE: tl.constexpr = 8):  #
    # Matmul using TMA and device-side descriptor creation
    dtype = c_ptr.dtype.element_ty
    start_pid = tl.program_id(axis=0)
    num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
    k_tiles = tl.cdiv(K, BLOCK_SIZE_K)
    num_tiles = num_pid_m * num_pid_n
    node_id = rank // LOCAL_WORLD_SIZE
    nnodes = num_ranks // LOCAL_WORLD_SIZE

    a_desc = tl.make_tensor_descriptor(
        a_ptr,
        shape=[M, K],
        strides=[K, 1],
        block_shape=[BLOCK_SIZE_M, BLOCK_SIZE_K],
    )
    b_desc = tl.make_tensor_descriptor(
        b_ptr,
        shape=[N, K],
        strides=[K, 1],
        block_shape=[BLOCK_SIZE_N, BLOCK_SIZE_K],
    )
    c_desc = tl.make_tensor_descriptor(
        c_ptr,
        shape=[M, N],
        strides=[N, 1],
        block_shape=[
            BLOCK_SIZE_M,
            BLOCK_SIZE_N if not EPILOGUE_SUBTILE else BLOCK_SIZE_N // 2,
        ],
    )

    tiles_per_SM = num_tiles // NUM_SMS
    if start_pid < num_tiles % NUM_SMS:
        tiles_per_SM += 1

    tile_id = start_pid - NUM_SMS
    ki = -1

    pid_m = 0
    pid_n = 0
    offs_am = 0
    offs_bn = 0

    M_per_rank = M // num_ranks
    pid_ms_per_rank = tl.cdiv(M_per_rank, BLOCK_SIZE_M)

    num_pid_in_group = GROUP_SIZE_M * num_pid_n

    accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)

    for _ in range(0, k_tiles * tiles_per_SM):
        ki = tl.where(ki == k_tiles - 1, 0, ki + 1)
        if ki == 0:
            tile_id += NUM_SMS
            group_id = tile_id // num_pid_in_group
            first_pid_m = group_id * GROUP_SIZE_M
            group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
            pid_m = first_pid_m + (tile_id % group_size_m)
            pid_n = (tile_id % num_pid_in_group) // group_size_m

            # swizzle m
            if nnodes == 1:
                alpha = 0
                beta = 0
                pid_m = (pid_m + ((((rank ^ alpha) + beta) % num_ranks) * pid_ms_per_rank)) % num_pid_m
            else:
                m_rank = pid_m // pid_ms_per_rank
                pid_m_intra_rank = pid_m - m_rank * pid_ms_per_rank
                m_node_id = m_rank // LOCAL_WORLD_SIZE
                m_local_rank = m_rank % LOCAL_WORLD_SIZE
                swizzle_m_node_id = (m_node_id + node_id) % nnodes
                swizzle_m_local_rank = (m_local_rank + rank) % LOCAL_WORLD_SIZE
                swizzle_m_rank = swizzle_m_node_id * LOCAL_WORLD_SIZE + swizzle_m_local_rank

                pid_m = swizzle_m_rank * pid_ms_per_rank + pid_m_intra_rank

            offs_am = pid_m * BLOCK_SIZE_M
            offs_bn = pid_n * BLOCK_SIZE_N

            rank_beg = offs_am // M_per_rank
            rank_end = (min(offs_am + BLOCK_SIZE_M, M) - 1) // M_per_rank
            token = dl.wait(ready_ptr + rank_beg, rank_end - rank_beg + 1, "gpu", "acquire", waitValue=ready_value)
            a_desc = dl.consume_token(a_desc, token)

        # You can also put the barrier here with a minor performance drop
        # if needs_wait:
        #     num_barriers_to_wait = num_barriers_wait_per_block
        #     token = dl.wait(ready_ptr + (ki * BLOCK_SIZE_K) // (K // num_ranks), num_barriers_to_wait, "gpu", "acquire")
        #     a_desc = dl.consume_token(a_desc, token)

        offs_k = ki * BLOCK_SIZE_K

        a = a_desc.load([offs_am, offs_k])
        b = b_desc.load([offs_bn, offs_k])
        accumulator = tl.dot(a, b.T, accumulator)

        if ki == k_tiles - 1:
            if EPILOGUE_SUBTILE:
                acc = tl.reshape(accumulator, (BLOCK_SIZE_M, 2, BLOCK_SIZE_N // 2))
                acc = tl.permute(acc, (0, 2, 1))
                acc0, acc1 = tl.split(acc)
                c0 = acc0.to(dtype)
                c_desc.store([offs_am, offs_bn], c0)
                c1 = acc1.to(dtype)
                c_desc.store([offs_am, offs_bn + BLOCK_SIZE_N // 2], c1)
            else:
                c = accumulator.to(dtype)
                c_desc.store([offs_am, offs_bn], c)

            accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)


def _kernel_consumer_gemm_non_persistent_repr(proxy):
    constexprs = proxy.constants
    cap_major, cap_minor = torch.cuda.get_device_capability()
    a_dtype = proxy.signature["a_ptr"].lstrip("*")
    b_dtype = proxy.signature["b_ptr"].lstrip("*")
    c_dtype = proxy.signature["c_ptr"].lstrip("*")
    BM, BN, BK = constexprs["BLOCK_SIZE_M"], constexprs["BLOCK_SIZE_N"], constexprs["BLOCK_SIZE_K"]
    if constexprs.get("stride_am", None) == 1:  # column major => n
        a_trans = "n"
    elif constexprs.get("stride_ak", None) == 1:  # row-major => t
        a_trans = "t"
    else:
        raise Exception("both stride_am/stride_ak != 1")

    if constexprs.get("stride_bk", None) == 1:
        b_trans = "n"
    elif constexprs.get("stride_bn", None) == 1:
        b_trans = "t"
    else:
        raise Exception("both stride_am/stride_ak != 1")

    if constexprs.get("stride_cm", None) == 1:
        c_trans = "n"
    elif constexprs.get("stride_cn", None) == 1:
        c_trans = "t"
    else:
        raise Exception("both stride_am/stride_ak != 1")

    return f"triton3x_sm{cap_major}{cap_minor}_ag_gemm_tensorop_{a_dtype}_{b_dtype}_{c_dtype}_{BM}x{BN}x{BK}_{a_trans}{b_trans}{c_trans}"


@triton.jit(do_not_specialize=["rank"], launch_metadata=_matmul_launch_metadata,
            repr=_kernel_consumer_gemm_non_persistent_repr)
def kernel_consumer_gemm_non_persistent(
        # Pointers to matrices
        a_ptr, b_ptr, c_ptr,
        # Matrix dimensions
        M, N, K,
        # The stride variables represent how much to increase the ptr by when moving by 1
        # element in a particular dimension. E.g. `stride_am` is how much to increase `a_ptr`
        # by to get the element one row down (A has M rows).
        stride_am, stride_ak,  #
        stride_bk, stride_bn,  #
        stride_cm, stride_cn, rank, WORLD_SIZE: tl.constexpr, barrier_ptr,
        # Meta-parameters
        BLOCK_SIZE_M: tl.constexpr, BLOCK_SIZE_N: tl.constexpr, BLOCK_SIZE_K: tl.constexpr,  #
        GROUP_SIZE_M: tl.constexpr,  #
):
    """Kernel for computing the matmul C = A x B.
    A has shape (M, K), B has shape (K, N) and C has shape (M, N)
    """
    # -----------------------------------------------------------
    # Map program ids `pid` to the block of C it should compute.
    # This is done in a grouped ordering to promote L2 data reuse.
    # See above `L2 Cache Optimizations` section for details.
    a_dtype = a_ptr.dtype.element_ty
    b_dtype = b_ptr.dtype.element_ty
    c_dtype = c_ptr.dtype.element_ty
    # IS_FP8 = tl.constexpr(a_dtype == tl.float8e5) or tl.constexpr(a_dtype == tl.float8e4nv)
    tl.static_assert(a_dtype == b_dtype, "A and B must have the same dtype")

    pid = tl.program_id(axis=0)
    num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
    num_pid_in_group = GROUP_SIZE_M * num_pid_n
    group_id = pid // num_pid_in_group
    first_pid_m = group_id * GROUP_SIZE_M
    group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
    pid_m = first_pid_m + ((pid % num_pid_in_group) % group_size_m)
    pid_n = (pid % num_pid_in_group) // group_size_m

    # threadblock swizzle
    #  no stream-k support. only split by m x n
    m_per_rank = M // WORLD_SIZE
    m_offset = m_per_rank * rank
    pid_m_offset = tl.cdiv(m_offset, BLOCK_SIZE_M)
    pid_m = (pid_m + pid_m_offset) % num_pid_m

    # wait for segment ready.
    offs_am = pid_m * BLOCK_SIZE_M
    rank_beg = offs_am // m_per_rank
    rank_end = (min(offs_am + BLOCK_SIZE_M, M) - 1) // m_per_rank
    token = dl.wait(barrier_ptr + rank_beg, rank_end - rank_beg + 1, "gpu", "acquire", waitValue=1)

    # ----------------------------------------------------------
    # Create pointers for the first blocks of A and B.
    # We will advance this pointer as we move in the K direction
    # and accumulate
    # `a_ptrs` is a block of [BLOCK_SIZE_M, BLOCK_SIZE_K] pointers
    # `b_ptrs` is a block of [BLOCK_SIZE_K, BLOCK_SIZE_N] pointers
    # See above `Pointer Arithmetic` section for details
    offs_am = (pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)) % M
    offs_bn = (pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)) % N
    offs_k = tl.arange(0, BLOCK_SIZE_K)
    a_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_k[None, :] * stride_ak)
    b_ptrs = b_ptr + (offs_k[:, None] * stride_bk + offs_bn[None, :] * stride_bn)

    a_ptrs = dl.consume_token(a_ptrs, token)

    # -----------------------------------------------------------
    # Iterate to compute a block of the C matrix.
    # We accumulate into a `[BLOCK_SIZE_M, BLOCK_SIZE_N]` block
    # of fp32 values for higher accuracy.
    # `accumulator` will be converted back to fp16 after the loop.
    if a_dtype == tl.int8:
        accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.int32)
    else:
        accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)
    for k in range(0, tl.cdiv(K, BLOCK_SIZE_K)):
        # Load the next block of A and B, generate a mask by checking the K dimension.
        # If it is out of bounds, set it to 0.
        a = tl.load(a_ptrs, mask=offs_k[None, :] < K - k * BLOCK_SIZE_K, other=0.0)
        b = tl.load(b_ptrs, mask=offs_k[:, None] < K - k * BLOCK_SIZE_K, other=0.0)
        # We accumulate along the K dimension.
        accumulator += tl.dot(a, b)
        # Advance the ptrs to the next K block.
        a_ptrs += BLOCK_SIZE_K * stride_ak
        b_ptrs += BLOCK_SIZE_K * stride_bk

    # -----------------------------------------------------------
    # Write back the block of the output matrix C with masks.
    offs_cm = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
    offs_cn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
    c_ptrs = c_ptr + stride_cm * offs_cm[:, None] + stride_cn * offs_cn[None, :]
    c_mask = (offs_cm[:, None] < M) & (offs_cn[None, :] < N)

    tl.store(c_ptrs, accumulator.to(c_dtype), mask=c_mask)


def matmul_get_configs():
    return [
        triton.Config({'BLOCK_SIZE_M': BM, 'BLOCK_SIZE_N': BN, "BLOCK_SIZE_K": BK, "GROUP_SIZE_M": 8}, num_stages=s,
                      num_warps=w)
        for BM in [128]
        for BN in [128, 256]
        for BK in [64, 128]
        for s in [3, 4]
        for w in [4, 8]
    ]


# Use Triton's autotune to create a wrapper
kernel_consumer_gemm_persistent_autotune = triton.autotune(configs=matmul_get_configs(),
                                                           key=["M", "N", "K"])(kernel_consumer_gemm_persistent)

# Use Triton's autotune to create a wrapper
kernel_consumer_gemm_non_persistent_autotune = triton.autotune(configs=matmul_get_configs(),
                                                               key=["M", "N", "K"])(kernel_consumer_gemm_non_persistent)


def ag_gemm_non_persistent_op(a, b, c, rank, num_local_ranks, num_ranks, workspace_tensors, barrier_tensors, comm_buf,
                              for_correctness=False, ag_stream=None, gemm_stream=None, serial=False, BLOCK_M=128,
                              BLOCK_N=256, BLOCK_K=64, stages=3, autotune=False,
                              all_gather_method: AllGatherMethod = AllGatherMethod.All2All_IntraNode):
    """allgather gemm for intra-node
    Allgather global matrix A and do matmul with local matrix B, produces local matrix C

    Args:
        a (torch.Tensor<float>): local matmul A matrix. shape: [M_per_rank, K]
        b (torch.Tensor<float>): local matmul B matrix. shape: [N_per_rank, K]
        c (torch.Tensor<float>): local matmul C matrix. shape: [M, N_per_rank]
        rank (int): current rank
        num_ranks (int): total number of ranks
        workspace_tensors (List[torch.Tensor<float>]): A list of symm-tensors used for inter-rank allgather.
            Each tensor shape: [maxM, K]. Created by `create_ag_gemm_intra_node_context`.
        barrier_tensors (List[torch.Tensor<int32>]): A list of symm-tensors used for allgather.
            Each tensor shape: [num_ranks]. Created by `create_ag_gemm_intra_node_context`.
        comm_buf (torch.Tensor<int32>): A symm-tensor used for global synchronization.
            Shape: [MAX_NUM_BLOCKS_ON_GPU(65536)*num_ranks]. Created by `create_ag_gemm_intra_node_context`.
        for_correctness (bool, optional): if only for correctness, communication would sleep some seconds to
            trigger possible synchronization and dependency bugs. Defaults to False.
        ag_stream (torch.cuda.streams.Stream, optional): The stream used for allgather, if not provided, create a new one. Defaults to None.
        gemm_stream (torch.cuda.streams.Stream, optional): The stream used for gemm, if not provided, use current stream. Defaults to None.
        serial (bool, optional): Make the execution serialized, for debug. Defaults to False.
        BLOCK_M (int, optional): GEMM tiling factor for M dim. Defaults to 128.
        BLOCK_N (int, optional): GEMM tiling factor for N dim. Defaults to 256.
        BLOCK_K (int, optional): GEMM tiling factor for K dim. Defaults to 64.
        stages (int, optional): GEMM async-copy stages. Defaults to 3.
        autotune (bool, optional): whether to enable autotune. Defaults to False.

    Returns:
        Triton compiled code: used for debug
    """
    # Check constraints.
    assert a.shape[1] == b.shape[1], "Incompatible dimensions"  # b is transposed
    assert a.dtype == b.dtype, "Incompatible dtypes"

    M_per_rank, K = a.shape
    M = M_per_rank * num_ranks
    N_per_rank, K = b.shape

    ag_stream = torch.cuda.Stream() if ag_stream is None else ag_stream
    gemm_stream = torch.cuda.current_stream() if gemm_stream is None else gemm_stream
    current_stream = torch.cuda.current_stream()
    ag_stream.wait_stream(current_stream)
    gemm_stream.wait_stream(current_stream)

    grid = lambda META: (triton.cdiv(M, META["BLOCK_SIZE_M"]) * triton.cdiv(N_per_rank, META["BLOCK_SIZE_N"]), )

    def call_ag():
        if num_local_ranks == num_ranks:
            cp_engine_producer_all_gather_intra_node(
                rank,
                num_ranks,
                a,
                workspace_tensors,
                barrier_tensors,
                ag_stream,
                for_correctness=for_correctness,
                all_gather_method=all_gather_method,
            )
        else:
            inter_node_allgather(
                a,
                workspace_tensors,
                barrier_tensors,
                1,  # signal_target
                rank,
                num_local_ranks,
                num_ranks,
                ag_stream,
                None,  # internode_ag_stream
                True,  # TODO(houqi.1993)
                all_gather_method=all_gather_method,
                for_correctness=for_correctness,
            )

    if serial:
        call_ag()
        current_stream.wait_stream(ag_stream)
        torch.cuda.synchronize()
    else:
        call_ag()

    local_rank = rank % num_local_ranks
    with torch.cuda.stream(gemm_stream):
        if not autotune:
            compiled = kernel_consumer_gemm_non_persistent[grid](
                workspace_tensors[local_rank][:M],
                b,
                c,  #
                M,
                N_per_rank,
                K,  #
                workspace_tensors[local_rank].stride(0),
                workspace_tensors[local_rank].stride(1),  #
                b.stride(1),
                b.stride(0),  #
                c.stride(0),
                c.stride(1),  #
                rank,
                num_ranks,
                barrier_tensors[local_rank],
                BLOCK_M,
                BLOCK_N,
                BLOCK_K,
                8,
                num_stages=stages,
                num_warps=8,
            )
        else:
            compiled = kernel_consumer_gemm_non_persistent_autotune[grid](
                workspace_tensors[local_rank][:M], b, c,  #
                M, N_per_rank, K,  #
                workspace_tensors[local_rank].stride(0), workspace_tensors[local_rank].stride(1),  #
                b.stride(1), b.stride(0),  #
                c.stride(0), c.stride(1),  #
                rank, num_ranks, barrier_tensors[local_rank])

    current_stream.wait_stream(ag_stream)
    current_stream.wait_stream(gemm_stream)

    return compiled


def ag_gemm_intra_node_persistent_op(a, b, c, rank, num_ranks, workspace_tensors, barrier_tensors, comm_buf,
                                     for_correctness=False, ag_stream=None, gemm_stream=None, serial=False, BLOCK_M=128,
                                     BLOCK_N=256, BLOCK_K=64, stages=3, autotune=False):
    """allgather gemm for intra-node
    Allgather global matrix A and do matmul with local matrix B, produces local matrix C

    Args:
        a (torch.Tensor<float>): local matmul A matrix. shape: [M_per_rank, K]
        b (torch.Tensor<float>): local matmul B matrix. shape: [N_per_rank, K]
        c (torch.Tensor<float>): local matmul C matrix. shape: [M, N_per_rank]
        rank (int): current rank
        num_ranks (int): total number of ranks
        workspace_tensors (List[torch.Tensor<float>]): A list of symm-tensors used for inter-rank allgather.
            Each tensor shape: [maxM, K]. Created by `create_ag_gemm_intra_node_context`.
        barrier_tensors (List[torch.Tensor<int32>]): A list of symm-tensors used for allgather.
            Each tensor shape: [num_ranks]. Created by `create_ag_gemm_intra_node_context`.
        comm_buf (torch.Tensor<int32>): A symm-tensor used for global synchronization.
            Shape: [MAX_NUM_BLOCKS_ON_GPU(65536)*num_ranks]. Created by `create_ag_gemm_intra_node_context`.
        for_correctness (bool, optional): if only for correctness, communication would sleep some seconds to
            trigger possible synchronization and dependency bugs. Defaults to False.
        ag_stream (torch.cuda.streams.Stream, optional): The stream used for allgather, if not provided, create a new one. Defaults to None.
        gemm_stream (torch.cuda.streams.Stream, optional): The stream used for gemm, if not provided, use current stream. Defaults to None.
        serial (bool, optional): Make the execution serialized, for debug. Defaults to False.
        BLOCK_M (int, optional): GEMM tiling factor for M dim. Defaults to 128.
        BLOCK_N (int, optional): GEMM tiling factor for N dim. Defaults to 256.
        BLOCK_K (int, optional): GEMM tiling factor for K dim. Defaults to 64.
        stages (int, optional): GEMM async-copy stages. Defaults to 3.
        autotune (bool, optional): whether to enable autotune. Defaults to False.

    Returns:
        Triton compiled code: used for debug
    """
    # Check constraints.
    assert a.shape[1] == b.shape[1], "Incompatible dimensions"  # b is transposed
    assert a.dtype == b.dtype, "Incompatible dtypes"

    M_per_rank, K = a.shape
    M = M_per_rank * num_ranks
    N_per_rank, K = b.shape

    ag_stream = torch.cuda.Stream() if ag_stream is None else ag_stream
    gemm_stream = torch.cuda.current_stream() if gemm_stream is None else gemm_stream
    current_stream = torch.cuda.current_stream()
    ag_stream.wait_stream(current_stream)
    gemm_stream.wait_stream(current_stream)

    NUM_SMS = torch.cuda.get_device_properties("cuda").multi_processor_count

    # TMA descriptors require a global memory allocation
    def alloc_fn(size: int, alignment: int, stream: Optional[int]):
        return torch.empty(size, device="cuda", dtype=torch.int8)

    triton.set_allocator(alloc_fn)

    grid = lambda META: (min(
        NUM_SMS,
        triton.cdiv(M, META["BLOCK_SIZE_M"]) * triton.cdiv(N_per_rank, META["BLOCK_SIZE_N"]),
    ), )

    def call_ag():
        cp_engine_producer_all_gather_full_mesh_pull(
            rank,
            num_ranks,
            a,
            workspace_tensors,
            barrier_tensors,
            ag_stream,
            for_correctness=for_correctness,
        )

    if serial:
        call_ag()
        current_stream.wait_stream(ag_stream)
        torch.cuda.synchronize()
    else:
        call_ag()
    with torch.cuda.stream(gemm_stream):
        if not autotune:
            compiled = kernel_consumer_gemm_persistent[grid](
                workspace_tensors[rank][:M],
                b,
                c,  #
                M,
                N_per_rank,
                K,  #
                rank,
                num_ranks,
                barrier_tensors[rank],
                comm_buf,
                BLOCK_M,
                BLOCK_N,
                BLOCK_K,
                8,
                False,
                NUM_SMS=NUM_SMS,  #
                num_stages=stages,
                num_warps=8,
            )
        else:
            compiled = kernel_consumer_gemm_persistent_autotune[grid](
                workspace_tensors[rank][:M], b, c,  #
                M, N_per_rank, K,  #
                rank, num_ranks, barrier_tensors[rank], comm_buf, EPILOGUE_SUBTILE=False, NUM_SMS=NUM_SMS,  #
            )

    current_stream.wait_stream(ag_stream)
    current_stream.wait_stream(gemm_stream)

    return compiled


def ag_gemm_inter_node_persistent_op(a, b, c, rank, num_ranks, workspace_tensors, barrier_tensors, comm_buf,
                                     ag_stream=None, internode_ag_stream=None, gemm_stream=None, BLOCK_M=128,
                                     BLOCK_N=256, BLOCK_K=64, stages=3, local_world_size=8, signal_target=1,
                                     autotune=None, copy_engine_dispatch=False):
    """allgather gemm for inter-node
    Allgather global matrix A and do matmul with local matrix B, produces local matrix C

    Args:
        a (torch.Tensor<float>): local matmul A matrix. shape: [M_per_rank, K]
        b (torch.Tensor<float>): local matmul B matrix. shape: [N_per_rank, K]
        c (torch.Tensor<float>): local matmul C matrix. shape: [M, N_per_rank]
        rank (int): current rank
        num_ranks (int): total number of ranks
        workspace_tensors (List[torch.Tensor<float>]): A list of symm-tensors used for inter-rank allgather.
            Each tensor shape: [maxM, K]. Created by `create_ag_gemm_intra_node_context`.
        barrier_tensors (List[torch.Tensor<int32>]): A list of symm-tensors used for allgather.
            Each tensor shape: [num_ranks]. Created by `create_ag_gemm_intra_node_context`.
        comm_buf (torch.Tensor<int32>): A symm-tensor used for global synchronization.
            Shape: [MAX_NUM_BLOCKS_ON_GPU(65536)*num_ranks]. Created by `create_ag_gemm_intra_node_context`.
        for_correctness (bool, optional): if only for correctness, communication would sleep some seconds to
            trigger possible synchronization and dependency bugs. Defaults to False.
        ag_stream (torch.cuda.streams.Stream, optional): The stream used for allgather, if not provided, create a new one. Defaults to None.
        gemm_stream (torch.cuda.streams.Stream, optional): The stream used for gemm, if not provided, use current stream. Defaults to None.
        serial (bool, optional): Make the execution serialized, for debug. Defaults to False.
        BLOCK_M (int, optional): GEMM tiling factor for M dim. Defaults to 128.
        BLOCK_N (int, optional): GEMM tiling factor for N dim. Defaults to 256.
        BLOCK_K (int, optional): GEMM tiling factor for K dim. Defaults to 64.
        stages (int, optional): GEMM async-copy stages. Defaults to 3.
        autotune (bool, optional): whether to enable autotune. Defaults to False.
        copy_engine_dispatch (bool, optional): whether to use copy enginer for intra-node dispatch. Defaults to True.

    Returns:
        Triton compiled code: used for debug
    """
    assert a.shape[1] == b.shape[1], "Incompatible dimensions"
    assert a.dtype == b.dtype, "Incompatible dtypes"

    M_per_rank, K = a.shape
    M = M_per_rank * num_ranks
    N_per_rank, K = b.shape

    local_rank = rank % local_world_size
    n_nodes = num_ranks // local_world_size
    num_ag_sms = n_nodes - 1 if copy_engine_dispatch else (local_world_size + n_nodes - 2)
    num_gemm_sms = torch.cuda.get_device_properties("cuda").multi_processor_count - num_ag_sms

    ag_stream = torch.cuda.Stream() if ag_stream is None else ag_stream
    gemm_stream = torch.cuda.current_stream() if gemm_stream is None else gemm_stream
    current_stream = torch.cuda.current_stream()
    ag_stream.wait_stream(current_stream)
    gemm_stream.wait_stream(current_stream)

    inter_node_allgather(a, workspace_tensors, barrier_tensors, signal_target, rank, local_world_size, num_ranks,
                         ag_stream, internode_ag_stream, copy_engine_dispatch)

    compiled = None
    with torch.cuda.stream(gemm_stream):

        def alloc_fn(size: int, alignment: int, stream: Optional[int]):
            return torch.empty(size, device="cuda", dtype=torch.int8)

        triton.set_allocator(alloc_fn)

        grid = lambda META: (min(
            num_gemm_sms,
            triton.cdiv(M, META["BLOCK_SIZE_M"]) * triton.cdiv(N_per_rank, META["BLOCK_SIZE_N"]),
        ), )
        if not autotune:
            compiled = kernel_consumer_gemm_persistent[grid](
                workspace_tensors[local_rank][:M],
                b,
                c,  #
                M,
                N_per_rank,
                K,  #
                rank,
                num_ranks,
                barrier_tensors[local_rank],
                comm_buf,
                BLOCK_M,
                BLOCK_N,
                BLOCK_K,
                8,
                False,
                NUM_SMS=num_gemm_sms,
                ready_value=signal_target,
                num_stages=stages,
                num_warps=8,
            )
        else:
            compiled = kernel_consumer_gemm_persistent_autotune[grid](
                workspace_tensors[local_rank][:M], b, c,  #
                M, N_per_rank, K,  #
                rank, num_ranks, barrier_tensors[local_rank], comm_buf, EPILOGUE_SUBTILE=False, NUM_SMS=num_gemm_sms,
                ready_value=signal_target  #
            )

    current_stream.wait_stream(ag_stream)
    current_stream.wait_stream(gemm_stream)

    return compiled


@dataclass
class AllGatherGEMMTensorParallelContext:
    rank: int
    num_ranks: int
    num_local_ranks: int
    local_rank: int
    workspace_tensors: List[torch.Tensor]
    barrier_tensors: List[torch.Tensor]
    fake_barrier_tensor: torch.Tensor
    comm_buf: torch.Tensor
    for_correctness: bool = False
    ag_stream: Optional[torch.cuda.streams.Stream] = None
    gemm_stream: Optional[torch.cuda.streams.Stream] = None
    internode_ag_stream: Optional[torch.cuda.streams.Stream] = None
    serial: bool = False
    BLOCK_M: int = 128
    BLOCK_N: int = 256
    BLOCK_K: int = 64
    stages: int = 3
    autotune: bool = False
    phase: int = 1
    all_gather_method: AllGatherMethod = AllGatherMethod.Auto

    def update(self, rank, num_ranks, num_local_ranks=8, BLOCK_M=128, BLOCK_N=256, BLOCK_K=64, stages=3,
               for_correctness=False, ag_stream=None, internode_ag_stream=None, gemm_stream=None, serial=False,
               autotune=False):
        self.rank = rank
        self.num_ranks = num_ranks
        self.num_local_ranks = num_local_ranks
        self.BLOCK_M = BLOCK_M
        self.BLOCK_N = BLOCK_N
        self.BLOCK_K = BLOCK_K
        self.stages = stages
        self.for_correctness = for_correctness
        self.ag_stream = ag_stream
        self.internode_ag_stream = internode_ag_stream
        self.gemm_stream = gemm_stream
        self.serial = serial
        self.autotune = autotune


def create_ag_gemm_intra_node_context(tensor_A, tensor_B, rank, num_ranks, max_M=2**14, max_blocks=65536, BLOCK_M=128,
                                      BLOCK_N=256, BLOCK_K=64, stages=3, for_correctness=False, ag_stream=None,
                                      gemm_stream=None, serial=False, autotune=False):
    """create context for allgather gemm intra-node

    Args:
        tensor_A (torch.Tensor<float>): local matmul A matrix. shape: [M_per_rank, K]
        tensor_B (torch.Tensor<float>): local matmul B matrix. shape: [N_per_rank, K]
        rank (int): current rank
        num_ranks (int): total number of ranks
        max_M: max number of M shape
        max_blocks: max number of blocks on GPU
        BLOCK_M (int, optional): GEMM tiling factor for M dim. Defaults to 128.
        BLOCK_N (int, optional): GEMM tiling factor for N dim. Defaults to 256.
        BLOCK_K (int, optional): GEMM tiling factor for K dim. Defaults to 64.
        stages (int, optional): GEMM async-copy stages. Defaults to 3.
        for_correctness (bool, optional): if only for correctness, communication would sleep some seconds to
            trigger possible synchronization and dependency bugs. Defaults to False.
        ag_stream (torch.cuda.streams.Stream, optional): The stream used for allgather, if not provided, create a new one. Defaults to None.
        gemm_stream (torch.cuda.streams.Stream, optional): The stream used for gemm, if not provided, use current stream. Defaults to None.
        serial (bool, optional): Make the execution serialized, for debug. Defaults to False.
        autotune (bool, optional): whether to enable autotune. Defaults to False.

    Returns:
        AllGatherGEMMTensorParallelContext
    """
    M_per_rank, K = tensor_A.shape
    assert tensor_B.shape[
        1] == K, f"tensor_B should has shape (col_major) [N_per_rank, {K}], but get [{tensor_B.shape}]"
    assert tensor_A.dtype == tensor_B.dtype
    dtype = tensor_A.dtype
    fake_barrier = torch.ones([num_ranks], dtype=torch.int32, device=tensor_A.device)
    workspaces = pynvshmem.nvshmem_create_tensor_list_intra_node([max_M, K], dtype)
    barriers = pynvshmem.nvshmem_create_tensor_list_intra_node([num_ranks], torch.int32)
    comm_buf = pynvshmem.nvshmem_create_tensor([3 * num_ranks], torch.int32)
    comm_buf.fill_(0)
    barriers[rank].fill_(0)
    current_stream = torch.cuda.current_stream()
    pynvshmem.nvshmemx_barrier_all_on_stream(current_stream.cuda_stream)
    torch.cuda.synchronize()

    ret = AllGatherGEMMTensorParallelContext(
        rank=rank, num_ranks=num_ranks, local_rank=rank, num_local_ranks=num_ranks, workspace_tensors=workspaces,
        barrier_tensors=barriers, fake_barrier_tensor=fake_barrier, comm_buf=comm_buf, for_correctness=for_correctness,
        ag_stream=ag_stream, gemm_stream=gemm_stream, serial=serial, BLOCK_M=BLOCK_M, BLOCK_N=BLOCK_N, BLOCK_K=BLOCK_K,
        stages=stages, autotune=autotune, all_gather_method=get_auto_all_gather_method(num_ranks, num_ranks))

    return ret


def ag_gemm_intra_node(a, b, ctx: AllGatherGEMMTensorParallelContext = None, rank=None, num_ranks=None,
                       persistent=True):
    """allgather gemm for intra-node
    Allgather global matrix A and do matmul with local matrix B, produces local matrix C

    Args:
        a (torch.Tensor<float>): local matmul A matrix. shape: [M_per_rank, K]
        b (torch.Tensor<float>): local matmul B matrix. shape: [N_per_rank, K]
        ctx: (Optional[AllGatherGEMMTensorParallelContext]): if not provided, created immediately

    Returns:
        c (torch.Tensor<float>): local matmul C matrix. shape: [M, N_per_rank]
    """
    if ctx is None:
        assert rank is not None and num_ranks is not None
        ctx = create_ag_gemm_intra_node_context(a, b, rank, num_ranks)
    M_per_rank, K = a.shape
    N_per_rank, _ = b.shape
    C = torch.empty([ctx.num_ranks * M_per_rank, N_per_rank], dtype=a.dtype, device=a.device)
    # The following version doesn't rely on our customized barrier kernel
    # Just reuse nvshmem barrier, they should produce similar performance

    # barriers[rank].fill_(0)
    # pynvshmem.nvshmemx_barrier_all_on_stream(current_stream.cuda_stream)
    # workspaces[rank][rank * M_per_rank:(rank + 1) * M_per_rank, :].copy_(A)
    # (err, ) = cuda.cuStreamWriteValue32(
    #     torch.cuda.current_stream().cuda_stream,
    #     barriers[rank][rank].data_ptr(),
    #     1,
    #     cuda.CUstreamWriteValue_flags.CU_STREAM_WRITE_VALUE_DEFAULT,
    # )
    # CUDA_CHECK(err)
    # pynvshmem.nvshmemx_barrier_all_on_stream(current_stream.cuda_stream)

    # Use our own customized barrier kernel
    local_copy_and_barrier_all(ctx.rank, ctx.num_ranks, a, ctx.workspace_tensors[ctx.rank], ctx.comm_buf,
                               ctx.barrier_tensors[ctx.rank], M_per_rank, K, ctx.phase)
    ctx.phase += 2
    if persistent:
        ag_gemm_intra_node_persistent_op(a, b, C, ctx.rank, ctx.num_ranks, ctx.workspace_tensors, ctx.barrier_tensors,
                                         ctx.comm_buf, for_correctness=ctx.for_correctness, ag_stream=ctx.ag_stream,
                                         gemm_stream=ctx.gemm_stream, serial=ctx.serial, autotune=ctx.autotune)
    else:
        ag_gemm_non_persistent_op(a, b, C, ctx.rank, ctx.num_local_ranks, ctx.num_ranks, ctx.workspace_tensors,
                                  ctx.barrier_tensors, ctx.comm_buf, for_correctness=ctx.for_correctness,
                                  ag_stream=ctx.ag_stream, gemm_stream=ctx.gemm_stream, serial=ctx.serial,
                                  autotune=ctx.autotune, all_gather_method=ctx.all_gather_method)
    return C


def create_ag_gemm_inter_node_context(tensor_A, tensor_B, rank, num_ranks, num_local_ranks=8, max_M=2**14,
                                      max_blocks=65536, BLOCK_M=128, BLOCK_N=256, BLOCK_K=64, stages=3,
                                      for_correctness=False, ag_stream=None, gemm_stream=None, serial=False,
                                      autotune=False):
    """create context for allgather gemm inter-node

    Args:
        tensor_A (torch.Tensor<float>): local matmul A matrix. shape: [M_per_rank, K]
        tensor_B (torch.Tensor<float>): local matmul B matrix. shape: [N_per_rank, K]
        rank (int): current rank
        num_ranks (int): total number of ranks
        max_M: max number of M shape
        max_blocks: max number of blocks on GPU
        BLOCK_M (int, optional): GEMM tiling factor for M dim. Defaults to 128.
        BLOCK_N (int, optional): GEMM tiling factor for N dim. Defaults to 256.
        BLOCK_K (int, optional): GEMM tiling factor for K dim. Defaults to 64.
        stages (int, optional): GEMM async-copy stages. Defaults to 3.
        ag_stream (torch.cuda.streams.Stream, optional): The stream used for allgather, if not provided, create a new one. Defaults to None.
        gemm_stream (torch.cuda.streams.Stream, optional): The stream used for gemm, if not provided, use current stream. Defaults to None.
        serial (bool, optional): Make the execution serialized, for debug. Defaults to False.
        autotune (bool, optional): whether to enable autotune. Defaults to False.

    Returns:
        AllGatherGEMMTensorParallelContext
    """
    _, K = tensor_A.shape
    assert tensor_B.shape[
        1] == K, f"tensor_B should has shape (col_major) [N_per_rank, {K}], but get [{tensor_B.shape}]"
    assert tensor_A.dtype == tensor_B.dtype
    dtype = tensor_A.dtype

    local_rank = rank % num_local_ranks

    fake_barrier = torch.ones([num_ranks], dtype=torch.int32, device=tensor_A.device)
    workspaces = pynvshmem.nvshmem_create_tensor_list_intra_node([max_M, K], dtype)
    barriers = pynvshmem.nvshmem_create_tensor_list_intra_node([num_ranks], torch.uint64)
    comm_buf = pynvshmem.nvshmem_create_tensor([3 * num_ranks], torch.int32)
    comm_buf.fill_(0)
    barriers[local_rank].fill_(0)
    current_stream = torch.cuda.current_stream()
    pynvshmem.nvshmemx_barrier_all_on_stream(current_stream.cuda_stream)
    torch.cuda.synchronize()

    ret = AllGatherGEMMTensorParallelContext(
        rank=rank, num_ranks=num_ranks, local_rank=local_rank, num_local_ranks=num_local_ranks,
        workspace_tensors=workspaces, barrier_tensors=barriers, fake_barrier_tensor=fake_barrier, comm_buf=comm_buf,
        for_correctness=for_correctness, ag_stream=ag_stream, internode_ag_stream=torch.cuda.Stream(),
        gemm_stream=gemm_stream, serial=serial, BLOCK_M=BLOCK_M, BLOCK_N=BLOCK_N, BLOCK_K=BLOCK_K, stages=stages,
        autotune=autotune, all_gather_method=get_auto_all_gather_method(num_local_ranks, num_ranks))

    return ret


def ag_gemm_inter_node(a, b, ctx=None, rank=None, num_ranks=None, local_world_size=8, signal_target=1, persistent=True):
    """allgather gemm for inter-node
    Allgather global matrix A and do matmul with local matrix B, produces local matrix C

    Args:
        a (torch.Tensor<float>): local matmul A matrix. shape: [M_per_rank, K]
        b (torch.Tensor<float>): local matmul B matrix. shape: [N_per_rank, K]
        ctx: (Optional[AllGatherGEMMTensorParallelContext]): if not provided, created immediately

    Returns:
        c (torch.Tensor<float>): local matmul C matrix. shape: [M, N_per_rank]
    """
    if ctx is None:
        assert rank is not None and num_ranks is not None
        ctx = create_ag_gemm_inter_node_context(a, b, rank, num_ranks)
    M_per_rank, K = a.shape
    N_per_rank, _ = b.shape
    C = torch.empty([ctx.num_ranks * M_per_rank, N_per_rank], dtype=a.dtype, device=a.device)

    local_copy_and_barrier_all(ctx.rank, ctx.num_ranks, a, ctx.workspace_tensors[ctx.local_rank], ctx.comm_buf,
                               ctx.barrier_tensors[ctx.local_rank], M_per_rank, K, ctx.phase, is_internode=True)
    ctx.phase += 2

    if persistent:
        # TODO(houqi.1993) many arguments use default such as BLOCK_M/N/K and stages. not passed
        ag_gemm_inter_node_persistent_op(a, b, C, ctx.rank, ctx.num_ranks, ctx.workspace_tensors, ctx.barrier_tensors,
                                         ctx.comm_buf, ag_stream=ctx.ag_stream, stages=ctx.stages,
                                         internode_ag_stream=ctx.internode_ag_stream, gemm_stream=ctx.gemm_stream,
                                         autotune=ctx.autotune, local_world_size=local_world_size,
                                         signal_target=signal_target, copy_engine_dispatch=True)
    else:
        # TODO(houqi.1993) many arguments use default such as BLOCK_M/N/K and stages. not passed
        ag_gemm_non_persistent_op(a, b, C, ctx.rank, ctx.num_local_ranks, ctx.num_ranks, ctx.workspace_tensors,
                                  ctx.barrier_tensors, ctx.comm_buf, for_correctness=ctx.for_correctness,
                                  ag_stream=ctx.ag_stream, gemm_stream=ctx.gemm_stream, serial=ctx.serial,
                                  autotune=ctx.autotune, all_gather_method=ctx.all_gather_method)

    return C


def gemm_persistent(a, b, ctx: AllGatherGEMMTensorParallelContext):
    M, K = a.shape
    N, _ = b.shape
    C = torch.empty([M, N], dtype=a.dtype, device=a.device)

    NUM_SMS = torch.cuda.get_device_properties("cuda").multi_processor_count

    def alloc_fn(size: int, alignment: int, stream: Optional[int]):
        return torch.empty(size, device="cuda", dtype=torch.int8)

    triton.set_allocator(alloc_fn)

    grid = lambda META: (min(
        NUM_SMS,
        triton.cdiv(M, META["BLOCK_SIZE_M"]) * triton.cdiv(N, META["BLOCK_SIZE_N"]),
    ), )
    if not ctx.autotune:
        kernel_consumer_gemm_persistent[grid](
            a,
            b,
            C,  #
            M,
            N,
            K,  #
            ctx.rank,
            ctx.num_ranks,
            ctx.fake_barrier_tensor,
            ctx.comm_buf,
            ctx.BLOCK_M,
            ctx.BLOCK_N,
            ctx.BLOCK_K,
            8,
            False,
            NUM_SMS=NUM_SMS,
            num_stages=ctx.stages,
            num_warps=8,
        )
    else:
        kernel_consumer_gemm_persistent_autotune[grid](
            a, b, C,  #
            M, N, K,  #
            ctx.rank, ctx.num_ranks, ctx.fake_barrier_tensor, ctx.comm_buf, EPILOGUE_SUBTILE=False, NUM_SMS=NUM_SMS  #
        )

    pynvshmem.nvshmemx_barrier_all_on_stream(torch.cuda.current_stream().cuda_stream)

    return C


def gemm_non_persistent(a, b, ctx: AllGatherGEMMTensorParallelContext):
    M, K = a.shape
    N, _ = b.shape
    C = torch.empty([M, N], dtype=a.dtype, device=a.device)

    grid = lambda META: (triton.cdiv(M, META["BLOCK_SIZE_M"]) * triton.cdiv(N, META["BLOCK_SIZE_N"]), )
    if not ctx.autotune:
        kernel_consumer_gemm_non_persistent[grid](
            a,
            b,
            C,  #
            M,
            N,
            K,  #
            a.stride(0),
            a.stride(1),
            b.stride(1),
            b.stride(0),
            C.stride(0),
            C.stride(1),
            ctx.rank,
            ctx.num_ranks,
            ctx.fake_barrier_tensor,
            ctx.BLOCK_M,
            ctx.BLOCK_N,
            ctx.BLOCK_K,
            8,
            num_stages=ctx.stages,
            num_warps=8,
        )
    else:
        kernel_consumer_gemm_persistent_autotune[grid](
            a, b, C,  #
            M, N, K,  #
            ctx.rank, ctx.num_ranks, ctx.fake_barrier_tensor, ctx.comm_buf, EPILOGUE_SUBTILE=False, NUM_SMS=0  #
        )

    return C
