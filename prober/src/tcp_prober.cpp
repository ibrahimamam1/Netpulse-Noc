#include "tcp_prober.hpp"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>

namespace netpulse {

TcpProber::TcpProber(const std::string& dst_ip, uint16_t port) {
  dst_.sin_family = AF_INET;
  dst_.sin_port = htons(port);
  inet_pton(AF_INET, dst_ip.c_str(), &dst_.sin_addr);  // validated in config
}

TcpProber::~TcpProber() { cancel(); }

void TcpProber::cancel() {
  if (fd_ >= 0) {
    ::close(fd_);
    fd_ = -1;
  }
}

TcpProber::Status TcpProber::start(double& rtt_ms) {
  cancel();
  fd_ = ::socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
  if (fd_ < 0) return Status::Failed;
  t_start_ = Clock::now();
  int rc = ::connect(fd_, reinterpret_cast<const sockaddr*>(&dst_), sizeof dst_);
  if (rc == 0) {
    rtt_ms = std::chrono::duration<double, std::milli>(Clock::now() - t_start_).count();
    cancel();
    return Status::Connected;
  }
  if (errno == EINPROGRESS) return Status::Pending;
  cancel();
  return Status::Failed;
}

TcpProber::Status TcpProber::check(double& rtt_ms) {
  const auto t_end = Clock::now();
  int err = 0;
  socklen_t len = sizeof err;
  if (::getsockopt(fd_, SOL_SOCKET, SO_ERROR, &err, &len) < 0) err = errno;
  cancel();
  if (err != 0) return Status::Failed;
  rtt_ms = std::chrono::duration<double, std::milli>(t_end - t_start_).count();
  return Status::Connected;
}

}  // namespace netpulse