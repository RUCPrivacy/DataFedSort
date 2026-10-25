// Copyright 2022 Ant Group Co., Ltd.
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0
#include "libspu/kernel/hlo/sort.h"

#include <atomic>
#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iterator>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <tuple>
#include <type_traits>
#include <vector>

#include "gtest/gtest.h"
#include "magic_enum.hpp"
#include "libspu/kernel/hal/polymorphic.h"
#include "libspu/kernel/hal/type_cast.h"
#include "libspu/kernel/hlo/casting.h"
#include "libspu/kernel/hlo/const.h"
#include "libspu/kernel/test_util.h"
#include "libspu/mpc/utils/simulate.h"
#include "yacl/link/link.h"

namespace spu::kernel::hlo {

// ============================================================================
// 1. Shared benchmark types.
// ============================================================================
enum class TestDType { INT8, INT32, INT64, FLOAT, DOUBLE, BOOL };

struct TableDataInfo {
    size_t rows = 0;
    size_t cols = 0;
    std::string path;
};

struct FileSortCase {
    std::string name;
    std::string file_name;
    size_t num_keys = 1;
    TestDType dtype = TestDType::INT32;
    FieldType field = FieldType::FM32;
    ProtocolKind protocol = ProtocolKind::ABY3;
    size_t npc = 3;
    RuntimeConfig::SortMethod method = RuntimeConfig::SORT_DEFAULT;
};

// ============================================================================
// 2. Performance sampling helpers.
// ============================================================================
#ifdef __linux__
inline size_t GetCurrentMemoryKB() {
    std::ifstream status("/proc/self/status");
    std::string line;
    while (std::getline(status, line)) {
        if (line.rfind("VmRSS:", 0) == 0) {
            try {
                size_t pos = line.find_first_of("0123456789");
                if (pos != std::string::npos) return std::stoull(line.substr(pos));
            } catch (...) {}
        }
    }
    return 0;
}
#else
inline size_t GetCurrentMemoryKB() { return 0; }
#endif

class MemorySampler {
public:
    MemorySampler() : running_(true), peak_(GetCurrentMemoryKB()) {
        thread_ = std::thread([this]() {
            while (running_) {
                size_t cur = GetCurrentMemoryKB();
                if (cur > peak_) peak_ = cur;
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
            }
        });
    }
    void Stop() {
        running_ = false;
        if (thread_.joinable()) thread_.join();
    }
    ~MemorySampler() { Stop(); }
    size_t GetPeak() const { return peak_; }
private:
    std::atomic<bool> running_;
    std::atomic<size_t> peak_{0};
    std::thread thread_;
};

// ============================================================================
// 3. File loading and CPU reference helpers.
// ============================================================================
static std::string SanitizeTestName(std::string name) {
    for (char& ch : name) {
        if (!std::isalnum(static_cast<unsigned char>(ch))) {
            ch = '_';
        }
    }
    return name;
}

static bool FileExists(const std::string& path) {
    std::ifstream input(path);
    return input.good();
}

static std::string ResolveDataFile(const std::string& file_name) {
    const char* input_file = std::getenv("FES_BENCH_INPUT_FILE");
    if (input_file != nullptr && *input_file != '\0') {
        if (!FileExists(input_file)) {
            throw std::runtime_error("Configured benchmark input does not exist");
        }
        return std::string(input_file);
    }
    const char* lan_data_dir = std::getenv("FES_BENCH_DATA_DIR");
    if (lan_data_dir != nullptr) {
        const std::string lan_path = std::string(lan_data_dir) + "/" + file_name;
        if (FileExists(lan_path)) {
            return lan_path;
        }
    }

    const std::vector<std::string> candidates = {
        "data/generated/" + file_name,
        "../../../data/generated/" + file_name,
        "../../../../data/generated/" + file_name,
        "../../../../../data/generated/" + file_name,
    };

    for (const auto& path : candidates) {
        if (FileExists(path)) {
            return path;
        }
    }

    throw std::runtime_error("Failed to find input data file: " + file_name);
}

static std::vector<std::string> SplitCsv(const std::string& value) {
    std::vector<std::string> out;
    std::stringstream ss(value);
    std::string item;
    while (std::getline(ss, item, ',')) {
        if (!item.empty()) {
            out.push_back(item);
        }
    }
    return out;
}

static int EnvInt(const char* name, int default_value) {
    const char* value = std::getenv(name);
    if (value == nullptr || *value == '\0') {
        return default_value;
    }
    try {
        return std::stoi(value);
    } catch (...) {
        return default_value;
    }
}

static std::shared_ptr<yacl::link::Context> TryMakeLanContext(size_t npc) {
    const char* rank_env = std::getenv("FES_BENCH_RANK");
    const char* parties_env = std::getenv("FES_BENCH_PARTIES");
    if (rank_env == nullptr || parties_env == nullptr) {
        return nullptr;
    }

    std::vector<std::string> parties = SplitCsv(parties_env);
    if (parties.size() != npc) {
        throw std::runtime_error("FES_BENCH_PARTIES count does not match npc");
    }

    yacl::link::ContextDesc desc;
    desc.recv_timeout_ms = EnvInt("FES_BENCH_LINK_TIMEOUT_MS", 2400 * 1000);
    for (size_t i = 0; i < parties.size(); ++i) {
        desc.parties.push_back({"party" + std::to_string(i), parties[i]});
    }

    const size_t rank = static_cast<size_t>(std::stoul(rank_env));
    auto lctx = yacl::link::FactoryBrpc().CreateContext(desc, rank);
    lctx->ConnectToMesh();
    return lctx;
}

template <typename T>
static T ParseToken(const std::string& token) {
    if constexpr (std::is_floating_point_v<T>) {
        return static_cast<T>(std::stold(token));
    } else {
        return static_cast<T>(std::stoll(token));
    }
}

template <typename T>
std::vector<xt::xarray<T>> ReadTableColumns(const std::string& file_name,
                                            TableDataInfo* info = nullptr) {
    const std::string file_path = ResolveDataFile(file_name);
    std::ifstream input(file_path);
    if (!input.is_open()) {
        throw std::runtime_error("Failed to open input data file: " + file_path);
    }

    std::vector<std::vector<T>> rows;
    std::string line;
    size_t expected_cols = 0;

    while (std::getline(input, line)) {
        if (line.empty()) {
            continue;
        }

        std::istringstream iss(line);
        std::vector<T> row;
        std::string token;
        while (iss >> token) {
            row.push_back(ParseToken<T>(token));
        }

        if (row.empty()) {
            continue;
        }
        if (expected_cols == 0) {
            expected_cols = row.size();
        } else if (row.size() != expected_cols) {
            throw std::runtime_error("Input file has inconsistent column counts: " + file_path);
        }
        rows.push_back(std::move(row));
    }

    if (rows.empty() || expected_cols == 0) {
        throw std::runtime_error("Input file contains no table data: " + file_path);
    }

    std::vector<xt::xarray<T>> columns(expected_cols);
    for (auto& col : columns) {
        col.resize(std::vector<size_t>{rows.size()});
    }
    for (size_t r = 0; r < rows.size(); ++r) {
        for (size_t c = 0; c < expected_cols; ++c) {
            columns[c](r) = rows[r][c];
        }
    }

    if (info != nullptr) {
        info->rows = rows.size();
        info->cols = expected_cols;
        info->path = file_path;
    }
    return columns;
}

template <typename T>
void SplitKeyPayloadColumns(std::vector<xt::xarray<T>> columns, size_t num_keys,
                            std::vector<xt::xarray<T>>& keys,
                            std::vector<xt::xarray<T>>& payloads) {
    if (num_keys == 0 || num_keys > columns.size()) {
        throw std::invalid_argument("num_keys must be in [1, num_columns].");
    }

    keys.assign(std::make_move_iterator(columns.begin()),
                std::make_move_iterator(columns.begin() + num_keys));
    payloads.assign(std::make_move_iterator(columns.begin() + num_keys),
                    std::make_move_iterator(columns.end()));
}

// ============================================================================
// 4. Core test runner with time, communication, and memory metrics.
// ============================================================================
template <typename T>
void RunSimpleSortTest(size_t npc, FieldType field, ProtocolKind prot, RuntimeConfig::SortMethod method,
                       const std::vector<xt::xarray<T>>& keys, const std::vector<xt::xarray<T>>& payloads,
                       size_t num_keys) {
    auto run_one_party = [&](const std::shared_ptr<yacl::link::Context>& lctx) {
        RuntimeConfig cfg;
        cfg.protocol = prot;
        cfg.field = field;
        cfg.enable_action_trace = false;
        cfg.sort_method = method;

        SPUContext ctx = test::makeSPUContext(cfg, lctx);

        std::vector<Value> operands;
        operands.reserve(keys.size() + payloads.size());
        for (const auto& k : keys) operands.push_back(test::makeValue(&ctx, k, VIS_SECRET));
        for (const auto& p : payloads) operands.push_back(test::makeValue(&ctx, p, VIS_SECRET));

        size_t comm_before = 0;
        try {
            auto stats = lctx->GetStats();
            if (stats) {
                comm_before = static_cast<size_t>(stats->sent_bytes);
            }
        } catch (...) {}

        MemorySampler mem_sampler;
        auto start_time = std::chrono::steady_clock::now();
        auto rets = SimpleSort(&ctx, operands, 0, hal::SortDirection::Ascending, num_keys);
        auto end_time = std::chrono::steady_clock::now();
        mem_sampler.Stop();
        const double party_time = std::chrono::duration<double>(end_time - start_time).count();
        const size_t party_sent = static_cast<size_t>(lctx->GetStats()->sent_bytes) - comm_before;
        std::cout << "SORT_PARTY_CSV," << lctx->Rank() << "," << keys[0].size()
                  << "," << operands.size() << "," << num_keys
                  << "," << magic_enum::enum_name(prot) << "," << magic_enum::enum_name(field)
                  << "," << magic_enum::enum_name(method) << ","
                  << std::setprecision(12) << party_time << "," << party_sent
                  << "," << mem_sampler.GetPeak() << std::endl;

        // Only party 0 reports benchmark metrics.
        if (lctx->Rank() == 0) {
            double duration_s = std::chrono::duration<double>(end_time - start_time).count();
            size_t peak_mem_kb = mem_sampler.GetPeak();
            size_t comm_bytes = 0;
            
            try {
                auto stats = lctx->GetStats();
                if (stats) {
                    size_t comm_after = static_cast<size_t>(stats->sent_bytes);
                    comm_bytes = comm_after >= comm_before ? comm_after - comm_before : comm_after;
                }
            } catch (...) {}

            double peak_mem_mb = peak_mem_kb / 1024.0;
            double comm_mb = comm_bytes / (1024.0 * 1024.0);

            std::cout << "[Perf] SortMethod=" << magic_enum::enum_name(method)
                      << " | Time=" << std::fixed << std::setprecision(3) << duration_s << "s"
                      << " | Comm=" << std::setprecision(2) << comm_mb << "MB"
                      << " | PeakMem=" << std::setprecision(2) << peak_mem_mb << "MB" << std::endl;

            ::testing::Test::RecordProperty("sort_method", std::string(magic_enum::enum_name(method)));
            ::testing::Test::RecordProperty("exec_time_s", duration_s);
            ::testing::Test::RecordProperty("comm_mb", comm_mb);
            ::testing::Test::RecordProperty("peak_mem_mb", peak_mem_mb);
        }
        if (EnvInt("FES_REVISION_BENCH", 0) == 1) {
            const size_t rows = keys[0].size();
            std::vector<std::vector<T>> actual(rows), expected(rows);
            for (size_t col = 0; col < rets.size(); ++col) {
                Value clear = hal::_s2p(&ctx, rets[col]).setDtype(rets[col].dtype());
                auto decoded = hal::dump_public_as<T>(&ctx, clear);
                const auto& source = col < keys.size() ? keys[col] : payloads[col - keys.size()];
                if (decoded.size() != rows) throw std::runtime_error("Wrong result cardinality");
                for (size_t i = 0; i < rows; ++i) {
                    actual[i].push_back(decoded.flat(i)); expected[i].push_back(source.flat(i));
                }
            }
            for (size_t i = 1; i < rows; ++i) {
                if (std::lexicographical_compare(actual[i].begin(), actual[i].begin()+num_keys,
                                                actual[i-1].begin(), actual[i-1].begin()+num_keys))
                    throw std::runtime_error("Unsorted output keys");
            }
            std::sort(actual.begin(),actual.end()); std::sort(expected.begin(),expected.end());
            if (actual != expected) throw std::runtime_error("Full tuple multiset differs from input");
            std::cout << "SORT_TUPLES_VERIFIED rows=" << rows << " cols=" << rets.size()
                      << " rank=" << lctx->Rank() << std::endl;
        }
    };

    auto lan_lctx = TryMakeLanContext(npc);
    if (lan_lctx != nullptr) {
        run_one_party(lan_lctx);
        return;
    }
    if (EnvInt("FES_REVISION_BENCH", 0) == 1)
        throw std::runtime_error("Revision experiments require real worker processes and LAN context");

    mpc::utils::simulate(npc, run_one_party);
}

// ============================================================================
// 5. File-driven parameterized benchmark cases.
// ============================================================================

static std::vector<RuntimeConfig::SortMethod> BenchSortMethods() {
    return {
        RuntimeConfig::SORT_NETWORK,
        RuntimeConfig::SORT_RADIX,
        RuntimeConfig::SORT_QUICK,
    };
}

static std::string SortMethodName(RuntimeConfig::SortMethod method) {
    return std::string(magic_enum::enum_name(method));
}

static std::string ProtocolName(ProtocolKind protocol) {
    return std::string(magic_enum::enum_name(protocol));
}

static std::string FileSortTestName(const testing::TestParamInfo<FileSortCase>& info) {
    return SanitizeTestName(info.param.name);
}

static std::vector<std::pair<TestDType, std::string>> DTypeCases() {
    return {
        {TestDType::INT8, "int8"},
        {TestDType::INT32, "int32"},
        {TestDType::INT64, "int64"},
    };
}

// R5/B1 row-count sensitivity.
static std::vector<FileSortCase> BuildScaleCases() {
    std::vector<FileSortCase> cases;
    for (int pow : {EnvInt("FES_BENCH_POW", 10)}) {
        for (auto method : BenchSortMethods()) {
            FileSortCase c;
            c.name = "R5_Rows_N2pow" + std::to_string(pow) + "_" + SortMethodName(method);
            c.file_name = "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Uniform.txt";
            c.num_keys = 1;
            c.method = method;
            cases.push_back(std::move(c));
        }
    }
    return cases;
}

static std::vector<FileSortCase> BuildDefaultAlgorithmCases() {
    std::vector<FileSortCase> cases;
    const int pow = EnvInt("FES_BENCH_POW", 10);
    const std::vector<std::pair<RuntimeConfig::SortMethod, std::string>> slots = {
        {RuntimeConfig::SORT_NETWORK, "R2_algorithm_odd_even"},
        {RuntimeConfig::SORT_RADIX, "R3_algorithm_radix"},
        {RuntimeConfig::SORT_QUICK, "R4_algorithm_quick"},
    };
    for (const auto& [method, slot] : slots) {
        FileSortCase c;
        c.name = slot + "_" + SortMethodName(method);
        c.file_name = "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Uniform.txt";
        c.num_keys = 1;
        c.method = method;
        cases.push_back(std::move(c));
    }
    return cases;
}

static std::vector<FileSortCase> BuildKeyCases() {
    std::vector<FileSortCase> cases;
    const int pow = EnvInt("FES_BENCH_POW", 10);
    for (int keys : {1, 2, 3, 4, 8}) {
        for (auto method : BenchSortMethods()) {
            FileSortCase c;
            c.name = "R6_Keys_K" + std::to_string(keys) + "_" + SortMethodName(method);
            c.file_name = "data_2pow" + std::to_string(pow) + "_m" + std::to_string(keys) + "_k" + std::to_string(keys) + "_int32_Uniform.txt";
            c.num_keys = keys;
            c.method = method;
            cases.push_back(std::move(c));
        }
    }
    return cases;
}

static std::vector<FileSortCase> BuildPayloadCases() {
    std::vector<FileSortCase> cases;
    const int pow = EnvInt("FES_BENCH_POW", 10);
    for (int payloads : {1, 2, 3, 4, 8}) {
        for (auto method : BenchSortMethods()) {
            FileSortCase c;
            c.name = "R7_Payloads_P" + std::to_string(payloads) + "_" + SortMethodName(method);
            c.file_name = "data_2pow" + std::to_string(pow) + "_m" + std::to_string(payloads + 1) + "_k1_int32_Uniform.txt";
            c.num_keys = 1;
            c.method = method;
            cases.push_back(std::move(c));
        }
    }
    return cases;
}

static std::vector<FileSortCase> BuildDTypeCases() {
    std::vector<FileSortCase> cases;
    const int pow = EnvInt("FES_BENCH_POW", 10);
    for (const auto& [dtype, name] : DTypeCases()) {
        for (auto method : BenchSortMethods()) {
            FileSortCase c;
            c.name = "R8_KeyType_" + name + "_" + SortMethodName(method);
            c.file_name = "data_2pow" + std::to_string(pow) + "_m2_k1_" + name + "_Uniform.txt";
            c.num_keys = 1;
            c.dtype = dtype;
            c.field = (dtype == TestDType::INT64) ? FieldType::FM64 : FieldType::FM32;
            c.method = method;
            cases.push_back(std::move(c));
        }
    }
    return cases;
}

static std::vector<FileSortCase> BuildDistributionCases() {
    std::vector<FileSortCase> cases;
    const int pow = EnvInt("FES_BENCH_POW", 10);
    const std::vector<std::string> files = {
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Zipf_z0p5.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Zipf_z1p0.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Zipf_z1p2.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Zipf_z1p5.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Zipf_z2p0.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_PartiallySorted_s0.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_PartiallySorted_s25.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_PartiallySorted_s50.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_PartiallySorted_s75.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_PartiallySorted_s100.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_HighRepeat_r10.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_HighRepeat_r30.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_HighRepeat_r50.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_HighRepeat_r70.txt",
        "data_2pow" + std::to_string(pow) + "_m2_k1_int32_HighRepeat_r90.txt",
    };
    for (const auto& file : files) {
        for (auto method : BenchSortMethods()) {
            FileSortCase c;
            c.name = "R9_Distribution_" + file + "_" + SortMethodName(method);
            c.file_name = file;
            c.num_keys = 1;
            c.method = method;
            cases.push_back(std::move(c));
        }
    }
    return cases;
}

static std::vector<FileSortCase> BuildProtocolCases() {
    std::vector<FileSortCase> cases;
    if (EnvInt("FES_REVISION_BENCH", 0) == 1) {
        const int pow = EnvInt("FES_BENCH_POW", 16);
        for (const auto protocol : {ProtocolKind::ABY3, ProtocolKind::SWIFT}) {
            FileSortCase c;
            c.name = "R10_Protocol_" + std::string(protocol == ProtocolKind::ABY3 ? "semi_honest" : "malicious") + "_SORT_NETWORK";
            c.file_name = "data_2pow" + std::to_string(pow) + "_m2_k1_int32_Uniform.txt";
            c.num_keys = 1; c.npc = 3; c.protocol = protocol;
            c.field = FieldType::FM64; c.method = RuntimeConfig::SORT_NETWORK;
            cases.push_back(std::move(c));
        }
        return cases;
    }
    const std::vector<std::pair<ProtocolKind, size_t>> protocols = {
        {ProtocolKind::SEMI2K, 2},
        {ProtocolKind::ABY3, 3},
        {ProtocolKind::CHEETAH, 2},
    };
    for (const auto& [protocol, npc] : protocols) {
        for (auto method : BenchSortMethods()) {
            FileSortCase c;
            c.name = "R10_Protocol_" + ProtocolName(protocol) + "_" + SortMethodName(method);
            c.file_name = "data_2pow10_m2_k1_int32_Uniform.txt";
            c.num_keys = 1;
            c.npc = npc;
            c.protocol = protocol;
            c.method = method;
            cases.push_back(std::move(c));
        }
    }
    return cases;
}

class DefaultAlgorithmSortTest : public ::testing::TestWithParam<FileSortCase> {};
class ScaleSortTest : public ::testing::TestWithParam<FileSortCase> {};
class KeySortTest : public ::testing::TestWithParam<FileSortCase> {};
class PayloadSortTest : public ::testing::TestWithParam<FileSortCase> {};
class DTypeSortTest : public ::testing::TestWithParam<FileSortCase> {};
class DistributionSortTest : public ::testing::TestWithParam<FileSortCase> {};
class ProtocolSortTest : public ::testing::TestWithParam<FileSortCase> {};

static void RunInt8FileSortCase(const FileSortCase& c) {
    TableDataInfo info;
    auto columns = ReadTableColumns<int8_t>(c.file_name, &info);
    std::vector<xt::xarray<int8_t>> keys, payloads;
    SplitKeyPayloadColumns(std::move(columns), c.num_keys, keys, payloads);
    std::cout << "[Input] Case=" << c.name << " | File=" << info.path
              << " | Rows=" << info.rows << " | Cols=" << info.cols
              << " | NumKeys=" << c.num_keys << std::endl;
    RunSimpleSortTest(c.npc, c.field, c.protocol, c.method, keys, payloads, c.num_keys);
}

static void RunInt32FileSortCase(const FileSortCase& c) {
    TableDataInfo info;
    auto columns = ReadTableColumns<int32_t>(c.file_name, &info);
    std::vector<xt::xarray<int32_t>> keys, payloads;
    SplitKeyPayloadColumns(std::move(columns), c.num_keys, keys, payloads);

    std::cout << "[Input] Case=" << c.name
              << " | File=" << info.path
              << " | Rows=" << info.rows
              << " | Cols=" << info.cols
              << " | NumKeys=" << c.num_keys << std::endl;

    RunSimpleSortTest(c.npc, c.field, c.protocol, c.method, keys, payloads, c.num_keys);
}

static void RunInt64FileSortCase(const FileSortCase& c) {
    TableDataInfo info;
    auto columns = ReadTableColumns<int64_t>(c.file_name, &info);
    std::vector<xt::xarray<int64_t>> keys, payloads;
    SplitKeyPayloadColumns(std::move(columns), c.num_keys, keys, payloads);
    std::cout << "[Input] Case=" << c.name << " | File=" << info.path
              << " | Rows=" << info.rows << " | Cols=" << info.cols
              << " | NumKeys=" << c.num_keys << std::endl;
    RunSimpleSortTest(c.npc, c.field, c.protocol, c.method, keys, payloads, c.num_keys);
}

static void RunFileSortCase(const FileSortCase& c) {
    if (c.dtype == TestDType::INT8) {
        RunInt8FileSortCase(c);
    } else if (c.dtype == TestDType::INT64) {
        RunInt64FileSortCase(c);
    } else {
        RunInt32FileSortCase(c);
    }
}

TEST_P(DefaultAlgorithmSortTest, Run) {
    RunFileSortCase(GetParam());
}

TEST_P(ScaleSortTest, Run) {
    RunFileSortCase(GetParam());
}

TEST_P(KeySortTest, Run) {
    RunFileSortCase(GetParam());
}

TEST_P(PayloadSortTest, Run) {
    RunFileSortCase(GetParam());
}

TEST_P(DTypeSortTest, Run) {
    RunFileSortCase(GetParam());
}

TEST_P(DistributionSortTest, Run) {
    RunFileSortCase(GetParam());
}

TEST_P(ProtocolSortTest, Run) {
    RunFileSortCase(GetParam());
}

INSTANTIATE_TEST_SUITE_P(DefaultAlgorithmInstances, DefaultAlgorithmSortTest,
                         testing::ValuesIn(BuildDefaultAlgorithmCases()),
                         FileSortTestName);

INSTANTIATE_TEST_SUITE_P(ScaleInstances, ScaleSortTest,
                         testing::ValuesIn(BuildScaleCases()),
                         FileSortTestName);

INSTANTIATE_TEST_SUITE_P(KeyInstances, KeySortTest,
                         testing::ValuesIn(BuildKeyCases()),
                         FileSortTestName);

INSTANTIATE_TEST_SUITE_P(PayloadInstances, PayloadSortTest,
                         testing::ValuesIn(BuildPayloadCases()),
                         FileSortTestName);

INSTANTIATE_TEST_SUITE_P(DTypeInstances, DTypeSortTest,
                         testing::ValuesIn(BuildDTypeCases()),
                         FileSortTestName);

INSTANTIATE_TEST_SUITE_P(DistributionInstances, DistributionSortTest,
                         testing::ValuesIn(BuildDistributionCases()),
                         FileSortTestName);

INSTANTIATE_TEST_SUITE_P(ProtocolInstances, ProtocolSortTest,
                         testing::ValuesIn(BuildProtocolCases()),
                         FileSortTestName);

} // namespace spu::kernel::hlo
