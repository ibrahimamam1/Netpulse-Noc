#pragma once
#include <netinet/in.h>

#include <chrono>
#include <cstdint>
#include <string>

namespace netpulse {

// Non-blocking TCP handshake latency prober (one in-flight connect at a time).
class TcpProber {
 public:
  enum class Status { Pending, Connected, Failed };
  using Clock = std::chrono::steady_clock;

  TcpProber(const std::string& dst_ip, uint16_t port);
  ~TcpProber();
  TcpProber(const TcpProber&) = delete;
  TcpProber& operator=(const TcpProber&) = delete;

  // Starts connect(). Pending: wait for POLLOUT on fd(), then call check().
  // Connected (immediate) sets rtt_ms. Failed: socket already closed.
  Status start(double& rtt_ms);
  int fd() const { return fd_; }

  // Call after poll() reports an event on fd(): verifies SO_ERROR, computes RTT, closes socket.
  Status check(double& rtt_ms);

  // Abandon an in-flight attempt (timeout/shutdown).
  void cancel();

 private:
  int fd_ = -1;
  sockaddr_in dst_{};
  Clock::time_point t_start_{};
};

}  // namespace netpulse