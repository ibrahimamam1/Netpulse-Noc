#pragma once
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include "config.hpp"

namespace netpulse {

struct LinkSnapshot {
  std::string site;
  std::string link;
  std::string target;
  double rtt_last_ms = 0;
  double rtt_mean_ms = 0;
  double rtt_p95_ms = 0;
  double jitter_ms = 0;
  double packet_loss_pct = 0;
  double tcp_rtt_ms = 0;
  int link_up = 0;
  bool tcp_ok = false;
  uint64_t probes_sent_total = 0;
  uint64_t probes_failed_total = 0;
  int window_samples = 0;
};

// Thread-safe per-link sliding windows. Writers: one probe thread per link.
// Readers: HTTP thread (snapshot computes stats on demand from the ring buffers).
class MetricsStore {
 public:
  explicit MetricsStore(const Config& cfg);

  void record_icmp(size_t idx, bool ok, double rtt_ms);
  void record_tcp(size_t idx, bool ok, double rtt_ms);
  std::vector<LinkSnapshot> snapshot() const;

 private:
  struct Sample {
    bool ok = false;
    double rtt_ms = 0;
  };
  struct Series {
    std::vector<Sample> ring;  // fixed size = WINDOW_SIZE, allocated once
    size_t head = 0;           // next write index
    size_t count = 0;
    bool has_data = false;
    bool last_ok = false;
    double last_rtt = 0;
    int consecutive_failures = 0;
    uint64_t sent = 0;
    uint64_t failed = 0;
    double tcp_rtt = 0;
    bool tcp_ok = false;
  };
  static constexpr int kDownAfterFailures = 3;

  static LinkSnapshot compute(const LinkTarget& t, const Series& s);

  std::vector<LinkTarget> targets_;
  std::vector<Series> series_;
  mutable std::mutex mu_;
};

}  // namespace netpulse