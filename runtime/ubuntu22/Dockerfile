FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git openssh-client sudo \
    python3 python3-pip python3-venv python3-dev \
    nodejs npm build-essential \
    nano vim-tiny procps psmisc htop tmux \
    iproute2 iputils-ping dnsutils unzip zip jq \
    && rm -rf /var/lib/apt/lists/*
RUN groupadd --gid 1000 noir; \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash noir; \
    usermod --home /home/noir --shell /bin/bash root; \
    mkdir -p /home/noir; \
    chown 1000:1000 /home/noir
COPY sshx /usr/local/bin/sshx
COPY noir-sshx /usr/local/bin/noir-sshx
RUN chmod 0755 /usr/local/bin/sshx /usr/local/bin/noir-sshx
ENV HOME=/home/noir \
    USER=root \
    LOGNAME=root \
    NPM_CONFIG_PREFIX=/home/noir/.npm-global \
    PATH=/home/noir/.npm-global/bin:/home/noir/.local/bin:$PATH
USER 0:0
WORKDIR /home/noir
CMD ["/usr/bin/sleep", "infinity"]
