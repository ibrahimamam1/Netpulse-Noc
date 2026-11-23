#include <poll.h>
#include <unistd.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <exception>
#include <string>
#include <thread>
#include <vector>

#include "config.hpp"
#include "http_server.hpp"
#include "icmp_prober.hpp"
#include "metrics_store.hpp"
#include "tcp_prober.hpp"

namespace {

std::atomic<bool> running{true};

void on_signal(int) { running.store(false); }

void sleep_until_or_stop(netpulse::Clock::time_point t) {
  using namespace std::chrono_literals;
  while (running.load()) {
    auto now = netpulse::Clock::now();
    if (now >= t) return;
    std::this_thread::sleep_for(std::min<netpulse::Clock::duration>(t - now, 50ms));
  }
}

void probe_worker(size_t idx, const netpulse::Config& cfg, netpulse::MetricsStore& store) {
  using namespace netpulse;
  const LinkTarget& t = cfg.targets[idx];
  const std::string name = t.site + "/" + t.link;

  IcmpProber icmp(t.ip, static_cast<uint16_t>((static_cast<unsigned>(::getpid()) ^ static_cast<unsigned>(idx + 1)) & 0xffff));
  TcpProber tcp(t.ip, t.tcp_port);
  std::string err;
  const bool icmp_open = icmp.open(err);
  if (!icmp_open) log_info("ERROR " + name + ": " + err + " (all ICMP probes will count as lost)");

  const auto interval = std::chrono::milliseconds(cfg.probe_interval_ms);
  const auto timeout = std::chrono::milliseconds(cfg.probe_timeout_ms);
  auto next = Clock::now();
  uint64_t cycle = 0;

  while (running.load()) {
    next += interval;  // fixed schedule: no drift from probe duration
    const auto deadline = Clock::now() + timeout;

    bool icmp_pending = icmp_open && icmp.send_request();
    bool icmp_ok = false;
    double icmp_rtt = 0;

    const bool do_tcp = (cycle++ % static_cast<uint64_t>(cfg.tcp_probe_every)) == 0;
    bool tcp_pending = false, tcp_ok = false;
    double tcp_rtt = 0;
    if (do_tcp) {
      auto st = tcp.start(tcp_rtt);
      tcp_pending = (st == TcpProber::Status::Pending);
      tcp_ok = (st == TcpProber::Status::Connected);
    }

    // One poll() covers both sockets, so a dead link costs one timeout, not two.
    while ((icmp_pending || tcp_pending) && running.load()) {
      auto left = std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now());
      if (left.count() <= 0) break;
      pollfd fds[2];
      nfds_t n = 0;
      int icmp_slot = -1, tcp_slot = -1;
      if (icmp_pending) {
        icmp_slot = static_cast<int>(n);
        fds[n++] = {icmp.fd(), POLLIN, 0};
      }
      if (tcp_pending) {
        tcp_slot = static_cast<int>(n);
        fds[n++] = {tcp.fd(), POLLOUT, 0};
      }
      int rc = ::poll(fds, n, static_cast<int>(left.count()));
      if (rc < 0) {
        if (errno == EINTR) continue;
        break;
      }
      if (rc == 0) break;  // timeout
      if (icmp_slot >= 0 && fds[icmp_slot].revents) {
        if (auto r = icmp.poll_reply()) {
          icmp_ok = true;
          icmp_rtt = *r;
          icmp_pending = false;
        }
      }
      if (tcp_slot >= 0 && fds[tcp_slot].revents) {
        tcp_ok = (tcp.check(tcp_rtt) == TcpProber::Status::Connected);
        tcp_pending = false;
      }
    }
    tcp.cancel();  // no-op if already closed
    if (!running.load()) break;  // don't record a cycle cut short by shutdown

    store.record_icmp(idx, icmp_ok, icmp_rtt);
    if (do_tcp) store.record_tcp(idx, tcp_ok, tcp_rtt);

    sleep_until_or_stop(next);
  }
}

}  // namespace

int main() {
  using namespace netpulse;

  netpulse::Config cfg;
  try {
    cfg = load_config();
  } catch (const std::exception& e) {
    log_info(std::string("FATAL config error: ") + e.what());
    return 2;
  }

  struct sigaction sa{};
  sa.sa_handler = on_signal;  // no SA_RESTART: blocking calls return EINTR promptly
  sigemptyset(&sa.sa_mask);
  sigaction(SIGINT, &sa, nullptr);
  sigaction(SIGTERM, &sa, nullptr);
  signal(SIGPIPE, SIG_IGN);

  MetricsStore store(cfg);
  HttpServer http(cfg, store, running);
  std::string err;
  if (!http.start(err)) {
    log_info("FATAL http server: " + err);
    return 1;
  }

  log_info("netpulse-prober starting: " + std::to_string(cfg.targets.size()) + " links, interval " +
           std::to_string(cfg.probe_interval_ms) + "ms, timeout " + std::to_string(cfg.probe_timeout_ms) +
           "ms, window " + std::to_string(cfg.window_size) + ", http :" + std::to_string(cfg.http_port));

  std::vector<std::thread> workers;
  workers.reserve(cfg.targets.size());
  for (size_t i = 0; i < cfg.targets.size(); ++i) workers.emplace_back(probe_worker, i, std::cref(cfg), std::ref(store));

  while (running.load()) std::this_thread::sleep_for(std::chrono::milliseconds(100));

  log_info("shutdown signal received, stopping");
  for (auto& w : workers) w.join();
  http.join();
  log_info("stopped cleanly");
  return 0;
}