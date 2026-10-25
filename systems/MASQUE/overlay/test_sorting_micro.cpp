#include "../core/Utils.h"
#include "../core/Party.h"
#include "../core/PermutationNetwork.h"
#include "../core/Sort/SortingNetwork.h"
#include "../core/Sort/OptSortingNetwork.h"
#include "../core/Sort/YaoSortingNetwork.h"
#include "../core/Sort/ShuffleQSort.h"
#include "abycore/sharing/sharing.h"
#include "abycore/circuit/booleancircuits.h"
#include "abycore/circuit/arithmeticcircuits.h"
#include "abycore/circuit/circuit.h"
#include "abycore/aby/abyparty.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <netdb.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sstream>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#include <vector>
#include <openssl/sha.h>

using namespace std;
using Clock = chrono::steady_clock;

struct BenchOptions {
    string input_path;
    string algorithm = "opt";
    uint32_t num_keys = 1;
    string data_type = "int32";
};

struct BenchMetrics {
    double time_s = 0.0;
    double comm_MB = 0.0;
    double peak_memory_MB = 0.0;
};

// Output opening requires explicit authorization for a public benchmark input.
// The runner supplies the SHA-256 from the verified TPC-H input sidecar.
static bool permit_public_fixture_verification(const BenchOptions& opts) {
    const char* enabled = getenv("FES_PUBLIC_BENCHMARK");
    if (!enabled || string(enabled) != "1") return false;
    const char* configured_hash = getenv("FES_INPUT_SHA256");
    string expected = configured_hash ? configured_hash : "";
    if (expected.size() != 64 || !all_of(expected.begin(), expected.end(),
            [](unsigned char ch) { return isxdigit(ch) != 0; }))
        throw runtime_error("Public benchmark verification requires FES_INPUT_SHA256");
    transform(expected.begin(), expected.end(), expected.begin(),
              [](unsigned char ch) { return static_cast<char>(tolower(ch)); });
    ifstream input(opts.input_path, ios::binary);
    if (!input) throw runtime_error("Cannot hash public benchmark input");
    SHA256_CTX state;
    SHA256_Init(&state);
    char buffer[65536];
    while (input.read(buffer, sizeof(buffer)) || input.gcount() > 0)
        SHA256_Update(&state, buffer, static_cast<size_t>(input.gcount()));
    if (input.bad()) throw runtime_error("Cannot read public benchmark input");
    unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256_Final(digest, &state);
    const char* hex = "0123456789abcdef";
    string actual;
    for (unsigned char value : digest) {
        actual += hex[value >> 4]; actual += hex[value & 15];
    }
    if (actual != expected)
        throw runtime_error("Public benchmark input does not match FES_INPUT_SHA256");
    return true;
}

static long get_peak_memory_kb() {
    ifstream status("/proc/self/status");
    string line;
    while (getline(status, line)) {
        if (line.find("VmPeak:") == 0) {
            size_t pos = line.find(':');
            string value_str = line.substr(pos + 1);
            size_t kb_pos = value_str.find("kB");
            if (kb_pos != string::npos) {
                value_str.erase(kb_pos);
            }
            return stol(value_str);
        }
    }
    return -1;
}

static bool send_all(int fd, const char* data, size_t len) {
    while (len > 0) {
        ssize_t sent = send(fd, data, len, 0);
        if (sent <= 0) {
            return false;
        }
        data += sent;
        len -= static_cast<size_t>(sent);
    }
    return true;
}

static string recv_until_newline(int fd) {
    string out;
    char ch = '\0';
    while (true) {
        ssize_t n = recv(fd, &ch, 1, 0);
        if (n <= 0) {
            break;
        }
        if (ch == '\n') {
            break;
        }
        out.push_back(ch);
    }
    return out;
}

static BenchMetrics parse_metrics_line(const string& line) {
    BenchMetrics metrics;
    istringstream iss(line);
    if (!(iss >> metrics.time_s >> metrics.comm_MB >> metrics.peak_memory_MB)) {
        throw runtime_error("Failed to parse peer metrics: " + line);
    }
    return metrics;
}

static string format_metrics_line(const BenchMetrics& metrics) {
    ostringstream oss;
    oss << metrics.time_s << " " << metrics.comm_MB << " " << metrics.peak_memory_MB << "\n";
    return oss.str();
}

static BenchMetrics exchange_plain_metrics(e_role role,
                                           const string& server_address,
                                           uint16_t port,
                                           const BenchMetrics& local) {
    int fd = -1;
    BenchMetrics peer;

    if (role == SERVER) {
        int listen_fd = socket(AF_INET, SOCK_STREAM, 0);
        if (listen_fd < 0) {
            throw runtime_error("socket() failed for metrics exchange");
        }
        int yes = 1;
        setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = htonl(INADDR_ANY);
        addr.sin_port = htons(port);
        if (bind(listen_fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) < 0) {
            close(listen_fd);
            throw runtime_error("bind() failed for metrics exchange on port " + to_string(port));
        }
        if (listen(listen_fd, 1) < 0) {
            close(listen_fd);
            throw runtime_error("listen() failed for metrics exchange");
        }
        fd = accept(listen_fd, nullptr, nullptr);
        close(listen_fd);
        if (fd < 0) {
            throw runtime_error("accept() failed for metrics exchange");
        }
        peer = parse_metrics_line(recv_until_newline(fd));
        string local_line = format_metrics_line(local);
        if (!send_all(fd, local_line.c_str(), local_line.size())) {
            close(fd);
            throw runtime_error("send() failed for metrics exchange");
        }
    } else {
        addrinfo hints{};
        hints.ai_family = AF_INET;
        hints.ai_socktype = SOCK_STREAM;
        addrinfo* res = nullptr;
        int rc = getaddrinfo(server_address.c_str(), to_string(port).c_str(), &hints, &res);
        if (rc != 0) {
            throw runtime_error(string("getaddrinfo() failed for metrics exchange: ") + gai_strerror(rc));
        }
        for (int attempt = 0; attempt < 60; ++attempt) {
            fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
            if (fd >= 0 && connect(fd, res->ai_addr, res->ai_addrlen) == 0) {
                break;
            }
            if (fd >= 0) {
                close(fd);
                fd = -1;
            }
            usleep(100000);
        }
        freeaddrinfo(res);
        if (fd < 0) {
            throw runtime_error("connect() failed for metrics exchange");
        }
        string local_line = format_metrics_line(local);
        if (!send_all(fd, local_line.c_str(), local_line.size())) {
            close(fd);
            throw runtime_error("send() failed for metrics exchange");
        }
        peer = parse_metrics_line(recv_until_newline(fd));
    }

    close(fd);

    BenchMetrics aggregate;
    aggregate.time_s = max(local.time_s, peer.time_s);
    aggregate.comm_MB = local.comm_MB + peer.comm_MB;
    aggregate.peak_memory_MB = max(local.peak_memory_MB, peer.peak_memory_MB);
    return aggregate;
}

static string upper_copy(string value) {
    transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(toupper(c));
    });
    return value;
}

static void print_usage(const char* prog) {
    cerr << "Usage: " << prog
         << " <role:0|1> --input <file> --algorithm <sortnet|opt|yao|quick>"
         << " [--num-keys K] [--data-type int32] [--address HOST] [--port PORT]\n";
}

static uint32_t parse_cell_as_uint32(const string& token) {
    if (token.find_first_of(".eE") != string::npos) {
        return static_cast<uint32_t>(stold(token));
    }
    long long value = stoll(token);
    return static_cast<uint32_t>(value);
}

static BenchOptions parse_bench_options(int argc, char** argv, CircSMPContext& context) {
    BenchOptions opts;
    for (int i = 2; i < argc; ++i) {
        string arg = argv[i];
        auto require_value = [&](const string& name) -> string {
            if (i + 1 >= argc) {
                throw invalid_argument("Missing value for " + name);
            }
            return argv[++i];
        };

        if (arg == "--input") {
            opts.input_path = require_value(arg);
        } else if (arg == "--algorithm" || arg == "--sort-protocol") {
            opts.algorithm = require_value(arg);
        } else if (arg == "--num-keys") {
            opts.num_keys = static_cast<uint32_t>(stoul(require_value(arg)));
        } else if (arg == "--data-type") {
            opts.data_type = require_value(arg);
        } else if (arg == "--address") {
            context.address = require_value(arg);
        } else if (arg == "--port") {
            context.port = static_cast<uint16_t>(stoul(require_value(arg)));
        } else {
            throw invalid_argument("Unknown argument: " + arg);
        }
    }

    if (opts.input_path.empty()) {
        throw invalid_argument("--input is required");
    }
    if (opts.num_keys == 0) {
        throw invalid_argument("--num-keys must be at least 1");
    }
    opts.algorithm = upper_copy(opts.algorithm);
    opts.data_type = upper_copy(opts.data_type);
    return opts;
}

static vector<vector<uint32_t>> read_public_table_shape_and_server_data(
    const string& input_path, e_role role, uint32_t& rows, uint32_t& cols) {
    ifstream in(input_path);
    if (!in) {
        throw runtime_error("Cannot open input file: " + input_path);
    }

    vector<vector<uint32_t>> table;
    string line;
    cols = 0;
    while (getline(in, line)) {
        if (line.empty()) {
            continue;
        }
        istringstream iss(line);
        vector<uint32_t> row;
        string token;
        while (iss >> token) {
            row.push_back(parse_cell_as_uint32(token));
        }
        if (row.empty()) {
            continue;
        }
        if (cols == 0) {
            cols = static_cast<uint32_t>(row.size());
        } else if (row.size() != cols) {
            throw runtime_error("Input file has inconsistent column counts: " + input_path);
        }
        table.push_back(role == SERVER ? row : vector<uint32_t>(cols, 0));
    }

    rows = static_cast<uint32_t>(table.size());
    if (rows == 0 || cols == 0) {
        throw runtime_error("Input file is empty: " + input_path);
    }
    return table;
}

static vector<uint64_t> build_sort_keys(const vector<vector<uint32_t>>& vals,
                                        uint32_t num_keys,
                                        e_role role) {
    if (num_keys != 1) {
        throw invalid_argument("MASQUE native sort supports exactly one 64-bit sort key");
    }
    if (vals.empty() || vals[0].empty()) {
        throw invalid_argument("MASQUE input must contain at least one sort-key column");
    }

    vector<uint64_t> keys(vals.size(), 0);
    if (role != SERVER) {
        return keys;
    }

    for (uint32_t i = 0; i < vals.size(); ++i) {
        keys[i] = static_cast<uint64_t>(vals[i][0]);
    }
    return keys;
}

static void print_result(const CircSMPContext& context,
                         const BenchOptions& opts,
                         uint32_t rows,
                         uint32_t cols,
                         const BenchMetrics& aggregate) {
    cout << "---- MASQUE sorting micro benchmark ----\n";
    cout << "input=" << opts.input_path << "\n";
    cout << "algorithm=" << opts.algorithm << ", rows=" << rows
         << ", cols=" << cols << ", num_keys=" << opts.num_keys
         << ", data_type=" << opts.data_type << "\n";
    PrintInfo(context);
    cout << "Aggregated time (max party) = " << aggregate.time_s << " s\n";
    cout << "Aggregated communication (sum parties) = " << aggregate.comm_MB << " MB\n";
    cout << "Peak memory (max party) = " << aggregate.peak_memory_MB << " MB\n";
    cout << "RESULT_CSV," << opts.input_path << "," << rows << "," << cols << ","
         << opts.num_keys << "," << opts.algorithm << "," << opts.data_type
         << "," << aggregate.time_s << "," << aggregate.comm_MB << ","
         << aggregate.peak_memory_MB << "\n";
}

static void run_sort_once(vector<uint64_t> keys,
                          vector<vector<uint32_t>>& vals,
                          CircSMPContext& context,
                          const string& algorithm) {
    if (algorithm == "SORTNET" || algorithm == "BITONIC") {
        gParty.Init(context.address, context.port, context.role);
        auto start_time = Clock::now();
        SortingNetwork(keys, vals, context.role);
        auto end_time = Clock::now();
        gParty.Update(context);
        context.time_cost = chrono::duration<double>(end_time - start_time).count();
    } else if (algorithm == "OPT" || algorithm == "OPTSORTNET" || algorithm == "OPT_BITONIC") {
        gParty.Init(context.address, context.port, context.role);
        vector<vector<uint32_t>> outs;
        auto start_time = Clock::now();
        SortNet sn;
        sn.LoadData(keys, vals);
        sn.ExecSortNet(context);
        sn.OutputData(outs);
        auto end_time = Clock::now();
        context.time_cost = chrono::duration<double>(end_time - start_time).count();
        vals = std::move(outs);
    } else if (algorithm == "YAO" || algorithm == "YAOSORTNET" || algorithm == "YAO_BITONIC") {
        gParty.Init(context.address, context.port, context.role);
        vector<vector<uint32_t>> outs;
        auto start_time = Clock::now();
        YaoSortNet ysn;
        ysn.LoadData(keys, vals);
        ysn.ExecSortNet(context);
        ysn.OutputData(outs);
        auto end_time = Clock::now();
        context.time_cost = chrono::duration<double>(end_time - start_time).count();
        vals = std::move(outs);
    } else if (algorithm == "QUICK" || algorithm == "SHUFFLEQSORT") {
        auto start_time = Clock::now();
        ShuffleQSort(keys, vals, context);
        auto end_time = Clock::now();
        context.time_cost = chrono::duration<double>(end_time - start_time).count();
    } else {
        throw invalid_argument("Unsupported algorithm: " + algorithm);
    }
}

int main(int argc, char** argv) {
    if (argc < 2) {
        print_usage(argv[0]);
        return 1;
    }

    try {
        auto context = read_options(argc, argv);
        context.circ_depth = 0;
        context.circ_gates = 0;
        context.comm_cost = 0;
        context.time_cost = 0;

        BenchOptions opts = parse_bench_options(argc, argv, context);
        const bool verify_public_fixture = permit_public_fixture_verification(opts);

        uint32_t rows = 0;
        uint32_t cols = 0;
        vector<vector<uint32_t>> vals =
            read_public_table_shape_and_server_data(opts.input_path, context.role, rows, cols);
        vector<uint64_t> keys = build_sort_keys(vals, opts.num_keys, context.role);

        run_sort_once(keys, vals, context, opts.algorithm);

        BenchMetrics local_metrics;
        local_metrics.time_s = context.time_cost;
        local_metrics.comm_MB = context.comm_cost;
        local_metrics.peak_memory_MB = static_cast<double>(get_peak_memory_kb()) / 1024.0;
        BenchMetrics aggregate_metrics =
            exchange_plain_metrics(context.role, context.address,
                                   static_cast<uint16_t>(context.port + 10000),
                                   local_metrics);

        if (context.role == SERVER) {
            print_result(context, opts, rows, cols, aggregate_metrics);
        }
        if (verify_public_fixture) {
            // OPT and QUICK return XOR shares. Reconstruct only the hash-guarded
            // public benchmark output, after all measured sorting counters stop.
            if (opts.algorithm != "SORTNET" && opts.algorithm != "BITONIC") {
                gParty.Reset();
                Circuit* bc = gParty.GetCircuit(S_BOOL);
                vector<share*> outputs(rows);
                for (uint32_t i = 0; i < rows; ++i) {
                    outputs[i] = bc->PutOUTGate(
                        bc->PutSharedSIMDINGate(cols, vals[i].data(), 32), ALL);
                }
                gParty.ExecCircuit();
                for (uint32_t i = 0; i < rows; ++i) {
                    uint32_t* decoded = nullptr, bitlen = 0, count = 0;
                    outputs[i]->get_clear_value_vec(&decoded, &bitlen, &count);
                    if (bitlen != 32 || count != cols) throw runtime_error("Bad verification output shape");
                    vals[i].assign(decoded, decoded + cols);
                    free(decoded);
                }
            }
            uint32_t expected_rows = 0, expected_cols = 0;
            auto expected = read_public_table_shape_and_server_data(
                opts.input_path, SERVER, expected_rows, expected_cols);
            // Equal-key rows need not retain their original order. Check key
            // order separately from the complete key/payload tuple multiset.
            if (!is_sorted(vals.begin(), vals.end(),
                    [](const vector<uint32_t>& a, const vector<uint32_t>& b) {
                        return a[0] < b[0];
                    }))
                throw runtime_error("Public benchmark output keys are not sorted");
            auto actual = vals;
            sort(expected.begin(), expected.end());
            sort(actual.begin(), actual.end());
            if (actual != expected)
                throw runtime_error("Full tuples differ from the public TPC-H oracle");
            cout << "SORT_TUPLES_VERIFIED rows=" << rows << " cols=" << cols << "\n";
        }
    } catch (const exception& ex) {
        cerr << "Error: " << ex.what() << "\n";
        print_usage(argv[0]);
        return 1;
    }

    return 0;
}
