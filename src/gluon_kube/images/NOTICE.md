# Third-party files

These files are copied unchanged from upstream container images, so gluon-kube's stand-in
images behave like the real ones. They keep their own licences, not gluon-kube's MIT.

| Files | From | Licence |
| --- | --- | --- |
| `postgres/docker-entrypoint.sh` | the official `postgres` image ([docker-library/postgres](https://github.com/docker-library/postgres)) | MIT, Copyright (c) 2014, Docker PostgreSQL Authors |
| `redis/docker-entrypoint.sh` | the official `redis` image ([docker-library/redis](https://github.com/docker-library/redis)) | BSD-3-Clause, Copyright (c) 2014 Docker, Inc. |
| `connect/docker-entrypoint.sh`, `connect/kafka-config/` | the Debezium Connect image ([debezium/container-images](https://github.com/debezium/container-images)) | MIT |
| `kafka/etc-kafka-docker/`, `kafka/opt-kafka-config/` | the `apache/kafka` image ([apache/kafka](https://github.com/apache/kafka)) | Apache-2.0 (headers in the files) |
