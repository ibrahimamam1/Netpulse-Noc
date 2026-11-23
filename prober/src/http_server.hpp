#pragma once
#include <atomic>
#include <string>
#include <thread>

#include "config.hpp"
#include "metrics_store.hpp"

namespace netpulse {

// Minimal HTTP/1.1 GET server: /metrics, /api/v1/telemetry, /health.
// Single accept thread; responses are small and sent with Connection: close.
class HttpServer {
 public:
  HttpServer(const Config& cfg, const MetricsStore& store, const std::atomic<bool>& running);
  ~HttpServer();
  HttpServer(const HttpServer&) = delete;
  HttpServer& operator=(const HttpServer&) = delete;

  bool start(std::string& err);  // binds 0.0.0.0:HTTP_PORT and starts the thread
  void join();

 private:
  void serve();
  void handle_client(int fd);

  const Config& cfg_;
  const MetricsStore& store_;
  const std::atomic<bool>& running_;
  int listen_fd_ = -1;
  std::thread thread_;
};

}  // namespace netpulse