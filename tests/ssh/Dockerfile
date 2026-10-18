# Test-only sshd image. The CI job copies an ephemeral public key to
# authorized_keys before building.
FROM alpine:3.20

RUN apk add --no-cache openssh-server \
    && adduser -D -s /bin/sh dbq \
    && passwd -u dbq \
    && ssh-keygen -A

COPY authorized_keys /home/dbq/.ssh/authorized_keys
COPY sshd_config /etc/ssh/sshd_config

RUN chown -R dbq:dbq /home/dbq/.ssh \
    && chmod 700 /home/dbq/.ssh \
    && chmod 600 /home/dbq/.ssh/authorized_keys

EXPOSE 2222

CMD ["sh", "-c", "mkdir -p /run/sshd && exec /usr/sbin/sshd -D -e"]
