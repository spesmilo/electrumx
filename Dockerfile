FROM python:3.8.3 

ENV GOSU_VERSION 1.12
ENV GOSU_ARCH amd64
ENV GOSU_URL https://github.com/tianon/gosu/releases/download
ENV GOSU_APP ${GOSU_URL}/${GOSU_VERSION}/gosu-${GOSU_ARCH}
ENV GOSU_ASC ${GOSU_URL}/${GOSU_VERSION}/gosu-${GOSU_ARCH}.asc


RUN set -x \
    && useradd -m -s /sbin/nologin electrumx \ 
    && chown electrumx:electrumx /home/electrumx \
    && curl -o /usr/local/bin/gosu -SL ${GOSU_APP} \
    && curl -o /usr/local/bin/gosu.asc -SL ${GOSU_ASC} \
    && export GNUPGHOME="$(mktemp -d)" \
    && gpg --keyserver ha.pool.sks-keyservers.net --recv-keys \
         B42F6819007F00F88E364FD4036A9C25BF357DD4 \
    && gpg --batch --verify /usr/local/bin/gosu.asc /usr/local/bin/gosu \
    && rm -rf "$GNUPGHOME" /usr/local/bin/gosu.asc \
    && chmod +x /usr/local/bin/gosu \
    && gosu nobody true

COPY . /electrumx
COPY docker/docker-entrypoint.sh /

RUN set -x \
    && cd /electrumx \
    && pip install .[ujson]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["electrumx_server"]
