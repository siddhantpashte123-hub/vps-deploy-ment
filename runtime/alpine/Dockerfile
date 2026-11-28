FROM alpine:3.20
RUN apk add --no-cache \
    bash ca-certificates curl wget git openssh-client sudo shadow \
    python3 py3-pip nodejs npm build-base \
    nano vim procps psmisc htop tmux \
    iproute2 iputils bind-tools unzip zip jq
RUN addgroup -g 1000 noir; \
    adduser -D -u 1000 -G noir -h /home/noir -s /bin/bash noir; \
    sed -i 's|^root:x:0:0:root:/root:/bin/.*|root:x:0:0:root:/home/noir:/bin/bash|' /etc/passwd; \
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
CMD ["/bin/sleep", "infinity"]
