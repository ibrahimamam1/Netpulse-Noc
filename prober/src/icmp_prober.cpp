#include "icmp_prober.hpp"

#include <arpa/inet.h>
#include <netinet/ip_icmp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

#ifndef SOL_RAW
#define SOL_RAW 255
#endif
#ifndef ICMP_FILTER
#define ICMP_FILTER 1
#endif

namespace netpulse {
namespace {

constexpr size_t kIcmpHeader = 8;
constexpr size_t kPayload = 32;
constexpr size_t kPacketLen = kIcmpHeader + kPayload;

// RFC 1071 one's complement checksum over big-endian 16-bit words.
uint16_t rfc1071(const uint8_t* data, size_t len) {
  uint32_t sum = 0;
  while (len > 1) {
    sum += static_cast<uint32_t>((data[0] << 8) | data[1]);
    data += 2;
    len -= 2;
  }
  if (len == 1) sum += static_cast<uint32_t>(data[0] << 8);
  while (sum >> 16) sum = (sum & 0xffff) + (sum >> 16);
  return static_cast<uint16_t>(~sum & 0xffff);
}

}  // namespace

IcmpProber::IcmpProber(const std::string& dst_ip, uint16_t ident) : ident_(ident) {
  dst_.sin_family = AF_INET;
  inet_pton(AF_INET, dst_ip.c_str(), &dst_.sin_addr);  // validated in config
}

IcmpProber::~IcmpProber() {
  if (fd_ >= 0) ::close(fd_);
}

bool IcmpProber::open(std::string& err) {
  fd_ = ::socket(AF_INET, SOCK_RAW | SOCK_NONBLOCK | SOCK_CLOEXEC, IPPROTO_ICMP);
  if (fd_ < 0) {
    err = std::string("raw ICMP socket failed (need CAP_NET_RAW): ") + std::strerror(errno);
    return false;
  }
  // Kernel-side filter: only deliver Echo Replies (bit set = blocked). Best effort.
  uint32_t filter = ~(1u << ICMP_ECHOREPLY);
  ::setsockopt(fd_, SOL_RAW, ICMP_FILTER, &filter, sizeof filter);
  return true;
}

bool IcmpProber::send_request() {
  uint8_t pkt[kPacketLen];
  ++seq_;
  pkt[0] = ICMP_ECHO;
  pkt[1] = 0;
  pkt[2] = pkt[3] = 0;
  pkt[4] = static_cast<uint8_t>(ident_ >> 8);
  pkt[5] = static_cast<uint8_t>(ident_ & 0xff);
  pkt[6] = static_cast<uint8_t>(seq_ >> 8);
  pkt[7] = static_cast<uint8_t>(seq_ & 0xff);
  for (size_t i = 0; i < kPayload; ++i) pkt[kIcmpHeader + i] = static_cast<uint8_t>('a' + (i % 26));
  uint16_t cs = rfc1071(pkt, kPacketLen);
  pkt[2] = static_cast<uint8_t>(cs >> 8);
  pkt[3] = static_cast<uint8_t>(cs & 0xff);

  awaiting_ = true;
  t_start_ = Clock::now();
  ssize_t n = ::sendto(fd_, pkt, kPacketLen, 0, reinterpret_cast<const sockaddr*>(&dst_), sizeof dst_);
  if (n != static_cast<ssize_t>(kPacketLen)) {
    awaiting_ = false;
    return false;
  }
  return true;
}

std::optional<double> IcmpProber::poll_reply() {
  uint8_t buf[1500];
  while (true) {
    sockaddr_in src{};
    socklen_t slen = sizeof src;
    ssize_t n = ::recvfrom(fd_, buf, sizeof buf, 0, reinterpret_cast<sockaddr*>(&src), &slen);
    const auto t_end = Clock::now();
    if (n < 0) {
      if (errno == EINTR) continue;
      return std::nullopt;  // EAGAIN (drained) or a pending socket error (consumed by this read)
    }
    if (n < 20) continue;
    const size_t ihl = static_cast<size_t>(buf[0] & 0x0f) * 4;
    if (ihl < 20 || static_cast<size_t>(n) < ihl + kIcmpHeader) continue;
    if (buf[ihl] != ICMP_ECHOREPLY || buf[ihl + 1] != 0) continue;
    const uint16_t id = static_cast<uint16_t>((buf[ihl + 4] << 8) | buf[ihl + 5]);
    const uint16_t seq = static_cast<uint16_t>((buf[ihl + 6] << 8) | buf[ihl + 7]);
    if (!awaiting_ || id != ident_ || seq != seq_) continue;
    if (src.sin_addr.s_addr != dst_.sin_addr.s_addr) continue;
    awaiting_ = false;
    return std::chrono::duration<double, std::milli>(t_end - t_start_).count();
  }
}

}  // namespace netpulse