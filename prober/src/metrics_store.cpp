#include "metrics_store.hpp"

#include <algorithm>
#include <cmath>

namespace netpulse {

MetricsStore::MetricsStore(const Config& cfg) : targets_(cfg.targets), series_(cfg.targets.size()) {
  for (auto& s : series_) s.ring.assign(static_cast<size_t>(cfg.window_size), Sample{});
}

void MetricsStore::record_icmp(size_t idx, bool ok, double rtt_ms) {
  std::lock_guard<std::mutex> lk(mu_);
  Series& s = series_[idx];
  s.ring[s.head] = Sample{ok, ok ? rtt_ms : 0.0};
  s.head = (s.head + 1) % s.ring.size();
  if (s.count < s.ring.size()) ++s.count;
  s.has_data = true;
  s.last_ok = ok;
  s.last_rtt = ok ? rtt_ms : 0.0;
  ++s.sent;
  if (ok) {
    s.consecutive_failures = 0;
  } else {
    ++s.failed;
    ++s.consecutive_failures;
  }
}

void MetricsStore::record_tcp(size_t idx, bool ok, double rtt_ms) {
  std::lock_guard<std::mutex> lk(mu_);
  Series& s = series_[idx];
  s.tcp_ok = ok;
  if (ok) s.tcp_rtt = rtt_ms;  // keeps the most recent *successful* handshake latency
}

LinkSnapshot MetricsStore::compute(const LinkTarget& t, const Series& s) {
  LinkSnapshot o;
  o.site = t.site;
  o.link = t.link;
  o.target = t.ip;
  o.window_samples = static_cast<int>(s.count);
  o.probes_sent_total = s.sent;
  o.probes_failed_total = s.failed;
  o.tcp_rtt_ms = s.tcp_rtt;
  o.tcp_ok = s.tcp_ok;
  o.rtt_last_ms = (s.has_data && s.last_ok) ? s.last_rtt : 0.0;
  o.link_up = (s.has_data && s.consecutive_failures < kDownAfterFailures) ? 1 : 0;

  std::vector<double> rtts;  // chronological successful RTTs
  rtts.reserve(s.count);
  size_t lost = 0;
  const size_t n = s.ring.size();
  const size_t start = (s.head + n - s.count) % n;
  for (size_t i = 0; i < s.count; ++i) {
    const Sample& smp = s.ring[(start + i) % n];
    if (smp.ok) rtts.push_back(smp.rtt_ms);
    else ++lost;
  }
  o.packet_loss_pct = s.count ? 100.0 * static_cast<double>(lost) / static_cast<double>(s.count) : 0.0;

  if (!rtts.empty()) {
    double sum = 0;
    for (double v : rtts) sum += v;
    o.rtt_mean_ms = sum / static_cast<double>(rtts.size());

    if (rtts.size() >= 2) {
      double dsum = 0;
      for (size_t i = 1; i < rtts.size(); ++i) dsum += std::fabs(rtts[i] - rtts[i - 1]);
      o.jitter_ms = dsum / static_cast<double>(rtts.size() - 1);
    }

    std::vector<double> sorted = rtts;
    std::sort(sorted.begin(), sorted.end());
    // nearest-rank percentile
    size_t rank = static_cast<size_t>(std::ceil(0.95 * static_cast<double>(sorted.size())));
    o.rtt_p95_ms = sorted[std::max<size_t>(rank, 1) - 1];
  }
  return o;
}

std::vector<LinkSnapshot> MetricsStore::snapshot() const {
  std::lock_guard<std::mutex> lk(mu_);
  std::vector<LinkSnapshot> out;
  out.reserve(series_.size());
  for (size_t i = 0; i < series_.size(); ++i) out.push_back(compute(targets_[i], series_[i]));
  return out;
}

}  // namespace netpulse