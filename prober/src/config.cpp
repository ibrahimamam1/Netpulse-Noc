#include "config.hpp"

#include <arpa/inet.h>

#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <stdexcept>

namespace netpulse {
namespace {

int parse_int(const std::string& v, const std::string& name) {
  char* end = nullptr;
  long n = std::strtol(v.c_str(), &end, 10);
  if (v.empty() || end == nullptr || *end != '\0') {
    throw std::runtime_error("invalid integer for " + name + ": '" + v + "'");
  }
  return static_cast<int>(n);
}

std::string trim(const std::string& s) {
  const char* ws = " \t\r\n";
  size_t b = s.find_first_not_of(ws);
  if (b == std::string::npos) return "";
  size_t e = s.find_last_not_of(ws);
  return s.substr(b, e - b + 1);
}

bool set_param(Config& c, const std::string& key, const std::string& val) {
  if (key == "PROBE_INTERVAL_MS") c.probe_interval_ms = parse_int(val, key);
  else if (key == "PROBE_TIMEOUT_MS") c.probe_timeout_ms = parse_int(val, key);
  else if (key == "WINDOW_SIZE") c.window_size = parse_int(val, key);
  else if (key == "HTTP_PORT") c.http_port = parse_int(val, key);
  else if (key == "TCP_PROBE_EVERY") c.tcp_probe_every = parse_int(val, key);
  else return false;
  return true;
}

void add_site(std::vector<LinkTarget>& out, const std::string& site, const std::string& pri,
              const std::string& bak, int port) {
  out.push_back({site, "primary", pri, static_cast<uint16_t>(port)});
  out.push_back({site, "backup", bak, static_cast<uint16_t>(port)});
}

void parse_target_line(std::vector<LinkTarget>& out, const std::string& val) {
  std::vector<std::string> f;
  size_t pos = 0;
  while (true) {
    size_t c = val.find(',', pos);
    f.push_back(trim(val.substr(pos, c == std::string::npos ? std::string::npos : c - pos)));
    if (c == std::string::npos) break;
    pos = c + 1;
  }
  if (f.size() != 4) throw std::runtime_error("TARGET must be site,primary_ip,backup_ip,tcp_port: " + val);
  add_site(out, f[0], f[1], f[2], parse_int(f[3], "TARGET tcp_port"));
}

void validate(const Config& c) {
  if (c.probe_interval_ms <= 0) throw std::runtime_error("PROBE_INTERVAL_MS must be > 0");
  if (c.probe_timeout_ms <= 0) throw std::runtime_error("PROBE_TIMEOUT_MS must be > 0");
  if (c.probe_timeout_ms >= c.probe_interval_ms)
    throw std::runtime_error("PROBE_TIMEOUT_MS must be < PROBE_INTERVAL_MS");
  if (c.window_size < 1 || c.window_size > 100000) throw std::runtime_error("WINDOW_SIZE out of range");
  if (c.http_port < 1 || c.http_port > 65535) throw std::runtime_error("HTTP_PORT out of range");
  if (c.tcp_probe_every < 1) throw std::runtime_error("TCP_PROBE_EVERY must be >= 1");
  if (c.targets.empty()) throw std::runtime_error("no probe targets configured");
  for (const auto& t : c.targets) {
    in_addr a{};
    if (inet_pton(AF_INET, t.ip.c_str(), &a) != 1)
      throw std::runtime_error("invalid IPv4 address for " + t.site + "/" + t.link + ": " + t.ip);
  }
}

}  // namespace

Config load_config() {
  Config c;
  std::vector<LinkTarget> file_targets;

  if (const char* path = std::getenv("CONFIG_FILE"); path && *path) {
    std::ifstream in(path);
    if (!in) throw std::runtime_error(std::string("cannot open CONFIG_FILE: ") + path);
    std::string line;
    while (std::getline(in, line)) {
      line = trim(line);
      if (line.empty() || line[0] == '#') continue;
      size_t eq = line.find('=');
      if (eq == std::string::npos) throw std::runtime_error("bad config line: " + line);
      std::string key = trim(line.substr(0, eq)), val = trim(line.substr(eq + 1));
      if (key == "TARGET") parse_target_line(file_targets, val);
      else if (!set_param(c, key, val)) throw std::runtime_error("unknown config key: " + key);
    }
  }

  for (const char* name : {"PROBE_INTERVAL_MS", "PROBE_TIMEOUT_MS", "WINDOW_SIZE", "HTTP_PORT",
                           "TCP_PROBE_EVERY"}) {
    if (const char* v = std::getenv(name); v && *v) set_param(c, name, v);
  }

  if (!file_targets.empty()) {
    c.targets = std::move(file_targets);
  } else {
    // Default matrix: matches Layer 1 static IP plan.
    add_site(c.targets, "douala", "172.20.1.11", "172.20.2.11", 8080);
    add_site(c.targets, "yaounde", "172.20.1.12", "172.20.2.12", 8080);
    add_site(c.targets, "edge", "172.20.1.13", "172.20.2.13", 8080);
  }
  validate(c);
  return c;
}

void log_info(const std::string& msg) {
  std::time_t now = std::time(nullptr);
  std::tm tm{};
  gmtime_r(&now, &tm);
  char ts[32];
  std::strftime(ts, sizeof ts, "%Y-%m-%d %H:%M:%S", &tm);
  std::fprintf(stdout, "[%sZ] %s\n", ts, msg.c_str());
  std::fflush(stdout);
}

}  // namespace netpulse