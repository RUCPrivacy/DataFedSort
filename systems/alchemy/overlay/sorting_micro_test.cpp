#include <gflags/gflags.h>
#include <gtest/gtest.h>
#include <iostream>
#include <vector>
#include <algorithm>
#include <iomanip>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <iterator>
#include <memory>
#include <cstring>
#include <openssl/sha.h>
#include <util/data_utilities.h>
#include <test/mpc/emp_base_test.h>
#include <operators/sort.h>
#include <query_table/plain_tuple.h>
#include <util/utilities.h>

using namespace emp;
using namespace vaultdb;

DEFINE_int32(party, 1, "party for EMP execution (1=Alice, 2=Bob)");
DEFINE_int32(port, 7654, "port for EMP execution");
DEFINE_string(alice_host, "127.0.0.1", "alice hostname for EMP execution");
DEFINE_int32(rows, 100, "unused; kept for compatibility");
DEFINE_int32(cols, 3, "unused; kept for compatibility");
DEFINE_int32(sort_cols, 1, "number of leading columns to use as sort keys");
DEFINE_string(data_type, "int32", "data type for sorting (bool, int32, int64, float)");
DEFINE_string(input, "", "path to the input table file");

DEFINE_string(unioned_db, "tpch_unioned_1500", "unioned db name");
DEFINE_string(alice_db, "tpch_alice_1500", "alice db name");
DEFINE_string(bob_db, "tpch_bob_1500", "bob db name");
DEFINE_int32(cutoff, -1, "limit clause for queries");
DEFINE_int32(ctrl_port, 65482, "port for managing EMP control flow by passing public values");
DEFINE_bool(validation, false, "Validate authorized, hash-verified public benchmark output after timing");
DEFINE_string(filter, "*", "run only the tests passing this filter");
DEFINE_string(storage, "column", "storage model for columns (column, wire_packed or compressed)");

class SortingMicroTest : public EmpBaseTest {
protected:
    void runMicroBenchmark(const std::string& input_file, int k, const std::string& type_str);
};

static std::string toLower(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

static FieldType parseFieldType(const std::string& type_str) {
    std::string t = toLower(type_str);
    if (t == "bool") return FieldType::BOOL;
    if (t == "int" || t == "int32") return FieldType::INT;
    if (t == "long" || t == "int64") return FieldType::LONG;
    if (t == "float") return FieldType::FLOAT;

    std::cerr << "Warning: Unknown data type '" << type_str << "', defaulting to int32" << std::endl;
    return FieldType::INT;
}

template <typename T>
static std::vector<std::vector<T>> readTableFile(const std::string& file_path) {
    std::ifstream input(file_path);
    if (!input.is_open()) {
        throw std::runtime_error("Failed to open input file: " + file_path);
    }

    std::vector<std::vector<T>> rows;
    std::string line;
    int expected_columns = -1;

    while (std::getline(input, line)) {
        if (line.empty()) {
            continue;
        }

        std::istringstream iss(line);
        std::vector<T> row;
        T value;

        while (iss >> value) {
            row.push_back(value);
        }

        if (row.empty()) {
            continue;
        }

        if (expected_columns == -1) {
            expected_columns = static_cast<int>(row.size());
        } else if (static_cast<int>(row.size()) != expected_columns) {
            throw std::runtime_error("Input file has inconsistent column counts: " + file_path);
        }

        rows.push_back(std::move(row));
    }

    if (rows.empty()) {
        throw std::runtime_error("Input file contains no table data: " + file_path);
    }

    return rows;
}

static void setPlainField(PlainTable* table, int row, int col, FieldType field_type, const std::string& value) {
    switch (field_type) {
        case FieldType::BOOL:
            table->setField(row, col, PlainField(FieldType::BOOL, std::stoll(value) != 0));
            break;
        case FieldType::INT:
            table->setField(row, col, PlainField(FieldType::INT, static_cast<int32_t>(std::stol(value))));
            break;
        case FieldType::LONG:
            table->setField(row, col, PlainField(FieldType::LONG, static_cast<int64_t>(std::stoll(value))));
            break;
        case FieldType::FLOAT:
            table->setField(row, col, PlainField(FieldType::FLOAT, std::stof(value)));
            break;
        default:
            throw std::runtime_error("Unsupported field type.");
    }
}

static void exchangeUint64(EmpManager* manager, int party, uint64_t mine, uint64_t& peer) {
    int64_t mine_low = static_cast<int64_t>(mine & 0xFFFFFFFF);
    int64_t mine_high = static_cast<int64_t>((mine >> 32) & 0xFFFFFFFF);

    if (party == emp::ALICE) {
        manager->sendPublic(static_cast<int>(mine_low));
        manager->sendPublic(static_cast<int>(mine_high));
        int64_t peer_low = manager->recvPublic();
        int64_t peer_high = manager->recvPublic();
        peer = (static_cast<uint64_t>(peer_high) << 32) |
               static_cast<uint64_t>(peer_low & 0xFFFFFFFF);
    } else {
        int64_t peer_low = manager->recvPublic();
        int64_t peer_high = manager->recvPublic();
        peer = (static_cast<uint64_t>(peer_high) << 32) |
               static_cast<uint64_t>(peer_low & 0xFFFFFFFF);
        manager->sendPublic(static_cast<int>(mine_low));
        manager->sendPublic(static_cast<int>(mine_high));
    }
}

static size_t readProcStatusKb(const std::string& key) {
    std::ifstream status("/proc/self/status");
    if (!status.is_open()) {
        return 0;
    }

    std::string line;
    while (std::getline(status, line)) {
        if (line.rfind(key, 0) == 0) {
            std::istringstream iss(line.substr(key.size()));
            size_t value_kb = 0;
            iss >> value_kb;
            return value_kb;
        }
    }

    return 0;
}

static void requirePublicInputHash(const std::string& input_file) {
    const char* enabled = std::getenv("FES_PUBLIC_BENCHMARK");
    const char* configured_hash = std::getenv("FES_INPUT_SHA256");
    if (!enabled || std::string(enabled) != "1")
        throw std::runtime_error("Output validation requires explicit public benchmark authorization");
    std::string expected = configured_hash ? configured_hash : "";
    if (expected.size() != 64 || !std::all_of(expected.begin(), expected.end(),
            [](unsigned char ch) { return std::isxdigit(ch) != 0; }))
        throw std::runtime_error("Public benchmark verification requires FES_INPUT_SHA256");
    expected = toLower(expected);
    std::ifstream source(input_file, std::ios::binary);
    if (!source) throw std::runtime_error("Cannot hash public benchmark input");
    SHA256_CTX state;
    SHA256_Init(&state);
    char buffer[65536];
    while (source.read(buffer, sizeof(buffer)) || source.gcount() > 0)
        SHA256_Update(&state, buffer, static_cast<size_t>(source.gcount()));
    if (source.bad()) throw std::runtime_error("Cannot read public benchmark input");
    unsigned char hash[SHA256_DIGEST_LENGTH];
    SHA256_Final(hash, &state);
    std::ostringstream actual;
    for (unsigned char value : hash)
        actual << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(value);
    if (actual.str() != expected)
        throw std::runtime_error("Public benchmark input does not match FES_INPUT_SHA256");
}

static int fieldBitCount(FieldType type) {
    if (type == FieldType::BOOL) return 1;
    if (type == FieldType::LONG) return 64;
    return 32;
}

static uint64_t encodedPlainField(const std::string& value, FieldType type) {
    if (type == FieldType::BOOL) return std::stoll(value) != 0;
    if (type == FieldType::INT) return static_cast<uint32_t>(std::stol(value));
    if (type == FieldType::LONG) return static_cast<uint64_t>(std::stoll(value));
    float parsed = std::stof(value);
    if (!std::isfinite(parsed)) throw std::runtime_error("Input contains a non-finite float");
    uint32_t encoded;
    std::memcpy(&encoded, &parsed, sizeof(encoded));
    return encoded;
}

static bool fieldLess(uint64_t a, uint64_t b, FieldType type) {
    if (type == FieldType::BOOL) return a < b;
    if (type == FieldType::INT) {
        uint32_t a_bits = static_cast<uint32_t>(a), b_bits = static_cast<uint32_t>(b);
        int32_t a_value, b_value;
        std::memcpy(&a_value, &a_bits, sizeof(a_value));
        std::memcpy(&b_value, &b_bits, sizeof(b_value));
        return a_value < b_value;
    }
    if (type == FieldType::LONG) {
        int64_t a_value, b_value;
        std::memcpy(&a_value, &a, sizeof(a_value));
        std::memcpy(&b_value, &b, sizeof(b_value));
        return a_value < b_value;
    }
    uint32_t a_bits = static_cast<uint32_t>(a), b_bits = static_cast<uint32_t>(b);
    float a_value, b_value;
    std::memcpy(&a_value, &a_bits, sizeof(a_value));
    std::memcpy(&b_value, &b_bits, sizeof(b_value));
    return a_value < b_value;
}

// Reveal only authorized public output labels, after measured counters stop.
// Direct label opening avoids the extra secure dummy-row sort in reveal().
static void validatePublicResult(EmpManager* manager, SecureTable* result,
                                 const std::vector<std::vector<std::string>>& input,
                                 int party, int keys, FieldType type) {
    const int rows = static_cast<int>(input.size());
    const int cols = static_cast<int>(input[0].size());
    if (result->tuple_cnt_ != static_cast<size_t>(rows))
        throw std::runtime_error("Wrong output cardinality");
    const int field_bits = fieldBitCount(type);
    const int bits_per_row = 1 + field_bits * cols;
    const int batch_rows = 4096;
    std::vector<std::vector<uint64_t>> actual(rows), expected(rows);
    bool correct = true;
    for (int begin = 0; begin < rows; begin += batch_rows) {
        const int count = std::min(batch_rows, rows - begin);
        const int bit_count = count * bits_per_row;
        std::vector<Bit> labels;
        labels.reserve(bit_count);
        for (int i = begin; i < begin + count; ++i) {
            labels.push_back(result->getDummyTag(i));
            for (int c = 0; c < cols; ++c) {
                const auto field = result->getField(i, c);
                if (field.payload_.size() != static_cast<size_t>(field_bits) * sizeof(Bit))
                    throw std::runtime_error("Unexpected public output field representation");
                const size_t offset = labels.size();
                labels.resize(offset + field_bits);
                std::memcpy(labels.data() + offset, field.payload_.data(), field.payload_.size());
            }
        }
        std::unique_ptr<bool[]> clear(new bool[bit_count]());
        manager->reveal(clear.get(), emp::ALICE, labels.data(), bit_count);
        manager->flush();
        if (party == emp::ALICE) {
            int cursor = 0;
            for (int i = begin; i < begin + count; ++i) {
                if (clear[cursor++]) correct = false;
                for (int c = 0; c < cols; ++c) {
                    uint64_t encoded = 0;
                    for (int bit = 0; bit < field_bits; ++bit)
                        if (clear[cursor++]) encoded |= uint64_t(1) << bit;
                    actual[i].push_back(encoded);
                    expected[i].push_back(encodedPlainField(input[i][c], type));
                }
            }
            std::cout << "VALIDATION_BATCH rows=" << begin + count
                      << "/" << rows << std::endl;
        }
    }
    if (party == emp::ALICE) {
        // Key ordering and tuple preservation are separate checks. Equal-key
        // payloads may be permuted because sorting stability is not required.
        correct = correct && std::is_sorted(actual.begin(), actual.end(),
            [keys, type](const std::vector<uint64_t>& a, const std::vector<uint64_t>& b) {
                for (int key = 0; key < keys; ++key) {
                    if (fieldLess(a[key], b[key], type)) return true;
                    if (fieldLess(b[key], a[key], type)) return false;
                }
                return false;
            });
        std::sort(actual.begin(), actual.end());
        std::sort(expected.begin(), expected.end());
        correct = correct && actual == expected;
    }
    uint64_t peer_status = 0;
    exchangeUint64(manager, party, party == emp::ALICE ? (correct ? 1 : 2) : 0,
                   peer_status);
    if ((party == emp::ALICE && !correct) ||
        (party == emp::BOB && peer_status != 1))
        throw std::runtime_error("Full sorted tuples or dummy tags differ from the public TPC-H oracle");
    std::cout << "SORT_TUPLES_VERIFIED rows=" << rows << " cols=" << cols << std::endl;
}

void SortingMicroTest::runMicroBenchmark(const std::string& input_file, int k, const std::string& type_str) {
    if (input_file.empty()) {
        throw std::runtime_error("Missing required --input argument.");
    }

    FieldType field_type = parseFieldType(type_str);
    std::vector<std::vector<std::string>> file_rows = readTableFile<std::string>(input_file);
    int n = static_cast<int>(file_rows.size());
    int m = static_cast<int>(file_rows[0].size());
    if (FLAGS_validation) {
        requirePublicInputHash(input_file);
    }

    if (k <= 0 || k > m) {
        throw std::runtime_error("sort_cols must satisfy 1 <= sort_cols <= number of input columns.");
    }

    QuerySchema schema;
    for (int c = 0; c < m; ++c) {
        schema.putField(QueryFieldDesc(c, "col" + std::to_string(c), "table", field_type, 0));
    }
    schema.initializeFieldOffsets();

    int local_rows = (FLAGS_party == emp::ALICE) ? n : 0;
    PlainTable* plain_data = PlainTable::getTable(local_rows, schema, StorageModel::ROW_STORE);

    if (FLAGS_party == emp::ALICE) {
        for (int i = 0; i < n; ++i) {
            for (int c = 0; c < m; ++c) {
                setPlainField(plain_data, i, c, field_type, file_rows[i][c]);
            }
        }
    }

    SecureTable* secure_data = manager_->secretShare(plain_data);
    for (size_t i = 0; i < secure_data->tuple_cnt_; ++i) {
        secure_data->setDummyTag(i, emp::Bit(false));
    }

    SortDefinition sort_def;
    for (int i = 0; i < k; ++i) {
        sort_def.push_back(ColumnSort(i, SortDirection::ASCENDING));
    }

    Sort<Bit>* sort_op = new Sort<Bit>(secure_data, sort_def);

    std::cout << "SORT_CALL_BEGIN rows=" << n << " cols=" << m << std::endl;
    auto start_gates = manager_->andGateCount();
    auto start_comm = manager_->getCommCost();

    auto start_time = clock_start();
    SecureTable* result = sort_op->run();

    double duration_sec = time_from(start_time) / 1e6;
    auto end_gates = manager_->andGateCount();
    auto end_comm = manager_->getCommCost();
    size_t peak_rss_kb = readProcStatusKb("VmHWM:");
    size_t current_rss_kb = readProcStatusKb("VmRSS:");
    std::cout << "SORT_CALL_END rows=" << n << " cols=" << m << std::endl;

    uint64_t my_comm = static_cast<uint64_t>(end_comm - start_comm);
    uint64_t peer_comm = 0;
    exchangeUint64(manager_, FLAGS_party, my_comm, peer_comm);

    uint64_t my_time_us = static_cast<uint64_t>(std::llround(duration_sec * 1000000.0));
    uint64_t peer_time_us = 0;
    exchangeUint64(manager_, FLAGS_party, my_time_us, peer_time_us);

    uint64_t my_peak_kb = static_cast<uint64_t>(peak_rss_kb);
    uint64_t peer_peak_kb = 0;
    exchangeUint64(manager_, FLAGS_party, my_peak_kb, peer_peak_kb);

    uint64_t my_current_kb = static_cast<uint64_t>(current_rss_kb);
    uint64_t peer_current_kb = 0;
    exchangeUint64(manager_, FLAGS_party, my_current_kb, peer_current_kb);

    uint64_t my_gates = static_cast<uint64_t>(end_gates - start_gates);
    uint64_t peer_gates = 0;
    exchangeUint64(manager_, FLAGS_party, my_gates, peer_gates);

    uint64_t total_comm = my_comm + peer_comm;
    double time_sec = static_cast<double>(std::max(my_time_us, peer_time_us)) / 1000000.0;
    double comm_mb = static_cast<double>(total_comm) / (1024.0 * 1024.0);
    double max_peak_rss_mb = static_cast<double>(std::max(my_peak_kb, peer_peak_kb)) / 1024.0;
    double max_current_rss_mb = static_cast<double>(std::max(my_current_kb, peer_current_kb)) / 1024.0;
    uint64_t max_gates = std::max(my_gates, peer_gates);

    if (FLAGS_party == emp::ALICE) {
        std::cout << std::fixed << std::setprecision(4);
        std::cout << "RESULT_CSV,InputFile,NumRows,NumCols,NumKeys,DataType,Stage,Round,Time_s,Comm_MB,PeakRSS_MB,CurrentRSS_MB,AND_Gates" << std::endl;
        std::cout << "RESULT_CSV,"
                  << input_file << ","
                  << n << ","
                  << m << ","
                  << k << ","
                  << toLower(type_str) << ","
                  << "total,0,"
                  << time_sec << ","
                  << comm_mb << ","
                  << max_peak_rss_mb << ","
                  << max_current_rss_mb << ","
                  << max_gates << std::endl;
        std::cout << "Computation Time (max party): " << time_sec << " s" << std::endl;
        std::cout << "Communication Volume (sum parties): " << comm_mb << " MB" << std::endl;
        std::cout << "Peak RSS (max party): " << max_peak_rss_mb << " MB" << std::endl;
        std::cout << "Current RSS (max party): " << max_current_rss_mb << " MB" << std::endl;
        std::cout << "AND Gates (max party): " << max_gates << std::endl;
    }

    if (FLAGS_validation) {
        // Public benchmark correctness only, outside the measured Sort::run().
        std::cout << "VALIDATION_CALL_BEGIN rows=" << n << " cols=" << m << std::endl;
        const auto validation_start = clock_start();
        validatePublicResult(manager_, result, file_rows, FLAGS_party, k, field_type);
        if (manager_->andGateCount() != end_gates)
            throw std::runtime_error("Output validation must not execute extra secure gates");
        std::cout << "VALIDATION_CALL_END seconds="
                  << time_from(validation_start) / 1e6 << " extra_AND_gates=0" << std::endl;
    }
    delete sort_op;
    delete plain_data;
}

TEST_F(SortingMicroTest, RunCustomSort) {
    runMicroBenchmark(FLAGS_input, FLAGS_sort_cols, FLAGS_data_type);
}

int main(int argc, char **argv) {
    ::testing::InitGoogleTest(&argc, argv);
    gflags::ParseCommandLineFlags(&argc, &argv, false);
    return RUN_ALL_TESTS();
}
