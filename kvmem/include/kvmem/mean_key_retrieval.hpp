#pragma once

#include <cstdint>
#include <vector>

namespace kvmem {
class RawKvStore;

// Borrowed post-RMSNorm/pre-RoPE sums. Layers match the statistics store's
// ordinals; each row contains query_heads * head_dim FP32 elements. A layer
// with no query tokens contributes no score. The caller owns prefix identity
// and commits only evaluated/accepted rows to the supplied statistics.
struct MeanKeyQueryView {
    const float* sums = nullptr;
    const uint32_t* token_counts = nullptr;
    uint32_t layers = 0;
    uint32_t head_dim = 0;
    uint32_t query_heads = 0;
    uint32_t kv_heads = 0;
};

// ActKV (arXiv:2609.31395, Sec. 7.1 Alg. 3 step 8): how the query heads of one
// GQA group are combined while scoring a block.
//   Mean — the historical folded probe (sum over the group, then normalize).
//          A head that matches strongly is cancelled by heads that do not.
//   Max  — keep the group's best-matching head (--kvmem-score-agg max). This is
//          what identifies an action-critical entry, at g x the dot products.
enum class MeanKeyGroupReduce : uint8_t {
    Mean = 0,
    Max  = 1,
};

// Mean Q over query rows and each GQA group, cosine with each block's mean K,
// averaged across KV heads and participating layers. FP32 dot/norm operations
// and epsilon 1e-6 preserve the existing llama product adapter's host policy.
// This is selection math only: it mutates no payload, frontier or residency.
//
// `group_reduce` selects the GQA aggregation. The default (Mean) keeps every
// existing caller byte-identical, including the ninfer backend patch.
std::vector<double> mean_key_retrieval_scores(const RawKvStore& keys, uint32_t blocks,
                                               MeanKeyQueryView query,
                                               MeanKeyGroupReduce group_reduce =
                                                   MeanKeyGroupReduce::Mean);
} // namespace kvmem
