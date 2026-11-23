cmake_minimum_required(VERSION 3.16)
project(netpulse_prober LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
set(CMAKE_CXX_FLAGS_RELEASE "-O2")

find_package(Threads REQUIRED)

add_executable(netpulse-prober
  src/main.cpp
  src/config.cpp
  src/icmp_prober.cpp
  src/tcp_prober.cpp
  src/metrics_store.cpp
  src/http_server.cpp
)
target_compile_options(netpulse-prober PRIVATE -Wall -Wextra)
target_link_libraries(netpulse-prober PRIVATE Threads::Threads)

install(TARGETS netpulse-prober RUNTIME DESTINATION bin)