# Top-level ESP-IDF project file for mockingbird.
cmake_minimum_required(VERSION 3.16)

# Pull MicroLink (Tailscale client) components in from the submodule tree.
# The submodule lives at external/microlink and ships its own component(s)
# under external/microlink/components/*.
set(EXTRA_COMPONENT_DIRS "external/microlink/components")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(mockingbird)
