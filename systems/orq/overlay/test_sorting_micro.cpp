#include <algorithm>
#include <atomic>
#include <iostream>
#include <numeric>
#include <chrono>
#include <vector>
#include <string>
#include <cassert>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <type_traits>
#include <thread>
#include <mpi.h>
#include "orq.h"

using namespace orq::debug;
using namespace orq::service;
using namespace COMPILED_MPC_PROTOCOL_NAMESPACE;

// Read resident memory for the current process in KiB.
static long rss_kib() {
    std::ifstream status("/proc/self/status");
    std::string line;
    while (std::getline(status, line)) {
        if (line.find("VmRSS:") == 0) {
            size_t pos = line.find(':');
            std::string value_str = line.substr(pos + 1);
            value_str.erase(value_str.find("kB"));
            return std::stol(value_str);
        }
    }
    throw std::runtime_error("VmRSS unavailable");
}

class WindowRSS {
    std::atomic<bool> running{true};
    std::atomic<long> peak{0};
    std::thread sampler;
public:
    WindowRSS() : peak(rss_kib()), sampler([this] {
        while (running.load()) {
            long value = rss_kib(), previous = peak.load();
            while (value > previous && !peak.compare_exchange_weak(previous, value)) {}
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }) {}
    long stop() {
        long value = rss_kib(), previous = peak.load();
        while (value > previous && !peak.compare_exchange_weak(previous, value)) {}
        running = false;
        sampler.join();
        return peak.load();
    }
    ~WindowRSS() {
        if (sampler.joinable()) {
            running = false;
            sampler.join();
        }
    }
};

static std::string to_lower(std::string s) {
    for (char& ch : s) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return s;
}

static bool has_txt_suffix(const std::string& path) {
    return path.size() >= 4 && path.substr(path.size() - 4) == ".txt";
}

static orq::SortingProtocol parse_sorting_protocol(const std::string& value) {
    std::string v = to_lower(value);

    if (v == "bitonic" || v == "bitonicsort" || v == "0") {
        return orq::SortingProtocol::BITONICSORT;
    }
    if (v == "radix" || v == "radixsort" || v == "1") {
        return orq::SortingProtocol::RADIXSORT;
    }
    if (v == "quick" || v == "quicksort" || v == "2") {
        return orq::SortingProtocol::QUICKSORT;
    }

    throw std::invalid_argument("Unknown sort protocol: " + value);
}

static std::string protocol_to_string(orq::SortingProtocol protocol) {
    switch (protocol) {
        case orq::SortingProtocol::BITONICSORT: return "BITONICSORT";
        case orq::SortingProtocol::RADIXSORT: return "RADIXSORT";
        case orq::SortingProtocol::QUICKSORT: return "QUICKSORT";
        default: return "UNKNOWN";
    }
}

template <typename T>
static T parse_token(const std::string& token) {
    if constexpr (std::is_floating_point_v<T>) {
        return static_cast<T>(std::stold(token));
    } else {
        return static_cast<T>(std::stoll(token));
    }
}

template <typename T>
static std::vector<std::vector<T>> read_table_file(const std::string& file_path,
                                                   int& num_rows,
                                                   int& num_columns) {
    if (!has_txt_suffix(file_path)) {
        throw std::invalid_argument("Input file must have .txt suffix: " + file_path);
    }

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
        std::string token;

        while (iss >> token) {
            row.push_back(parse_token<T>(token));
        }

        if (row.empty()) {
            continue;
        }

        if (expected_columns == -1) {
            expected_columns = static_cast<int>(row.size());
        } else if (static_cast<int>(row.size()) != expected_columns) {
            throw std::runtime_error("Input file has inconsistent column counts.");
        }

        rows.push_back(std::move(row));
    }

    if (rows.empty()) {
        throw std::runtime_error("Input file contains no table data.");
    }

    num_rows = static_cast<int>(rows.size());
    num_columns = expected_columns;

    std::vector<std::vector<T>> columns(num_columns, std::vector<T>(num_rows));
    for (int r = 0; r < num_rows; ++r) {
        for (int c = 0; c < num_columns; ++c) {
            columns[c][r] = rows[r][c];
        }
    }

    return columns;
}

static std::vector<std::string> build_schema(int num_columns) {
    std::vector<std::string> schema;
    schema.reserve(num_columns);

    for (int i = 0; i < num_columns; i++) {
        schema.push_back("[" + std::to_string(i) + "]");
    }

    return schema;
}

static std::vector<std::pair<std::string, SortOrder>> build_sort_spec(int num_columns,
                                                                      int num_keys) {
    if (num_keys <= 0) {
        throw std::invalid_argument("num_keys must be positive.");
    }

    std::vector<std::pair<std::string, SortOrder>> spec;

    for (int i = 0; i < num_columns && static_cast<int>(spec.size()) < num_keys; ++i) {
        spec.push_back(std::make_pair("[" + std::to_string(i) + "]", ASC));
    }

    if (static_cast<int>(spec.size()) != num_keys) {
        throw std::invalid_argument("num_keys is too large for the input table columns.");
    }

    return spec;
}

// Load a two-dimensional input table and call table1.sort(spec, protocol).
template <typename T>
void test_table_sort_from_file(const std::string& input_file,
                               int num_keys,
                               orq::SortingProtocol protocol,
                               const std::string& dtype_str) {
    int num_rows = 0;
    int num_columns = 0;

    std::vector<std::vector<T>> plain_data =
        read_table_file<T>(input_file, num_rows, num_columns);

    std::vector<std::string> schema = build_schema(num_columns);
    std::vector<std::pair<std::string, SortOrder>> spec =
        build_sort_spec(num_columns, num_keys);

    std::vector<orq::Vector<T>> table_data;
    table_data.reserve(num_columns);

    for (int i = 0; i < num_columns; i++) {
        orq::Vector<T> col(num_rows);
        for (int j = 0; j < num_rows; j++) {
            col[j] = plain_data[i][j];
        }
        table_data.push_back(std::move(col));
    }

    EncodedTable<T> table = secret_share(table_data, schema);

    size_t comm_before = 0;
    int num_threads = orq::service::runTime->get_num_threads();
    for (int i = 0; i < num_threads; ++i) {
        comm_before += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }

    WindowRSS rss;
    auto start = std::chrono::high_resolution_clock::now();
    table.sort(spec, protocol);
    auto end = std::chrono::high_resolution_clock::now();
    const long sort_peak_rss_kib = rss.stop();

    double time_s =
        std::chrono::duration<double, std::milli>(end - start).count() / 1000.0;

    size_t comm_after = 0;
    for (int i = 0; i < num_threads; ++i) {
        comm_after += orq::service::runTime->workers[i].getCommunicator()->getBytesSent();
    }

    size_t sort_comm_bytes = comm_after - comm_before;
    double sort_comm_MB = static_cast<double>(sort_comm_bytes) / (1024.0 * 1024.0);
    double mem_peak_MB = static_cast<double>(sort_peak_rss_kib) / 1024.0;
    double max_time_s = 0.0;
    double sum_comm_MB = 0.0;
    double max_mem_peak_MB = 0.0;
    MPI_Reduce(&time_s, &max_time_s, 1, MPI_DOUBLE, MPI_MAX, 0, MPI_COMM_WORLD);
    MPI_Reduce(&sort_comm_MB, &sum_comm_MB, 1, MPI_DOUBLE, MPI_SUM, 0, MPI_COMM_WORLD);
    MPI_Reduce(&mem_peak_MB, &max_mem_peak_MB, 1, MPI_DOUBLE, MPI_MAX, 0, MPI_COMM_WORLD);

    std::string proto_name = protocol_to_string(protocol);

    if (protocol == orq::SortingProtocol::BITONICSORT) {
        double residual_time_s =
            time_s > orq::operators::last_bitonic_profile_time_s
                ? time_s - orq::operators::last_bitonic_profile_time_s
                : 0.0;
        double residual_comm_MB =
            sort_comm_MB > orq::operators::last_bitonic_profile_comm_MB
                ? sort_comm_MB - orq::operators::last_bitonic_profile_comm_MB
                : 0.0;
        single_cout("RESULT_BITONIC_CSV,"
                    << "TABLE_SORT_RESIDUAL" << ","
                    << 10 << ","
                    << 1 << ","
                    << 56 << ","
                    << num_rows << ","
                    << 0 << ","
                    << num_keys << ","
                    << residual_time_s << ","
                    << residual_comm_MB);
    }

    // Public benchmark inputs may be opened after all sorting measurements.
    // Check tuple preservation independently of the order of equal-key rows.
    const char* verify_public = std::getenv("FES_PUBLIC_BENCHMARK");
    if (verify_public != nullptr && std::string(verify_public) == "1") {
        auto opened = table.open();
        std::vector<std::vector<T>> expected_rows(num_rows, std::vector<T>(num_columns));
        std::vector<std::vector<T>> actual_rows(num_rows, std::vector<T>(num_columns));
        int valid = static_cast<int>(opened.size()) == num_columns ? 1 : 0;
        if (valid) {
            for (int column = 0; column < num_columns; ++column) {
                if (static_cast<int>(opened[column].size()) != num_rows) {
                    valid = 0;
                    break;
                }
                for (int row = 0; row < num_rows; ++row) {
                    expected_rows[row][column] = plain_data[column][row];
                    actual_rows[row][column] = opened[column][row];
                }
            }
        }
        if (valid) {
            for (int row = 1; row < num_rows; ++row) {
                if (std::lexicographical_compare(actual_rows[row].begin(),
                                                  actual_rows[row].begin() + num_keys,
                                                  actual_rows[row - 1].begin(),
                                                  actual_rows[row - 1].begin() + num_keys)) {
                    valid = 0;
                    break;
                }
            }
            std::sort(expected_rows.begin(), expected_rows.end());
            std::sort(actual_rows.begin(), actual_rows.end());
            valid = valid && expected_rows == actual_rows;
        }
        int all_valid = 0;
        MPI_Allreduce(&valid, &all_valid, 1, MPI_INT, MPI_MIN, MPI_COMM_WORLD);
        if (!all_valid) {
            throw std::runtime_error("Public benchmark tuple verification failed");
        }
        single_cout("SORT_TUPLES_VERIFIED");
    }

    // single_cout("RESULT_CSV,InputFile,NumRows,NumCols,NumKeys,Protocol,DataType,Stage,Round,Time_s,Comm_MB,Mem_MB");
    single_cout("RESULT_CSV,"
                << input_file << ","
                << num_rows << ","
                << num_columns << ","
                << num_keys << ","
                << proto_name << ","
                << dtype_str << ","
                << "total" << ","
                << 0 << ","
                << max_time_s << ","
                << sum_comm_MB << ","
                << max_mem_peak_MB);

}

static std::string get_arg_value(int argc, char** argv,
                                 const std::string& name,
                                 const std::string& default_value = "") {
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];

        if (arg == name && i + 1 < argc) {
            return argv[i + 1];
        }

        std::string prefix = name + "=";
        if (arg.find(prefix) == 0) {
            return arg.substr(prefix.size());
        }
    }

    return default_value;
}

static void print_usage(const char* program) {
    single_cout("Usage:");
    single_cout(program << " --input data.txt --sort-protocol bitonic --num-keys 1 --data-type int32");
    single_cout("Supported --sort-protocol: bitonic, radix, quick, 0, 1, 2");
    single_cout("Supported --data-type: int8, int32, int64");
}

int main(int argc, char** argv) {
    orq_init(argc, argv);

    std::string input_file = get_arg_value(argc, argv, "--input");
    std::string protocol_str = get_arg_value(argc, argv, "--sort-protocol", "bitonic");
    std::string num_keys_str = get_arg_value(argc, argv, "--num-keys", "1");
    std::string dtype_str = to_lower(get_arg_value(argc, argv, "--data-type", "int32"));

    if (input_file.empty()) {
        print_usage(argv[0]);
        return 1;
    }

    try {
        int num_keys = std::stoi(num_keys_str);
        orq::SortingProtocol protocol = parse_sorting_protocol(protocol_str);

        if (dtype_str == "int8") {
            test_table_sort_from_file<int8_t>(input_file, num_keys, protocol, "int8");
        } else if (dtype_str == "int32" || dtype_str == "int") {
            test_table_sort_from_file<int32_t>(input_file, num_keys, protocol, "int32");
        } else if (dtype_str == "int64") {
            test_table_sort_from_file<int64_t>(input_file, num_keys, protocol, "int64");
        } else {
            throw std::invalid_argument("Unknown data type: " + dtype_str);
        }
    } catch (const std::exception& e) {
        single_cout("Error: " << e.what());
        print_usage(argv[0]);
        return 1;
    }

    return 0;
}
