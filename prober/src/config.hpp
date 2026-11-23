#pragma once
#include <cstdint>
#include <string>
#include <vector>

namespace netpulse {

struct LinkTarget {
  std::string site;   // douala | yaounde | edge
  std::string link;   // primary | backup
  std::string ip;
  uint16_t tcp_port = 8080;
};

struct Config {
  int probe_interval_ms = 500;
  int probe_timeout_ms = 450;
  int window_size = 20;
  int http_port = 9100;
  int tcp_probe_every = 1;          // run TCP probe every K-th cycle
  std::vector<LinkTarget> targets;  // one entry per (site, link)
};

// Defaults -> optional CONFIG_FILE (KEY=VALUE lines, TARGET=site,primary_ip,backup_ip,tcp_port)
// -> environment variables. Throws std::runtime_error on invalid configuration.
Config load_config();

// Timestamped line on stdout (thread-safe enough for line-sized writes).
void log_info(const std::string& msg);

}  // namespace netpulse