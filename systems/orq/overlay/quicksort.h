#pragma once

#include <random>
#include <chrono>

#include "common.h"
#include "profiling/stopwatch.h"

// #define DEBUG_QUICKSORT

using namespace orq::benchmarking;

namespace orq::operators {

/**
 * @brief Partitions a vector segment based on comparison results.
 *
 * Rearranges elements between start and end indices based on pivot comparisons.
 * Elements equal to 0 in comparisons are moved to the left partition.
 *
 * @tparam T Share data type.
 * @tparam E Share container type.
 * @param v Vector to partition (modified in place).
 * @param comparisons Comparison results for each element.
 * @param start Starting index (inclusive).
 * @param end Ending index (inclusive).
 * @param pivots Vector tracking pivot positions.
 */
template <typename T, typename E>
void partition(BSharedVector<T, E> &v, Vector<T> &comparisons, int start, int end,
               Vector<long> &pivots) {
    int i = start;
    for (int j = start + 1; j <= end; j++) {
        if (comparisons[j] == 0) {
            // swap element i+1 with element j
            i++;
            for (int k = 0; k < E::replicationNumber; k++) {
                std::swap(v.vector(k)[j], v.vector(k)[i]);
            }
        }
    }

    // swap element i with start
    for (int k = 0; k < E::replicationNumber; k++) {
        std::swap(v.vector(k)[start], v.vector(k)[i]);
    }

    // Set the next pivots
    pivots[i] = i;
    if (i + 1 <= end) {
        pivots[i + 1] = i + 1;
    }
}

/**
 * @brief Core quicksort algorithm implementation.
 *
 * Performs the main quicksort logic on a padded vector.
 *
 * @tparam T Share data type.
 * @tparam EVector Share container type.
 * @param v Vector to sort (modified in place).
 */
template <typename T, typename EVector>
static void quicksort_body(BSharedVector<T, EVector> &v) {
    int num_threads = orq::service::runTime->get_num_threads();

    size_t comm_before = 0;
    for (int i = 0; i < num_threads; ++i) {
        comm_before += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }
    auto time_before = std::chrono::high_resolution_clock::now();

    v.shuffle();

    auto time_after = std::chrono::high_resolution_clock::now();
    size_t comm_after = 0;
    for (int i = 0; i < num_threads; ++i) {
        comm_after += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }

    single_cout("QS_PROFILE_CSV,quicksort_body,shuffle,0,"
                << std::chrono::duration<double>(time_after - time_before).count() << ","
                << static_cast<double>(comm_after - comm_before) / (1024.0 * 1024.0) << ","
                << v.size() << ","
                << v.size() << ","
                << 0);

    v.vector.materialize_inplace();

    size_t N = v.size();

#ifdef DEBUG_QUICKSORT
    auto pid = runTime->getPartyID();
    single_cout_nonl("input: ");
    print(v.open(), pid);
#endif

    Vector<long> pivots(N, -1);
    Vector<long> pivot_temp(N);
    pivots[0] = 0;

    int num_comparisons = 0;
    Vector<T> exp_cmp_plaintext(N);

    BSharedVector<T, EVector> temp(N);
    BSharedVector<T, EVector> comparisons(N);

    int num_pivots = 1;
    int round = 0;

#ifdef MPC_PROTOCOL_DUMMY_ZERO
    std::binomial_distribution<> binom(N, 0.5);
    std::random_device rd;
#endif

    while (true) {
        round++;

        int size = N - num_pivots;
        int num_pivots_before = num_pivots;

#ifdef DEBUG_QUICKSORT
        single_cout("== QS iter size: " << size << " ==");
#endif

        temp.resize(size);
        comparisons.resize(size);

        auto non_pivots = pivots < 0;

        pivot_temp = pivots;
        pivot_temp.prefix_sum(std::max);
        auto pivot_vec = v.mapping_reference(pivot_temp.included_reference(non_pivots));
        auto reduced_vec = v.included_reference(non_pivots);

#ifdef MPC_PROTOCOL_DUMMY_ZERO
        pivot_vec.resize(size);
        reduced_vec.resize(size);
#endif

#ifdef DEBUG_QUICKSORT
        single_cout_nonl("  v ");
        print(v.open(), pid);
        single_cout_nonl(" p= ");
        print(pivots, pid);
        single_cout_nonl(" pv ");
        print(pivot_vec.open(), pid);
        single_cout_nonl(" rv ");
        print(reduced_vec.open(), pid);
#endif

        comm_before = 0;
        for (int i = 0; i < num_threads; ++i) {
            comm_before += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
        }
        time_before = std::chrono::high_resolution_clock::now();

#ifdef QUICKSORT_USE_SUBTRACTION_CMP
        auto comparisons_sub = *rca_compare(pivot_vec, reduced_vec);
        auto cmp_plaintext = comparisons_sub.open();
#else
        reduced_vec._compare(pivot_vec, temp, comparisons);
        auto cmp_plaintext = comparisons.open();
#endif

        time_after = std::chrono::high_resolution_clock::now();
        comm_after = 0;
        for (int i = 0; i < num_threads; ++i) {
            comm_after += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
        }

        single_cout("QS_PROFILE_CSV,quicksort_body,compare_and_open,"
                    << round << ","
                    << std::chrono::duration<double>(time_after - time_before).count() << ","
                    << static_cast<double>(comm_after - comm_before) / (1024.0 * 1024.0) << ","
                    << N << ","
                    << size << ","
                    << num_pivots_before);

        num_comparisons += cmp_plaintext.size();

#ifdef DEBUG_QUICKSORT
        single_cout_nonl(" <= ");
        print(cmp_plaintext, pid);
#endif

        comm_before = 0;
        for (int i = 0; i < num_threads; ++i) {
            comm_before += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
        }
        time_before = std::chrono::high_resolution_clock::now();

        exp_cmp_plaintext.included_reference(non_pivots) = cmp_plaintext;

#ifdef DEBUG_QUICKSORT
        single_cout_nonl(" xc ");
        print(exp_cmp_plaintext, pid);
#endif

        int index = -1;
        for (int p = 0; p < pivots.size(); p++) {
            if (pivots[p] < 0) {
                continue;
            }
            if ((p - index) == 1) {
                index++;
                continue;
            }
            partition(v, exp_cmp_plaintext, index, p - 1, pivots);
            index = p;
        }

        partition(v, exp_cmp_plaintext, index, pivots.size() - 1, pivots);

#ifdef DEBUG_QUICKSORT
        single_cout_nonl(" up ");
        print(exp_cmp_plaintext, pid);
#endif

#ifdef MPC_PROTOCOL_DUMMY_ZERO
        binom.param(std::binomial_distribution<>::param_type(num_pivots * 2 / 3 + 1,
                                                             1 - (double)num_pivots / N));
        num_pivots += binom(rd);
#else
        num_pivots = std::ranges::count_if(pivots, [](int x) { return x >= 0; });
#endif

        time_after = std::chrono::high_resolution_clock::now();
        comm_after = 0;
        for (int i = 0; i < num_threads; ++i) {
            comm_after += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
        }

        single_cout("QS_PROFILE_CSV,quicksort_body,partition,"
                    << round << ","
                    << std::chrono::duration<double>(time_after - time_before).count() << ","
                    << static_cast<double>(comm_after - comm_before) / (1024.0 * 1024.0) << ","
                    << N << ","
                    << size << ","
                    << num_pivots);

#ifdef DEBUG_QUICKSORT
        single_cout_nonl(" v' ");
        print(v.open(), pid);
#endif

        if (num_pivots >= N) {
#ifdef DEBUG_QUICKSORT
            single_cout("Num comparisons: " << num_comparisons);
#endif
            return;
        }
    }
}


/**
 * @brief Main quicksort entry point.
 *
 * Sorts a vector using the quicksort algorithm with padding for stability.
 * Handles both ascending and descending order sorting.
 *
 * @tparam Share Share data type.
 * @tparam EVector Share container type.
 * @param v Vector to sort (modified in place).
 * @param order Sorting direction (ASC or DESC).
 * @return Permutation representing the applied sort order.
 */
// the quicksort entry point which calls the body
template <typename Share, typename EVector>
static ElementwisePermutation<EVector> quicksort(BSharedVector<Share, EVector> &v,
                                                 SortOrder order) {
    int num_threads = orq::service::runTime->get_num_threads();

    size_t comm_before = 0;
    for (int i = 0; i < num_threads; ++i) {
        comm_before += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }
    auto time_before = std::chrono::high_resolution_clock::now();

    // 1 for shuffle, 1 for remove_padding (b2a)
    int num_permutations = 2;
    if (runTime->getNumParties() == 2) {
        // 2PC doesn't need one for remove_padding, uses direct b2a conversion
        num_permutations -= 1;
    }
    // 1 pair for invert (calls obliv_apply_elementwise_perm)
    int num_pairs = 1;
    orq::random::PermutationManager::get()->reserve(v.size(), num_permutations, num_pairs);

    auto reversed = order == SortOrder::DESC;

    // pad the input to ensure unique elements
    auto padded = pad_input(v, reversed);

    auto time_after = std::chrono::high_resolution_clock::now();
    size_t comm_after = 0;
    for (int i = 0; i < num_threads; ++i) {
        comm_after += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }

    single_cout("QS_PROFILE_CSV,quicksort,before_body,0,"
                << std::chrono::duration<double>(time_after - time_before).count() << ","
                << static_cast<double>(comm_after - comm_before) / (1024.0 * 1024.0) << ","
                << v.size() << ","
                << v.size() << ","
                << 0);

    quicksort_body(padded);

    comm_before = 0;
    for (int i = 0; i < num_threads; ++i) {
        comm_before += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }
    time_before = std::chrono::high_resolution_clock::now();

    if (reversed) {
        padded.reverse();
    }

    // unpad the result to obtain the original sorted list and the secret-shared applied
    // permutation
    ElementwisePermutation<EVector> permutation = remove_padding(v, padded, reversed);
    permutation.invert();

    time_after = std::chrono::high_resolution_clock::now();
    comm_after = 0;
    for (int i = 0; i < num_threads; ++i) {
        comm_after += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }

    single_cout("QS_PROFILE_CSV,quicksort,after_body,0,"
                << std::chrono::duration<double>(time_after - time_before).count() << ","
                << static_cast<double>(comm_after - comm_before) / (1024.0 * 1024.0) << ","
                << v.size() << ","
                << v.size() << ","
                << 0);

    return permutation;
}

}  // namespace orq::operators
