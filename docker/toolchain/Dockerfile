# Toolchain image for SiliconFlow CI firmware jobs: the host toolchain, plus
# GNU Arm Embedded (arm-none-eabi-gcc, newlib-nano) for the cortex-m4 profile.
# Contains only open-source tools, so the published package may be public.
#
# Base is pinned by digest (ubuntu:24.04); Python tools are pinned by version.
# Apt packages follow the noble archive at build time; CI pins the *built*
# image by digest, so a given workflow run always sees the same tool versions.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      binutils \
      binutils-arm-none-eabi \
      ca-certificates \
      cmake \
      cppcheck \
      curl \
      gcc \
      gcc-arm-none-eabi \
      git \
      libc6-dev \
      libnewlib-arm-none-eabi \
      ninja-build \
      python3 \
      python3-pip \
      python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Python tools in a venv (Ubuntu 24.04's system pip is externally managed).
ENV VIRTUAL_ENV=/opt/sfci/venv
ENV PATH=/opt/sfci/venv/bin:$PATH
COPY requirements.txt /opt/sfci/requirements.txt
RUN python3 -m venv "$VIRTUAL_ENV" \
 && pip install --no-cache-dir -r /opt/sfci/requirements.txt

# Actions checks out the workspace as the runner user while container steps run
# as root; without this, `git describe` refuses the repo ("dubious ownership").
RUN git config --system --add safe.directory '*'

RUN gcc --version | head -1 \
 && arm-none-eabi-gcc --version | head -1 \
 && test -f "$(arm-none-eabi-gcc -print-file-name=nano.specs)" \
 && cmake --version | head -1 \
 && ninja --version \
 && cppcheck --version \
 && curl --version | head -1 \
 && gcovr --version | head -1 \
 && pytest --version
