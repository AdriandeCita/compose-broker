# compose-broker sandbox image (SPEC 5.9, SBX-2).
#
# Built only by `compose-broker sandbox build`. That command downloads the
# binaries listed in manifest.json, verifies each against its pinned sha256,
# and builds this file from a broker-owned directory. The image contains
# nothing but those three static binaries and the host's CA bundle (the
# client verifies registry TLS itself): no shell, no libraries, no
# credentials.
FROM scratch
COPY ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY docker /usr/local/bin/docker
COPY docker-compose docker-buildx /usr/libexec/docker/cli-plugins/
ENTRYPOINT ["/usr/local/bin/docker"]
