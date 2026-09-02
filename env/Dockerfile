# syntax=docker/dockerfile:1.7
ARG UBUNTU_VERSION=24.04
FROM ubuntu:${UBUNTU_VERSION} AS common
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git jq xz-utils unzip file \
    build-essential make cmake ninja-build pkg-config \
    python3 python3-venv python3-pip \
    gcc g++ clang clang-tidy clang-format cppcheck \
    ccache lcov gdb-multiarch \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/common.txt /tmp/requirements-common.txt
RUN python3 -m venv /opt/embedded-venv \
    && /opt/embedded-venv/bin/pip install --no-cache-dir --upgrade pip wheel \
    && /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-common.txt
ENV PATH="/opt/embedded-venv/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CCACHE_DIR=/workspaces/.cache/ccache \
    XDG_CACHE_HOME=/workspaces/.cache/xdg
WORKDIR /workspace

FROM common AS firmware
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-arm-none-eabi libnewlib-arm-none-eabi \
    openocd qemu-system-arm qemu-system-misc device-tree-compiler \
    ruby-full \
    gcc-avr avr-libc avrdude simavr \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/firmware.txt /tmp/requirements-firmware.txt
RUN /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-firmware.txt \
    && gem install --no-document ceedling

FROM common AS linux-driver
RUN apt-get update && apt-get install -y --no-install-recommends \
    bc bison flex cpio rsync kmod \
    libssl-dev libelf-dev libncurses-dev dwarves \
    sparse coccinelle \
    qemu-system-x86 qemu-system-arm qemu-utils \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/linux-driver.txt /tmp/requirements-linux-driver.txt
RUN /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-linux-driver.txt

FROM common AS fpga
RUN apt-get update && apt-get install -y --no-install-recommends \
    iverilog verilator yosys ghdl gtkwave z3 boolector \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/fpga.txt /tmp/requirements-fpga.txt
RUN /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-fpga.txt


FROM common AS qt-hmi
RUN apt-get update && apt-get install -y --no-install-recommends \
    qt6-base-dev qt6-tools-dev-tools libgl1 libegl1 libxkbcommon-x11-0 libxcb-xinerama0 libxcb-cursor0 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/qt-hmi.txt /tmp/requirements-qt-hmi.txt
RUN /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-qt-hmi.txt

FROM common AS edge-ai
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopencv-dev python3-opencv \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/edge-ai.txt /tmp/requirements-edge-ai.txt
RUN /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-edge-ai.txt
# The host onboarding image has no CUDA. Install the CPU wheel explicitly so the
# default CUDA build does not add multiple GB of unusable runtime.
RUN /opt/embedded-venv/bin/pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu torch

FROM edge-ai AS jetson
RUN apt-get update && apt-get install -y --no-install-recommends \
    gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/jetson.txt /tmp/requirements-jetson.txt
RUN /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-jetson.txt
# This is a host-side onboarding image only. It intentionally contains no JetPack, CUDA,
# TensorRT, DeepStream, device mapping or privileged runtime.

FROM common AS system-modeling
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsystemc-dev octave ngspice \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/system-modeling.txt /tmp/requirements-system-modeling.txt
RUN /opt/embedded-venv/bin/pip install --no-cache-dir -r /tmp/requirements-system-modeling.txt

FROM common AS all
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-arm-none-eabi libnewlib-arm-none-eabi openocd qemu-system-arm qemu-system-misc device-tree-compiler ruby-full \
    gcc-avr avr-libc avrdude simavr \
    bc bison flex cpio rsync kmod libssl-dev libelf-dev libncurses-dev dwarves sparse coccinelle qemu-system-x86 qemu-utils \
    iverilog verilator yosys ghdl gtkwave z3 boolector \
    qt6-base-dev qt6-tools-dev-tools libgl1 libegl1 libxkbcommon-x11-0 libxcb-xinerama0 libxcb-cursor0 libdbus-1-3 \
    libopencv-dev python3-opencv gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    libsystemc-dev octave ngspice \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/firmware.txt env/requirements/linux-driver.txt env/requirements/fpga.txt env/requirements/qt-hmi.txt env/requirements/edge-ai.txt env/requirements/jetson.txt env/requirements/system-modeling.txt /tmp/requirements/
RUN /opt/embedded-venv/bin/pip install --no-cache-dir \
      -r /tmp/requirements/firmware.txt \
      -r /tmp/requirements/linux-driver.txt \
      -r /tmp/requirements/fpga.txt \
      -r /tmp/requirements/qt-hmi.txt \
      -r /tmp/requirements/edge-ai.txt \
      -r /tmp/requirements/jetson.txt \
      -r /tmp/requirements/system-modeling.txt \
    && gem install --no-document ceedling
# The host onboarding image has no CUDA. Install the CPU wheel explicitly so the
# default CUDA build does not add multiple GB of unusable runtime.
RUN /opt/embedded-venv/bin/pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu torch

# ---------------------------------------------------------------------------
# R300 AI Motor: firmware (STM32F411 + ATmega128A) + qt-hmi + edge-ai + jetson.
# This is the project's active profile set. The `all` target above stays
# available but additionally carries FPGA, kernel and modeling toolchains that
# this project does not use.
# ---------------------------------------------------------------------------
FROM common AS r300
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc-arm-none-eabi libnewlib-arm-none-eabi openocd qemu-system-arm ruby-full \
    gcc-avr avr-libc avrdude simavr \
    qt6-base-dev qt6-tools-dev-tools libgl1 libegl1 libxkbcommon-x11-0 libxcb-xinerama0 libxcb-cursor0 libdbus-1-3 \
    libopencv-dev python3-opencv gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    && rm -rf /var/lib/apt/lists/*
COPY env/requirements/firmware.txt env/requirements/qt-hmi.txt env/requirements/edge-ai.txt env/requirements/jetson.txt /tmp/requirements/
RUN /opt/embedded-venv/bin/pip install --no-cache-dir \
      -r /tmp/requirements/firmware.txt \
      -r /tmp/requirements/qt-hmi.txt \
      -r /tmp/requirements/edge-ai.txt \
      -r /tmp/requirements/jetson.txt \
    && gem install --no-document ceedling
# The host onboarding image has no CUDA. Install the CPU wheel explicitly so the
# default CUDA build does not add multiple GB of unusable runtime.
RUN /opt/embedded-venv/bin/pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cpu torch
# No JetPack, CUDA, TensorRT or device mapping. TensorRT engines are built on the
# Jetson Orin Nano itself; this image only produces the PyTorch and ONNX stages.

# USB/JTAG devices, privileged mode, host kernel modules and production credentials are
# intentionally not mapped into any image. Add a reviewed, least-privilege run configuration
# only for an approved HIL worker.
