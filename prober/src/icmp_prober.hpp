#pragma once
#include <netinet/in.h>

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>

namespace netpulse {

using Clock = std::chrono::steady_clock;

// Raw ICMP echo prober for one destination. One instance per worker thread.
// Split into send_request() / poll_reply() so the worker can wait on ICMP and TCP
// sockets with a single poll() (a dead link then costs one timeout, not two).
class IcmpProber {
 public:
  IcmpProber(const std::string& dst_ip, uint16_t ident);
  ~IcmpProber();
  IcmpProber(const IcmpProber&) = delete;
  IcmpProber& operator=(const IcmpProber&) = delete;

  bool open(std::string& err);  // needs CAP_NET_RAW
  int fd() const { return fd_; }

  // Builds + sends an Echo Request with the next sequence number. t_start is taken
  // immediately before sendto(). Returns false if the send failed.
  bool send_request();

  // Drains the (non-blocking) socket; returns RTT in ms when the reply matching our
  // identifier, current sequence number and destination arrives. Unrelated ICMP is ignored.
  std::optional<double> poll_reply();

 private:
  int fd_ = -1;
  sockaddr_in dst_{};
  uint16_t ident_;
  uint16_t seq_ = 0;
  bool awaiting_ = false;
  Clock::time_point t_start_{};
};

}  // namespace netpulse