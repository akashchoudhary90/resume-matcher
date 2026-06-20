# Caddy with the Hostinger DNS-01 plugin baked in, so the demo can obtain a TRUSTED Let's Encrypt
# certificate for schulich.edufund.ca via a DNS TXT challenge — NO port 80/443 required, so the
# trading stack's Caddy on 80/443 is never touched. Caddy also auto-renews the cert via the same API.
#
# Plugin: github.com/sbrunk/caddy-dns-hostinger  (provider name: "hostinger"; token: HOSTINGER_API_TOKEN)
# Pinned for reproducibility.

FROM caddy:2-builder AS build
RUN xcaddy build --with github.com/sbrunk/caddy-dns-hostinger@v0.1.2

FROM caddy:2-alpine
COPY --from=build /usr/bin/caddy /usr/bin/caddy
