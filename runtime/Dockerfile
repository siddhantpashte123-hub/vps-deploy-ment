FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl wget git openssh-client sudo \
    python3 python3-pip python3-venv python3-dev \
    nodejs npm build-essential \
    nano vim-tiny procps psmisc htop tmux \
    iproute2 iputils-ping dnsutils unzip zip jq \
    && rm -rf /var/lib/apt/lists/*
RUN userdel -r ubuntu 2>/dev/null || true; \
    groupdel ubuntu 2>/dev/null || true; \
    groupadd --gid 1000 fier; \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash fier; \
    usermod --home /home/fier --shell /bin/bash root; \
    mkdir -p /home/fier; \
    chown 1000:1000 /home/fier
COPY sshx /usr/local/bin/sshx
COPY fier-sshx /usr/local/bin/fier-sshx
RUN chmod 0755 /usr/local/bin/sshx /usr/local/bin/fier-sshx
ENV HOME=/home/fier \
    USER=root \
    LOGNAME=root \
    NPM_CONFIG_PREFIX=/home/fier/.npm-global \
    PATH=/home/fier/.npm-global/bin:/home/fier/.local/bin:$PATH
USER 0:0
WORKDIR /home/fier
CMD ["/usr/bin/sleep", "infinity"]
