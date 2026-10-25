FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash build-essential ca-certificates cmake curl git iproute2 iputils-ping \
    iperf3 ethtool kmod libssl-dev libgmp-dev libboost-all-dev libopenmpi-dev \
    openmpi-bin openssh-client openssh-server pkg-config python3 python3-dev \
    python3-pip python3-venv rsync sudo unzip wget xz-utils zip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /work/datafedsort /run/benchmark-ssh \
    && python3 -m pip install --no-cache-dir 'numpy>=1.24,<2'
COPY containers/entrypoint.sh /usr/local/bin/datafedsort-entrypoint
RUN chmod 755 /usr/local/bin/datafedsort-entrypoint
WORKDIR /artifact
ENTRYPOINT ["/usr/local/bin/datafedsort-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-e"]
