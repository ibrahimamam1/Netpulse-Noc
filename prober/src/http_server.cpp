#include "http_server.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string_view>

namespace netpulse {
namespace {

std::string num(double v) {
  if (!std::isfinite(v)) v = 0;
  char b[48];
  std::snprintf(b, sizeof b, "%.3f", v);
  return b;
}

std::string integer(double v) {
  char b[48];
  std::snprintf(b, sizeof b, "%.0f", v);
  return b;
}

std::string json_escape(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') o += '\\';
    o += c;
  }
  return o;
}

struct MetricDef {
  const char* name;
  const char* help;
  const char* type;
  bool is_integer;
  double (*get)(const LinkSnapshot&);
};

const MetricDef kMetrics[] = {
    {"netpulse_link_rtt_ms", "Mean ICMP RTT over the sliding window (ms).", "gauge", false,
     [](const LinkSnapshot& s) { return s.rtt_mean_ms; }},
    {"netpulse_link_rtt_p95_ms", "95th percentile ICMP RTT over the sliding window (ms).", "gauge", false,
     [](const LinkSnapshot& s) { return s.rtt_p95_ms; }},
    {"netpulse_link_jitter_ms", "Mean absolute difference of consecutive successful ICMP RTTs (ms).", "gauge",
     false, [](const LinkSnapshot& s) { return s.jitter_ms; }},
    {"netpulse_link_packet_loss_pct", "ICMP packet loss over the sliding window (percent).", "gauge", false,
     [](const LinkSnapshot& s) { return s.packet_loss_pct; }},
    {"netpulse_link_tcp_rtt_ms", "Most recent successful TCP handshake latency (ms).", "gauge", false,
     [](const LinkSnapshot& s) { return s.tcp_rtt_ms; }},
    {"netpulse_link_up", "1 if the link is up, 0 after 3 consecutive failed probes.", "gauge", true,
     [](const LinkSnapshot& s) { return static_cast<double>(s.link_up); }},
    {"netpulse_probes_sent_total", "Total ICMP probes sent since startup.", "counter", true,
     [](const LinkSnapshot& s) { return static_cast<double>(s.probes_sent_total); }},
    {"netpulse_probes_failed_total", "Total ICMP probes lost since startup.", "counter", true,
     [](const LinkSnapshot& s) { return static_cast<double>(s.probes_failed_total); }},
};

std::string render_prometheus(const std::vector<LinkSnapshot>& snaps) {
  std::string out;
  out.reserve(4096);
  for (const auto& m : kMetrics) {
    out += "# HELP ";
    out += m.name;
    out += ' ';
    out += m.help;
    out += "\n# TYPE ";
    out += m.name;
    out += ' ';
    out += m.type;
    out += '\n';
    for (const auto& s : snaps) {
      out += m.name;
      out += "{site=\"" + json_escape(s.site) + "\",link=\"" + json_escape(s.link) + "\"} ";
      out += m.is_integer ? integer(m.get(s)) : num(m.get(s));
      out += '\n';
    }
  }
  return out;
}

std::string render_telemetry(const Config& cfg, const std::vector<LinkSnapshot>& snaps) {
  const auto ts = std::chrono::duration_cast<std::chrono::seconds>(
                      std::chrono::system_clock::now().time_since_epoch())
                      .count();
  std::string o = "{\"timestamp\":" + std::to_string(ts) +
                  ",\"window_size\":" + std::to_string(cfg.window_size) +
                  ",\"probe_interval_ms\":" + std::to_string(cfg.probe_interval_ms) + ",\"links\":[";
  bool first = true;
  for (const auto& s : snaps) {
    if (!first) o += ',';
    first = false;
    o += "{\"site\":\"" + json_escape(s.site) + "\",\"link\":\"" + json_escape(s.link) +
         "\",\"target\":\"" + json_escape(s.target) + "\"" +
         ",\"link_up\":" + std::to_string(s.link_up) +
         ",\"rtt_last_ms\":" + num(s.rtt_last_ms) + ",\"rtt_mean_ms\":" + num(s.rtt_mean_ms) +
         ",\"rtt_p95_ms\":" + num(s.rtt_p95_ms) + ",\"jitter_ms\":" + num(s.jitter_ms) +
         ",\"packet_loss_pct\":" + num(s.packet_loss_pct) + ",\"tcp_rtt_ms\":" + num(s.tcp_rtt_ms) +
         ",\"tcp_ok\":" + (s.tcp_ok ? "true" : "false") +
         ",\"probes_sent_total\":" + std::to_string(s.probes_sent_total) +
         ",\"probes_failed_total\":" + std::to_string(s.probes_failed_total) +
         ",\"window_samples\":" + std::to_string(s.window_samples) + "}";
  }
  o += "]}";
  return o;
}

std::string response(int code, const char* reason, const char* ctype, const std::string& body) {
  std::string r = "HTTP/1.1 " + std::to_string(code) + " " + reason + "\r\nContent-Type: " + ctype +
                  "\r\nContent-Length: " + std::to_string(body.size()) + "\r\nConnection: close\r\n\r\n";
  r += body;
  return r;
}

void send_all(int fd, const std::string& data) {
  size_t off = 0;
  while (off < data.size()) {
    ssize_t n = ::send(fd, data.data() + off, data.size() - off, MSG_NOSIGNAL);
    if (n < 0) {
      if (errno == EINTR) continue;
      return;
    }
    off += static_cast<size_t>(n);
  }
}

}  // namespace

HttpServer::HttpServer(const Config& cfg, const MetricsStore& store, const std::atomic<bool>& running)
    : cfg_(cfg), store_(store), running_(running) {}

HttpServer::~HttpServer() {
  join();
  if (listen_fd_ >= 0) ::close(listen_fd_);
}

bool HttpServer::start(std::string& err) {
  listen_fd_ = ::socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, 0);
  if (listen_fd_ < 0) {
    err = std::string("socket: ") + std::strerror(errno);
    return false;
  }
  int one = 1;
  ::setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
  sockaddr_in a{};
  a.sin_family = AF_INET;
  a.sin_addr.s_addr = htonl(INADDR_ANY);
  a.sin_port = htons(static_cast<uint16_t>(cfg_.http_port));
  if (::bind(listen_fd_, reinterpret_cast<sockaddr*>(&a), sizeof a) < 0 || ::listen(listen_fd_, 16) < 0) {
    err = "bind/listen on port " + std::to_string(cfg_.http_port) + ": " + std::strerror(errno);
    return false;
  }
  thread_ = std::thread([this] { serve(); });
  return true;
}

void HttpServer::join() {
  if (thread_.joinable()) thread_.join();
}

void HttpServer::serve() {
  while (running_.load()) {
    pollfd p{listen_fd_, POLLIN, 0};
    int rc = ::poll(&p, 1, 200);  // wake regularly to notice shutdown
    if (rc <= 0) continue;
    int c = ::accept4(listen_fd_, nullptr, nullptr, SOCK_CLOEXEC);
    if (c < 0) continue;
    handle_client(c);
    ::close(c);
  }
}

void HttpServer::handle_client(int fd) {
  char buf[4096];
  size_t used = 0;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
  while (used < sizeof buf - 1) {
    auto left = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now());
    if (left.count() <= 0) break;
    pollfd p{fd, POLLIN, 0};
    if (::poll(&p, 1, static_cast<int>(left.count())) <= 0) break;
    ssize_t n = ::recv(fd, buf + used, sizeof buf - 1 - used, 0);
    if (n <= 0) break;
    used += static_cast<size_t>(n);
    buf[used] = '\0';
    if (std::strstr(buf, "\r\n\r\n") || std::strstr(buf, "\n\n")) break;
  }
  std::string_view req(buf, used);
  size_t eol = req.find('\n');
  std::string_view line = req.substr(0, eol);
  size_t sp1 = line.find(' ');
  size_t sp2 = sp1 == std::string_view::npos ? sp1 : line.find(' ', sp1 + 1);
  if (sp1 == std::string_view::npos || sp2 == std::string_view::npos) {
    send_all(fd, response(400, "Bad Request", "text/plain", "Bad Request\n"));
    return;
  }
  std::string_view method = line.substr(0, sp1);
  std::string_view path = line.substr(sp1 + 1, sp2 - sp1 - 1);
  if (size_t q = path.find('?'); q != std::string_view::npos) path = path.substr(0, q);

  if (method != "GET") {
    send_all(fd, response(405, "Method Not Allowed", "text/plain", "Method Not Allowed\n"));
  } else if (path == "/health") {
    send_all(fd, response(200, "OK", "text/plain", "OK"));
  } else if (path == "/metrics") {
    send_all(fd, response(200, "OK", "text/plain; version=0.0.4", render_prometheus(store_.snapshot())));
  } else if (path == "/api/v1/telemetry") {
    send_all(fd, response(200, "OK", "application/json", render_telemetry(cfg_, store_.snapshot())));
  } else {
    send_all(fd, response(404, "Not Found", "text/plain", "Not Found\n"));
  }
}

}  // namespace netpulse