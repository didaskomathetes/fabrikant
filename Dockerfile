# fabrik server image (ADR 0069): the published wheel + git, data in the /data volume.
#
#   docker build -t fabrik .
#   docker run -p 8321:8321 -v fabrik-data:/data fabrik
#   docker exec -it <container> fabrik user add admin admin --password-stdin   # first login (FABRIK_AUTH=local)
#
# Plugins: FROM this image, then `pip install my-fabrik-steps` (docs/plugins.md).
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:${PYTHON_VERSION}-slim
# git: fabrik drives worktrees/branches by argument list; ca-certificates: HTTPS to hosting providers.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home fabrik \
    && mkdir /data && chown fabrik /data
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
USER fabrik
ENV FABRIK_HOME=/data \
    FABRIK_AUTH=local \
    PYTHONUNBUFFERED=1
VOLUME /data
WORKDIR /data
EXPOSE 8321
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8321/', timeout=4)"
ENTRYPOINT ["fabrik"]
CMD ["serve", "--host", "0.0.0.0"]
