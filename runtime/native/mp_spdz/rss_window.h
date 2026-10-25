#pragma once
#include <atomic>
#include <chrono>
#include <fstream>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <thread>

class FESNativeWindowRSS {
    std::atomic<bool> running{true};
    std::atomic<long> peak{0};
    std::thread sampler;
    static long read_rss() {
        std::ifstream f("/proc/self/status");std::string line;
        while(std::getline(f,line))if(line.rfind("VmRSS:",0)==0) {
            std::istringstream s(line.substr(6));long value;s>>value;return value;
        }
        throw std::runtime_error("Sorting-window VmRSS unavailable");
    }
    void sample() {
        long value=read_rss(),old=peak.load();
        while(value>old && !peak.compare_exchange_weak(old,value)){}
    }
public:
    FESNativeWindowRSS():peak(read_rss()),sampler([this] {
        while(running.load()){sample();std::this_thread::sleep_for(std::chrono::milliseconds(5));}
    }){}
    long stop(){sample();running=false;sampler.join();return peak.load();}
    ~FESNativeWindowRSS(){if(sampler.joinable()){running=false;sampler.join();}}
};

static std::unique_ptr<FESNativeWindowRSS> fes_sort_window_rss;
