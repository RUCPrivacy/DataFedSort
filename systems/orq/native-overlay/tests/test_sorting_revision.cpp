// Native ORQ table sorting, checked completion, and full-tuple verification.
// Input sharing and output reconstruction are outside the measured window.
#include "orq.h"
#include <atomic>
#include <iomanip>
#include <sstream>
#include <thread>

#if defined(USE_DUMMY_TRIPLES) || defined(USE_ZERO_TRIPLES) || defined(MAL_TEST_MODE) || defined(DEBUG_SKIP_EXPENSIVE_TABLE_OPERATIONS)
#error "Benchmark measurements require real protocol execution and checks"
#endif

using namespace orq::service;
using namespace COMPILED_MPC_PROTOCOL_NAMESPACE;

static long rss_kib() {
    std::ifstream f("/proc/self/status");
    std::string line;
    while (std::getline(f,line)) {
        if (line.rfind("VmRSS:",0)==0) {
            std::istringstream s(line.substr(6));long v;s>>v;return v;
        }
    }
    throw std::runtime_error("VmRSS unavailable");
}

class WindowRSS {
    std::atomic<bool> running{true};
    std::atomic<long> peak{0};
    std::thread sampler;
public:
    WindowRSS():peak(rss_kib()),sampler([this] {
        while(running.load()) {
            long v=rss_kib(),old=peak.load();
            while(v>old && !peak.compare_exchange_weak(old,v)){}
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }){}
    long stop() {
        long final=rss_kib(),old=peak.load();
        while(final>old && !peak.compare_exchange_weak(old,final)){}
        running=false;sampler.join();return peak.load();
    }
    ~WindowRSS(){if(sampler.joinable()){running=false;sampler.join();}}
};

static size_t sent_bytes() {
    size_t total=0;
    for(auto& worker:runTime->workers)total+=worker.getCommunicator()->getBytesSent();
    return total;
}

int main(int argc,char** argv) {
    orq_init(argc,argv);
    try {
        const char* file=std::getenv("FES_CASE_INPUT");
        const char* alg=std::getenv("FES_CASE_ALGORITHM");
        if(!file||!alg)throw std::runtime_error("Case input and algorithm are required");
        std::string algorithm(alg);
        auto protocol=orq::SortingProtocol::NETWORK;
        if(algorithm=="radix")protocol=orq::SortingProtocol::RADIXSORT;
        else if(algorithm=="quick")protocol=orq::SortingProtocol::QUICKSORT;
        else if(algorithm!="bitonic")throw std::runtime_error("Unsupported algorithm");
        std::ifstream f(file);
        if(!f)throw std::runtime_error("Input file missing");
        std::vector<std::pair<int32_t,int32_t>> expected;
        std::string line;
        while(std::getline(f,line)) {
            std::istringstream row(line);int64_t key,payload;std::string extra;
            if(!(row>>key>>payload)||row>>extra||key<0||payload<0||key>INT32_MAX||payload>INT32_MAX)
                throw std::runtime_error("Invalid two-column int32 input");
            expected.emplace_back(key,payload);
        }
        const auto n=expected.size();
        if(n!=256 && n!=65536)throw std::runtime_error("This source profile supports 256 or 65536 input rows");
        orq::Vector<int32_t> key(n),payload(n);
        for(size_t i=0;i<n;i++){key[i]=expected[i].first;payload[i]=expected[i].second;}
        EncodedTable<int32_t> table=secret_share<int32_t>({key,payload},{"[key]","[payload]"});
        // Drain input-phase checks before the measured native sorting call.
        if(!runTime->malicious_check())throw std::runtime_error("Input checks failed");
        MPI_Barrier(MPI_COMM_WORLD);
        WindowRSS rss;
        const size_t before=sent_bytes();
        const auto start=std::chrono::steady_clock::now();
        table.sort({{"[key]",SortOrder::ASC}},protocol);
        // Include any native checks deferred by the sorting operator.
        if(!runTime->malicious_check())throw std::runtime_error("Sort checks failed");
        const auto end=std::chrono::steady_clock::now();
        const size_t bytes=sent_bytes()-before;
        const long peak=rss.stop();
        const double seconds=std::chrono::duration<double>(end-start).count();
        auto opened=table.open();
        int ok=opened.size()==2;
        if(ok)ok=opened[0].size()==n && opened[1].size()==n;
        std::vector<std::pair<int32_t,int32_t>> actual;
        if(ok) {
            actual.reserve(n);
            for(size_t i=0;i<n;i++) {
                if(i>0 && opened[0][i]<opened[0][i-1])ok=0;
                actual.emplace_back(opened[0][i],opened[1][i]);
            }
            std::sort(expected.begin(),expected.end());
            std::sort(actual.begin(),actual.end());
            ok=ok && actual==expected;
        }
        int all_ok=0;
        MPI_Allreduce(&ok,&all_ok,1,MPI_INT,MPI_MIN,MPI_COMM_WORLD);
        if(!all_ok)throw std::runtime_error("Full-tuple correctness failed");
        double max_time=0;long max_rss=0;unsigned long long local_bytes=bytes,total_bytes=0;
        MPI_Reduce(&seconds,&max_time,1,MPI_DOUBLE,MPI_MAX,0,MPI_COMM_WORLD);
        MPI_Reduce(&peak,&max_rss,1,MPI_LONG,MPI_MAX,0,MPI_COMM_WORLD);
        MPI_Reduce(&local_bytes,&total_bytes,1,MPI_UNSIGNED_LONG_LONG,MPI_SUM,0,MPI_COMM_WORLD);
        std::ostringstream party;
        party<<std::setprecision(12)<<"PARTY_REVISION,{\"rank\":"<<runTime->getPartyID()
             <<",\"time_s\":"<<seconds<<",\"sent_bytes\":"<<bytes<<",\"rss_kib\":"<<peak<<"}";
        std::cout<<party.str()<<std::endl;
        if(runTime->getPartyID()==0) {
            int parties=0;MPI_Comm_size(MPI_COMM_WORLD,&parties);
            std::ostringstream result;
            result<<std::setprecision(12)<<"RESULT_REVISION,{\"system\":\"orq\",\"algorithm\":\""<<algorithm
                  <<"\",\"rows\":"<<n<<",\"parties\":"<<parties<<",\"protocol\":"<<PROTOCOL_NUM
                  <<",\"security\":\""<<(MALICIOUS_PROTOCOL?"MAL":"SH")<<"\",\"time_s\":"<<max_time
                  <<",\"communication_mib\":"<<double(total_bytes)/(1024*1024)
                  <<",\"peak_rss_mib\":"<<double(max_rss)/1024
                  <<",\"full_tuple_correct\":true,\"real_materials\":true,\"sort_checks_completed\":true}";
            std::cout<<result.str()<<std::endl;
        }
        return 0;
    }catch(const std::exception& e) {
        std::cerr<<"Native ORQ sort failed: "<<e.what()<<std::endl;
        MPI_Abort(MPI_COMM_WORLD,2);return 2;
    }
}
